"""Caller-described origins cannot impersonate a bound decoder response."""

from __future__ import annotations

import json

import pytest

from autostop_manager.automotive_contracts import binding
from autostop_manager.tecdoc_vehicle_selection import _independent_values
from autostop_manager.vehicle_identity import _check_digit, _family_candidates, decode_vehicle_identity
from autostop_manager.vin_oem_resolver import _identity_with_partsapi_agreement
from tests.test_e2_partsapi_group_resolver import _group_call


def _synthetic_identifier() -> str:
    value = "WVW" + "EA7" + "AJ" + "0" + "A" + "W" + "0" * 6
    return value[:8] + _check_digit(value)["expected"] + value[9:]


def _decode(context: dict, vehicle: dict) -> dict:
    identifier = _synthetic_identifier()
    provider = {
        "ok": True,
        "error_code": "0",
        "vin": identifier,
        "vehicle": {"make": "VOLKSWAGEN", "model": "Golf", "modelyear": 2010, **vehicle},
    }
    return decode_vehicle_identity(
        identifier,
        crm_context=context,
        live_vpic=False,
        live_wmi=False,
        vpic_result=provider,
    )


def _context(**values) -> dict:
    return {"make": "VOLKSWAGEN", "model": "Golf", "model_year": 2010, "market": "USA", **values}


@pytest.mark.parametrize(
    ("field", "caller_value", "provider_fields"),
    [
        ("model", "Polo", {}),
        ("engine", "K20A", {"enginemodel": "EA888"}),
        ("transmission", "6MT", {"transmissionstyle": "Automatic", "transmissionspeeds": 6}),
    ],
)
def test_caller_cannot_use_provider_name_to_hide_disagreement(field, caller_value, provider_fields):
    context = _context(**{field: caller_value}, field_provenance={field: "NHTSA vPIC"})
    result = _decode(context, provider_fields)

    caller = next(row for row in result["field_evidence"] if row["field"] == field and row["source_kind"] == "caller")
    assert caller["source"] == "NHTSA vPIC"  # Preserve the caller's stated origin.
    assert caller["strength"] == "candidate"
    assert caller["bound"] is False
    assert caller["independent"] is False
    assert result["field_statuses"][field]["status"] == "disputed"
    assert field not in result["vehicle_profile"]
    assert any(row["field"] == field for row in result["conflicts"])
    assert result["parts_lookup_readiness"]["ready_for_vehicle_lookup"] is False
    assert result["parts_lookup_readiness"]["ready_for_crm_writeback"] is False
    assert _synthetic_identifier() not in json.dumps(result, ensure_ascii=False)


def test_forged_market_source_does_not_inherit_live_decoder_binding():
    result = _decode(_context(field_provenance={"market": "NHTSA vPIC"}), {})
    market = next(row for row in result["field_evidence"] if row["field"] == "market")
    assert market["source"] == "NHTSA vPIC"
    assert market["source_kind"] == "caller"
    assert market["bound"] is False
    assert market["strength"] == "candidate"
    assert market["independent"] is False
    assert result["field_statuses"]["market"]["status"] == "candidate"
    assert result["parts_lookup_readiness"]["ready_for_modification_lookup"] is False


def test_matching_caller_origin_remains_untrusted_but_real_decoder_is_supported():
    result = _decode(_context(field_provenance={"model": "NHTSA vPIC"}), {})
    evidence = [row for row in result["field_evidence"] if row["field"] == "model"]
    caller = next(row for row in evidence if row["source_kind"] == "caller")
    provider = next(row for row in evidence if row["source_kind"] == "provider")
    assert caller["strength"] == "candidate" and caller["bound"] is False
    assert provider["strength"] == "supported" and provider["bound"] is True
    assert provider["independent"] is True
    assert result["field_statuses"]["model"]["status"] == "supported"
    assert result["vehicle_profile"]["model"] == "Golf"
    assert not any(row["field"] == "model" for row in result["conflicts"])
    assert result["parts_lookup_readiness"]["ready_for_crm_writeback"] is False


