from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata
from mcp.types import CallToolRequest, CallToolRequestParams, ToolAnnotations
import pytest

from autostop_manager import avito_listings, config, listing_executor, mcp_tools
from autostop_manager.listing_executor import BoundedListingExecutor
from autostop_manager.mcp_server import build_server
from autostop_manager.mcp_contract import MANAGER_MCP_CATALOG_PATH
from autostop_manager.storage import StoreState


AVITO_TOOLS = {"avito_search_listings", "avito_read_listing", "assess_avito_price_sample"}


@pytest.fixture
def native_server(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "manager.sqlite3"))
    monkeypatch.setattr(config, "_ENV_LOADED", False)
    for name in (
        "REEFAPI_API_KEY",
        "AUTOSTOP_STORE_API_URL",
        "AUTOSTOP_STORE_READ_TOKEN",
        "AUTOSTOP_STORE_MANAGE_TOKEN",
        "AUTOSTOP_STORE_OWNER_TOKEN",
        "AUTOSTOP_STORE_QUOTE_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    def forbidden_network(*args, **kwargs):
        raise AssertionError("native_mcp_test_must_not_contact_provider")

    monkeypatch.setattr(avito_listings, "_open_request", forbidden_network)
    server = FastMCP("isolated-avito-native-test")
    mcp_tools.register_manager_tools(server, StoreState(tmp_path / "store.sqlite3"), include_tools=AVITO_TOOLS)
    executor = BoundedListingExecutor()
    monkeypatch.setattr(listing_executor, "_EXECUTOR", executor)
    try:
        yield server
    finally:
        executor.close()


async def _call(server, name, arguments):
    # Exercise the installed SDK's original request handler, including its input
    # pre-parser and Pydantic validation, rather than calling Python wrappers.
    handler = server._mcp_server.request_handlers[CallToolRequest]
    result = await handler(CallToolRequest(params=CallToolRequestParams(name=name, arguments=arguments)))
    return result.root


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("avito_search_listings", {"query": "filter", "page": True}),
        ("avito_search_listings", {"query": "filter", "limit": True}),
        ("avito_search_listings", {"query": "filter", "price_min": True}),
        ("avito_search_listings", {"query": "filter", "delivery_only": "false"}),
        ("avito_search_listings", {"query": "filter", "page": "2"}),
        ("avito_search_listings", {"query": "filter", "price_max": True}),
        ("avito_search_listings", {"query": "filter", "price_min": "123"}),
        ("avito_search_listings", {"query": "filter", "price_min": "null"}),
        ("avito_search_listings", {"query": "filter", "price_max": "null"}),
        ("avito_search_listings", {"query": "filter", "limit": 2.0}),
        ("avito_search_listings", {"query": "filter", "dry_run": "false"}),
        ("avito_search_listings", {"query": "filter", "dry_run": 1}),
        ("avito_search_listings", {"query": "filter", "delivery_only": 0}),
        ("avito_read_listing", {"ad_id": "8386499447", "dry_run": "false"}),
        ("avito_read_listing", {"ad_id": "8386499447", "dry_run": 0}),
    ],
)
def test_original_wire_type_errors_never_reach_provider(native_server, monkeypatch, name, arguments):
    calls = []

    def request(action, payload, **kwargs):
        calls.append((action, payload))
        return {"ok": True, "data": {"listings": []}}

    monkeypatch.setattr(avito_listings, "_request_json", request)
    result = asyncio.run(_call(native_server, name, arguments))
    assert result.isError is True
    assert calls == []


@pytest.mark.parametrize(
    "parameters",
    [
        {"page": 0},
        {"page": 31},
        {"limit": 0},
        {"limit": 51},
        {"price_min": -1},
        {"price_min": 2_000_000_001},
        {"price_max": -1},
        {"price_max": 2_000_000_001},
    ],
)
def test_schema_bounds_reject_native_calls_before_provider(native_server, monkeypatch, parameters):
    calls = []
    monkeypatch.setattr(avito_listings, "_request_json", lambda *args, **kwargs: calls.append(args))
    result = asyncio.run(_call(native_server, "avito_search_listings", {"query": "filter", **parameters}))
    assert result.isError is True
    assert calls == []


def test_valid_original_json_types_reach_provider_exactly_once(native_server, monkeypatch):
    calls = []

    def request(action, payload, **kwargs):
        calls.append((action, payload))
        return {"ok": True, "data": {"listings": []}}

    monkeypatch.setattr(avito_listings, "_request_json", request)
    arguments = {
        "query": "filter",
        "page": 30,
        "limit": 50,
        "price_min": 0,
        "price_max": 2_000_000_000,
        "delivery_only": False,
        "dry_run": False,
    }
    result = asyncio.run(_call(native_server, "avito_search_listings", arguments))
    assert result.isError is False
    assert result.structuredContent["ok"] is True
    assert len(calls) == 1
    assert calls[0][0] == "search"
    assert calls[0][1]["page"] == 30


