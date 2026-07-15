from __future__ import annotations

from autostop_manager import config as manager_config
from autostop_manager.catalog_adapters import build_oem_parts_provider_plan, catalog_provider_status
from autostop_manager.vehicle_identity import decode_vehicle_identity


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
    "PARTSAPI_BASE_URL",
]


def _clear_partsapi_env(monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/tmp/autostop-manager-test-empty.env")
    monkeypatch.setattr(manager_config, "_ENV_LOADED", False)
    for name in PARTSAPI_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_catalog_provider_status_reports_missing_secret_names(monkeypatch):
    _clear_partsapi_env(monkeypatch)
    for name in ["PARTS_CATALOGS_API_KEY", "PARTS_CATALOGS_BASE_URL", "ROSSKO_KEY1", "ROSSKO_KEY2"]:
        monkeypatch.delenv(name, raising=False)

    status = catalog_provider_status()

    assert status["ok"] is True
    assert any(provider["source_id"] == "nhtsa_vpic" and provider["configured"] for provider in status["providers"])
    assert any(
        provider["source_id"] == "mann_filter_catalog" and provider["live_callable_now"]
        for provider in status["providers"]
    )
    assert any(
        provider["source_id"] == "denso_aftermarket_catalog" and provider["live_callable_now"]
        for provider in status["providers"]
    )
    partsapi = next(provider for provider in status["providers"] if provider["source_id"] == "partsapi_ru")
    assert partsapi["configured"] is False
    assert "PARTSAPI_BASE_URL" in partsapi["missing_env_names"]
    assert "PARTSAPI_KEY" in partsapi["missing_env_names"]
    assert ["PARTSAPI_KEY"] in partsapi["missing_env_groups"]
    assert ["PARTSAPI_PARTS_BY_VIN_KEY"] in partsapi["missing_env_groups"]


def test_aftermarket_catalog_status_has_two_public_live_sources():
    status = catalog_provider_status(stage="aftermarket_catalog")

    assert status["ok"] is True
    assert status["configured_count"] == 2
    assert status["live_callable_count"] == 2
    assert {provider["source_id"] for provider in status["providers"]} == {
        "mann_filter_catalog",
        "denso_aftermarket_catalog",
    }


def test_catalog_provider_status_detects_configured_partsapi(monkeypatch):
    _clear_partsapi_env(monkeypatch)
    monkeypatch.setenv("PARTSAPI_KEY", "test-secret")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    status = catalog_provider_status(stage="catalog_cross")
    partsapi = next(provider for provider in status["providers"] if provider["source_id"] == "partsapi_ru")

    assert partsapi["configured"] is True
    assert partsapi["live_callable_now"] is True
    assert partsapi["present_env_names"] == ["PARTSAPI_BASE_URL", "PARTSAPI_KEY"]


def test_catalog_provider_status_detects_configured_partsapi_method_key(monkeypatch):
    _clear_partsapi_env(monkeypatch)
    monkeypatch.setenv("PARTSAPI_PARTS_BY_VIN_KEY", "test-secret")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.test/api")

    status = catalog_provider_status(stage="catalog_cross")
    partsapi = next(provider for provider in status["providers"] if provider["source_id"] == "partsapi_ru")

    assert partsapi["configured"] is True
    assert partsapi["live_callable_now"] is True
    assert partsapi["present_env_names"] == ["PARTSAPI_BASE_URL", "PARTSAPI_PARTS_BY_VIN_KEY"]


def test_catalog_provider_status_detects_configured_17vin_account(monkeypatch):
    monkeypatch.setenv("VIN17_ACCOUNT", "test-user")
    monkeypatch.setenv("VIN17_SECRET", "test-secret")

    status = catalog_provider_status(stage="oem_catalog")
    vin17 = next(provider for provider in status["providers"] if provider["source_id"] == "vin17_api")

    assert vin17["configured"] is True
    assert vin17["live_callable_now"] is True
    assert vin17["present_env_names"] == ["VIN17_ACCOUNT", "VIN17_SECRET"]


def test_catalog_provider_status_detects_emex_account(monkeypatch):
    monkeypatch.setenv("EMEX_LOGIN", "test-user")
    monkeypatch.setenv("EMEX_PASSWORD", "test-secret")

    status = catalog_provider_status(stage="procurement_price")
    emex = next(provider for provider in status["providers"] if provider["source_id"] == "emex")

    assert emex["configured"] is True
    assert emex["live_callable_now"] is True
    assert emex["present_env_names"] == ["EMEX_LOGIN", "EMEX_PASSWORD"]
    assert "whitelist" in emex["limits"]


def test_catalog_provider_status_accepts_rossko_app_key_aliases(monkeypatch):
    monkeypatch.delenv("ROSSKO_KEY1", raising=False)
    monkeypatch.delenv("ROSSKO_KEY2", raising=False)
    monkeypatch.setenv("ROSSKO_API_KEY1", "test-key-1")
    monkeypatch.setenv("ROSSKO_API_KEY2", "test-key-2")

    status = catalog_provider_status(stage="procurement_price")
    rossko = next(provider for provider in status["providers"] if provider["source_id"] == "rossko")

    assert rossko["configured"] is True
    assert rossko["live_callable_now"] is True
    assert rossko["present_env_names"] == ["ROSSKO_API_KEY1", "ROSSKO_API_KEY2"]


def test_catalog_provider_status_marks_exist_public_route_live(monkeypatch):
    monkeypatch.delenv("EXIST_LOGIN", raising=False)
    monkeypatch.delenv("EXIST_PASSWORD", raising=False)

    status = catalog_provider_status(stage="procurement_price")
    exist = next(provider for provider in status["providers"] if provider["source_id"] == "exist")

    assert exist["configured"] is True
    assert exist["live_callable_now"] is True
    assert exist["access_mode"] == "public_site_read_only"
    assert exist["env_names"] == []
    assert exist["missing_env_names"] == []
    assert "retail_price_benchmark" in exist["capabilities"]


def test_oem_parts_provider_plan_redacts_identifier_and_reports_blockers(monkeypatch):
    _clear_partsapi_env(monkeypatch)
    for name in ["PARTS_CATALOGS_API_KEY", "PARTS_CATALOGS_BASE_URL", "ROSSKO_KEY1", "ROSSKO_KEY2"]:
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
    oem_blocker = next(blocker for blocker in plan["blockers"] if blocker["stage"] == "oem_catalog")
    assert oem_blocker["missing_env_names"] == oem_blocker["missing_env"]
    assert "PARTSAPI_KEY" in oem_blocker["missing_env_names"]
    assert any(step["step"] == "find_oem_candidates" for step in plan["pipeline"])
    assert any(step["step"] == "lookup_public_aftermarket_catalogs" for step in plan["pipeline"])
    assert plan["manual_public_search_queries"]
    combined_queries = "\n".join(item["query"] + "\n" + item["url"] for item in plan["manual_public_search_queries"])
    assert "MR41S123456" not in combined_queries
    assert "Suzuki" in combined_queries
