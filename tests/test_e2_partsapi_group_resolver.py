"""A provider's representative VIN permits catalog research, never exact fitment."""

from __future__ import annotations

import copy

import pytest

from autostop_manager import vin_oem_resolver as resolver
from autostop_manager.automotive_contracts import binding


def _identity() -> dict:
    return {
        "ok": True,
        "confidence": 0.0,
        "confidence_label": "low",
        "vehicle_profile": {},
        "field_statuses": {},
        "parts_lookup_readiness": {
            "ready_for_vehicle_lookup": False,
            "ready_for_oem_lookup": False,
            "ready_for_oem_candidate_lookup": False,
            "ready_for_crm_writeback": False,
            "blocking_reasons": ["identity_confidence_below_high", "vehicle_family_unresolved"],
        },
        "conflicts": [],
        "warnings": [],
    }


def _group_call() -> dict:
    return {
        "ok": True,
        "provider": "partsapi_ru",
        "operation": "vin_decode",
        "outcome": "group_match",
        "dry_run": False,
        "identifier_matches_request": False,
        "requires_exact_identifier_confirmation": True,
        "binding_kind": "provider_group_reference",
        "identifier_semantics": "group_representative",
        "input_binding": binding("A" * 17, "vin"),
        "vehicle_profiles": [
            {
                "provider": "partsapi_ru",
                "source_operation": "vin_decode",
                "make": "SYNTHETIC",
                "model": "Example",
                "tecdoc_car_id": "123",
                "vehicle_type": "PC",
                "displacement_cc": 1995,
                "identifier_matches_request": False,
                "requires_exact_identifier_confirmation": True,
                "binding_kind": "provider_group_reference",
                "identifier_semantics": "group_representative",
                "evidence_status": "catalog_candidate",
                "catalog_candidate_only": True,
                "independent_vehicle_confirmation": False,
                "fitment_confirmed": False,
                "input_binding": binding("A" * 17, "vin"),
                "provider_identifier_is_vehicle_confirmation": False,
                "vin_prefix_is_vehicle_confirmation": False,
                "vin_fitment_confirmed": False,
                "provenance": {
                    "provider": "partsapi_ru",
                    "primary_lineage": "partsapi_ru",
                    "semantics_basis": "owner_clarification",
                },
            }
        ],
    }


def test_representative_profile_allows_candidate_research_without_exact_identity():
    identity = _identity()
    updated = resolver._identity_with_partsapi_agreement(identity, _group_call())

    readiness = updated["parts_lookup_readiness"]
    assert readiness["ready_for_tecdoc_candidate_lookup"] is True
    agreement = readiness["cross_source_agreement"]
    assert agreement["status"] == "group_match"
    assert agreement["identifier_matches_request"] is False
    assert agreement["requires_exact_identifier_confirmation"] is True
    assert agreement["evidence_status"] == "catalog_candidate"
    assert updated["vehicle_profile"] == identity["vehicle_profile"] == {}
    assert updated["field_statuses"] == {}
    assert updated["confidence_label"] == "low"
    for field in ("ready_for_vehicle_lookup", "ready_for_oem_lookup", "ready_for_crm_writeback"):
        assert readiness[field] is False


@pytest.mark.parametrize(
    ("field", "value", "context"),
    [
        ("make", "OTHER", None),
        ("model", "Other model", None),
        ("transmission", "Manual", None),
        ("model_year", 2025, None),
        ("displacement_cc", 3000, {"displacement_cc": 3000}),
    ],
)
def test_representative_profile_conflicts_block_lookup(field, value, context):
    call = _group_call()
    call["vehicle_profiles"][0].update(transmission="Automatic", model_year=2015)
    identity = _identity()
    identity["vehicle_profile"][field] = value

    updated = resolver._identity_with_partsapi_agreement(identity, call, independent_context=context)

    readiness = updated["parts_lookup_readiness"]
    assert readiness["ready_for_tecdoc_candidate_lookup"] is False
    assert readiness["cross_source_agreement"]["status"] == "conflict"
    assert readiness["cross_source_agreement"]["blocks_vehicle_lookup"] is True
    assert field in {row["field"] for row in readiness["cross_source_agreement"]["conflicting_fields"]}


