from __future__ import annotations

import asyncio
from contextvars import ContextVar
import threading

import pytest
from mcp.server.fastmcp import FastMCP

from autostop_manager import mcp_read_executor as executor_module
from autostop_manager.listing_executor import BoundedListingExecutor, ListingExecutionError
from autostop_manager.mcp_telemetry import instrument_manager_tools


def _error_code(result):
    error = result["error"]
    return error["code"] if isinstance(error, dict) else error


@pytest.mark.parametrize("tool_name", ["search_web_multi", "fetch_page_excerpt", "fetch_page_browser"])
@pytest.mark.parametrize("stage", ["admission", "worker_wait"])
def test_native_web_execution_failure_keeps_documented_gateway_envelope(monkeypatch, tool_name, stage):
    code = "provider_busy" if stage == "admission" else "provider_wait_timeout"

    class FailedPool:
        async def run(self, _operation):
            raise ListingExecutionError(
                code,
                stage=stage,
                adapter_started=stage == "worker_wait",
                adapter_may_continue=stage == "worker_wait",
            )

    monkeypatch.setattr(executor_module, "_WEB_EXECUTOR", FailedPool())
    server = FastMCP("synthetic-web-envelope")

    def web_read(marker: str) -> dict:
        pytest.fail("The synthetic executor must fail before invoking the adapter")

    server.tool(name=tool_name)(web_read)
    instrument_manager_tools(server)
    result = asyncio.run(server._tool_manager.get_tool(tool_name).run({"marker": "unprocessed-private-input"}))
    assert result["schema"] == "WebResearchGatewayV1"
    assert result["capability"] == tool_name
    assert result["read_only"] is True
    assert result["error"] == {"code": code, "retryable": False}
    assert result["stage"] == stage
    assert result["adapter_started"] is (stage == "worker_wait")
    assert result["adapter_may_continue"] is (stage == "worker_wait")
    assert result["tool_execution"]["outcome"] == "error"
    assert "unprocessed-private-input" not in str(result)
    if tool_name == "search_web_multi":
        assert result["query"] == ""
        assert result["results"] == []
        assert result["providers"] == []
        assert result["allowed_domains"] == []
        assert result["provider_order"] == []
        assert result["fallback_used"] is False


@pytest.mark.parametrize(
    ("tool_name", "pool_name"),
    [
        ("search_web_multi", "_WEB_EXECUTOR"),
        ("fetch_page_excerpt", "_WEB_EXECUTOR"),
        ("fetch_page_browser", "_WEB_EXECUTOR"),
        ("manager_automations", "_CONTROL_EXECUTOR"),
        ("manager_automation_control", "_CONTROL_EXECUTOR"),
    ],
)
def test_native_blocking_callback_allows_other_requests_to_progress(monkeypatch, tool_name, pool_name):
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=2)
    monkeypatch.setattr(executor_module, pool_name, pool, raising=False)
    heartbeat = threading.Event()
    entered = asyncio.Event()
    loop = None

    def blocking() -> dict:
        loop.call_soon_threadsafe(entered.set)
        return {"ok": True, "other_request_progressed": heartbeat.wait(0.3)}

    server = FastMCP("synthetic-loop-progress")
    server.tool(name=tool_name)(blocking)
    instrument_manager_tools(server)

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()

        async def another_request():
            await entered.wait()
            heartbeat.set()

        task = asyncio.create_task(another_request())
        try:
            result = await server._tool_manager.get_tool(tool_name).run({})
            assert result["other_request_progressed"] is True
        finally:
            await asyncio.wait_for(task, 1)

    try:
        asyncio.run(run())
    finally:
        pool.close()


@pytest.mark.parametrize(
    ("tool_name", "pool_name"),
    [
        ("partsapi_catalog_lookup", "_EXECUTOR"),
        ("elcats_catalog_query", "_EXECUTOR"),
        ("search_web_multi", "_WEB_EXECUTOR"),
        ("fetch_page_excerpt", "_WEB_EXECUTOR"),
        ("fetch_page_browser", "_WEB_EXECUTOR"),
        ("manager_automations", "_CONTROL_EXECUTOR"),
        ("manager_automation_control", "_CONTROL_EXECUTOR"),
    ],
)
def test_native_blocking_reads_overlap_keep_heartbeat_and_preserve_schema(monkeypatch, tool_name, pool_name):
    pool = BoundedListingExecutor(max_workers=2, max_waiting_calls=0, timeout_seconds=2)
    monkeypatch.setattr(executor_module, pool_name, pool, raising=False)
    release = threading.Event()
    server = FastMCP("synthetic-read-workers")
    entered = [asyncio.Event(), asyncio.Event()]
    loop = None

    def read(operation: str, article_id: int, provider_parameters: dict[str, str | int | float]) -> dict:
        assert loop is not None
        loop.call_soon_threadsafe(entered[article_id].set)
        assert release.wait(2)
        return {"ok": True, "operation": operation, "provider_parameters": provider_parameters}

    server.tool(name=tool_name)(read)
    server.tool(name="inspect_vehicle_identifier")(lambda identifier: {"ok": True})
    tool = server._tool_manager._tools[tool_name]
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


