"""Analytics owns the Store HTTP deadline and admission; all data are synthetic."""

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import bounded_http_read, mcp_telemetry, mcp_tools, store_analytics, store_api
from autostop_manager.bounded_http_read import TrackedTransportAdmission
from autostop_manager.listing_executor import BoundedListingExecutor
from autostop_manager.mcp_store_executor import StoreWorkers


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_dns = socket.getaddrinfo

    def check(address):
        assert address[0] in {"127.0.0.1", "::1"}, "external network forbidden"

    def connect(sock, address):
        check(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(address)
        return original_connect_ex(sock, address)

    def dns(host, *args, **kwargs):
        assert host in {"127.0.0.1", "::1", b"127.0.0.1", b"::1"}, "external DNS forbidden"
        return original_dns(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(mcp_telemetry.LOGGER, "disabled", True)


def payload():
    return {
        "ok": True,
        "schema": store_analytics.STORE_ANALYTICS_FORMAT,
        "timezone": "Asia/Krasnoyarsk",
        "periodPreset": "today",
        "period": {"start": "2026-10-30T00:00:00+07:00", "end": "2026-10-31T00:00:00+07:00"},
        "previousPeriod": {"start": "2026-10-29T00:00:00+07:00", "end": "2026-10-30T00:00:00+07:00"},
        "summary": dict.fromkeys(store_analytics._SUMMARY_FIELDS, 0),
        "funnel": dict.fromkeys(store_analytics._FUNNEL_FIELDS, 0),
        "topPages": [],
        "topProducts": [],
        "clicks": [],
        "comparison": {},
        "meta": {"aggregatedOnly": True, "rawEventsIncluded": False, "retentionDays": 60},
    }


@contextmanager
def local_response(*, phase=None, status=200, body=None):
    body = json.dumps(payload()).encode() if body is None else body
    release, entered, closed = threading.Event(), threading.Event(), threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(
                (self.path, self.headers.get("Authorization"), self.rfile.read(int(self.headers["Content-Length"])))
            )
            try:
                if phase == "headers":
                    self.wfile.write(f"HTTP/1.0 {status} Synthetic\r\nX-Synthetic: ".encode())
                else:
                    self.send_response(status)
                    self.end_headers()
                self.wfile.flush()
                entered.set()
                if phase:
                    until = time.monotonic() + 3
                    while not release.is_set() and time.monotonic() < until:
                        self.wfile.write(b" " if phase == "body" else b"x")
                        self.wfile.flush()
                        release.wait(0.03)
                if phase == "headers":
                    self.wfile.write(b"\r\n\r\n")
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                closed.set()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/internal/agent/v1", requests, entered, release, closed
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


def report(url, **kwargs):
    return store_analytics.get_store_analytics_report(api_url=url, read_token="synthetic-token", **kwargs)


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_analytics_deadline_ends_while_response_is_still_incomplete(phase):
    with local_response(phase=phase) as (url, requests, entered, release, closed):
        results = []
        caller = threading.Thread(target=lambda: results.append(report(url, timeout=0.5)))
        caller.start()
        try:
            assert entered.wait(2)
            caller.join(1.2)
            returned_before_release = not caller.is_alive()
        finally:
            release.set()
            caller.join(3)
        assert returned_before_release, "analytics kept its reader beyond the total HTTP deadline"
        assert results[0]["error"]["code"] == "store_analytics_unavailable"
        assert len(requests) == 1
        assert closed.wait(2)


@pytest.mark.parametrize("status", [302, 401, 403, 429, 503])
def test_known_http_status_survives_incomplete_error_body(status):
    with local_response(phase="body", status=status, body=b"private backend error") as (
        url,
        requests,
        entered,
        release,
        _,
    ):
        result = report(url, timeout=0.5)
        assert entered.is_set()
        assert result["error"]["code"] == f"store_analytics_http_{status}"
        assert "private backend error" not in repr(result)
        assert len(requests) == 1
        release.set()


def test_exact_body_limit_succeeds_and_one_extra_byte_is_rejected():
    body = json.dumps(payload()).encode()
    for extra in (b"", b" "):
        with local_response(body=body + extra) as (url, requests, *_):
            result = report(url, max_response_bytes=len(body))
            if extra:
                assert result["error"]["code"] == "store_analytics_response_too_large"
            else:
                assert result["ok"] is True
                assert result["summary"] == payload()["summary"]
            assert len(requests) == 1
            assert requests[0][0].endswith("/analytics/report")
            assert requests[0][1] == "Bearer synthetic-token"


@pytest.mark.parametrize("closed", [False, True])
def test_analytics_respects_the_current_transport_owner_without_dispatch(monkeypatch, closed):
    owner = TrackedTransportAdmission(1)
    if closed:
        owner.begin_drain()
    else:
        assert owner.acquire(blocking=False)

    def denied(*_args, **_kwargs):
        pytest.fail("analytics bypassed its owning Store HTTP admission")

    monkeypatch.setattr(socket, "getaddrinfo", denied)
    token = store_api.store_transport_admission.set(owner)
    try:
        result = report("http://127.0.0.1:1/internal/agent/v1")
        assert result["error"]["code"] == "store_analytics_unavailable"
        assert owner.pending_count == int(not closed)
    finally:
        store_api.store_transport_admission.reset(token)
        if not closed:
            owner.release()


def test_native_analytics_tracks_transport_cleanup_after_its_adapter_returns(monkeypatch):
    class Inert:
        def __init__(self, *_args, **_kwargs):
            pass

    for name in (
        "StoreIntegration",
        "StoreApiClient",
        "StoreOwnerApiClient",
        "StoreQuoteConductor",
        "StoreQuoteOwnerApi",
    ):
        monkeypatch.setattr(mcp_tools, name, Inert)
    for name in ("get_store_manage_token", "get_store_quote_token", "get_store_owner_token"):
        monkeypatch.setattr(mcp_tools, name, lambda: "")
    monkeypatch.setattr(mcp_tools, "get_store_api_url", lambda: "http://127.0.0.1:1/internal/agent/v1")
    monkeypatch.setattr(mcp_tools, "get_store_read_token", lambda: "synthetic-token")
    owner = StoreWorkers(reads=BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=2))
    entered, release = threading.Event(), threading.Event()

    def cleanup():
        entered.set()
        assert release.wait(4)

    async def fake_fetch(_request, *, deadline, **_kwargs):
        # Model a resolver cleanup that outlives the HTTP coroutine's deadline.
        async with asyncio.timeout(0.1):
            await asyncio.to_thread(cleanup)

    monkeypatch.setattr(bounded_http_read, "_fetch", fake_fetch)
    original_report = mcp_tools.get_store_analytics_report
    monkeypatch.setattr(
        mcp_tools, "get_store_analytics_report", lambda **kwargs: original_report(**kwargs, timeout=0.2)
    )
    server = FastMCP("synthetic-analytics-transport")
    server.store_workers = owner
    mcp_tools.register_manager_tools(
        server, store=Inert(), store_client=Inert(), include_tools={"get_store_analytics_report"}
    )
    tool = server._tool_manager.get_tool("get_store_analytics_report")

    async def run():
        try:
            content, result = await tool.run({}, convert_result=True)
            assert json.loads(content[0].text) == result
            assert result["error"]["code"] == "store_analytics_unavailable"
            assert entered.is_set()
            async with asyncio.timeout(1):
                while owner.reads.pending_count:
                    await asyncio.sleep(0.01)
            snapshot = await owner.drain(0.03)
            assert snapshot["ok"] is False
            assert snapshot["pending"] == {"read": 0, "stateful": 0, "transport": 1}
            release.set()
            assert (await owner.drain(2))["ok"] is True
        finally:
            release.set()

    try:
        asyncio.run(run())
    finally:
        release.set()
        asyncio.run(owner.drain(2))
        owner.close()
