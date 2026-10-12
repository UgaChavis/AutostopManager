from __future__ import annotations

import json

import pytest

from autostop_manager import vin_oem_resolver, vin_parts_benchmark
from autostop_manager.vehicle_identity_inputs import validate_identity_input


VIN = "A" * 8 + "XJ" + "A" * 7
OTHER_VIN = "A" * 8 + "XL" + "B" * 7
FACTS = {
    "make": "Volkswagen",
    "model": "Touareg",
    "engine": "DENA",
    "production_year": 2019,
    "model_year": 2020,
}


def _identity(facts):
    return {
        "ok": True,
        "confidence": 0.8,
        "confidence_label": "medium",
        "vehicle_profile": dict(facts),
        "conflicts": [],
        "warnings": [],
        "field_evidence": [{"source": "CRM context", "field": field, "value": value} for field, value in facts.items()],
        "parts_lookup_readiness": {"ready_for_vehicle_lookup": True, "ready_for_family_lookup": True},
    }


def _cars():
    return [
        {
            "tecdoc_car_id": 101,
            "engine_code": "DENA",
            "power_kw_from": 165,
            "production_date_from": "2018-05-01",
            "production_date_to": "2020-12-31",
        },
        {
            "tecdoc_car_id": 102,
            "engine_code": "DENA",
            "power_kw_from": 165,
            "production_date_from": "2021-01-01",
            "production_date_to": None,
        },
    ]


def _install(monkeypatch, *, outcome="identifier_mismatch", source_fields=None, cars=None, failure=None):
    calls = []

    def lookup(**kwargs):
        calls.append(kwargs)
        operation = kwargs["operation"]
        dry = kwargs.get("dry_run", False)
        failed = operation == failure and not dry
        response = {
            "operation": operation,
            "ok": not failed,
            "outcome": "upstream_failure" if failed else "success",
            "dry_run": dry,
            "attempt_count": 0 if dry else 1,
            "request_plan": {"configured": True},
            "vehicle_profiles": [],
        }
        if failed:
            return response
        if operation == "vin_decode":
            response.update(
                ok=outcome != "identifier_mismatch",
                outcome=outcome,
                failure_class="provider_identifier_mismatch" if outcome == "identifier_mismatch" else None,
                vehicle_profiles=[
                    {
                        "make": "Volkswagen",
                        "model": "Touareg",
                        "tecdoc_car_id": 999,
                        "vehicle_type": "PC",
                        "engine_code": "WRONG",
                        "model_year": 2014,
                        "identifier_matches_request": False
                        if outcome == "identifier_mismatch"
                        else None
                        if outcome == "identifier_unverified"
                        else True,
                        **(source_fields or {}),
                    }
                ],
            )
        elif operation == "getMakes":
            response["payload"] = {"data": [{"makeId": 121, "makeName": "Volkswagen"}]}
        elif operation == "getModels":
            response["payload"] = [{"modelId": 456, "modelName": "Touareg (CR)"}]
        elif operation == "getCars":
            response["vehicle_profiles"] = _cars() if cars is None else cars
        elif operation == "search_tree":
            response["search_tree_rows"] = [{"NODE_3_TEXT": "Колодки тормозные", "NODE_3_STR_ID": 100470}]
        elif operation == "articles":
            response["article_candidates"] = [
                {"part_number": "TEST-1", "brand": "Test", "product_name": "Front brake pads"}
            ]
        else:
            pytest.fail(f"Unexpected catalog operation: {operation}")
        return response

    monkeypatch.setattr(vin_oem_resolver, "partsapi_catalog_lookup", lookup)
    monkeypatch.setattr(vin_parts_benchmark, "partsapi_catalog_lookup", lookup)
    monkeypatch.setattr(
        vin_oem_resolver,
        "decode_vehicle_identity",
        lambda _identifier, **kw: _identity(validate_identity_input(_identifier, kw["crm_context"])["context"]),
    )
    monkeypatch.setattr(
        vin_parts_benchmark,
        "decode_vehicle_identities",
        lambda items, **kw: {
            "ok": True,
            "results": [
                _identity(validate_identity_input(item["identifier"], item["crm_context"])["context"]) for item in items
            ],
        },
    )
    return calls


def _resolve(facts=None, **kwargs):
    return vin_oem_resolver.resolve_vin_oem_parts(
        identifier=VIN,
        requested_part="передние колодки",
        crm_context=FACTS if facts is None else facts,
        vehicle_type="PC",
        live_vpic=False,
        live_partsapi_oem=True,
        max_live_calls=6,
        **kwargs,
    )