def test_actual_json_null_price_filters_remain_valid(native_server, monkeypatch):
    calls = []

    def request(action, payload, **kwargs):
        calls.append((action, payload))
        return {"ok": True, "data": {"listings": []}}

    monkeypatch.setattr(avito_listings, "_request_json", request)
    result = asyncio.run(
        _call(native_server, "avito_search_listings", {"query": "filter", "price_min": None, "price_max": None})
    )
    assert result.isError is False
    assert result.structuredContent["ok"] is True
    assert len(calls) == 1


def test_native_e10_schema_bounds_and_read_only_annotations_are_preserved(native_server):
    tools = {tool.name: tool for tool in asyncio.run(native_server.list_tools())}
    assert set(tools) == AVITO_TOOLS
    properties = tools["avito_search_listings"].inputSchema["properties"]
    for name, lower, upper in (("page", 1, 30), ("limit", 1, 50)):
        assert properties[name]["type"] == "integer"
        assert properties[name]["minimum"] == lower
        assert properties[name]["maximum"] == upper
    for name in ("price_min", "price_max"):
        integer = next(item for item in properties[name]["anyOf"] if item["type"] == "integer")
        assert integer["minimum"] == 0
        assert integer["maximum"] == 2_000_000_000
    for name in ("delivery_only", "dry_run"):
        assert properties[name]["type"] == "boolean"
    for tool in tools.values():
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
    assert tools["assess_avito_price_sample"].annotations.idempotentHint is True
    assert tools["assess_avito_price_sample"].annotations.openWorldHint is False


def test_scalar_preparser_is_scoped_to_two_e10_tools_and_preserves_other_tools(native_server):
    server = build_server()
    tools = server._tool_manager._tools
    manifest = json.loads(MANAGER_MCP_CATALOG_PATH.read_text(encoding="utf-8"))
    assert set(tools) == set(manifest["expected_tool_names"])
    for name, tool in tools.items():
        if name in {"avito_search_listings", "avito_read_listing"}:
            assert isinstance(tool.fn_metadata, mcp_tools._ListingScalarMetadata)
            assert tool.fn_metadata.pre_parse_json({"price_min": "null"}) == {"price_min": "null"}
        else:
            assert type(tool.fn_metadata) is FuncMetadata
    assessment = tools["assess_avito_price_sample"]
    assert assessment.fn_metadata.pre_parse_json({"listings": "[]"}) == {"listings": []}


@pytest.mark.parametrize(
    ("name", "arguments"),
    [("avito_search_listings", {"query": "filter"}), ("avito_read_listing", {"ad_id": "8386499447"})],
)
def test_native_async_control_finishes_while_the_provider_worker_is_held(native_server, monkeypatch, name, arguments):
    executor = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=5)
    monkeypatch.setattr(listing_executor, "_EXECUTOR", executor)
    release = threading.Event()
    worker_threads = []

    @native_server.tool(name="async_control", annotations=ToolAnnotations(readOnlyHint=True))
    async def control() -> dict[str, Any]:
        return {"ok": True}

    async def run():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()

        def provider(**kwargs):
            worker_threads.append(threading.get_ident())
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "test_provider_worker_was_not_released"
            return {"ok": True}

        monkeypatch.setattr(mcp_tools, name, provider)
        task = asyncio.create_task(_call(native_server, name, arguments))
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert not task.done()
        result = await asyncio.wait_for(_call(native_server, "async_control", {}), timeout=2)
        assert result.structuredContent == {"ok": True}
        assert not release.is_set()
        assert not task.done()
        assert len(worker_threads) == 1
        assert worker_threads[0] != threading.get_ident()
        release.set()
        assert (await task).structuredContent == {"ok": True}

    try:
        asyncio.run(run())
    finally:
        release.set()
        executor.close()


def test_native_cancelled_waiter_does_not_free_a_worker_and_dry_run_remains_prompt(native_server, monkeypatch):
    executor = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=5)
    monkeypatch.setattr(listing_executor, "_EXECUTOR", executor)
    release = threading.Event()
    calls = []

    async def run():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()

        def request(action, payload, **kwargs):
            calls.append(action)
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "test_provider_worker_was_not_released"
            return {"ok": True, "data": {"listings": []}}

        monkeypatch.setattr(avito_listings, "_request_json", request)
        task = asyncio.create_task(_call(native_server, "avito_search_listings", {"query": "filter"}))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert executor.pending_count == 1
        busy = await _call(native_server, "avito_search_listings", {"query": "filter"})
        assert busy.structuredContent["error"] == "provider_busy"
        assert busy.structuredContent["adapter_started"] is False
        for name, arguments in (
            ("avito_search_listings", {"query": "filter", "dry_run": True}),
            ("avito_read_listing", {"ad_id": "8386499447", "dry_run": True}),
        ):
            dry_run = await asyncio.wait_for(_call(native_server, name, arguments), timeout=2)
            assert dry_run.structuredContent["ok"] is True
            assert dry_run.structuredContent["dry_run"] is True
        assert executor.pending_count == 1
        assert calls == ["search"]
        release.set()
        async with asyncio.timeout(5):
            while executor.pending_count:
                await asyncio.sleep(0)

    try:
        asyncio.run(run())
    finally:
        release.set()
        executor.close()
