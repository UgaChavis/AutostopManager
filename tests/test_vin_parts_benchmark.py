from __future__ import annotations

import json

from autostop_manager.vin_parts_benchmark import benchmark_vin_parts_lookup


def test_vin_parts_benchmark_reports_coverage_without_raw_identifier(monkeypatch):
    for name in [
        "PARTSAPI_KEY",
        "PARTSAPI_BASE_URL",
        "PARTS_CATALOGS_API_KEY",
        "PARTS_CATALOGS_BASE_URL",
        "VIN17_ACCOUNT",
        "VIN17_SECRET",
        "ROSSKO_KEY1",
        "ROSSKO_KEY2",
        "AUTOEURO_API_KEY",
        "ARMTEK_LOGIN",
        "ARMTEK_PASSWORD",
        "AUTOPITER_USER_ID",
        "AUTOPITER_PASSWORD",
    ]:
        monkeypatch.delenv(name, raising=False)

    items = [
        {
            "identifier": "MR41S123456",
            "make": "Suzuki",
            "model": "Hustler",
            "model_year": 2018,
        },
        {
            "identifier": "XW8ZZZ61ZJG000000",
            "requested_part": "стойка стабилизатора",
            "vehicle_profile_compact": {
                "make_display": "Volkskwagen",
                "model_display": "Polo",
                "production_year": 2018,
            },
        },
    ]

    result = benchmark_vin_parts_lookup(
        items,
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
    )

    assert result["ok"] is True
    assert result["summary"]["count"] == 2
    assert result["summary"]["part_intent_recognized_count"] == 2
    assert result["summary"]["manual_public_search_count"] == 6
    assert result["summary"]["manual_public_queries_with_raw_identifier_count"] == 0
    assert result["summary"]["full_auto_lookup_count"] == 0
    assert "PARTSAPI_KEY" in result["summary"]["missing_env_names"]
    assert "PARTSAPI_BASE_URL" in result["summary"]["missing_env_names"]
    assert result["items"][0]["prepared_calls"]["partsapi"][0]["request_param_names"] == ["vin"]
    assert result["items"][0]["prepared_calls"]["vin17"]["missing_env_names"] == ["VIN17_ACCOUNT", "VIN17_SECRET"]

    rendered = json.dumps(result, ensure_ascii=False)
    assert "MR41S123456" not in rendered
    assert "MR41S-123456" not in rendered
    assert "XW8ZZZ61ZJG000000" not in rendered
    assert "Volkswagen" in rendered


def test_vin_parts_benchmark_status_tracks_identity_ready_but_missing_live_sources(monkeypatch):
    monkeypatch.delenv("PARTSAPI_KEY", raising=False)
    monkeypatch.delenv("PARTSAPI_BASE_URL", raising=False)
    monkeypatch.delenv("PARTS_CATALOGS_API_KEY", raising=False)
    monkeypatch.delenv("PARTS_CATALOGS_BASE_URL", raising=False)
    monkeypatch.delenv("VIN17_ACCOUNT", raising=False)
    monkeypatch.delenv("VIN17_SECRET", raising=False)

    result = benchmark_vin_parts_lookup(
        [
            {
                "identifier": "1C4RJFCT9CC000000",
                "make": "Jeep",
                "model": "Grand Cherokee",
                "model_year": 2012,
                "engine": "5.7 V8",
            }
        ],
        requested_part="передние колодки",
        live_vpic=False,
    )

    assert result["summary"]["high_identity_count"] == 1
    assert result["summary"]["ready_for_oem_lookup_count"] == 1
    assert result["summary"]["benchmark_status"] == "identity_ready_but_blocked_by_live_catalog_or_supplier_credentials"
    assert "oem_catalog" in result["blockers_by_stage"]
    assert result["privacy"]["raw_identifier_redacted_from_output"] is True


def test_vin_parts_benchmark_prepares_three_catalog_oem_smoke_call(monkeypatch):
    monkeypatch.setenv("PARTS_CATALOGS_API_KEY", "pc-secret")
    monkeypatch.setenv("PARTS_CATALOGS_BASE_URL", "https://api.parts-catalogs.example/v1")
    monkeypatch.setenv("PARTSAPI_KEY", "partsapi-secret")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example/api")
    monkeypatch.setenv("VIN17_ACCOUNT", "vin17-user")
    monkeypatch.setenv("VIN17_SECRET", "vin17-secret")

    result = benchmark_vin_parts_lookup(
        [
            {
                "identifier": "JTEBU3FJX05027767",
                "make": "Toyota",
                "model": "Land Cruiser Prado 150",
                "model_year": 2012,
                "engine": "1GR-FE",
                "catalog_id": "toyota",
                "car_id": "car-1",
                "group_id": "front-brake",
                "epc": "toyota",
            }
        ],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
    )

    smoke = result["items"][0]["prepared_calls"]["oem_catalog_lookup"]
    rendered = json.dumps(result, ensure_ascii=False)

    assert result["summary"]["oem_catalog_request_shape_count"] == 1
    assert smoke["provider"] == "multi_oem_catalog_lookup"
    assert smoke["provider_count"] == 3
    assert smoke["ok"] is True
    assert smoke["blockers"] == []
    assert "JTEBU3FJX05027767" not in rendered
