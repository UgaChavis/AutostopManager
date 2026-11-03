"""An implicit labor unit belongs to the selected time field, including null/zero."""

import asyncio
from copy import deepcopy
import json
import socket

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager.automotive_labor import calculate_work_price, normalize_labor_time
from autostop_manager.automotive_mcp import register_automotive_tools


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("supplied labor normalization must remain pure")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture(scope="module")
def native_tool():
    server = FastMCP("isolated-selected-labor-time-unit")
    register_automotive_tools(server)
    return server._tool_manager.get_tool("normalize_labor_time")


@pytest.fixture(params=["direct", "native", "native-json"])
def call(request, native_tool):
    def invoke(row, *, unit=None):
        arguments = {
            "rows": [{"workName": "DEMO work", "operation_id": "demo", **row}],
            "unit": unit,
            "source": {"provider": "DEMO"},
        }
        original = deepcopy(arguments)
        if request.param == "direct":
            response = normalize_labor_time(**arguments)
        else:
            wire = {
                key: json.dumps(value) if request.param == "native-json" and isinstance(value, (dict, list)) else value
                for key, value in arguments.items()
            }
            content, response = asyncio.run(native_tool.run(wire, convert_result=True))
            assert json.loads(content[0].text) == response
        assert arguments == original
        assert response["execution"] == {"network_calls": 0, "attempts": []}
        return response

    return invoke


def _assert_time(response, raw_time, raw_unit, hours, missing):
    assert response["ok"] is True
    assert response["outcome"] == ("partial" if missing else "success")
    assert response["missing_fields"] == [f"rows[0].{field}" for field in sorted(missing)]
    row = response["data"]["labor"][0]
    assert row["raw_time"] == raw_time
    assert row["raw_unit"] == raw_unit
    if hours is None:
        assert row["hours"] is None
    else:
        assert row["hours"] == pytest.approx(hours)


@pytest.mark.parametrize("selected", ["workTime", "raw_time", "time"])
@pytest.mark.parametrize("ignored", ["hours", "labor_hours", "norm_hours"])
@pytest.mark.parametrize("ignored_value", [None, 0])
def test_ignored_hours_key_cannot_supply_a_unit(call, selected, ignored, ignored_value):
    response = call({selected: 120, ignored: ignored_value})
    _assert_time(response, 120, None, None, ["unit"])


@pytest.mark.parametrize("selected", ["workTime", "raw_time", "time"])
@pytest.mark.parametrize("value", [None, 0])
def test_selected_null_or_zero_does_not_fall_back_to_lower_hours(call, selected, value):
    response = call({selected: value, "hours": 2})
    _assert_time(response, value, None, None, ["unit", "time"] if value is None else ["unit"])


@pytest.mark.parametrize("selected", ["hours", "labor_hours", "norm_hours"])
@pytest.mark.parametrize("value,hours", [(None, None), (0, 0), ("1,5", 1.5)])
def test_selected_hours_alias_retains_its_implicit_unit(call, selected, value, hours):
    response = call({selected: value})
    _assert_time(response, value, "hours", hours, ["time"] if value is None else [])


@pytest.mark.parametrize(
    "fields,unit,raw_time,raw_unit,hours,missing",
    [
        (
            {"workTime": 1, "raw_time": 2, "time": 3, "hours": 4, "labor_hours": 5, "norm_hours": 6},
            "minutes",
            1,
            "minutes",
            1 / 60,
            [],
        ),
        ({"workTime": None, "raw_time": 2, "hours": 3}, "minutes", None, "minutes", None, ["time"]),
        ({"raw_time": 0, "time": 3, "hours": 4}, "minutes", 0, "minutes", 0, []),
        ({"time": 2, "hours": 4, "norm_hours": 5}, "seconds", 2, "seconds", 2 / 3600, []),
        ({"hours": 1, "labor_hours": 2, "norm_hours": 3}, None, 1, "hours", 1, []),
        ({"labor_hours": None, "norm_hours": 3}, None, None, "hours", None, ["time"]),
    ],
)
def test_existing_first_present_value_order_is_preserved(call, fields, unit, raw_time, raw_unit, hours, missing):
    _assert_time(call(fields, unit=unit), raw_time, raw_unit, hours, missing)


