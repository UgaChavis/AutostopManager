"""A negative raw count must not become a positive quantity or decimal tail."""

import asyncio
from copy import deepcopy
import json
import socket
import sqlite3

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import config, mcp_telemetry, mcp_tools
from autostop_manager.automotive_parts import normalize_parts_request


def _denied(*args, **kwargs):
    raise AssertionError("parts request normalization must not access external state")


def _forbid_effects(patch):
    patch.setattr(socket.socket, "connect", _denied)
    patch.setattr(socket.socket, "connect_ex", _denied)
    patch.setattr(socket, "create_connection", _denied)
    patch.setattr(socket, "getaddrinfo", _denied)
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
        server = FastMCP("isolated-negative-request-quantity")
        mcp_tools.register_manager_tools(
            server, store=Inert(), store_client=Inert(), include_tools={"normalize_parts_request"}
        )
        assert set(server._tool_manager._tools) == {"normalize_parts_request"}
        yield server._tool_manager.get_tool("normalize_parts_request")


@pytest.fixture(params=["direct", "native", "native-json"])
def call(request, native_tool):
    def invoke(**arguments):
        snapshot = deepcopy(arguments)
        expected = normalize_parts_request(**deepcopy(arguments))
        if request.param == "direct":
            response = normalize_parts_request(**arguments)
        else:
            wire = {
                key: json.dumps(value, ensure_ascii=False)
                if request.param == "native-json" and key == "items" and isinstance(value, list)
                else value
                for key, value in arguments.items()
            }
            content, response = asyncio.run(native_tool.run(wire, convert_result=True))
            assert len(content) == 1
            assert json.loads(content[0].text) == response
            assert response["tool_execution"]["tool"] == "normalize_parts_request"
            assert response["tool_execution"]["network_calls"] == 0
        stable = {key: value for key, value in response.items() if key != "tool_execution"}
        # JSON transport serializes tuple-valued intent metadata as arrays.
        assert json.loads(json.dumps(stable, ensure_ascii=False)) == json.loads(
            json.dumps(expected, ensure_ascii=False)
        )
        assert arguments == snapshot
        assert response["execution"] == {"network_calls": 0, "attempts": []}
        return response

    return invoke


NEGATIVE_QUANTITIES = [
    "-2",
    "-12",
    "-2.5",
    "-2,5",
    "-0.25",
    "-0,25",
    "−2",
    "−12",
    "−2.5",
    "−2,5",
    "−0.25",
    "−0,25",
    "-.5",
    "-,5",
    "−.5",
    "−,5",
]


@pytest.mark.parametrize("quantity", NEGATIVE_QUANTITIES)
@pytest.mark.parametrize("unit", ["шт", "комплект"])
def test_negative_raw_quantity_remains_unknown_without_suffix_salvage(call, quantity, unit):
    raw = f"масляный фильтр {quantity} {unit}"
    response = call(text=raw)

    assert response["ok"] is True
    assert response["outcome"] == "partial"
    assert len(response["data"]["items"]) == 1
    item = response["data"]["items"][0]
    assert item["raw"] == raw
    assert item["intent"]["intent_id"] == "oil_filter"
    assert item["quantity"] is None
    assert item["quantity_status"] == "unknown"
    assert item["item_id"].startswith("part-")
    assert "quantity" in item["missing_fields"]


@pytest.mark.parametrize("raw_quantity", ["-2.5", "−2"])
@pytest.mark.parametrize("shape", ["string", "object"])
def test_negative_quantity_in_explicit_raw_items_uses_the_same_unknown_contract(call, raw_quantity, shape):
    raw = f"масляный фильтр {raw_quantity} шт"
    supplied = raw if shape == "string" else {"raw": raw, "item_id": "synthetic-negative"}
    response = call(items=[supplied])

    assert response["outcome"] == "partial"
    item = response["data"]["items"][0]
    assert item["raw"] == raw
    assert item["quantity"] is None and item["quantity_status"] == "unknown"
    if shape == "object":
        assert item["item_id"] == "synthetic-negative"


