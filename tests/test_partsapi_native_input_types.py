from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import threading

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from autostop_manager import catalog_clients, mcp_read_executor, mcp_tools
from autostop_manager.mcp_contract import mcp_schema_fingerprint


ID_FIELDS = (
    "article_id",
    "supplier_id",
    "lang_id",
    "model_id",
    "motor_id",
    "top_category_id",
    "sub_category_id",
    "car_id",
)


class Inert:
    def __init__(self, *_args, **_kwargs):
        pass


@pytest.fixture(autouse=True)
def isolated_inputs(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("synthetic PartsAPI native test attempted external access")

    for owner, name in (
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (sqlite3, "connect"),
        (subprocess, "Popen"),
        (threading.Thread, "start"),
        (catalog_clients, "load_runtime_env"),
        (mcp_tools, "StoreState"),
        (mcp_tools, "StoreApiClient"),
    ):
        monkeypatch.setattr(owner, name, denied)
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


@pytest.fixture
def native(monkeypatch):
    calls = {"pool": 0, "adapter": []}

    class InlinePool:
        async def run(self, operation):
            calls["pool"] += 1
            return operation()

    def fake_adapter(**kwargs):
        calls["adapter"].append(kwargs)
        return {"ok": True, "outcome": "success", "received": kwargs, "attempt_count": 0}

    monkeypatch.setattr(mcp_read_executor, "_EXECUTOR", InlinePool())
    monkeypatch.setattr(mcp_tools, "partsapi_catalog_lookup", fake_adapter)

    def build(*, all_tools=False):
        server = FastMCP("partsapi-native-input-types")
        mcp_tools.register_manager_tools(
            server,
            store=Inert(),
            store_client=Inert(),
            include_tools=None if all_tools else {"partsapi_catalog_lookup"},
        )
        return server

    return build(), calls, build


def invoke(server, arguments):
    original = deepcopy(arguments)
    try:
        return asyncio.run(server.call_tool("partsapi_catalog_lookup", arguments))
    finally:
        assert arguments == original


@pytest.mark.parametrize("field", ID_FIELDS)
@pytest.mark.parametrize("value", [True, False], ids=["true", "false"])
def test_native_boolean_id_aliases_fail_before_handler_or_pool(native, field, value):
    server, calls, _build = native

    with pytest.raises(ToolError, match=field):
        invoke(server, {"operation": "article", field: value})

    assert calls == {"pool": 0, "adapter": []}


@pytest.mark.parametrize("encoded", [False, True], ids=["object", "encoded-object"])
@pytest.mark.parametrize("value", [True, False], ids=["true", "false"])
def test_native_boolean_provider_values_fail_before_dispatch(native, encoded, value):
    server, calls, _build = native
    parameters = {"carType": "PC", "makeId": value, "modelId": 42}
    supplied = json.dumps(parameters) if encoded else parameters

    with pytest.raises(ToolError, match="provider_parameters"):
        invoke(server, {"operation": "getCars", "provider_parameters": supplied})

    assert calls == {"pool": 0, "adapter": []}


@pytest.mark.parametrize("encoded", [False, True], ids=["object", "encoded-object"])
@pytest.mark.parametrize("value", [True, False], ids=["true", "false"])
def test_boolean_provider_error_does_not_echo_sensitive_sibling_value(native, caplog, encoded, value):
    server, calls, _build = native
    vin = "A" * 17
    parameters = {"vin": vin, "lang": value}
    supplied = json.dumps(parameters) if encoded else parameters

    with pytest.raises(ToolError) as caught:
        invoke(server, {"operation": "vin_decode", "provider_parameters": supplied})

    assert vin not in str(caught.value)
    assert vin not in repr(caught.value)
    assert vin not in caplog.text
    assert "provider_parameters.lang" in str(caught.value)
    assert calls == {"pool": 0, "adapter": []}


def test_partsapi_native_guard_is_idempotent(native, monkeypatch):
    server, calls, _build = native
    model = server._tool_manager.get_tool("partsapi_catalog_lookup").fn_metadata.arg_model
    schema = model.model_json_schema(by_alias=True)

    def unexpected_rebuild(*_args, **_kwargs):
        raise AssertionError("unchanged native guards must not rebuild the argument model")

    monkeypatch.setattr(model, "model_rebuild", unexpected_rebuild)
    mcp_tools._guard_partsapi_native_inputs(server)
    mcp_tools._guard_partsapi_native_inputs(server)

    assert model.model_json_schema(by_alias=True) == schema
    assert calls == {"pool": 0, "adapter": []}


@pytest.mark.parametrize("field", ID_FIELDS)
@pytest.mark.parametrize(
    "value,union_expected,int_expected",
    [
        (42, 42, 42),
        ("42", "42", 42),
        (1.0, 1, 1),
        ("1.0", "1.0", 1),
        (None, None, None),
        ("null", None, None),
        ("true", "true", "sdk_error"),
        ("false", "false", "sdk_error"),
    ],
)
def test_native_nonboolean_id_coercions_remain_compatible(native, field, value, union_expected, int_expected):
    server, calls, _build = native
    expected = int_expected if field == "lang_id" else union_expected
    if expected == "sdk_error":
        with pytest.raises(ToolError, match=field):
            invoke(server, {"operation": "article", field: value})
        assert calls == {"pool": 0, "adapter": []}
        return

    content, response = invoke(server, {"operation": "article", field: value})

    observed = calls["adapter"][0][field]
    assert observed == expected and type(observed) is type(expected)
    assert calls["pool"] == len(calls["adapter"]) == 1
    assert response["execution"]["network_calls"] == 0
    assert json.loads(content[0].text) == response


@pytest.mark.parametrize("encoded", [False, True], ids=["object", "encoded-object"])
@pytest.mark.parametrize("value", [42, "42", 1.0, 1.5, "true", "false"])
def test_native_provider_nonboolean_scalars_reach_adapter_unchanged(native, encoded, value):
    server, calls, _build = native
    parameters = {"makeId": value, "carType": "PC"}
    supplied = json.dumps(parameters) if encoded else parameters

    _content, response = invoke(server, {"operation": "getModels", "provider_parameters": supplied})

    observed = calls["adapter"][0]["provider_parameters"]
    assert observed == parameters
    assert type(observed["makeId"]) is type(value)
    assert calls["pool"] == len(calls["adapter"]) == 1
    assert response["execution"]["network_calls"] == 0


@pytest.mark.parametrize("value", [True, False])
def test_direct_domain_keeps_boolean_rejection_before_credentials(value):
    result = catalog_clients.partsapi_catalog_lookup(
        operation="getModels", provider_parameters={"carType": "PC", "makeId": value, "lang": 16}, dry_run=True
    )

    assert result["outcome"] == "invalid_input"
    assert result["attempt_count"] == 0


def test_all_native_input_schemas_and_partsapi_annotations_keep_public_contract(native):
    _server, calls, build = native
    server = build(all_tools=True)
    tools = asyncio.run(server.list_tools())
    schemas = {tool.name: tool.inputSchema for tool in tools}
    root = Path(__file__).resolve().parents[1]
    catalog = json.loads((root / "docs/agent/manager_mcp_catalog.json").read_text(encoding="utf-8"))
    assert len(tools) == catalog["expected_tool_count"] == 70
    assert set(schemas) == set(catalog["expected_tool_names"])
    assert mcp_schema_fingerprint(schemas) == catalog["schema_fingerprint"]
    partsapi = next(tool for tool in tools if tool.name == "partsapi_catalog_lookup")
    assert partsapi.annotations.readOnlyHint is True
    assert partsapi.annotations.destructiveHint is False
    registered = server._tool_manager.get_tool("partsapi_catalog_lookup")
    assert registered.fn_metadata.arg_model.model_json_schema(by_alias=True) == partsapi.inputSchema
    assert calls == {"pool": 0, "adapter": []}
