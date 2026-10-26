from __future__ import annotations

import asyncio
from contextvars import ContextVar
from copy import deepcopy
import inspect
import json
from pathlib import Path
import threading

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from autostop_manager import mcp_read_executor as executor, mcp_tools
from autostop_manager.listing_executor import BoundedListingExecutor, ListingExecutionError
from autostop_manager.mcp_contract import mcp_schema_fingerprint
from autostop_manager.mcp_telemetry import instrument_manager_tools


NAME = "j1_research_results"
GENERAL = "autostop.j1.research.v1"
VIN = "autostop.j1.vin_research.v1"
FIRST, SECOND, THIRD = "0" * 32, "1" * 32, "2" * 32
DEFAULT_POOL = getattr(executor, "_J1_RESULTS_EXECUTOR", None)
PAYLOADS = [
    {
        "ok": True,
        "schema": GENERAL,
        "job_id": FIRST,
        "query": "",
        "cursor": 0,
        "total": 2,
        "next_cursor": 1,
        "results": [{"document_id": "d" * 32, "status": "fetched"}],
    },
    {
        "ok": True,
        "schema": VIN,
        "job_id": FIRST,
        "total": 1,
        "next_cursor": None,
        "results": [{"document_id": "d" * 32, "matching_pages": [1], "document_revision": 1}],
    },
    {"ok": False, "schema": GENERAL, "error": {"code": "job_not_found"}},
    {"ok": False, "schema": VIN, "error": {"code": "vin_job_expired"}},
    {"ok": False, "schema": GENERAL, "error": {"code": "vin_store_unavailable"}},
]


@pytest.fixture(autouse=True)
def inert_registration(monkeypatch):
    class Inert:
        def __init__(self, *args, **kwargs):
            pass

    def forbidden(*args, **kwargs):
        pytest.fail("J1 results transport tests must not construct Store or read configuration")

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
    private_pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=30)
    monkeypatch.setattr(executor, "_J1_RESULTS_EXECUTOR", private_pool, raising=False)
    private_status = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=30)
    monkeypatch.setattr(executor, "_J1_STATUS_EXECUTOR", private_status)
    private_report = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=30)
    monkeypatch.setattr(executor, "_J1_REPORT_EXECUTOR", private_report)
    yield Inert
    pool = getattr(executor, "_J1_RESULTS_EXECUTOR", None)
    if isinstance(pool, BoundedListingExecutor) and pool is not private_pool:
        pool.close()
    private_pool.close()
    private_status.close()
    private_report.close()


def _native(monkeypatch, inert, backend, *, all_tools=False, include_peers=False):
    assert Path(mcp_tools.__file__).resolve() == Path(__file__).resolve().parents[1] / "autostop_manager/mcp_tools.py"
    monkeypatch.setattr(mcp_tools, "research_results", backend)
    names = {NAME}
    if include_peers:
        names.update({"j1_research_status", "j1_research_report"})
        monkeypatch.setattr(
            mcp_tools,
            "research_status",
            lambda **kwargs: {"ok": True, "schema": "autostop.j1.research.v1", "synthetic_status": True},
        )
        monkeypatch.setattr(
            mcp_tools,
            "research_report",
            lambda **kwargs: {"ok": True, "schema": "autostop.j1.report.v1", "synthetic_report": True},
        )
    server = FastMCP("synthetic-j1-results-transport")
    mcp_tools.register_manager_tools(
        server, store=inert(), store_client=inert(), include_tools=None if all_tools else names
    )
    return server


async def _pending(pool, count):
    while pool.pending_count != count:
        await asyncio.sleep(0)


def _failure(result, reason, *, started, may_continue):
    assert result["ok"] is False and result["schema"] == GENERAL
    assert result["error"] == {"code": "j1_results_executor_unavailable", "reason": reason}
    assert result["adapter_started"] is started and result["adapter_may_continue"] is may_continue
    assert result["outcome_uncertain"] is started
    assert result["retryable"] is False and result["retry_attempted"] is False
    assert "job_id" not in result and "outcome" not in result
    assert result["tool_execution"]["outcome"] == "error"
    assert FIRST not in json.dumps(result)