@pytest.mark.parametrize(
    "suffix,quantity,unit",
    [
        ("2 шт", 2, "piece"),
        ("2.5 шт", 2.5, "piece"),
        ("2,5 шт", 2.5, "piece"),
        ("0 шт", 0, "piece"),
        ("2.5 комплекта", 2.5, "set"),
        ("2,5 пары", 2.5, "pair"),
        ("2,5 л", 2.5, "liter"),
        ("2 шт.", 2, "piece"),
        ("– 2 шт", 2, "piece"),
        ("— 2 шт", 2, "piece"),
    ],
)
def test_supported_positive_counts_units_and_spaced_typographic_delimiters_are_preserved(call, suffix, quantity, unit):
    raw = f"масляный фильтр {suffix}"
    response = call(text=raw)

    assert response["outcome"] == "success"
    item = response["data"]["items"][0]
    assert item["raw"] == raw
    assert (item["quantity"], item["unit"], item["quantity_status"]) == (quantity, unit, "known")


@pytest.mark.parametrize(
    "raw,quantity", [("масляный фильтр,1 шт", 1), ("масляный фильтр,2,5 шт", 2.5), ("масляный фильтр.2 шт", 2)]
)
def test_punctuation_before_a_complete_quantity_keeps_supported_legacy_forms(call, raw, quantity):
    response = call(text=raw)
    assert response["outcome"] == "success"
    assert len(response["data"]["items"]) == 1
    item = response["data"]["items"][0]
    assert item["raw"] == raw
    assert (item["quantity"], item["unit"], item["quantity_status"]) == (quantity, "piece", "known")


@pytest.mark.parametrize("token", ["SKU12", "AB-12", "SKU12.5", "SKU12,5", "AB-12.5", "AB-12,5"])
def test_word_or_sku_digits_cannot_supply_a_numeric_tail_quantity(call, token):
    raw = f"масляный фильтр {token} шт"
    response = call(text=raw)

    assert response["outcome"] == "partial"
    item = response["data"]["items"][0]
    assert item["raw"] == raw
    assert item["quantity"] is None and item["quantity_status"] == "unknown"


@pytest.mark.parametrize("quantity", [0, None])
@pytest.mark.parametrize("raw_quantity", ["2", "-2.5", "−2"])
def test_explicit_zero_or_null_overrides_raw_inference_without_losing_metadata(call, quantity, raw_quantity):
    raw = f"масляный фильтр {raw_quantity} шт"
    response = call(
        items=[
            {
                "item_id": "synthetic-explicit",
                "raw": raw,
                "quantity": quantity,
                "unit": "piece",
                "brand": "DEMO",
                "part_number": "DEMO-001",
            }
        ]
    )

    assert response["ok"] is True
    assert response["outcome"] == ("partial" if quantity is None else "success")
    item = response["data"]["items"][0]
    assert item["item_id"] == "synthetic-explicit" and item["raw"] == raw
    assert (item["quantity"], item["unit"]) == (quantity, "piece")
    assert item["quantity_status"] == ("unknown" if quantity is None else "known")
    assert item["brand"] == "DEMO"
    assert item["raw_part_number"] == "DEMO-001" and item["normalized_part_number"] == "DEMO001"
    assert item["number_kind"] == "unknown"


@pytest.mark.parametrize("quantity", [-2, -2.5, False])
def test_explicit_invalid_quantity_keeps_existing_invalid_input_contract(call, quantity):
    response = call(items=[{"raw": "масляный фильтр 2 шт", "quantity": quantity, "unit": "piece"}])

    assert response["ok"] is False and response["outcome"] == "invalid_input"
    assert response["missing_fields"] == ["quantity"]


@pytest.mark.parametrize("quantity", ["-2.5", "-2,5", "−2"])
def test_negative_and_positive_positions_are_both_retained(call, quantity):
    first = f"масляный фильтр {quantity} шт"
    second = "салонный фильтр 2 шт"
    response = call(text=first + "; " + second)

    assert response["outcome"] == "partial"
    items = response["data"]["items"]
    assert [item["raw"] for item in items] == [first, second]
    assert [item["intent"]["intent_id"] for item in items] == ["oil_filter", "cabin_filter"]
    assert [item["quantity"] for item in items] == [None, 2]
    assert [item["quantity_status"] for item in items] == ["unknown", "known"]


