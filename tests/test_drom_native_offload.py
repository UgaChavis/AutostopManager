from __future__ import annotations

import asyncio
from contextvars import ContextVar
from copy import deepcopy
import importlib
import inspect
import json
import logging
from pathlib import Path
import threading
from types import SimpleNamespace

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from autostop_manager import mcp_read_executor as shared, mcp_tools
from autostop_manager.listing_executor import BoundedListingExecutor, ListingExecutionError
from autostop_manager.mcp_contract import mcp_schema_fingerprint
from autostop_manager import mcp_telemetry
from autostop_manager.mcp_telemetry import instrument_manager_tools

START, GET = "drom_start_parts_search", "drom_get_parts_search"
FIRST, SECOND, THIRD = 101, 102, 103
QUERIES = {
    FIRST: "synthetic public parts first",
    SECOND: "synthetic public parts second",
    THIRD: "synthetic public parts third",
}
UIDS = {key: "synthetic-private-run-" + str(key) for key in QUERIES}
POOL_NAMES = {START: "_START_EXECUTOR", GET: "_GET_EXECUTOR"}
BUDGETS = {START: 90, GET: 50}
module_path = Path(__file__).resolve().parents[1] / "autostop_manager/mcp_drom_executor.py"
executor = importlib.import_module("autostop_manager.mcp_drom_executor") if module_path.exists() else SimpleNamespace()
DECLARED_POOLS = {name: getattr(executor, attr, None) for name, attr in POOL_NAMES.items()}
SHARED_POOLS = (
    "_EXECUTOR",
    "_WEB_EXECUTOR",
    "_CONTROL_EXECUTOR",
    "_J1_STATUS_EXECUTOR",
    "_J1_REPORT_EXECUTOR",
    "_J1_RESULTS_EXECUTOR",
    "_J1_DOCUMENT_EXECUTOR",
    "_J1_ADD_QUERIES_EXECUTOR",
    "_J1_CANCEL_EXECUTOR",
    "_J1_START_EXECUTOR",
    "_J1_RECORD_FACTS_EXECUTOR",
    "_J1_VIN_START_EXECUTOR",
)


@pytest.fixture(params=[START, GET])
def control(request):
    return request.param


@pytest.fixture(autouse=True)
def inert_registration(monkeypatch):
    events = []

    class MemoryLog(logging.Handler):
        def emit(self, record):
            events.append(json.loads(record.getMessage()))

    monkeypatch.setattr(mcp_telemetry.LOGGER, "handlers", [MemoryLog()])

    class Inert:
        def __init__(self, *args, **kwargs):
            pass

    Inert.events = events

    def forbidden(*args, **kwargs):
        pytest.fail("Inert native Drom registration must not construct Store or load configuration")

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
    fresh = []
    touched = [(shared, name) for name in SHARED_POOLS] + [(executor, name) for name in POOL_NAMES.values()]
    for module, attr in touched:
        pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=30)
        fresh.append(pool)
        monkeypatch.setattr(module, attr, pool, raising=False)
    yield Inert
    for module, attr in touched:
        pool = getattr(module, attr)
        if isinstance(pool, BoundedListingExecutor) and pool not in fresh:
            pool.close()
    for pool in fresh:
        pool.close()


def _arguments(name, marker=FIRST):
    if name == START:
        return {"query": QUERIES[marker], "region": "krasnoyarsk", "limit": 50, "page_limit": 3, "dry_run": False}
    return {"task_id": marker, "uid": UIDS[marker]}


def _tag(arguments):
    return arguments.get("task_id") or next(marker for marker, value in QUERIES.items() if value == arguments["query"])


def _native(monkeypatch, inert, name, backend, *, all_tools=False):
    assert Path(mcp_tools.__file__).resolve() == Path(__file__).resolve().parents[1] / "autostop_manager/mcp_tools.py"
    monkeypatch.setattr(mcp_tools, START, lambda **kwargs: {"ok": True})
    monkeypatch.setattr(mcp_tools, GET, lambda **kwargs: {"ok": True})
    monkeypatch.setattr(mcp_tools, name, backend)
    server = FastMCP("synthetic-drom-native-transport")
    mcp_tools.register_manager_tools(
        server, store=inert(), store_client=inert(), include_tools=None if all_tools else {START, GET}
    )
    return server


