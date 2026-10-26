"""Labor normalization rejects malformed names without guessing an operation."""

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
    def denied(*args, **kwargs):
        raise AssertionError("labor normalization must remain a pure operation")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture(scope="module")
def native_tool():
    server = FastMCP("isolated-labor-operation-names")
    register_automotive_tools(server)
    return server._tool_manager.get_tool("normalize_labor_time")


@pytest.fixture(params=["direct", "native", "native-json"])
def call(request, native_tool):
    def invoke(rows, *, unit=None, source=None):
        arguments = {"rows": rows, "unit": unit, "source": source}
        snapshot = deepcopy(arguments)
        if request.param == "direct":
            response = normalize_labor_time(**arguments)
        else:
            wire = (
                {
                    key: json.dumps(value) if isinstance(value, (dict, list)) else value
                    for key, value in arguments.items()
                }
                if request.param == "native-json"
                else arguments
            )
            content, response = asyncio.run(native_tool.run(wire, convert_result=True))
            assert json.loads(content[0].text) == response
        assert arguments == snapshot
        assert response["execution"] == {"network_calls": 0, "attempts": []}
        return response

    return invoke


@pytest.mark.parametrize("alias", ["workName", "operation_name", "operation", "name"])
@pytest.mark.parametrize("name", [False, 123, 1.5, ["DEMO"], {"label": "DEMO"}])
def test_selected_non_string_operation_is_invalid(call, alias, name):
    response = call([{alias: name, "hours": 1}], source={"provider": "DEMO"})

    assert response["ok"] is False
    assert response["outcome"] == "invalid_input"
    assert response["data"] == {}
    assert response["missing_fields"] == ["rows.structure"]


@pytest.mark.parametrize("alias", ["workName", "operation_name", "operation", "name"])
@pytest.mark.parametrize("name", [None, "", " \t\n\u00a0"])
def test_absent_or_blank_operation_is_partial_and_keeps_raw_value(call, alias, name):
    source = {"provider": "DEMO"}
    response = call([{alias: name, "workTime": "120"}], unit="minutes", source=source)

    assert response["ok"] is True
    assert response["outcome"] == "partial"
    assert response["missing_fields"] == ["rows[0].operation_name"]
    row = response["data"]["labor"][0]
    assert row["operation_name"] == name
    assert row["raw_time"] == "120"
    assert row["raw_unit"] == "minutes"
    assert row["hours"] == 2
    assert row["source"] == source


def test_missing_operation_keeps_existing_partial_contract(call):
    response = call([{"hours": 1}], source={"provider": "DEMO"})

    assert response["outcome"] == "partial"
    assert response["missing_fields"] == ["rows[0].operation_name"]
    assert response["data"]["labor"][0]["operation_name"] is None


@pytest.mark.parametrize("alias", ["workName", "operation_name", "operation", "name"])
def test_valid_name_retains_raw_metadata_and_zero_operation_id(call, alias):
    source = {"provider": "DEMO", "locator": "synthetic:manual"}
    supplied = {
        alias: "  Замена масла #42  ",
        "operation_id": 0,
        "workTime": "120",
        "source": source,
        "scope": "operation",
        "catalog_ref": "synthetic:operation",
        "conditions": {"engine": "DEMO"},
        "included_operations": ["included"],
        "overlaps_with": ["overlap"],
    }
    response = call([supplied], unit="minutes", source={"provider": "ignored fallback"})

    assert response["outcome"] == "success"
    assert response["missing_fields"] == []
    row = response["data"]["labor"][0]
    assert row["operation_name"] == supplied[alias]
    assert row["operation_id"] == "0"
    assert row["raw_time"] == "120" and row["raw_unit"] == "minutes" and row["hours"] == 2
    for field in ("source", "scope", "catalog_ref", "conditions", "included_operations", "overlaps_with"):
        assert row[field] == supplied[field]
    assert response["data"]["overlaps"] == [
        {"operation_id": "0", "included_operations": ["included"], "overlaps_with": ["overlap"]}
    ]


@pytest.mark.parametrize(
    "aliases",
    [
        {"workName": "selected", "operation_name": False, "operation": [], "name": {}},
        {"operation_name": "selected", "operation": False, "name": []},
        {"operation": "selected", "name": False},
    ],
)
def test_lower_priority_aliases_do_not_override_or_invalidate_selected_name(call, aliases):
    response = call([{**aliases, "hours": 1}], source={"provider": "DEMO"})

    assert response["outcome"] == "success"
    assert response["data"]["labor"][0]["operation_name"] == "selected"


def test_malformed_high_priority_name_does_not_fall_back_to_valid_alias(call):
    response = call([{"workName": False, "operation_name": "different work", "hours": 1}], source={"provider": "DEMO"})

    assert response["outcome"] == "invalid_input"
    assert response["missing_fields"] == ["rows.structure"]


@pytest.mark.parametrize("first", [None, "", " \t"])
def test_empty_high_priority_name_does_not_fall_back_to_another_operation(call, first):
    response = call([{"workName": first, "operation_name": "different work", "hours": 1}], source={"provider": "DEMO"})

    assert response["outcome"] == "partial"
    assert response["missing_fields"] == ["rows[0].operation_name"]
    assert response["data"]["labor"][0]["operation_name"] == first


def test_name_check_does_not_replace_missing_time_or_source(call):
    response = call([{"name": " \t"}], unit="hours")

    assert response["outcome"] == "partial"
    assert response["missing_fields"] == ["rows[0].operation_name", "rows[0].source", "rows[0].time"]
    assert response["data"]["labor"][0]["hours"] is None


def test_calculation_retains_existing_handling_of_malformed_operation_names():
    response = calculate_work_price(
        [{"operation_id": "unbound", "operation_name": False}],
        {"basis": "public_observations", "version": "DEMO-v1"},
        observations=[],
    )

    assert response["outcome"] == "partial"
    assert response["data"]["total"] is None
    assert response["data"]["exclusions"] == [{"operation_id": "unbound", "reason": "unknown_labor_or_price"}]
