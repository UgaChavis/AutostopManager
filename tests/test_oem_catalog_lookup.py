from __future__ import annotations

import json

from autostop_manager.catalog_clients import (
    extract_oem_candidates,
    lookup_oem_catalog_candidates,
    vin17_search_std_part_name_by_vin,
)


def test_extract_oem_candidates_handles_partsapi_and_17vin_payloads():
    partsapi_candidates = extract_oem_candidates(
        provider="partsapi_ru",
        payload={
            "data": [
                {
                    "oem": "30520-RRA-007",
                    "name": "Ignition coil",
                    "brand": "HONDA",
                    "applicability": "Accord 2.4",
                }
            ]
        },
    )
    vin17_candidates = extract_oem_candidates(
        provider="vin17_api",
        payload={
            "data": {
                "searchlist": [
                    {
                        "partnumber_original": "091140G010",
                        "name_en": "Jack handle extension",
                        "cata_name_en": "Standard tool",
                        "qty": "01",
                        "is_fit_for_this_vin": 1,
                    }
                ]
            }
        },
    )

    assert partsapi_candidates[0]["part_number"] == "30520-RRA-007"
    assert partsapi_candidates[0]["brand"] == "HONDA"
    assert vin17_candidates[0]["part_number"] == "091140G010"
    assert vin17_candidates[0]["fitment_evidence"]["is_fit_for_this_vin"] == 1


def test_vin17_std_part_name_lookup_builds_signed_request(monkeypatch):
    monkeypatch.setenv("VIN17_ACCOUNT", "myusername")
    monkeypatch.setenv("VIN17_SECRET", "mypassword")

    result = vin17_search_std_part_name_by_vin(
        epc="toyota",
        identifier="LFMGJE720DS070251",
        query_part_name="front brake pad",
        query_match_type="exact",
        dry_run=True,
    )

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["provider"] == "vin17_api"
    assert "action=search_std_part_name" in result["request_plan"]["url_parameters"]
    assert "front+brake+pad" in result["request_plan"]["url_parameters"]
    assert "token=***" in result["request_plan"]["redacted_url"]


def test_lookup_oem_catalog_candidates_combines_partsapi_and_17vin(monkeypatch):
    monkeypatch.setenv("PARTSAPI_KEY", "partsapi-secret")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example/api")
    monkeypatch.setenv("VIN17_ACCOUNT", "vin17-user")
    monkeypatch.setenv("VIN17_SECRET", "vin17-secret")

    def fake_partsapi_catalog_lookup(**kwargs):
        return {
            "ok": True,
            "provider": "partsapi_ru",
            "operation": kwargs["operation"],
            "payload": {"data": [{"oem": "04465-60280", "name": "front brake pads"}]},
        }

    def fake_vin17_search_std_part_name_by_vin(**kwargs):
        return {
            "ok": True,
            "provider": "vin17_api",
            "operation": "search_std_part_name",
            "payload": {
                "data": {
                    "searchlist": [
                        {
                            "partnumber_original": "04465-60280",
                            "name_en": "front brake pad kit",
                            "cata_name_en": "Front brake",
                            "is_fit_for_this_vin": 1,
                        }
                    ]
                }
            },
        }

    monkeypatch.setattr("autostop_manager.catalog_clients.partsapi_catalog_lookup", fake_partsapi_catalog_lookup)
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.vin17_search_std_part_name_by_vin", fake_vin17_search_std_part_name_by_vin
    )

    result = lookup_oem_catalog_candidates(
        identifier="JTEBU3FJX05027767",
        requested_part="передние колодки",
        epc="toyota",
        partsapi_category="100",
        dry_run=False,
    )

    assert result["ok"] is True
    assert result["provider_count"] == 2
    assert result["partsapi_category_resolution"]["category_kind"] == "numeric_id"
    assert {item["provider"] for item in result["provider_results"]} == {
        "partsapi_ru",
        "vin17_api",
    }
    assert len(result["oem_candidates"]) == 2
    assert result["oem_candidates"][0]["part_number"] == "04465-60280"


def test_lookup_oem_catalog_candidates_uses_partsapi_vin_decode_oe_fallback(monkeypatch):
    calls = []

    def fake_partsapi_catalog_lookup(**kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "provider": "partsapi_ru",
            "operation": kwargs["operation"],
            "payload": {
                "data": {
                    "array": {
                        "oem": "30520-RRA-007",
                        "name": "Ignition coil",
                        "brand": "HONDA",
                        "applicability": "VIN/OE decoded vehicle profile",
                    }
                }
            },
        }

    monkeypatch.setattr("autostop_manager.catalog_clients.partsapi_catalog_lookup", fake_partsapi_catalog_lookup)

    result = lookup_oem_catalog_candidates(
        identifier="JHLRD58503C000000",
        requested_part="катушка зажигания",
        dry_run=False,
    )

    assert [call["operation"] for call in calls] == ["vin_decode_oe"]
    assert result["provider_count"] == 1
    assert result["oem_candidates"][0]["part_number"] == "30520-RRA-007"
    assert any(blocker.get("fallback_operation") == "vin_decode_oe" for blocker in result["blockers"])


def test_lookup_oem_catalog_candidates_reports_unavailable_providers_as_inconclusive(monkeypatch):
    monkeypatch.setattr(
        "autostop_manager.catalog_clients.partsapi_catalog_lookup",
        lambda **kwargs: {
            "ok": False,
            "provider": "partsapi_ru",
            "operation": kwargs["operation"],
            "error": "provider unavailable",
        },
    )

    result = lookup_oem_catalog_candidates(
        identifier="SYNTHETICVIN00001",
        requested_part="масляный фильтр",
        dry_run=False,
    )

    assert result["ok"] is False
    assert result["status"] == "inconclusive"
    assert result["candidate_count"] == 0
    assert result["has_successful_provider"] is False


def test_lookup_oem_catalog_candidates_redacts_raw_identifier_from_dry_run(monkeypatch):
    monkeypatch.delenv("PARTSAPI_KEY", raising=False)
    monkeypatch.delenv("PARTSAPI_BASE_URL", raising=False)

    raw_identifier = "SYNTHETICVIN00001"

    result = lookup_oem_catalog_candidates(
        identifier=raw_identifier,
        requested_part="топливные форсунки",
        dry_run=True,
    )

    serialized = json.dumps(result, ensure_ascii=False)
    assert raw_identifier not in serialized
    assert "SYN***001" in serialized