async def _pending(pool, count):
    while pool.pending_count != count:
        await asyncio.sleep(0)


def _failure(result, name, reason, *, started, may_continue):
    suffix = {"execution_failed": "failed", "executor_unavailable": "unavailable", "executor_draining": "draining"}.get(
        reason, reason
    )
    prefix = "drom_start_executor_" if name == START else "drom_get_executor_"
    expected = {
        "ok": False,
        "source": "drom",
        "status": "uncertain" if name == START and started else "failed",
        "error": prefix + suffix,
        "task_id": None,
        "uid": None,
        "outcome_uncertain": started,
    }
    assert {key: value for key, value in result.items() if key != "tool_execution"} == expected
    assert set(result) == {*expected, "tool_execution"}
    assert result["tool_execution"]["outcome"] == "error"
    assert result["tool_execution"]["network_calls"] is None
    encoded = json.dumps(result)
    assert all(value not in encoded for value in [*QUERIES.values(), *UIDS.values(), "A" * 17])


def _payload(name, case):
    if case != "normal":
        return {
            "ok": False,
            "source": "drom",
            "status": "disabled"
            if case == "disabled"
            else "uncertain"
            if case == "uncertain" and name == START
            else "failed",
            "error": {
                "disabled": "webbee_disabled",
                "domain_error": "webbee_auth_failed",
                "uncertain": "webbee_start_outcome_uncertain" if name == START else "webbee_result_unavailable",
            }[case],
            "task_id": FIRST if name == GET or case == "uncertain" else None,
            "uid": UIDS[FIRST] if name == GET or case == "uncertain" else None,
            "outcome_uncertain": case == "uncertain",
            **(
                {"provider_status": "complete", "progress": {"total": 1}} if case == "uncertain" and name == GET else {}
            ),
        }
    if name == START:
        return {
            "ok": True,
            "source": "drom",
            "status": "queued",
            "task_id": FIRST,
            "uid": UIDS[FIRST],
            "search_url": "https://baza.drom.ru/krasnoyarsk/sell_spare_parts/?query=DEMO",
            "limit": 50,
            "page_limit": 3,
            "outcome_uncertain": False,
            "idempotent": False,
        }
    return {
        "ok": True,
        "source": "drom",
        "status": "complete",
        "task_id": FIRST,
        "uid": UIDS[FIRST],
        "listings": [],
        "count": 0,
        "observed_at": "2026-10-25T00:00:00Z",
        "progress": {"total": 0},
        "fitment_confirmed": False,
        "availability_confirmed": False,
    }


@pytest.mark.parametrize("case", ["normal", "disabled", "domain_error", "uncertain"])
def test_native_offloads_whole_callback_preserves_payload_context_and_single_trace(
    monkeypatch, inert_registration, control, case
):
    marker = ContextVar("synthetic_drom_context", default="missing")
    payload = _payload(control, case)
    observed = []

    def backend(**kwargs):
        # A cold loader/guard belongs to the callback, including disabled paths.
        observed.append((threading.get_ident(), marker.get(), deepcopy(kwargs)))
        return deepcopy(payload)

    server = _native(monkeypatch, inert_registration, control, backend)
    tool = server._tool_manager.get_tool(control)
    original_fn, signature, parameters, metadata = (
        tool.fn,
        inspect.signature(tool.fn),
        tool.parameters,
        tool.fn_metadata,
    )
    instrument_manager_tools(server)
    assert tool.fn is original_fn

    async def run():
        marker.set("copied")
        loop_thread = threading.get_ident()
        arguments = _arguments(control)
        original = deepcopy(arguments)
        result = await tool.run(arguments)
        assert arguments == original
        stripped = {key: value for key, value in result.items() if key != "tool_execution"}
        assert json.dumps(stripped, separators=(",", ":")) == json.dumps(payload, separators=(",", ":"))
        assert observed[0][0] != loop_thread and observed[0][1:] == ("copied", original)
        assert getattr(executor, POOL_NAMES[control]).pending_count == 0

    asyncio.run(run())
    assert (
        tool.is_async
        and inspect.signature(tool.fn) == signature
        and tool.parameters is parameters
        and tool.fn_metadata is metadata
    )
    events = inert_registration.events
    assert [e["event"] for e in events] == ["mcp_tool_start", "mcp_tool_end"]
    assert events[0]["call_id"] == events[1]["call_id"] and all(e["tool"] == control for e in events)
    assert all(value not in json.dumps(events) for value in [*QUERIES.values(), *UIDS.values()])


