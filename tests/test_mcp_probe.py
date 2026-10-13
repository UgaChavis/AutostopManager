from __future__ import annotations

import asyncio
import io
import json
import os
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import httpx
import anyio
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from mcp import types
from mcp.shared.message import SessionMessage

from autostop_manager import config
from autostop_manager import catalog_clients
from autostop_manager import mcp_probe
from autostop_manager.catalog_adapters import CATALOG_STAGES, catalog_provider_status
import autostop_manager.web_research_gateway as web_gateway
import autostop_manager.store_api as store_api
from autostop_manager.mcp_probe import (
    EFFECTFUL_TOOL_ANNOTATIONS,
    SYNTHETIC_IDENTIFIER,
    async_probe_manager_mcp,
    classify_transport_exception,
)
from autostop_manager.mcp_contract import MANAGER_MCP_CATALOG_PATH
from autostop_manager.mcp_server import build_server


def _memory_probe_peer(monkeypatch, *, stall_method=None, slow_pages=False, stall_write_before=None):
    """Answer synthetic JSON-RPC over memory streams, never opening a socket."""
    requests = []

    @asynccontextmanager
    async def transport(_url, *, http_client):
        del http_client
        response_writer, response_reader = anyio.create_memory_object_stream[SessionMessage](10)
        request_writer, request_reader = anyio.create_memory_object_stream[SessionMessage](
            0 if stall_write_before else 10
        )

        async def peer():
            if stall_write_before == "initialize":
                await asyncio.Event().wait()
            async for envelope in request_reader:
                request = envelope.message.root
                if not isinstance(request, types.JSONRPCRequest):
                    if (
                        stall_write_before == "ping"
                        and isinstance(request, types.JSONRPCNotification)
                        and request.method == "notifications/initialized"
                    ):
                        await asyncio.Event().wait()
                    continue
                requests.append(request.method)
                if request.method == stall_method:
                    continue
                if request.method == "initialize":
                    result = types.InitializeResult(
                        protocolVersion=types.LATEST_PROTOCOL_VERSION,
                        capabilities=types.ServerCapabilities(tools=types.ToolsCapability()),
                        serverInfo=types.Implementation(name="synthetic-memory-peer", version="1"),
                    ).model_dump(mode="json", by_alias=True, exclude_none=True)
                elif request.method == "ping":
                    result = {}
                elif request.method == "tools/list":
                    page = requests.count("tools/list")
                    if slow_pages:
                        await asyncio.sleep(0.03)
                    result = types.ListToolsResult(
                        tools=[types.Tool(name=f"fixture_{page}", inputSchema={"type": "object"})],
                        nextCursor=str(page) if slow_pages and page < 3 else None,
                    ).model_dump(mode="json", by_alias=True, exclude_none=True)
                else:
                    pytest.fail(f"Unexpected RPC: {request.method}")
                await response_writer.send(
                    SessionMessage(
                        types.JSONRPCMessage(types.JSONRPCResponse(jsonrpc="2.0", id=request.id, result=result))
                    )
                )
                if stall_write_before == "initialized" and request.method == "initialize":
                    await asyncio.Event().wait()

        async with response_writer, response_reader, request_writer, request_reader:
            task = asyncio.create_task(peer())
            try:
                yield response_reader, request_writer, None
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    monkeypatch.setattr(mcp_probe, "streamable_http_client", transport)
    return requests


@pytest.mark.parametrize("stall_method", ["initialize", "ping", "tools/list"])
def test_probe_bounds_stalled_session_rpc_with_real_memory_transport(monkeypatch, stall_method):
    requests = _memory_probe_peer(monkeypatch, stall_method=stall_method)

    async def run():
        return await asyncio.wait_for(async_probe_manager_mcp("http://synthetic.invalid/mcp", timeout=0.03), 0.5)

    report = asyncio.run(run())

    assert stall_method in requests
    assert report["ok"] is False
    assert report["diagnostic"] == "transport_failure"
    assert report["checks"]["transport"]["ok"] is False


