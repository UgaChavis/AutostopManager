"""Fail closed after a real ThreadPoolExecutor worker-start failure.

Four pytest cases cover two fault regimes: the first worker through provider,
web and control native interfaces (two interface repeats), and the second worker
while an accepted sibling owns the first worker. Only one named Thread.start
raises; submit, queue, Future, wrappers and SDK registration remain real.

Every backend records synthetic markers only. No network, services or databases.
The second-worker case proves post-refusal/drain behavior; it deliberately does
not claim that an immediate Thread.start exception proves the separate gate
needed before submit's outcome is known. That race needs an additional observed
worker-boundary handshake, not a scheduling sleep in these public regressions.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from mcp.server.fastmcp import FastMCP

from autostop_manager import mcp_read_executor
from autostop_manager.listing_executor import BoundedListingExecutor, ListingExecutionError
from autostop_manager.mcp_telemetry import instrument_manager_tools


_FAMILIES = (
    ("partsapi_catalog_lookup", "_EXECUTOR"),
    ("fetch_page_browser", "_WEB_EXECUTOR"),
    ("manager_automation_control", "_CONTROL_EXECUTOR"),
)
_FLAGS = {
    "stage": "admission",
    "adapter_started": False,
    "adapter_may_continue": False,
    "retry_attempted": False,
    "retryable": False,
}
_UNAVAILABLE = {
    "error": "provider_executor_unavailable",
    "stage": "admission",
    "adapter_started": False,
    "adapter_may_continue": False,
}


def _pool(prefix, *, workers=1):
    pool = BoundedListingExecutor(max_workers=workers, max_waiting_calls=2, timeout_seconds=30)
    pool._pool._thread_name_prefix = prefix
    return pool


def _native_tools(calls):
    server = FastMCP("synthetic-executor-start-failure")

    def backend_for(family):
        def backend(marker: str) -> dict:
            calls.append((family, marker))
            return {"ok": True, "family": family, "marker": marker}

        return backend

    for family, _pool_name in _FAMILIES:
        server.tool(name=family)(backend_for(family))
    instrument_manager_tools(server)
    return {family: server._tool_manager.get_tool(family) for family, _pool_name in _FAMILIES}


def _without_trace(result):
    return {key: value for key, value in result.items() if key != "tool_execution"}


def _native_failure(family):
    if family == "manager_automation_control":
        return {"ok": False, "error": "automation_control_unavailable", **_FLAGS}
    if family == "fetch_page_browser":
        return {
            "ok": False,
            "schema": "WebResearchGatewayV1",
            "capability": family,
            "read_only": True,
            "error": {"code": "provider_executor_unavailable", "retryable": False},
            **_FLAGS,
        }
    return {"ok": False, "outcome": "provider_error", "error": "provider_executor_unavailable", **_FLAGS}


def _assert_native_failure(result, family):
    assert _without_trace(result) == _native_failure(family)
    assert result["tool_execution"]["tool"] == family
    assert result["tool_execution"]["outcome"] == "error"
    assert "synthetic_thread_start_failure" not in repr(result)
    assert "rejected-private-marker" not in repr(result)


def _assert_drained(pools):
    for pool in pools:
        assert pool.pending_count == 0
        assert all(not thread.is_alive() for thread in pool._pool._threads)


@pytest.mark.parametrize(("family", "pool_name"), _FAMILIES)
def test_first_worker_start_failure_closes_only_its_native_family(monkeypatch, family, pool_name):
    pools = {name: _pool(f"native-start-failure-{name}") for _family, name in _FAMILIES}
    for name, pool in pools.items():
        monkeypatch.setattr(mcp_read_executor, name, pool)
    failed_pool = pools[pool_name]
    prefix = failed_pool._pool._thread_name_prefix
    calls = []
    tools = _native_tools(calls)
    original_start = threading.Thread.start
    faulted = []
    starts = []
    owned_pools = list(pools.values())

    def start(thread, *args, **kwargs):
        if thread.name.startswith(prefix + "_"):
            starts.append(thread.name)
            if not faulted:
                faulted.append(thread.name)
                raise RuntimeError("synthetic_thread_start_failure")
        return original_start(thread, *args, **kwargs)

    async def scenario():
        rejected = await tools[family].run({"marker": "rejected-private-marker"})
        immediately = (failed_pool.pending_count, list(calls))
        late = [await tools[family].run({"marker": f"late-{index}"}) for index in range(2)]
        independent = {}
        for other, _name in _FAMILIES:
            if other != family:
                independent[other] = await tools[other].run({"marker": "healthy-family"})
        replacement = _pool("native-recreated-family")
        owned_pools.append(replacement)
        monkeypatch.setattr(mcp_read_executor, pool_name, replacement)
        recreated = await tools[family].run({"marker": "recreated-family"})
        return rejected, immediately, late, independent, recreated

    try:
        with monkeypatch.context() as fault:
            fault.setattr(threading.Thread, "start", start)
            rejected, immediately, late, independent, recreated = asyncio.run(scenario())
    finally:
        for pool in owned_pools:
            pool.close()

    assert len(faulted) == 1
    assert starts == [prefix + "_0"]
    _assert_native_failure(rejected, family)
    assert immediately == (0, [])
    for result in late:
        _assert_native_failure(result, family)
    for other, result in independent.items():
        assert _without_trace(result) == {"ok": True, "family": other, "marker": "healthy-family"}
        assert result["tool_execution"]["outcome"] == "ok"
    assert _without_trace(recreated) == {"ok": True, "family": family, "marker": "recreated-family"}
    assert recreated["tool_execution"]["outcome"] == "ok"
    assert calls == [
        *((other, "healthy-family") for other, _name in _FAMILIES if other != family),
        (family, "recreated-family"),
    ]
    _assert_drained(owned_pools)


async def _outcome(pool, operation):
    try:
        return {"result": await pool.run(operation)}
    except ListingExecutionError as error:
        return {
            "error": error.code,
            "stage": error.stage,
            "adapter_started": error.adapter_started,
            "adapter_may_continue": error.adapter_may_continue,
        }


def test_second_worker_start_failure_keeps_accepted_sibling_and_refuses_late_work(monkeypatch):
    prefix = "direct-second-worker-start-failure"
    pool = _pool(prefix, workers=2)
    replacement = _pool("direct-recreated-family")
    release = threading.Event()
    calls = []
    starts = []
    faulted = []
    original_start = threading.Thread.start

    def start(thread, *args, **kwargs):
        if thread.name.startswith(prefix + "_"):
            starts.append(thread.name)
            if thread.name == prefix + "_1" and not faulted:
                faulted.append(thread.name)
                raise RuntimeError("synthetic_thread_start_failure")
        return original_start(thread, *args, **kwargs)

    def record(marker):
        calls.append(marker)
        return {"ok": True, "marker": marker}

    async def scenario():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def accepted():
            calls.append("accepted-sibling")
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(10), "accepted sibling was not released"
            return {"ok": True, "marker": "accepted-result"}

        first = asyncio.create_task(pool.run(accepted))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            before = pool.pending_count
            rejected = await _outcome(pool, lambda: record("rejected"))
            after = pool.pending_count
            late = [await _outcome(pool, lambda index=index: record(f"late-{index}")) for index in range(3)]
            during = (pool.pending_count, first.done())
            recreated = await _outcome(replacement, lambda: record("recreated-family"))
            release.set()
            accepted_result = await asyncio.wait_for(first, timeout=5)
            return before, rejected, after, late, during, recreated, accepted_result, pool.pending_count
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)

    try:
        with monkeypatch.context() as fault:
            fault.setattr(threading.Thread, "start", start)
            before, rejected, after, late, during, recreated, accepted_result, after_sibling = asyncio.run(scenario())
    finally:
        release.set()
        pool.close()
        replacement.close()

    assert faulted == [prefix + "_1"]
    assert starts == [prefix + "_0", prefix + "_1"]
    assert before == after == 1
    assert rejected == _UNAVAILABLE
    assert late == [_UNAVAILABLE] * 3
    assert during == (1, False)
    assert recreated == {"result": {"ok": True, "marker": "recreated-family"}}
    assert accepted_result == {"ok": True, "marker": "accepted-result"}
    assert after_sibling == 0
    assert calls == ["accepted-sibling", "recreated-family"]
    _assert_drained([pool, replacement])
