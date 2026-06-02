from __future__ import annotations

import json

from autostop_manager.catalog_clients import (
    build_parts_catalogs_request,
    extract_oem_candidates,
    lookup_oem_catalog_candidates,
    parts_catalogs_lookup,
    vin17_search_std_part_name_by_vin,
)


class _FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_parts_catalogs_request_uses_authorization_header_and_redacts_key():
    request = build_parts_catalogs_request(
        operation="car_info",
        params={"vin": "JTEBU3FJX05027767"},
        api_key="pc-secret",
        base_url="https://api.parts-catalogs.example/v1",
    )

    assert request["ok"] is True
    assert request["url"] == "https://api.parts-catalogs.example/v1/car/info?vin=JTEBU3FJX05027767"
    assert request["headers"]["Authorization"] == "pc-secret"
    assert request["redacted_headers"]["Authorization"] == "***"
    assert request["secret_exposed"] is False


def test_parts_catalogs_lookup_normalizes_parts_payload(monkeypatch):
    monkeypatch.setenv("PARTS_CATALOGS_API_KEY", "pc-secret")
    monkeypatch.setenv("PARTS_CATALOGS_BASE_URL", "https://api.parts-catalogs.example/v1")

    def fake_urlopen(request, timeout=20.0):
        assert request.full_url.endswith("/catalogs/toyota/parts2?carId=car-1&groupId=front-brake")
        assert request.headers["Authorization"] == "pc-secret"
        return _FakeResponse(
            {
                "parts": [
                    {
                        "number": "04465-60280",
                        "name": "PAD KIT, DISC BRAKE, FRONT",
                        "groupName": "Front disc brake caliper & dust cover",
                        "quantity": "1",
                        "applicability": "GRJ150, production 2009-2013",
                    }
                ]
            }
        )

    monkeypatch.setattr("autostop_manager.catalog_clients.urlopen", fake_urlopen)

    result = parts_catalogs_lookup(
        operation="parts",
        catalog_id="toyota",
        car_id="car-1",
        group_id="front-brake",
    )

    assert result["ok"] is True
    assert result["provider"] == "parts_catalogs_api"
    assert result["oem_candidates"][0]["part_number"] == "04465-60280"
    assert result["oem_candidates"][0]["provider"] == "parts_catalogs_api"
    assert result["oem_candidates"][0]["fitment_evidence"]["group"] == "Front disc brake caliper & dust cover"


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


def test_lookup_oem_catalog_candidates_combines_three_catalogs(monkeypatch):
    monkeypatch.setenv("PARTS_CATALOGS_API_KEY", "pc-secret")
    monkeypatch.setenv("PARTS_CATALOGS_BASE_URL", "https://api.parts-catalogs.example/v1")
    monkeypatch.setenv("PARTSAPI_KEY", "partsapi-secret")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example/api")
    monkeypatch.setenv("VIN17_ACCOUNT", "vin17-user")
    monkeypatch.setenv("VIN17_SECRET", "vin17-secret")

    def fake_parts_catalogs_lookup(**kwargs):
        return {
            "ok": True,
            "provider": "parts_catalogs_api",
            "operation": kwargs["operation"],
            "oem_candidates": [
                {
                    "provider": "parts_catalogs_api",
                    "part_number": "04465-60280",
                    "name": "PAD KIT, DISC BRAKE",
                    "source_operation": "parts",
                    "fitment_evidence": {"group": "front brake"},
                }
            ],
        }

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

    monkeypatch.setattr("autostop_manager.catalog_clients.parts_catalogs_lookup", fake_parts_catalogs_lookup)
    monkeypatch.setattr("autostop_manager.catalog_clients.partsapi_catalog_lookup", fake_partsapi_catalog_lookup)
    monkeypatch.setattr("autostop_manager.catalog_clients.vin17_search_std_part_name_by_vin", fake_vin17_search_std_part_name_by_vin)

    result = lookup_oem_catalog_candidates(
        identifier="JTEBU3FJX05027767",
        requested_part="передние колодки",
        catalog_id="toyota",
        car_id="car-1",
        group_id="front-brake",
        epc="toyota",
        partsapi_category="100",
        dry_run=False,
    )

    assert result["ok"] is True
    assert result["provider_count"] == 3
    assert result["partsapi_category_resolution"]["category_kind"] == "numeric_id"
    assert {item["provider"] for item in result["provider_results"]} == {
        "parts_catalogs_api",
        "partsapi_ru",
        "vin17_api",
    }
    assert len(result["oem_candidates"]) == 3
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
