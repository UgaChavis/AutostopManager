from __future__ import annotations

import asyncio
import json
import threading

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from autostop_manager import mcp_read_executor, mcp_tools, oem_candidate_web_evidence, work_pricing_research
from autostop_manager.listing_executor import BoundedListingExecutor
from autostop_manager.listing_executor import ListingExecutionError


ROOTS = ("collect_work_price_evidence", "estimate_repair_work_cost", "verify_oem_candidates_web")
INPUTS = {
    ROOTS[0]: {"work_items": ["synthetic labor"], "max_queries": 1},
    ROOTS[1]: {"work_items": ["synthetic labor"], "use_internal_experience": False},
    ROOTS[2]: {"candidates": [{"part_number": "12345", "brand": "DEMO"}], "live_search": True},
}


def _native(monkeypatch):
    class Inert:
        def __init__(self, *args, **kwargs):
            pass

    class Control(Inert):
        def request(self, *args, **kwargs):
            return {"ok": True, "data": {"synthetic": True}}

    for name in (
        "StoreIntegration",
        "StoreApiClient",
        "StoreOwnerApiClient",
        "StoreQuoteConductor",
        "StoreQuoteOwnerApi",
    ):
        monkeypatch.setattr(mcp_tools, name, Inert)
    for name in (
        "get_store_api_url",
        "get_store_read_token",
        "get_store_manage_token",
        "get_store_quote_token",
        "get_store_owner_token",
    ):
        monkeypatch.setattr(mcp_tools, name, lambda: "")
    monkeypatch.setattr(mcp_tools, "AutomationControlClient", Control)
    server = FastMCP("synthetic-labor-native")
    mcp_tools.register_manager_tools(
        server, store=Inert(), store_client=Inert(), include_tools={*ROOTS, "manager_automations"}
    )
    return server._tool_manager._tools


