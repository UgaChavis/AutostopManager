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


NAME = "j1_research_vin"
SCHEMA = "autostop.j1.vin_research.v1"
SYNTHETIC_VIN, FOREIGN_VIN = "A" * 17, "B" * 17
FIRST, SECOND, THIRD = "0" * 32, "1" * 32, "2" * 32
KEYS = {FIRST: "synthetic-vin-key-first", SECOND: "synthetic-vin-key-second", THIRD: "synthetic-vin-key-third"}
POOL_NAMES = {NAME: "_J1_VIN_START_EXECUTOR"}
DECLARED_POOL = getattr(executor, POOL_NAMES[NAME], None)
OLD_POOLS = (
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
)
ALL_POOLS = (*OLD_POOLS, *POOL_NAMES.values())
PEERS = {
    "j1_research_" + name: "research_" + name
    for name in ("status", "report", "results", "document", "add_queries", "cancel")
}
PEERS.update(j1_research_start="start_research", j1_research_record_facts="j1_research_record_facts")


@pytest.fixture
def control():
    return NAME


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


def _arguments(name=NAME, marker=FIRST):
    return {"vin": SYNTHETIC_VIN, "idempotency_key": KEYS[marker]}


def _tag(arguments):
    return next(marker for marker, key in KEYS.items() if arguments["idempotency_key"] == key)


def _native(monkeypatch, inert, name, backend, *, all_tools=False, peers=False):
    assert Path(mcp_tools.__file__).resolve() == Path(__file__).resolve().parents[1] / "autostop_manager/mcp_tools.py"
    monkeypatch.setattr(mcp_tools, "j1_research_vin", backend)
    names = {NAME}
    if peers:
        names.update(PEERS)
        for peer, function in PEERS.items():
            monkeypatch.setattr(mcp_tools, function, lambda _peer=peer, **kwargs: {"ok": True, "synthetic_peer": _peer})
    server = FastMCP("synthetic-j1-vin-start-transport")
    mcp_tools.register_manager_tools(
        server, store=inert(), store_client=inert(), include_tools=None if all_tools else names
    )
    return server


async def _pending(pool, count):
    while pool.pending_count != count:
        await asyncio.sleep(0)


def _failure(result, name, reason, *, started, may_continue):
    assert result["ok"] is False and result["schema"] == SCHEMA
    assert result["error"] == {"code": "j1_vin_executor_unavailable", "reason": reason}
    assert result["adapter_started"] is started and result["adapter_may_continue"] is may_continue
    assert result["outcome_uncertain"] is started
    assert result["retryable"] is False and result["retry_attempted"] is False
    assert not {"job_id", "vin", "queries", "query", "input", "idempotency_key", "receipt", "outcome"} & result.keys()
    assert result["tool_execution"]["outcome"] == "error"
    encoded = json.dumps(result)
    assert SYNTHETIC_VIN not in encoded and FOREIGN_VIN not in encoded
    assert all(key not in encoded for key in KEYS.values())


@pytest.mark.parametrize("case", ["queued", "replay", "disabled", "conflict", "busy", "store_error"])
def test_native_vin_start_offloads_and_preserves_exact_response(monkeypatch, inert_registration, case):
    marker = ContextVar("synthetic_vin_start_context", default="missing")
    payload = {
        "ok": True,
        "schema": SCHEMA,
        "job_id": FIRST,
        "profile": "vin",
        "target": "[redacted VIN]",
        "collection_status": "completed" if case == "replay" else "queued",
        "revision": 3 if case == "replay" else 0,
    }
    if case not in {"queued", "replay"}:
        payload = {
            "ok": False,
            "schema": SCHEMA,
            "error": {
                "code": {
                    "disabled": "vin_research_disabled",
                    "conflict": "idempotency_conflict",
                    "busy": "j1_busy",
                    "store_error": "vin_store_unavailable",
                }[case]
            },
        }
    observed = []

    def backend(**kwargs):
        observed.append((threading.get_ident(), marker.get(), deepcopy(kwargs)))
        return deepcopy(payload)

    server = _native(monkeypatch, inert_registration, NAME, backend)
    tool = server._tool_manager.get_tool(NAME)
    signature, parameters, metadata = inspect.signature(tool.fn), tool.parameters, tool.fn_metadata
    wrapper = tool.fn
    instrument_manager_tools(server)

    async def run():
        marker.set("copied")
        loop_thread = threading.get_ident()
        arguments = _arguments()
        original = deepcopy(arguments)
        result = await tool.run(arguments)
        assert arguments == original
        stripped = {key: value for key, value in result.items() if key != "tool_execution"}
        assert json.dumps(stripped, separators=(",", ":")) == json.dumps(payload, separators=(",", ":"))
        assert observed[0][0] != loop_thread
        assert observed[0][1:] == ("copied", original)

    asyncio.run(run())
    assert tool.is_async and tool.parameters is parameters and tool.fn_metadata is metadata
    assert inspect.signature(tool.fn) == signature and tool.fn is wrapper


def test_pool_limits_required_strings_and_validation_before_admission(monkeypatch, inert_registration):
    calls = []
    tool = _native(
        monkeypatch, inert_registration, NAME, lambda **kwargs: calls.append(kwargs) or {"ok": True}
    )._tool_manager.get_tool(NAME)
    pool = getattr(executor, POOL_NAMES[NAME])
    assert DECLARED_POOL._limit == 2 and DECLARED_POOL._pool._max_workers == 1 and DECLARED_POOL._timeout_seconds == 30
    assert tool.parameters["required"] == ["vin", "idempotency_key"]
    assert all(value["type"] == "string" and "default" not in value for value in tool.parameters["properties"].values())
    for arguments in [
        {},
        {"vin": SYNTHETIC_VIN},
        {"idempotency_key": KEYS[FIRST]},
        {**_arguments(), "vin": None},
        {**_arguments(), "idempotency_key": []},
    ]:
        with pytest.raises(ToolError):
            asyncio.run(tool.run(arguments))
    assert calls == [] and not pool._pool._threads and pool.pending_count == 0
    assert asyncio.run(tool.run(_arguments()))["ok"] and calls == [_arguments()]


