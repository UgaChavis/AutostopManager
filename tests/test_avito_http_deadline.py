"""Avito transport proofs using loopback only; never call ReefAPI or spend credits."""

from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from autostop_manager import avito_listings as avito, listing_executor
from autostop_manager.bounded_http_read import TrackedTransportAdmission
from autostop_manager.listing_executor import BoundedListingExecutor


@pytest.fixture(autouse=True)
def isolated_transport(monkeypatch):
    connect, connect_ex, getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def checked(address):
        assert isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}, "external network forbidden"

    def local_connect(sock, address):
        checked(address)
        return connect(sock, address)

    def local_connect_ex(sock, address):
        checked(address)
        return connect_ex(sock, address)

    def local_getaddrinfo(host, *args, **kwargs):
        assert host in {"127.0.0.1", "::1", "localhost", b"127.0.0.1", b"::1", b"localhost"}
        return getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", local_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", local_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", local_getaddrinfo)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,::1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,::1,localhost")
    monkeypatch.setenv("REEFAPI_API_KEY", "synthetic-avito-key")
    monkeypatch.setattr(avito, "_HTTP_ADMISSION", TrackedTransportAdmission(2))


@pytest.fixture
def local_avito(monkeypatch):
    servers = []

    def start(respond):
        state = SimpleNamespace(requests=[], entered=threading.Event(), stop=threading.Event(), threads=[])

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                state.threads.append(threading.current_thread())
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                state.requests.append((self.path, dict(self.headers), json.loads(body)))
                state.entered.set()
                try:
                    respond(self, state)
                except OSError:
                    pass  # A deadline closes the peer while the synthetic server is sending.
                finally:
                    self.close_connection = True

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        state.url = f"http://127.0.0.1:{server.server_port}"
        servers.append((server, thread, state))
        thread.start()
        monkeypatch.setattr(avito, "_API_BASE_URL", state.url)
        return state

    yield start
    for server, thread, state in servers:
        state.stop.set()
        server.shutdown()
        server.server_close()
        thread.join(2)
        for handler in state.threads:
            handler.join(2)
            assert not handler.is_alive()


def _respond(handler, body, *, status=200, headers=None):
    handler.send_response(status)
    handler.send_header("Content-Length", str(len(body)))
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()


@pytest.fixture
def invoke(monkeypatch, tmp_path):
    # Import the real registry only when a native call is requested.
    server = None
    pool = BoundedListingExecutor(timeout_seconds=3)
    monkeypatch.setattr(listing_executor, "_EXECUTOR", pool)

    def call(mode, action="search", **extra):
        nonlocal server
        arguments = {"query": "synthetic part"} if action == "search" else {"ad_id": "123456789"}
        arguments.update(extra)
        if mode == "direct":
            operation = avito.avito_search_listings if action == "search" else avito.avito_read_listing
            return operation(**arguments)
        if server is None:
            from mcp.server.fastmcp import FastMCP

            from autostop_manager.mcp_tools import register_manager_tools
            from autostop_manager.storage import StoreState

            server = FastMCP("avito-loopback")
            register_manager_tools(
                server,
                StoreState(tmp_path / "store.sqlite3"),
                include_tools={"avito_search_listings", "avito_read_listing"},
            )
        name = "avito_search_listings" if action == "search" else "avito_read_listing"

        async def run():
            tool = server._tool_manager.get_tool(name)
            assert tool is not None
            return await tool.run(arguments)

        return asyncio.run(run())

    yield call
    pool.close()


@pytest.mark.parametrize("mode", ["direct", "native"])
@pytest.mark.parametrize("action", ["search", "listing"])
@pytest.mark.parametrize("stage", ["headers", "body", "error_body"])
def test_total_deadline_closes_trickling_response_once(monkeypatch, local_avito, invoke, mode, action, stage):
    def respond(handler, state):
        if stage == "headers":
            handler.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
        else:
            handler.send_response(429 if stage == "error_body" else 200)
            handler.send_header("Content-Length", "1000000")
            handler.end_headers()
        while not state.stop.wait(0.03):
            handler.wfile.write(b" ")
            handler.wfile.flush()

    state = local_avito(respond)
    monkeypatch.setattr(avito, "_REQUEST_TIMEOUT_SECONDS", 0.8)
    started = time.monotonic()
    result = invoke(mode, action)
    elapsed = time.monotonic() - started
    assert state.entered.is_set(), "exercise a dispatched request, not slow Windows initialization"
    assert elapsed < 2.5
    assert result["error"] == ("rate_limited" if stage == "error_body" else "provider_timeout")
    assert result["http_status"] == {"headers": None, "body": 200, "error_body": 429}[stage]
    assert result["provider_code"] is None
    assert result["retryable"] is True
    assert len(state.requests) == 1
    assert state.requests[0][0] == f"/avito/v1/{action}"
    assert avito._HTTP_ADMISSION.pending_count == 0
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("mode", ["direct", "native"])
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, "request_rejected"),
        (401, "authentication_failed"),
        (402, "quota_exceeded"),
        (403, "provider_forbidden"),
        (404, "listing_not_found"),
        (429, "rate_limited"),
        (500, "provider_unavailable"),
        (504, "provider_timeout"),
    ],
)
def test_http_error_classification_never_retries(local_avito, invoke, mode, status, error):
    state = local_avito(lambda handler, _state: _respond(handler, b"not json", status=status))
    result = invoke(mode)
    assert result["error"] == error
    assert result["http_status"] == status
    assert len(state.requests) == 1