@pytest.mark.parametrize("tool_name", ROOTS)
def test_real_native_labor_roots_keep_control_and_loop_responsive(monkeypatch, tool_name):
    tools = _native(monkeypatch)
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=2)
    monkeypatch.setattr(mcp_read_executor, "_WEB_EXECUTOR", pool)
    release = threading.Event()
    progressed = []
    backend_threads = []
    loop = None
    entered = asyncio.Event()

    def backend(*args, **kwargs):
        backend_threads.append(threading.get_ident())
        loop.call_soon_threadsafe(entered.set)
        progressed.append(release.wait(0.3))
        return {"ok": True, "results": []}

    monkeypatch.setattr(work_pricing_research, "_ddg_search", backend)
    monkeypatch.setattr(oem_candidate_web_evidence, "research_part_public_evidence", backend)

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()

        async def control():
            await entered.wait()
            assert (await tools["manager_automations"].run({"operation": "templates"}))["ok"]
            release.set()

        control_task = asyncio.create_task(control())
        try:
            assert (await tools[tool_name].run(INPUTS[tool_name]))["ok"]
            await asyncio.wait_for(control_task, 1)
            assert progressed and all(progressed)
            assert all(worker != threading.get_ident() for worker in backend_threads)
            assert tools[tool_name].is_async
        finally:
            release.set()
            await asyncio.gather(control_task, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        release.set()
        pool.close()


@pytest.mark.parametrize("max_queries", [True, False, 1.0, "1", "true", "false", "null", None])
def test_native_collector_rejects_non_json_integer_before_adapter(monkeypatch, max_queries):
    tools = _native(monkeypatch)
    calls = []
    monkeypatch.setattr(
        work_pricing_research, "_ddg_search", lambda *args, **kwargs: calls.append(1) or {"results": []}
    )
    with pytest.raises(ToolError):
        asyncio.run(tools[ROOTS[0]].run({**INPUTS[ROOTS[0]], "max_queries": max_queries}))
    assert calls == []


@pytest.mark.parametrize("max_queries", [1, 2, 3, 4])
def test_native_collector_keeps_integer_and_encoded_nested_inputs(monkeypatch, max_queries):
    tools = _native(monkeypatch)
    calls = []
    monkeypatch.setattr(
        work_pricing_research, "_ddg_search", lambda *args, **kwargs: calls.append(1) or {"results": []}
    )
    result = asyncio.run(
        tools[ROOTS[0]].run(
            {
                "work_items": json.dumps(["synthetic labor"]),
                "vehicle_context": json.dumps({"make": "DEMO"}),
                "max_queries": max_queries,
            }
        )
    )
    assert result["ok"]
    assert result["data"]["vehicle_context"] == {"make": "DEMO"}
    assert 1 <= len(calls) <= max_queries
    assert tools[ROOTS[0]].parameters["properties"]["max_queries"]["type"] == "integer"


def _assert_failure(result, tool_name, code, *, started):
    assert result["ok"] is False
    assert result["error"] == code
    assert result["adapter_started"] is started
    assert result["execution"] == {"network_calls": None if started else 0, "attempts": None if started else []}
    assert result["retryable"] is False
    assert result["retry_attempted"] is False
    assert result["tool_execution"]["outcome"] == "error"
    if tool_name == ROOTS[0]:
        assert result["schema_version"] == "autostop.automotive-result.v1"
        assert result["tool_id"] == "manager.collect_work_price_evidence"
        assert result["data"] == {} and result["evidence"] == []
    else:
        assert "schema_version" not in result
        if tool_name == ROOTS[2]:
            assert result["schema"] == "OemCandidatePublicEvidenceV1"
            assert result["input_context"] == {} and result["candidate_evidence"] == []
        else:
            assert result["read_only"] and not result["crm_write_allowed"]


@pytest.mark.parametrize("tool_name", ROOTS)
@pytest.mark.parametrize(
    ("code", "stage", "started", "continues"),
    [
        ("provider_busy", "admission", False, False),
        ("provider_executor_unavailable", "admission", False, False),
        ("provider_wait_timeout", "worker_wait", True, True),
        ("provider_execution_failed", "worker_wait", True, False),
    ],
)
def test_native_failure_preserves_each_output_family_and_identical_channels(
    monkeypatch, tool_name, code, stage, started, continues
):
    class FailedPool:
        async def run(self, operation):
            raise ListingExecutionError(code, stage=stage, adapter_started=started, adapter_may_continue=continues)

    tools = _native(monkeypatch)
    monkeypatch.setattr(mcp_read_executor, "_WEB_EXECUTOR", FailedPool())
    arguments = {**INPUTS[tool_name]}
    if tool_name == ROOTS[2]:
        arguments["requested_part"] = "UNVALIDATED_PRIVATE_MARKER"
    else:
        arguments["city"] = "UNVALIDATED_PRIVATE_MARKER"
    content, result = asyncio.run(tools[tool_name].run(arguments, convert_result=True))
    _assert_failure(result, tool_name, code, started=started)
    assert json.loads(content[0].text) == result
    assert "UNVALIDATED_PRIVATE_MARKER" not in json.dumps(result)
    assert result["stage"] == stage and result["adapter_may_continue"] is continues


@pytest.mark.parametrize("tool_name", ROOTS)
@pytest.mark.parametrize("mode", ["cancel", "timeout"])
def test_native_labor_running_abandonment_keeps_real_admission(monkeypatch, tool_name, mode):
    tools = _native(monkeypatch)
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=0.1)
    monkeypatch.setattr(mcp_read_executor, "_WEB_EXECUTOR", pool)
    release = threading.Event()
    entered = asyncio.Event()
    calls = []
    loop = None

    def backend(*args, **kwargs):
        calls.append(1)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        return {"ok": True, "results": []}

    monkeypatch.setattr(work_pricing_research, "_ddg_search", backend)
    monkeypatch.setattr(oem_candidate_web_evidence, "research_part_public_evidence", backend)

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(tools[tool_name].run(INPUTS[tool_name]))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if mode == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                result = await asyncio.wait_for(task, 1)
                _assert_failure(result, tool_name, "provider_wait_timeout", started=True)
                assert result["adapter_may_continue"] is True
            assert pool.pending_count == 1
            _assert_failure(await tools[tool_name].run(INPUTS[tool_name]), tool_name, "provider_busy", started=False)
            assert calls == [1]
            assert (await tools["manager_automations"].run({"operation": "templates"}))["ok"]
            release.set()
            async with asyncio.timeout(2):
                while pool.pending_count:
                    await asyncio.sleep(0.001)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        release.set()
        pool.close()