@pytest.mark.parametrize(
    "payload", PAYLOADS, ids=["general_ok", "vin_ok", "general_error", "vin_error", "routing_error"]
)
def test_native_results_offloads_and_preserves_domain_response(monkeypatch, inert_registration, payload):
    marker = ContextVar("synthetic_results_context", default="missing")
    observed = []

    def backend(*, job_id, query="", cursor=0, limit=20):
        observed.append((threading.get_ident(), marker.get(), (job_id, query, cursor, limit)))
        return deepcopy(payload)

    server = _native(monkeypatch, inert_registration, backend)
    tool = server._tool_manager.get_tool(NAME)
    signature, parameters, metadata = inspect.signature(tool.fn), tool.parameters, tool.fn_metadata
    wrapper = tool.fn
    instrument_manager_tools(server)
    assert tool.fn is wrapper

    async def run():
        marker.set("copied")
        loop_thread = threading.get_ident()
        arguments = {"job_id": FIRST}
        result = await tool.run(arguments)
        assert arguments == {"job_id": FIRST}
        stripped = {key: value for key, value in result.items() if key != "tool_execution"}
        assert json.dumps(stripped, separators=(",", ":")) == json.dumps(payload, separators=(",", ":"))
        assert observed[0][0] != loop_thread
        assert observed[0][1:] == ("copied", (FIRST, "", 0, 20))

    asyncio.run(run())
    assert tool.is_async and tool.parameters is parameters and tool.fn_metadata is metadata
    assert inspect.signature(tool.fn) == signature


@pytest.mark.parametrize(
    ("cursor", "limit", "query", "expected_cursor", "expected_limit"),
    [
        ("2", "1", "synthetic needle", 2, 1),
        (2**63, 1, "", 2**63, 1),
        (10**100, 1, "synthetic needle", 10**100, 1),
        (1.0, 1.0, "", 1, 1),
    ],
)
def test_native_results_preserves_sdk_coercions_and_unbounded_positive_cursor(
    monkeypatch, inert_registration, cursor, limit, query, expected_cursor, expected_limit
):
    received = []

    def backend(*, job_id, query, cursor, limit):
        received.append((job_id, query, cursor, limit))
        return {"ok": True, "schema": GENERAL, "cursor": cursor, "next_cursor": None, "results": []}

    tool = _native(monkeypatch, inert_registration, backend)._tool_manager.get_tool(NAME)
    arguments = {"job_id": FIRST, "query": query, "cursor": cursor, "limit": limit}
    original = deepcopy(arguments)
    result = asyncio.run(tool.run(arguments))
    assert arguments == original
    assert received == [(FIRST, query, expected_cursor, expected_limit)]
    assert type(received[0][2]) is type(received[0][3]) is int
    assert result["cursor"] == expected_cursor


def test_results_pool_is_lazy_fixed_and_validation_precedes_admission(monkeypatch, inert_registration):
    called = []

    def backend(*, job_id, query="", cursor=0, limit=20):
        called.append(job_id)
        return {"ok": True}

    assert DEFAULT_POOL._limit == 2 and DEFAULT_POOL._timeout_seconds == 30
    assert DEFAULT_POOL._pool._max_workers == 1
    pool = executor._J1_RESULTS_EXECUTOR
    tool = _native(monkeypatch, inert_registration, backend)._tool_manager.get_tool(NAME)
    assert {key: tool.parameters["properties"][key]["default"] for key in ("query", "cursor", "limit")} == {
        "query": "",
        "cursor": 0,
        "limit": 20,
    }
    assert not pool._pool._threads
    with pytest.raises(ToolError):
        asyncio.run(tool.run({"job_id": None}))
    assert not pool._pool._threads and called == []
    for _ in range(2):
        assert asyncio.run(tool.run({"job_id": FIRST}))["ok"]
    assert len(pool._pool._threads) == 1
    assert called == [FIRST, FIRST]


