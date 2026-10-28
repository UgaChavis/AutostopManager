"""Synthetic decode/reuse and malformed-profile boundaries, without providers."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import socket

import pytest
from mcp.server.fastmcp import FastMCP

from autostop_manager import vehicle_identity
from autostop_manager import automotive_identity
from autostop_manager import catalog_adapters
from autostop_manager.automotive_contracts import binding, ready_identity
from autostop_manager.automotive_contracts import result as automotive_result
from autostop_manager.automotive_identity import reconcile_vehicle_identity
from autostop_manager.mcp_tools import register_manager_tools
from autostop_manager.storage import StoreState
from autostop_manager.vehicle_identity_inputs import validate_identity_input


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("profile validation/reuse must not acquire provider data")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(vehicle_identity, "catalog_provider_status", lambda: {"providers": []})


@pytest.mark.parametrize(
    ("prefix", "context", "decoded", "expected_field", "expected_value"),
    [
        ("XW8AC2NH9JK", {"make": "Skoda", "model": "Rapid"}, {}, "make", "Skoda"),
        (
            "JHL",
            None,
            {"make": "Honda", "model": "CR-V", "vehicletype": "MULTIPURPOSE PASSENGER VEHICLE (MPV)"},
            "vehicle_type",
            "MULTIPURPOSE PASSENGER VEHICLE (MPV)",
        ),
    ],
)
def test_decoder_weak_hints_survive_ready_identity_and_reconciliation(
    prefix, context, decoded, expected_field, expected_value
):
    identifier = prefix + "0" * (17 - len(prefix))
    profile = vehicle_identity.decode_vehicle_identity(
        identifier,
        crm_context=context,
        live_vpic=False,
        live_wmi=False,
        vpic_result={
            "ok": True,
            "error_code": "0",
            "vin": identifier,
            "vehicle": {"vin": identifier, **decoded},
        },
    )
    assert profile["conflicts"] == []
    assert profile["vehicle_profile"][expected_field] == expected_value
    before = deepcopy(profile)
    accepted, errors = ready_identity(profile, identifier)
    assert errors == [] and accepted is not None
    assert profile == before
    assert accepted["trust"]["state"] == "caller_supplied_unverified"
    assert accepted["parts_lookup_readiness"]["ready_for_crm_writeback"] is False
    for supplied in (profile, accepted):
        for _round in range(3):
            reconciled = reconcile_vehicle_identity(identifier, [supplied], detail="summary")
            assert reconciled["conflicts"] == []
            assert reconciled["data"]["vehicle_profile"][expected_field] == expected_value
            again, errors = ready_identity(reconciled, identifier)
            assert errors == [] and again is not None
            assert again["parts_lookup_readiness"]["ready_for_crm_writeback"] is False
            supplied = again


@pytest.mark.parametrize("evidence_field", ["model", "model_display"])
def test_low_rule_provenance_inherits_only_its_matching_evidence_before_repeated_reuse(evidence_field):
    identifier = "XW8AC2NH" + "0" * 9
    decoded = vehicle_identity.decode_vehicle_identity(
        identifier,
        live_vpic=False,
        live_wmi=False,
        vpic_result={
            "ok": True,
            "error_code": "0",
            "vin": identifier,
            "vehicle": {"vin": identifier, "make": "Skoda", "model": "Provider model"},
        },
    )
    assert decoded["conflicts"] == []
    hint = next(item for item in decoded["field_evidence"] if item.get("value") == "Rapid")
    hint["field"] = evidence_field
    origin = next(item for item in decoded["provenance"]["model"] if item["evidence_id"] == hint["evidence_id"])
    assert hint["strength"] == "candidate" and hint["confidence"] == 0.68
    assert "strength" not in origin and "confidence" not in origin
    before = deepcopy(decoded)
    accepted, errors = ready_identity(decoded, identifier)
    assert errors == [] and decoded == before
    hint_id = hint["evidence_id"]
    for _round in range(3):
        accepted, errors = ready_identity(accepted, identifier)
        assert errors == [] and accepted["vehicle_profile"]["model"] == "Provider model"
        reported = next(item for item in accepted["provenance"]["model"] if item["evidence_id"] == hint_id)
        assert reported["reported_hint"]["value"] == "Rapid" and "value" not in reported
        assert reported["source"] == origin["source"] and reported["source_kind"] == "caller"
        assert reported["independent"] is False and reported["bound"] is False
    for _round in range(3):
        reconciled = reconcile_vehicle_identity(identifier, [accepted], detail="summary")
        assert reconciled["conflicts"] == []
        accepted, errors = ready_identity(reconciled, identifier)
        assert errors == [] and accepted["vehicle_profile"]["model"] == "Provider model"
        assert accepted["parts_lookup_readiness"]["ready_for_crm_writeback"] is False
        references = [
            ref
            for variant in accepted["variants"]
            for ref in variant.get("provenance_refs", {}).get("model", [])
            if ref.get("path") == "data.source_evidence"
        ]
        reported = next(
            accepted["source_evidence"][ref["index"]]
            for ref in references
            if accepted["source_evidence"][ref["index"]].get("evidence_id") == hint_id
        )
        assert reported["reported_hint"]["value"] == "Rapid" and "value" not in reported
        assert reported["source"] == origin["source"] and reported["source_kind"] == "caller"


@pytest.mark.parametrize("forgery", ["field", "value", "claimed_field", "explicit_strong", "caller", "ambiguous_id"])
def test_public_evidence_reference_cannot_hide_another_or_stronger_claim(forgery):
    identifier = "WBA" + "0" * 14
    evidence = {
        "evidence_id": "weak-hint",
        "field": "make",
        "value": "Skoda",
        "source_kind": "local_rule",
        "strength": "candidate",
        "confidence": 0.68,
    }
    provenance = {"evidence_id": "weak-hint", "value": "Skoda", "source_kind": "local_rule"}
    supplied = {
        "input_binding": binding(identifier),
        "vehicle_profile": {"make": "BMW", "model": "Demo"},
        "field_evidence": [evidence],
        "provenance": {"make": [provenance]},
    }
    if forgery == "field":
        evidence["field"] = "model"
    elif forgery == "value":
        evidence["value"] = "Honda"
    elif forgery == "claimed_field":
        provenance["field"] = "model"
    elif forgery == "explicit_strong":
        provenance["strength"] = "supported"
    elif forgery == "caller":
        provenance["source_kind"] = "caller"
    else:
        supplied["field_evidence"].append({**evidence, "value": "BMW", "strength": "supported"})
    accepted, errors = ready_identity(supplied, identifier)
    assert accepted is None
    assert {"field": "make", "code": "ready_identity_evidence_conflict"} in errors
    reconciled = reconcile_vehicle_identity(identifier, [supplied])
    assert reconciled["conflicts"] and ready_identity(reconciled, identifier)[0] is None


@pytest.mark.parametrize(
    "metadata",
    [
        {"source_kind": "caller", "strength": "candidate"},
        {"source_kind": "provider", "strength": "supported"},
        {"source_kind": "local_rule", "strength": "candidate", "confidence": 0.8},
        {},
    ],
)
@pytest.mark.parametrize("hint_tag", [False, True])
def test_real_conflicting_constraints_still_block_reuse(metadata, hint_tag):
    identifier = "WBA" + "0" * 14
    supplied = {
        "input_binding": binding(identifier),
        "vehicle_profile": {"make": "Skoda", "model": "Demo"},
        "field_evidence": [{"field": "make", "value": "Volkswagen", **metadata}],
    }
    if hint_tag:
        supplied["field_evidence"][0]["reported_conflict_role"] = "hint"
    accepted, errors = ready_identity(supplied, identifier)
    assert accepted is None
    assert errors == [{"field": "make", "code": "ready_identity_evidence_conflict"}]
    reconciled = reconcile_vehicle_identity(identifier, [supplied])
    assert "make" not in reconciled["data"]["vehicle_profile"]
    assert reconciled["data"]["field_statuses"]["make"] == "disputed"


@pytest.mark.parametrize("source_kind", ["provider", "caller"])
def test_native_planner_rejects_hint_tag_on_supported_same_source_conflict(tmp_path, source_kind):
    identifier = "WBA" + "0" * 14
    supplied = {
        "input_binding": binding(identifier),
        "vehicle_profile": {"make": "BMW", "model": "Demo"},
        "field_evidence": [
            {
                "field": "make",
                "value": make,
                "source": "Same source",
                "source_kind": source_kind,
                "strength": "supported",
                "confidence": 0.9,
                "reported_conflict_role": "hint",
            }
            for make in ("BMW", "Skoda")
        ],
    }
    server = FastMCP("reject-forged-hint")
    register_manager_tools(
        server, StoreState(tmp_path / "synthetic.sqlite3"), include_tools={"plan_oem_parts_providers"}
    )
    response = asyncio.run(
        server._tool_manager.get_tool("plan_oem_parts_providers").run(
            {"identifier": identifier, "requested_part": "фильтр", "vehicle_identity": supplied}
        )
    )
    assert response["ok"] is False
    assert response["lookup_scope"] == "blocked"
    assert {"field": "make", "code": "ready_identity_evidence_conflict"} in response["identity_errors"]
    assert response["execution"]["network_calls"] == 0


@pytest.mark.parametrize("carrier", ["vehicle_profile", "vehicle_profile_compact", "crm_vehicle_profile"])
def test_deep_json_profile_is_structured_invalid_input_before_provider(tmp_path, monkeypatch, carrier):
    import autostop_manager.mcp_tools as mcp_tools

    def forbidden_planner(**_kwargs):
        raise AssertionError("malformed profile reached provider planning")

    monkeypatch.setattr(mcp_tools, "build_oem_parts_provider_plan", forbidden_planner)
    server = FastMCP("deep-profile-boundary")
    register_manager_tools(
        server, StoreState(tmp_path / "synthetic.sqlite3"), include_tools={"plan_oem_parts_providers"}
    )
    nested = '{"private":"private-invalid-value","nested":' + "[" * 10000 + "0" + "]" * 10000 + "}"
    row = asyncio.run(
        server._tool_manager.get_tool("plan_oem_parts_providers").run(
            {
                "identifier": "",
                "requested_part": "фильтр",
                "vehicle_identity": {"vehicle_profile": {"make": "BMW", "model": "Demo", carrier: nested}},
            }
        )
    )
    assert row["ok"] is False
    assert row["error"] == "invalid_vehicle_identity"
    assert row["identity_errors"] == [{"field": "vehicle_profile", "code": "invalid_vehicle_profile_field"}]
    assert row["execution"]["network_calls"] == 0
    assert "private-invalid-value" not in json.dumps(row)
    direct = validate_identity_input("", {carrier: nested})
    assert direct["ok"] is False
    assert direct["errors"][0]["code"] == "invalid_profile_json"


def test_failed_source_keeps_independent_multiunit_profile_for_reuse(tmp_path, monkeypatch):
    identifier = "WBA" + "0" * 14
    calls = []

    def failed_decode(*_args, **_kwargs):
        calls.append("vpic")
        return {"ok": False, "outcome": "quota_error", "vehicle": {}}

    monkeypatch.setattr(automotive_identity.vin_lookup, "decode_vin_vpic", failed_decode)
    monkeypatch.setattr(catalog_adapters, "_providers_for_stage", lambda *_args: [])
    failed = automotive_identity.decode_vin_vpic(identifier)
    context = {
        "make": "BMW",
        "model": "Demo",
        "generation": "G-DEMO",
        "engine": "E-DEMO",
        "transmission": "Automatic",
        "transmission_code": "AT-DEMO",
        "drivetrain": "AWD",
        "transfer_case": "TC-DEMO",
        "front_differential": "FD-DEMO",
    }
    alternate = vehicle_identity.decode_vehicle_identity(
        identifier, crm_context=context, live_vpic=False, live_wmi=False
    )
    merged = reconcile_vehicle_identity(identifier, [failed, alternate])
    assert calls == ["vpic"]  # A quota failure is not retried.
    assert merged["outcome"] == "partial"
    assert merged["data"]["vehicle_profile"].items() >= context.items()
    assert "rear_differential" not in merged["data"]["vehicle_profile"]
    assert merged["execution"]["network_calls"] == 0
    for unit in ("transmission_code", "transfer_case", "front_differential"):
        assert merged["data"]["field_statuses"][unit] == "candidate"
        assert merged["data"]["provenance"][unit]
    assert merged["conflicts"] == []
    assert merged["data"]["source_failures"] == [{"result_index": 0, "outcome": "quota_error"}]
    reusable, errors = ready_identity(merged, identifier)
    assert errors == []
    assert reusable["vehicle_profile"].items() >= context.items()
    repeated = reconcile_vehicle_identity(identifier, [reusable])
    assert repeated["data"]["vehicle_profile"].items() >= context.items()
    assert repeated["data"]["source_failures"] == merged["data"]["source_failures"]
    server = FastMCP("reuse-after-source-failure")
    register_manager_tools(
        server, StoreState(tmp_path / "synthetic.sqlite3"), include_tools={"plan_oem_parts_providers"}
    )
    response = asyncio.run(
        server._tool_manager.get_tool("plan_oem_parts_providers").run(
            {"identifier": identifier, "requested_part": "фильтр", "vehicle_identity": repeated}
        )
    )
    assert response["ok"] is True
    assert response["lookup_scope"] == "family"
    assert response["vehicle_profile"].items() >= context.items()
    assert response["live_capability"]["identity_ready_for_crm_writeback"] is False


@pytest.mark.parametrize("failure", ["provider_error", "auth_error", "empty", "unsupported"])
def test_empty_bound_source_failures_are_diagnostics_only_with_usable_profile(failure):
    identifier = "WBA" + "0" * 14
    failed = automotive_result("demo", failure, {"input_binding": binding(identifier), "vehicle_profile": {}})
    combined = reconcile_vehicle_identity(identifier, [failed], context={"make": "BMW", "model": "Demo"})
    assert combined["conflicts"] == []
    accepted, errors = ready_identity(combined, identifier)
    assert errors == [] and accepted["vehicle_profile"] == {"make": "BMW", "model": "Demo"}
    assert combined["data"]["source_failures"] == [{"result_index": 0, "outcome": failure}]
    failed_alone = reconcile_vehicle_identity(identifier, [failed])
    assert ready_identity(failed_alone, identifier)[0] is None


@pytest.mark.parametrize("unsafe", ["foreign", "provider_mismatch", "facts", "conflict", "malformed"])
def test_source_failure_diagnostics_never_hide_real_conflicts_or_foreign_results(unsafe):
    identifier = "WBA" + "0" * 14
    failed = automotive_result("demo", "quota_error", {"input_binding": binding(identifier), "vehicle_profile": {}})
    if unsafe == "foreign":
        failed["data"]["input_binding"] = binding("WBA" + "1" * 14)
    elif unsafe == "provider_mismatch":
        failed["data"]["identifier_binding"] = {"status": "mismatch", "verified": False}
    elif unsafe == "facts":
        failed["data"]["vehicle_profile"] = {"make": "Skoda"}
    elif unsafe == "conflict":
        failed["conflicts"] = [{"field": "make", "code": "field_disagreement"}]
    else:
        failed["data"]["vehicle_profile"] = ["private-malformed-value"]
    combined = reconcile_vehicle_identity(identifier, [failed], context={"make": "BMW", "model": "Demo"})
    assert combined["conflicts"]
    assert ready_identity(combined, identifier)[0] is None
    assert combined["data"]["source_failures"] == []


@pytest.mark.parametrize("carrier", ["model_year_candidates", "diagnostics.model_year.candidate_years"])
@pytest.mark.parametrize("location", ["data", "envelope"])
@pytest.mark.parametrize("candidate_years", [[2018], "malformed-years"])
def test_failed_source_with_any_model_year_carrier_is_not_factless(carrier, location, candidate_years):
    identifier = "WBA" + "0" * 14
    failed = automotive_result("demo", "quota_error", {"input_binding": binding(identifier), "vehicle_profile": {}})
    target = failed["data"] if location == "data" else failed
    if carrier == "model_year_candidates":
        target[carrier] = candidate_years
    else:
        target["diagnostics"] = {"model_year": {"candidate_years": candidate_years}}
    combined = reconcile_vehicle_identity(
        identifier, [failed], context={"make": "BMW", "model": "Demo", "model_year": 2020}
    )
    assert combined["conflicts"]
    assert ready_identity(combined, identifier)[0] is None
    assert combined["data"]["source_failures"] == []


@pytest.mark.parametrize("malformed", ["outcome", "conflict_code", "binding_status"])
def test_source_failure_metadata_is_not_assumed_to_be_hashable(malformed):
    identifier = "WBA" + "0" * 14
    failed = automotive_result("demo", "quota_error", {"input_binding": binding(identifier), "vehicle_profile": {}})
    if malformed == "outcome":
        failed["outcome"] = {"invalid": "private-value"}
    elif malformed == "conflict_code":
        failed["conflicts"] = [{"field": "identifier", "code": []}]
    else:
        failed["data"]["identifier_binding"] = {"status": {"invalid": "private-value"}}
    combined = reconcile_vehicle_identity(identifier, [failed], context={"make": "BMW", "model": "Demo"})
    assert combined["conflicts"]
    assert ready_identity(combined, identifier)[0] is None
    assert combined["data"]["source_failures"] == []


def test_historical_failure_diagnostics_are_bounded_and_do_not_become_facts():
    identifier = "WBA" + "0" * 14
    supplied = {
        "input_binding": binding(identifier),
        "vehicle_profile": {"make": "BMW", "model": "Demo"},
        "source_failures": [
            {"result_index": 0, "outcome": {"private": "value"}},
            {"result_index": 1, "outcome": "quota_error", "payload": "private-provider-body"},
        ],
    }
    combined = reconcile_vehicle_identity(identifier, [supplied])
    assert combined["conflicts"] == []
    assert combined["data"]["source_failures"] == [{"result_index": 1, "outcome": "quota_error"}]
    assert "private-provider-body" not in json.dumps(combined)
    assert "private" not in json.dumps(combined["data"]["source_failures"])


def test_native_single_and_batch_decode_preserve_known_aggregate_fields(tmp_path):
    identifier = "WBA" + "0" * 14
    aggregates = {
        "generation": "G-DEMO",
        "transmission_code": "AT-DEMO",
        "transfer_case": "TC-DEMO",
        "front_differential": "FD-DEMO",
        "rear_differential": "RD-DEMO",
    }
    server = FastMCP("single-batch-context-parity")
    register_manager_tools(
        server,
        StoreState(tmp_path / "synthetic.sqlite3"),
        include_tools={"decode_vehicle_identity", "decode_vehicle_identities"},
    )
    single = server._tool_manager.get_tool("decode_vehicle_identity")
    assert aggregates.keys() <= single.parameters["properties"].keys()
    one = asyncio.run(
        single.run(
            {
                "identifier": identifier,
                "make": "BMW",
                "model": "Demo",
                **aggregates,
                "live_vpic": False,
                "live_wmi": False,
            }
        )
    )
    many = asyncio.run(
        server._tool_manager.get_tool("decode_vehicle_identities").run(
            {
                "items": [{"identifier": identifier, "make": "BMW", "model": "Demo", **aggregates}],
                "live_vpic": False,
                "use_vpic_batch": False,
            }
        )
    )
    assert one["vehicle_profile"].items() >= aggregates.items()
    assert many["results"][0]["vehicle_profile"].items() >= aggregates.items()
    accepted, errors = ready_identity(one, identifier)
    assert errors == [] and accepted["vehicle_profile"].items() >= aggregates.items()
    assert accepted["parts_lookup_readiness"]["ready_for_crm_writeback"] is False


def test_partial_identifier_plans_preliminary_family_research_without_fitment_claim(tmp_path, monkeypatch):
    from autostop_manager import catalog_adapters

    monkeypatch.setattr(catalog_adapters, "_providers_for_stage", lambda *_args: [])
    server = FastMCP("partial-identifier-preliminary-plan")
    register_manager_tools(
        server, StoreState(tmp_path / "synthetic.sqlite3"), include_tools={"plan_oem_parts_providers"}
    )
    profile = {
        "make": "BMW",
        "model": "Demo",
        "generation": "G-DEMO",
        "transfer_case": "TC-DEMO",
        "rear_differential": "RD-DEMO",
    }
    partial = "WBA" + "0" * 9
    row = asyncio.run(
        server._tool_manager.get_tool("plan_oem_parts_providers").run(
            {
                "identifier": partial,
                "requested_part": "масляный фильтр",
                "vehicle_identity": {"vehicle_profile": profile},
            }
        )
    )
    assert row["ok"] is True
    assert row["lookup_scope"] == "family"
    assert row["vehicle_profile"] == profile
    assert row["manual_public_search_queries"]
    assert partial not in json.dumps(row["manual_public_search_queries"])
    assert row["live_capability"]["identity_ready_for_vehicle_lookup"] is False
    assert row["live_capability"]["identity_ready_for_crm_writeback"] is False
