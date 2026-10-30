"""Owner HTTP deadlines and worker cleanup with synthetic loopback responses."""

from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
import time
from types import SimpleNamespace
from urllib.request import Request

import pytest
import httpx

from autostop_manager import bounded_http_read, store_owner_api as owner
from autostop_manager.bounded_http_read import TrackedTransportAdmission
from autostop_manager.listing_executor import BoundedListingExecutor
from autostop_manager.mcp_store_executor import StoreWorkers, offload_store
from autostop_manager.store_api import store_transport_admission


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    getaddrinfo = socket.getaddrinfo

    def checked(address):
        assert isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}, "external network forbidden"

    def local_connect(sock, address):
        checked(address)
        return connect(sock, address)

    def local_connect_ex(sock, address):
        checked(address)
        return connect_ex(sock, address)

    def local_getaddrinfo(host, *args, **kwargs):
        assert host in {"127.0.0.1", "::1", b"127.0.0.1", b"::1"}, "external DNS forbidden"
        return getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", local_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", local_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", local_getaddrinfo)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,::1")
    monkeypatch.setenv("no_proxy", "127.0.0.1,::1")


@pytest.fixture
def local_owner():
    servers = []

    def start(respond):
        state = SimpleNamespace(
            requests=[], entered=threading.Event(), stopped=threading.Event(), closed=threading.Event(), threads=[]
        )

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def handle_request(self):
                state.threads.append(threading.current_thread())
                size = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(size) if size else None
                state.requests.append((self.command, self.path, self.headers, body))
                try:
                    respond(self, state)
                except OSError:
                    state.closed.set()
                finally:
                    self.close_connection = True

            do_GET = handle_request
            do_PATCH = handle_request

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
        thread.join(2)
        assert not thread.is_alive()
        for handler in state.threads:
            handler.join(2)
            assert not handler.is_alive()


def client(url, *, write=False):
    api = owner.StoreOwnerApiClient(agent_api_url=url, owner_token="synthetic-owner-token", timeout=1)
    capability = owner.OwnerCapability(
        "synthetic_operation",
        "PATCH" if write else "GET",
        "/api/v1/parts/part-1",
        "write" if write else "read",
        (),
        False,
        (),
    )
    api._capabilities = {capability.operation_id: capability}
    api._cached_at = time.monotonic()
    return api


def invoke(api, stage):
    if stage == "openapi":
        api._capabilities.clear()
        return api.list_capabilities()
    return api.invoke(
        operation_id="synthetic_operation",
        mode=stage,
        expected_revision="synthetic-revision",
        owner_intent="Synthetic owner transport regression",
        idempotency_key="synthetic-owner-key",
        correlation_id="synthetic-owner-correlation",
        dry_run_proof="a" * 64,
    )


def trickle(handler, state, *, phase, status=200):
    if phase == "headers":
        handler.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
    else:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", "10000000")
        handler.end_headers()
    state.entered.set()
    while not state.stopped.wait(0.01):
        handler.connection.sendall(b"x")


def bounded_call(state, operation):
    outcome = []
    finished = threading.Event()

    def call():
        try:
            outcome.append(operation())
        finally:
            finished.set()

    thread = threading.Thread(target=call)
    start = time.monotonic()
    thread.start()
    try:
        assert state.entered.wait(3)
        assert finished.wait(3), "Owner HTTP caller waited indefinitely for a trickling response"
        assert time.monotonic() - start < 2.5
        assert len(outcome) == 1
        assert state.closed.wait(1), "The deadline must close the owned HTTP stream"
        return outcome[0]
    finally:
        state.stopped.set()
        thread.join(3)
        assert not thread.is_alive()


@pytest.mark.parametrize("stage", ["openapi", "revision", "read", "apply"])
@pytest.mark.parametrize("phase", ["headers", "body"])
def test_owner_http_deadline_covers_headers_and_body(local_owner, stage, phase):
    state = local_owner(lambda handler, state: trickle(handler, state, phase=phase))
    result = bounded_call(state, lambda: invoke(client(state.url, write=stage in {"revision", "apply"}), stage))
    assert result["ok"] is False
    assert len(state.requests) == 1
    assert (
        result["error"]["code"]
        == {
            "openapi": "store_owner_openapi_unavailable",
            "revision": "store_owner_revision_read_failed",
            "read": "store_owner_read_failed",
            "apply": "store_owner_outcome_uncertain",
        }[stage]
    )
    assert result["meta"].get("outcome_uncertain", False) is (stage == "apply")
    if stage == "apply":
        assert result["meta"]["readback_required"] is True
        assert result["meta"]["request_dispatched"] is True
        assert state.requests[0][2][owner.IDEMPOTENCY_HEADER] == "synthetic-owner-key"


@pytest.mark.parametrize("stage", ["read", "apply"])
@pytest.mark.parametrize("status", [403, 503])
def test_known_rejection_survives_incomplete_body(local_owner, stage, status):
    state = local_owner(lambda handler, state: trickle(handler, state, phase="body", status=status))
    result = bounded_call(state, lambda: invoke(client(state.url, write=stage == "apply"), stage))
    assert result["meta"]["http_status"] == status
    assert result["meta"]["outcome_uncertain"] is (stage == "apply")
    assert len(state.requests) == 1