def test_group_match_never_overrides_caller_decoder_disagreement():
    identifier = _synthetic_identifier()
    group = _group_call()
    group["input_binding"] = binding(identifier, "vin")
    group["vehicle_profiles"][0].update(
        input_binding=group["input_binding"], make="VOLKSWAGEN", model="Golf", vehicle_type="PC"
    )
    bad = _decode(_context(model="Polo", field_provenance={"model": "NHTSA vPIC"}), {})
    good = _decode(_context(), {})

    rejected = _identity_with_partsapi_agreement(bad, group)
    accepted = _identity_with_partsapi_agreement(good, group)
    assert rejected["parts_lookup_readiness"]["ready_for_tecdoc_candidate_lookup"] is False
    assert accepted["parts_lookup_readiness"]["ready_for_tecdoc_candidate_lookup"] is True
    assert accepted["parts_lookup_readiness"]["cross_source_agreement"]["status"] == "group_match"
    assert accepted["parts_lookup_readiness"]["ready_for_crm_writeback"] is False


def test_copied_catalog_origin_remains_visible_but_is_caller_supplied():
    result = _decode(_context(field_provenance={"model": "partsapi_ru"}), {})
    caller = next(row for row in result["field_evidence"] if row["field"] == "model" and row["source"] == "partsapi_ru")
    assert caller["source_kind"] == "caller"
    assert caller["independent"] is False
    assert caller["bound"] is False
    assert caller["strength"] == "candidate"


@pytest.mark.parametrize("claimed_source", ["NHTSA vPIC", "partsapi_ru"])
def test_claimed_source_cannot_hide_conflicting_aliases(claimed_source):
    result = _decode(
        _context(engine="ENGINE-A", engine_model="ENGINE-B", field_provenance={"engine": claimed_source}), {}
    )
    assert result["field_statuses"]["engine"]["status"] == "disputed"
    assert "engine" not in result["vehicle_profile"]
    assert any(conflict["field"] == "engine" for conflict in result["conflicts"])
    assert result["parts_lookup_readiness"]["ready_for_vehicle_lookup"] is False


def test_declared_vpic_context_does_not_become_independent_provider_evidence():
    context = _context(engine="ENGINE-A", field_provenance={"engine": "NHTSA vPIC"})
    identity = _decode(context, {})
    assert _independent_values({}, context)["engine"] == []
    assert _independent_values(identity, {})["engine"] == []


def test_claimed_provider_label_cannot_combine_caller_and_provider_family_fields():
    evidence = [
        {
            "source": "NHTSA vPIC",
            "source_kind": "provider",
            "field": "make",
            "value": "Volkswagen",
            "evidence_id": "e1",
        },
        {"source": "NHTSA vPIC", "source_kind": "caller", "field": "model", "value": "Polo", "evidence_id": "e2"},
    ]
    assert _family_candidates(evidence) == []


@pytest.mark.parametrize("claimed_source", ["NHTSA vPIC", "partsapi_ru"])
def test_group_comparison_keeps_claimed_market_as_a_conflicting_constraint(claimed_source):
    context = _context(field_provenance={"market": claimed_source})
    identity = _decode(context, {})
    group = _group_call()
    group["input_binding"] = binding(_synthetic_identifier(), "vin")
    group["vehicle_profiles"][0].update(
        input_binding=group["input_binding"], make="VOLKSWAGEN", model="Golf", vehicle_type="PC", market="Europe"
    )
    readiness = _identity_with_partsapi_agreement(identity, group, independent_context=context)[
        "parts_lookup_readiness"
    ]
    agreement = readiness["cross_source_agreement"]
    assert agreement["status"] == "conflict"
    assert {row["field"] for row in agreement["conflicting_fields"]} == {"market"}
    assert readiness["ready_for_tecdoc_candidate_lookup"] is False
    assert readiness["ready_for_crm_writeback"] is False