@pytest.mark.parametrize("dry_run", [False, True])
def test_start_dryrun_never_bypasses_whole_callback_offload(monkeypatch, inert_registration, dry_run):
    seen = []

    def backend(**kwargs):
        seen.append(threading.get_ident())
        return {
            "ok": False,
            "source": "drom",
            "status": "disabled",
            "error": "webbee_disabled",
            "task_id": None,
            "uid": None,
            "outcome_uncertain": False,
        }

    tool = _native(monkeypatch, inert_registration, START, backend)._tool_manager.get_tool(START)
    caller_thread = threading.get_ident()
    result = asyncio.run(tool.run({**_arguments(START), "dry_run": dry_run}))
    assert result["status"] == "disabled" and seen and seen[0] != caller_thread


def test_declared_soft_budgets_and_required_validation_before_admission(monkeypatch, inert_registration, control):
    seen = []
    tool = _native(
        monkeypatch, inert_registration, control, lambda **kwargs: seen.append(kwargs) or {"ok": True}
    )._tool_manager.get_tool(control)
    declared = DECLARED_POOLS[control]
    assert declared._limit == 2 and declared._pool._max_workers == 1 and declared._timeout_seconds == BUDGETS[control]
    required = ["query"] if control == START else ["task_id", "uid"]
    assert tool.parameters["required"] == required
    bad = [{}, {**_arguments(control), "query" if control == START else "uid": None}]
    if control == GET:
        bad.append({"task_id": FIRST})
    pool = getattr(executor, POOL_NAMES[control])
    for arguments in bad:
        with pytest.raises(ToolError):
            asyncio.run(tool.run(arguments))
    assert seen == [] and pool.pending_count == 0 and not pool._pool._threads and inert_registration.events == []


@pytest.mark.parametrize(
    "name,changed,expected",
    [
        (START, {}, {}),
        (START, {"limit": "2"}, {"limit": 2}),
        (START, {"page_limit": 2.0}, {"page_limit": 2}),
        (START, {"dry_run": "false"}, {"dry_run": False}),
        (START, {"dry_run": 1}, {"dry_run": True}),
        (START, {"limit": 1001}, {"limit": 1001}),
        (START, {"page_limit": 11}, {"page_limit": 11}),
        (START, {"query": "x" * 129}, {"query": "x" * 129}),
        (GET, {"task_id": "12"}, {"task_id": 12}),
        (GET, {"task_id": 12.0}, {"task_id": 12}),
        (GET, {"task_id": True}, {"task_id": 1}),
    ],
)
def test_original_native_defaults_coercions_and_domain_bounds_are_forwarded(
    monkeypatch, inert_registration, name, changed, expected
):
    seen = []
    tool = _native(
        monkeypatch,
        inert_registration,
        name,
        lambda **kwargs: seen.append(kwargs) or {"ok": False, "error": "synthetic_domain_decision"},
    )._tool_manager.get_tool(name)
    arguments = {"query": QUERIES[FIRST]} if name == START else _arguments(GET)
    arguments.update(changed)
    original = deepcopy(arguments)
    result = asyncio.run(tool.run(arguments))
    assert arguments == original and seen == [{**_arguments(name), **expected}]
    assert result["error"] == "synthetic_domain_decision"
    assert all("maximum" not in row and "maxLength" not in row for row in tool.parameters["properties"].values())


