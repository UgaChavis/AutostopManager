"""Store HTTP budgets and classifications with synthetic responses or loopback only."""

from __future__ import annotations

import base64
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import socket
import threading
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import httpx
import pytest

from autostop_manager import store_api
from autostop_manager.storage import StoreState
from autostop_manager.store_api import StoreApiClient
from autostop_manager.store_integration import StoreIntegration


class Response(BytesIO):
    def __init__(self, payload, *, content_type="application/json"):
        super().__init__(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self.headers = {"Content-Type": content_type}


def envelope(*, item=None, ok=True, summary=None):
    return {
        "ok": ok,
        "format": "store_agent_v1",
        "status": "completed" if ok else "conflict",
        "summary": summary or {},
        "items": [] if item is None else [item],
        "changes": [],
        "page": {},
        "warnings": [],
        "meta": {},
    }


def client(url="http://store.synthetic.invalid", **options):
    return StoreApiClient(
        api_url=url,
        read_token="synthetic-read-token",
        manage_token="synthetic-manage-token",
        quote_token="synthetic-quote-token",
        **options,
    )


def call_surface(api, surface):
    if surface == "photo":
        return api.quote_vin_photo_preview(quote_request_id="quote-1", expected_photo_sha256="c" * 64)
    if surface == "post":
        return api.management_action(
            operation="set_batch_storage_location",
            target_id="batch-1",
            expected_updated_at="version-1",
            owner_intent="Synthetic instruction for the exact test batch",
            idempotency_key="synthetic-http-budget-write",
            correlation_id="contract:synthetic-http-budget-write",
            mode="apply",
            planned_changes={"storage_location": "B-2"},
        )
    return api.search(entity="store_part")


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    """Allow real local fixtures and asyncio's Windows self-pipe, never Store/DNS."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def check(address):
        assert isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}, "external network forbidden"

    def connect(sock, address):
        check(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(address)
        return original_connect_ex(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        assert host in {"127.0.0.1", "::1", b"127.0.0.1", b"::1"}, "external DNS forbidden"
        return original_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,::1")
    monkeypatch.setenv("no_proxy", "127.0.0.1,::1")


@pytest.fixture
def local_store():
    servers = []

    def start(respond):
        state = SimpleNamespace(
            requests=[], entered=threading.Event(), stopped=threading.Event(), threads=[], errors=[]
        )

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def handle_request(self):
                state.threads.append(threading.current_thread())
                size = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(size) if size else None
                state.requests.append((self.command, self.path, self.headers.get("Authorization"), body))
                try:
                    respond(self, state)
                except OSError:
                    pass  # Deadline cancellation closes the peer while a fixture is writing.
                except Exception as error:  # noqa: BLE001 - surface fixture assertions in the owning test.
                    state.errors.append(error)
                finally:
                    self.close_connection = True

            do_GET = handle_request
            do_POST = handle_request

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        state.url = f"http://127.0.0.1:{server.server_port}"
        servers.append((server, thread, state))
        thread.start()
        return state

    yield start
    for server, thread, state in servers:
        state.stopped.set()
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()
        for handler in state.threads:
            handler.join(3)
            assert not handler.is_alive()
        assert not state.errors


def send_response(handler, body, *, status=200, content_type="application/json", location=None):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    if location is not None:
        handler.send_header("Location", location)
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()


def trickle(handler, state, *, phase, status=200):
    if phase == "headers":
        handler.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
    elif phase != "no_headers":
        handler.send_response(status)
        handler.send_header("Content-Type", "image/jpeg" if "vin-photo-preview" in handler.path else "application/json")
        handler.send_header("Content-Length", "10000000")
        handler.end_headers()
    state.entered.set()
    # Keep the response incomplete until the caller has returned. Continuous
    # data avoids an idle timeout; the wide watchdog below catches missing total budgets.
    while not state.stopped.wait(0.01):
        if phase != "no_headers":
            handler.connection.sendall(b"x")


def call_while_response_incomplete(state, operation):
    finished = threading.Event()
    outcome = []

    def call():
        try:
            outcome.append(operation())
        finally:
            finished.set()

    thread = threading.Thread(target=call, name="synthetic-store-caller")
    thread.start()
    try:
        assert state.entered.wait(3), "The HTTP fixture never reached its controlled slow phase"
        assert finished.wait(5), "Store caller did not finish while the response remained incomplete"
        assert len(outcome) == 1
        return outcome[0]
    finally:
        state.stopped.set()
        thread.join(5)
        assert not thread.is_alive()


@pytest.mark.parametrize("surface", ["json", "photo"])
def test_get_retries_share_one_timeout_budget(monkeypatch, surface):
    now = [100.0]
    calls = []
    monkeypatch.setattr(store_api, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def urlopen(request, *, timeout):
        calls.append((request.full_url, request.get_header("Authorization"), timeout))
        now[0] += 0.3
        if len(calls) < 3:
            raise URLError("synthetic temporary failure")
        return Response(b"jpeg", content_type="image/jpeg") if surface == "photo" else Response(envelope())

    monkeypatch.setattr(store_api, "urlopen", urlopen)
    result = call_surface(client(timeout=1, max_read_attempts=3), surface)
    assert result["ok"] is True and result["meta"]["attempt_count"] == 3
    assert [row[2] for row in calls] == pytest.approx([1, 0.7, 0.4])
    assert all(row[:2] == calls[0][:2] for row in calls)


@pytest.mark.parametrize("surface", ["json", "photo"])
def test_expired_budget_does_not_dispatch_another_get(monkeypatch, surface):
    now = [100.0]
    calls = []
    monkeypatch.setattr(store_api, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def urlopen(request, *, timeout):
        calls.append(request)
        now[0] += timeout + 0.01
        raise URLError("synthetic temporary failure")

    monkeypatch.setattr(store_api, "urlopen", urlopen)
    result = call_surface(client(timeout=1, max_read_attempts=3), surface)
    assert result["ok"] is False
    assert result["meta"]["attempt_count"] == 1
    assert result["meta"]["request_dispatched"] is True
    assert len(calls) == 1


@pytest.mark.parametrize("surface", ["json", "photo"])
@pytest.mark.parametrize("phase", ["no_headers", "headers", "body"])
def test_real_store_deadline_ends_without_waiting_for_headers_or_body_completion(local_store, surface, phase):
    state = local_store(lambda handler, fixture: trickle(handler, fixture, phase=phase))
    result = call_while_response_incomplete(
        state, lambda: call_surface(client(state.url, timeout=0.8, max_read_attempts=3), surface)
    )
    assert result["ok"] is False
    expected = "store_attachment_timeout_or_network_error" if surface == "photo" else "store_timeout_or_network_error"
    assert result["summary"]["error_code"] == expected
    assert result["meta"]["attempt_count"] == 1
    assert len(state.requests) == 1


@pytest.mark.parametrize("surface", ["json", "photo", "post"])
@pytest.mark.parametrize("status", [401, 403, 429])
def test_known_http_rejection_survives_a_body_that_never_finishes(local_store, surface, status):
    state = local_store(lambda handler, fixture: trickle(handler, fixture, phase="body", status=status))
    api = client(state.url, timeout=0.8, max_read_attempts=3, failure_threshold=1)
    result = call_while_response_incomplete(state, lambda: call_surface(api, surface))
    assert result["ok"] is False and result["status"] == "blocked"
    assert result["meta"]["http_status"] == status
    assert result["meta"]["attempt_count"] == 1
    assert result["meta"]["outcome_uncertain"] is False
    assert api.local_status()["consecutive_failures"] == 0
    assert not api.local_status()["circuit_open"]
    assert len(state.requests) == 1


@pytest.mark.parametrize("status", [401, 403, 404, 409, 429])
def test_photo_client_does_not_retry_known_http_rejections(monkeypatch, status):
    calls = []
    errors = []

    def urlopen(request, *, timeout):
        calls.append(request)
        error = HTTPError(request.full_url, status, "synthetic rejection", {}, BytesIO(b"not-json"))
        errors.append(error)
        raise error

    monkeypatch.setattr(store_api, "urlopen", urlopen)
    api = client(max_read_attempts=3, failure_threshold=1)
    result = call_surface(api, "photo")
    assert result["ok"] is False
    assert result["meta"]["http_status"] == status
    assert result["meta"]["attempt_count"] == 1
    assert result["status"] == ("conflict" if status == 409 else "blocked")
    assert len(calls) == 1 and all(error.closed for error in errors)
    assert not api.local_status()["circuit_open"]


def test_photo_503_retries_with_remaining_budget_and_resets_failures_on_success(monkeypatch):
    now = [100.0]
    calls = []
    monkeypatch.setattr(store_api, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def urlopen(request, *, timeout):
        calls.append(timeout)
        now[0] += 0.25
        if len(calls) == 1:
            raise HTTPError(request.full_url, 503, "synthetic unavailable", {}, BytesIO(b""))
        return Response(b"jpeg", content_type="image/jpeg")

    monkeypatch.setattr(store_api, "urlopen", urlopen)
    api = client(timeout=1, max_read_attempts=3, failure_threshold=1)
    result = call_surface(api, "photo")
    assert result["ok"] is True and result["meta"]["attempt_count"] == 2
    assert calls == pytest.approx([1, 0.75])
    assert api.local_status()["consecutive_failures"] == 0


def test_repeated_photo_503_degrades_and_opens_circuit(monkeypatch):
    calls = []

    def urlopen(request, *, timeout):
        calls.append(request)
        raise HTTPError(request.full_url, 503, "synthetic unavailable", {}, BytesIO(b""))

    monkeypatch.setattr(store_api, "urlopen", urlopen)
    api = client(max_read_attempts=2, failure_threshold=2, circuit_cooldown_seconds=60)
    for failure in (1, 2):
        result = call_surface(api, "photo")
        assert result["ok"] is False and result["status"] == "degraded"
        assert result["summary"]["error_code"] == "store_attachment_http_503"
        assert result["meta"]["http_status"] == 503
        assert result["meta"]["attempt_count"] == 2
        assert api.local_status()["consecutive_failures"] == failure
    assert call_surface(api, "photo")["summary"]["error_code"] == "store_circuit_open"
    assert len(calls) == 4


@pytest.mark.parametrize("surface", ["json", "photo", "post"])
@pytest.mark.parametrize("reason", ["provider_transport_busy", "provider_transport_unavailable"])
def test_local_capacity_refusal_is_pre_dispatch_without_retry_or_circuit_failure(monkeypatch, surface, reason):
    calls = []

    def urlopen(request, *, timeout):
        calls.append(request)
        raise URLError(reason)

    monkeypatch.setattr(store_api, "urlopen", urlopen)
    api = client(max_read_attempts=3, failure_threshold=1)
    result = call_surface(api, surface)
    assert result["summary"]["error_code"] == reason.replace("provider_", "store_")
    assert result["meta"]["request_dispatched"] is False
    assert result["meta"]["outcome_uncertain"] is False
    assert result["meta"]["attempt_count"] == 0
    assert len(calls) == 1
    assert api.local_status()["consecutive_failures"] == 0
    assert not api.local_status()["circuit_open"]


@pytest.mark.parametrize("surface", ["json", "post"])
def test_malformed_port_is_classified_before_dispatch_without_retry(monkeypatch, surface):
    sends = []

    async def reject_send(_client, request, **_kwargs):
        sends.append(request)
        raise AssertionError("An invalid Store URL must not reach HTTP transport")

    monkeypatch.setattr(httpx.AsyncClient, "send", reject_send)
    api = client("http://localhost:bad/internal/agent/v1", max_read_attempts=3, failure_threshold=1)
    result = call_surface(api, surface)
    assert result["ok"] is False
    assert result["summary"]["error_code"] == "store_request_invalid"
    assert result["meta"]["request_dispatched"] is False
    assert result["meta"]["outcome_uncertain"] is False
    assert result["meta"]["attempt_count"] == 0
    assert sends == []
    assert api.local_status()["consecutive_failures"] == 0
    assert not api.local_status()["circuit_open"]


def test_real_error_envelope_larger_than_provider_default_is_preserved(local_store):
    payload = envelope(
        ok=False,
        summary={
            "error_code": "store_expected_updated_at_mismatch",
            "message": "synthetic " * 900,
            "details": {"current_updated_at": "version-2"},
        },
    )
    body = json.dumps(payload).encode()
    assert 4096 < len(body) < 16000
    state = local_store(lambda handler, _fixture: send_response(handler, body, status=409))
    result = call_surface(client(state.url, timeout=3, max_response_bytes=16000), "post")
    assert result["ok"] is False and result["status"] == "conflict"
    assert result["summary"] == payload["summary"]
    assert result["meta"]["http_status"] == 409
    assert result["meta"]["outcome_uncertain"] is False
    assert len(state.requests) == 1


@pytest.mark.parametrize("extra", [-1, 0, 1])
def test_custom_json_limit_above_default_is_preserved_through_real_transport(local_store, extra):
    maximum = 1_100_000
    payload = envelope(item={"entity": "store_part", "id": "part-1", "name": ""})
    payload["items"][0]["name"] = "x" * (maximum + extra - len(json.dumps(payload).encode()))
    body = json.dumps(payload).encode()
    assert len(body) == maximum + extra
    state = local_store(lambda handler, _fixture: send_response(handler, body))
    result = call_surface(client(state.url, timeout=3, max_response_bytes=maximum), "json")
    if extra <= 0:
        assert result["ok"] is True and result["items"] == payload["items"]
    else:
        assert result["ok"] is False
        assert result["summary"]["error_code"] == "store_response_too_large"
    assert len(state.requests) == 1


@pytest.mark.parametrize("extra", [0, 1])
def test_real_photo_keeps_its_two_megabyte_limit_separate_from_json_limit(local_store, extra):
    maximum = store_api.MAX_QUOTE_VIN_PHOTO_PREVIEW_BYTES
    body = b"\xff\xd8" + b"x" * (maximum + extra - 4) + b"\xff\xd9"
    state = local_store(lambda handler, _fixture: send_response(handler, body, content_type="image/jpeg"))
    result = call_surface(client(state.url, timeout=3, max_response_bytes=4096), "photo")
    if extra == 0:
        assert result["ok"] is True
        assert base64.b64decode(result["data"]["content_base64"]) == body
    else:
        assert result["ok"] is False
        assert result["summary"]["error_code"] == "store_attachment_response_invalid"
    assert len(state.requests) == 1


@pytest.mark.parametrize("surface", ["json", "photo", "post"])
@pytest.mark.parametrize("redirect_kind", ["valid", "invalid_port", "long_relative"])
def test_real_redirect_does_not_forward_store_credentials_or_repeat(local_store, surface, redirect_kind):
    target = local_store(lambda handler, _fixture: send_response(handler, b"{}"))
    location = {"valid": target.url, "invalid_port": "http://127.0.0.1:bad/", "long_relative": "a" * 65530}[
        redirect_kind
    ]
    source = local_store(lambda handler, _fixture: send_response(handler, b"", status=302, location=location))
    result = call_surface(client(source.url, timeout=3, max_read_attempts=3), surface)
    assert result["ok"] is False and result["meta"]["http_status"] == 302
    expected_code = "store_attachment_request_rejected" if surface == "photo" else "store_redirect_rejected"
    assert result["summary"]["error_code"] == expected_code
    assert result["meta"]["attempt_count"] == 1
    assert result["meta"]["request_dispatched"] is True
    assert result["meta"]["outcome_uncertain"] is (surface == "post")
    assert len(source.requests) == 1 and target.requests == []
    expected_token = {"json": "read", "photo": "quote", "post": "manage"}[surface]
    assert source.requests[0][2] == f"Bearer synthetic-{expected_token}-token"
    assert "synthetic-" not in json.dumps(result)


def test_expired_post_budget_keeps_one_write_and_performs_exact_uncertain_readback(local_store, tmp_path):
    before = {"entity": "store_batch", "id": "batch-1", "storage_location": "A-1", "updated_at": "version-1"}
    after = {**before, "storage_location": "B-2", "updated_at": "version-2"}
    written = threading.Event()

    def respond(handler, state):
        if handler.command == "POST":
            written.set()
            trickle(handler, state, phase="body")
        else:
            payload = envelope(item=after if written.is_set() else before)
            send_response(handler, json.dumps(payload).encode())

    state = local_store(respond)
    arguments = {
        "domain": "store_batch",
        "action": "set_batch_storage_location",
        "target_id": "batch-1",
        "expected_updated_at": "version-1",
        "owner_intent": "Synthetic instruction for this exact batch",
        "idempotency_key": "synthetic-http-uncertain-write",
        "correlation_id": "contract:synthetic-http-uncertain-write",
        "mode": "apply",
        "planned_changes": {"storage_location": "B-2"},
    }
    original = deepcopy(arguments)
    integration = StoreIntegration(client=client(state.url, timeout=0.8), store=StoreState(tmp_path / "unused.sqlite3"))
    result = call_while_response_incomplete(state, lambda: integration.management_action(**arguments))
    assert result["ok"] is False and result["status"] == "compensating"
    assert result["summary"]["error_code"] == "store_apply_outcome_uncertain"
    assert result["meta"]["outcome_uncertain"] is True
    assert result["meta"]["post_state_read"] is True
    assert result["summary"]["failed_fields"] == []
    assert [request[0] for request in state.requests] == ["GET", "POST", "GET"]
    posted = json.loads(state.requests[1][3])
    for field in ("target_id", "expected_updated_at", "idempotency_key", "correlation_id", "mode", "planned_changes"):
        assert posted[field] == original[field]
    assert arguments == original


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_store_deadline_is_rejected_before_transport(timeout):
    with pytest.raises(ValueError, match="finite"):
        client(timeout=timeout)