@pytest.mark.parametrize("stall_write_before", ["initialize", "initialized", "ping"])
def test_probe_bounds_handshake_backpressure_before_response_wait(monkeypatch, stall_write_before):
    _memory_probe_peer(monkeypatch, stall_write_before=stall_write_before)

    async def run():
        return await asyncio.wait_for(
            async_probe_manager_mcp("http://synthetic.invalid/mcp", timeout=0.03, browser_check=True), 0.5
        )

    report = asyncio.run(run())

    assert report["ok"] is False
    assert report["diagnostic"] == "transport_failure"
    assert report["checks"]["transport"]["ok"] is False
    assert "native_ping" not in report["checks"]


def test_probe_call_bounds_backpressure_before_sdk_response_timeout():
    async def run():
        response_writer, response_reader = anyio.create_memory_object_stream[SessionMessage](0)
        request_writer, request_reader = anyio.create_memory_object_stream[SessionMessage](0)
        async with response_writer, response_reader, request_writer, request_reader:
            async with ClientSession(response_reader, request_writer) as session:
                guard = asyncio.timeout(0.5)
                async with guard:
                    with pytest.raises(TimeoutError):
                        await mcp_probe._call(session, "synthetic_tool", {}, timeout=0.03)
                assert not guard.expired()

    asyncio.run(run())


def test_probe_tools_list_has_one_deadline_across_slow_pages(monkeypatch):
    requests = _memory_probe_peer(monkeypatch, slow_pages=True)

    async def run():
        return await asyncio.wait_for(async_probe_manager_mcp("http://synthetic.invalid/mcp", timeout=0.05), 0.5)

    report = asyncio.run(run())

    assert report["ok"] is False
    assert report["diagnostic"] == "transport_failure"
    assert report["checks"]["transport"]["ok"] is False
    assert 1 <= requests.count("tools/list") < 3


@pytest.mark.parametrize("cursor_cycle", [["same", "same"], ["first", "second", "first"]])
def test_tools_list_rejects_cursor_cycles_before_another_request(cursor_cycle):
    class Session:
        def __init__(self):
            self.calls = 0

        async def list_tools(self, *, cursor=None):
            self.calls += 1
            assert self.calls <= 64, "tools/list exceeded its request bound"
            await asyncio.sleep(0)
            return SimpleNamespace(tools=[], nextCursor=cursor_cycle[(self.calls - 1) % len(cursor_cycle)])

    session = Session()
    with pytest.raises(ValueError, match="pagination"):
        asyncio.run(asyncio.wait_for(mcp_probe._list_all_tools(session), 0.5))
    assert session.calls == len(cursor_cycle)


def test_tools_list_rejects_exhaustion_with_always_unique_cursors():
    class Session:
        def __init__(self):
            self.calls = 0

        async def list_tools(self, *, cursor=None):
            self.calls += 1
            assert self.calls <= 64, "tools/list exceeded its request bound"
            return SimpleNamespace(tools=[], nextCursor=str(self.calls))

    session = Session()
    with pytest.raises(ValueError, match="pagination"):
        asyncio.run(mcp_probe._list_all_tools(session))
    assert session.calls == 64


@pytest.mark.parametrize("page_count", [2, 64])
def test_tools_list_preserves_normal_pagination_and_last_allowed_page(page_count):
    class Session:
        def __init__(self):
            self.calls = 0

        async def list_tools(self, *, cursor=None):
            assert cursor == (str(self.calls) if self.calls else None)
            self.calls += 1
            return SimpleNamespace(tools=[self.calls], nextCursor=str(self.calls) if self.calls < page_count else None)

    session = Session()
    assert asyncio.run(mcp_probe._list_all_tools(session)) == list(range(1, page_count + 1))