def test_saturated_drom_keeps_opposite_and_old_callback_capacity(monkeypatch, inert_registration, control):
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=10)
    monkeypatch.setattr(executor, POOL_NAMES[control], pool)
    release, entered = threading.Event(), asyncio.Event()
    calls = []
    loop = None

    def backend(**kwargs):
        calls.append(_tag(kwargs))
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return {"ok": True, "job_id": _tag(kwargs)}

    server = _native(monkeypatch, inert_registration, control, backend)
    for peer in ("partsapi_catalog_lookup", "fetch_page_excerpt", "manager_automations", "j1_research_vin"):
        server.tool(name=peer)(lambda: {"ok": True})
    instrument_manager_tools(server)
    tool = server._tool_manager.get_tool(control)
    opposite = GET if control == START else START
    assert pool is not getattr(executor, POOL_NAMES[opposite])
    assert all(pool is not getattr(shared, name) for name in SHARED_POOLS)

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        tasks = [asyncio.create_task(tool.run(_arguments(control)))]
        try:
            await asyncio.wait_for(entered.wait(), 5)
            tasks.append(asyncio.create_task(tool.run(_arguments(control, SECOND))))
            await asyncio.wait_for(_pending(pool, 2), 5)
            assert calls == [FIRST] and not any(task.done() for task in tasks)
            assert (await server._tool_manager.get_tool(opposite).run(_arguments(opposite)))["ok"]
            for peer in ("partsapi_catalog_lookup", "fetch_page_excerpt", "manager_automations", "j1_research_vin"):
                assert (await server._tool_manager.get_tool(peer).run({}))["ok"]
            assert pool.pending_count == 2 and calls == [FIRST]
            _failure(await tool.run(_arguments(control, THIRD)), control, "busy", started=False, may_continue=False)
            release.set()
            result = await asyncio.gather(*tasks)
            assert [r["job_id"] for r in result] == [FIRST, SECOND] and calls == [FIRST, SECOND]
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("cancel_queued", [False, True])
def test_caller_cancellation_retains_real_slot_and_abandoned_queue_never_starts(
    monkeypatch, inert_registration, control, cancel_queued
):
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=10)
    monkeypatch.setattr(executor, POOL_NAMES[control], pool)
    release = threading.Event()
    entered = asyncio.Event()
    calls = []
    loop = None

    def backend(**kwargs):
        calls.append(_tag(kwargs))
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return {"ok": True}

    tool = _native(monkeypatch, inert_registration, control, backend)._tool_manager.get_tool(control)

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        assert tool.is_async
        running = asyncio.create_task(tool.run(_arguments(control)))
        tasks = [running]
        try:
            await asyncio.wait_for(entered.wait(), 5)
            queued = asyncio.create_task(tool.run(_arguments(control, SECOND)))
            tasks.append(queued)
            await asyncio.wait_for(_pending(pool, 2), 5)
            cancelled = queued if cancel_queued else running
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            assert pool.pending_count == 2 and calls == [FIRST]
            _failure(await tool.run(_arguments(control, THIRD)), control, "busy", started=False, may_continue=False)
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.wait_for(_pending(pool, 0), 5)
            assert calls == ([FIRST] if cancel_queued else [FIRST, SECOND])
            assert (await tool.run(_arguments(control, THIRD)))["ok"]
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


