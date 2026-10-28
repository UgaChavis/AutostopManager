"""Pure capture can classify supplied evidence, but cannot authenticate it."""

from __future__ import annotations

from copy import deepcopy

import pytest

from autostop_manager.automotive_contracts import binding
from autostop_manager.automotive_parts import assess_part_fitment, capture_oem_evidence


_IDENTIFIER = "WBA" + "0" * 14
_MODIFICATION = {
    "provider": "partsapi_ru",
    "namespace": "tecdoc",
    "entity_kind": "modification",
    "id": "42",
    "carType": "PC",
}


def _source(scope: str) -> tuple[dict, dict | None]:
    bound = binding(_IDENTIFIER) if scope == "exact_identifier" else _MODIFICATION if scope == "modification" else None
    source = {
        "provider": "DEMO",
        "primary_lineage": "DEMO manufacturer",
        "method": "epc",
        "locator": "https://example.invalid/oem",
        "fetched_at": "2026-01-01T00:00:00Z",
        "document_kind": "official_epc",
        "declares_oem": True,
        "part_number": "DEMO-1",
        "brand": "DEMO",
        "scope": scope,
        "source_verified": True,
        "independent_source_verified": True,
        "identifier_verified": True,
        "ready_for_crm_writeback": True,
    }
    if scope == "exact_identifier":
        source["identifier_binding"] = bound
    elif scope == "modification":
        source["catalog_ref"] = bound
    return source, bound


@pytest.mark.parametrize("scope", ["family", "modification", "exact_identifier"])
def test_caller_document_metadata_can_only_establish_conditional_oem_candidate(scope):
    source, bound = _source(scope)
    original = deepcopy(source)
    result = capture_oem_evidence("DEMO-1", source, scope, bound, "DEMO")
    candidate = result["data"]["candidate"]
    assert result["outcome"] == "success"  # The structural assessment completed.
    assert candidate["kind"] == "oem_candidate"
    assert candidate["scope"] == scope
    assert candidate["vehicle_binding"] == bound
    assert candidate["caller_evidence_sufficient"] is True
    assert candidate["oem_confirmed"] is False
    assert candidate["fitment_confirmed"] is False
    assert candidate["exact_applicability_confirmed"] is False
    assert candidate["independent_source_verified"] is False
    assert candidate["conditional_on_caller_evidence"] is True
    assert candidate["ready_for_crm_writeback"] is False
    assert result["evidence"][0]["independent_source_verified"] is False
    assert result["evidence"][0]["source_verified"] is False
    assert result["evidence"][0]["identifier_verified"] is False
    assert result["evidence"][0]["reported_identifier_verified"] is True
    assert result["evidence"][0]["ready_for_crm_writeback"] is False
    assert result["warnings"]
    assert result["execution"]["network_calls"] == 0
    assert source == original


@pytest.mark.parametrize("change", [{"part_number": "OTHER"}, {"brand": "OTHER"}, {"document_kind": "article"}])
def test_missing_structural_evidence_remains_unknown(change):
    source, bound = _source("exact_identifier")
    result = capture_oem_evidence("DEMO-1", {**source, **change}, "exact_identifier", bound, "DEMO")
    candidate = result["data"]["candidate"]
    assert result["outcome"] == "partial"
    assert candidate["kind"] == "unknown"
    assert candidate["caller_evidence_sufficient"] is False
    assert candidate["oem_confirmed"] is False
    assert candidate["fitment_confirmed"] is False
    assert candidate["independent_source_verified"] is False
    assert result["missing_fields"]


def test_wrong_binding_remains_structurally_unverified():
    source, bound = _source("exact_identifier")
    source["identifier_binding"] = binding("WBA" + "1" * 14)
    result = capture_oem_evidence("DEMO-1", source, "exact_identifier", bound, "DEMO")
    assert result["data"]["candidate"]["caller_evidence_sufficient"] is False
    assert result["data"]["candidate"]["oem_confirmed"] is False
    assert "exact_vehicle_binding" in result["missing_fields"]