@pytest.mark.parametrize("mode", ["direct", "native"])
@pytest.mark.parametrize("location", ["https://must-not-contact.invalid/credit", "http://[invalid"])
def test_redirect_is_rejected_before_location_parsing_or_dispatch(local_avito, invoke, mode, location):
    state = local_avito(lambda handler, _state: _respond(handler, b"", status=302, headers={"Location": location}))
    result = invoke(mode)
    assert result["error"] == "provider_error"
    assert result["http_status"] == 302
    assert len(state.requests) == 1


@pytest.mark.parametrize("mode", ["direct", "native"])
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("excess", [0, 1])
def test_exact_body_limits_and_metadata(monkeypatch, local_avito, invoke, mode, failed, excess):
    monkeypatch.setattr(avito, "_MAX_RESPONSE_BYTES", 128)
    monkeypatch.setattr(avito, "_MAX_ERROR_RESPONSE_BYTES", 64)
    body = b'{"ok":false,"error":{"code":"RATE_LIMITED"}}' if failed else b'{"ok":true,"data":{"listings":[]}}'
    body = body.ljust((64 if failed else 128) + excess, b" ")
    state = local_avito(lambda handler, _state: _respond(handler, body, status=502 if failed else 200))
    result = invoke(mode)
    if failed:
        assert result["error"] == ("provider_unavailable" if excess else "rate_limited")
        assert result["provider_code"] == (None if excess else "RATE_LIMITED")
        assert result["http_status"] == 502
    elif excess:
        assert result["error"] == "response_too_large"
        assert result["http_status"] == 200
        assert result["retryable"] is False
    else:
        assert result["ok"] is True and result["listings"] == []
    assert len(state.requests) == 1


@pytest.mark.parametrize("mode", ["direct", "native"])
@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"ok":true}', b"\xff"])
def test_malformed_response_is_distinct(local_avito, invoke, mode, body):
    state = local_avito(lambda handler, _state: _respond(handler, body))
    result = invoke(mode)
    assert result["error"] == "malformed_response"
    assert result["http_status"] == 200
    assert result["retryable"] is False
    assert len(state.requests) == 1


@pytest.mark.parametrize("cancel", [False, True])
def test_dns_cleanup_retains_both_workers_and_shutdown_without_late_request(monkeypatch, local_avito, cancel):
    state = local_avito(lambda handler, _state: _respond(handler, b'{"ok":true,"data":{"listings":[]}}'))
    monkeypatch.setattr(avito, "_API_BASE_URL", state.url.replace("127.0.0.1", "localhost"))
    monkeypatch.setattr(avito, "_REQUEST_TIMEOUT_SECONDS", 0.8)
    release = threading.Event()
    entered = [threading.Event(), threading.Event()]
    original_dns = socket.getaddrinfo
    lock = threading.Lock()
    dns_calls = []

    def slow_dns(host, *args, **kwargs):
        with lock:
            index = len(dns_calls)
            dns_calls.append(host)
        assert index < 2, "transport capacity must stay held throughout cleanup"
        entered[index].set()
        assert release.wait(7)
        return original_dns(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", slow_dns)
    pool = BoundedListingExecutor(timeout_seconds=1.6)
    monkeypatch.setattr(listing_executor, "_EXECUTOR", pool)
    closed = threading.Event()
    closer = threading.Thread(target=lambda: (pool.close(), closed.set()))

    async def run():
        active = [
            asyncio.create_task(listing_executor.run_listing_request(lambda: avito.avito_search_listings("part")))
            for _ in range(2)
        ]
        for event in entered:
            assert await asyncio.to_thread(event.wait, 3), (
                dns_calls,
                [task.result() for task in active if task.done()],
            )
        queued = [
            asyncio.create_task(listing_executor.run_listing_request(lambda: avito.avito_search_listings("part")))
            for _ in range(2)
        ]
        await asyncio.sleep(0)
        assert pool.pending_count == 4
        busy = await listing_executor.run_listing_request(lambda: avito.avito_search_listings("part"))
        assert busy["error"] == "provider_busy" and busy["adapter_started"] is False
        if cancel:
            for task in active + queued:
                task.cancel()
            results = await asyncio.gather(*active, *queued, return_exceptions=True)
            assert all(isinstance(item, asyncio.CancelledError) for item in results)
            await asyncio.sleep(1)
        else:
            results = await asyncio.gather(*active, *queued)
            assert all(item["error"] == "provider_wait_timeout" for item in results)
            assert all(item["adapter_may_continue"] for item in results[:2])
            assert all(not item["adapter_started"] for item in results[2:])
        assert pool.pending_count == 4
        assert avito._HTTP_ADMISSION.pending_count == 2
        assert state.requests == []
        closer.start()
        assert not await asyncio.to_thread(closed.wait, 0.1)
        release.set()
        assert await asyncio.to_thread(closed.wait, 3)
        assert pool.pending_count == avito._HTTP_ADMISSION.pending_count == 0
        assert len(dns_calls) == 2
        assert state.requests == [], "DNS completion after the deadline must not send the paid POST"

    try:
        asyncio.run(run())
    finally:
        release.set()
        pool.close()
        if closer.ident is not None:
            closer.join(3)