def test_probe_browser_keeps_explicit_timeout_with_bounded_session(monkeypatch):
    _memory_probe_peer(monkeypatch)
    monkeypatch.setattr(mcp_probe, "validate_manager_mcp_surface", lambda _: {"ok": True, "diagnostic": "ok"})
    calls = []

    async def call(_session, name, _arguments, *, timeout):
        calls.append((name, timeout))
        payload = {
            "catalog_provider_status": {"ok": True, "providers": [], "stage": "catalog_cross"},
            "resolve_vin_oem_parts": {
                "status": "planned",
                "calls": [{"operation": "vin_decode", "dry_run": True}],
                "live_call_count": 0,
                "oem_candidates": [],
            },
            "fetch_page_browser": {
                "ok": True,
                "capability": "fetch_page_browser",
                "read_only": True,
                "mode": "browser",
                "domain": "example.com",
                "excerpt": "synthetic excerpt",
            },
        }[name]
        return False, payload

    monkeypatch.setattr(mcp_probe, "_call", call)
    report = asyncio.run(async_probe_manager_mcp("http://synthetic.invalid/mcp", timeout=0.5, browser_check=True))

    assert report["ok"] is True
    assert calls == [("catalog_provider_status", 0.5), ("resolve_vin_oem_parts", 0.5), ("fetch_page_browser", 90.0)]


def test_catalog_provider_status_native_transport_preserves_both_json_channels_compactly(tmp_path, monkeypatch):
    previous_env_loaded = config._ENV_LOADED
    for name in tuple(os.environ):
        if name.startswith("PARTSAPI_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", str(tmp_path / "empty.env"))
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "manager.sqlite3"))
    config._ENV_LOADED = False

    async def run_call():
        manager_server = build_server()
        previous_server = FastMCP("previous")
        previous_server.tool(name="catalog_provider_status")(catalog_provider_status)
        manager_tool = next(
            tool for tool in await manager_server.list_tools() if tool.name == "catalog_provider_status"
        )
        previous_tool = next(
            tool for tool in await previous_server.list_tools() if tool.name == "catalog_provider_status"
        )
        properties = manager_tool.inputSchema["properties"]
        assert properties["stage"]["anyOf"][0]["enum"] == list(CATALOG_STAGES)
        assert properties["stage"]["default"] is None
        assert properties["detail"]["enum"] == ["summary", "full"]
        assert properties["detail"]["default"] == "full"
        assert manager_tool.outputSchema == previous_tool.outputSchema

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        uvicorn_server = uvicorn.Server(
            uvicorn.Config(manager_server.streamable_http_app(), log_level="warning", access_log=False)
        )
        task = asyncio.create_task(uvicorn_server.serve(sockets=[listener]))
        try:
            for _ in range(200):
                if uvicorn_server.started:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("native_manager_mcp_test_server_did_not_start")
            async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (reader, writer, _):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    return await session.call_tool("catalog_provider_status", {"stage": "catalog_cross"})
        finally:
            uvicorn_server.should_exit = True
            await asyncio.wait_for(task, timeout=5)
            listener.close()

    try:
        result = asyncio.run(run_call())
        original = catalog_provider_status(stage="catalog_cross")
    finally:
        config._ENV_LOADED = previous_env_loaded

    assert result.isError is False
    assert len(result.content) == 1
    text = result.content[0].text
    parsed = json.loads(text)
    assert parsed == result.structuredContent
    assert parsed["tool_execution"]["tool"] == "catalog_provider_status"
    assert {key: value for key, value in parsed.items() if key != "tool_execution"} == json.loads(
        json.dumps(original, ensure_ascii=False)
    )
    assert text == json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    assert len(text.encode()) < len(json.dumps(parsed, ensure_ascii=False, indent=2).encode())


def test_transport_classifier_keeps_route_auth_and_registration_failures_distinct():
    assert classify_transport_exception(httpx.ConnectError("connection refused")) == "transport_route_unavailable"
    assert classify_transport_exception(ExceptionGroup("transport", [httpx.ConnectError("connection refused")])) == (
        "transport_route_unavailable"
    )
    assert classify_transport_exception(RuntimeError("HTTP 403")) == "transport_auth_failure"
    assert classify_transport_exception(RuntimeError("Unknown tool requested")) == "tool_not_registered"
    assert classify_transport_exception(RuntimeError("Tool not found: retired_command")) == "tool_not_registered"
    assert classify_transport_exception(RuntimeError("unclassified transport error")) == "transport_failure"