@pytest.mark.parametrize("unit", ["pc", "piece"])
def test_english_units_remain_raw_unknown_without_new_conversion(call, unit):
    raw = f"масляный фильтр 2 {unit}"
    response = call(text=raw)

    assert response["outcome"] == "partial"
    item = response["data"]["items"][0]
    assert item["raw"] == raw
    assert item["quantity"] is None and item["unit"] is None and item["quantity_status"] == "unknown"


@pytest.mark.parametrize("separator", ["-", "−", "–", "—"])
@pytest.mark.parametrize("padding", ["", " ", "\u00a0"])
@pytest.mark.parametrize("lower,upper", [("2", "3"), ("0.5", "1.5"), ("0,5", "1,5")])
def test_quantity_range_does_not_become_its_upper_bound(call, separator, padding, lower, upper):
    raw = f"масляный фильтр {lower}{padding}{separator}{padding}{upper} шт"
    response = call(text=raw)

    assert response["ok"] is True and response["outcome"] == "partial"
    assert len(response["data"]["items"]) == 1
    item = response["data"]["items"][0]
    assert item["raw"] == raw and item["intent"]["intent_id"] == "oil_filter"
    assert item["quantity"] is None and item["quantity_status"] == "unknown"
    assert "quantity" in item["missing_fields"]


@pytest.mark.parametrize("bounds", ["-2–3", "−0.5—1.5"])
def test_invalid_negative_lower_bound_does_not_make_the_upper_bound_exact(call, bounds):
    response = call(text=f"масляный фильтр {bounds} шт")
    assert response["outcome"] == "partial"
    item = response["data"]["items"][0]
    assert item["quantity"] is None and item["quantity_status"] == "unknown"


@pytest.mark.parametrize("shape", ["string", "object"])
@pytest.mark.parametrize(
    "unit,normalized_unit", [("шт", "piece"), ("комплект", "set"), ("пары", "pair"), ("л", "liter")]
)
def test_explicit_raw_items_keep_range_unknown_for_each_existing_unit(call, shape, unit, normalized_unit):
    raw = f"масляный фильтр 2 — 3 {unit}"
    supplied = raw if shape == "string" else {"raw": raw, "item_id": "synthetic-range"}
    response = call(items=[supplied])

    assert response["outcome"] == "partial"
    item = response["data"]["items"][0]
    assert item["raw"] == raw and item["quantity"] is None and item["quantity_status"] == "unknown"
    assert item["unit"] == normalized_unit and item["missing_fields"] == ["quantity"]
    if shape == "object":
        assert item["item_id"] == "synthetic-range"


@pytest.mark.parametrize(
    "raw",
    [
        "масляный фильтр AB-12 – 3 шт",
        "масляный фильтр SKU12.5 — 3 шт",
        "масляный фильтр SKU12,5 - 3 шт",
        "масляный фильтр: 3 шт",
        "1. масляный фильтр — 3 шт",
        "— масляный фильтр 3 шт",
    ],
)
def test_item_markers_and_part_number_delimiters_are_not_quantity_ranges(call, raw):
    response = call(text=raw)

    assert response["outcome"] == "success"
    assert len(response["data"]["items"]) == 1
    item = response["data"]["items"][0]
    assert item["raw"] == raw
    assert (item["quantity"], item["unit"], item["quantity_status"]) == (3, "piece", "known")


@pytest.mark.parametrize("quantity", [None, 0, 5])
@pytest.mark.parametrize("supplied_unit", [True, False])
def test_explicit_quantity_still_overrides_raw_range(call, quantity, supplied_unit):
    row = {"item_id": "explicit-range", "raw": "масляный фильтр 2–3 шт", "quantity": quantity, "unit": "piece"}
    if not supplied_unit:
        del row["unit"]
    response = call(items=[row])

    assert response["outcome"] == ("partial" if quantity is None else "success")
    item = response["data"]["items"][0]
    assert (item["item_id"], item["raw"], item["quantity"], item["unit"]) == (
        row["item_id"],
        row["raw"],
        quantity,
        "piece",
    )


