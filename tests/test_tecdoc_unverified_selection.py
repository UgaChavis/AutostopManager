"""Catalog filtering must disclose dependencies on unverified caller facts."""

from __future__ import annotations

import json

import pytest

from autostop_manager import vin_oem_resolver
from autostop_manager.tecdoc_vehicle_selection import resolve_tecdoc_vehicle
from autostop_manager.vehicle_identity_inputs import validate_identity_input
from tests.test_tecdoc_vehicle_selection import _car, _catalogue, _identity
from tests.test_vin_fallback_integration import FACTS, VIN, _identity as _resolver_identity, _install, _resolve


def _provider_identity(**facts):
    result = _identity(**facts)
    for row in result["field_evidence"]:
        row.update(
            source="NHTSA vPIC",
            source_kind="provider",
            bound=True,
            independent=True,
            identifier_binding="exact",
            strength="supported",
        )
    return result


@pytest.mark.parametrize(
    ("field", "value", "other", "selected"),
    [
        ("power_hp", 245, {"power_hp": 204}, {"power_hp": 245}),
        ("fuel_type", "diesel", {"fuel_type": "petrol"}, {"fuel_type": "diesel"}),
        (
            "production_date",
            "2020-06-01",
            {"production_date_from": "2018-01-01", "production_date_to": "2019-12-31"},
            {"production_date_from": "2020-01-01", "production_date_to": "2021-12-31"},
        ),
    ],
)
def test_selection_records_which_unverified_constraint_excluded_an_alternative(field, value, other, selected):
    call, _ = _catalogue([_car(101, **other), _car(102, **selected)])
    context = {field: value, "field_provenance": {field: "NHTSA vPIC"}}
    result = resolve_tecdoc_vehicle(
        _provider_identity(engine="CRCA"),
        vehicle_type="PC",
        call=call,
        independent_context=context,
        trusted_provider_evidence=True,
    )
    assert result["status"] == "matched"
    assert result["selected_profile"]["tecdoc_car_id"] == 102
    assert result["selection_depends_on_unverified_fields"] == [field]
    assert result["warnings"]
    assert result["exact_applicability_confirmed"] is False


def test_a_single_catalog_candidate_does_not_claim_a_selection_dependency():
    call, _ = _catalogue([_car(102, power_hp=245)])
    result = resolve_tecdoc_vehicle(
        _provider_identity(engine="CRCA"),
        vehicle_type="PC",
        call=call,
        independent_context={"power_hp": 245, "field_provenance": {"power_hp": "NHTSA vPIC"}},
        trusted_provider_evidence=True,
    )
    assert result["status"] == "matched"
    assert result["selection_depends_on_unverified_fields"] == []
    assert result["warnings"] == []


def test_plain_crm_claim_is_still_unverified_even_when_it_drives_selection():
    call, _ = _catalogue([_car(101, power_hp=204), _car(102, power_hp=245)])
    result = resolve_tecdoc_vehicle(
        _provider_identity(engine="CRCA"),
        vehicle_type="PC",
        call=call,
        independent_context={"power_hp": 245},
        trusted_provider_evidence=True,
    )
    assert result["status"] == "matched"
    assert result["selection_depends_on_unverified_fields"] == ["power_hp"]
    assert result["warnings"]


def test_redundant_unverified_filters_are_both_disclosed():
    call, _ = _catalogue([_car(101, power_hp=204, fuel_type="petrol"), _car(102, power_hp=245, fuel_type="diesel")])
    result = resolve_tecdoc_vehicle(
        _provider_identity(engine="CRCA"),
        vehicle_type="PC",
        call=call,
        independent_context={"power_hp": 245, "fuel_type": "diesel"},
        trusted_provider_evidence=True,
    )
    assert result["status"] == "matched"
    assert result["selection_depends_on_unverified_fields"] == ["fuel_type", "power_hp"]


def test_fresh_bound_independent_provider_value_is_not_called_unverified():
    identity = _provider_identity(engine="CRCA")
    identity["field_evidence"].append(
        {
            "field": "power_hp",
            "value": 245,
            "source": "NHTSA vPIC",
            "source_kind": "provider",
            "bound": True,
            "independent": True,
            "identifier_binding": "exact",
            "strength": "supported",
        }
    )
    call, _ = _catalogue([_car(101, power_hp=204), _car(102, power_hp=245)])
    result = resolve_tecdoc_vehicle(
        identity, vehicle_type="PC", call=call, independent_context={"power_hp": 245}, trusted_provider_evidence=True
    )
    assert result["status"] == "matched"
    assert result["selection_depends_on_unverified_fields"] == []
    assert result["warnings"] == []