@pytest.mark.parametrize(
    ("store_check", "store_ready", "browser_check", "orders_response"),
    [
        (False, False, False, "unused"),
        (True, False, False, "unused"),
        (True, True, False, "valid"),
        (True, True, False, "empty"),
        (True, True, False, "invalid_schema"),
        (True, True, False, "invalid_payment_status"),
        (True, True, False, "provider_error"),
        (False, False, True, "unused"),
    ],
)
def test_native_manager_mcp_transport_probe_uses_only_synthetic_redacted_data(
    tmp_path, monkeypatch, caplog, store_check, store_ready, browser_check, orders_response
):
    sentinel_secret = "probe-secret-must-not-appear"
    sentinel_order = "private-order-id-must-not-appear"
    previous_env_loaded = config._ENV_LOADED
    for name in tuple(os.environ):
        if name.startswith("PARTSAPI_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "manager.sqlite3"))
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", str(tmp_path / "does-not-exist.env"))
    # Keep the synthetic origin HTTPS like the real provider contract. The
    # transport boundary below injects failure without any provider traffic.
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://127.0.0.1:9")
    monkeypatch.setenv("PARTSAPI_VINDECODE_KEY", sentinel_secret)
    monkeypatch.setenv("AUTOSTOP_STORE_API_URL", "http://127.0.0.1:9/internal/agent/v1")
    monkeypatch.setenv("AUTOSTOP_STORE_READ_TOKEN", sentinel_secret)
    config._ENV_LOADED = False
    order_requests = []
    provider_requests = []

    def unavailable_provider(request, *, timeout):
        endpoint = urlsplit(request.full_url)
        assert endpoint.scheme == "https"
        assert endpoint.hostname == "127.0.0.1" and endpoint.port == 9
        assert timeout >= 1
        provider_requests.append(request)
        raise URLError("synthetic unavailable provider")

    monkeypatch.setattr(catalog_clients, "urlopen", unavailable_provider)

    def store_response(request, *, timeout):
        # The actual Store adapter validates this provider response; search is
        # not replaced with a success stub, so contract drift reaches the probe.
        endpoint = urlsplit(request.full_url)
        assert request.method == "GET"
        assert endpoint.path == "/internal/agent/v1/search"
        assert parse_qs(endpoint.query) == {"entity": ["store_order"], "limit": ["1"]}
        order_requests.append(request)
        order = {
            "entity": "store_order",
            "id": sentinel_order,
            "order_number": sentinel_order,
            "payment_status": "PAID",
            "paid_at": "2026-09-26T03:30:00Z",
        }
        if orders_response == "invalid_schema":
            order["unexpected_provider_field"] = sentinel_secret
        if orders_response == "invalid_payment_status":
            order["payment_status"] = {"private": sentinel_secret}
        payload = {
            "ok": orders_response != "provider_error",
            "format": "store_agent_v1",
            "status": "blocked" if orders_response == "provider_error" else "completed",
            "summary": {"error_code": sentinel_secret} if orders_response == "provider_error" else {},
            "items": [] if orders_response in {"empty", "provider_error"} else [order],
            "changes": [],
            "page": {"has_more": False, "next_cursor": None, "limit": 1},
            "warnings": [],
            "meta": {},
        }
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(store_api, "urlopen", store_response)
    monkeypatch.setattr(
        "autostop_manager.store_api.StoreApiClient.runtime_status",
        lambda *a, **kw: {"ok": store_ready, "private_payload": sentinel_secret},
    )
    monkeypatch.setattr(
        "autostop_manager.store_owner_api.StoreOwnerApiClient.list_capabilities",
        lambda *a, **kw: {"ok": store_ready, "private_payload": sentinel_secret},
    )
    monkeypatch.setattr(
        web_gateway,
        "fetch_j1_browser_page",
        lambda url, *, max_chars: {
            "ok": True,
            "url": url,
            "title": "Example Domain",
            "text": "Example Domain synthetic browser smoke"[:max_chars],
        },
    )

    async def run_probe() -> dict[str, object]:
        manager_server = build_server()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        uvicorn_server = uvicorn.Server(
            uvicorn.Config(manager_server.streamable_http_app(), log_level="warning", access_log=False)
        )
        task = asyncio.create_task(uvicorn_server.serve(sockets=[listener]))
        try:
            for _ in range(200):
                if uvicorn_server.started:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("native_manager_mcp_test_server_did_not_start")
            return await async_probe_manager_mcp(
                f"http://127.0.0.1:{port}/mcp",
                timeout=5,
                provider_failure_check=True,
                store_check=store_check,
                browser_check=browser_check,
            )
        finally:
            uvicorn_server.should_exit = True
            await asyncio.wait_for(task, timeout=5)
            listener.close()

    try:
        report = asyncio.run(run_probe())
    finally:
        config._ENV_LOADED = previous_env_loaded

    rendered = json.dumps(report, ensure_ascii=False)
    expected_diagnostic = "ok"
    if store_check:
        if not store_ready:
            expected_diagnostic = "store_connection_unavailable"
        elif orders_response.startswith("invalid_"):
            expected_diagnostic = "store_response_schema_invalid"
        elif orders_response == "provider_error":
            expected_diagnostic = "store_order_read_unavailable"
    assert report["ok"] is (expected_diagnostic == "ok")
    assert report["diagnostic"] == expected_diagnostic
    if store_check:
        assert report["checks"]["store_runtime_status"] == {"ok": store_ready}
        assert report["checks"]["store_owner_capabilities"] == {"ok": store_ready}
    if store_check and store_ready:
        assert len(order_requests) == 1
        assert report["checks"]["store_order_search"] == {
            "ok": expected_diagnostic == "ok",
            "diagnostic": "store_order_sample_empty" if orders_response == "empty" else expected_diagnostic,
        }
        assert report["privacy"]["store_order_read_attempted"] is True
    else:
        assert order_requests == []
        assert "store_order_search" not in report["checks"]
        assert report["privacy"]["store_order_read_attempted"] is False
    if browser_check:
        assert report["checks"]["fetch_page_browser"] == {
            "ok": True,
            "mode": "browser",
            "domain": "example.com",
            "has_excerpt": True,
        }
    assert report["checks"]["native_ping"]["ok"] is True
    assert report["checks"]["tools_list"]["ok"] is True
    manifest = json.loads(MANAGER_MCP_CATALOG_PATH.read_text(encoding="utf-8"))
    assert report["checks"]["tools_list"]["tool_count"] == manifest["expected_tool_count"]
    assert report["checks"]["tools_list"]["effectful_annotations_checked"] == len(EFFECTFUL_TOOL_ANNOTATIONS)
    assert report["checks"]["tools_list"]["annotation_mismatch_tools"] == []
    assert report["checks"]["catalog_provider_status"]["ok"] is True
    assert report["checks"]["catalog_provider_status"]["stage"] == "catalog_cross"
    assert report["checks"]["synthetic_resolver"]["ok"] is True
    assert report["checks"]["synthetic_resolver"]["diagnostic"] == "vin_decode_dry_run"
    assert report["checks"]["synthetic_resolver"]["live_call_count"] == 0
    assert report["checks"]["synthetic_resolver"]["oem_candidate_count"] == 0
    assert report["checks"]["provider_failure"]["ok"] is True
    assert report["checks"]["provider_failure"]["diagnostic"] == "provider_failure"
    assert report["checks"]["provider_failure"]["failure_class"] in {
        "network_error",
        "timeout",
        "adapter_malformed_payload",
    }
    assert len(provider_requests) == 1
    assert report["privacy"]["raw_identifier_returned"] is False
    assert report["privacy"]["secret_exposed"] is False
    assert SYNTHETIC_IDENTIFIER not in rendered
    assert sentinel_secret not in rendered
    assert sentinel_order not in rendered
    assert SYNTHETIC_IDENTIFIER not in caplog.text
    assert sentinel_secret not in caplog.text
    assert sentinel_order not in caplog.text