@pytest.mark.parametrize("tool_name", ROOTS)
@pytest.mark.parametrize("mode", ["cancel", "timeout"])
def test_native_queued_abandoned_labor_never_starts_adapter(monkeypatch, tool_name, mode):
    tools = _native(monkeypatch)
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=0.1)
    monkeypatch.setattr(mcp_read_executor, "_WEB_EXECUTOR", pool)
    release = threading.Event()
    entered = asyncio.Event()
    calls = []
    monkeypatch.setattr(
        work_pricing_research, "_ddg_search", lambda *args, **kwargs: calls.append(1) or {"results": []}
    )
    monkeypatch.setattr(
        oem_candidate_web_evidence,
        "research_part_public_evidence",
        lambda **kwargs: calls.append(1) or {"ok": True, "results": []},
    )

    async def run():
        loop = asyncio.get_running_loop()

        def blocker():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3)
            return {"ok": True}

        first = asyncio.create_task(pool.run(blocker))
        await asyncio.wait_for(entered.wait(), 1)
        queued = asyncio.create_task(tools[tool_name].run(INPUTS[tool_name]))
        try:
            async with asyncio.timeout(1):
                while pool.pending_count != 2:
                    await asyncio.sleep(0)
            if mode == "cancel":
                queued.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await queued
            else:
                result = await asyncio.wait_for(queued, 1)
                _assert_failure(result, tool_name, "provider_wait_timeout", started=False)
                assert result["stage"] == "admission"
            assert pool.pending_count == 2
            _assert_failure(await tools[tool_name].run(INPUTS[tool_name]), tool_name, "provider_busy", started=False)
            assert calls == []
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            async with asyncio.timeout(1):
                while pool.pending_count:
                    await asyncio.sleep(0.001)
            assert calls == []
        finally:
            release.set()
            await asyncio.gather(first, queued, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        release.set()
        pool.close()


@pytest.mark.parametrize("starts_before_deadline", [False, True])
def test_collector_original_budget_includes_queue_wait(monkeypatch, starts_before_deadline):
    tools = _native(monkeypatch)
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=2)
    monkeypatch.setattr(mcp_read_executor, "_WEB_EXECUTOR", pool)
    release = threading.Event()
    entered = asyncio.Event()
    observed_timeouts = []
    monkeypatch.setattr(
        work_pricing_research,
        "_ddg_search",
        lambda *args, timeout_seconds: observed_timeouts.append(timeout_seconds) or {"results": []},
    )

    async def run():
        loop = asyncio.get_running_loop()

        def blocker():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3)
            return {"ok": True}

        first = asyncio.create_task(pool.run(blocker))
        await asyncio.wait_for(entered.wait(), 1)
        budget = 0.5 if starts_before_deadline else 0.1
        queued_at = loop.time()
        queued = asyncio.create_task(tools[ROOTS[0]].run({**INPUTS[ROOTS[0]], "deadline_seconds": budget}))
        try:
            async with asyncio.timeout(1):
                while pool.pending_count != 2:
                    await asyncio.sleep(0)
            if starts_before_deadline:
                await asyncio.sleep(0.05)
                released_at = loop.time()
                release.set()
            result = await asyncio.wait_for(queued, 1)
            if starts_before_deadline:
                assert result["ok"] and len(observed_timeouts) == 1
                assert 0 < observed_timeouts[0] <= budget - (released_at - queued_at) + 0.02
                assert result["data"]["deadline_seconds"] == budget
            else:
                _assert_failure(result, ROOTS[0], "provider_wait_timeout", started=False)
                assert result["stage"] == "admission"
                assert loop.time() - queued_at < 0.6
                assert pool.pending_count == 2 and observed_timeouts == []
            release.set()
            await first
            async with asyncio.timeout(1):
                while pool.pending_count:
                    await asyncio.sleep(0.001)
            assert len(observed_timeouts) == int(starts_before_deadline)
        finally:
            release.set()
            await asyncio.gather(first, queued, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        release.set()
        pool.close()
