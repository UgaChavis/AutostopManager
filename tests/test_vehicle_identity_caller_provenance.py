"""Caller-described origins cannot impersonate a bound decoder response."""

from __future__ import annotations

import json

import pytest

from autostop_manager.automotive_contracts import binding
from autostop_manager.tecdoc_vehicle_selection import _independent_values, resolve_tecdoc_vehicle
from autostop_manager.vehicle_identity import _check_digit, _family_candidates, decode_vehicle_identity
from autostop_manager.vin_oem_resolver import _identity_with_partsapi_agreement
from tests.test_e2_partsapi_group_resolver import _group_call
from tests.test_tecdoc_vehicle_selection import _car, _catalogue, _identity


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


_TECHNICAL_CONSTRAINTS = [
    ("displacement_cc", 9999, 1995, {"displacement_cc": 1995}),
    ("power_hp", 999, 245, {"power_hp": 245}),
    ("power_kw", 999, 180, {"power_kw": 180}),
    ("fuel_type", "petrol", "diesel", {"fuel_type": "diesel"}),
    (
        "production_date",
        "2010-01-01",
        "2011-06-01",
        {"production_date_from": "2011-01-01", "production_date_to": "2011-12-31"},
    ),
]


@pytest.mark.parametrize(("field", "claimed", "matching", "catalog"), _TECHNICAL_CONSTRAINTS)
@pytest.mark.parametrize("location", ["context", "caller_evidence"])
@pytest.mark.parametrize("claimed_source", ["NHTSA vPIC", "partsapi_ru"])
def test_claimed_vpic_technical_constraint_blocks_both_catalog_paths(
    field, claimed, matching, catalog, location, claimed_source
):
    context = (
        _context(**{field: claimed}, field_provenance={field: claimed_source}) if location == "context" else _context()
    )
    identity = _decode(context, {})
    fallback_identity = _identity(engine="CRCA")
    if location == "caller_evidence":
        row = {"field": field, "value": claimed, "source": claimed_source, "source_kind": "caller"}
        identity["field_evidence"].append(row)
        fallback_identity["field_evidence"].append(row)

    group = _group_call()
    group["input_binding"] = binding(_synthetic_identifier(), "vin")
    group["vehicle_profiles"][0].update(
        input_binding=group["input_binding"], make="VOLKSWAGEN", model="Golf", vehicle_type="PC", **catalog
    )
    agreement = _identity_with_partsapi_agreement(identity, group, independent_context=context)[
        "parts_lookup_readiness"
    ]
    assert agreement["cross_source_agreement"]["status"] == "conflict"
    assert field in {row["field"] for row in agreement["cross_source_agreement"]["conflicting_fields"]}
    assert agreement["ready_for_tecdoc_candidate_lookup"] is False

    call, _ = _catalogue([_car(**catalog)])
    fallback = resolve_tecdoc_vehicle(
        fallback_identity,
        vehicle_type="PC",
        call=call,
        independent_context={field: claimed, "field_provenance": {field: claimed_source}}
        if location == "context"
        else None,
    )
    assert fallback["status"] == "conflict"
    assert field in {row["field"] for row in fallback["conflicting_fields"]}
    assert fallback["selected_profile"] is None
    assert fallback["exact_applicability_confirmed"] is False


@pytest.mark.parametrize(("field", "claimed", "matching", "catalog"), _TECHNICAL_CONSTRAINTS)
@pytest.mark.parametrize("claimed_source", ["NHTSA vPIC", "partsapi_ru"])
def test_matching_claimed_technical_constraint_is_not_positive_independent_confirmation(
    field, claimed, matching, catalog, claimed_source
):
    context = {field: matching, "field_provenance": {field: claimed_source}}
    assert _independent_values({}, context)[field] == []
    call, _ = _catalogue([_car(**catalog)])
    fallback = resolve_tecdoc_vehicle(_identity(), vehicle_type="PC", call=call, independent_context=context)
    assert fallback["status"] == "needs_vehicle_context"
    assert field not in fallback["matched_fields"]
    assert fallback["selected_profile"] is None

    call, _ = _catalogue([_car(**catalog)])
    matched = resolve_tecdoc_vehicle(
        _identity(engine="CRCA"), vehicle_type="PC", call=call, independent_context=context
    )
    assert matched["status"] == "matched"
    assert field not in matched["matched_fields"]
    assert field in matched["missing_fields"]
    assert matched["exact_applicability_confirmed"] is False

    group_context = _context(**{field: matching}, field_provenance={field: claimed_source})
    group_identity = _decode(group_context, {})
    group = _group_call()
    group["input_binding"] = binding(_synthetic_identifier(), "vin")
    group["vehicle_profiles"][0].update(
        input_binding=group["input_binding"], make="VOLKSWAGEN", model="Golf", vehicle_type="PC", **catalog
    )
    readiness = _identity_with_partsapi_agreement(group_identity, group, independent_context=group_context)[
        "parts_lookup_readiness"
    ]
    assert readiness["cross_source_agreement"]["status"] == "group_match"
    assert field not in readiness["cross_source_agreement"]["matched_fields"]
    assert readiness["ready_for_crm_writeback"] is False


def test_contradictory_caller_context_constraints_stop_before_catalogue_calls():
    context = {
        "power_hp": 245,
        "vehicle_profile": {"power_hp": 999},
        "field_provenance": {"power_hp": "NHTSA vPIC"},
    }
    call, calls = _catalogue([_car(power_hp=245)])
    result = resolve_tecdoc_vehicle(_identity(engine="CRCA"), vehicle_type="PC", call=call, independent_context=context)
    assert result["status"] == "conflict"
    assert {row["field"] for row in result["conflicting_fields"]} == {"power_hp"}
    assert calls == []