@pytest.mark.parametrize(
    "changed",
    [
        {},
        {"vin": "  " + "a" * 17 + "  "},
        {"vin": FOREIGN_VIN},
        {"vin": ""},
        {"vin": "A" * 16},
        {"idempotency_key": "short"},
        {"idempotency_key": "k" * 129},
        {"idempotency_key": ""},
    ],
)
def test_native_string_forwarding_keeps_domain_syntax_scope_and_caps(monkeypatch, inert_registration, changed):
    seen = []
    tool = _native(
        monkeypatch,
        inert_registration,
        NAME,
        lambda **kwargs: (
            seen.append(kwargs) or {"ok": False, "schema": SCHEMA, "error": {"code": "synthetic_domain_decision"}}
        ),
    )._tool_manager.get_tool(NAME)
    arguments = {**_arguments(), **changed}
    original = deepcopy(arguments)
    assert asyncio.run(tool.run(arguments))["error"] == {"code": "synthetic_domain_decision"}
    assert arguments == original and seen == [original]
    assert all("maxLength" not in value for value in tool.parameters["properties"].values())


def _peer_arguments(name):
    if name == "j1_research_start":
        return {"objective": "Public synthetic topic", "queries": ["public synthetic source"]}
    if name == "j1_research_record_facts":
        return {"job_id": FIRST, "expected_revision": 0, "facts": [], "idempotency_key": "synthetic-facts-key"}
    return {
        "job_id": FIRST,
        **({"document_id": "d" * 32} if name == "j1_research_document" else {}),
        **({"queries": ["public synthetic source"]} if name == "j1_research_add_queries" else {}),
    }


def test_saturated_vin_start_leaves_all_eight_J1_and_other_capacity(monkeypatch, inert_registration):
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=10)
    monkeypatch.setattr(executor, POOL_NAMES[NAME], pool)
    release, entered = threading.Event(), asyncio.Event()
    calls = []
    loop = None

    def backend(**kwargs):
        calls.append(_tag(kwargs))
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return {"ok": True, "job_id": _tag(kwargs)}

    server = _native(monkeypatch, inert_registration, NAME, backend, peers=True)
    for peer in ("partsapi_catalog_lookup", "fetch_page_excerpt", "manager_automations"):
        server.tool(name=peer)(lambda: {"ok": True})
    instrument_manager_tools(server)
    tool = server._tool_manager.get_tool(NAME)
    assert len({id(getattr(executor, name)) for name in ALL_POOLS}) == len(ALL_POOLS)

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        tasks = [asyncio.create_task(tool.run(_arguments()))]
        try:
            await asyncio.wait_for(entered.wait(), 5)
            tasks.append(asyncio.create_task(tool.run(_arguments(NAME, SECOND))))
            await asyncio.wait_for(_pending(pool, 2), 5)
            assert calls == [FIRST] and not any(task.done() for task in tasks)
            for peer in PEERS:
                result = await server._tool_manager.get_tool(peer).run(_peer_arguments(peer))
                assert result["ok"] and result["synthetic_peer"] == peer
            for peer in ("partsapi_catalog_lookup", "fetch_page_excerpt", "manager_automations"):
                assert (await server._tool_manager.get_tool(peer).run({}))["ok"]
            assert pool.pending_count == 2 and calls == [FIRST]
            _failure(await tool.run(_arguments(NAME, THIRD)), NAME, "busy", started=False, may_continue=False)
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
    result = asyncio.run(
        _native(monkeypatch, inert_registration, control, forbidden)._tool_manager.get_tool(control).run(arguments)
    )
    _failure(result, control, reason, started=started, may_continue=continuing)
    assert result["stage"] == ("worker_wait" if started else "admission") and "A" * 17 not in repr(result)


def test_all_native_metadata_and_old_eight_J1_sets_are_unchanged(monkeypatch, inert_registration):
    monkeypatch.setattr(mcp_tools, "instrument_manager_tools", lambda server: None)
    server = _native(monkeypatch, inert_registration, NAME, lambda **kwargs: {"ok": True}, all_tools=True)
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
    assert snapshot() == before and {name: inspect.signature(tool.fn) for name, tool in tools.items()} == signatures
    wrapper = tools[NAME].fn
    instrument_manager_tools(server)
    assert tools[NAME].fn is wrapper and tools[NAME].is_async
    hints = before[NAME]["annotations"]
    assert not hints.readOnlyHint and hints.destructiveHint and not hints.idempotentHint and hints.openWorldHint
    assert not getattr(executor, POOL_NAMES[NAME])._pool._threads
    assert executor.BLOCKING_J1_VIN_START_TOOLS == {NAME}
    existing = executor.BLOCKING_READ_TOOLS | executor.BLOCKING_WEB_TOOLS | executor.BLOCKING_CONTROL_TOOLS
    for suffix in ("STATUS", "REPORT", "RESULTS", "DOCUMENT", "ADD_QUERIES", "CANCEL", "START", "RECORD_FACTS"):
        group = getattr(executor, "BLOCKING_J1_" + suffix + "_TOOLS")
        assert group == {"j1_research_" + suffix.lower()}
        existing |= group
    assert NAME not in existing
