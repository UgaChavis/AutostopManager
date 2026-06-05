from __future__ import annotations

import json

from autostop_manager import config as manager_config
from autostop_manager.vin_parts_benchmark import benchmark_vin_parts_lookup


PARTSAPI_ENV_NAMES = [
    "PARTSAPI_KEY",
    "PARTSAPI_VINDECODE_KEY",
    "PARTSAPI_VINDECODE_OE_KEY",
    "PARTSAPI_PARTS_BY_VIN_KEY",
    "PARTSAPI_OE_APPLICABILITY_KEY",
    "PARTSAPI_CROSSES_KEY",
    "PARTSAPI_CROSSES_WITH_BRAND_KEY",
    "PARTSAPI_CROSSES_TITLE_KEY",
    "PARTSAPI_ARTICLE_CROSSES_KEY",
    "PARTSAPI_SEARCH_ARTICLES_KEY",
    "PARTSAPI_GET_ENGINE_KEY",
    "PARTSAPI_SEARCH_TREE_KEY",
    "PARTSAPI_ARTICLES_KEY",
    "PARTSAPI_ARTICLE_KEY",
    "PARTSAPI_ARTICLE_CRITERIA_KEY",
    "PARTSAPI_BASE_URL",
]


def _clear_partsapi_env(monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/tmp/autostop-manager-test-empty.env")
    monkeypatch.setattr(manager_config, "_ENV_LOADED", False)
    for name in PARTSAPI_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_vin_parts_benchmark_reports_coverage_without_raw_identifier(monkeypatch):
    _clear_partsapi_env(monkeypatch)
    for name in [
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
    _clear_partsapi_env(monkeypatch)
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


def test_vin_parts_benchmark_allows_read_only_lookup_after_partsapi_oe_agreement(monkeypatch):
    _clear_partsapi_env(monkeypatch)

    monkeypatch.setattr(
        "autostop_manager.vin_parts_benchmark.decode_vehicle_identities",
        lambda *args, **kwargs: {
            "ok": True,
            "count": 1,
            "high_confidence_count": 0,
            "medium_confidence_count": 1,
            "low_confidence_count": 0,
            "identity_coverage": {},
            "vpic_batch": {},
            "results": [
                {
                    "confidence": 0.7,
                    "confidence_label": "medium",
                    "parts_lookup_readiness": {
                        "ready_for_oem_lookup": False,
                        "ready_for_oem_candidate_lookup": False,
                        "ready_for_crm_writeback": False,
                        "blocking_reasons": ["identity_confidence_below_high"],
                    },
                    "vehicle_profile": {"make": "HONDA", "model": "Accord", "model_year": 2003},
                    "diagnostics": {},
                    "warnings": [],
                    "conflicts": [],
                    "required_next_sources": [],
                    "evidence_sources": [],
                }
            ],
        },
    )

    def fake_partsapi_catalog_lookup(**kwargs):
        operation = kwargs["operation"]
        if operation == "vin_decode_oe":
            return {
                "ok": True,
                "provider": "partsapi_ru",
                "operation": operation,
                "partsapi_method": "VINdecodeOE",
                "request_plan": {"configured": True, "params": {"vin": "1HG***352"}, "redacted_url": "https://api.partsapi.ru?key=***"},
                "vehicle_profiles": [{"make": "HONDA", "catalog": "HONDA2017", "grade": "EXV6"}],
            }
        return {
            "ok": True,
            "provider": "partsapi_ru",
            "operation": operation,
            "partsapi_method": operation,
            "dry_run": True,
            "request_plan": {"configured": True, "params": {}, "redacted_url": "https://api.partsapi.ru?key=***"},
        }

    monkeypatch.setattr("autostop_manager.vin_parts_benchmark.partsapi_catalog_lookup", fake_partsapi_catalog_lookup)

    result = benchmark_vin_parts_lookup(
        [{"identifier": "1HGCM82633A004352", "requested_part": "передние колодки"}],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
        include_vin17_dry_run=False,
        include_oem_catalog_dry_run=False,
        live_partsapi_identity=True,
    )

    identity = result["items"][0]["identity"]
    assert identity["ready_for_oem_candidate_lookup"] is True
    assert identity["ready_for_crm_writeback"] is False
    assert identity["cross_source_agreement"]["status"] == "matched"
    assert result["summary"]["ready_for_oem_candidate_lookup_count"] == 1
    assert result["summary"]["ready_for_crm_writeback_count"] == 0


def test_vin_parts_benchmark_blocks_read_only_lookup_after_partsapi_oe_conflict(monkeypatch):
    _clear_partsapi_env(monkeypatch)

    monkeypatch.setattr(
        "autostop_manager.vin_parts_benchmark.decode_vehicle_identities",
        lambda *args, **kwargs: {
            "ok": True,
            "count": 1,
            "high_confidence_count": 0,
            "medium_confidence_count": 1,
            "low_confidence_count": 0,
            "identity_coverage": {},
            "vpic_batch": {},
            "results": [
                {
                    "confidence": 0.7,
                    "confidence_label": "medium",
                    "parts_lookup_readiness": {"ready_for_oem_lookup": False, "ready_for_oem_candidate_lookup": False, "ready_for_crm_writeback": False},
                    "vehicle_profile": {"make": "HONDA", "model": "Accord"},
                    "diagnostics": {},
                    "warnings": [],
                    "conflicts": [],
                    "required_next_sources": [],
                    "evidence_sources": [],
                }
            ],
        },
    )

    monkeypatch.setattr(
        "autostop_manager.vin_parts_benchmark.partsapi_catalog_lookup",
        lambda **kwargs: {
            "ok": True,
            "provider": "partsapi_ru",
            "operation": kwargs["operation"],
            "partsapi_method": "VINdecodeOE",
            "request_plan": {"configured": True, "params": {}, "redacted_url": "https://api.partsapi.ru?key=***"},
            "vehicle_profiles": [{"make": "TOYOTA", "catalog": "TOYOTA"}],
        },
    )

    result = benchmark_vin_parts_lookup(
        [{"identifier": "1HGCM82633A004352", "requested_part": "передние колодки"}],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
        include_vin17_dry_run=False,
        include_oem_catalog_dry_run=False,
        live_partsapi_identity=True,
    )

    identity = result["items"][0]["identity"]
    assert identity["ready_for_oem_candidate_lookup"] is False
    assert identity["cross_source_agreement"]["status"] == "conflict"
    assert "partsapi_oe_identity_conflict" in identity["blocking_reasons"]


def test_vin_parts_benchmark_prepares_three_catalog_oem_smoke_call(monkeypatch):
    _clear_partsapi_env(monkeypatch)
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


def test_vin_parts_benchmark_can_attach_oem_resolution(monkeypatch):
    _clear_partsapi_env(monkeypatch)

    def fake_resolver(**kwargs):
        return {
            "schema": "VinOemResolution",
            "status": "ready_for_live_oem_candidate_lookup",
            "identity": {
                "confidence_label": "high",
                "ready_for_oem_lookup": True,
                "ready_for_oem_candidate_lookup": True,
                "ready_for_crm_writeback": False,
                "vehicle_profile": {"make": "Honda"},
            },
            "candidate_count": 0,
            "calls": [],
            "manual_actions": [{"code": "run_live_get_parts_by_vin", "message": "call"}],
        }

    monkeypatch.setattr("autostop_manager.vin_parts_benchmark.resolve_vin_oem_parts", fake_resolver)

    result = benchmark_vin_parts_lookup(
        [{"identifier": "1HGCM82633A004352", "requested_part": "передние колодки"}],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
        include_vin17_dry_run=False,
        include_oem_catalog_dry_run=False,
        resolve_oem=True,
    )

    assert result["summary"]["oem_resolution_count"] == 1
    assert result["items"][0]["oem_resolution"]["schema"] == "VinOemResolution"
    assert result["items"][0]["identity"]["ready_for_oem_candidate_lookup"] is True
