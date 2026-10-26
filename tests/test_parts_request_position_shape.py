"""Malformed nested position hints return an envelope through every public route."""

import asyncio
from copy import deepcopy
import json
import socket
import sqlite3
import subprocess

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import config, mcp_telemetry, mcp_tools
from autostop_manager.automotive_parts import normalize_parts_request


def _denied(*args, **kwargs):
    raise AssertionError("request normalization must not access external state")


def _forbid_effects(patch):
    for name in ("connect", "connect_ex"):
        patch.setattr(socket.socket, name, _denied)
    for name in ("create_connection", "getaddrinfo"):
        patch.setattr(socket, name, _denied)
    for name in ("Popen", "run"):
        patch.setattr(subprocess, name, _denied)
    patch.setattr(sqlite3, "connect", _denied)
    patch.setattr(config, "get_db_path", _denied)
    patch.setattr(mcp_tools, "StoreState", _denied)


@pytest.fixture(autouse=True)
def forbid_external_state(monkeypatch):
    _forbid_effects(monkeypatch)


@pytest.fixture(scope="module")
def native_tool():
    class Inert:
        def __init__(self, *args, **kwargs):
            pass

    with pytest.MonkeyPatch.context() as patch:
        _forbid_effects(patch)
        for name in (
            "StoreIntegration",
            "StoreApiClient",
            "StoreOwnerApiClient",
            "StoreQuoteConductor",
            "StoreQuoteOwnerApi",
        ):
            patch.setattr(mcp_tools, name, Inert)
        for name in (
            "get_store_api_url",
            "get_store_read_token",
            "get_store_manage_token",
            "get_store_quote_token",
            "get_store_owner_token",
        ):
            patch.setattr(mcp_tools, name, lambda: "")
        patch.setattr(mcp_telemetry.LOGGER, "disabled", True)
        server = FastMCP("isolated-parts-position-shape")
        mcp_tools.register_manager_tools(
            server, store=Inert(), store_client=Inert(), include_tools={"normalize_parts_request"}
        )
        assert set(server._tool_manager._tools) == {"normalize_parts_request"}
        yield server._tool_manager.get_tool("normalize_parts_request")


@pytest.fixture(params=["direct", "native", "native-json"])
def call(request, native_tool):
    def invoke(**arguments):
        before = deepcopy(arguments)
        if request.param == "direct":
            response = normalize_parts_request(**arguments)
        else:
            wire = {
                name: json.dumps(value, ensure_ascii=False)
                if name == "items" and request.param == "native-json"
                else value
                for name, value in arguments.items()
            }
            content, response = asyncio.run(native_tool.run(wire, convert_result=True))
            assert len(content) == 1
            assert json.loads(content[0].text) == response
            assert response["tool_execution"]["network_calls"] == 0
        assert arguments == before
        assert response["execution"] == {"network_calls": 0, "attempts": []}
        return response

    return invoke


@pytest.mark.parametrize("field", ["axle", "side", "position"])
@pytest.mark.parametrize("value", [True, False, 2, 0, 1.5, [], ["left"], {}, {"value": "front"}])
def test_nonstring_position_hint_returns_invalid_input_without_echo(call, field, value):
    response = call(items=[{"raw": "масляный фильтр", field: value, "quantity": 1, "unit": "piece"}])

    assert response["ok"] is False
    assert response["outcome"] == "invalid_input"
    assert response["data"] == {}
    assert response["missing_fields"] == [f"items[0].{field}"]
    assert response["evidence"] == response["conflicts"] == response["warnings"] == []


@pytest.mark.parametrize("field", ["axle", "side", "position"])
@pytest.mark.parametrize("value", [None, "", "   ", "custom position"])
def test_optional_strings_remain_compatible_without_enum_restriction(call, field, value):
    response = call(items=[{"raw": "масляный фильтр", field: value, "quantity": 1, "unit": "piece"}])

    assert response["ok"] is True
    assert response["outcome"] == "success"
    item = response["data"]["items"][0]
    assert item["raw"] == "масляный фильтр"
    assert item["intent"]["intent_id"] == "oil_filter"
    assert item["quantity"] == 1 and item["unit"] == "piece"


@pytest.mark.parametrize("field", ["axle", "side", "position"])
def test_later_bad_row_identifies_its_own_input_field(call, field):
    response = call(
        items=[
            {"raw": "масляный фильтр", "quantity": 1, "unit": "piece"},
            {"raw": "передние колодки", field: {"unexpected": True}},
        ]
    )

    assert response["outcome"] == "invalid_input"
    assert response["data"] == {}
    assert response["missing_fields"] == [f"items[1].{field}"]


@pytest.mark.parametrize("quantity", [0, None])
def test_valid_position_hints_preserve_explicit_quantity_and_identifiers(call, quantity):
    supplied = {
        "item_id": "synthetic-position-shape",
        "raw": "передний левый амортизатор",
        "axle": "front",
        "side": "left",
        "position": "outer",
        "quantity": quantity,
        "unit": "piece",
        "part_number": "SYN-42",
        "brand": "Synthetic",
    }
    response = call(items=[supplied])

    assert response["outcome"] == ("success" if quantity == 0 else "partial")
    item = response["data"]["items"][0]
    assert item["quantity"] == quantity
    assert item["quantity_status"] == ("known" if quantity == 0 else "unknown")
    for field in ("item_id", "raw", "axle", "side", "position", "unit", "brand"):
        assert item[field] == supplied[field]
    assert item["raw_part_number"] == "SYN-42"


@pytest.mark.parametrize("structured", [False, True])
def test_raw_text_and_string_rows_keep_existing_quantity_extraction(call, structured):
    raw = "передние колодки 2 комплекта"
    response = call(**({"items": [raw]} if structured else {"text": raw}))

    assert response["outcome"] == "success"
    item = response["data"]["items"][0]
    assert item["raw"] == raw
    assert item["quantity"] == 2 and item["unit"] == "set"
    assert item["axle"] == "front"
