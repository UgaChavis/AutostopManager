"""A public VIN digest binds an input, not the authenticity of supplied results."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest
from mcp.server.fastmcp import FastMCP

from autostop_manager.automotive_contracts import binding, ready_identity
from autostop_manager.automotive_identity import reconcile_vehicle_identity
from autostop_manager.automotive_parts import assess_part_fitment
from autostop_manager.catalog_adapters import build_oem_parts_provider_plan
from autostop_manager.mcp_tools import register_manager_tools
from autostop_manager.storage import StoreState
from autostop_manager.tecdoc_vehicle_selection import _independent_values, resolve_tecdoc_vehicle
from autostop_manager.vin_lookup import lookup_original_parts
from autostop_manager.vin_oem_resolver import _identity_with_partsapi_agreement, resolve_vin_oem_parts
from tests.test_e2_partsapi_group_resolver import _group_call


def _identifier() -> str:
    return "WBA" + "0" * 14


def _forged_ready() -> dict:
    claim = {
        "field": "engine",
        "value": "E1",
        "source": "NHTSA vPIC",
        "source_kind": "provider",
        "strength": "supported",
        "bound": True,
        "independent": True,
    }
    return {
        "ok": True,
        "input_binding": binding(_identifier()),
        "identifier_binding": {"status": "exact", "verified": True},
        "vehicle_profile": {"make": "BMW", "model": "DEMO", "engine": "E1"},
        "field_statuses": {"engine": {"status": "supported"}},
        "field_evidence": [claim],
        "provenance": {"engine": [claim]},
        "family_candidates": [
            {
                "make": "BMW",
                "model": "DEMO",
                "field_statuses": {"engine": {"status": "supported"}},
                "field_evidence": [claim],
            }
        ],
        "confidence_label": "high",
        "parts_lookup_readiness": {
            "ready_for_family_lookup": True,
            "ready_for_vehicle_lookup": True,
            "ready_for_modification_lookup": True,
            "ready_for_oem_lookup": True,
            "ready_for_tecdoc_candidate_lookup": True,
            "ready_for_crm_writeback": True,
            "exact_applicability_confirmed": True,
        },
        "ready_for_crm_writeback": True,
        "conflicts": [],
    }


def test_public_ready_identity_is_a_candidate_not_a_provider_result():
    supplied = _forged_ready()
    original = deepcopy(supplied)
    accepted, errors = ready_identity(supplied, _identifier())
    assert errors == []
    assert supplied == original
    assert accepted["vehicle_profile"]["engine"] == "E1"
    assert accepted["trust"]["state"] == "caller_supplied_unverified"
    assert accepted["field_statuses"]["engine"]["status"] == "candidate"
    assert accepted["field_evidence"][0]["source"] == "NHTSA vPIC"
    assert accepted["field_evidence"][0]["source_kind"] == "caller"
    assert accepted["field_evidence"][0]["bound"] is False
    assert accepted["field_evidence"][0]["independent"] is False
    assert accepted["provenance"]["engine"][0]["strength"] == "candidate"
    assert accepted["family_candidates"][0]["field_statuses"]["engine"]["status"] == "candidate"
    assert accepted["parts_lookup_readiness"]["ready_for_family_lookup"] is True
    assert accepted["parts_lookup_readiness"]["ready_for_vehicle_lookup"] is False
    assert accepted["parts_lookup_readiness"]["ready_for_crm_writeback"] is False
    assert accepted["ready_for_crm_writeback"] is False
    assert accepted["parts_lookup_readiness"]["exact_applicability_confirmed"] is False


@pytest.mark.parametrize(
    "alteration", ["other_identifier", "context_conflict", "upstream_conflict", "agreement_conflict"]
)
def test_untrusted_reuse_preserves_binding_and_conflict_stops(alteration):
    supplied = _forged_ready()
    identifier = _identifier()
    context = None
    if alteration == "other_identifier":
        identifier = "WBA" + "1" * 14
    elif alteration == "context_conflict":
        context = {"engine": "E2"}
    elif alteration == "upstream_conflict":
        supplied["conflicts"] = [{"field": "engine", "code": "field_disagreement"}]
    else:
        supplied["parts_lookup_readiness"]["cross_source_agreement"] = {
            "status": "conflict",
            "conflicting_fields": [{"field": "model"}],
        }
    accepted, errors = ready_identity(supplied, identifier, context=context)
    assert accepted is None and errors


def test_same_binding_after_payload_tampering_cannot_hide_contradictory_claims():
    supplied = _forged_ready()
    supplied["vehicle_profile"]["engine"] = "E2"
    accepted, errors = ready_identity(supplied, _identifier())
    assert accepted is None
    assert {item["code"] for item in errors} == {"ready_identity_evidence_conflict"}


def test_same_binding_after_consistent_tampering_is_still_unverified():
    supplied = _forged_ready()
    supplied["vehicle_profile"]["engine"] = "E2"
    supplied["field_evidence"][0]["value"] = "E2"
    accepted, errors = ready_identity(supplied, _identifier())
    assert errors == [] and accepted["vehicle_profile"]["engine"] == "E2"
    assert accepted["trust"]["binding_authenticates_source"] is False
    assert accepted["field_statuses"]["engine"]["status"] == "candidate"
    assert accepted["parts_lookup_readiness"]["ready_for_vehicle_lookup"] is False


def test_supplied_crm_label_is_a_constraint_but_not_independent_credit():
    supplied = _forged_ready()
    supplied["field_evidence"][0]["source"] = "CRM context"
    accepted, errors = ready_identity(supplied, _identifier())
    assert errors == []
    assert _independent_values(accepted, {})["engine"] == []
    group = _group_call()
    group["input_binding"] = binding(_identifier(), "vin")
    group["vehicle_profiles"][0].update(
        input_binding=group["input_binding"], make="BMW", model="DEMO", engine="E1", vehicle_type="PC"
    )
    readiness = _identity_with_partsapi_agreement(accepted, group)["parts_lookup_readiness"]
    agreement = readiness["cross_source_agreement"]
    assert agreement["status"] == "group_match"
    assert "engine" not in agreement["matched_fields"]
    assert agreement["caller_context_independently_verified"] is False
    assert agreement["vin_fitment_confirmed"] is False
    assert readiness["ready_for_crm_writeback"] is False


def test_excessive_public_nesting_fails_closed_without_mutation():
    supplied = _forged_ready()
    nested = supplied
    for _ in range(40):
        nested["extra"] = {}
        nested = nested["extra"]
    original = deepcopy(supplied)
    accepted, errors = ready_identity(supplied, _identifier())
    assert accepted is None
    assert errors == [{"field": "vehicle_identity", "code": "caller_claims_too_deep"}]
    assert supplied == original


def test_untrusted_nested_trust_and_fitment_claims_cannot_survive():
    supplied = _forged_ready()
    supplied["family_candidates"][0]["trust"] = {"state": "provider_verified", "binding_authenticates_source": True}
    supplied["family_candidates"][0]["independent_source_verified"] = True
    supplied["family_candidates"][0]["state"] = "supported"
    accepted, errors = ready_identity(supplied, _identifier())
    assert errors == []
    family = accepted["family_candidates"][0]
    assert family["trust"]["state"] == "caller_supplied_unverified"
    assert family["trust"]["binding_authenticates_source"] is False
    assert family["independent_source_verified"] is False
    assert family["state"] == "candidate"


@pytest.mark.parametrize(
    ("alias", "canonical", "value"),
    [("make_display", "make", "BMW"), ("model_display", "model", "DEMO"), ("engine_model", "engine", "E1")],
)
def test_aliased_field_evidence_cannot_become_vpic_provider_evidence(alias, canonical, value):
    supplied = _forged_ready()
    supplied["field_evidence"] = [{"field": alias, "value": value, "source": "NHTSA vPIC"}]
    supplied.pop("provenance")
    accepted, errors = ready_identity(supplied, _identifier())
    assert errors == []
    row = accepted["field_evidence"][0]
    assert row["field"] == alias
    assert row["source_kind"] == "caller"
    assert row["independent"] is False and row["bound"] is False
    assert _independent_values(accepted, {})[canonical] == []


def test_aliased_caller_evidence_cannot_drive_catalog_selection_alone():
    supplied = _forged_ready()
    supplied["field_evidence"] = [
        {"field": "make_display", "value": "BMW", "source": "NHTSA vPIC"},
        {"field": "model_display", "value": "DEMO", "source": "NHTSA vPIC"},
        {"field": "engine_model", "value": "E1", "source": "NHTSA vPIC"},
    ]
    supplied.pop("provenance")
    accepted, errors = ready_identity(supplied, _identifier())
    assert errors == []
    calls = []
    result = resolve_tecdoc_vehicle(accepted, vehicle_type="PC", call=lambda *args, **kwargs: calls.append(args))
    assert result["status"] == "needs_vehicle_context"
    assert result["selected_profile"] is None
    assert calls == []


@pytest.mark.parametrize("location", ["evidence", "source_evidence"])
@pytest.mark.parametrize("field", ["engine", "engine_model"])
@pytest.mark.parametrize("placement", ["flat", "inner", "outer"])
def test_all_public_evidence_containers_detect_profile_conflict(location, field, placement):
    supplied = _forged_ready()
    evidence = [{"field": field, "value": "E2", "source": "NHTSA vPIC"}]
    if placement == "inner":
        supplied[location] = evidence
        supplied = {"data": supplied}
    elif placement == "outer":
        supplied = {"data": supplied, location: evidence}
    else:
        supplied[location] = evidence
    accepted, errors = ready_identity(supplied, _identifier())
    assert accepted is None
    assert any(row["field"] == "engine" and row["code"] == "ready_identity_evidence_conflict" for row in errors)
    merged = reconcile_vehicle_identity(_identifier(), [supplied])
    assert merged["data"]["field_statuses"]["engine"] == "disputed"
    assert "engine" not in merged["data"]["vehicle_profile"]


def test_aliased_disputed_field_does_not_become_a_supported_canonical_field():
    supplied = _forged_ready()
    supplied["field_statuses"] = {"engine_model": {"status": "disputed"}}
    accepted, errors = ready_identity(supplied, _identifier())
    assert accepted is None
    assert {row["field"] for row in errors if row["code"] == "disputed_ready_identity"} == {"engine"}
    merged = reconcile_vehicle_identity(_identifier(), [supplied])
    assert merged["data"]["field_statuses"]["engine"] == "disputed"
    assert "engine" not in merged["data"]["vehicle_profile"]


def test_alternate_variant_evidence_is_not_misclassified_as_root_vehicle_fact():
    supplied = _forged_ready()
    supplied["source_evidence"] = [{"field": "engine", "value": "E2", "source": "NHTSA vPIC"}]
    supplied["variants"] = [
        {
            "profile": {"engine": "E2"},
            "field_evidence_refs": [{"path": "data.source_evidence", "index": 0}],
        }
    ]
    supplied["family_candidate_refs"] = [0]
    accepted, errors = ready_identity(supplied, _identifier())
    assert errors == []
    assert accepted["vehicle_profile"]["engine"] == "E1"
    merged = reconcile_vehicle_identity(_identifier(), [supplied])
    assert merged["data"]["vehicle_profile"]["engine"] == "E1"
    assert any(row.get("profile", {}).get("engine") == "E2" for row in merged["data"]["variants"])


def test_conflicting_evidence_inside_one_variant_remains_a_conflict():
    supplied = _forged_ready()
    supplied["variants"] = [{"profile": {"engine": "E2"}, "field_evidence": [{"field": "engine_model", "value": "E3"}]}]
    accepted, errors = ready_identity(supplied, _identifier())
    assert accepted is None
    assert any(row["field"] == "engine" and row["code"] == "ready_identity_evidence_conflict" for row in errors)


def test_engine_description_and_exact_code_remain_incomparable_not_a_false_conflict():
    supplied = _forged_ready()
    supplied["vehicle_profile"].update(engine="3.0 TDI", engine_code="CRCA")
    supplied.pop("field_evidence")
    supplied.pop("provenance")
    accepted, errors = ready_identity(supplied, _identifier())
    assert errors == []
    assert accepted["vehicle_profile"]["engine"] == "3.0 TDI"
    assert accepted["vehicle_profile"]["engine_code"] == "CRCA"


def test_conflicting_exact_engine_code_aliases_remain_a_conflict():
    supplied = _forged_ready()
    supplied["vehicle_profile"].update(engine="CRCA", engine_code="WRONG")
    supplied.pop("field_evidence")
    supplied.pop("provenance")
    accepted, errors = ready_identity(supplied, _identifier())
    assert accepted is None
    assert any(row["field"] == "engine" and row["code"] == "ready_identity_evidence_conflict" for row in errors)


def test_source_label_on_unknown_field_is_still_caller_supplied():
    supplied = _forged_ready()
    supplied["field_evidence"].append({"field": "future_field", "value": "claim", "source": "NHTSA vPIC"})
    accepted, errors = ready_identity(supplied, _identifier())
    assert errors == []
    claim = accepted["field_evidence"][-1]
    assert claim["source_kind"] == "caller"
    assert claim["independent"] is False and claim["bound"] is False


@pytest.mark.parametrize(
    "flag",
    ["exact_vehicle_confirmed", "epc_confirmed", "oem_number_confirmed", "anything_verified", "custom_authenticated"],
)
def test_any_claimed_confirmation_flag_on_caller_evidence_is_false(flag):
    supplied = _forged_ready()
    supplied["field_evidence"][0][flag] = True
    accepted, errors = ready_identity(supplied, _identifier())
    assert errors == []
    assert accepted["field_evidence"][0][flag] is False


def test_spoofed_ready_values_do_not_survive_resolver_as_ready_or_writeable():
    result = resolve_vin_oem_parts(
        identifier=_identifier(),
        requested_part="масляный фильтр",
        vehicle_identity=_forged_ready(),
        live_vpic=False,
        live_partsapi_identity=False,
        live_partsapi_oem=False,
        dry_run=True,
    )
    identity = result["identity"]
    assert identity["vehicle_profile"]["engine"] == "E1"
    assert identity["field_statuses"]["engine"]["status"] == "candidate"
    assert identity["trust"]["state"] == "caller_supplied_unverified"
    assert identity["ready_for_vehicle_lookup"] is False
    assert identity["ready_for_crm_writeback"] is False
    assert result["readiness"]["ready_for_crm_writeback"] is False
    assert result["live_call_count"] == 0
    assert _identifier() not in json.dumps(result, ensure_ascii=False)


def test_lookup_original_parts_does_not_relabel_supplied_profile_as_decoded_vehicle():
    result = lookup_original_parts(
        _identifier(),
        vehicle_identity=_forged_ready(),
        live_vpic=False,
        part_name="фильтр",
        captured_oem_number="DEMO12345",
        captured_source="BMW AIR/ETK via AOS",
    )
    assert not result["decoded_vehicle"]
    assert result["catalog_vehicle"]["make"] == "BMW"
    assert result["catalog_vehicle"]["source"] == "caller_supplied_unverified"
    assert result["fitment_confidence"]["level"] == "blocked"
    assert result["oem_candidates"][0]["confidence"] == "low"


@pytest.mark.parametrize("detail", ["full", "summary"])
def test_reconcile_preserves_untrusted_claims_without_promoting_them(detail):
    supplied = _forged_ready()
    result = reconcile_vehicle_identity(_identifier(), [supplied], detail=detail)
    data = result["data"]
    assert data["vehicle_profile"]["engine"] == "E1"
    assert data["field_statuses"]["engine"] == "candidate"
    assert data["trust"]["state"] == "caller_supplied_unverified"
    assert data["provenance"]["engine"][0]["independent"] is False
    assert data["provenance"]["engine"][0]["strength"] == "candidate"
    assert data["parts_lookup_readiness"]["ready_for_crm_writeback"] is False
    assert data["parts_lookup_readiness"]["ready_for_vehicle_lookup"] is False
    reused = reconcile_vehicle_identity(_identifier(), [result], detail=detail)
    assert reused["data"]["field_statuses"]["engine"] == "candidate"


def test_reconciliation_keeps_mismatching_caller_facts_as_a_conflict():
    first = _forged_ready()
    second = deepcopy(first)
    second["vehicle_profile"]["engine"] = "E2"
    result = reconcile_vehicle_identity(_identifier(), [first, second])
    assert result["data"]["field_statuses"]["engine"] == "disputed"
    assert "engine" not in result["data"]["vehicle_profile"]
    assert any(item["field"] == "engine" for item in result["conflicts"])


def test_pure_fitment_supported_is_explicitly_conditional_on_supplied_evidence():
    bound = binding(_identifier())
    evidence = [
        {
            "provider": "DEMO",
            "primary_lineage": "DEMO",
            "method": "epc",
            "locator": "https://example.com/epc",
            "fetched_at": "2026-01-01T00:00:00Z",
            "document_kind": "official_epc",
            "fitment_assertion": True,
            "part_number": "DEMO1",
            "brand": "DEMO",
            "scope": "exact_identifier",
            "identifier_binding": bound,
        }
    ]
    result = assess_part_fitment(
        {"input_binding": bound, "vehicle_profile": {"engine": "E1"}},
        {"number": "DEMO1", "brand": "DEMO"},
        {"engine": "E1"},
        evidence,
        "exact_identifier",
    )
    assert result["data"]["state"] == "supported"
    assert result["data"]["conditional_on_caller_evidence"] is True
    assert result["data"]["independent_source_verified"] is False
    assert result["data"]["exact_applicability_confirmed"] is False
    assert result["data"]["ready_for_crm_writeback"] is False
    assert result["execution"]["network_calls"] == 0
    assert result["warnings"]


@pytest.mark.parametrize("state", ["unknown", "conflict"])
def test_pure_fitment_keeps_conditional_marker_for_missing_and_conflicted_inputs(state):
    vehicle = {"vehicle_profile": {"make": "DEMO"}}
    if state == "conflict":
        vehicle["field_statuses"] = {"engine": {"status": "disputed"}}
    result = assess_part_fitment(vehicle, {"number": "DEMO1", "brand": "DEMO"}, {"engine": "E1"}, [])
    assert result["data"]["state"] == state
    assert result["data"]["conditional_on_caller_evidence"] is True
    assert result["data"]["independent_source_verified"] is False
    assert result["data"]["exact_applicability_confirmed"] is False
    assert result["data"]["ready_for_crm_writeback"] is False
    assert result["warnings"]


def test_public_planner_cannot_promote_supplied_ready_or_grant_writeback(tmp_path, monkeypatch):
    import autostop_manager.catalog_adapters as adapters

    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setattr(adapters, "catalog_provider_status", lambda **_kwargs: {"providers": []})
    monkeypatch.setattr(adapters, "_providers_for_stage", lambda *_args, **_kwargs: [])
    server = FastMCP("isolated-caller-ready-test")
    register_manager_tools(
        server, StoreState(tmp_path / "synthetic.sqlite3"), include_tools={"plan_oem_parts_providers"}
    )
    supplied = _forged_ready()
    original = deepcopy(supplied)
    tool = server._tool_manager.get_tool("plan_oem_parts_providers")
    assert {"identifier", "requested_part", "vehicle_identity", "city"} <= set(tool.parameters["properties"])
    original_schema = FastMCP("original-planner-schema")
    original_schema.tool(name="plan_oem_parts_providers")(build_oem_parts_provider_plan)
    assert tool.parameters == original_schema._tool_manager.get_tool("plan_oem_parts_providers").parameters
    plan = asyncio.run(
        tool.run({"identifier": _identifier(), "requested_part": "масляный фильтр", "vehicle_identity": supplied})
    )
    assert supplied == original
    assert plan["trust"]["state"] == "caller_supplied_unverified"
    assert plan["vehicle_profile"]["engine"] == "E1"
    assert plan["field_statuses"]["engine"]["status"] == "candidate"
    assert plan["lookup_scope"] == "family"
    assert plan["live_capability"]["identity_ready_for_vehicle_lookup"] is False
    assert plan["live_capability"]["identity_ready_for_crm_writeback"] is False


@pytest.mark.parametrize(
    "problem",
    [
        "foreign_binding",
        "outer_foreign_binding",
        "evidence_conflict",
        "agreement_conflict",
        "profile_list",
        "profile_string",
        "profile_number",
    ],
)
def test_public_planner_rejects_invalid_identity_before_provider_status(tmp_path, monkeypatch, problem):
    import autostop_manager.mcp_tools as mcp_tools

    def forbidden_planner(**_kwargs):
        raise AssertionError("invalid identity must not reach provider planning")

    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setattr(mcp_tools, "build_oem_parts_provider_plan", forbidden_planner)
    server = FastMCP("invalid-caller-ready-test")
    register_manager_tools(
        server, StoreState(tmp_path / "synthetic.sqlite3"), include_tools={"plan_oem_parts_providers"}
    )
    supplied = _forged_ready()
    if problem == "foreign_binding":
        supplied["input_binding"] = binding("WBA" + "1" * 14)
    elif problem == "outer_foreign_binding":
        supplied["input_binding"] = None
        supplied = {"data": supplied, "input_binding": binding("WBA" + "1" * 14)}
    elif problem == "evidence_conflict":
        supplied["evidence"] = [{"field": "engine_model", "value": "E2", "source": "NHTSA vPIC"}]
    elif problem == "agreement_conflict":
        supplied["parts_lookup_readiness"]["cross_source_agreement"] = {"status": "conflict"}
    else:
        supplied["vehicle_profile"] = {"profile_list": [], "profile_string": "bad", "profile_number": 7}[problem]
    tool = server._tool_manager.get_tool("plan_oem_parts_providers")
    plan = asyncio.run(
        tool.run({"identifier": _identifier(), "requested_part": "масляный фильтр", "vehicle_identity": supplied})
    )
    assert plan["ok"] is False
    assert plan["lookup_scope"] == "blocked"
    assert plan["identity_errors"]
    expected = {
        "foreign_binding": "ready_identity_binding_mismatch",
        "outer_foreign_binding": "ready_identity_binding_mismatch",
        "evidence_conflict": "ready_identity_evidence_conflict",
        "agreement_conflict": "unresolved_identity_agreement",
        "profile_list": "invalid_vehicle_profile",
        "profile_string": "invalid_vehicle_profile",
        "profile_number": "invalid_vehicle_profile",
    }
    assert expected[problem] in {row["code"] for row in plan["identity_errors"]}
    assert plan["live_capability"]["identity_ready_for_vehicle_lookup"] is False
    assert plan["live_capability"]["identity_ready_for_crm_writeback"] is False
    assert plan["manual_public_search_queries"] == []
    assert plan["execution"]["network_calls"] == 0
    assert all(value is False for value in plan["live_capability"].values())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("make", ["private-invalid-value"]),
        ("model", ["private-invalid-value"]),
        ("model", {"secret": "private-invalid-value"}),
        ("model", True),
        ("engine", ["private-invalid-value"]),
        ("model_year", True),
        ("displacement_cc", -1),
        ("power_hp", float("inf")),
        ("transmission_speeds", 31),
        ("production_date", "private-invalid-value"),
        ("options", "private-invalid-value"),
        ("make_display", {"secret": "private-invalid-value"}),
        ("engine_model", ["private-invalid-value"]),
        ("engine_code", ["private-invalid-value"]),
        ("horsepower", -1),
        ("model_family", ["private-invalid-value"]),
        ("platform", {"secret": "private-invalid-value"}),
        ("series2", True),
    ],
)
def test_public_planner_rejects_bad_profile_fields_before_planning(tmp_path, monkeypatch, field, value):
    import autostop_manager.mcp_tools as mcp_tools

    def forbidden_planner(**_kwargs):
        raise AssertionError("invalid profile must not reach provider planning")

    monkeypatch.setattr(mcp_tools, "build_oem_parts_provider_plan", forbidden_planner)
    server = FastMCP("invalid-profile-fields-test")
    register_manager_tools(
        server, StoreState(tmp_path / "synthetic.sqlite3"), include_tools={"plan_oem_parts_providers"}
    )
    profile = {"make": "BMW", "model": "DEMO", "engine": "E1", field: value}
    result = asyncio.run(
        server._tool_manager.get_tool("plan_oem_parts_providers").run(
            {"identifier": "", "requested_part": "фильтр", "vehicle_identity": {"vehicle_profile": profile}}
        )
    )
    assert result["ok"] is False
    assert result["lookup_scope"] == "blocked"
    assert result["manual_public_search_queries"] == []
    assert result["execution"]["network_calls"] == 0
    assert all(value is False for value in result["live_capability"].values())
    assert "private-invalid-value" not in json.dumps(result)


@pytest.mark.parametrize("carrier", ["vehicle_profile", "vehicle_profile_compact", "crm_vehicle_profile"])
def test_bad_nested_profile_field_is_not_hidden_by_a_valid_outer_field(tmp_path, monkeypatch, carrier):
    import autostop_manager.mcp_tools as mcp_tools

    def forbidden_planner(**_kwargs):
        raise AssertionError("invalid nested profile must not reach provider planning")

    monkeypatch.setattr(mcp_tools, "build_oem_parts_provider_plan", forbidden_planner)
    server = FastMCP("invalid-nested-profile-test")
    register_manager_tools(
        server, StoreState(tmp_path / "synthetic.sqlite3"), include_tools={"plan_oem_parts_providers"}
    )
    profile = {"make": "BMW", "model": "DEMO", carrier: {"model": ["private-invalid-value"]}}
    result = asyncio.run(
        server._tool_manager.get_tool("plan_oem_parts_providers").run(
            {"identifier": "", "requested_part": "фильтр", "vehicle_identity": {"vehicle_profile": profile}}
        )
    )
    assert result["ok"] is False
    assert result["lookup_scope"] == "blocked"
    assert result["execution"]["network_calls"] == 0
    assert "private-invalid-value" not in json.dumps(result)


def test_public_planner_normalizes_supported_aliases_without_inventing_vehicle_fields(tmp_path, monkeypatch):
    import autostop_manager.catalog_adapters as adapters

    monkeypatch.setattr(adapters, "catalog_provider_status", lambda **_kwargs: {"providers": []})
    monkeypatch.setattr(adapters, "_providers_for_stage", lambda *_args, **_kwargs: [])
    server = FastMCP("profile-aliases-test")
    register_manager_tools(
        server, StoreState(tmp_path / "synthetic.sqlite3"), include_tools={"plan_oem_parts_providers"}
    )
    supplied = {
        "vehicle_profile": {
            "make_display": " BMW ",
            "model_display": " DEMO ",
            "engine_model": " E1 ",
            "model_year": "2010",
            "horsepower": 245,
        }
    }
    original = deepcopy(supplied)
    result = asyncio.run(
        server._tool_manager.get_tool("plan_oem_parts_providers").run(
            {"identifier": "", "requested_part": "фильтр", "vehicle_identity": supplied}
        )
    )
    assert supplied == original
    assert result["ok"] is True
    assert result["lookup_scope"] == "family"
    assert result["vehicle_profile"] == {"make": "BMW", "model": "DEMO", "model_year": 2010, "engine": "E1"}
    assert result["trust"]["state"] == "caller_supplied_unverified"
    assert result["live_capability"]["identity_ready_for_crm_writeback"] is False


@pytest.mark.parametrize(
    "families",
    [
        "private-invalid-value",
        [True],
        [{"make": ["private-invalid-value"], "model": "DEMO"}],
        [{"make": "BMW", "model_family": {"private": "private-invalid-value"}}],
        [{"make": "BMW", "model": "DEMO", "model_display": "OTHER"}],
    ],
)
def test_bad_family_candidates_fail_before_planning(tmp_path, monkeypatch, families):
    import autostop_manager.mcp_tools as mcp_tools

    def forbidden_planner(**_kwargs):
        raise AssertionError("invalid family candidates must not reach provider planning")

    monkeypatch.setattr(mcp_tools, "build_oem_parts_provider_plan", forbidden_planner)
    server = FastMCP("invalid-family-test")
    register_manager_tools(
        server, StoreState(tmp_path / "synthetic.sqlite3"), include_tools={"plan_oem_parts_providers"}
    )
    result = asyncio.run(
        server._tool_manager.get_tool("plan_oem_parts_providers").run(
            {"identifier": "", "requested_part": "фильтр", "vehicle_identity": {"family_candidates": families}}
        )
    )
    assert result["ok"] is False
    assert result["lookup_scope"] == "blocked"
    assert result["manual_public_search_queries"] == []
    assert result["execution"]["network_calls"] == 0
    assert "private-invalid-value" not in json.dumps(result)


def test_incomplete_family_candidate_cannot_create_family_readiness(tmp_path, monkeypatch):
    import autostop_manager.catalog_adapters as adapters

    monkeypatch.setattr(adapters, "catalog_provider_status", lambda **_kwargs: {"providers": []})
    monkeypatch.setattr(adapters, "_providers_for_stage", lambda *_args, **_kwargs: [])
    server = FastMCP("incomplete-family-test")
    register_manager_tools(
        server, StoreState(tmp_path / "synthetic.sqlite3"), include_tools={"plan_oem_parts_providers"}
    )
    result = asyncio.run(
        server._tool_manager.get_tool("plan_oem_parts_providers").run(
            {"identifier": "", "requested_part": "фильтр", "vehicle_identity": {"family_candidates": [{}]}}
        )
    )
    assert result["ok"] is True
    assert result["lookup_scope"] == "blocked"
    assert result["live_capability"]["identity_ready_for_family_lookup"] is False


def test_family_candidate_aliases_are_normalized_without_fabricating_profile(tmp_path, monkeypatch):
    import autostop_manager.catalog_adapters as adapters

    monkeypatch.setattr(adapters, "catalog_provider_status", lambda **_kwargs: {"providers": []})
    monkeypatch.setattr(adapters, "_providers_for_stage", lambda *_args, **_kwargs: [])
    server = FastMCP("family-aliases-test")
    register_manager_tools(
        server, StoreState(tmp_path / "synthetic.sqlite3"), include_tools={"plan_oem_parts_providers"}
    )
    result = asyncio.run(
        server._tool_manager.get_tool("plan_oem_parts_providers").run(
            {
                "identifier": "",
                "requested_part": "фильтр",
                "vehicle_identity": {"family_candidates": [{"make_display": " BMW ", "model_display": " DEMO "}]},
            }
        )
    )
    assert result["ok"] is True
    assert result["lookup_scope"] == "family"
    assert result["vehicle_profile"] == {}
    assert result["family_candidates"][0]["make"] == "BMW"
    assert result["family_candidates"][0]["model"] == "DEMO"
    assert result["live_capability"]["identity_ready_for_crm_writeback"] is False


def test_nested_compact_profile_aliases_are_normalized_without_fabricating_facts(tmp_path, monkeypatch):
    import autostop_manager.catalog_adapters as adapters

    monkeypatch.setattr(adapters, "catalog_provider_status", lambda **_kwargs: {"providers": []})
    monkeypatch.setattr(adapters, "_providers_for_stage", lambda *_args, **_kwargs: [])
    server = FastMCP("nested-profile-aliases-test")
    register_manager_tools(
        server, StoreState(tmp_path / "synthetic.sqlite3"), include_tools={"plan_oem_parts_providers"}
    )
    supplied = {
        "vehicle_profile": {
            "vehicle_profile_compact": json.dumps(
                {"make_display": " BMW ", "model_display": " DEMO ", "engine_code": " E1 "}
            )
        }
    }
    result = asyncio.run(
        server._tool_manager.get_tool("plan_oem_parts_providers").run(
            {"identifier": "", "requested_part": "фильтр", "vehicle_identity": supplied}
        )
    )
    assert result["ok"] is True
    assert result["lookup_scope"] == "family"
    assert result["vehicle_profile"] == {"make": "BMW", "model": "DEMO", "engine": "E1"}
    assert result["live_capability"]["identity_ready_for_crm_writeback"] is False


@pytest.mark.parametrize("carrier", ["vehicle_profile", "family_candidates"])
@pytest.mark.parametrize("field", ["make", "model", "model_family"])
def test_whitespace_only_vehicle_fields_do_not_create_family_readiness(tmp_path, monkeypatch, carrier, field):
    import autostop_manager.catalog_adapters as adapters

    monkeypatch.setattr(adapters, "catalog_provider_status", lambda **_kwargs: {"providers": []})
    monkeypatch.setattr(adapters, "_providers_for_stage", lambda *_args, **_kwargs: [])
    server = FastMCP("blank-family-test")
    register_manager_tools(
        server, StoreState(tmp_path / "synthetic.sqlite3"), include_tools={"plan_oem_parts_providers"}
    )
    profile = {"make": "BMW", "model": "DEMO"}
    if field == "model_family":
        profile.pop("model")
    profile[field] = " \t\u00a0 "
    identity = {carrier: profile if carrier == "vehicle_profile" else [profile]}
    result = asyncio.run(
        server._tool_manager.get_tool("plan_oem_parts_providers").run(
            {"identifier": "", "requested_part": "фильтр", "vehicle_identity": identity}
        )
    )
    assert result["ok"] is True
    assert result["lookup_scope"] == "blocked"
    assert result["live_capability"]["identity_ready_for_family_lookup"] is False
    stored = result["vehicle_profile"] if carrier == "vehicle_profile" else result["family_candidates"][0]
    assert field not in stored


@pytest.mark.parametrize("carrier", ["vehicle_profile", "family_candidates"])
def test_blank_model_does_not_hide_an_explicit_model_family(tmp_path, monkeypatch, carrier):
    import autostop_manager.catalog_adapters as adapters

    monkeypatch.setattr(adapters, "catalog_provider_status", lambda **_kwargs: {"providers": []})
    monkeypatch.setattr(adapters, "_providers_for_stage", lambda *_args, **_kwargs: [])
    server = FastMCP("blank-model-fallback-test")
    register_manager_tools(
        server, StoreState(tmp_path / "synthetic.sqlite3"), include_tools={"plan_oem_parts_providers"}
    )
    profile = {"make": "BMW", "model": "  ", "model_family": "DEMO"}
    identity = {carrier: profile if carrier == "vehicle_profile" else [profile]}
    result = asyncio.run(
        server._tool_manager.get_tool("plan_oem_parts_providers").run(
            {"identifier": "", "requested_part": "фильтр", "vehicle_identity": identity}
        )
    )
    assert result["ok"] is True
    assert result["lookup_scope"] == "family"
    stored = result["vehicle_profile"] if carrier == "vehicle_profile" else result["family_candidates"][0]
    assert "model" not in stored
    assert stored["model_family"] == "DEMO"
    assert any("DEMO" in query["query"] for query in result["manual_public_search_queries"])


@pytest.mark.parametrize("carrier", ["vehicle_profile", "vehicle_profile_compact", "crm_vehicle_profile"])
@pytest.mark.parametrize("field", ["model", "engine"])
@pytest.mark.parametrize("outer", ["vehicle_profile", "family_candidates"])
def test_nested_conflicting_facts_stop_planner_before_provider_status(tmp_path, monkeypatch, carrier, field, outer):
    import autostop_manager.mcp_tools as mcp_tools

    def forbidden_planner(**_kwargs):
        raise AssertionError("conflicting nested profile must not reach provider planning")

    monkeypatch.setattr(mcp_tools, "build_oem_parts_provider_plan", forbidden_planner)
    server = FastMCP("nested-contradiction-test")
    register_manager_tools(
        server, StoreState(tmp_path / "synthetic.sqlite3"), include_tools={"plan_oem_parts_providers"}
    )
    nested = {field: "OTHER"}
    profile = {
        "make": "BMW",
        "model": "DEMO",
        "engine": "E1",
        carrier: json.dumps(nested) if carrier == "vehicle_profile_compact" else nested,
    }
    identity = {outer: profile if outer == "vehicle_profile" else [profile]}
    result = asyncio.run(
        server._tool_manager.get_tool("plan_oem_parts_providers").run(
            {"identifier": "", "requested_part": "фильтр", "vehicle_identity": identity}
        )
    )
    assert result["ok"] is False
    assert result["lookup_scope"] == "blocked"
    assert result["execution"]["network_calls"] == 0
    assert any(
        row["field"] == field and row["code"] == "ready_identity_evidence_conflict" for row in result["identity_errors"]
    )


def test_sibling_nested_alias_contradiction_blocks_before_planning(tmp_path, monkeypatch):
    import autostop_manager.mcp_tools as mcp_tools

    def forbidden_planner(**_kwargs):
        raise AssertionError("conflicting sibling profiles must not reach provider planning")

    monkeypatch.setattr(mcp_tools, "build_oem_parts_provider_plan", forbidden_planner)
    server = FastMCP("sibling-contradiction-test")
    register_manager_tools(
        server, StoreState(tmp_path / "synthetic.sqlite3"), include_tools={"plan_oem_parts_providers"}
    )
    profile = {"make": "BMW", "vehicle_profile": {"model_display": "DEMO"}, "crm_vehicle_profile": {"model": "OTHER"}}
    result = asyncio.run(
        server._tool_manager.get_tool("plan_oem_parts_providers").run(
            {"identifier": "", "requested_part": "фильтр", "vehicle_identity": {"vehicle_profile": profile}}
        )
    )
    assert result["ok"] is False
    assert result["lookup_scope"] == "blocked"
    assert any(
        row["field"] == "model" and row["code"] == "ready_identity_evidence_conflict"
        for row in result["identity_errors"]
    )


@pytest.mark.parametrize(
    ("alias", "outer", "field", "inner"),
    [
        ("model_display", "DEMO", "model", "OTHER"),
        ("engine_model", "E1", "engine", "E2"),
        ("engine_code", "E1", "engine", "E2"),
    ],
)
def test_outer_alias_cannot_hide_conflicting_nested_canonical_fact(tmp_path, monkeypatch, alias, outer, field, inner):
    import autostop_manager.mcp_tools as mcp_tools

    def forbidden_planner(**_kwargs):
        raise AssertionError("conflicting aliases must not reach provider planning")

    monkeypatch.setattr(mcp_tools, "build_oem_parts_provider_plan", forbidden_planner)
    server = FastMCP("outer-alias-contradiction-test")
    register_manager_tools(
        server, StoreState(tmp_path / "synthetic.sqlite3"), include_tools={"plan_oem_parts_providers"}
    )
    profile = {"make": "BMW", alias: outer, "vehicle_profile": {field: inner}}
    result = asyncio.run(
        server._tool_manager.get_tool("plan_oem_parts_providers").run(
            {"identifier": "", "requested_part": "фильтр", "vehicle_identity": {"vehicle_profile": profile}}
        )
    )
    assert result["ok"] is False
    assert result["lookup_scope"] == "blocked"
    assert result["execution"]["network_calls"] == 0
    assert any(
        row["field"] == field and row["code"] == "ready_identity_evidence_conflict" for row in result["identity_errors"]
    )


def test_equivalent_nested_aliases_and_year_still_plan_without_writeback(tmp_path, monkeypatch):
    import autostop_manager.catalog_adapters as adapters

    monkeypatch.setattr(adapters, "catalog_provider_status", lambda **_kwargs: {"providers": []})
    monkeypatch.setattr(adapters, "_providers_for_stage", lambda *_args, **_kwargs: [])
    server = FastMCP("nested-agreement-test")
    register_manager_tools(
        server, StoreState(tmp_path / "synthetic.sqlite3"), include_tools={"plan_oem_parts_providers"}
    )
    profile = {
        "make": "BMW",
        "model_display": " DEMO ",
        "model_year": "2010",
        "vehicle_profile_compact": {"make_display": "bmw", "model": "demo", "model_year": 2010},
    }
    result = asyncio.run(
        server._tool_manager.get_tool("plan_oem_parts_providers").run(
            {"identifier": "", "requested_part": "фильтр", "vehicle_identity": {"vehicle_profile": profile}}
        )
    )
    assert result["ok"] is True
    assert result["lookup_scope"] == "family"
    assert result["vehicle_profile"]["model_year"] == 2010
    assert result["live_capability"]["identity_ready_for_crm_writeback"] is False


@pytest.mark.parametrize(
    ("field", "outer", "nested"),
    [("power_hp", 245, 245.0), ("production_date", "2020-06", "2020-06-17")],
)
def test_equivalent_nested_numeric_or_partial_date_facts_remain_valid(tmp_path, monkeypatch, field, outer, nested):
    import autostop_manager.catalog_adapters as adapters

    monkeypatch.setattr(adapters, "catalog_provider_status", lambda **_kwargs: {"providers": []})
    monkeypatch.setattr(adapters, "_providers_for_stage", lambda *_args, **_kwargs: [])
    server = FastMCP("nested-compatible-facts-test")
    register_manager_tools(
        server, StoreState(tmp_path / "synthetic.sqlite3"), include_tools={"plan_oem_parts_providers"}
    )
    profile = {"make": "BMW", "model": "DEMO", field: outer, "vehicle_profile": {field: nested}}
    result = asyncio.run(
        server._tool_manager.get_tool("plan_oem_parts_providers").run(
            {"identifier": "", "requested_part": "фильтр", "vehicle_identity": {"vehicle_profile": profile}}
        )
    )
    assert result["ok"] is True
    assert result["lookup_scope"] == "family"
    assert result["live_capability"]["identity_ready_for_crm_writeback"] is False


def test_internal_planner_never_converts_input_readiness_to_write_permission(monkeypatch):
    import autostop_manager.catalog_adapters as adapters

    monkeypatch.setattr(adapters, "catalog_provider_status", lambda **_kwargs: {"providers": []})
    monkeypatch.setattr(adapters, "_providers_for_stage", lambda *_args, **_kwargs: [])
    plan = build_oem_parts_provider_plan(
        identifier=_identifier(), requested_part="масляный фильтр", vehicle_identity=_forged_ready()
    )
    assert plan["live_capability"]["identity_ready_for_crm_writeback"] is False
