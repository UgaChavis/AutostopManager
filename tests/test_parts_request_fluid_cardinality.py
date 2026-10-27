"""Existing RU liter quantities identify their own requested position."""

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
from autostop_manager.parts_intent import normalize_part_intent


LITERS = [("4 л", 4), ("4л", 4), ("4 литр", 4), ("4 литра", 4), ("4 литров", 4), ("1,5 л", 1.5), ("0 л", 0)]


def forbid_effects(patch):
    def denied(*args, **kwargs):
        raise AssertionError("fluid cardinality tests cannot acquire external/default state")

    for owner, name in (
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (sqlite3, "connect"),
        (subprocess, "run"),
        (subprocess, "Popen"),
        (config, "get_db_path"),
        (mcp_tools, "StoreState"),
    ):
        patch.setattr(owner, name, denied)


@pytest.fixture(autouse=True)
def isolated_request(monkeypatch):
    forbid_effects(monkeypatch)


@pytest.fixture(scope="module")
def native_tool():
    class Inert:
        def __init__(self, *args, **kwargs):
            pass

    with pytest.MonkeyPatch.context() as patch:
        forbid_effects(patch)
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
        server = FastMCP("offline-fluid-cardinality")
        mcp_tools.register_manager_tools(
            server, store=Inert(), store_client=Inert(), include_tools={"normalize_parts_request"}
        )
        yield server._tool_manager.get_tool("normalize_parts_request")


def native_call(tool, arguments):
    snapshot = deepcopy(arguments)
    content, response = asyncio.run(tool.run(arguments, convert_result=True))
    assert arguments == snapshot
    assert json.loads(content[0].text) == response
    assert response["execution"] == {"network_calls": 0, "attempts": []}
    return response


@pytest.fixture(params=["direct", "native"])
def parse(request, native_tool):
    def call(text):
        if request.param == "direct":
            response = normalize_parts_request(text=text)
        else:
            response = native_call(native_tool, {"text": text})
        assert response["execution"] == {"network_calls": 0, "attempts": []}
        return response

    return call


@pytest.mark.parametrize("suffix,_quantity", LITERS)
def test_shared_intent_guard_requires_splitting_named_liter_position(suffix, _quantity):
    text = "масляный фильтр и масло " + suffix
    intent = normalize_part_intent(text)

    assert intent["raw"] == text
    assert intent["intent_id"] == "multiple_parts"
    assert intent["clarification_required"] is True
    assert intent["clarification_fields"] == ["split_by_part"]


@pytest.mark.parametrize("suffix,quantity", LITERS)
def test_named_liter_position_does_not_supply_the_filters_unknown_quantity(parse, suffix, quantity):
    first, second = "масляный фильтр", "масло " + suffix
    response = parse(first + " и " + second)
    items = response["data"]["items"]

    assert response["outcome"] == "partial"
    assert [row["raw"] for row in items] == [first, second]
    assert [row["intent"]["intent_id"] for row in items] == ["oil_filter", "unknown"]
    assert [(row["quantity"], row["unit"]) for row in items] == [(None, None), (quantity, "liter")]
    assert items[0]["quantity_status"] == "unknown" and "quantity" in items[0]["missing_fields"]
    assert items[1]["quantity_status"] == "known" and "part_intent" in items[1]["missing_fields"]
    assert len({row["item_id"] for row in items}) == 2


@pytest.mark.parametrize("separator", [" и ", " and ", " а также ", " & ", " / ", "+"])
def test_existing_conjunctions_keep_the_named_liter_position(parse, separator):
    response = parse("масляный фильтр" + separator + "масло 4 л")
    assert [row["raw"] for row in response["data"]["items"]] == ["масляный фильтр", "масло 4 л"]


@pytest.mark.parametrize("first_quantity", [1, 4])
def test_equal_and_different_numeric_counts_retain_their_units(parse, first_quantity):
    response = parse(f"масляный фильтр {first_quantity} шт и масло 4 л")
    assert [(row["quantity"], row["unit"]) for row in response["data"]["items"]] == [
        (first_quantity, "piece"),
        (4, "liter"),
    ]


