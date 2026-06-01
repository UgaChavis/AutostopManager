from __future__ import annotations

from autostop_manager.catalog_adapters import build_oem_parts_provider_plan, catalog_provider_status
from autostop_manager.vehicle_identity import decode_vehicle_identity


def test_catalog_provider_status_reports_missing_secret_names(monkeypatch):
    for name in ["PARTSAPI_KEY", "PARTSAPI_BASE_URL", "PARTS_CATALOGS_API_KEY", "PARTS_CATALOGS_BASE_URL", "ROSSKO_KEY1", "ROSSKO_KEY2"]:
        monkeypatch.delenv(name, raising=False)

    status = catalog_provider_status()

    assert status["ok"] is True
    assert any(provider["source_id"] == "nhtsa_vpic" and provider["configured"] for provider in status["providers"])
    assert any(provider["source_id"] == "mann_filter_catalog" and provider["live_callable_now"] for provider in status["providers"])
    assert any(provider["source_id"] == "denso_aftermarket_catalog" and provider["live_callable_now"] for provider in status["providers"])
    partsapi = next(provider for provider in status["providers"] if provider["source_id"] == "partsapi_ru")
    assert partsapi["configured"] is False
    assert partsapi["missing_env_names"] == ["PARTSAPI_KEY", "PARTSAPI_BASE_URL"]


def test_aftermarket_catalog_status_has_two_public_live_sources():
    status = catalog_provider_status(stage="aftermarket_catalog")

    assert status["ok"] is True
    assert status["configured_count"] == 2
    assert status["live_callable_count"] == 2
    assert {provider["source_id"] for provider in status["providers"]} == {"mann_filter_catalog", "denso_aftermarket_catalog"}


def test_catalog_provider_status_detects_configured_partsapi(monkeypatch):
    monkeypatch.setenv("PARTSAPI_KEY", "test-secret")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    status = catalog_provider_status(stage="catalog_cross")
    partsapi = next(provider for provider in status["providers"] if provider["source_id"] == "partsapi_ru")

    assert partsapi["configured"] is True
    assert partsapi["live_callable_now"] is True
    assert partsapi["present_env_names"] == ["PARTSAPI_KEY", "PARTSAPI_BASE_URL"]


def test_catalog_provider_status_detects_configured_17vin_account(monkeypatch):
    monkeypatch.setenv("VIN17_ACCOUNT", "test-user")
    monkeypatch.setenv("VIN17_SECRET", "test-secret")

    status = catalog_provider_status(stage="oem_catalog")
    vin17 = next(provider for provider in status["providers"] if provider["source_id"] == "vin17_api")

    assert vin17["configured"] is True
    assert vin17["live_callable_now"] is True
    assert vin17["present_env_names"] == ["VIN17_ACCOUNT", "VIN17_SECRET"]


def test_oem_parts_provider_plan_redacts_identifier_and_reports_blockers(monkeypatch):
    for name in ["PARTSAPI_KEY", "PARTSAPI_BASE_URL", "PARTS_CATALOGS_API_KEY", "PARTS_CATALOGS_BASE_URL", "ROSSKO_KEY1", "ROSSKO_KEY2"]:
        monkeypatch.delenv(name, raising=False)

    identity = decode_vehicle_identity(
        "MR41S123456",
        crm_context={"make": "Suzuki", "model": "Hustler", "model_year": 2018},
        live_vpic=False,
    )
    plan = build_oem_parts_provider_plan(
        identifier="MR41S123456",
        requested_part="передние колодки",
        vehicle_identity=identity,
    )

    assert plan["identifier"]["redacted"]["display"] == "MR4***456"
    assert plan["live_capability"]["live_oem_catalog_available"] is False
    assert plan["live_capability"]["live_aftermarket_catalog_available"] is True
    assert plan["live_capability"]["can_complete_full_auto_lookup_now"] is False
    assert plan["requested_part_profile"]["intent_id"] == "front_brake_pads"
    assert any(blocker["stage"] == "oem_catalog" for blocker in plan["blockers"])
    assert any(step["step"] == "find_oem_candidates" for step in plan["pipeline"])
    assert any(step["step"] == "lookup_public_aftermarket_catalogs" for step in plan["pipeline"])
    assert plan["manual_public_search_queries"]
    combined_queries = "\n".join(item["query"] + "\n" + item["url"] for item in plan["manual_public_search_queries"])
    assert "MR41S123456" not in combined_queries
    assert "Suzuki" in combined_queries
