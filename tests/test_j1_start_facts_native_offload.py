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


START = "j1_research_start"
FACTS = "j1_research_record_facts"
GENERAL = "autostop.j1.research.v1"
VIN = "autostop.j1.vin_research.v1"
FIRST, SECOND, THIRD = "0" * 32, "1" * 32, "2" * 32
QUERY = "synthetic public followup"
POOL_NAMES = {
    START: "_J1_START_EXECUTOR",
    FACTS: "_J1_RECORD_FACTS_EXECUTOR",
}
DECLARED_POOLS = {name: getattr(executor, attr, None) for name, attr in POOL_NAMES.items()}
ALL_POOLS = (
    "_EXECUTOR",
    "_WEB_EXECUTOR",
    "_CONTROL_EXECUTOR",
    "_J1_STATUS_EXECUTOR",
    "_J1_REPORT_EXECUTOR",
    "_J1_RESULTS_EXECUTOR",
    "_J1_DOCUMENT_EXECUTOR",
    "_J1_ADD_QUERIES_EXECUTOR",
    "_J1_CANCEL_EXECUTOR",
    *POOL_NAMES.values(),
)


@pytest.fixture(params=[START, FACTS])
def control(request):
    return request.param


@pytest.fixture(autouse=True)
def inert_registration(monkeypatch):
    class Inert:
        def __init__(self, *args, **kwargs):
            pass

    def forbidden(*args, **kwargs):
        pytest.fail("Synthetic control transport must not construct Store or load configuration")

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
    for name in ALL_POOLS:
        pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=30)
        fresh.append(pool)
        monkeypatch.setattr(executor, name, pool, raising=False)
    yield Inert
    for name in ALL_POOLS:
        pool = getattr(executor, name)
        if isinstance(pool, BoundedListingExecutor) and pool not in fresh:
            pool.close()
    for pool in fresh:
        pool.close()


OBJECTIVES = {
    FIRST: "Public technical first topic",
    SECOND: "Public technical second topic",
    THIRD: "Public technical third topic",
}


def _arguments(name, marker=FIRST):
    if name == START:
        return {
            "objective": OBJECTIVES[marker],
            "queries": [QUERY],
            "max_pages": 300,
            "automotive_context": None,
            "profile": "general",
        }
    return {
        "job_id": marker,
        "expected_revision": 0,
        "facts": [],
        "idempotency_key": "synthetic-facts-key",
        "finalize": False,
    }


def _tag(arguments):
    return arguments.get("job_id") or next(
        marker for marker, topic in OBJECTIVES.items() if arguments["objective"] == topic
    )


def _schema(name):
    return GENERAL if name == START else VIN


def _payload(name, case):
    if case.endswith("error"):
        code = (
            ("j1_busy" if case == "domain_error" else "j1_store_unavailable")
            if name == START
            else ("revision_conflict" if case == "domain_error" else "vin_research_disabled")
        )
        return {"ok": False, "schema": _schema(name), "error": {"code": code}}
    if name == START:
        return {
            "ok": True,
            "schema": GENERAL,
            "job_id": FIRST,
            "status": "queued",
            "profile": "automotive" if case == "alternate_ok" else "general",
            "max_pages": 60 if case == "alternate_ok" else 300,
            "query_count": 12 if case == "alternate_ok" else 1,
            "automotive_context": {"make": "Synthetic"} if case == "alternate_ok" else None,
        }
    return {
        "ok": True,
        "schema": VIN,
        "job_id": FIRST,
        "claim_ids": [],
        "revision": 4,
        "analysis_status": "ready_partial" if case == "alternate_ok" else "draft",
    }


