"""Loopback HTTP fixtures exercise elapsed budgets, not provider availability."""

from __future__ import annotations

import asyncio
import json
import select
import threading
import time
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from autostop_manager import bounded_http_read as bounded
from autostop_manager import catalog_clients, vin_lookup


@pytest.fixture
def local_http(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    closed = threading.Event()
    stopped = threading.Event()
    connected = threading.Event()
    observed = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def client_closed(self):
            if select.select([self.connection], [], [], 0)[0] and not self.connection.recv(1):
                closed.set()
                return True
            return False

        def do_GET(self):
            observed.append((self.command, self.path, self.headers.get("X-Test")))
            connected.set()
            try:
                if self.path == "/headers-trickle":
                    self.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                elif self.path == "/delayed-headers":
                    time.sleep(1)
                    if self.client_closed():
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                elif self.path in {"/body-trickle", "/shared-budget"}:
                    if self.path == "/shared-budget":
                        time.sleep(0.15)
                    self.send_response(200)
                    self.send_header("Content-Length", "10000")
                    self.end_headers()
                else:
                    status = 302 if self.path == "/redirect" else 429 if self.path == "/quota" else 200
                    body = b'{"msg":"quota"}' if status == 429 else b"0123456789"
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(body)))
                    if status == 302:
                        self.send_header("Location", "/success")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                while not stopped.is_set():
                    if self.client_closed():
                        return
                    self.connection.sendall(b"x")
                    time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError):
                closed.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", closed, observed, connected
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)


@pytest.mark.parametrize("path", ["/headers-trickle", "/body-trickle", "/shared-budget", "/delayed-headers"])
def test_total_deadline_closes_trickling_or_delayed_http(local_http, path):
    base, closed, _, _ = local_http
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="provider_total_deadline_exceeded"):
        bounded.urlopen(Request(base + path), timeout=0.45, maximum=100000)
    assert time.monotonic() - started < 0.85
    assert closed.wait(1.1), "the owned HTTP stream must close after its deadline"


def test_stream_size_bound_and_urllib_response_contract(local_http):
    base, _, _, _ = local_http
    with pytest.raises(ValueError, match="provider_response_too_large"):
        bounded.urlopen(Request(base + "/success"), timeout=2, maximum=8)
    with bounded.urlopen(Request(base + "/success"), timeout=2, maximum=10) as response:
        assert response.read(4) == b"0123" and response.read() == b"456789"
        assert response.status == response.getcode() == 200
        assert response.headers["Content-Length"] == "10"
        assert response.geturl() == base + "/success"


def test_no_redirect_and_error_body_keep_http_error_compatibility(local_http):
    base, _, observed, _ = local_http
    with pytest.raises(HTTPError) as redirected:
        bounded.urlopen(Request(base + "/redirect", headers={"X-Test": "synthetic"}), timeout=2, maximum=20)
    assert redirected.value.code == 302
    assert redirected.value.headers["Location"] == "/success"
    assert observed == [("GET", "/redirect", "synthetic")]
    with pytest.raises(HTTPError) as quota:
        bounded.urlopen(Request(base + "/quota"), timeout=2, maximum=20)
    assert quota.value.code == 429 and json.loads(quota.value.read()) == {"msg": "quota"}


@pytest.mark.parametrize("facade", [catalog_clients, vin_lookup])
def test_existing_facade_read_uses_whole_allocated_deadline(local_http, facade):
    base, closed, _, _ = local_http
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="provider_total_deadline_exceeded"):
        facade.urlopen(Request(base + "/body-trickle"), timeout=0.45)
    assert time.monotonic() - started < 0.85
    assert closed.wait(1)


def test_catalog_allocated_budget_does_not_change_public_timeout_clamp(monkeypatch):
    seen = []
    monkeypatch.setattr(catalog_clients, "bounded_urlopen", lambda request, **kwargs: seen.append(kwargs))
    catalog_clients.urlopen(Request("https://fixture.invalid/"), timeout=0.25)
    assert seen == [{"timeout": 0.25, "maximum": catalog_clients.MAX_PROVIDER_RESPONSE_BYTES}]
    assert catalog_clients._clamp_timeout(0.25) == catalog_clients.MIN_PROVIDER_TIMEOUT_SECONDS


def test_async_cancellation_closes_owned_http_stream(local_http):
    base, closed, _, connected = local_http

    async def run():
        task = asyncio.create_task(
            bounded._fetch(
                Request(base + "/body-trickle"), deadline=time.monotonic() + 2, maximum=100000, follow_redirects=False
            )
        )
        assert await asyncio.to_thread(connected.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert closed.wait(1)


def test_cancelling_to_thread_does_not_claim_thread_termination_but_read_still_expires(local_http):
    base, closed, _, connected = local_http
    worker_finished = threading.Event()

    def read():
        try:
            bounded.urlopen(Request(base + "/body-trickle"), timeout=0.45, maximum=100000)
        except TimeoutError:
            pass
        finally:
            worker_finished.set()

    async def run():
        task = asyncio.create_task(asyncio.to_thread(read))
        assert await asyncio.to_thread(connected.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not worker_finished.is_set()
        assert await asyncio.to_thread(worker_finished.wait, 1)

    asyncio.run(run())
    assert closed.wait(1)


def test_cancelled_caller_keeps_transport_admission_through_blocked_dns_shutdown(monkeypatch):
    admission = threading.BoundedSemaphore(1)
    monkeypatch.setattr(bounded, "_ADMISSIONS", admission)
    dns_entered, release_dns, caller_finished = threading.Event(), threading.Event(), threading.Event()
    worker_starts = []
    original_start = threading.Thread.start

    def counted_start(thread):
        if thread.name == "autostop-bounded-http":
            worker_starts.append(thread.name)
        return original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", counted_start)

    def blocked_dns():
        dns_entered.set()
        assert release_dns.wait(2)

    async def fetch_waiting_for_dns(request, *, deadline, **_kwargs):
        # Cancellation cannot terminate getaddrinfo's underlying executor thread.
        # asyncio.run waits for that executor during shutdown after this deadline.
        async with asyncio.timeout(deadline - time.monotonic()):
            await asyncio.to_thread(blocked_dns)
        return bounded.BufferedResponse(b"synthetic", status=200, headers=Message(), url=request.full_url)

    monkeypatch.setattr(bounded, "_fetch", fetch_waiting_for_dns)
    request = Request("https://fixture.invalid/")

    def first_read():
        try:
            bounded.urlopen(request, timeout=0.1, maximum=20)
        except TimeoutError:
            pass
        finally:
            caller_finished.set()

    async def run():
        task = asyncio.create_task(asyncio.to_thread(first_read))
        try:
            assert await asyncio.to_thread(dns_entered.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await asyncio.to_thread(caller_finished.wait, 0.5)
            with pytest.raises(URLError) as busy:
                bounded.urlopen(request, timeout=0.1, maximum=20)
            assert busy.value.reason == "provider_transport_busy"
            assert worker_starts == ["autostop-bounded-http"]
            release_dns.set()
            assert await asyncio.to_thread(admission.acquire, True, 1)
            admission.release()
            assert bounded.urlopen(request, timeout=0.5, maximum=20).read() == b"synthetic"
            assert len(worker_starts) == 2
        finally:
            release_dns.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("code", ["provider_transport_busy", "provider_transport_unavailable"])
def test_transport_admission_errors_never_request_provider_retry(code):
    error = URLError(code)
    assert catalog_clients._partsapi_failure_details(error)[1] is False
    assert vin_lookup._vpic_failure_details(error)[1] is False