@pytest.mark.parametrize("outcome", ["identifier_mismatch", "identifier_unverified", "upstream_failure"])
def test_rejected_decode_uses_independent_catalogue_route(monkeypatch, outcome):
    calls = _install(monkeypatch, outcome=outcome, failure="vin_decode" if outcome == "upstream_failure" else None)
    facts = dict(FACTS)
    result = _resolve(facts)
    assert [call["operation"] for call in calls] == [
        "vin_decode",
        "getMakes",
        "getModels",
        "getCars",
        "search_tree",
        "articles",
    ]
    assert calls[0]["identifier"] == VIN
    assert calls[-1]["type_id"] == "101"
    assert all("identifier" not in call for call in calls[1:])
    assert result["status"] == "tecdoc_articles_found_needs_manual_fitment"
    assert result["tecdoc_vehicle_fallback"]["status"] == "matched"
    assert result["tecdoc_vehicle"]["selection_route"] == "vehicle_context"
    assert result["tecdoc_vehicle"]["requires_exact_identifier_confirmation"] is True
    assert result["article_candidates"][0]["vin_fitment_confirmed"] is False
    assert result["readiness"]["ready_for_crm_writeback"] is False
    assert result["source_failures"][0]["operation"] == "vin_decode"
    assert not any(row.get("operation") == "vin_decode" for row in result["blockers"])
    assert result["identity"]["vehicle_profile"]["model_year"] == 2020
    assert facts == FACTS
    assert VIN not in json.dumps(result)


@pytest.mark.parametrize(
    ("vin", "year", "model_year", "car_id"), [(VIN, 2019, 2020, "101"), (OTHER_VIN, 2021, 2022, "102")]
)
def test_same_prefix_different_years_do_not_reuse_catalogue_vin(monkeypatch, vin, year, model_year, car_id):
    calls = _install(monkeypatch)
    result = vin_oem_resolver.resolve_vin_oem_parts(
        identifier=vin,
        requested_part="передние колодки",
        crm_context={**FACTS, "production_year": year, "model_year": model_year},
        vehicle_type="PC",
        live_vpic=False,
        live_partsapi_oem=True,
        max_live_calls=6,
    )
    assert calls[0]["identifier"] == vin
    assert result["tecdoc_vehicle"]["car_id"] == car_id
    assert result["identity"]["vehicle_profile"]["production_year"] == year
    assert result["identity"]["vehicle_profile"]["model_year"] == model_year


@pytest.mark.parametrize(
    "field,value",
    [
        ("engine_code", "OTHER"),
        ("model", "Golf"),
        ("modification", "WRONG"),
        ("drive_type", "FWD"),
        ("power_kw_from", 240),
    ],
)
def test_bound_response_real_characteristic_conflicts_block_all_exact_routes(monkeypatch, field, value):
    facts = {**FACTS, "modification": "3.0 TDI", "drivetrain": "AWD", "power_kw": 165}
    source = {
        "engine_code": "DENA",
        "model_year": 2020,
        "modification": "3.0 TDI",
        "drive_type": "AWD",
        "power_kw_from": 165,
        field: value,
    }
    calls = _install(monkeypatch, outcome="success", source_fields=source)
    result = _resolve(facts)
    assert [call["operation"] for call in calls] == ["vin_decode"]
    assert result["identity"]["cross_source_agreement"]["status"] == "conflict"
    assert result["identity"]["cross_source_agreement"]["blocks_vehicle_lookup"] is True
    assert result["tecdoc_vehicle_fallback"] is None
    assert result["article_candidates"] == []


def test_multiple_modifications_request_power_without_selecting_first(monkeypatch):
    calls = _install(
        monkeypatch,
        cars=[
            {"tecdoc_car_id": 101, "engine_code": "DENA", "power_kw_from": 165},
            {"tecdoc_car_id": 102, "engine_code": "DENA", "power_kw_from": 240},
        ],
    )
    result = _resolve()
    fallback = result["tecdoc_vehicle_fallback"]
    assert fallback["status"] == "ambiguous_vehicle_modification"
    assert fallback["missing_fields"] == ["power_kw"]
    assert fallback["selected_profile"] is None
    assert result["tecdoc_vehicle"]["car_id"] is None
    assert not any(call["operation"] == "search_tree" for call in calls)


