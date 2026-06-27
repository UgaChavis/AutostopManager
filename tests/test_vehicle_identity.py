from __future__ import annotations

import json

from autostop_manager.vehicle_identity import decode_vehicle_identities, decode_vehicle_identity
from autostop_manager.vin_lookup import classify_identifier


def test_decode_vehicle_identity_builds_high_confidence_clean_us_vin(monkeypatch):
    def fake_decode_vin_vpic(vin: str, *, model_year: int | None = None, timeout: float = 10.0):
        return {
            "ok": True,
            "source": "NHTSA vPIC",
            "request_url": "https://vpic.example.test",
            "vin": vin,
            "vehicle": {
                "make": "Jeep",
                "model": "Grand Cherokee",
                "modelyear": 2012,
                "bodyclass": "Sport Utility Vehicle (SUV)/Multi-Purpose Vehicle (MPV)",
                "plantcountry": "UNITED STATES (USA)",
                "enginecylinders": "8",
                "drivetype": "4WD/4-Wheel Drive/4x4",
            },
        }

    monkeypatch.setattr("autostop_manager.vehicle_identity.decode_vin_vpic", fake_decode_vin_vpic)

    result = decode_vehicle_identity(
        "1C4RJFCT9CC000000",
        crm_context={"make": "Jeep", "model": "Grand Cherokee", "model_year": 2012},
    )

    assert result["confidence_label"] == "high"
    assert result["vehicle_profile"]["make"] == "Jeep"
    assert result["vehicle_profile"]["model"] == "Grand Cherokee"
    assert result["vehicle_profile"]["engine"] == "5.7 V8 gasoline"
    assert result["diagnostics"]["check_digit"]["status"] == "pass"
    assert result["parts_lookup_readiness"]["ready_for_oem_lookup"] is True
    assert result["parts_lookup_readiness"]["ready_for_oem_candidate_lookup"] is True
    assert result["parts_lookup_readiness"]["ready_for_crm_writeback"] is True
    assert result["parts_lookup_readiness"]["cross_source_agreement"]["status"] == "not_checked"


def test_decode_vehicle_identity_handles_jdm_frame_without_pretending_it_is_iso_vin():
    result = decode_vehicle_identity(
        "MR41S123456",
        crm_context={"make": "Suzuki", "model": "Hustler", "model_year": 2018},
        live_vpic=False,
    )

    assert result["identifier"]["kind"] == "market_code"
    assert result["diagnostics"]["frame_query_hint"] == "MR4***456"
    assert result["vehicle_profile"]["make"] == "Suzuki"
    assert result["vehicle_profile"]["model"] == "Hustler"
    assert result["vehicle_profile"]["platform"] == "MR41S"
    assert any(source["source_id"] == "parts_catalogs_api" for source in result["required_next_sources"])
    assert any("not treat it as a 17-character ISO VIN" in warning for warning in result["warnings"])


def test_decode_vehicle_identity_reports_row_vin_caveats_without_demoting_identity(monkeypatch):
    def fake_decode_vin_vpic(vin: str, *, model_year: int | None = None, timeout: float = 10.0):
        return {
            "ok": True,
            "source": "NHTSA vPIC",
            "request_url": "https://vpic.example.test",
            "vin": vin,
            "vehicle": {},
        }

    monkeypatch.setattr("autostop_manager.vehicle_identity.decode_vin_vpic", fake_decode_vin_vpic)

    result = decode_vehicle_identity(
        "XW8AC2NH9JK000000",
        crm_context={"make": "Skoda", "model": "Rapid", "model_year": 2020},
    )

    assert result["vehicle_profile"]["make"] == "Skoda"
    assert result["vehicle_profile"]["model"] == "Rapid"
    assert result["confidence_label"] == "high"
    assert result["conflicts"] == []
    assert any(source["source_id"] == "partsapi_ru" for source in result["required_next_sources"])


def test_classify_identifier_keeps_existing_market_code_behavior_for_unhyphenated_frame():
    identifier = classify_identifier("MR41S123456")

    assert identifier.kind == "market_code"
    assert identifier.normalized == "MR41S123456"


def test_decode_vehicle_identities_reads_nested_crm_vehicle_profile():
    result = decode_vehicle_identities(
        [
            {
                "vehicle_profile_compact": {
                    "vin": "MR41S123456",
                    "make_display": "Suzuki",
                    "model_display": "Hustler",
                    "production_year": 2018,
                    "source_confidence": 0.95,
                }
            }
        ],
        live_vpic=False,
    )

    item = result["results"][0]
    assert item["vehicle_profile"]["make"] == "Suzuki"
    assert item["vehicle_profile"]["model"] == "Hustler"
    assert item["vehicle_profile"]["model_year"] == 2018
    assert result["identity_coverage"]["needs_epc_or_document_check_count"] == 1


def test_decode_vehicle_identities_normalizes_common_crm_make_typos():
    result = decode_vehicle_identities(
        [
            {
                "vehicle_profile_compact": {
                    "vin": "XW8ZZZ61ZJG000000",
                    "make_display": "Volkskwagen",
                    "model_display": "Polo",
                    "production_year": 2018,
                    "source_confidence": 0.95,
                }
            }
        ],
        live_vpic=False,
    )

    assert result["results"][0]["vehicle_profile"]["make"] == "Volkswagen"


def test_decode_vehicle_identity_treats_european_check_digit_failure_as_caveat_not_conflict():
    result = decode_vehicle_identity(
        "WVWZZZAUZFP000000",
        crm_context={"make": "Volkswagen", "model": "Golf", "model_year": 2014, "source_confidence": 0.95},
        live_vpic=False,
    )

    assert result["vehicle_profile"]["make"] == "Volkswagen"
    assert result["vehicle_profile"]["platform"] == "Mk7 / MQB AU"
    assert result["diagnostics"]["check_digit"]["status"] == "fail"
    assert result["conflicts"] == []
    assert result["confidence_label"] == "high"


def test_decode_vehicle_identity_recognizes_honda_es1_frame_pattern():
    result = decode_vehicle_identity(
        "ES19999999",
        crm_context={"make": "Honda", "model": "Civic", "source_confidence": 0.95},
        live_vpic=False,
    )

    assert result["identifier"]["kind"] == "market_code"
    assert result["diagnostics"]["frame_query_hint"] == "ES1***999"
    assert result["vehicle_profile"]["platform"] == "ES1"
    assert result["confidence_label"] == "high"


def test_decode_vehicle_identity_redacts_raw_identifier_and_honors_no_live_vpic(monkeypatch):
    raw_vin = "JTEBU3FJX05027767"

    def fail_decode(*args, **kwargs):
        raise AssertionError("vPIC should not be called when live_vpic is false")

    monkeypatch.setattr("autostop_manager.vehicle_identity.decode_vin_vpic", fail_decode)
    monkeypatch.setattr("autostop_manager.vin_lookup.decode_vin_vpic", fail_decode)

    result = decode_vehicle_identity(raw_vin, live_vpic=False, live_wmi=False)
    rendered = json.dumps(result, ensure_ascii=False)

    assert raw_vin not in rendered
    assert result["identifier"]["redacted"]["display"] == "JTE***767"
    assert result["normalized_query"] == "JTE***767"
    assert result["privacy"]["raw_identifier_redacted_from_output"] is True
    assert result["lookup_plan"]["identifier"]["redacted"]["display"] == "JTE***767"