@pytest.mark.parametrize("separator", [", ", "; ", "\n"])
def test_hard_separators_keep_existing_liter_behavior(parse, separator):
    response = parse("масляный фильтр" + separator + "масло 4 л")
    assert [row["raw"] for row in response["data"]["items"]] == ["масляный фильтр", "масло 4 л"]
    assert [row["quantity"] for row in response["data"]["items"]] == [None, 4]


@pytest.mark.parametrize(
    "raw,quantity",
    [("масляный фильтр, 4 л", 4), ("масляный фильтр,количество:4л", 4), ("масляный фильтр, qty=1,5л", 1.5)],
)
def test_quantity_only_comma_labels_stay_attached_to_their_item(parse, raw, quantity):
    assert normalize_part_intent(raw)["intent_id"] == "oil_filter"
    response = parse(raw)
    assert response["outcome"] == "success"
    assert len(response["data"]["items"]) == 1
    item = response["data"]["items"][0]
    assert item["raw"] == raw and (item["quantity"], item["unit"]) == (quantity, "liter")


@pytest.mark.parametrize(
    "first,intent",
    [
        ("injector seal and washer", "injector_seal_washer"),
        ("oil filter with housing and seal", "oil_filter"),
        ("Straße OIL FILTER", "oil_filter"),
        ("ﬃ injector seal and washer", "injector_seal_washer"),
    ],
)
def test_assembly_names_and_expanding_casefolds_keep_their_raw_boundaries(parse, first, intent):
    response = parse(first + " и масло 4 л")
    items = response["data"]["items"]
    assert [row["raw"] for row in items] == [first, "масло 4 л"]
    assert [row["intent"]["intent_id"] for row in items] == [intent, "unknown"]


@pytest.mark.parametrize("suffix", ["4 литража", "4 лекс", "4 л_", "4 л9", "SKU4 л", "AB-4 л"])
def test_unit_word_suffixes_and_markings_cannot_become_liter_quantities(parse, suffix):
    response = parse("масляный фильтр " + suffix)
    assert response["outcome"] == "partial"
    assert all(row["quantity"] is None and row["unit"] is None for row in response["data"]["items"])


@pytest.mark.parametrize("raw", ["масло 4 l", "масляный фильтр и масло 4 l"])
def test_english_l_stays_unknown_without_new_unit_conversion(parse, raw):
    response = parse(raw)
    assert response["outcome"] == "partial"
    assert all(row["quantity"] is None and row["unit"] is None for row in response["data"]["items"])


@pytest.mark.parametrize("encoded", [False, True], ids=["object-items", "JSON-items"])
def test_explicit_fused_raw_uses_shared_guard_without_implicit_resplitting(native_tool, encoded):
    rows = [
        {"item_id": "explicit-fluid", "raw": "масляный фильтр и масло 4 л", "brand": "DEMO", "part_number": "DEMO-001"}
    ]
    response = native_call(native_tool, {"items": json.dumps(rows) if encoded else rows})
    assert response["outcome"] == "partial"
    assert len(response["data"]["items"]) == 1
    item = response["data"]["items"][0]
    assert item["raw"] == rows[0]["raw"] and item["item_id"] == "explicit-fluid"
    assert item["intent"]["intent_id"] == "multiple_parts" and "part_intent" in item["missing_fields"]
    assert item["brand"] == "DEMO" and item["normalized_part_number"] == "DEMO001"


@pytest.mark.parametrize("encoded", [False, True], ids=["object-items", "JSON-items"])
def test_explicit_positions_keep_caller_boundaries_and_null_quantity(native_tool, encoded):
    rows = [{"item_id": "first", "raw": "масляный фильтр", "quantity": None}, {"item_id": "second", "raw": "масло 4 л"}]
    response = native_call(native_tool, {"items": json.dumps(rows) if encoded else rows})
    assert response["outcome"] == "partial"
    items = response["data"]["items"]
    assert [row["item_id"] for row in items] == ["first", "second"]
    assert [row["raw"] for row in items] == [row["raw"] for row in rows]
    assert [(row["quantity"], row["unit"]) for row in items] == [(None, None), (4, "liter")]