@pytest.mark.parametrize(
    "fields,unit,raw_unit,hours,missing",
    [
        ({"workTime": 120, "hours": None}, "minutes", "minutes", 2, []),
        ({"workTime": 120, "hours": 0, "unit": "minutes"}, "seconds", "minutes", 2, []),
        ({"workTime": 120, "hours": None, "unit": "seconds"}, "minutes", "seconds", 1 / 30, []),
        ({"workTime": 120, "unit": None}, "minutes", None, None, ["unit"]),
        ({"workTime": 120, "hours": None, "unit": None}, "minutes", None, None, ["unit"]),
        ({"workTime": 120, "hours": 0, "unit": "ticks"}, "minutes", "ticks", None, ["unit"]),
        ({"workTime": 120, "unit": ""}, "hours", "", None, ["unit"]),
    ],
)
def test_explicit_row_and_global_units_keep_their_priority(call, fields, unit, raw_unit, hours, missing):
    _assert_time(call(fields, unit=unit), 120, raw_unit, hours, missing)


@pytest.mark.parametrize(
    "fields,raw_unit,hours,missing",
    [
        ({"hours": 2}, "minutes", 2 / 60, []),
        ({"hours": 2, "unit": None}, "hours", 2, []),
        ({"norm_hours": None, "unit": None}, "hours", None, ["time"]),
    ],
)
def test_own_hours_with_explicit_or_null_unit_retains_existing_behavior(call, fields, raw_unit, hours, missing):
    _assert_time(call(fields, unit="minutes"), fields.get("hours", fields.get("norm_hours")), raw_unit, hours, missing)


@pytest.mark.parametrize("unknown", [{"": 120}, {"unknown": 120}, {"": None, "null": 180}])
@pytest.mark.parametrize("unit", [None, "minutes"])
def test_no_known_time_field_keeps_raw_time_unknown(call, unknown, unit):
    _assert_time(call(unknown, unit=unit), None, unit, None, ["time", "unit"] if unit is None else ["time"])


@pytest.mark.parametrize("unknown", [{None: 180}, {"": 120, None: 180}])
def test_direct_absent_time_field_never_looks_up_none_or_empty_key(unknown):
    response = normalize_labor_time([{"workName": "DEMO work", **unknown}], unit="minutes", source={"provider": "DEMO"})
    _assert_time(response, None, "minutes", None, ["time"])


@pytest.mark.parametrize("raw", [[1, 2], {"low": 1, "high": 2}, True, -1])
def test_unusable_time_preserves_raw_input_and_unknown_time(call, raw):
    _assert_time(call({"workTime": raw}, unit="minutes"), raw, "minutes", None, ["time"])


def test_nonfinite_time_is_unknown_and_both_native_channels_are_strict_json(native_tool):
    def reject_constant(value):
        raise ValueError(value)

    cases = [
        (float("inf"), None),
        (float("-inf"), None),
        (float("nan"), None),
        ([1, {"time": float("inf"), "name": "demo"}], [1, {"time": None, "name": "demo"}]),
        ({"values": [float("-inf"), 2, float("nan")], "ready": True}, {"values": [None, 2, None], "ready": True}),
    ]
    for raw, expected in cases:
        arguments = {"rows": [{"workName": "DEMO work", "hours": raw}], "source": {"provider": "DEMO"}}
        original = json.dumps(arguments, sort_keys=True)
        direct = normalize_labor_time(**arguments)
        content, structured = asyncio.run(native_tool.run(arguments, convert_result=True))
        assert json.loads(content[0].text, parse_constant=reject_constant) == structured == direct
        json.dumps(structured, allow_nan=False)
        _assert_time(direct, expected, "hours", None, ["time"])
        assert json.dumps(arguments, sort_keys=True) == original


