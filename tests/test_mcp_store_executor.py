"""Bound real Store MCP workers; all adapters here use synthetic in-memory data."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from copy import deepcopy
import json
import socket
import threading
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from autostop_manager import mcp_read_executor, mcp_store_executor
from autostop_manager.listing_executor import BoundedListingExecutor, ListingExecutionError
from autostop_manager.mcp_telemetry import instrument_manager_tools


READ_TOOLS = {
    "store_runtime_status",
    "store_search",
    "store_entity_context",
    "download_store_quote_vin_photo",
    "get_store_analytics_report",
    "store_owner_capabilities",
}
STATEFUL_TOOLS = {"store_digest", "store_management_action", "store_owner_api", "store_quote_conductor"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("Store executor regression attempted real network")

    # Windows asyncio uses an internal socketpair for wakeups. Block HTTP/DNS
    # acquisition without disabling that event-loop implementation detail.
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture
def pools(monkeypatch):
    created = []

    def install(name="_STATEFUL_EXECUTOR", *, workers=1, waiting=0, timeout=5):
        pool = BoundedListingExecutor(max_workers=workers, max_waiting_calls=waiting, timeout_seconds=timeout)
        monkeypatch.setattr(mcp_store_executor, name, pool)
        created.append(pool)
        return pool

    yield install
    for pool in created:
        pool.close()
        assert pool.pending_count == 0


def native(name, backend):
    server = FastMCP("synthetic-store-executor")
    server.tool(name=name)(backend)
    tool = server._tool_manager.get_tool(name)
    parameters, metadata = tool.parameters, tool.fn_metadata
    instrument_manager_tools(server)
    first_wrapper = tool.fn
    instrument_manager_tools(server)
    assert tool.fn is first_wrapper
    assert tool.is_async is True
    assert tool.parameters is parameters and tool.fn_metadata is metadata
    return tool


async def pending(pool, expected):
    async with asyncio.timeout(3):
        while pool.pending_count != expected:
            await asyncio.sleep(0)


def format_for(name):
    if name in {"store_owner_api", "store_owner_capabilities"}:
        return "autostop_store_owner_api_v1"
    if name == "store_quote_conductor":
        return "agent_envelope_v2"
    if name == "get_store_analytics_report":
        return "store_analytics_report_v1"
    return "store_agent_v1"


def test_every_store_root_is_assigned_to_one_separate_capacity_group():
    assert mcp_store_executor.STORE_READ_TOOLS == READ_TOOLS
    assert mcp_store_executor.STORE_STATEFUL_TOOLS == STATEFUL_TOOLS
    assert mcp_store_executor.STORE_BLOCKING_TOOLS == READ_TOOLS | STATEFUL_TOOLS
    assert mcp_store_executor._READ_EXECUTOR is not mcp_store_executor._STATEFUL_EXECUTOR
    assert mcp_store_executor._STATEFUL_EXECUTOR is not mcp_read_executor._CONTROL_EXECUTOR


@pytest.mark.parametrize("name", sorted(READ_TOOLS | STATEFUL_TOOLS))
def test_native_store_callback_preserves_arguments_result_and_responsive_loop(pools, name):
    pools("_STATEFUL_EXECUTOR" if name in STATEFUL_TOOLS else "_READ_EXECUTOR")
    release = threading.Event()
    entered = asyncio.Event()
    calls = []
    loop = None
    original = {"ok": True, "format": format_for(name), "nested": {"items": ["synthetic"]}}
    snapshot = deepcopy(original)

    def backend(marker: str, planned_changes: dict[str, str]) -> dict[str, Any]:
        calls.append((threading.get_ident(), marker, deepcopy(planned_changes)))
        loop.call_soon_threadsafe(entered.set)
        progressed = release.wait(1)
        assert progressed, "The native MCP event loop could not release the Store worker"
        return original

    tool = native(name, backend)

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()

        async def heartbeat():
            await entered.wait()
            release.set()

        ticker = asyncio.create_task(heartbeat())
        try:
            content, result = await tool.run(
                {"marker": "synthetic", "planned_changes": '{"storage_location":"A-1"}'}, convert_result=True
            )
            await asyncio.wait_for(ticker, 1)
            assert json.loads(content[0].text) == result
            assert {key: value for key, value in result.items() if key != "tool_execution"} == snapshot
            assert result["tool_execution"]["outcome"] == "ok"
            assert calls == [(calls[0][0], "synthetic", {"storage_location": "A-1"})]
            assert calls[0][0] != threading.get_ident()
            assert original == snapshot
        finally:
            release.set()
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("name", sorted(READ_TOOLS | STATEFUL_TOOLS))
@pytest.mark.parametrize(
    ("code", "stage", "started", "continuing"),
    [
        ("provider_busy", "admission", False, False),
        ("provider_executor_unavailable", "admission", False, False),
        ("provider_wait_timeout", "worker_wait", True, True),
        ("provider_execution_failed", "worker_wait", True, False),
    ],
)
def test_native_failure_keeps_domain_envelope_and_truthful_write_state(
    monkeypatch, name, code, stage, started, continuing
):
    class FailedPool:
        async def run(self, _operation):
            raise ListingExecutionError(code, stage=stage, adapter_started=started, adapter_may_continue=continuing)

    monkeypatch.setattr(
        mcp_store_executor, "_STATEFUL_EXECUTOR" if name in STATEFUL_TOOLS else "_READ_EXECUTOR", FailedPool()
    )

    def backend(private_input: str) -> dict[str, Any]:
        pytest.fail("Refused Store invocation must not run the backend")

    tool = native(name, backend)
    content, result = asyncio.run(tool.run({"private_input": "do-not-echo"}, convert_result=True))
    error = {
        "provider_busy": "store_executor_busy",
        "provider_executor_unavailable": "store_executor_unavailable",
        "provider_wait_timeout": "store_executor_wait_timeout",
        "provider_execution_failed": "store_executor_failed",
    }[code]
    uncertain = started and name in STATEFUL_TOOLS
    assert result["ok"] is False
    assert result["format"] == format_for(name)
    assert result["status"] == ("compensating" if uncertain else "degraded")
    assert result["summary"]["error_code"] == error
    assert result["meta"]["request_dispatched"] is (None if started else False)
    assert result["meta"]["outcome_uncertain"] is uncertain
    assert result["meta"]["stage"] == stage
    assert result["meta"]["adapter_started"] is started
    assert result["meta"]["adapter_may_continue"] is continuing
    assert result["meta"]["retryable"] is False
    assert result["meta"]["retry_attempted"] is False
    if uncertain:
        assert result["meta"]["readback_required"] is True
        assert result["meta"]["readback_verified"] is False
    if name == "store_quote_conductor":
        assert result["meta"]["conductor_format"] == "store_quote_conductor_v1"
    if name == "get_store_analytics_report":
        assert result["meta"]["aggregatedOnly"] is True
        assert result["meta"]["rawEventsIncluded"] is False
    assert result["tool_execution"]["outcome"] == "error"
    assert json.loads(content[0].text) == result
    assert "do-not-echo" not in repr(result)


def test_native_validation_rejects_missing_arguments_before_worker_admission(monkeypatch):
    class NoAdmission:
        async def run(self, _operation):
            pytest.fail("SDK validation must run before Store worker admission")

    monkeypatch.setattr(mcp_store_executor, "_STATEFUL_EXECUTOR", NoAdmission())
    tool = native("store_management_action", lambda required_argument: {"ok": True})
    with pytest.raises(ToolError):
        asyncio.run(tool.run({}))


def test_stateful_workers_serialize_with_a_bounded_queue(pools):
    pool = pools(waiting=2)
    release = threading.Event()
    entered = asyncio.Event()
    calls = []
    loop = None

    def backend(marker: str) -> dict:
        calls.append(marker)
        if marker == "running":
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3)
        return {"ok": True}

    tool = native("store_management_action", backend)

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        tasks = [asyncio.create_task(tool.run({"marker": "running"}))]
        try:
            await asyncio.wait_for(entered.wait(), 1)
            tasks += [asyncio.create_task(tool.run({"marker": marker})) for marker in ("queued-1", "queued-2")]
            await pending(pool, 3)
            refusal = await tool.run({"marker": "overflow"})
            assert refusal["summary"]["error_code"] == "store_executor_busy"
            assert refusal["meta"]["request_dispatched"] is False
            assert calls == ["running"]
            release.set()
            assert all(row["ok"] for row in await asyncio.gather(*tasks))
            await pending(pool, 0)
            assert calls == ["running", "queued-1", "queued-2"]
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("cancel_queued", [False, True], ids=["running", "queued"])
def test_native_cancellation_retains_admission_and_does_not_start_abandoned_write(pools, cancel_queued):
    pool = pools(waiting=int(cancel_queued))
    release = threading.Event()
    entered = asyncio.Event()
    calls = []
    loop = None

    def backend(marker: str) -> dict:
        calls.append(marker)
        if marker == "running":
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3)
        return {"ok": True}

    tool = native("store_management_action", backend)

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        tasks = [asyncio.create_task(tool.run({"marker": "running"}))]
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if cancel_queued:
                tasks.append(asyncio.create_task(tool.run({"marker": "abandoned"})))
                await pending(pool, 2)
            tasks[-1].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[-1]
            assert pool.pending_count == len(tasks)
            result = await tool.run({"marker": "overflow"})
            assert result["summary"]["error_code"] == "store_executor_busy"
            assert calls == ["running"]
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await pending(pool, 0)
            assert calls == ["running"]
            assert (await tool.run({"marker": "next"}))["ok"] is True
            assert calls == ["running", "next"]
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


def test_native_timeouts_keep_running_write_uncertain_and_skip_expired_queue(pools):
    pool = pools(waiting=1, timeout=0.2)
    release = threading.Event()
    entered = asyncio.Event()
    calls = []
    context = ContextVar("synthetic_store_request", default="missing")
    loop = None

    def backend(marker: str) -> dict:
        calls.append((marker, context.get()))
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        return {"ok": True}

    tool = native("store_management_action", backend)

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        context.set("copied")
        first = asyncio.create_task(tool.run({"marker": "running"}))
        queued = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            queued = asyncio.create_task(tool.run({"marker": "queued"}))
            running_result, queued_result = await asyncio.gather(first, queued)
            assert running_result["status"] == "compensating"
            assert running_result["meta"]["outcome_uncertain"] is True
            assert running_result["meta"]["adapter_may_continue"] is True
            assert queued_result["meta"]["request_dispatched"] is False
            assert queued_result["meta"]["outcome_uncertain"] is False
            assert pool.pending_count == 2
            assert calls == [("running", "copied")]
            release.set()
            await pending(pool, 0)
            assert calls == [("running", "copied")]
        finally:
            release.set()
            await asyncio.gather(first, *([queued] if queued is not None else []), return_exceptions=True)

    asyncio.run(run())


def test_store_stateful_saturation_keeps_read_and_control_capacity_available(pools, monkeypatch):
    pool = pools()
    pools("_READ_EXECUTOR")
    control_pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=5)
    monkeypatch.setattr(mcp_read_executor, "_CONTROL_EXECUTOR", control_pool)
    release = threading.Event()
    entered = asyncio.Event()
    loop = None

    def write() -> dict:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        return {"ok": True}

    write_tool = native("store_management_action", write)
    read_tool = native("store_search", lambda: {"ok": True})
    control_tool = native("manager_automations", lambda: {"ok": True})

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(write_tool.run({}))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert (await write_tool.run({}))["summary"]["error_code"] == "store_executor_busy"
            assert (await asyncio.wait_for(read_tool.run({}), 1))["ok"] is True
            assert (await asyncio.wait_for(control_tool.run({}), 1))["ok"] is True
            assert pool.pending_count == 1 and not task.done()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        release.set()
        control_pool.close()
