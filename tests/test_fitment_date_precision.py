"""A supplied production month is an interval, never a conflicting exact day."""

import asyncio
from copy import deepcopy
import json
import socket

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager.automotive_mcp import register_automotive_tools
from autostop_manager.automotive_parts import assess_part_fitment


REF = {"provider": "partsapi_ru", "namespace": "tecdoc", "entity_kind": "modification", "id": "42", "carType": "PC"}
BINDING = {"version": 1, "identifier_kind": "market_code", "identifier_sha256": "a" * 64}


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("supplied date evidence must remain offline")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture(scope="module")
def native_tool():
    # Create Windows asyncio's self-pipe before the per-test acquisition block.
    loop = asyncio.new_event_loop()
    server = FastMCP("synthetic-fitment-date-precision")
    register_automotive_tools(server)
    try:
        yield server._tool_manager.get_tool("assess_part_fitment"), loop
    finally:
        loop.close()


def arguments(actual, expected, *, carrier="criteria", scope="modification", field="production_date"):
    conditions = {field: expected}
    source = {
        "provider": "manufacturer",
        "primary_lineage": "synthetic-official",
        "method": "supplied_document",
        "locator": "https://example.org/synthetic-fitment",
        "fetched_at": "2026-01-01T00:00:00Z",
        "document_kind": "official_epc",
        "fitment_assertion": True,
        "scope": scope,
        "part_number": "DEMO123",
        "brand": "DEMO",
        "catalog_ref": deepcopy(REF),
        "identifier_binding": deepcopy(BINDING),
    }
    if carrier == "evidence":
        source["conditions"] = conditions
    return {
        "vehicle": {
            "vehicle_profile": {"make": "DEMO", field: actual},
            "catalog_ref": deepcopy(REF),
            "input_binding": deepcopy(BINDING),
        },
        "part": {"number": "DEMO123", "brand": "DEMO"},
        "criteria": conditions if carrier == "criteria" else {},
        "evidence": [source],
        "scope": scope,
    }


@pytest.fixture(params=["direct", "native", "native-json"])
def assess(request, native_tool):
    def call(inputs, expected_state, *, condition_state=None):
        original = deepcopy(inputs)
        if request.param == "direct":
            response = assess_part_fitment(**inputs)
        else:
            tool, loop = native_tool
            wire = {
                key: json.dumps(value) if request.param == "native-json" and isinstance(value, (dict, list)) else value
                for key, value in inputs.items()
            }
            content, response = loop.run_until_complete(tool.run(wire, convert_result=True))
            assert json.loads(content[0].text) == response
        assert inputs == original
        data = response["data"]
        assert data["state"] == expected_state
        assert data["checked_conditions"][0]["state"] == (condition_state or expected_state)
        assert response["outcome"] == ("partial" if expected_state == "unknown" else "success")
        assert response["execution"] == {"network_calls": 0, "attempts": []}
        assert data["conditional_on_caller_evidence"] is True
        assert data["independent_source_verified"] is False
        assert data["exact_applicability_confirmed"] is False
        assert data["ready_for_crm_writeback"] is False
        assert data["part"] == original["part"]
        if expected_state == "unknown":
            assert response["conflicts"] == []
            if condition_state is None:
                assert data["checked_conditions"][0]["field"] in response["missing_fields"]
        return response

    return call


@pytest.mark.parametrize("carrier", ["criteria", "evidence"])
@pytest.mark.parametrize(
    "actual,required,state",
    [
        ("2020-06-17", "2020-06", "supported"),
        ("2020-06", "2020-06-17", "unknown"),
        ("2020-06", "2020-06", "supported"),
        ("2020-06-17", "2020-06-17", "supported"),
        ("2020-06-16", "2020-06-17", "rejected"),
        ("2020-06", "2020-07", "rejected"),
        ("2021-06-17", "2020-06", "rejected"),
        ("2020-02-29", "2020-02", "supported"),
        ("2019-02-28", "2019-02", "supported"),
    ],
)
def test_scalar_dates_keep_calendar_precision(assess, carrier, actual, required, state):
    response = assess(arguments(actual, required, carrier=carrier), state)
    condition = response["data"]["checked_conditions"][0]
    assert condition["actual"] == actual and condition["required"] == required
    assert ("source_index" in condition) == (carrier == "evidence")


@pytest.mark.parametrize("scope", ["family", "modification", "exact_identifier"])
def test_month_never_proves_a_specific_day_even_with_a_bound_primary_source(assess, scope):
    assess(arguments("2020-06", "2020-06-17", scope=scope, carrier="evidence"), "unknown")


@pytest.mark.parametrize("required", ["2020", "2020-13", "2019-02-29", "unknown", 2020, True, None])
def test_invalid_or_absent_scalar_date_is_unknown(assess, required):
    assess(arguments("2020-06-17", required), "unknown")


@pytest.mark.parametrize("actual", [None, ""])
def test_missing_actual_date_does_not_use_separate_years(assess, actual):
    inputs = arguments(actual, "2020-06")
    inputs["vehicle"]["vehicle_profile"].update(production_year=2020, model_year=2020)
    assess(inputs, "unknown")


def test_matching_date_does_not_replace_primary_bound_fitment_evidence(assess):
    inputs = arguments("2020-06-17", "2020-06")
    inputs["evidence"] = []
    response = assess(inputs, "unknown", condition_state="supported")
    assert "primary_bound_fitment_evidence" in response["missing_fields"]


@pytest.mark.parametrize(
    "actual,required,state",
    [
        ("2020-02", {"from": "2020-02-15"}, "unknown"),
        ("2020-02", {"to": "2020-02-15"}, "unknown"),
        ("2020-02", {"from": "2020-02", "to": "2020-02"}, "supported"),
        ("2020-02", {"from": "2020-03-01"}, "rejected"),
        ("2020-02-17", {"from": "2020-02", "to": "2020-02"}, "supported"),
        ("2020-02-17", {"from": "2020-03", "to": "2020-02"}, "unknown"),
        ("2020-02-17", {"from": "2020-01", "extra": True}, "unknown"),
        ("2020-02-17", ["2020-01", "2020-02"], "supported"),
        ("2020-02", ["2020-01-17", "2020-02-17"], "unknown"),
        ("2020-02-17", ["2020-01", "2020-03"], "rejected"),
    ],
)
def test_ranges_and_allowed_alternatives_preserve_unknown_and_rejection(assess, actual, required, state):
    assess(arguments(actual, required), state)


@pytest.mark.parametrize("field", ["production_year", "model_year"])
@pytest.mark.parametrize(
    "required,state", [(2020, "supported"), (2021, "rejected"), ({"from": 2019, "to": 2021}, "supported")]
)
def test_separate_year_fields_keep_their_existing_semantics(assess, field, required, state):
    assess(arguments(2020, required, field=field), state)
