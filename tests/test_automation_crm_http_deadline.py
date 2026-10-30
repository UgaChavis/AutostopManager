from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import sqlite3
import threading
import time
from types import SimpleNamespace

import anyio
import httpx
import pytest

from autostop_manager import automation_jobs as module
from autostop_manager import automation_registry as registry
from autostop_manager.bounded_http_read import TrackedTransportAdmission
from autostop_manager.config import AutomationCrmConnectionConfig
from tests.test_automation_crm_response_bounds import READINESS
from tests.test_automation_jobs import FakeNotifier, FakeSource, claimed_digest_run


def source(url, *, timeout=1, admission=None):
    return module.HttpCrmDigestSource(
        AutomationCrmConnectionConfig(configured=True, api_url=url, bearer_token="synthetic-only"),
        timeout_seconds=timeout,
        admission=admission,
    )


def invoke(client, operation):
    if operation == "ack":
        return client.acknowledge(ack="exact-original-ack")
    if operation == "summarize":
        return client.summarize(ack="exact-original-ack")
    return getattr(client, operation)()


def wait_idle(admission):
    deadline = time.monotonic() + 3
    while admission.pending_count and time.monotonic() < deadline:
        time.sleep(0.01)
    assert admission.pending_count == 0


@pytest.fixture
def admission(monkeypatch):
    result = TrackedTransportAdmission(1)
    monkeypatch.setattr(module, "_CRM_HTTP_ADMISSION", result)
    yield result
    wait_idle(result)


@pytest.fixture
def endpoint():
    case = SimpleNamespace(mode="body", status=200, calls=[], received=threading.Event(), stop=threading.Event())
    case.release_request = threading.Event()
    case.release_request.set()
    case.readiness_received = threading.Event()
    case.release_readiness = threading.Event()
    case.release_readiness.set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            operation = self.path.rsplit("/", 1)[-1]
            case.calls.append((operation, payload))
            case.received.set()
            if operation == "readiness":
                case.readiness_received.set()
            gate = case.release_readiness if operation == "readiness" else case.release_request
            if not gate.wait(5):
                return
            data = (
                dict(READINESS)
                if operation == "readiness"
                else {
                    "format": module.HttpCrmDigestSource._FORMATS[operation],
                    "consumer_id": module.CRM_DIGEST_CONSUMER_ID,
                    "acked_sequence": 10 if operation == "register" else 15,
                }
            )
            body = b" " * 2048 + json.dumps({"ok": True, "data": data}).encode()
            try:
                if case.mode == "headers":
                    self.wfile.write(b"HTTP/1.0 200 OK\r\n")
                    for _ in range(75):
                        self.wfile.write(b"X-Trickle: yes\r\n")
                        self.wfile.flush()
                        if case.stop.wait(0.04):
                            return
                    self.wfile.write(f"Content-Length: {len(body)}\r\n\r\n".encode())
                else:
                    self.send_response(case.status)
                    self.send_header("Content-Length", str(len(body)))
                    if case.status == 302:
                        self.send_header("Location", "/redirected")
                    self.end_headers()
                for offset in range(0, len(body), 32):
                    self.wfile.write(body[offset : offset + 32])
                    self.wfile.flush()
                    if case.mode == "body" and case.stop.wait(0.04):
                        return
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    case.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield case
    finally:
        case.release_request.set()
        case.release_readiness.set()
        case.stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("phase", ["headers", "body"])
@pytest.mark.parametrize("operation", ["readiness", "register", "read", "summarize", "ack"])
def test_one_deadline_bounds_trickling_response_without_retry(endpoint, admission, phase, operation):
    endpoint.mode = phase
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as caller:
        result = caller.submit(invoke, source(endpoint.url), operation)
        assert endpoint.received.wait(2), "request did not reach the loopback server before the deadline"
        with pytest.raises(module.AutomationJobError) as caught:
            result.result(timeout=2)
    assert time.monotonic() - started < 2
    assert caught.value.code == "automation_crm_transport_failed"
    assert caught.value.outcome_uncertain is (operation in {"register", "ack"})
    wait_idle(admission)
    assert len(endpoint.calls) == 1
    if operation in {"ack", "summarize"}:
        assert endpoint.calls[0][1]["ack"] == "exact-original-ack"


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_http_error_classification_and_redirect_prohibition_stay_unchanged(endpoint, admission, status):
    endpoint.mode, endpoint.status = "fast", status
    with pytest.raises(module.AutomationJobError) as caught:
        source(endpoint.url, timeout=2).readiness()
    assert caught.value.code == "automation_crm_request_failed"
    assert caught.value.outcome_uncertain is False
    assert len(endpoint.calls) == 1