@pytest.mark.parametrize("scope", ["family", "modification", "exact_identifier"])
def test_source_and_nested_caller_claims_cannot_appear_as_verified(scope):
    source, bound = _source(scope)
    claims = {
        "source_kind": "provider",
        "independent": True,
        "bound": True,
        "strength": "supported",
        "oem_number_confirmed": True,
        "vin_fitment_confirmed": True,
        "custom_verified": True,
        "custom_authenticated": True,
        "FUTURE_VEHICLE_CONFIRMED": True,
        "ready_for_payment": True,
        "can_charge": True,
        "confirmed": True,
        "authenticated": True,
        "ready": True,
        "status": "provider_verified",
        "verification": "verified_by_provider",
        "authentication": "confirmed",
        "reported_vin_fitment_confirmed": True,
        "reported_source_kind": "provider",
        "trust": {"state": "provider_verified", "binding_authenticates_source": True},
    }
    source.update({**claims, "fitment_assertion": True, "nested": {"evidence": [deepcopy(claims)]}})
    original = deepcopy(source)
    result = capture_oem_evidence("DEMO-1", source, scope, bound, "DEMO")
    returned = result["evidence"][0]
    nested = returned["nested"]["evidence"][0]
    for evidence in (returned, nested):
        assert evidence["source_kind"] == "caller"
        assert evidence["independent"] is False and evidence["bound"] is False
        assert evidence["strength"] == "candidate"
        for flag in (
            "oem_number_confirmed",
            "vin_fitment_confirmed",
            "custom_verified",
            "custom_authenticated",
            "FUTURE_VEHICLE_CONFIRMED",
            "ready_for_payment",
            "can_charge",
            "confirmed",
            "authenticated",
            "ready",
        ):
            assert evidence[flag] is False
        assert evidence["status"] == "candidate"
        assert evidence["verification"] == evidence["authentication"] == "unverified"
        assert "reported_vin_fitment_confirmed" not in evidence
        assert "reported_source_kind" not in evidence
        assert evidence["trust"] == {"state": "caller_supplied_unverified", "binding_authenticates_source": False}
        assert {entry["field"] for entry in evidence["reported_claims"]} >= {
            "source_kind",
            "strength",
            "oem_number_confirmed",
            "vin_fitment_confirmed",
        }
        assert all(entry["verification"] == "caller_supplied_unverified" for entry in evidence["reported_claims"])
        reported = {entry["field"]: entry["value"] for entry in evidence["reported_claims"]}
        assert reported["source_kind"] == "provider"
        assert reported["strength"] == "supported"
        assert reported["oem_number_confirmed"] is True
        assert reported["reported_vin_fitment_confirmed"] is True
        assert reported["reported_source_kind"] == "provider"
    assert returned["fitment_assertion"] is True
    assert returned["reported_identifier_verified"] is True
    assert result["data"]["candidate"]["caller_evidence_sufficient"] is True
    assert result["data"]["candidate"]["oem_confirmed"] is False
    assert result["data"]["candidate"]["ready_for_crm_writeback"] is False
    assert result["execution"]["network_calls"] == 0
    assert source == original


@pytest.mark.parametrize("scope", ["family", "modification", "exact_identifier"])
def test_sanitized_capture_evidence_remains_usable_for_conditional_fitment(scope):
    source, bound = _source(scope)
    source["fitment_assertion"] = True
    captured = capture_oem_evidence("DEMO-1", source, scope, bound, "DEMO")
    returned = captured["evidence"][0]
    vehicle = {"vehicle_profile": {"make": "BMW", "model": "DEMO"}}
    if scope == "modification":
        vehicle["catalog_ref"] = bound
        assert returned["catalog_ref"] == bound
    if scope == "exact_identifier":
        vehicle["input_binding"] = bound
        assert returned["identifier_binding"] == bound
    assessed = assess_part_fitment(vehicle, {"number": "DEMO-1", "brand": "DEMO"}, {}, [returned], scope)
    assert assessed["data"]["state"] == "supported"
    assert assessed["data"]["conditional_on_caller_evidence"] is True
    assert assessed["data"]["independent_source_verified"] is False
    assert assessed["data"]["exact_applicability_confirmed"] is False
    assert assessed["data"]["ready_for_crm_writeback"] is False


def test_deep_nested_capture_claims_fail_closed_without_exposing_input():
    source, bound = _source("family")
    nested = source
    for _ in range(40):
        nested["child"] = {}
        nested = nested["child"]
    nested["secret"] = "private-test-marker"
    result = capture_oem_evidence("DEMO-1", source, "family", bound, "DEMO")
    assert result["ok"] is False
    assert result["outcome"] == "invalid_input"
    assert "private-test-marker" not in str(result)


