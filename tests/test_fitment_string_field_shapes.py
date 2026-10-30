"""Containers and booleans are not confirmed textual vehicle facts."""

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
        raise AssertionError("supplied fitment conditions must remain offline")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture(scope="module")
def native_tool():
    server = FastMCP("synthetic-fitment-string-shapes")
    register_automotive_tools(server)
    return server._tool_manager.get_tool("assess_part_fitment")


def arguments(field, actual, required, *, carrier="criteria", scope="modification", **profile_fields):
    conditions = {field: required}
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
            "vehicle_profile": {"make": "DEMO", **profile_fields, field: actual},
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
    def call(inputs, state):
        original = deepcopy(inputs)
        if request.param == "direct":
            response = assess_part_fitment(**inputs)
        else:
            wire = {
                key: json.dumps(value) if request.param == "native-json" and isinstance(value, (dict, list)) else value
                for key, value in inputs.items()
            }
            content, response = asyncio.run(native_tool.run(wire, convert_result=True))
            assert json.loads(content[0].text) == response
        assert inputs == original
        data = response["data"]
        assert data["state"] == state
        assert response["outcome"] == ("partial" if state == "unknown" else "success")
        assert response["execution"] == {"network_calls": 0, "attempts": []}
        assert data["conditional_on_caller_evidence"] is True
        assert data["independent_source_verified"] is False
        assert data["exact_applicability_confirmed"] is False
        assert data["ready_for_crm_writeback"] is False
        assert data["part"] == original["part"]
        condition = data["checked_conditions"][0]
        assert condition["state"] == state
        field = condition["field"]
        assert condition["actual"] == original["vehicle"]["vehicle_profile"][field]
        source_condition = not original["criteria"]
        required = original["evidence"][0]["conditions"] if source_condition else original["criteria"]
        assert condition["required"] == required[field]
        assert ("source_index" in condition) == source_condition
        if state == "unknown":
            assert response["missing_fields"] == [field]
            assert response["conflicts"] == []
        elif state == "supported":
            assert response["missing_fields"] == response["conflicts"] == []
        else:
            assert response["missing_fields"] == []
            assert response["conflicts"] == [condition]
        return response

    return call


@pytest.mark.parametrize("carrier", ["criteria", "evidence"])
@pytest.mark.parametrize(
    "field,actual,required",
    [
        ("engine", ["K24", "K20"], "K24"),
        ("engine", ["K20", "K24"], "K24"),
        ("engine", {"K24": "K20"}, "K24"),
        ("engine", [], "K24"),
        ("engine", {}, "K24"),
        ("engine", True, "true"),
        ("engine", False, "false"),
        ("transmission", ["6AT"], "6AT"),
        ("transmission", {"6AT": None}, "6AT"),
        ("model", ["Civic"], "Civic"),
        ("model", 3, "3"),
        ("make", {"DEMO": None}, "DEMO"),
        ("market", {"Europe": None}, "Europe"),
        ("drivetrain", ["4WD"], "AWD"),
    ],
)
def test_non_string_actual_fact_is_unknown(assess, carrier, field, actual, required):
    assess(arguments(field, actual, required, carrier=carrier), "unknown")


@pytest.mark.parametrize("carrier", ["criteria", "evidence"])
@pytest.mark.parametrize(
    "field,actual,required",
    [
        ("engine", "true", True),
        ("engine", "false", False),
        ("model", "3", 3),
        ("engine", "K24", {"code": "K24"}),
        ("engine", "K24", [True, "K20"]),
        ("engine", "K24", [{"code": "K24"}, "K20"]),
    ],
)
def test_non_string_required_fact_is_unknown(assess, carrier, field, actual, required):
    assess(arguments(field, actual, required, carrier=carrier), "unknown")


@pytest.mark.parametrize("scope", ["family", "modification", "exact_identifier"])
def test_primary_evidence_cannot_select_one_engine_from_candidates(assess, scope):
    assess(arguments("engine", ["K24", "K20"], "K24", scope=scope, carrier="evidence"), "unknown")


@pytest.mark.parametrize("carrier", ["criteria", "evidence"])
@pytest.mark.parametrize(
    "field,actual,required,state,extra",
    [
        ("engine", "K24", ["K20", "K24"], "supported", {}),
        ("engine", "K24", [True, "K24"], "supported", {}),
        ("engine", "K24", ["K20", "K30"], "rejected", {}),
        ("engine", "K24", "K24", "supported", {}),
        ("engine", "K24", "K20", "rejected", {}),
        ("transmission", "Automatic", "6AT", "supported", {"transmission_speeds": 6}),
        ("transmission", "Automatic", "6AT", "unknown", {}),
        ("drivetrain", "4WD", "AWD", "supported", {}),
        ("model_year", 2020, {"from": 2019, "to": 2021}, "supported", {}),
        ("displacement_cc", 1998, {"from": 2000}, "rejected", {}),
        ("power_kw", None, {"from": 100}, "unknown", {}),
        ("production_date", "2020-02", "2020-02-15", "unknown", {}),
        ("production_date", "2020-02-15", "2020-02", "supported", {}),
        ("production_date", "2020-02", {"from": "2020-02-01", "to": "2020-02-29"}, "supported", {}),
        ("pr_codes", ["A01"], {"all_of": ["A01"]}, "supported", {}),
        ("pr_codes", ["A01"], {"any_of": ["B02", "A01"]}, "supported", {}),
        ("pr_codes", ["A01"], {"none_of": ["B02"]}, "supported", {"pr_codes_complete": True}),
        ("pr_codes", ["A01"], {"none_of": ["B02"]}, "unknown", {}),
        ("pr_codes", ["A01"], {"none_of": ["A01"]}, "rejected", {}),
        ("custom_flag", True, True, "supported", {}),
    ],
)
def test_alternatives_and_other_condition_types_keep_their_contract(
    assess, carrier, field, actual, required, state, extra
):
    assess(arguments(field, actual, required, carrier=carrier, **extra), state)