@pytest.mark.parametrize("separator", ["; ", ", ", " и ", "\n"])
def test_unknown_range_does_not_consume_the_next_items_quantity(call, separator):
    first, second = "масляный фильтр 2–3 шт", "салонный фильтр — 4 шт"
    response = call(text=first + separator + second)

    assert response["outcome"] == "partial"
    items = response["data"]["items"]
    assert [item["raw"] for item in items] == [first, second]
    assert [item["quantity"] for item in items] == [None, 4]
    assert [item["quantity_status"] for item in items] == ["unknown", "known"]


@pytest.mark.parametrize(
    "fraction",
    [
        "1/2",
        "1 / 2",
        "1 /2",
        "1/ 2",
        "1\u00a0/\u202f2",
        "0/2",
        "1/0",
        "-1/2",
        "−1/2",
        "2 1/2",
        "1.5/2",
        "1,5/2",
        "1/2/3",
    ],
)
def test_fraction_is_not_converted_to_a_denominator_quantity(call, fraction):
    raw = f"свечи зажигания {fraction} комплекта"
    response = call(text=raw)

    assert response["ok"] is True and response["outcome"] == "partial"
    assert len(response["data"]["items"]) == 1
    item = response["data"]["items"][0]
    assert item["raw"] == raw
    assert (item["quantity"], item["unit"], item["quantity_status"]) == (None, "set", "unknown")
    assert item["missing_fields"] == ["quantity"]


@pytest.mark.parametrize("shape", ["string", "object"])
@pytest.mark.parametrize(
    "unit,normalized_unit", [("шт", "piece"), ("комплект", "set"), ("пары", "pair"), ("л", "liter")]
)
def test_explicit_fraction_items_keep_raw_id_and_unit(call, shape, unit, normalized_unit):
    raw = f"масляный фильтр 1/2 {unit}"
    row = raw if shape == "string" else {"raw": raw, "item_id": "fraction", "brand": "DEMO", "part_number": "AB-12"}
    response = call(items=[row])

    assert response["outcome"] == "partial"
    item = response["data"]["items"][0]
    assert item["raw"] == raw and item["quantity"] is None and item["unit"] == normalized_unit
    assert item["quantity_status"] == "unknown" and item["missing_fields"] == ["quantity"]
    if shape == "object":
        assert (item["item_id"], item["brand"], item["raw_part_number"], item["normalized_part_number"]) == (
            "fraction",
            "DEMO",
            "AB-12",
            "AB12",
        )


@pytest.mark.parametrize(
    "raw",
    [
        "масляный фильтр /2 шт",
        "масляный фильтр / 2 шт",
        "/ 2 шт масляный фильтр",
        "масляный фильтр AB-12 / 2 шт",
        "масляный фильтр SKU12.5 / 2 шт",
        "масляный фильтр SKU12,5 / 2 шт",
        "масляный фильтр AB-12/7 / 2 шт",
        "масляный фильтр SKU12/7 / 2 шт",
    ],
)
def test_slash_item_delimiters_and_skus_keep_exact_quantity(call, raw):
    response = call(text=raw)
    assert response["outcome"] == "success"
    assert len(response["data"]["items"]) == 1
    item = response["data"]["items"][0]
    assert item["raw"] == raw
    assert (item["quantity"], item["unit"], item["quantity_status"]) == (2, "piece", "known")


@pytest.mark.parametrize("quantity", [None, 0, 0.5])
@pytest.mark.parametrize("supplied_unit", [True, False])
def test_explicit_quantity_keeps_priority_over_fraction(call, quantity, supplied_unit):
    row = {"item_id": "explicit-fraction", "raw": "свечи зажигания 1/2 комплекта", "quantity": quantity}
    if supplied_unit:
        row["unit"] = "set"
    response = call(items=[row])
    assert response["outcome"] == ("partial" if quantity is None else "success")
    item = response["data"]["items"][0]
    assert (item["item_id"], item["quantity"], item["unit"]) == ("explicit-fraction", quantity, "set")


@pytest.mark.parametrize("separator", ["; ", ", ", " и ", "\n", " / "])
def test_fraction_keeps_the_next_items_quantity(call, separator):
    first, second = "свечи зажигания 1/2 комплекта", "масляный фильтр / 2 шт"
    response = call(text=first + separator + second)
    assert response["outcome"] == "partial"
    items = response["data"]["items"]
    assert [item["raw"] for item in items] == [first, second]
    assert [item["quantity"] for item in items] == [None, 2]
