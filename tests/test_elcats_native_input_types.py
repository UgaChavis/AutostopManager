from __future__ import annotations

import asyncio
from copy import deepcopy
import inspect
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import threading

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from autostop_manager import catalog_clients, elcats_catalog as catalog, mcp_read_executor, mcp_tools
from autostop_manager.mcp_contract import mcp_schema_fingerprint


IDENTITY = {"vehicle_profile": {"make": "SYNTHETIC", "model": "MODEL"}}


class Inert:
    def __init__(self, *_args, **_kwargs):
        pass


@pytest.fixture(autouse=True)
def isolated_inputs(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("synthetic Elcats native test attempted external access")

    for owner, name in (
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (sqlite3, "connect"),
        (subprocess, "Popen"),
        (threading.Thread, "start"),
        (catalog_clients, "load_runtime_env"),
        (catalog.PublicCatalogReader, "_request"),
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
    calls = {"pool": 0, "readers": []}
    reader_type = catalog.PublicCatalogReader

    class InlinePool:
        async def run(self, operation):
            calls["pool"] += 1
            return operation()

    def reader(**kwargs):
        calls["readers"].append(kwargs)
        return reader_type(**kwargs)

    monkeypatch.setattr(mcp_read_executor, "_EXECUTOR", InlinePool())
    monkeypatch.setattr(catalog, "PublicCatalogReader", reader)
    monkeypatch.setattr(
        catalog, "elcats_catalog_entries", lambda: [{"id": "synthetic", "brand": "SYNTHETIC", "aliases": []}]
    )
    monkeypatch.setattr(catalog, "elcats_catalog_status", lambda: {"enabled": True, "registry_version": "synthetic"})
    monkeypatch.setattr(catalog._Navigation, "vehicles", lambda _self, _entry: [])

    def build(*, all_tools=False, include_tools=None):
        server = FastMCP("elcats-native-input-types")
        mcp_tools.register_manager_tools(
            server,
            store=Inert(),
            store_client=Inert(),
            include_tools=None if all_tools else include_tools or {"elcats_catalog_query"},
        )
        return server

    return build(), calls, build


def invoke(server, **arguments):
    supplied = {"operation": "resolve_vehicle", "vehicle_identity": deepcopy(IDENTITY), **arguments}
    original = deepcopy(supplied)
    try:
        return asyncio.run(server.call_tool("elcats_catalog_query", supplied))
    finally:
        assert supplied == original


@pytest.mark.parametrize("value", [True, False], ids=["true", "false"])
def test_native_boolean_page_budget_rejected_before_pool_or_reader(native, value):
    server, calls, _build = native

    with pytest.raises(ToolError, match="page_budget"):
        invoke(server, page_budget=value)

    assert calls == {"pool": 0, "readers": []}


@pytest.mark.parametrize("value", [True, False], ids=["true", "false"])
@pytest.mark.parametrize("encoded", [False, True], ids=["object", "encoded-object"])
def test_native_boolean_error_does_not_echo_sibling_vehicle_profile(native, caplog, value, encoded):
    server, calls, _build = native
    synthetic_vin = "A" * 17
    other_field = "SYNTHETIC-SIBLING-CONTENT"
    identity = {
        "vehicle_profile": {"make": "SYNTHETIC", "model": "MODEL", "engine": synthetic_vin, "series": other_field}
    }
    supplied = json.dumps(identity) if encoded else identity

    with pytest.raises(ToolError, match="page_budget") as caught:
        invoke(server, vehicle_identity=supplied, page_budget=value)

    for rendered in (str(caught.value), repr(caught.value), caplog.text):
        assert synthetic_vin not in rendered
        assert other_field not in rendered
    assert calls == {"pool": 0, "readers": []}


@pytest.mark.parametrize("value,expected", [(1, 1), (24, 24), ("1", 1), (1.0, 1)])
def test_native_integer_and_existing_nonboolean_coercions_keep_reader_budget(native, value, expected):
    server, calls, _build = native

    _content, response = invoke(server, page_budget=value)

    assert response["outcome"] == "empty"
    assert response["execution"]["network_calls"] == 0
    assert calls["pool"] == len(calls["readers"]) == 1
    assert calls["readers"][0]["page_budget"] == expected
    assert type(calls["readers"][0]["page_budget"]) is int


@pytest.mark.parametrize("value", [0, 25])
def test_native_range_failures_still_use_domain_validation_without_reader(native, value):
    server, calls, _build = native

    _content, response = invoke(server, page_budget=value)

    assert response["outcome"] == "invalid_input"
    assert calls == {"pool": 1, "readers": []}


@pytest.mark.parametrize("value", [True, False, 0, 25, "1", 1.0])
def test_direct_domain_retains_type_and_range_rejection(native, value):
    _server, calls, _build = native

    response = catalog.elcats_catalog_query("resolve_vehicle", deepcopy(IDENTITY), page_budget=value)

    assert response["outcome"] == "invalid_input"
    assert calls == {"pool": 0, "readers": []}


def test_native_default_page_budget_remains_twelve(native):
    server, calls, _build = native

    _content, response = invoke(server)

    assert response["outcome"] == "empty"
    assert calls["readers"][0]["page_budget"] == 12


def test_reapplying_guard_preserves_metadata_signature_and_partial_registration(native):
    server, calls, build = native
    tool = server._tool_manager.get_tool("elcats_catalog_query")
    signature = inspect.signature(tool.fn)
    schema = deepcopy(tool.parameters)
    metadata = tool.fn_metadata
    core = metadata.arg_model.__pydantic_core_schema__

    for _ in range(3):
        mcp_tools._guard_elcats_native_inputs(server)

    assert metadata.arg_model.__pydantic_core_schema__ is core
    assert tool.fn_metadata is metadata
    assert inspect.signature(tool.fn) == signature
    assert tool.parameters == schema
    assert metadata.arg_model.model_json_schema(by_alias=True) == schema
    assert len(metadata.arg_model.model_fields["page_budget"].metadata) == 1
    mcp_tools._guard_elcats_native_inputs(object())
    mcp_tools._guard_elcats_native_inputs(FastMCP("no-elcats"))
    other = build(include_tools={"inspect_vehicle_identifier"})
    assert other._tool_manager.get_tool("elcats_catalog_query") is None
    assert calls == {"pool": 0, "readers": []}


def test_all_native_schemas_and_elcats_annotations_keep_public_contract(native):
    _server, calls, build = native
    server = build(all_tools=True)
    tools = asyncio.run(server.list_tools())
    schemas = {tool.name: tool.inputSchema for tool in tools}
    manifest = json.loads((Path(__file__).resolve().parents[1] / "docs/agent/manager_mcp_catalog.json").read_text())

    assert len(tools) == manifest["expected_tool_count"]
    assert set(schemas) == set(manifest["expected_tool_names"])
    assert mcp_schema_fingerprint(schemas) == manifest["schema_fingerprint"]
    elcats = next(tool for tool in tools if tool.name == "elcats_catalog_query")
    assert elcats.annotations.readOnlyHint is True
    assert elcats.annotations.destructiveHint is False
    assert elcats.annotations.idempotentHint is False
    assert elcats.annotations.openWorldHint is True
    assert calls == {"pool": 0, "readers": []}
