"""Exercise the registered role/CRM callbacks with synthetic bounded backends."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from copy import deepcopy
import inspect
import socket
import threading

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from autostop_manager import config, mcp_read_executor, mcp_store_executor, mcp_tools
from autostop_manager.crm_parts_store import parts_store_cards
from autostop_manager.listing_executor import BoundedListingExecutor
from autostop_manager.mcp_contract import mcp_schema_fingerprint
from autostop_manager.mcp_telemetry import instrument_manager_tools
from autostop_manager.storage import StoreState


TOOLS = {"manager_roles", "manager_role_action", "parts_store_cards"}


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setattr(config, "_ENV_LOADED", False)
    for name in (
        "AUTOSTOP_STORE_API_URL",
        "AUTOSTOP_STORE_READ_TOKEN",
        "AUTOSTOP_STORE_MANAGE_TOKEN",
        "AUTOSTOP_STORE_OWNER_TOKEN",
        "AUTOSTOP_STORE_QUOTE_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    def denied(*_args, **_kwargs):
        raise AssertionError("MCP point-fix regression attempted real network")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture
def pools(monkeypatch):
    created = []

    def install(*, waiting=1, timeout=2):
        reads = BoundedListingExecutor(max_workers=2, max_waiting_calls=waiting, timeout_seconds=timeout)
        stateful = BoundedListingExecutor(max_workers=1, max_waiting_calls=waiting, timeout_seconds=timeout)
        control = BoundedListingExecutor(max_workers=1, max_waiting_calls=waiting, timeout_seconds=timeout)
        created.extend((reads, stateful, control))
        monkeypatch.setattr(mcp_store_executor, "_READ_EXECUTOR", reads)
        monkeypatch.setattr(mcp_store_executor, "_STATEFUL_EXECUTOR", stateful)
        monkeypatch.setattr(mcp_read_executor, "_CONTROL_EXECUTOR", control)
        return mcp_store_executor.StoreWorkers(reads=reads, stateful=stateful), control

    yield install
    for pool in created:
        pool.close()
        assert pool.pending_count == 0


def registered(tmp_path, monkeypatch, backend, *, workers=None):
    class FakeControlClient:
        def __init__(self, **_kwargs):
            pass

        def request(self, operation, payload, **kwargs):
            name = "manager_roles" if operation == "role_read" else "manager_role_action"
            return {"ok": True, "data": backend(name, operation=operation, payload=payload, **kwargs)}

    monkeypatch.setattr(mcp_tools, "AutomationControlClient", FakeControlClient)
    monkeypatch.setattr(
        mcp_tools,
        "parts_store_cards",
        lambda operation, **kwargs: backend("parts_store_cards", operation=operation, **kwargs),
    )
    before = {}

    def capture_registration(server):
        for name, tool in server._tool_manager._tools.items():
            before[name] = (tool.parameters, tool.fn_metadata, inspect.signature(tool.fn), tool.annotations)
        fingerprint = mcp_schema_fingerprint({name: row[0] for name, row in before.items()})
        instrument_manager_tools(server)
        assert (
            mcp_schema_fingerprint({name: tool.parameters for name, tool in server._tool_manager._tools.items()})
            == fingerprint
        )

    monkeypatch.setattr(mcp_tools, "instrument_manager_tools", capture_registration)
    server = FastMCP("synthetic-point-fix")
    if workers is not None:
        server.store_workers = workers
    mcp_tools.register_manager_tools(server, StoreState(tmp_path / "memory.sqlite3"), include_tools=TOOLS)
    for name, tool in server._tool_manager._tools.items():
        parameters, metadata, signature, annotations = before[name]
        assert tool.parameters is parameters and tool.fn_metadata is metadata
        assert inspect.signature(tool.fn) == signature and tool.annotations is annotations
        assert tool.is_async is True
        wrapped = tool.fn
        instrument_manager_tools(server)
        assert tool.fn is wrapped
    return server._tool_manager._tools


def arguments(name, operation="create", marker="synthetic"):
    if name == "manager_roles":
        return {"operation": "status", "role": "M2", "reference_id": marker, "limit": 7}
    if name == "manager_role_action":
        return {
            "operation": "note",
            "role": "M2",
            "actor": "owner",
            "idempotency_key": marker,
            "payload": {"kind": "observation", "text": "synthetic"},
            "expected_revision": 4,
        }
    return {"operation": operation, "card_id": "synthetic-card", "title": marker, "idempotency_key": marker}


async def wait_for(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.parametrize(
    ("name", "operation"),
    [
        ("manager_roles", "status"),
        ("manager_role_action", "note"),
        *[("parts_store_cards", op) for op in ("list", "get", "create", "append_note")],
    ],
)
def test_real_registration_offloads_preserves_results_context_and_event_loop(
    tmp_path, monkeypatch, pools, name, operation
):
    workers, _ = pools()
    entered, release = threading.Event(), threading.Event()
    context = ContextVar("point_fix_synthetic", default="missing")
    calls = []
    original = {"ok": True, "nested": {"items": ["synthetic"]}}
    snapshot = deepcopy(original)

    def backend(tool_name, **kwargs):
        calls.append((tool_name, threading.get_ident(), context.get(), kwargs))
        entered.set()
        assert release.wait(1), "The native MCP event loop could not release its backend"
        return original

    tools = registered(tmp_path, monkeypatch, backend, workers=workers)

    async def run():
        context.set("copied")
        task = asyncio.create_task(tools[name].run(arguments(name, operation)))
        try:
            await wait_for(entered.is_set)
            # Another request/coroutine can run while the synchronous backend waits.
            await asyncio.sleep(0)
            release.set()
            result = await task
            assert {key: value for key, value in result.items() if key != "tool_execution"} == snapshot
            assert result["tool_execution"]["outcome"] == "ok"
            assert calls[0][0:1] == (name,)
            assert calls[0][1] != threading.get_ident() and calls[0][2] == "copied"
            if name == "manager_role_action":
                assert calls[0][3]["idempotency_key"] == "synthetic"
                assert calls[0][3]["expected_revision"] == 4
            elif name == "manager_roles":
                assert calls[0][3]["payload"]["reference_id"] == "synthetic"
            else:
                assert calls[0][3]["operation"] == operation
            assert original == snapshot
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_queued_cancel_never_calls_adapter_and_running_cancel_holds_real_slot(tmp_path, monkeypatch, pools, name):
    workers, control = pools(waiting=1)
    pool = control if name != "parts_store_cards" else workers.stateful
    entered, release = threading.Event(), threading.Event()
    calls = []

    def backend(_name, **kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(2)
        return {"ok": True}

    tools = registered(tmp_path, monkeypatch, backend)

    async def run():
        active = asyncio.create_task(tools[name].run(arguments(name, marker="active")))
        queued = None
        try:
            await wait_for(entered.is_set)
            queued = asyncio.create_task(tools[name].run(arguments(name, marker="queued")))
            await wait_for(lambda: pool.pending_count == 2)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
            assert pool.pending_count == 2
            busy = await tools[name].run(arguments(name, marker="rejected"))
            assert busy["ok"] is False and busy["adapter_started"] is False
            assert busy["adapter_may_continue"] is False and busy["retryable"] is False
            if name != "manager_roles":
                assert busy["outcome_uncertain"] is False and busy["readback_required"] is False
                assert busy["request_dispatched"] is False
            assert len(calls) == 1
            release.set()
            await wait_for(lambda: pool.pending_count == 0)
            assert len(calls) == 1
        finally:
            release.set()
            await asyncio.gather(active, *([queued] if queued is not None else []), return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("name", sorted(TOOLS))
@pytest.mark.parametrize("operation", ["get", "append_note"])
def test_started_timeout_keeps_slot_and_requires_readback_only_for_mutations(
    tmp_path, monkeypatch, pools, name, operation
):
    workers, control = pools(waiting=0, timeout=0.05)
    pool = control if name != "parts_store_cards" else workers.reads if operation == "get" else workers.stateful
    release = threading.Event()
    calls = []

    def backend(_name, **_kwargs):
        calls.append(1)
        assert release.wait(2)
        return {"ok": True}

    tools = registered(tmp_path, monkeypatch, backend, workers=workers)

    async def run():
        try:
            result = await tools[name].run(arguments(name, operation, marker="private-input"))
            assert result["ok"] is False and result["adapter_started"] is True
            assert result["adapter_may_continue"] is True and result["stage"] == "worker_wait"
            assert result["retry_attempted"] is False and result["retryable"] is False
            assert pool.pending_count == 1 and calls == [1]
            assert "private-input" not in str(result)
            if name != "manager_roles":
                uncertain = name == "manager_role_action" or operation == "append_note"
                assert result["outcome_uncertain"] is uncertain
                assert result["readback_required"] is uncertain
                assert result["readback_verified"] is False and result["request_dispatched"] is None
            if name == "parts_store_cards":
                assert result["column"] == "parts_store"
                assert result["status"] == ("uncertain" if operation == "append_note" else "blocked")
                assert result["error"] == "crm_executor_wait_timeout"
        finally:
            release.set()
            await wait_for(lambda: pool.pending_count == 0)

    asyncio.run(run())


def test_native_instance_drain_rejects_new_cards_and_tracks_started_write(tmp_path, monkeypatch, pools):
    workers, _ = pools()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def backend(_name, **kwargs):
        calls.append(kwargs["operation"])
        entered.set()
        assert release.wait(2)
        return {"ok": True}

    tools = registered(tmp_path, monkeypatch, backend, workers=workers)

    class WrongPool:
        async def run(self, _operation):
            pytest.fail("Native CRM callbacks must use their server's drainable workers")

    monkeypatch.setattr(mcp_store_executor, "_READ_EXECUTOR", WrongPool())
    monkeypatch.setattr(mcp_store_executor, "_STATEFUL_EXECUTOR", WrongPool())

    async def run():
        active = asyncio.create_task(tools["parts_store_cards"].run(arguments("parts_store_cards")))
        try:
            await wait_for(entered.is_set)
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
            status = workers.begin_drain()
            assert status["pending"]["stateful"] == 1 and status["drained"] is False
            for operation in ("get", "append_note"):
                blocked = await tools["parts_store_cards"].run(arguments("parts_store_cards", operation))
                assert blocked["error"] == "crm_executor_draining"
                assert blocked["adapter_started"] is False and blocked["outcome_uncertain"] is False
                assert blocked["request_dispatched"] is False and blocked["readback_required"] is False
            assert calls == ["create"]
            release.set()
            await wait_for(lambda: workers.stateful.pending_count == 0)
            assert workers.status()["drained"] is True
        finally:
            release.set()
            await asyncio.gather(active, return_exceptions=True)

    asyncio.run(run())


def test_serialized_crm_append_retains_both_notes_and_leaves_independent_readback(tmp_path, monkeypatch, pools):
    workers, _ = pools()
    entered, release = threading.Event(), threading.Event()
    card = {"id": "synthetic-card", "column": "parts_store", "description": "original", "updated_at": "r1"}
    writes = []

    class FakeCrmTransport:
        def invoke(self, name, arguments, *, idempotency_key=""):
            if name == "update_card":
                if not writes:
                    entered.set()
                    assert release.wait(2)
                assert arguments["expected_updated_at"] == card["updated_at"]
                writes.append((idempotency_key, arguments["description"]))
                card.update(description=arguments["description"], updated_at=f"r{len(writes) + 1}")
            else:
                assert name == "get_card"
            return {"ok": True, "data": {"data": {"card": dict(card)}}}

    transport = FakeCrmTransport()

    def backend(_name, **kwargs):
        return parts_store_cards(transport=transport, **kwargs)

    tools = registered(tmp_path, monkeypatch, backend, workers=workers)
    tool = tools["parts_store_cards"]

    async def run():
        first = asyncio.create_task(
            tool.run({**arguments("parts_store_cards", "append_note", "first"), "note": "first"})
        )
        second = None
        try:
            await wait_for(entered.is_set)
            second = asyncio.create_task(
                tool.run({**arguments("parts_store_cards", "append_note", "second"), "note": "second"})
            )
            await wait_for(lambda: workers.stateful.pending_count == 2)
            readback = await asyncio.wait_for(tool.run(arguments("parts_store_cards", "get")), 1)
            assert readback["ok"] is True and readback["card"]["description"] == "original"
            assert writes == []
            release.set()
            results = await asyncio.gather(first, second)
            assert all(result["ok"] is True for result in results)
            assert writes == [("first", "original\n\nfirst"), ("second", "original\n\nfirst\n\nsecond")]
            assert results[-1]["card"]["description"] == "original\n\nfirst\n\nsecond"
        finally:
            release.set()
            await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_native_validation_and_worker_failure_never_echo_sensitive_inputs(tmp_path, monkeypatch, pools, name):
    pools()
    calls = []

    def backend(_name, **_kwargs):
        calls.append(1)
        raise RuntimeError("sensitive synthetic backend failure")

    tools = registered(tmp_path, monkeypatch, backend)

    async def run():
        invalid = {**arguments(name), "operation": "invalid_operation"}
        with pytest.raises(ToolError):
            await tools[name].run(invalid)
        assert calls == []
        result = await tools[name].run(arguments(name, marker="sensitive synthetic input"))
        assert result["ok"] is False and result["adapter_started"] is True
        assert result["adapter_may_continue"] is False and calls == [1]
        assert "sensitive synthetic" not in str(result)
        if name != "manager_roles":
            assert result["outcome_uncertain"] is True and result["readback_required"] is True

    asyncio.run(run())
