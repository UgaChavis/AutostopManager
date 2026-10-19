"""Use original Store registrations and HTTP adapter with only synthetic responses."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from io import BytesIO
import json
import os
import socket
import threading
from urllib.parse import parse_qs, urlsplit

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import mcp_read_executor, mcp_store_executor, mcp_tools, store_api
from autostop_manager.listing_executor import BoundedListingExecutor
from autostop_manager.storage import StoreState
from autostop_manager.store_api import StoreApiClient


BEFORE = "2026-10-19T10:00:00+00:00"
AFTER = "2026-10-19T10:01:00+00:00"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    def denied(*_args, **_kwargs):
        raise AssertionError("Native Store regression attempted real network")

    # Permit asyncio's internal Windows socketpair, while HTTP/DNS acquisition
    # and the Store transport remain denied unless this test replaces them.
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(store_api, "urlopen", denied)
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", os.devnull)
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "manager.sqlite3"))
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "j1"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "vin"))
    for name in (
        "get_store_api_url",
        "get_store_read_token",
        "get_store_manage_token",
        "get_store_quote_token",
        "get_store_owner_token",
    ):
        monkeypatch.setattr(mcp_tools, name, lambda: "")


def envelope(item=None):
    return {
        "ok": True,
        "format": "store_agent_v1",
        "status": "completed",
        "summary": {},
        "items": [] if item is None else [item],
        "changes": [],
        "page": {},
        "warnings": [],
        "meta": {},
    }


def register(tmp_path, names):
    client = StoreApiClient(
        api_url="https://store.synthetic.example/internal/agent/v1",
        read_token="synthetic-read",
        manage_token="synthetic-manage",
        quote_token="synthetic-quote",
        max_read_attempts=1,
    )
    server = FastMCP("synthetic-native-store")
    mcp_tools.register_manager_tools(
        server, store=StoreState(tmp_path / "state.sqlite3"), store_client=client, include_tools=names
    )
    return server._tool_manager._tools


async def drained(pool):
    async with asyncio.timeout(3):
        while pool.pending_count:
            await asyncio.sleep(0)


def test_original_store_registrations_become_async_without_changing_sdk_contract(monkeypatch, tmp_path):
    instrument = mcp_tools.instrument_manager_tools
    captured = {}

    def observe(server):
        for name, tool in server._tool_manager._tools.items():
            captured[name] = (tool.parameters, deepcopy(tool.parameters), tool.fn_metadata, tool.annotations)
        instrument(server)

    monkeypatch.setattr(mcp_tools, "instrument_manager_tools", observe)
    names = mcp_store_executor.STORE_BLOCKING_TOOLS
    tools = register(tmp_path, names)
    assert set(tools) == names
    for name, tool in tools.items():
        parameters, original_schema, metadata, annotations = captured[name]
        assert tool.is_async is True
        assert tool.parameters is parameters and tool.parameters == original_schema
        assert tool.fn_metadata is metadata
        assert tool.annotations is annotations
    assert tools["store_management_action"].annotations.idempotentHint is True
    assert tools["store_quote_conductor"].annotations.idempotentHint is False
    assert not (tmp_path / "state.sqlite3").exists()


def test_original_store_http_read_leaves_native_control_responsive(monkeypatch, tmp_path):
    read_pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=5)
    control_pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=5)
    monkeypatch.setattr(mcp_store_executor, "_READ_EXECUTOR", read_pool)
    monkeypatch.setattr(mcp_read_executor, "_CONTROL_EXECUTOR", control_pool)
    release = threading.Event()
    entered = asyncio.Event()
    calls = []
    loop = None

    class Control:
        def __init__(self, **_kwargs):
            pass

        def request(self, operation, payload, **_kwargs):
            assert operation == "templates"
            return {"ok": True, "data": {"synthetic_control": True}}

    monkeypatch.setattr(mcp_tools, "AutomationControlClient", Control)

    def urlopen(request, timeout):
        calls.append((request.get_method(), threading.get_ident()))
        parsed = urlsplit(request.full_url)
        assert parsed.netloc == "store.synthetic.example"
        assert parsed.path == "/internal/agent/v1/search"
        assert parse_qs(parsed.query)["query"] == ["synthetic"]
        assert timeout > 0
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(1), "Store HTTP read blocked the MCP control request"
        return BytesIO(json.dumps(envelope({"entity": "store_order", "id": "order-1"})).encode())

    monkeypatch.setattr(store_api, "urlopen", urlopen)
    tools = register(tmp_path, {"store_search", "manager_automations"})

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()

        async def control():
            await entered.wait()
            result = await tools["manager_automations"].run({"operation": "templates"})
            assert result["synthetic_control"] is True
            release.set()

        task = asyncio.create_task(control())
        try:
            content, result = await tools["store_search"].run(
                {"entity": "store_order", "query": "synthetic"}, convert_result=True
            )
            await asyncio.wait_for(task, 1)
            assert result["ok"] is True
            assert result["items"] == [{"entity": "store_order", "id": "order-1"}]
            assert json.loads(content[0].text) == result
            assert calls == [("GET", calls[0][1])]
            assert calls[0][1] != threading.get_ident()
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        release.set()
        read_pool.close()
        control_pool.close()


@pytest.mark.parametrize("abandonment", ["cancel", "timeout"])
def test_original_management_write_finishes_one_post_and_readback_after_caller_leaves(
    monkeypatch, tmp_path, abandonment
):
    pool = BoundedListingExecutor(
        max_workers=1, max_waiting_calls=0, timeout_seconds=0.2 if abandonment == "timeout" else 5
    )
    monkeypatch.setattr(mcp_store_executor, "_STATEFUL_EXECUTOR", pool)
    release = threading.Event()
    posted = asyncio.Event()
    calls = []
    loop = None
    before = {"entity": "store_batch", "id": "batch-1", "storage_location": "A-1", "updated_at": BEFORE}
    after = {**before, "storage_location": "B-2", "updated_at": AFTER}
    arguments = {
        "domain": "store_batch",
        "action": "set_batch_storage_location",
        "target_id": "batch-1",
        "planned_changes": {"storage_location": "B-2"},
        "owner_intent": "Synthetic owner instruction for the exact batch",
        "expected_updated_at": BEFORE,
        "idempotency_key": "synthetic-store-offload-write",
        "correlation_id": "contract:synthetic-store-offload-write",
        "mode": "apply",
    }
    original_arguments = deepcopy(arguments)

    def urlopen(request, timeout):
        parsed = urlsplit(request.full_url)
        assert parsed.netloc == "store.synthetic.example"
        assert timeout > 0
        method = request.get_method()
        body = json.loads(request.data) if request.data else None
        calls.append((method, body))
        if method == "POST":
            assert parsed.path == "/internal/agent/v1/actions/set_batch_storage_location"
            loop.call_soon_threadsafe(posted.set)
            assert release.wait(3)
            payload = envelope()
        else:
            assert method == "GET"
            assert parsed.path == "/internal/agent/v1/entities/store_batch/batch-1"
            payload = envelope(before if len(calls) == 1 else after)
        return BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(store_api, "urlopen", urlopen)
    tool = register(tmp_path, {"store_management_action"})["store_management_action"]

    async def run():
        nonlocal loop
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(tool.run(arguments))
        try:
            await asyncio.wait_for(posted.wait(), 1)
            if abandonment == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                result = await task
                assert result["status"] == "compensating"
                assert result["meta"]["outcome_uncertain"] is True
                assert result["meta"]["readback_required"] is True
                assert result["meta"]["readback_verified"] is False
            assert pool.pending_count == 1
            refused = await tool.run(arguments)
            assert refused["summary"]["error_code"] == "store_executor_busy"
            assert refused["meta"]["request_dispatched"] is False
            assert [method for method, _body in calls] == ["GET", "POST"]
            release.set()
            await drained(pool)
            assert [method for method, _body in calls] == ["GET", "POST", "GET"]
            body = calls[1][1]
            for field in (
                "target_id",
                "planned_changes",
                "owner_intent",
                "expected_updated_at",
                "idempotency_key",
                "correlation_id",
                "mode",
            ):
                assert body[field] == original_arguments[field]
            assert arguments == original_arguments
            assert not (tmp_path / "state.sqlite3").exists()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        release.set()
        pool.close()
