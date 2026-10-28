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
from autostop_manager.automotive_contracts import binding, ready_identity
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
        reconciled = reconcile_vehicle_identity(identifier, [supplied])
        assert reconciled["conflicts"] == []
        assert reconciled["data"]["vehicle_profile"][expected_field] == expected_value
        again, errors = ready_identity(reconciled, identifier)
        assert errors == [] and again is not None
        assert again["parts_lookup_readiness"]["ready_for_crm_writeback"] is False


@pytest.mark.parametrize(
    "metadata",
    [
        {"source_kind": "caller", "strength": "candidate"},
        {"source_kind": "provider", "strength": "supported"},
        {"source_kind": "local_rule", "strength": "candidate", "confidence": 0.8},
        {},
    ],
)
def test_real_conflicting_constraints_still_block_reuse(metadata):
    identifier = "WBA" + "0" * 14
    supplied = {
        "input_binding": binding(identifier),
        "vehicle_profile": {"make": "Skoda", "model": "Demo"},
        "field_evidence": [{"field": "make", "value": "Volkswagen", **metadata}],
    }
    accepted, errors = ready_identity(supplied, identifier)
    assert accepted is None
    assert errors == [{"field": "make", "code": "ready_identity_evidence_conflict"}]
    reconciled = reconcile_vehicle_identity(identifier, [supplied])
    assert "make" not in reconciled["data"]["vehicle_profile"]
    assert reconciled["data"]["field_statuses"]["make"] == "disputed"


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


def test_failed_source_keeps_independent_multiunit_profile_for_reuse(monkeypatch):
    identifier = "WBA" + "0" * 14
    calls = []

    def failed_decode(*_args, **_kwargs):
        calls.append("vpic")
        return {"ok": False, "outcome": "quota_error", "vehicle": {}}

    monkeypatch.setattr(automotive_identity.vin_lookup, "decode_vin_vpic", failed_decode)
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
    reusable, errors = ready_identity(alternate, identifier)
    assert errors == []
    assert reusable["vehicle_profile"].items() >= context.items()
    repeated = reconcile_vehicle_identity(identifier, [reusable])
    assert repeated["data"]["vehicle_profile"].items() >= context.items()


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
