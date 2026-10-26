"""Native batch deadlines reject booleans before entering the decoder queue."""

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
from typing import get_type_hints

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
import pytest

from autostop_manager import automotive_identity as identity
from autostop_manager import automotive_mcp, config, mcp_telemetry, mcp_tools
from autostop_manager.automotive_contracts import result


class Inert:
    def __init__(self, *_args, **_kwargs):
        pass


@pytest.fixture(autouse=True)
def batch_environment(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("batch deadline tests must not acquire external or default runtime state")

    for owner, name in (
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (sqlite3, "connect"),
        (subprocess, "Popen"),
        (threading.Thread, "start"),
        (config, "get_db_path"),
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
    monkeypatch.setattr(mcp_tools, "StoreState", denied)
    monkeypatch.setattr(mcp_telemetry.LOGGER, "disabled", True)
    calls = {"entrypoint": 0, "admission": 0, "decoder": []}

    class InlinePool:
        async def run(self, operation):
            calls["admission"] += 1
            return operation()

    def decoder(identifier, **_options):
        calls["decoder"].append(identifier)
        return result("decode_vin_vpic", "success", {"identifier": identifier})

    original = identity.decode_vehicle_batch_async

    @wraps(original)
    async def tracked(*args, **kwargs):
        calls["entrypoint"] += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(identity, "_BATCH_READ_EXECUTOR", InlinePool())
    monkeypatch.setattr(identity, "_selected_decoders", lambda: {"decode_vin_vpic": decoder})
    monkeypatch.setattr(automotive_mcp, "decode_vehicle_batch_async", tracked)
    return calls


@pytest.fixture(params=["bare", "manager"])
def native(request, batch_environment):
    server = FastMCP("isolated-native-batch-deadline")

    def register():
        if request.param == "bare":
            automotive_mcp.register_automotive_tools(server)
        else:
            mcp_tools.register_manager_tools(
                server, store=Inert(), store_client=Inert(), include_tools={"decode_vehicle_batch"}
            )

    register()
    return server, batch_environment, register


@pytest.fixture(params=[False, True], ids=["object-items", "encoded-items"])
def arguments(request):
    def build(**overrides):
        supplied = {
            "items": ["DEMO-A", {"identifier": "DEMO-B", "context": {"nested": [True, None]}}],
            "decoder": "decode_vin_vpic",
            **overrides,
        }
        if request.param:
            supplied["items"] = json.dumps(supplied["items"])
        return supplied

    return build


def invoke(server, supplied):
    original = deepcopy(supplied)
    try:
        content, response = asyncio.run(server.call_tool("decode_vehicle_batch", supplied))
        assert json.loads(content[0].text) == response
        return response
    finally:
        assert supplied == original


@pytest.mark.parametrize("deadline", [True, False])
def test_native_boolean_deadline_fails_before_entrypoint_admission_or_decoder(native, arguments, deadline):
    server, calls, _register = native
    with pytest.raises(ToolError, match="deadline_seconds"):
        invoke(server, arguments(deadline_seconds=deadline))
    assert calls == {"entrypoint": 0, "admission": 0, "decoder": []}


@pytest.mark.parametrize("deadline", [1, 1.5, "1.5", 120])
def test_native_valid_deadlines_preserve_coercion_ordered_items_and_input(native, arguments, deadline):
    server, calls, _register = native
    response = invoke(server, arguments(deadline_seconds=deadline))
    assert response["outcome"] == "success" and response["data"]["deadline_seconds"] == float(deadline)
    assert [row["item_index"] for row in response["data"]["items"]] == [0, 1]
    assert [row["result"]["data"]["identifier"] for row in response["data"]["items"]] == ["DEMO-A", "DEMO-B"]
    assert response["execution"] == {"network_calls": 0, "attempts": []}
    assert calls == {"entrypoint": 1, "admission": 2, "decoder": ["DEMO-A", "DEMO-B"]}


def test_native_omitted_deadline_keeps_default_thirty_seconds(native, arguments):
    server, calls, _register = native
    response = invoke(server, arguments())
    assert response["outcome"] == "success" and response["data"]["deadline_seconds"] == 30
    assert calls == {"entrypoint": 1, "admission": 2, "decoder": ["DEMO-A", "DEMO-B"]}


@pytest.mark.parametrize("deadline", [0, 120.1])
def test_numeric_out_of_bounds_keeps_existing_domain_invalid_envelope(native, arguments, deadline):
    server, calls, _register = native
    response = invoke(server, arguments(deadline_seconds=deadline))
    assert response["ok"] is False and response["outcome"] == "invalid_input"
    assert response["data"] == {} and response["missing_fields"] == ["items_or_deadline"]
    assert response["execution"] == {"network_calls": 0, "attempts": []}
    assert calls == {"entrypoint": 1, "admission": 0, "decoder": []}


def test_minimum_numeric_budget_is_accepted_without_rounding_up(native, arguments):
    server, calls, _register = native
    response = invoke(server, arguments(items=[], deadline_seconds=0.1))
    assert response["outcome"] == "success" and response["data"]["deadline_seconds"] == 0.1
    assert calls == {"entrypoint": 1, "admission": 0, "decoder": []}


@pytest.mark.parametrize("deadline", [None, "true", "1,5"])
def test_other_native_wrong_types_keep_sdk_rejection(native, arguments, deadline):
    server, calls, _register = native
    with pytest.raises(ToolError, match="deadline_seconds"):
        invoke(server, arguments(deadline_seconds=deadline))
    assert calls == {"entrypoint": 0, "admission": 0, "decoder": []}


def test_native_schema_annotations_and_direct_signatures_are_preserved(native, arguments):
    server, _calls, _register = native
    tool = server._tool_manager.get_tool("decode_vehicle_batch")
    assert tool.is_async
    assert tool.parameters["properties"]["deadline_seconds"] == {
        "default": 30,
        "title": "Deadline Seconds",
        "type": "number",
    }
    assert tool.fn_metadata.arg_model.model_json_schema(by_alias=True) == tool.parameters
    assert tool.annotations == ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
    for helper in (identity.decode_vehicle_batch, identity.decode_vehicle_batch_async):
        assert get_type_hints(helper)["deadline_seconds"] is float
        assert inspect.signature(helper).parameters["deadline_seconds"].default == 30


def test_repeated_registration_keeps_metadata_and_boolean_rejection(native, arguments):
    server, calls, register = native
    tool = server._tool_manager.get_tool("decode_vehicle_batch")
    schema = deepcopy(tool.parameters)
    metadata = list(tool.fn_metadata.arg_model.model_fields["deadline_seconds"].metadata)
    annotations = deepcopy(tool.annotations)
    register()
    assert tool.parameters == schema and tool.annotations == annotations
    assert tool.fn_metadata.arg_model.model_fields["deadline_seconds"].metadata == metadata
    with pytest.raises(ToolError, match="deadline_seconds"):
        invoke(server, arguments(deadline_seconds=True))
    assert calls == {"entrypoint": 0, "admission": 0, "decoder": []}


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("deadline", [True, False])
def test_direct_helpers_keep_domain_boolean_rejection(batch_environment, mode, deadline):
    if mode == "sync":
        response = identity.decode_vehicle_batch(["DEMO-A"], "decode_vin_vpic", deadline)
    else:
        response = asyncio.run(identity.decode_vehicle_batch_async(["DEMO-A"], "decode_vin_vpic", deadline))
    assert response["outcome"] == "invalid_input" and response["data"] == {}
    assert response["execution"] == {"network_calls": 0, "attempts": []}
    assert batch_environment == {"entrypoint": 0, "admission": 0, "decoder": []}


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("deadline", ["1.5", "1,5"])
def test_direct_helpers_keep_supported_numeric_strings(batch_environment, mode, deadline):
    if mode == "sync":
        response = identity.decode_vehicle_batch(["DEMO-A"], "decode_vin_vpic", deadline)
    else:
        response = asyncio.run(identity.decode_vehicle_batch_async(["DEMO-A"], "decode_vin_vpic", deadline))
    assert response["outcome"] == "success" and response["data"]["deadline_seconds"] == 1.5
    assert batch_environment["decoder"] == ["DEMO-A"]
    assert batch_environment["admission"] == int(mode == "async")