def test_caller_confirmation_claims_inside_vehicle_binding_are_downgraded_without_breaking_ref():
    source, bound = _source("modification")
    bound = {**bound, "source_kind": "provider", "oem_number_confirmed": True, "bound": True}
    source["catalog_ref"] = deepcopy(bound)
    source["fitment_assertion"] = True
    result = capture_oem_evidence("DEMO-1", source, "modification", bound, "DEMO")
    returned_binding = result["data"]["candidate"]["vehicle_binding"]
    source_binding = result["evidence"][0]["catalog_ref"]
    assert returned_binding == source_binding
    for ref in (returned_binding, source_binding):
        assert ref["source_kind"] == "caller"
        assert ref["oem_number_confirmed"] is False and ref["bound"] is False
        assert ref["trust"]["state"] == "caller_supplied_unverified"
    assert result["data"]["candidate"]["caller_evidence_sufficient"] is True
    vehicle = {"vehicle_profile": {"make": "BMW", "model": "DEMO"}, "catalog_ref": returned_binding}
    assessed = assess_part_fitment(
        vehicle, {"number": "DEMO-1", "brand": "DEMO"}, {}, result["evidence"], "modification"
    )
    assert assessed["data"]["state"] == "supported"
    assert assessed["data"]["exact_applicability_confirmed"] is False
    assert assessed["data"]["ready_for_crm_writeback"] is False


def test_untrusted_reported_alias_cannot_override_the_actual_caller_flag():
    source, bound = _source("family")
    source["identifier_verified"] = False
    source["reported_identifier_verified"] = True
    result = capture_oem_evidence("DEMO-1", source, "family", bound, "DEMO")
    returned = result["evidence"][0]
    assert returned["identifier_verified"] is False
    assert returned["reported_identifier_verified"] is False


def test_native_catalog_exact_fitment_stays_unknown_without_fresh_identifier_verification():
    source, bound = _source("exact_identifier")
    native_ref = {
        "provider": "elcats_catalog",
        "namespace": "elcats_epc",
        "entity_kind": "modification",
        "id": "demo-vehicle",
        "entry_id": "demo-entry",
        "vehicle_context": {"make": "BMW", "model": "DEMO"},
    }
    source.update({"catalog_ref": native_ref, "fitment_assertion": True})
    captured = capture_oem_evidence("DEMO-1", source, "exact_identifier", bound, "DEMO")
    assert captured["data"]["candidate"]["caller_evidence_sufficient"] is True
    assert captured["evidence"][0]["catalog_ref"] == native_ref
    assert captured["evidence"][0]["identifier_verified"] is False
    vehicle = {"vehicle_profile": {"make": "BMW", "model": "DEMO"}, "catalog_ref": native_ref, "input_binding": bound}
    assessed = assess_part_fitment(
        vehicle, {"number": "DEMO-1", "brand": "DEMO"}, {}, captured["evidence"], "exact_identifier"
    )
    assert assessed["data"]["state"] == "unknown"
    assert "evidence[0].primary_identifier_verification" in assessed["missing_fields"]
    assert assessed["data"]["ready_for_crm_writeback"] is False


@pytest.mark.parametrize("strength", ["strong", "high", 0.9, {"source_kind": "provider"}])
def test_unrecognized_claimed_strength_cannot_become_confirmation(strength):
    source, bound = _source("family")
    source["strength"] = strength
    returned = capture_oem_evidence("DEMO-1", source, "family", bound, "DEMO")["evidence"][0]
    assert returned["strength"] == "candidate"
    assert returned["trust"]["state"] == "caller_supplied_unverified"


def test_disputed_strength_is_not_silently_promoted():
    source, bound = _source("family")
    source["strength"] = "disputed"
    returned = capture_oem_evidence("DEMO-1", source, "family", bound, "DEMO")["evidence"][0]
    assert returned["strength"] == "disputed"
    assert returned["trust"]["state"] == "caller_supplied_unverified"


@pytest.mark.parametrize("status", ["unverified", "not_verified", "not_confirmed", "disputed"])
def test_negative_status_is_not_promoted_or_hidden(status):
    source, bound = _source("family")
    source["status"] = status
    returned = capture_oem_evidence("DEMO-1", source, "family", bound, "DEMO")["evidence"][0]
    assert returned["status"] == status
    assert returned["trust"]["state"] == "caller_supplied_unverified"


@pytest.mark.parametrize("status", ["strongly_supported", "verified_by_source", "ready_for_crm_writeback"])
def test_positive_status_is_preserved_only_as_an_unverified_claim(status):
    source, bound = _source("family")
    source["status"] = status
    returned = capture_oem_evidence("DEMO-1", source, "family", bound, "DEMO")["evidence"][0]
    assert returned["status"] == "candidate"
    assert any(
        entry["field"] == "status"
        and entry["value"] == status
        and entry["verification"] == "caller_supplied_unverified"
        for entry in returned["reported_claims"]
    )
