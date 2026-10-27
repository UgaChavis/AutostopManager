"""Native metadata checks over memory streams or mocked HTTP, never a listener."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import anyio
import httpx
from mcp import ClientSession, types
from mcp.shared.message import SessionMessage
import pytest

from autostop_manager import cli, mcp_contract, mcp_probe as probe


URL = "http://synthetic.invalid/mcp"
REAL_HTTP_CLIENT = httpx.AsyncClient


@pytest.fixture
def peer(tmp_path, monkeypatch):
    names = json.loads(mcp_contract.MANAGER_MCP_CATALOG_PATH.read_text())["expected_tool_names"]
    tools = [
        types.Tool(
            name=name,
            inputSchema={"type": "object", "properties": {"fixture": {"const": name}}},
            annotations=types.ToolAnnotations(**probe.EFFECTFUL_TOOL_ANNOTATIONS[name])
            if name in probe.EFFECTFUL_TOOL_ANNOTATIONS
            else None,
        )
        for name in names
    ]
    manifest = tmp_path / "metadata-catalog.json"
    manifest.write_text(
        json.dumps(
            {
                "format": "mcp_surface_manifest_v1",
                "expected_tool_names": names,
                "expected_tool_count": len(names),
                "schema_fingerprint": mcp_contract.mcp_schema_fingerprint({t.name: t.inputSchema for t in tools}),
            }
        )
    )
    state = SimpleNamespace(
        tools=tools,
        requests=[],
        tool_calls=[],
        transport_kwargs=[],
        http_kwargs=[],
        schemas_checked=[],
        allow_tool_calls=False,
        stall=None,
        page_delay=0,
        cycle=False,
        cleanup_error=False,
    )

    def validate(schemas):
        state.schemas_checked.append(schemas)
        return mcp_contract.validate_manager_mcp_surface(schemas, manifest_path=manifest)

    class HttpClient:
        def __init__(self, **kwargs):
            state.http_kwargs.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    original_call_tool = ClientSession.call_tool

    async def call_tool(session, name, *args, **kwargs):
        state.tool_calls.append(name)
        assert state.allow_tool_calls, "metadata-only attempted a project tool call"
        return await original_call_tool(session, name, *args, **kwargs)

    @asynccontextmanager
    async def transport(_url, **kwargs):
        assert _url == URL
        state.transport_kwargs.append(kwargs)
        response_writer, response_reader = anyio.create_memory_object_stream[SessionMessage](10)
        request_writer, request_reader = anyio.create_memory_object_stream[SessionMessage](10)

        async def respond():
            async for envelope in request_reader:
                request = envelope.message.root
                state.requests.append(request.method)
                if not isinstance(request, types.JSONRPCRequest) or request.method == state.stall:
                    continue
                if request.method == "initialize":
                    result = {
                        "protocolVersion": types.LATEST_PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "synthetic-metadata-peer", "version": "1"},
                    }
                elif request.method == "ping":
                    result = {}
                elif request.method == "tools/list":
                    await asyncio.sleep(state.page_delay)
                    cursor = (request.params or {}).get("cursor")
                    start = int(cursor or 0)
                    end = start + 23
                    result = types.ListToolsResult(
                        tools=state.tools[start:end],
                        nextCursor="1" if state.cycle else str(end) if end < len(state.tools) else None,
                    ).model_dump(mode="json", by_alias=True, exclude_none=True)
                elif request.method == "tools/call":
                    name = request.params["name"]
                    payload = {
                        "catalog_provider_status": {"ok": True, "stage": "catalog_cross", "providers": []},
                        "resolve_vin_oem_parts": {
                            "status": "planned",
                            "calls": [{"operation": "vin_decode", "dry_run": True}],
                            "live_call_count": 0,
                            "oem_candidates": [],
                        },
                    }[name]
                    result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
                else:
                    raise AssertionError("unexpected RPC method: " + request.method)
                await response_writer.send(
                    SessionMessage(
                        types.JSONRPCMessage(types.JSONRPCResponse(jsonrpc="2.0", id=request.id, result=result))
                    )
                )

        async with response_writer, response_reader, request_writer, request_reader:
            task = asyncio.create_task(respond())
            try:
                yield response_reader, request_writer, None
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if state.cleanup_error:
                    raise httpx.ConnectError("synthetic metadata cleanup failure")

    monkeypatch.setattr(probe, "validate_manager_mcp_surface", validate)
    monkeypatch.setattr(probe.httpx, "AsyncClient", HttpClient)
    monkeypatch.setattr(probe, "streamable_http_client", transport)
    monkeypatch.setattr(ClientSession, "call_tool", call_tool)
    return state


def test_metadata_only_lists_all_pages_without_tool_calls(peer):
    report = asyncio.run(probe.async_probe_manager_mcp(URL, metadata_only=True))

    assert report["ok"] is True
    assert report["diagnostic"] == "ok"
    assert report["mode"] == "metadata_only"
    assert set(report["checks"]) == {"native_ping", "tools_list"}
    expected_names = {tool.name for tool in peer.tools}
    assert {"manager_roles", "manager_role_action"} <= expected_names
    assert report["checks"]["tools_list"]["tool_count"] == len(expected_names)
    assert report["checks"]["tools_list"]["effectful_annotations_checked"] == 14
    assert report["checks"]["tools_list"]["annotation_mismatch_tools"] == []
    assert set(peer.schemas_checked[0]) == expected_names
    expected_pages = (len(expected_names) + 22) // 23
    assert peer.requests == ["initialize", "notifications/initialized", "ping"] + ["tools/list"] * expected_pages
    assert peer.tool_calls == []
    assert peer.transport_kwargs[0]["terminate_on_close"] is False
    assert report["privacy"]["store_order_read_attempted"] is False


def test_metadata_only_cli_returns_bounded_json(peer, capsys):
    assert cli.main(["mcp-probe", "--url", URL, "--metadata-only"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "metadata_only"
    assert set(report["checks"]) == {"native_ping", "tools_list"}
    assert peer.tool_calls == []


def test_metadata_only_cleanup_failure_is_not_success(peer, capsys):
    peer.cleanup_error = True
    report = probe.probe_manager_mcp(URL, metadata_only=True)
    assert report["ok"] is False
    assert report["diagnostic"] == "transport_route_unavailable"
    assert report["checks"]["tools_list"]["ok"] is True
    assert report["checks"]["transport"]["ok"] is False
    assert cli.main(["mcp-probe", "--url", URL, "--metadata-only"]) == 1
    assert json.loads(capsys.readouterr().out) == report
    assert peer.tool_calls == []


@pytest.mark.parametrize("explicit_false", [False, True])
def test_default_mode_calls_and_report_stay_unchanged(peer, capsys, explicit_false):
    peer.allow_tool_calls = True
    options = {"metadata_only": False} if explicit_false else {}
    report = probe.probe_manager_mcp(URL, **options)
    assert report["ok"] is True
    assert "mode" not in report
    assert set(report["checks"]) == {"native_ping", "tools_list", "catalog_provider_status", "synthetic_resolver"}
    assert peer.tool_calls == ["catalog_provider_status", "resolve_vin_oem_parts"]
    assert set(peer.transport_kwargs[0]) == {"http_client"}
    assert cli.main(["mcp-probe", "--url", URL]) == 0
    assert json.loads(capsys.readouterr().out) == report
    assert peer.tool_calls == ["catalog_provider_status", "resolve_vin_oem_parts"] * 2
    assert all(set(kwargs) == {"http_client"} for kwargs in peer.transport_kwargs)


@pytest.mark.parametrize(
    "options",
    [
        {"provider_failure_check": True},
        {"store_check": True},
        {"browser_check": True},
        {"provider_failure_check": True, "store_check": True, "browser_check": True},
    ],
)
def test_metadata_only_rejects_mixed_flags_before_http_client(peer, capsys, options):
    report = asyncio.run(probe.async_probe_manager_mcp(URL, metadata_only=True, **options))
    assert report["ok"] is False
    assert report["diagnostic"] == "invalid_probe_options"
    assert report["mode"] == "metadata_only"
    assert report["checks"] == {}
    args = ["mcp-probe", "--url", URL, "--metadata-only"] + ["--" + name.replace("_", "-") for name in options]
    assert cli.main(args) == 1
    assert json.loads(capsys.readouterr().out) == report
    assert peer.http_kwargs == peer.transport_kwargs == peer.requests == peer.tool_calls == []


@pytest.mark.parametrize("drift", ["schema", "missing", "duplicate", "annotation"])
def test_metadata_only_rejects_surface_drift_without_tool_calls(peer, capsys, drift):
    if drift == "schema":
        peer.tools[0].inputSchema = {"type": "null"}
    elif drift == "missing":
        peer.tools.pop()
    elif drift == "duplicate":
        peer.tools.append(peer.tools[0])
    else:
        tool = next(tool for tool in peer.tools if tool.name in probe.EFFECTFUL_TOOL_ANNOTATIONS)
        tool.annotations.readOnlyHint = not tool.annotations.readOnlyHint
    assert cli.main(["mcp-probe", "--url", URL, "--metadata-only"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert report["mode"] == "metadata_only"
    assert report["checks"]["tools_list"]["ok"] is False
    assert peer.tool_calls == []
    if drift == "annotation":
        assert report["diagnostic"] == "annotation_mismatch"
        assert report["checks"]["tools_list"]["annotation_mismatch_tools"] == [tool.name]
    if drift == "duplicate":
        assert report["checks"]["tools_list"]["warnings"] == ["manager_mcp_transport_duplicate_tool_name"]


@pytest.mark.parametrize("stage", ["initialize", "ping", "tools/list", "slow_pages", "cursor_cycle"])
def test_metadata_only_bounds_rpc_and_pagination(peer, stage):
    peer.stall = stage
    peer.page_delay = 0.02 if stage == "slow_pages" else 0
    peer.cycle = stage == "cursor_cycle"
    report = probe.probe_manager_mcp(URL, metadata_only=True, timeout=0.03)
    assert report["ok"] is False
    assert report["diagnostic"] == "transport_failure"
    assert report["checks"]["transport"]["ok"] is False
    assert peer.tool_calls == []
    if stage == "slow_pages":
        assert 1 <= peer.requests.count("tools/list") < 4
    if stage == "cursor_cycle":
        assert peer.requests.count("tools/list") == 2


def test_metadata_only_real_http_transport_never_deletes_or_calls_tools(peer, monkeypatch):
    from mcp.client.streamable_http import streamable_http_client

    requests = []

    def respond(request):
        body = json.loads(request.content) if request.method == "POST" else None
        requests.append((request.method, body and body["method"]))
        if request.method == "GET":
            return httpx.Response(405)
        assert request.method == "POST", "metadata-only attempted an HTTP mutation"
        method = body["method"]
        if method == "initialize":
            result = {
                "protocolVersion": types.LATEST_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-http-metadata", "version": "1"},
            }
        elif method == "notifications/initialized":
            return httpx.Response(202)
        elif method == "ping":
            result = {}
        else:
            assert method == "tools/list"
            result = types.ListToolsResult(tools=peer.tools).model_dump(mode="json", by_alias=True, exclude_none=True)
        return httpx.Response(
            200,
            headers={"Mcp-Session-Id": "synthetic-session"},
            json={"jsonrpc": "2.0", "id": body["id"], "result": result},
        )

    monkeypatch.setattr(probe, "streamable_http_client", streamable_http_client)
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        lambda **kwargs: REAL_HTTP_CLIENT(transport=httpx.MockTransport(respond), trust_env=False, **kwargs),
    )
    report = probe.probe_manager_mcp(URL, metadata_only=True)
    assert report["ok"] is True
    assert [method for verb, method in requests if verb == "POST"] == [
        "initialize",
        "notifications/initialized",
        "ping",
        "tools/list",
    ]
    assert all(verb in {"POST", "GET"} for verb, _method in requests)
    assert peer.tool_calls == []
