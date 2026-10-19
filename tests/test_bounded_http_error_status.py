"""Known HTTP failures survive incomplete bodies; only loopback HTTP is used."""

from __future__ import annotations

import asyncio
import select
import socket
import threading
import time
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from autostop_manager import bounded_http_read as bounded


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    # Windows asyncio creates its own loopback socketpair while starting a loop.
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def checked_address(address):
        assert isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}, "external network forbidden"

    def connect(sock, address):
        checked_address(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        checked_address(address)
        return original_connect_ex(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        assert host in {"127.0.0.1", "::1", b"127.0.0.1", b"::1"}, "external DNS forbidden"
        return original_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")


@pytest.fixture
def error_server():
    stopped, closed = threading.Event(), threading.Event()
    body = b'{"message":"' + b"x" * 6000 + b'"}'

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_GET(self):
            _, raw_status, mode = self.path.split("/")
            self.send_response(int(raw_status))
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Synthetic", "known-status")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body if mode == "complete" else body[:3])
                self.wfile.flush()
                if mode == "complete":
                    return
                if mode == "truncated":
                    self.close_connection = True
                    return
                expires = time.monotonic() + 2
                while not stopped.is_set() and time.monotonic() < expires:
                    if select.select([self.connection], [], [], 0.02)[0] and not self.connection.recv(1):
                        closed.set()
                        return
                self.close_connection = True
            except OSError:
                closed.set()
                self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", body, closed
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)


@pytest.mark.parametrize("status", [401, 403, 429, 503])
@pytest.mark.parametrize("mode", ["stalled", "truncated"])
def test_known_error_status_survives_stalled_or_truncated_body(error_server, status, mode):
    base, _, closed = error_server
    started = time.monotonic()
    with pytest.raises(HTTPError) as failure:
        bounded.urlopen(Request(f"{base}/{status}/{mode}"), timeout=0.5, maximum=100, preserve_error_status=True)
    with failure.value as response:
        assert response.code == status
        assert response.headers["X-Synthetic"] == "known-status"
        assert response.read() == b""
    assert time.monotonic() - started < 1.1
    if mode == "stalled":
        assert closed.wait(1), "deadline must close the owned HTTP stream"


@pytest.mark.parametrize("mode", ["stalled", "truncated"])
def test_provider_default_keeps_transport_error_for_incomplete_error_body(error_server, mode):
    base, _, _ = error_server
    with pytest.raises((TimeoutError, URLError)) as failure:
        bounded.urlopen(Request(f"{base}/403/{mode}"), timeout=0.5, maximum=100)
    assert not isinstance(failure.value, HTTPError)


@pytest.mark.parametrize("preserve", [False, True])
def test_extended_error_budget_keeps_complete_body_and_default_stays_bounded(error_server, preserve):
    base, body, _ = error_server
    for extra, expected in (({}, body[: bounded.ERROR_BODY_LIMIT + 1]), ({"error_maximum": len(body)}, body)):
        with pytest.raises(HTTPError) as failure:
            bounded.urlopen(
                Request(f"{base}/422/complete"),
                timeout=2,
                maximum=20,
                preserve_error_status=preserve,
                **extra,
            )
        with failure.value as response:
            assert response.code == 422
            assert response.read() == expected


@pytest.mark.parametrize("short_absolute", [False, True])
def test_absolute_deadline_only_shortens_relative_timeout(monkeypatch, short_absolute):
    observed = []
    worker_done = threading.Event()

    async def stalled_fetch(_request, *, deadline, **_kwargs):
        observed.append(deadline)
        try:
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                await asyncio.sleep(2)
        finally:
            worker_done.set()

    monkeypatch.setattr(bounded, "_fetch", stalled_fetch)
    start = time.monotonic()
    absolute = start + (0.1 if short_absolute else 2)
    relative = 2 if short_absolute else 0.1
    with pytest.raises(TimeoutError):
        bounded.urlopen(Request("http://127.0.0.1/synthetic"), timeout=relative, maximum=20, deadline=absolute)
    assert worker_done.wait(1)
    assert observed and observed[0] <= start + 0.2
    assert time.monotonic() - start < 0.8


def test_known_status_keeps_private_admission_until_dns_cleanup_finishes(monkeypatch):
    private = threading.BoundedSemaphore(1)
    provider = threading.BoundedSemaphore(1)
    assert provider.acquire(blocking=False)
    monkeypatch.setattr(bounded, "_ADMISSIONS", provider)
    dns_entered, release_dns = threading.Event(), threading.Event()
    starts = []
    original_start = threading.Thread.start

    def counted_start(thread):
        if thread.name == "autostop-bounded-http":
            starts.append(thread.name)
        return original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", counted_start)

    def blocked_dns():
        dns_entered.set()
        assert release_dns.wait(2)

    async def known_error_with_dns_cleanup(_request, *, deadline, on_error_headers, **_kwargs):
        headers = Message()
        headers["X-Synthetic"] = "known-status"
        on_error_headers(403, headers)
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            await asyncio.to_thread(blocked_dns)

    monkeypatch.setattr(bounded, "_fetch", known_error_with_dns_cleanup)
    request = Request("http://127.0.0.1/synthetic")
    try:
        with pytest.raises(HTTPError) as failure:
            bounded.urlopen(request, timeout=0.1, maximum=20, admission=private, preserve_error_status=True)
        with failure.value as response:
            assert response.code == 403 and response.read() == b""
        assert dns_entered.is_set()
        assert not private.acquire(blocking=False)
        with pytest.raises(URLError) as busy:
            bounded.urlopen(request, timeout=0.1, maximum=20, admission=private, preserve_error_status=True)
        assert busy.value.reason == "provider_transport_busy"
        assert starts == ["autostop-bounded-http"]
        assert not provider.acquire(blocking=False)
    finally:
        release_dns.set()
        assert private.acquire(timeout=1)
        private.release()
        provider.release()


@pytest.mark.parametrize("arguments", [{"error_maximum": 0}, {"error_maximum": True}, {"deadline": float("nan")}])
def test_invalid_new_limits_do_not_acquire_capacity(monkeypatch, arguments):
    class NoAdmission:
        def acquire(self, **_kwargs):
            pytest.fail("invalid transport limits must fail before admission")

    monkeypatch.setattr(bounded, "_ADMISSIONS", NoAdmission())
    with pytest.raises(ValueError):
        bounded.urlopen(Request("http://127.0.0.1/synthetic"), timeout=1, maximum=20, **arguments)


def test_expired_absolute_deadline_never_starts_worker(monkeypatch):
    def unexpected_start(_thread):
        pytest.fail("expired deadline must not start an HTTP worker")

    monkeypatch.setattr(threading.Thread, "start", unexpected_start)
    with pytest.raises(TimeoutError):
        bounded.urlopen(Request("http://127.0.0.1/synthetic"), timeout=1, maximum=20, deadline=time.monotonic() - 1)
