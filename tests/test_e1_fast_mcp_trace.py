"""Offline checks for per-call timing and stale host-schema evidence."""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from autostop_manager import mcp_telemetry, mcp_tools
from autostop_manager.mcp_contract import manager_session_schema_evidence
from autostop_manager.storage import StoreState
from autostop_manager.telegram_wake import AppServer, WakeConfig


def test_sdk_registration_preserves_schema_annotations_and_result_channels(monkeypatch):
    events = []
    monkeypatch.setattr(mcp_telemetry.LOGGER, "info", events.append)
    server = FastMCP("offline-trace")

    @server.tool(name="fixture", annotations=ToolAnnotations(readOnlyHint=True))
    def fixture(secret: str) -> dict[str, Any]:
        return {
            "ok": True,
            "private": secret,
            "execution": {"elapsed_ms": 3, "network_calls": 1, "attempts": [{"private": secret}]},
        }

    tool = server._tool_manager.get_tool("fixture")
    before_schema, before_annotations = tool.parameters.copy(), tool.annotations
    mcp_telemetry.instrument_manager_tools(server)
    assert tool.parameters == before_schema and tool.annotations == before_annotations
    assert inspect.signature(tool.fn) == inspect.signature(fixture)
    result = asyncio.run(server.call_tool("fixture", {"secret": "PRIVATE_TEST_DATA"}))
    content, structured = result
    assert json.loads(content[0].text) == structured
    trace = structured["tool_execution"]
    assert trace["wall_ms"] >= 0
    assert trace["started_at"] <= trace["ended_at"]
    assert trace["backend_elapsed_ms"] == 3 and trace["network_calls"] == 1 and trace["attempt_count"] == 1
    assert structured["execution"] == {
        "elapsed_ms": 3,
        "network_calls": 1,
        "attempts": [{"private": "PRIVATE_TEST_DATA"}],
    }
    assert [json.loads(event)["event"] for event in events] == ["mcp_tool_start", "mcp_tool_end"]
    assert "PRIVATE_TEST_DATA" not in "".join(events) and "private" not in "".join(events)


def test_each_e8_call_has_its_own_trace(tmp_path, monkeypatch):
    from tests.test_e1_fast_catalog_presentation import FakeServer

    events = []
    monkeypatch.setattr(mcp_telemetry.LOGGER, "info", events.append)
    monkeypatch.setattr(mcp_tools, "search_web_multi", lambda **_: {"ok": True})
    monkeypatch.setattr(mcp_tools, "fetch_page_excerpt", lambda **_: {"ok": True})
    monkeypatch.setattr(mcp_tools, "fetch_page_browser", lambda **_: {"ok": True})
    server = FakeServer()
    names = ("search_web_multi", "fetch_page_excerpt", "fetch_page_browser")
    mcp_tools.register_manager_tools(server, StoreState(tmp_path / "trace.sqlite3"), include_tools=names)
    for name in names:
        arguments = (
            {"query": "PRIVATE_QUERY"} if name == "search_web_multi" else {"url": "https://private.invalid/path"}
        )
        assert server.tools[name](**arguments)["tool_execution"]["tool"] == name
    assert [json.loads(event)["tool"] for event in events] == [name for name in names for _ in range(2)]
    assert "PRIVATE_QUERY" not in "".join(events) and "private.invalid" not in "".join(events)


def test_async_error_is_traced_without_exception_text(monkeypatch):
    events = []
    monkeypatch.setattr(mcp_telemetry.LOGGER, "info", events.append)

    async def backend():
        raise ValueError("PRIVATE_EXCEPTION_TEXT")

    function = mcp_telemetry.traced_tool(backend, "fixture_async")
    assert inspect.iscoroutinefunction(function)
    with pytest.raises(ValueError):
        asyncio.run(function())
    assert json.loads(events[-1])["outcome"] == "error"
    assert "PRIVATE_EXCEPTION_TEXT" not in "".join(events)


