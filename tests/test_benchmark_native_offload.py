from __future__ import annotations

import asyncio
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
import inspect
import json
from pathlib import Path
import threading
from typing import Any

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import mcp_read_executor, mcp_tools
from autostop_manager.listing_executor import BoundedListingExecutor, ListingExecutionError
from autostop_manager.mcp_contract import mcp_schema_fingerprint
from autostop_manager.mcp_telemetry import instrument_manager_tools
from autostop_manager.vin_parts_benchmark import benchmark_vin_parts_lookup


NAME = "benchmark_vin_parts_lookup"
ITEMS = [
    {"identifier": "MR41S-000000", "identifier_type": "frame_number"},
    {"identifier": "ES1-0000000", "identifier_type": "frame_number"},
]
LOCAL_ARGUMENTS = {
    "items": ITEMS,
    "requested_part": "передние колодки 1 комплект",
    "live_vpic": False,
    "use_vpic_batch": False,
    "include_partsapi_dry_run": False,
    "live_partsapi_identity": False,
    "live_partsapi_oem": False,
    "resolve_oem": False,
    "max_live_calls": 0,
}


@pytest.fixture(autouse=True)
def inert_registration(monkeypatch):
    class Inert:
        def __init__(self, *args, **kwargs):
            pass

    def forbidden(*args, **kwargs):
        pytest.fail("Native benchmark tests must not construct real Store state or clients")

    for name in ("StoreState", "StoreApiClient"):
        monkeypatch.setattr(mcp_tools, name, forbidden)
    for name in ("StoreIntegration", "StoreOwnerApiClient", "StoreQuoteConductor", "StoreQuoteOwnerApi"):
        monkeypatch.setattr(mcp_tools, name, Inert)
    for name in (
        "get_store_api_url",
        "get_store_read_token",
        "get_store_manage_token",
        "get_store_quote_token",
        "get_store_owner_token",
    ):
        monkeypatch.setattr(mcp_tools, name, lambda: "")
    return Inert


def _native(monkeypatch, inert, backend, *, all_tools=False):
    @wraps(benchmark_vin_parts_lookup)
    def callback(*args: Any, **kwargs: Any) -> dict[str, Any]:
        bound = inspect.signature(benchmark_vin_parts_lookup).bind(*args, **kwargs)
        bound.apply_defaults()
        return backend(bound.arguments)

    monkeypatch.setattr(mcp_tools, NAME, callback)
    server = FastMCP("synthetic-native-benchmark")
    mcp_tools.register_manager_tools(
        server, store=inert(), store_client=inert(), include_tools=None if all_tools else {NAME}
    )
    return server


async def _drained(pool):
    while pool.pending_count:
        await asyncio.sleep(0)


async def _queued(pool):
    while pool.pending_count != 2:
        await asyncio.sleep(0)


@pytest.mark.parametrize("explicit", [False, True])
def test_native_benchmark_offloads_actual_callback_preserving_defaults(monkeypatch, inert_registration, explicit):
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=2)
    monkeypatch.setattr(mcp_read_executor, "_EXECUTOR", pool)
    arguments = deepcopy(LOCAL_ARGUMENTS) if explicit else {"items": deepcopy(ITEMS), "requested_part": "колодки"}
    if explicit:
        arguments.update(partsapi_timeout="3.0", max_candidates="1")
    original = deepcopy(arguments)
    observed = []
    payload = {"ok": True, "summary": {"count": 2}, "items": [{"index": 1}, {"index": 2}]}
    marker = ContextVar("synthetic_benchmark_context", default="missing")

    def backend(received):
        observed.append((threading.get_ident(), marker.get(), received))
        return deepcopy(payload)

    server = _native(monkeypatch, inert_registration, backend)
    tool = server._tool_manager.get_tool(NAME)
    signature = inspect.signature(tool.fn)
    parameters, metadata = tool.parameters, tool.fn_metadata
    wrapped = tool.fn
    instrument_manager_tools(server)
    assert tool.fn is wrapped

    async def run():
        marker.set("copied")
        loop_thread = threading.get_ident()
        result = await tool.run(arguments)
        assert {key: value for key, value in result.items() if key != "tool_execution"} == payload
        assert result["tool_execution"]["outcome"] == "ok"
        assert observed[0][0] != loop_thread
        assert observed[0][1] == "copied"

    try:
        asyncio.run(run())
    finally:
        pool.close()
    assert arguments == original
    assert tool.is_async and tool.parameters is parameters and tool.fn_metadata is metadata
    assert inspect.signature(tool.fn) == signature == inspect.signature(benchmark_vin_parts_lookup)
    expected = {
        "items": ITEMS,
        "requested_part": arguments["requested_part"],
        "city": "Красноярск",
        "live_vpic": not explicit,
        "use_vpic_batch": not explicit,
        "include_partsapi_dry_run": not explicit,
        "live_partsapi_identity": False,
        "live_partsapi_oem": False,
        "resolve_oem": False,
        "max_live_calls": 0 if explicit else 3,
        "max_candidates": 1 if explicit else 3,
        "partsapi_category_index": None,
        "partsapi_timeout": 3.0 if explicit else 20.0,
    }
    assert observed[0][2] == expected


