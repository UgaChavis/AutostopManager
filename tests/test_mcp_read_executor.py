from __future__ import annotations

import asyncio
from contextvars import ContextVar
import threading

import pytest
from mcp.server.fastmcp import FastMCP

from autostop_manager import mcp_read_executor as executor_module
from autostop_manager.listing_executor import BoundedListingExecutor, ListingExecutionError
from autostop_manager.mcp_telemetry import instrument_manager_tools


def test_native_blocking_reads_overlap_keep_heartbeat_and_preserve_schema(monkeypatch):
    pool = BoundedListingExecutor(max_workers=2, max_waiting_calls=0, timeout_seconds=2)
    monkeypatch.setattr(executor_module, "_EXECUTOR", pool)
    release = threading.Event()
    server = FastMCP("synthetic-read-workers")
    entered = [asyncio.Event(), asyncio.Event()]
    loop = None

    def read(operation: str, article_id: int, provider_parameters: dict[str, str | int | float]) -> dict:
        assert loop is not None
        loop.call_soon_threadsafe(entered[article_id].set)
        assert release.wait(2)
        return {"ok": True, "operation": operation, "provider_parameters": provider_parameters}

    server.tool(name="partsapi_catalog_lookup")(read)
    server.tool(name="inspect_vehicle_identifier")(lambda identifier: {"ok": True})
    tool = server._tool_manager._tools["partsapi_catalog_lookup"]
    parameters, metadata = tool.parameters, tool.fn_metadata
    instrument_manager_tools(server)
    first_wrapper = tool.fn
    instrument_manager_tools(server)
    assert tool.fn is first_wrapper
    assert tool.is_async is True
    assert tool.parameters is parameters
    assert tool.fn_metadata is metadata
    assert server._tool_manager._tools["inspect_vehicle_identifier"].is_async is False

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        tasks = [
            asyncio.create_task(
                tool.run({"operation": "synthetic", "article_id": index, "provider_parameters": {"carId": 123}})
            )
            for index in range(2)
        ]
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), 1)
            await asyncio.wait_for(asyncio.sleep(0.01), 0.1)
            assert not any(task.done() for task in tasks)
            release.set()
            results = await asyncio.gather(*tasks)
            assert all(row["provider_parameters"] == {"carId": 123} for row in results)
            assert len({row["tool_execution"]["call_id"] for row in results}) == 2
            assert all(row["tool_execution"]["outcome"] == "ok" for row in results)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        release.set()
        pool.close()


def test_cancelled_native_read_keeps_admission_until_real_worker_finishes(monkeypatch):
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=2)
    monkeypatch.setattr(executor_module, "_EXECUTOR", pool)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def read():
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return {"ok": True}

    wrapped = executor_module.offload_read(read)

    async def run():
        task = asyncio.create_task(wrapped())
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert pool.pending_count == 1
            busy = await wrapped()
            assert busy["ok"] is False and busy["error"] == "provider_busy"
            assert busy["adapter_started"] is False
            assert busy["adapter_may_continue"] is False
            assert busy["retryable"] is False
            assert calls == [1]
            release.set()
            while pool.pending_count:
                await asyncio.sleep(0.001)
            assert (await wrapped())["ok"] is True
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        release.set()
        pool.close()


def test_worker_wait_timeout_retains_admission_and_copied_context(monkeypatch):
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=0.05)
    monkeypatch.setattr(executor_module, "_EXECUTOR", pool)
    release = threading.Event()
    context = ContextVar("synthetic_read_context", default="missing")
    captured = []

    def read():
        captured.append(context.get())
        assert release.wait(2)
        return {"ok": True}

    async def run():
        context.set("copied")
        failure = await executor_module.offload_read(read)()
        assert failure["ok"] is False and failure["error"] == "provider_wait_timeout"
        assert failure["stage"] == "worker_wait"
        assert failure["adapter_started"] is True
        assert failure["adapter_may_continue"] is True
        assert failure["retry_attempted"] is False
        assert failure["retryable"] is False
        assert pool.pending_count == 1
        assert captured == ["copied"]
        release.set()

    try:
        asyncio.run(run())
    finally:
        release.set()
        pool.close()
    assert pool.pending_count == 0


def test_fake_registration_remains_synchronous():
    def read():
        return {"ok": True}

    class FakeServer:
        def __init__(self):
            self.tools = {"partsapi_catalog_lookup": read}

    server = FakeServer()
    instrument_manager_tools(server)
    result = server.tools["partsapi_catalog_lookup"]()
    assert result["ok"] is True
    assert result["tool_execution"]["tool"] == "partsapi_catalog_lookup"


def test_native_busy_result_preserves_error_flags_and_failure_telemetry(monkeypatch):
    class BusyPool:
        async def run(self, _operation):
            raise ListingExecutionError("provider_busy", stage="admission")

    monkeypatch.setattr(executor_module, "_EXECUTOR", BusyPool())
    server = FastMCP("synthetic-worker-overflow")

    def read(identifier: str) -> dict:
        pytest.fail("An admission refusal must not start the adapter")

    server.tool(name="decode_vin_vpic")(read)
    instrument_manager_tools(server)
    tool = server._tool_manager._tools["decode_vin_vpic"]
    result = asyncio.run(tool.run({"identifier": "synthetic"}))
    assert result["ok"] is False
    assert result["error"] == "provider_busy"
    assert result["stage"] == "admission"
    assert result["adapter_started"] is False
    assert result["adapter_may_continue"] is False
    assert result["retry_attempted"] is False
    assert result["retryable"] is False
    assert result["tool_execution"]["outcome"] == "error"
