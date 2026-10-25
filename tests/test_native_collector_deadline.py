"""Native deadline validation keeps booleans out of the numeric read budget."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from functools import wraps
import inspect
import json
import socket
import sqlite3
import subprocess
import threading
from typing import Any, get_type_hints

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from autostop_manager import automotive_labor, automotive_mcp, mcp_read_executor, mcp_tools, work_pricing_research


class Inert:
    def __init__(self, *_args, **_kwargs):
        pass


@pytest.fixture(autouse=True)
def isolated_collector(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("supplied-aggregate deadline test attempted external acquisition")

    for owner, name in (
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (sqlite3, "connect"),
        (subprocess, "Popen"),
        (threading.Thread, "start"),
        (work_pricing_research, "_ddg_search"),
    ):
        monkeypatch.setattr(owner, name, denied)
    for name in (
        "StoreIntegration",
        "StoreApiClient",
        "StoreOwnerApiClient",
        "StoreQuoteConductor",
        "StoreQuoteOwnerApi",
        "AutomationControlClient",
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


@pytest.fixture(params=["bare", "manager"])
def native(request, monkeypatch):
    calls = {"backend": 0, "pool": 0}
    backend = automotive_labor.collect_work_price_evidence

    @wraps(backend)
    def tracked_backend(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls["backend"] += 1
        return backend(*args, **kwargs)

    class InlinePool:
        async def run(self, operation):
            calls["pool"] += 1
            return operation()

    monkeypatch.setattr(automotive_mcp, "collect_work_price_evidence", tracked_backend)
    monkeypatch.setattr(mcp_read_executor, "_WEB_EXECUTOR", InlinePool())
    server = FastMCP("native-collector-deadline")
    if request.param == "bare":
        automotive_mcp.register_automotive_tools(server)
    else:
        mcp_tools.register_manager_tools(
            server, store=Inert(), store_client=Inert(), include_tools={"collect_work_price_evidence"}
        )
    return server, calls, request.param


def arguments(**overrides):
    return {
        "work_items": ["DEMO"],
        "vehicle_context": {"make": "DEMO"},
        "sources": ["provided_aggregate"],
        "aggregate_evidence": {},
        "max_queries": 1,
        **overrides,
    }


def invoke(server, supplied):
    original = deepcopy(supplied)
    try:
        return asyncio.run(server.call_tool("collect_work_price_evidence", supplied))
    finally:
        assert supplied == original


@pytest.mark.parametrize("deadline", [True, False, "true", "false"])
def test_native_boolean_deadlines_fail_before_backend_or_admission(native, deadline):
    server, calls, _route = native
    with pytest.raises(ToolError, match="deadline_seconds"):
        invoke(server, arguments(deadline_seconds=deadline))
    assert calls == {"backend": 0, "pool": 0}


@pytest.mark.parametrize("deadline", [1, 1.5, "1.5", 0.1, 60])
def test_native_numeric_deadlines_preserve_supported_coercion_and_nested_json(native, deadline):
    server, calls, route = native
    supplied = arguments(deadline_seconds=deadline)
    if isinstance(deadline, str):
        for name in ("work_items", "vehicle_context", "sources", "aggregate_evidence"):
            supplied[name] = json.dumps(supplied[name])
    content, response = invoke(server, supplied)
    assert json.loads(content[0].text) == response
    assert response["outcome"] == "success" and response["ok"] is True
    assert response["data"]["deadline_seconds"] == float(deadline)
    assert response["data"]["provided_aggregate"] == {}
    assert response["execution"] == {"network_calls": 0, "attempts": []}
    assert calls == {"backend": 1, "pool": int(route == "manager")}


def test_omitted_native_deadline_preserves_default_budget(native):
    server, calls, route = native
    _content, response = invoke(server, arguments())
    assert response["outcome"] == "success" and response["data"]["deadline_seconds"] == 20
    assert calls == {"backend": 1, "pool": int(route == "manager")}


@pytest.mark.parametrize("deadline", [None, [], "1,5"])
def test_other_native_wrong_types_keep_sdk_validation_errors(native, deadline):
    server, calls, _route = native
    with pytest.raises(ToolError, match="deadline_seconds"):
        invoke(server, arguments(deadline_seconds=deadline))
    assert calls == {"backend": 0, "pool": 0}


@pytest.mark.parametrize("deadline", [0, 61])
def test_invalid_numeric_budgets_keep_domain_error_and_admission_order(native, deadline):
    server, calls, route = native
    content, response = invoke(server, arguments(deadline_seconds=deadline))
    assert json.loads(content[0].text) == response
    assert response["outcome"] == "invalid_input" and response["ok"] is False
    assert response["data"] == {} and response["missing_fields"] == ["work_items_sources_budget"]
    assert response["execution"] == {"network_calls": 0, "attempts": []}
    assert calls == {"backend": 1, "pool": int(route == "manager")}


def test_native_schema_and_public_python_signature_remain_numeric(native):
    server, _calls, _route = native
    tool = server._tool_manager.get_tool("collect_work_price_evidence")
    assert tool.parameters["properties"]["deadline_seconds"] == {
        "default": 20,
        "title": "Deadline Seconds",
        "type": "number",
    }
    assert tool.fn_metadata.arg_model.model_json_schema(by_alias=True) == tool.parameters
    assert get_type_hints(automotive_labor.collect_work_price_evidence)["deadline_seconds"] is float
    assert inspect.signature(automotive_labor.collect_work_price_evidence).parameters["deadline_seconds"].default == 20


@pytest.mark.parametrize(
    "deadline,expected", [(True, "invalid_input"), (False, "invalid_input"), (1, "success"), ("1,5", "success")]
)
def test_direct_helper_preserves_boolean_rejection_and_comma_numeric_strings(deadline, expected):
    response = automotive_labor.collect_work_price_evidence(**arguments(deadline_seconds=deadline))
    assert response["outcome"] == expected
    assert response["execution"] == {"network_calls": 0, "attempts": []}
    if expected == "invalid_input":
        assert response["missing_fields"] == ["work_items_sources_budget"]
    else:
        assert response["data"]["deadline_seconds"] == float(str(deadline).replace(",", "."))