def test_native_benchmark_keeps_loop_responsive_and_bounds_slots(monkeypatch, inert_registration):
    pool = BoundedListingExecutor(max_workers=2, max_waiting_calls=0, timeout_seconds=2)
    monkeypatch.setattr(mcp_read_executor, "_EXECUTOR", pool)
    release = threading.Event()
    entered = [asyncio.Event(), asyncio.Event()]
    loop = None
    calls = []

    def backend(received):
        index = int(received["requested_part"])
        calls.append(received)
        loop.call_soon_threadsafe(entered[index].set)
        assert release.wait(2)
        return {"ok": True, "synthetic": True}

    server = _native(monkeypatch, inert_registration, backend)
    tool = server._tool_manager.get_tool(NAME)
    server.tool(name="synthetic_ping")(lambda: {"ok": True})

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        assert tool.is_async
        tasks = [
            asyncio.create_task(tool.run({**deepcopy(LOCAL_ARGUMENTS), "requested_part": str(index)}))
            for index in range(2)
        ]
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 1)
            assert (await server._tool_manager.get_tool("synthetic_ping").run({}))["ok"]
            assert pool.pending_count == 2 and not any(task.done() for task in tasks)
            busy = await tool.run(deepcopy(LOCAL_ARGUMENTS))
            assert busy["error"] == "provider_busy" and busy["stage"] == "admission"
            assert busy["adapter_started"] is False and busy["retry_attempted"] is False
            assert busy["tool_execution"]["outcome"] == "error"
            assert len(calls) == 2
            release.set()
            results = await asyncio.gather(*tasks)
            assert all(row["ok"] for row in results)
            assert len({row["tool_execution"]["call_id"] for row in results}) == 2
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        release.set()
        pool.close()


@pytest.mark.parametrize("queued", [False, True])
def test_native_benchmark_cancellation_retains_real_worker_admission(monkeypatch, inert_registration, queued):
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=int(queued), timeout_seconds=2)
    monkeypatch.setattr(mcp_read_executor, "_EXECUTOR", pool)
    release = threading.Event()
    entered = asyncio.Event()
    loop = None
    calls = []

    def backend(received):
        calls.append(received["requested_part"])
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        return {"ok": True}

    tool = _native(monkeypatch, inert_registration, backend)._tool_manager.get_tool(NAME)

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        assert tool.is_async
        running = asyncio.create_task(tool.run(deepcopy(LOCAL_ARGUMENTS)))
        tasks = [running]
        try:
            await asyncio.wait_for(entered.wait(), 1)
            cancelled = running
            if queued:
                cancelled = asyncio.create_task(tool.run({**deepcopy(LOCAL_ARGUMENTS), "requested_part": "queued"}))
                tasks.append(cancelled)
                await asyncio.wait_for(_queued(pool), 1)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            assert pool.pending_count == 1 + int(queued)
            assert calls == [LOCAL_ARGUMENTS["requested_part"]]
            busy = await tool.run(deepcopy(LOCAL_ARGUMENTS))
            assert busy["error"] == "provider_busy" and busy["adapter_started"] is False
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.wait_for(_drained(pool), 1)
            assert calls == [LOCAL_ARGUMENTS["requested_part"]]
            assert (await tool.run(deepcopy(LOCAL_ARGUMENTS)))["ok"]
            assert len(calls) == 2
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        release.set()
        pool.close()


@pytest.mark.parametrize("started", [False, True])
def test_native_benchmark_reports_executor_refusal_without_echo(monkeypatch, inert_registration, started):
    class FailedPool:
        async def run(self, operation):
            raise ListingExecutionError(
                "provider_wait_timeout" if started else "provider_busy",
                stage="worker_wait" if started else "admission",
                adapter_started=started,
                adapter_may_continue=started,
            )

    monkeypatch.setattr(mcp_read_executor, "_EXECUTOR", FailedPool())

    def backend(received):
        pytest.fail("The refused synthetic operation must not be dispatched")

    tool = _native(monkeypatch, inert_registration, backend)._tool_manager.get_tool(NAME)
    result = asyncio.run(tool.run(deepcopy(LOCAL_ARGUMENTS)))
    assert result["ok"] is False and result["outcome"] == "provider_error"
    assert result["error"] == ("provider_wait_timeout" if started else "provider_busy")
    assert result["adapter_started"] is started and result["adapter_may_continue"] is started
    assert result["retryable"] is False and result["retry_attempted"] is False
    assert all(row["identifier"] not in json.dumps(result) for row in ITEMS)


def test_benchmark_offload_preserves_all_native_schemas_annotations_and_signatures(monkeypatch, inert_registration):
    monkeypatch.setattr(mcp_tools, "instrument_manager_tools", lambda server: None)
    server = _native(monkeypatch, inert_registration, lambda received: {"ok": True}, all_tools=True)
    tools = server._tool_manager._tools
    signatures = {name: inspect.signature(tool.fn) for name, tool in tools.items()}

    def snapshot():
        return {
            tool.name: {"inputSchema": tool.inputSchema, "annotations": tool.annotations}
            for tool in asyncio.run(server.list_tools())
        }

    before = snapshot()
    manifest = json.loads((Path(__file__).resolve().parents[1] / "docs/agent/manager_mcp_catalog.json").read_text())
    assert len(before) == manifest["expected_tool_count"] and set(before) == set(manifest["expected_tool_names"])
    assert (
        mcp_schema_fingerprint({name: row["inputSchema"] for name, row in before.items()})
        == manifest["schema_fingerprint"]
    )
    instrument_manager_tools(server)
    assert snapshot() == before
    assert {name: inspect.signature(tool.fn) for name, tool in tools.items()} == signatures
    assert tools[NAME].is_async