def _native(monkeypatch, inert, name, backend, *, all_tools=False, peers=False):
    assert Path(mcp_tools.__file__).resolve() == Path(__file__).resolve().parents[1] / "autostop_manager/mcp_tools.py"
    monkeypatch.setattr(mcp_tools, "start_research", lambda **kwargs: {"ok": True, "schema": GENERAL})
    monkeypatch.setattr(mcp_tools, "j1_research_record_facts", lambda **kwargs: {"ok": True, "schema": VIN})
    monkeypatch.setattr(mcp_tools, "start_research" if name == START else "j1_research_record_facts", backend)
    names = {START, FACTS}
    if peers:
        for peer in ("status", "report", "results", "document", "add_queries", "cancel"):
            names.add("j1_research_" + peer)
            monkeypatch.setattr(
                mcp_tools,
                "research_" + peer,
                lambda _peer=peer, **kwargs: {"ok": True, "synthetic_peer": _peer},
            )
    server = FastMCP("synthetic-j1-start-facts-transport")
    mcp_tools.register_manager_tools(
        server, store=inert(), store_client=inert(), include_tools=None if all_tools else names
    )
    return server


async def _pending(pool, count):
    while pool.pending_count != count:
        await asyncio.sleep(0)


def _failure(result, name, reason, *, started, may_continue):
    code = "j1_start_executor_unavailable" if name == START else "j1_record_facts_executor_unavailable"
    assert result["ok"] is False and result["schema"] == _schema(name)
    assert result["error"] == {"code": code, "reason": reason}
    assert result["adapter_started"] is started and result["adapter_may_continue"] is may_continue
    assert result["outcome_uncertain"] is started
    assert result["retryable"] is False and result["retry_attempted"] is False
    assert (
        not {
            "job_id",
            "queries",
            "query",
            "objective",
            "facts",
            "quote",
            "idempotency_key",
            "expected_revision",
            "input",
            "outcome",
        }
        & result.keys()
    )
    assert result["tool_execution"]["outcome"] == "error"
    encoded = json.dumps(result)
    assert (
        FIRST not in encoded
        and QUERY not in encoded
        and "synthetic-facts-key" not in encoded
        and "private synthetic quote" not in encoded
        and "A" * 17 not in encoded
    )


@pytest.mark.parametrize("case", ["ok", "alternate_ok", "domain_error", "policy_error"])
def test_native_control_offloads_and_preserves_domain_response(monkeypatch, inert_registration, control, case):
    marker = ContextVar("synthetic_control_context", default="missing")
    payload = _payload(control, case)
    observed = []

    def backend(**kwargs):
        observed.append((threading.get_ident(), marker.get(), deepcopy(kwargs)))
        return deepcopy(payload)

    server = _native(monkeypatch, inert_registration, control, backend)
    tool = server._tool_manager.get_tool(control)
    signature, parameters, metadata = inspect.signature(tool.fn), tool.parameters, tool.fn_metadata
    wrapper = tool.fn
    instrument_manager_tools(server)
    assert tool.fn is wrapper

    async def run():
        marker.set("copied")
        loop_thread = threading.get_ident()
        arguments = _arguments(control)
        original = deepcopy(arguments)
        result = await tool.run(arguments)
        assert arguments == original
        stripped = {key: value for key, value in result.items() if key != "tool_execution"}
        assert json.dumps(stripped, separators=(",", ":")) == json.dumps(payload, separators=(",", ":"))
        assert observed[0][0] != loop_thread
        assert observed[0][1:] == ("copied", original)

    asyncio.run(run())
    assert tool.is_async and tool.parameters is parameters and tool.fn_metadata is metadata
    assert inspect.signature(tool.fn) == signature