@pytest.mark.parametrize("stage", ["getMakes", "getModels", "getCars"])
def test_fallback_outage_is_provider_failure_not_absent_vehicle(monkeypatch, stage):
    calls = _install(monkeypatch, failure=stage)
    result = _resolve()
    assert result["tecdoc_vehicle_fallback"]["status"] == "provider_failed"
    assert result["tecdoc_vehicle_fallback"]["provider_outcome"] == "upstream_failure"
    assert result["status"] == "tecdoc_lookup_provider_failed"
    assert calls[-1]["operation"] == stage
    assert result["article_candidates"] == []


@pytest.mark.parametrize("resolve_oem", [False, True])
def test_benchmark_and_resolver_share_fallback_without_redecoding(monkeypatch, resolve_oem):
    calls = _install(monkeypatch)
    result = vin_parts_benchmark.benchmark_vin_parts_lookup(
        [{"identifier": VIN, "crm_context": FACTS, "vehicle_type": "PC"}],
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_identity=True,
        live_partsapi_oem=True,
        resolve_oem=resolve_oem,
        max_live_calls=6,
    )
    row = result["items"][0]
    selection = row["oem_resolution"]["tecdoc_vehicle_fallback"] if resolve_oem else row["tecdoc_vehicle_fallback"]
    assert selection["status"] == "matched"
    assert selection["selected_profile"]["tecdoc_car_id"] == 101
    assert sum(call["operation"] == "vin_decode" and not call["dry_run"] for call in calls) == 1
    assert row["identity"]["ready_for_tecdoc_candidate_lookup"] is True
    assert row["identity"]["ready_for_crm_writeback"] is False
    assert VIN not in json.dumps(result)


@pytest.mark.parametrize(
    "field,value", [("engine", "OTHER"), ("vin", OTHER_VIN), ("power_kw", float("nan")), ("displacement_cc", True)]
)
def test_conflicting_or_malformed_context_is_rejected_before_catalogue(monkeypatch, field, value):
    calls = _install(monkeypatch)
    context = {**FACTS, field: value}
    kwargs = {"engine": "DENA"} if field == "engine" else {}
    result = _resolve(context, **kwargs)
    assert result["identity"]["ok"] is False
    assert calls == []


def test_predecoded_binding_includes_power(monkeypatch):
    from autostop_manager.vin_oem_resolver import _predecoded_identity_input_matches

    binding = validate_identity_input(VIN, {**FACTS, "power_kw": 165})
    assert not _predecoded_identity_input_matches(binding, VIN, {**FACTS, "power_kw": 240}, identifier_type="auto")


def test_fallback_keeps_global_live_budget(monkeypatch):
    calls = _install(monkeypatch)
    result = vin_oem_resolver.resolve_vin_oem_parts(
        identifier=VIN,
        requested_part="передние колодки",
        crm_context=FACTS,
        vehicle_type="PC",
        live_vpic=False,
        live_partsapi_oem=True,
        max_live_calls=2,
    )
    assert result["live_call_count"] == 2
    assert sum(not call["dry_run"] for call in calls) == 2
    assert calls[-1]["dry_run"] is True
    assert result["tecdoc_vehicle_fallback"]["status"] == "not_checked"
    assert any(row["stage"] == "partsapi_live_budget" for row in result["blockers"])


def test_independent_power_conflict_revokes_exact_readiness_after_bad_decode(monkeypatch):
    calls = _install(monkeypatch)
    identity = _identity({**FACTS, "power_kw": 165})
    identity["field_evidence"].append({"source": "NHTSA vPIC", "bound": True, "field": "power_kw", "value": 240})
    identity["parts_lookup_readiness"].update(ready_for_oem_lookup=True, ready_for_oem_candidate_lookup=True)
    monkeypatch.setattr(vin_oem_resolver, "decode_vehicle_identity", lambda *a, **kw: identity)
    result = _resolve({**FACTS, "power_kw": 165})
    assert [call["operation"] for call in calls] == ["vin_decode"]
    assert result["tecdoc_vehicle_fallback"]["conflict_scope"] == "independent_vehicle"
    assert result["identity"]["ready_for_vehicle_lookup"] is False
    assert result["identity"]["ready_for_oem_candidate_lookup"] is False
    assert result["identity"]["field_statuses"]["power_kw"]["status"] == "disputed"
    assert "power_kw" not in result["identity"]["vehicle_profile"]


@pytest.mark.parametrize("field,left,right", [("engine", "DENA", "OTHER"), ("power_kw", 165, 240)])
def test_nested_independent_facts_are_not_silently_overwritten(monkeypatch, field, left, right):
    calls = _install(monkeypatch)
    context = {**FACTS, field: left, "vehicle_profile": {field: right}}
    result = _resolve(context)
    assert result["identity"]["ok"] is False
    assert calls == []