@pytest.mark.parametrize("stage", ["openapi", "revision", "read", "apply"])
@pytest.mark.parametrize("reason", ["busy", "unavailable"])
def test_rejected_transport_admission_never_claims_dispatch(monkeypatch, stage, reason):
    gate = TrackedTransportAdmission(1)
    token = store_transport_admission.set(gate)
    if reason == "busy":
        assert gate.acquire()
    else:

        def fail_start(_thread):
            raise RuntimeError("synthetic unavailable thread")

        monkeypatch.setattr(bounded_http_read.threading.Thread, "start", fail_start)
    try:
        result = invoke(client("http://127.0.0.1:9", write=stage in {"revision", "apply"}), stage)
        assert result["error"]["code"] == f"store_owner_transport_{reason}"
        assert result["meta"]["request_dispatched"] is False
        assert result["meta"]["outcome_uncertain"] is False
        assert result["meta"].get("readback_required") is not True
    finally:
        if reason == "busy":
            gate.release()
        store_transport_admission.reset(token)
    assert gate.pending_count == 0


@pytest.mark.parametrize(
    "content_type,allow_binary,expected",
    [
        ("application/json", True, 33),
        ("application/problem+json", True, 33),
        ("image/jpeg", False, 33),
        ("image/jpeg", True, 65),
    ],
)
def test_success_limit_is_selected_before_reading_body(monkeypatch, local_owner, content_type, allow_binary, expected):
    monkeypatch.setattr(owner, "MAX_RESPONSE_BYTES", 32)
    monkeypatch.setattr(owner, "MAX_BINARY_RESPONSE_BYTES", 64)

    def respond(handler, state):
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", "1000000")
        handler.end_headers()
        handler.wfile.write(b"x" * expected)
        handler.wfile.flush()
        state.entered.set()
        state.stopped.wait(2)

    state = local_owner(respond)
    start = time.monotonic()
    with owner._open_response(
        Request(state.url), timeout=1, maximum=32, allow_binary_response=allow_binary
    ) as response:
        assert response.read() == b"x" * expected
    assert time.monotonic() - start < 0.8, "Oversized response must stop on the sentinel byte, without EOF"
    assert len(state.requests) == 1


@pytest.mark.parametrize("stage", ["read", "apply"])
def test_redirect_is_not_followed_even_with_invalid_location(local_owner, stage):
    def respond(handler, _state):
        handler.send_response(302)
        handler.send_header("Location", "http://[invalid")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    state = local_owner(respond)
    result = invoke(client(state.url, write=stage == "apply"), stage)
    assert result["meta"]["http_status"] == 302
    assert result["meta"]["outcome_uncertain"] is (stage == "apply")
    assert len(state.requests) == 1


@pytest.mark.parametrize("abandonment", ["timeout", "cancel"])
def test_abandoned_owner_worker_eventually_drains_its_http_stream(local_owner, abandonment):
    state = local_owner(lambda handler, state: trickle(handler, state, phase="body"))
    api = client(state.url, write=True)
    workers = StoreWorkers(
        reads=BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=0.2),
        stateful=BoundedListingExecutor(
            max_workers=1, max_waiting_calls=0, timeout_seconds=3 if abandonment == "cancel" else 0.2
        ),
    )

    async def scenario():
        operation = offload_store(lambda: invoke(api, "apply"), "store_owner_api", workers)
        task = asyncio.create_task(operation())
        if abandonment == "cancel":
            async with asyncio.timeout(1):
                while not state.entered.is_set():
                    await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result["summary"]["error_code"] == "store_executor_wait_timeout"
            assert result["meta"]["outcome_uncertain"] is True
        pending = workers.status()["pending"]
        assert pending == {"read": 0, "stateful": 1, "transport": 1}
        assert (await operation())["summary"]["error_code"] == "store_executor_busy"
        assert (await workers.drain(2))["ok"] is True
        assert state.closed.wait(1)
        assert len(state.requests) == 1
        workers.close()

    try:
        asyncio.run(scenario())
    finally:
        state.stopped.set()
        workers.reads.close()
        workers.stateful.close()


@pytest.mark.parametrize("allow_binary", [False, True])
def test_error_body_keeps_json_budget_with_binary_opt_in(monkeypatch, local_owner, allow_binary):
    monkeypatch.setattr(owner, "MAX_RESPONSE_BYTES", 32)
    monkeypatch.setattr(owner, "MAX_BINARY_RESPONSE_BYTES", 64)

    def respond(handler, _state):
        handler.send_response(422)
        handler.send_header("Content-Type", "image/jpeg")
        handler.send_header("Content-Length", "40")
        handler.end_headers()
        handler.wfile.write(b"x" * 40)

    state = local_owner(respond)
    with owner._open_response(
        Request(state.url), timeout=1, maximum=32, allow_binary_response=allow_binary
    ) as response:
        assert response.status == 422
        assert response.read() == b"x" * 33


def test_openapi_uses_its_own_larger_budget(monkeypatch, local_owner):
    monkeypatch.setattr(owner, "MAX_RESPONSE_BYTES", 32)
    monkeypatch.setattr(owner, "MAX_OPENAPI_BYTES", 96)
    body = b'{"openapi":"3.1.0","paths":{},"info":{"title":"Synthetic OpenAPI"}}'

    def respond(handler, _state):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    state = local_owner(respond)
    assert client(state.url)._read_openapi()["info"]["title"] == "Synthetic OpenAPI"


def test_invalid_request_is_not_write_uncertainty(monkeypatch):
    def invalid(*_args, **_kwargs):
        raise httpx.InvalidURL("synthetic invalid URL")

    monkeypatch.setattr(owner, "bounded_urlopen", invalid)
    result = invoke(client("http://127.0.0.1:9", write=True), "apply")
    assert result["error"]["code"] == "store_owner_request_invalid"
    assert result["meta"]["request_dispatched"] is False
    assert result["meta"]["outcome_uncertain"] is False