def test_results_has_one_worker_one_queue_and_independent_capacity(monkeypatch, inert_registration):
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=2)
    monkeypatch.setattr(executor, "_J1_RESULTS_EXECUTOR", pool)
    other_pools = [BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=2) for _ in range(3)]
    for name, other in zip(("_EXECUTOR", "_WEB_EXECUTOR", "_CONTROL_EXECUTOR"), other_pools, strict=True):
        monkeypatch.setattr(executor, name, other)
    release = threading.Event()
    entered = asyncio.Event()
    calls = []
    loop = None

    def backend(*, job_id, query="", cursor=0, limit=20):
        calls.append(job_id)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        return {"ok": True, "schema": GENERAL, "job_id": job_id}

    assert pool is not executor._J1_STATUS_EXECUTOR
    assert pool is not executor._J1_REPORT_EXECUTOR
    server = _native(monkeypatch, inert_registration, backend, include_peers=True)
    for name in ("partsapi_catalog_lookup", "fetch_page_excerpt", "manager_automations"):
        server.tool(name=name)(lambda: {"ok": True})
    instrument_manager_tools(server)
    tool = server._tool_manager.get_tool(NAME)

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        assert tool.is_async
        running = asyncio.create_task(tool.run({"job_id": FIRST}))
        tasks = [running]
        try:
            await asyncio.wait_for(entered.wait(), 1)
            tasks.append(asyncio.create_task(tool.run({"job_id": SECOND})))
            await asyncio.wait_for(_pending(pool, 2), 1)
            assert calls == [FIRST] and not any(task.done() for task in tasks)
            for name in ("partsapi_catalog_lookup", "fetch_page_excerpt", "manager_automations"):
                assert (await server._tool_manager.get_tool(name).run({}))["ok"]
            status = await server._tool_manager.get_tool("j1_research_status").run({"job_id": FIRST})
            assert status["ok"] and status["synthetic_status"] and status["schema"] == "autostop.j1.research.v1"
            report = await server._tool_manager.get_tool("j1_research_report").run({"job_id": FIRST})
            assert report["ok"] and report["synthetic_report"] and report["schema"] == "autostop.j1.report.v1"
            assert pool.pending_count == 2 and calls == [FIRST]
            _failure(await tool.run({"job_id": THIRD}), "busy", started=False, may_continue=False)
            release.set()
            result = await asyncio.gather(*tasks)
            assert [row["job_id"] for row in result] == [FIRST, SECOND]
            assert calls == [FIRST, SECOND]
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        release.set()
        for other in other_pools:
            other.close()


@pytest.mark.parametrize("cancel_queued", [False, True])
def test_results_cancellation_holds_slots_and_abandoned_queue_does_not_start(
    monkeypatch, inert_registration, cancel_queued
):
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=2)
    monkeypatch.setattr(executor, "_J1_RESULTS_EXECUTOR", pool)
    release = threading.Event()
    entered = asyncio.Event()
    loop = None
    calls = []

    def backend(*, job_id, query="", cursor=0, limit=20):
        calls.append(job_id)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        return {"ok": True}

    tool = _native(monkeypatch, inert_registration, backend)._tool_manager.get_tool(NAME)

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        assert tool.is_async
        running = asyncio.create_task(tool.run({"job_id": FIRST}))
        tasks = [running]
        try:
            await asyncio.wait_for(entered.wait(), 1)
            queued = asyncio.create_task(tool.run({"job_id": SECOND}))
            tasks.append(queued)
            await asyncio.wait_for(_pending(pool, 2), 1)
            cancelled = queued if cancel_queued else running
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            assert pool.pending_count == 2 and calls == [FIRST]
            _failure(await tool.run({"job_id": THIRD}), "busy", started=False, may_continue=False)
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.wait_for(_pending(pool, 0), 1)
            assert calls == ([FIRST] if cancel_queued else [FIRST, SECOND])
            assert (await tool.run({"job_id": THIRD}))["ok"]
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_results_caller_timeout_does_not_stop_worker_or_claim_no_effects(monkeypatch, inert_registration):
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=2)
    monkeypatch.setattr(executor, "_J1_RESULTS_EXECUTOR", pool)
    release, finished = threading.Event(), threading.Event()
    entered = asyncio.Event()
    loop = None

    def backend(*, job_id, query="", cursor=0, limit=20):
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(10)
            return {"ok": True}
        finally:
            finished.set()

    tool = _native(monkeypatch, inert_registration, backend)._tool_manager.get_tool(NAME)

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(tool.run({"job_id": FIRST}))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            result = await task
            _failure(result, "wait_timeout", started=True, may_continue=True)
            assert entered.is_set() and not finished.is_set() and pool.pending_count == 1
            release.set()
            await asyncio.wait_for(_pending(pool, 0), 1)
            assert finished.is_set()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_completed_worker_exception_still_has_uncertain_housekeeping_outcome(monkeypatch, inert_registration):
    sensitive = "A" * 17
    entered = []

    def backend(*, job_id, query="", cursor=0, limit=20):
        entered.append(True)
        raise RuntimeError(sensitive)

    tool = _native(monkeypatch, inert_registration, backend)._tool_manager.get_tool(NAME)
    result = asyncio.run(tool.run({"job_id": FIRST}))
    _failure(result, "execution_failed", started=True, may_continue=False)
    assert entered == [True] and sensitive not in str(result) and sensitive not in repr(result)