def test_unverified_filter_already_excluded_by_verified_evidence_is_not_a_dependency():
    identity = _provider_identity(engine="CRCA")
    identity["field_evidence"].append(
        {
            "field": "engine",
            "value": "CRCA",
            "source": "NHTSA vPIC",
            "source_kind": "provider",
            "bound": True,
            "independent": True,
            "identifier_binding": "exact",
            "strength": "supported",
        }
    )
    call, _ = _catalogue([_car(101, engine_code="OTHER", power_hp=204), _car(102, power_hp=245)])
    result = resolve_tecdoc_vehicle(
        identity,
        vehicle_type="PC",
        call=call,
        independent_context={"power_hp": 245, "field_provenance": {"power_hp": "NHTSA vPIC"}},
        trusted_provider_evidence=True,
    )
    assert result["status"] == "matched"
    assert result["selection_depends_on_unverified_fields"] == []


def test_tolerance_does_not_hide_dependency_on_a_distinct_caller_number():
    identity = _provider_identity(engine="CRCA")
    identity["field_evidence"].append(
        {
            "field": "power_hp",
            "value": 244,
            "source": "NHTSA vPIC",
            "source_kind": "provider",
            "bound": True,
            "independent": True,
            "identifier_binding": "exact",
            "strength": "supported",
        }
    )
    call, _ = _catalogue([_car(101, power_hp=242), _car(102, power_hp=245)])
    result = resolve_tecdoc_vehicle(
        identity,
        vehicle_type="PC",
        call=call,
        independent_context={"power_hp": 245, "field_provenance": {"power_hp": "NHTSA vPIC"}},
        trusted_provider_evidence=True,
    )
    assert result["status"] == "matched"
    assert result["selection_depends_on_unverified_fields"] == ["power_hp"]


@pytest.mark.parametrize("problem", ["untrusted_boundary", "caller_dossier", "mismatched_binding", "candidate_only"])
def test_supplied_or_misbound_provider_claim_does_not_silence_warning(problem):
    identity = _provider_identity(engine="CRCA")
    if problem == "caller_dossier":
        identity["trust"] = {"state": "caller_supplied_unverified"}
    identity["field_evidence"].append(
        {
            "field": "power_hp",
            "value": 245,
            "source": "NHTSA vPIC",
            "source_kind": "provider",
            "bound": True,
            "independent": True,
            "identifier_binding": "mismatch" if problem == "mismatched_binding" else "exact",
            "strength": "candidate" if problem == "candidate_only" else "supported",
        }
    )
    call, _ = _catalogue([_car(101, power_hp=204), _car(102, power_hp=245)])
    result = resolve_tecdoc_vehicle(
        identity,
        vehicle_type="PC",
        call=call,
        independent_context={"power_hp": 245},
        trusted_provider_evidence=problem != "untrusted_boundary",
    )
    assert result["status"] == "matched"
    assert "power_hp" in result["selection_depends_on_unverified_fields"]
    assert result["warnings"]


def test_single_candidate_still_discloses_unverified_facts_needed_for_selection():
    call, _ = _catalogue([_car(102)])
    result = resolve_tecdoc_vehicle(_identity(engine="CRCA"), vehicle_type="PC", call=call)
    assert result["status"] == "matched"
    assert result["selection_depends_on_unverified_fields"] == ["engine", "make", "model"]
    assert result["warnings"]


def test_resolver_exposes_unverified_selection_at_all_three_levels(monkeypatch):
    calls = _install(
        monkeypatch,
        cars=[
            {"tecdoc_car_id": 101, "engine_code": "DENA", "power_hp": 204},
            {"tecdoc_car_id": 102, "engine_code": "DENA", "power_hp": 245},
        ],
    )

    def decode(identifier, **kwargs):
        context = validate_identity_input(identifier, kwargs["crm_context"])["context"]
        result = _resolver_identity(context)
        for row in result["field_evidence"]:
            if row["field"] == "power_hp":
                row.update(source="NHTSA vPIC", source_kind="caller", bound=False, independent=False)
            else:
                row.update(
                    source="NHTSA vPIC",
                    source_kind="provider",
                    bound=True,
                    independent=True,
                    identifier_binding="exact",
                    strength="supported",
                )
        return result

    monkeypatch.setattr(vin_oem_resolver, "decode_vehicle_identity", decode)
    result = _resolve({**FACTS, "power_hp": 245, "field_provenance": {"power_hp": "NHTSA vPIC"}})
    assert result["status"] == "tecdoc_articles_found_needs_manual_fitment"
    assert result["tecdoc_vehicle"]["car_id"] == "102"
    agreement = result["identity"]["cross_source_agreement"]
    fallback = result["tecdoc_vehicle_fallback"]
    for level in (result, agreement, fallback):
        assert level["selection_depends_on_unverified_fields"] == ["power_hp"]
        assert level["warnings"]
    assert "power_hp" not in fallback["matched_fields"]
    assert result["article_candidates"][0]["vin_fitment_confirmed"] is False
    assert result["readiness"]["ready_for_crm_writeback"] is False
    assert calls[-1]["operation"] == "articles"
    assert VIN not in json.dumps(result)