def test_entered_worker_timeout_keeps_uncertainty_and_admission(monkeypatch, inert_registration, control):
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=2)
    monkeypatch.setattr(executor, POOL_NAMES[control], pool)
    release, finished = threading.Event(), threading.Event()
    entered = asyncio.Event()
    loop = None

    def backend(**kwargs):
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(10)
            return {"ok": True}
        finally:
            finished.set()

    tool = _native(monkeypatch, inert_registration, control, backend)._tool_manager.get_tool(control)

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(tool.run(_arguments(control)))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            result = await task
            _failure(result, control, "wait_timeout", started=True, may_continue=True)
            assert not finished.is_set() and pool.pending_count == 1
            release.set()
            await asyncio.wait_for(_pending(pool, 0), 5)
            assert finished.is_set()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_unstarted_queued_timeout_never_invokes_domain(monkeypatch, inert_registration, control):
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=2)
    monkeypatch.setattr(executor, POOL_NAMES[control], pool)
    release = threading.Event()
    entered = asyncio.Event()
    loop = None
    calls = []

    def backend(**kwargs):
        calls.append(_tag(kwargs))
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return {"ok": True}

    tool = _native(monkeypatch, inert_registration, control, backend)._tool_manager.get_tool(control)

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        running = asyncio.create_task(tool.run(_arguments(control)))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            result = await tool.run(_arguments(control, SECOND))
            _failure(result, control, "wait_timeout", started=False, may_continue=False)
            assert pool.pending_count == 2 and calls == [FIRST]
            release.set()
            await asyncio.wait_for(_pending(pool, 0), 5)
            assert calls == [FIRST]
        finally:
            release.set()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(run())


def test_finished_worker_exception_still_has_uncertain_private_error(monkeypatch, inert_registration, control, capsys):
    sensitive = "A" * 17
    calls = []

    def backend(**kwargs):
        calls.append(True)
        raise RuntimeError(sensitive)

    result = asyncio.run(
        _native(monkeypatch, inert_registration, control, backend)
        ._tool_manager.get_tool(control)
        .run(_arguments(control))
    )
    _failure(result, control, "execution_failed", started=True, may_continue=False)
    assert calls == [True] and sensitive not in str(result) and sensitive not in repr(result)
    assert sensitive not in capsys.readouterr().err
    assert sensitive not in json.dumps(inert_registration.events)


@pytest.mark.parametrize(
    "code,reason,started,continuing",
    [
        ("provider_busy", "busy", False, False),
        ("provider_wait_timeout", "wait_timeout", True, True),
        ("provider_execution_failed", "execution_failed", True, False),
        ("provider_executor_unavailable", "executor_unavailable", False, False),
        ("provider_executor_draining", "executor_draining", False, False),
        ("A" * 17, "executor_unavailable", True, False),
    ],
)
def test_finite_failure_codes_without_routing_or_input_echo(
    monkeypatch, inert_registration, control, code, reason, started, continuing
):
    class FailedPool:
        async def run(self, operation):
            raise ListingExecutionError(
                code,
                stage="worker_wait" if started else "admission",
                adapter_started=started,
                adapter_may_continue=continuing,
            )

    monkeypatch.setattr(executor, POOL_NAMES[control], FailedPool())

    def forbidden(**kwargs):
        pytest.fail("Executor refusal must not route a family or invoke a domain mutation")

    arguments = _arguments(control)
    result = asyncio.run(
        _native(monkeypatch, inert_registration, control, forbidden)._tool_manager.get_tool(control).run(arguments)
    )
    _failure(result, control, reason, started=started, may_continue=continuing)
    assert "A" * 17 not in repr(result)


def test_all70_metadata_and_only_Drom_native_instrumentation(monkeypatch, inert_registration):
    monkeypatch.setattr(mcp_tools, "instrument_manager_tools", lambda server: None)
    server = _native(monkeypatch, inert_registration, START, lambda **kwargs: {"ok": True}, all_tools=True)
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
    instrument_manager_tools(server)
    assert snapshot() == before and {name: inspect.signature(tool.fn) for name, tool in tools.items()} == signatures
    for name in (START, GET):
        assert tools[name].is_async and not getattr(executor, POOL_NAMES[name])._pool._threads
        hints = before[name]["annotations"]
        assert hints.readOnlyHint is (name == GET) and not hints.destructiveHint and hints.openWorldHint
        assert hints.idempotentHint is (False if name == START else None)
    assert executor.DROM_BLOCKING_TOOLS == {START, GET}
    current_sets = [value for name, value in vars(shared).items() if name.startswith("BLOCKING_")]
    assert not {START, GET} & set().union(*current_sets)