@pytest.mark.parametrize(
    "guard",
    [
        "missing_id",
        "unknown_type",
        "type_conflict",
        "identity_error",
        "identity_errors",
        "invalid_identifier",
        "identity_conflict",
    ],
)
def test_representative_profile_keeps_other_candidate_guards(guard):
    call, identity, vehicle_type = _group_call(), _identity(), None
    if guard == "missing_id":
        call["vehicle_profiles"][0]["tecdoc_car_id"] = None
    elif guard == "unknown_type":
        call["vehicle_profiles"][0]["vehicle_type"] = None
    elif guard == "type_conflict":
        vehicle_type = "CV"
    elif guard == "identity_error":
        identity["ok"] = False
    elif guard == "identity_errors":
        identity["errors"] = ["synthetic_identity_error"]
    elif guard == "invalid_identifier":
        identity["identifier_validation"] = {"ok": False}
    elif guard == "identity_conflict":
        identity["conflicts"] = [{"field": "model", "severity": "high"}]

    updated = resolver._identity_with_partsapi_agreement(identity, call, vehicle_type=vehicle_type)
    assert updated["parts_lookup_readiness"]["ready_for_tecdoc_candidate_lookup"] is False


@pytest.mark.parametrize("operation", ["decodeVINus", "vin_decode_oe"])
def test_other_decoders_do_not_accept_representative_vin(operation):
    call = _group_call()
    call["operation"] = operation
    call["vehicle_profiles"][0]["source_operation"] = operation
    agreement = resolver._assess_partsapi_identity_agreement(_identity(), call)
    assert agreement["status"] == "identifier_mismatch"
    assert agreement["identifier_matches_request"] is False


@pytest.mark.parametrize("stale_binding", [False, True])
def test_resolver_uses_group_car_id_without_manual_navigation_or_exact_fitment(monkeypatch, stale_binding):
    calls = []

    def lookup(**kwargs):
        calls.append(kwargs)
        operation = kwargs["operation"]
        if operation == "vin_decode":
            group = _group_call()
            if stale_binding:
                wrong = binding("B" * 17, "vin")
                group["input_binding"] = group["vehicle_profiles"][0]["input_binding"] = wrong
            return {**group, "attempt_count": 1}
        if operation == "search_tree":
            return {
                "ok": True,
                "provider": "partsapi_ru",
                "operation": operation,
                "outcome": "success",
                "attempt_count": 1,
                "search_tree_rows": [{"NODE_3_TEXT": "Масляный фильтр", "NODE_3_STR_ID": "42"}],
            }
        assert operation == "articles"
        return {
            "ok": True,
            "provider": "partsapi_ru",
            "operation": operation,
            "outcome": "success",
            "attempt_count": 1,
            "article_candidates": [{"article_id": "1", "part_number": "SYNTHETIC-1", "brand": "EXAMPLE"}],
        }

    monkeypatch.setattr(resolver, "decode_vehicle_identity", lambda *_args, **_kwargs: copy.deepcopy(_identity()))
    monkeypatch.setattr(resolver, "partsapi_catalog_lookup", lookup)
    result = resolver.resolve_vin_oem_parts(
        identifier="A" * 17,
        requested_part="масляный фильтр",
        live_vpic=False,
        live_partsapi_identity=True,
        live_partsapi_oem=True,
        max_live_calls=3 if not stale_binding else 1,
    )

    if stale_binding:
        assert result["tecdoc_vehicle"]["car_id"] is None
        assert result["readiness"]["ready_for_tecdoc_candidate_lookup"] is False
        assert result["article_candidates"] == []
        assert result["calls"][0]["failure_class"] == "provider_request_binding_mismatch"
        return
    assert [call["operation"] for call in calls] == ["vin_decode", "search_tree", "articles"]
    assert calls[1]["type_id"] == calls[2]["type_id"] == "123"
    assert result["tecdoc_vehicle"]["car_id"] == "123"
    assert result["tecdoc_vehicle"]["identity_agreement"] == "group_match"
    assert result["tecdoc_vehicle"]["identifier_matches_request"] is False
    assert result["tecdoc_vehicle"]["requires_exact_identifier_confirmation"] is True
    assert result["identity"]["vehicle_profile"] == {}
    assert result["readiness"]["ready_for_vehicle_lookup"] is False
    assert result["readiness"]["ready_for_oem_candidate_lookup"] is False
    assert result["readiness"]["ready_for_crm_writeback"] is False
    assert result["article_candidates"][0]["vin_fitment_confirmed"] is False
    assert result["article_candidates"][0]["oem_number_confirmed"] is False