def test_fixed_independent_pools_and_validation_before_admission(monkeypatch, inert_registration, control):
    calls = []

    def backend(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    declared = DECLARED_POOLS[control]
    assert declared._limit == 2 and declared._pool._max_workers == 1 and declared._timeout_seconds == 30
    pool = getattr(executor, POOL_NAMES[control])
    assert not pool._pool._threads
    tool = _native(monkeypatch, inert_registration, control, backend)._tool_manager.get_tool(control)
    required = ["objective"] if control == START else ["job_id", "expected_revision", "facts", "idempotency_key"]
    assert tool.parameters["required"] == required
    assert (
        tool.parameters["properties"]["queries"]["default"] is None
        if control == START
        else tool.parameters["properties"]["finalize"]["default"] is False
    )
    invalid = [{}, {**_arguments(control), "objective" if control == START else "job_id": None}]
    for arguments in invalid:
        with pytest.raises(ToolError):
            asyncio.run(tool.run(arguments))
    assert not pool._pool._threads and pool.pending_count == 0 and calls == []
    assert asyncio.run(tool.run(_arguments(control)))["ok"]
    assert len(pool._pool._threads) == 1 and calls == [_arguments(control)]


@pytest.mark.parametrize(
    "changed,expected",
    [
        ({}, {}),
        ({"max_pages": "3"}, {"max_pages": 3}),
        ({"max_pages": 3.0}, {"max_pages": 3}),
        ({"queries": ("first", "second")}, {"queries": ["first", "second"]}),
        ({"queries": '["synthetic public followup"]'}, {"queries": [QUERY]}),
        ({"queries": []}, {"queries": []}),
        ({"queries": [QUERY] * 31}, {"queries": [QUERY] * 31}),
        ({"queries": ["x" * 501]}, {"queries": ["x" * 501]}),
        ({"automotive_context": '{"make":"Synthetic"}'}, {"automotive_context": {"make": "Synthetic"}}),
    ],
)
def test_start_defaults_coercions_and_domain_boundaries(monkeypatch, inert_registration, changed, expected):
    seen = []
    tool = _native(
        monkeypatch,
        inert_registration,
        START,
        lambda **kwargs: seen.append(kwargs) or {"ok": False, "error": {"code": "synthetic_domain_decision"}},
    )._tool_manager.get_tool(START)
    arguments = {"objective": OBJECTIVES[FIRST], **changed}
    original = deepcopy(arguments)
    result = asyncio.run(tool.run(arguments))
    defaults = {**_arguments(START), "queries": None}
    assert seen == [{**defaults, **expected}] and arguments == original
    assert result["error"] == {"code": "synthetic_domain_decision"}
    assert "maxItems" not in tool.parameters["properties"]["queries"]


@pytest.mark.parametrize("facts", [[], [{}] * 101, [{"quote": "x" * 250001}]])
def test_facts_native_shape_preserves_domain_caps_and_default_finalize(monkeypatch, inert_registration, facts):
    seen = []
    tool = _native(
        monkeypatch,
        inert_registration,
        FACTS,
        lambda **kwargs: seen.append(kwargs) or {"ok": False, "error": {"code": "synthetic_domain_decision"}},
    )._tool_manager.get_tool(FACTS)
    arguments = {**_arguments(FACTS), "facts": facts}
    del arguments["finalize"]
    original = deepcopy(arguments)
    result = asyncio.run(tool.run(arguments))
    assert arguments == original and seen == [{**arguments, "finalize": False}]
    assert result["error"] == {"code": "synthetic_domain_decision"}
    assert "maxItems" not in tool.parameters["properties"]["facts"]


@pytest.mark.parametrize(
    "changed",
    [
        {"expected_revision": True},
        {"expected_revision": "2"},
        {"expected_revision": 1.0},
        {"expected_revision": -1},
        {"finalize": "false"},
        {"finalize": 1},
        {"finalize": None},
        {"facts": "[]"},
        {"facts": None},
    ],
)
def test_facts_strict_types_reject_before_admission(monkeypatch, inert_registration, changed):
    seen = []
    tool = _native(monkeypatch, inert_registration, FACTS, lambda **kwargs: seen.append(kwargs))._tool_manager.get_tool(
        FACTS
    )
    pool = getattr(executor, POOL_NAMES[FACTS])
    with pytest.raises(ToolError):
        asyncio.run(tool.run({**_arguments(FACTS), **changed}))
    assert seen == [] and not pool._pool._threads and pool.pending_count == 0


def test_saturated_control_preserves_opposite_and_reader_capacity(monkeypatch, inert_registration, control):
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
        return {"ok": True, "job_id": _tag(kwargs)}

    server = _native(monkeypatch, inert_registration, control, backend, peers=True)
    for peer in ("partsapi_catalog_lookup", "fetch_page_excerpt", "manager_automations"):
        server.tool(name=peer)(lambda: {"ok": True})
    instrument_manager_tools(server)
    tool = server._tool_manager.get_tool(control)
    opposite = FACTS if control == START else START
    assert pool is not getattr(executor, POOL_NAMES[opposite])
    assert len({id(getattr(executor, name)) for name in ALL_POOLS}) == len(ALL_POOLS)

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        assert tool.is_async
        tasks = [asyncio.create_task(tool.run(_arguments(control)))]
        try:
            await asyncio.wait_for(entered.wait(), 5)
            tasks.append(asyncio.create_task(tool.run(_arguments(control, SECOND))))
            await asyncio.wait_for(_pending(pool, 2), 5)
            assert calls == [FIRST] and not any(task.done() for task in tasks)
            assert (await server._tool_manager.get_tool(opposite).run(_arguments(opposite)))["ok"]
            for peer in ("status", "report", "results", "document", "add_queries", "cancel"):
                arguments = {
                    "job_id": FIRST,
                    **({"document_id": "d" * 32} if peer == "document" else {}),
                    **({"queries": [QUERY]} if peer == "add_queries" else {}),
                }
                result = await server._tool_manager.get_tool("j1_research_" + peer).run(arguments)
                assert result["ok"] and result["synthetic_peer"] == peer
            for peer in ("partsapi_catalog_lookup", "fetch_page_excerpt", "manager_automations"):
                assert (await server._tool_manager.get_tool(peer).run({}))["ok"]
            assert pool.pending_count == 2 and calls == [FIRST]
            _failure(await tool.run(_arguments(control, THIRD)), control, "busy", started=False, may_continue=False)
            release.set()
            result = await asyncio.gather(*tasks)
            assert [item["job_id"] for item in result] == [FIRST, SECOND] and calls == [FIRST, SECOND]
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


def test_entered_worker_timeout_keeps_mutation_uncertain_and_admission(monkeypatch, inert_registration, control):
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
            assert result["stage"] == "admission" and pool.pending_count == 2 and calls == [FIRST]
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
    if control == FACTS:
        arguments["facts"] = [{"quote": "private synthetic quote", "value": "A" * 17}]
    result = asyncio.run(
        _native(monkeypatch, inert_registration, control, forbidden)._tool_manager.get_tool(control).run(arguments)
    )
    _failure(result, control, reason, started=started, may_continue=continuing)
    assert result["stage"] == ("worker_wait" if started else "admission") and "A" * 17 not in repr(result)


def test_all_native_schemas_annotations_signatures_and_old_six_J1_sets_unchanged(monkeypatch, inert_registration):
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
    assert len(before) == manifest["expected_tool_count"] and set(before) == set(manifest["expected_tool_names"])
    assert (
        mcp_schema_fingerprint({name: row["inputSchema"] for name, row in before.items()})
        == manifest["schema_fingerprint"]
    )
    assert not any(getattr(executor, POOL_NAMES[name])._pool._threads for name in (START, FACTS))
    instrument_manager_tools(server)
    assert snapshot() == before and {name: inspect.signature(tool.fn) for name, tool in tools.items()} == signatures
    for name in (START, FACTS):
        annotations = before[name]["annotations"]
        assert (
            not annotations.readOnlyHint
            and annotations.destructiveHint is (name == FACTS)
            and not annotations.idempotentHint
        )
        assert annotations.openWorldHint is (name == START)
        assert tools[name].is_async and not getattr(executor, POOL_NAMES[name])._pool._threads
    assert executor.BLOCKING_J1_START_TOOLS == {START}
    assert executor.BLOCKING_J1_RECORD_FACTS_TOOLS == {FACTS}
    for suffix in ("STATUS", "REPORT", "RESULTS", "DOCUMENT", "ADD_QUERIES", "CANCEL"):
        assert getattr(executor, "BLOCKING_J1_" + suffix + "_TOOLS") == {"j1_research_" + suffix.lower()}
    existing = (
        executor.BLOCKING_READ_TOOLS
        | executor.BLOCKING_WEB_TOOLS
        | executor.BLOCKING_CONTROL_TOOLS
        | executor.BLOCKING_J1_STATUS_TOOLS
        | executor.BLOCKING_J1_REPORT_TOOLS
        | executor.BLOCKING_J1_RESULTS_TOOLS
        | executor.BLOCKING_J1_DOCUMENT_TOOLS
        | executor.BLOCKING_J1_ADD_QUERIES_TOOLS
        | executor.BLOCKING_J1_CANCEL_TOOLS
    )
    assert not {START, FACTS} & existing