@pytest.mark.parametrize(
    ("tool_name", "pool_name", "busy_error"),
    [
        ("", "_EXECUTOR", "provider_busy"),
        ("fetch_page_browser", "_WEB_EXECUTOR", "provider_busy"),
        ("manager_automation_control", "_CONTROL_EXECUTOR", "automation_control_unavailable"),
    ],
)
def test_cancelled_native_read_keeps_admission_until_real_worker_finishes(
    monkeypatch, tool_name, pool_name, busy_error
):
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=2)
    monkeypatch.setattr(executor_module, pool_name, pool)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def read():
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return {"ok": True}

    wrapped = executor_module.offload_read(read, tool_name)

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
            assert busy["ok"] is False and _error_code(busy) == busy_error
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


@pytest.mark.parametrize(
    ("tool_name", "pool_name", "timeout_error"),
    [
        ("", "_EXECUTOR", "provider_wait_timeout"),
        ("fetch_page_browser", "_WEB_EXECUTOR", "provider_wait_timeout"),
        ("manager_automation_control", "_CONTROL_EXECUTOR", "automation_control_unavailable"),
    ],
)
def test_worker_wait_timeout_retains_admission_and_copied_context(monkeypatch, tool_name, pool_name, timeout_error):
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=0.05)
    monkeypatch.setattr(executor_module, pool_name, pool)
    release = threading.Event()
    context = ContextVar("synthetic_read_context", default="missing")
    captured = []

    def read():
        captured.append(context.get())
        assert release.wait(2)
        return {"ok": True}

    async def run():
        context.set("copied")
        failure = await executor_module.offload_read(read, tool_name)()
        assert failure["ok"] is False and _error_code(failure) == timeout_error
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


def test_blocked_web_read_does_not_consume_automation_control_capacity(monkeypatch):
    web_pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=2)
    control_pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=2)
    monkeypatch.setattr(executor_module, "_WEB_EXECUTOR", web_pool)
    monkeypatch.setattr(executor_module, "_CONTROL_EXECUTOR", control_pool)
    entered, release = threading.Event(), threading.Event()

    def web_read():
        entered.set()
        assert release.wait(2)
        return {"ok": True}

    async def run():
        web = executor_module.offload_read(web_read, "fetch_page_browser")
        blocked = asyncio.create_task(web())
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            assert _error_code(await web()) == "provider_busy"
            control = executor_module.offload_read(lambda: {"ok": True}, "manager_automation_control")
            assert (await asyncio.wait_for(control(), 0.5))["ok"] is True
            assert not blocked.done()
        finally:
            release.set()
            await asyncio.gather(blocked, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        release.set()
        web_pool.close()
        control_pool.close()


@pytest.mark.parametrize("tool_name", ["fetch_page_browser", "manager_automation_control"])
def test_cancelled_queued_mcp_call_never_starts_backend(monkeypatch, tool_name):
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=2)
    pool_name = "_CONTROL_EXECUTOR" if tool_name == "manager_automation_control" else "_WEB_EXECUTOR"
    monkeypatch.setattr(executor_module, pool_name, pool)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocking():
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return {"ok": True}

    wrapped = executor_module.offload_read(blocking, tool_name)

    async def run():
        first = asyncio.create_task(wrapped())
        queued = None
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            queued = asyncio.create_task(wrapped())
            while pool.pending_count != 2:
                await asyncio.sleep(0.001)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            assert pool.pending_count == 2
            release.set()
            assert (await first)["ok"] is True
            while pool.pending_count:
                await asyncio.sleep(0.001)
            assert calls == [1]
        finally:
            release.set()
            await asyncio.gather(first, *([queued] if queued is not None else []), return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        release.set()
        pool.close()


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