@pytest.mark.parametrize(
    ("code", "reason", "started", "continuing"),
    [
        ("provider_busy", "busy", False, False),
        ("provider_wait_timeout", "wait_timeout", True, True),
        ("provider_execution_failed", "execution_failed", True, False),
        ("provider_executor_unavailable", "executor_unavailable", False, False),
        ("provider_executor_draining", "executor_draining", False, False),
        ("A" * 17, "executor_unavailable", True, False),
    ],
)
def test_results_executor_failures_have_finite_private_j1_transport_shape(
    monkeypatch, inert_registration, code, reason, started, continuing
):
    class FailedPool:
        async def run(self, operation):
            raise ListingExecutionError(
                code,
                stage="worker_wait" if started else "admission",
                adapter_started=started,
                adapter_may_continue=continuing,
            )

    monkeypatch.setattr(executor, "_J1_RESULTS_EXECUTOR", FailedPool())

    def backend(*, job_id, query="", cursor=0, limit=20):
        pytest.fail("Refusal must not route J1 or run the domain callback")

    result = asyncio.run(
        _native(monkeypatch, inert_registration, backend)._tool_manager.get_tool(NAME).run({"job_id": FIRST})
    )
    _failure(result, reason, started=started, may_continue=continuing)
    assert result["stage"] == ("worker_wait" if started else "admission")
    assert "A" * 17 not in str(result)


def test_results_offload_preserves_all_native_metadata_and_effectful_hints(monkeypatch, inert_registration):
    monkeypatch.setattr(mcp_tools, "instrument_manager_tools", lambda server: None)
    server = _native(monkeypatch, inert_registration, lambda **kwargs: {"ok": True}, all_tools=True)
    tools = server._tool_manager._tools
    signatures = {name: inspect.signature(tool.fn) for name, tool in tools.items()}

    def snapshot():
        return {
            tool.name: {"inputSchema": tool.inputSchema, "annotations": tool.annotations}
            for tool in asyncio.run(server.list_tools())
        }

    before = snapshot()
    manifest = json.loads((Path(__file__).resolve().parents[1] / "docs/agent/manager_mcp_catalog.json").read_text())
    assert len(before) == 70 and set(before) == set(manifest["expected_tool_names"])
    assert (
        mcp_schema_fingerprint({name: row["inputSchema"] for name, row in before.items()})
        == manifest["schema_fingerprint"]
    )
    assert not executor._J1_RESULTS_EXECUTOR._pool._threads
    instrument_manager_tools(server)
    assert snapshot() == before
    assert {name: inspect.signature(tool.fn) for name, tool in tools.items()} == signatures
    annotations = before[NAME]["annotations"]
    assert not annotations.readOnlyHint and annotations.destructiveHint
    assert not annotations.idempotentHint and not annotations.openWorldHint
    assert tools[NAME].is_async and not executor._J1_RESULTS_EXECUTOR._pool._threads
    assert executor.BLOCKING_J1_RESULTS_TOOLS == {NAME}
    assert executor.BLOCKING_J1_STATUS_TOOLS == {"j1_research_status"}
    assert executor.BLOCKING_J1_REPORT_TOOLS == {"j1_research_report"}
    assert NAME not in executor.BLOCKING_J1_STATUS_TOOLS
    assert NAME not in executor.BLOCKING_J1_REPORT_TOOLS
    assert NAME not in executor.BLOCKING_READ_TOOLS | executor.BLOCKING_WEB_TOOLS | executor.BLOCKING_CONTROL_TOOLS