def test_sdk_error_result_keeps_content_and_error_outcome(monkeypatch):
    events = []
    monkeypatch.setattr(mcp_telemetry.LOGGER, "info", events.append)
    original = CallToolResult(isError=True, content=[TextContent(type="text", text="PRIVATE_ERROR_RESULT")])
    function = mcp_telemetry.traced_tool(lambda: original, "fixture_error")
    result = function()
    assert result.content == original.content and result.isError
    assert result.meta["tool_execution"]["outcome"] == "error"
    assert json.loads(events[-1])["outcome"] == "error"
    assert "PRIVATE_ERROR_RESULT" not in "".join(events)


def test_app_inventory_is_not_model_visible_declaration_evidence():
    expected = {"fixture": {"type": "object", "properties": {"detail": {"enum": ["summary", "full"]}}}}
    unverified = manager_session_schema_evidence(expected, expected, connected=True)
    assert unverified["app_schema_matches"] and not unverified["ok"]
    assert unverified["diagnostic"] == "model_declarations_unverified"
    stale = manager_session_schema_evidence(
        expected, expected, connected=True, declarations={"fixture": {"type": "object"}}
    )
    assert stale["diagnostic"] == "model_declarations_stale" and not stale["ok"]
    assert stale["reload_guarantees_declarations"] is False
    assert manager_session_schema_evidence(expected, expected, connected=True, declarations=expected)["ok"]
    assert not manager_session_schema_evidence(
        expected, expected, connected=True, tools_error=True, declarations=expected
    )["ok"]
    extra = {**expected, "removed_tool": {"type": "object"}}
    assert manager_session_schema_evidence(expected, extra, connected=True, declarations=expected)[
        "app_schema_drift_tools"
    ] == ["removed_tool"]
    assert manager_session_schema_evidence(expected, expected, connected=True, declarations=extra)[
        "declarations_drift_tools"
    ] == ["removed_tool"]


def test_concurrent_same_tool_calls_have_unique_correlated_ids(monkeypatch):
    events = []
    monkeypatch.setattr(mcp_telemetry.LOGGER, "info", events.append)
    monkeypatch.setattr(mcp_telemetry, "_utc_now", lambda: "2026-10-07T00:00:00.000Z")

    async def backend():
        await asyncio.sleep(0)
        return {
            "ok": True,
            "execution": {
                "elapsed_ms": 1,
                "network_request_count": 2,
                "attempt_count": 3,
                "provider_network_attempt_count": 2,
            },
        }

    function = mcp_telemetry.traced_tool(backend, "fixture_concurrent")

    async def run():
        return await asyncio.gather(function(), function())

    results = asyncio.run(run())
    ids = {row["tool_execution"]["call_id"] for row in results}
    assert len(ids) == 2
    for call_id in ids:
        paired = [json.loads(event) for event in events if json.loads(event)["call_id"] == call_id]
        assert [row["event"] for row in paired] == ["mcp_tool_start", "mcp_tool_end"]
        assert paired[-1]["network_calls"] == 2 and paired[-1]["attempt_count"] == 3


def test_thread_schema_status_paginates_without_reload_or_private_output():
    app = AppServer(WakeConfig("private-thread"))
    expected = {"first": {"type": "object"}, "second": {"type": "object"}}
    requests = []

    async def request(method, params):
        requests.append((method, params))
        index = len(requests) - 1
        name = ("first", "second")[index]
        return {
            "data": [
                {
                    "name": "autostopmanager",
                    "runtimeStatus": "connected",
                    "toolsError": None,
                    "httpOrigin": "PRIVATE_ORIGIN",
                    "tools": {name: {"name": name, "inputSchema": expected[name]}},
                }
            ],
            "nextCursor": "private-cursor" if index == 0 else None,
        }

    app.request = request
    report = asyncio.run(app.manager_mcp_schema_status(expected, declarations=expected))
    assert report["ok"] and report["app_tool_count"] == 2
    assert len(requests) == 2 and all(method == "mcpServerStatus/list" for method, _ in requests)
    assert requests[1][1]["cursor"] == "private-cursor"
    assert all(
        params["serverName"] == "autostopmanager" and params["threadId"] == "private-thread" for _, params in requests
    )
    assert "private" not in json.dumps(report) and "PRIVATE" not in json.dumps(report)