@pytest.mark.parametrize("operation", ["register", "ack"])
def test_full_admission_is_certainly_pre_dispatch(endpoint, admission, operation):
    assert admission.acquire()
    try:
        with pytest.raises(module.AutomationJobError) as caught:
            invoke(source(endpoint.url), operation)
        assert caught.value.outcome_uncertain is False
        assert endpoint.calls == []
    finally:
        admission.release()


@pytest.mark.parametrize("stage", ["construct", "start"])
def test_worker_setup_failure_releases_admission_before_dispatch(endpoint, admission, monkeypatch, stage):
    class BrokenThread:
        def __init__(self, **_kwargs):
            if stage == "construct":
                raise RuntimeError("synthetic thread construction failure")

        def start(self):
            raise RuntimeError("synthetic thread start failure")

    monkeypatch.setattr(
        module, "threading", SimpleNamespace(Thread=BrokenThread, Event=threading.Event, Lock=threading.Lock)
    )
    with pytest.raises(module.AutomationJobError) as caught:
        source(endpoint.url).acknowledge(ack="original")
    assert caught.value.outcome_uncertain is False
    assert admission.pending_count == 0 and endpoint.calls == []


def test_timed_out_client_setup_cannot_dispatch_later(endpoint, admission, monkeypatch):
    release = threading.Event()
    constructing = threading.Event()
    real_client = httpx.AsyncClient

    def client(**kwargs):
        constructing.set()
        assert release.wait(3)
        return real_client(**kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", client)
    try:
        with pytest.raises(module.AutomationJobError) as caught:
            source(endpoint.url, timeout=0.3).acknowledge(ack="original")
        assert constructing.is_set()
        assert caught.value.outcome_uncertain is False
        assert admission.pending_count == 1
        release.set()
        wait_idle(admission)
        assert endpoint.calls == []
    finally:
        release.set()


def test_invalid_url_is_pre_dispatch(admission):
    with pytest.raises(module.AutomationJobError) as caught:
        source("http://127.0.0.1:invalid").register()
    assert caught.value.outcome_uncertain is False
    assert admission.pending_count == 0


def test_capacity_is_retained_until_async_stream_cleanup_finishes(admission, monkeypatch):
    release, closing = threading.Event(), threading.Event()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"{"
            raise httpx.ReadError("synthetic incomplete response")

        async def aclose(self):
            closing.set()
            with anyio.CancelScope(shield=True):
                await asyncio.to_thread(release.wait, 3)

    transport = httpx.MockTransport(lambda _request: httpx.Response(200, stream=Body()))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    try:
        with pytest.raises(module.AutomationJobError) as first:
            source("https://crm.invalid").register()
        assert closing.is_set() and first.value.outcome_uncertain is True
        assert admission.pending_count == 1
        with pytest.raises(module.AutomationJobError) as second:
            source("https://crm.invalid").register()
        assert second.value.outcome_uncertain is False
    finally:
        release.set()
        wait_idle(admission)


def test_capacity_is_retained_until_dns_worker_finishes(endpoint, admission, monkeypatch):
    release, resolving = threading.Event(), threading.Event()
    original = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        if host in {"crm.synthetic", b"crm.synthetic"}:
            resolving.set()
            assert release.wait(3)
            host = "127.0.0.1"
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    try:
        with pytest.raises(module.AutomationJobError) as caught:
            source(endpoint.url.replace("127.0.0.1", "crm.synthetic")).register()
        assert resolving.is_set() and caught.value.outcome_uncertain is True
        assert admission.pending_count == 1
        with pytest.raises(module.AutomationJobError) as second:
            source(endpoint.url).register()
        assert second.value.outcome_uncertain is False
    finally:
        release.set()
        wait_idle(admission)
    assert endpoint.calls == []


def test_abandoned_async_caller_does_not_repeat_ack(endpoint, admission):
    async def scenario():
        task = asyncio.create_task(asyncio.to_thread(source(endpoint.url, timeout=2).acknowledge, ack="original"))
        assert await asyncio.to_thread(endpoint.received.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert admission.pending_count == 1
        await asyncio.to_thread(wait_idle, admission)
        assert endpoint.calls == [("ack", {"consumer_id": module.CRM_DIGEST_CONSUMER_ID, "ack": "original"})]

    asyncio.run(scenario())


@pytest.fixture
def claimed_store(tmp_path, monkeypatch):
    # Keep real schemas, leases, cursors and transactions. Only the POSIX
    # directory ownership gate is replaced for the Windows synthetic run.
    @contextmanager
    def local_connection(store):
        store.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(store.path, timeout=1)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    monkeypatch.setattr(registry.AutomationStore, "connect", local_connection)
    return claimed_digest_run(tmp_path)


def test_register_deadline_stops_executor_before_further_effects(endpoint, admission, claimed_store):
    store, _service, _job, claim = claimed_store
    notifier = FakeNotifier()
    result = module.CrmDigestExecutor(store=store, source=source(endpoint.url), notifier=notifier)(claim)
    assert result == {"ok": False, "result_code": "automation_crm_transport_failed", "outcome_uncertain": True}
    assert [operation for operation, _payload in endpoint.calls] == ["register"]
    assert notifier.applied == []
    assert store.run_claim_is_current(
        job_id=claim["job_id"],
        run_id=claim["run_id"],
        owner=claim["lease_owner"],
        fencing_token=claim["fencing_token"],
        claimed_revision=claim["claimed_revision"],
    )


def test_ack_timeout_preserves_exact_checkpoint_until_registration_readback(endpoint, admission, claimed_store):
    store, _service, _job, claim = claimed_store
    fake = FakeSource()
    fake.acknowledge = source(endpoint.url).acknowledge
    notifier = FakeNotifier()
    executor = module.CrmDigestExecutor(store=store, source=fake, notifier=notifier)
    first = executor(claim)
    assert first == {"ok": False, "result_code": "automation_crm_transport_failed", "outcome_uncertain": True}
    cursors = store.get_cursors(job_id=claim["job_id"])
    assert cursors["delivery_state"]["value"] == "delivered_unacked"
    assert cursors["pending_ack"]["value"] == "full-window-ack"
    assert cursors["pending_through_sequence"]["value"] == "15"
    assert endpoint.calls == [("ack", {"consumer_id": module.CRM_DIGEST_CONSUMER_ID, "ack": "full-window-ack"})]
    assert len(notifier.applied) == 1
    wait_idle(admission)
    fake.acked_sequence = 15  # Independent CRM registration readback confirms the exact pending window.
    assert executor(claim)["result_code"] == "no_changes"
    assert len(endpoint.calls) == len(notifier.applied) == 1
    assert all(item["value"] is None for item in store.get_cursors(job_id=claim["job_id"]).values())


def test_fencing_after_completed_http_still_blocks_further_effects(endpoint, admission, claimed_store, monkeypatch):
    store, _service, _job, claim = claimed_store
    client = source(endpoint.url, timeout=2)
    endpoint.mode = "fast"
    original = client.register

    def register_then_expire():
        result = original()
        expired = registry.utc_now() + timedelta(seconds=61)
        monkeypatch.setattr(registry, "utc_now", lambda: expired)
        return result

    monkeypatch.setattr(client, "register", register_then_expire)
    notifier = FakeNotifier()
    result = module.CrmDigestExecutor(store=store, source=client, notifier=notifier)(claim)
    assert result["result_code"] == "automation_fencing_conflict"
    assert [operation for operation, _payload in endpoint.calls] == ["register"]
    assert notifier.applied == []
