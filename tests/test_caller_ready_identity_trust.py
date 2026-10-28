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
from autostop_manager.tecdoc_vehicle_selection import _independent_values
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


def test_internal_planner_never_converts_input_readiness_to_write_permission(monkeypatch):
    import autostop_manager.catalog_adapters as adapters

    monkeypatch.setattr(adapters, "catalog_provider_status", lambda **_kwargs: {"providers": []})
    monkeypatch.setattr(adapters, "_providers_for_stage", lambda *_args, **_kwargs: [])
    plan = build_oem_parts_provider_plan(
        identifier=_identifier(), requested_part="масляный фильтр", vehicle_identity=_forged_ready()
    )
    assert plan["live_capability"]["identity_ready_for_crm_writeback"] is False