def test_nonfinite_echoed_metadata_keeps_labor_outputs_consistent():
    def reject_constant(value):
        raise ValueError(value)

    nonfinite = float("inf")
    nested = {"values": [nonfinite, 3, {"unit": "demo"}]}
    safe_nested = {"values": [None, 3, {"unit": "demo"}]}
    source = {"provider": "DEMO", "extra": nested}
    normalize_args = {
        "rows": [
            {
                "operation_id": "demo1",
                "operation_name": "DEMO work",
                "workTime": nested,
                "unit": nested,
                "source": source,
                "catalog_ref": nested,
                "scope": nested,
                "conditions": nested,
            },
            {"operation_id": "demo2", "operation_name": "DEMO work 2", "hours": 2},
        ],
        "source": source,
    }
    price_args = {
        "labor": [
            {"operation_id": "demo1", "operation_name": nested, "hours": 1},
            {"operation_id": "demo2", "operation_name": "DEMO work", "hours": 2},
        ],
        "policy": {"basis": "hourly_rate", "version": nested, "currency": nested, "metadata": nested},
        "hourly_rate": 1000,
        "unknown_costs": ["extra"],
    }
    server = FastMCP("isolated-labor-json")
    register_automotive_tools(server)
    for name, function, arguments in (
        ("normalize_labor_time", normalize_labor_time, normalize_args),
        ("calculate_work_price", calculate_work_price, price_args),
    ):
        original = json.dumps(arguments, sort_keys=True)
        direct = function(**arguments)
        content, structured = asyncio.run(server._tool_manager.get_tool(name).run(arguments, convert_result=True))
        assert json.loads(content[0].text, parse_constant=reject_constant) == structured == direct
        json.dumps(structured, allow_nan=False)
        assert json.dumps(arguments, sort_keys=True) == original
        if name == "normalize_labor_time":
            first, second = direct["data"]["labor"]
            assert first["raw_time"] == first["raw_unit"] == safe_nested
            assert first["catalog_ref"] == first["scope"] == first["conditions"] == safe_nested
            assert first["source"]["extra"] == second["source"]["extra"] == safe_nested
            assert second["hours"] == 2
            assert direct["missing_fields"] == ["rows[0].time", "rows[0].unit"]
        else:
            assert direct["data"]["policy"]["version"] == safe_nested
            assert direct["data"]["policy"]["metadata"] == direct["data"]["currency"] == safe_nested
            assert direct["data"]["operations"][0]["operation_name"] == safe_nested
            assert direct["data"]["known_subtotal"] == 3000 and direct["data"]["total"] is None
            assert direct["missing_fields"] == ["extra"]


def test_selected_time_unit_keeps_raw_metadata_and_repeatable_output(call):
    source = {"provider": "DEMO", "locator": "synthetic:manual"}
    supplied = {
        "workName": "  DEMO work  ",
        "operation_id": 0,
        "workTime": "1,5",
        "hours": None,
        "unit": "minutes",
        "source": source,
        "scope": "operation",
        "catalog_ref": "synthetic:operation",
        "conditions": {"engine": "DEMO", "nested": [None, 0]},
        "included_operations": ["included"],
        "overlaps_with": ["overlap"],
    }
    first = call(supplied, unit="seconds")
    second = call(supplied, unit="seconds")
    assert first == second
    _assert_time(first, "1,5", "minutes", 0.025, [])
    row = first["data"]["labor"][0]
    assert row["operation_id"] == "0" and row["operation_name"] == supplied["workName"]
    for field in ("source", "scope", "catalog_ref", "conditions", "included_operations", "overlaps_with"):
        assert row[field] == supplied[field]
    assert first["data"]["overlaps"] == [
        {"operation_id": "0", "included_operations": ["included"], "overlaps_with": ["overlap"]}
    ]
