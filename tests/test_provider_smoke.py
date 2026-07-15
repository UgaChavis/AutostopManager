from __future__ import annotations

from autostop_manager.catalog_adapters import catalog_provider_status
from autostop_manager.provider_smoke import build_provider_smoke_report


def test_catalog_status_includes_stage_matrix():
    status = catalog_provider_status()

    assert status["ok"] is True
    stages = {row["stage"] for row in status["stage_matrix"]}
    assert {
        "identity",
        "oem_catalog",
        "catalog_cross",
        "aftermarket_catalog",
        "procurement_price",
        "market_price",
    }.issubset(stages)
    identity = next(row for row in status["stage_matrix"] if row["stage"] == "identity")
    assert identity["configured_count"] >= 1


def test_provider_smoke_dry_run_all_is_safe():
    report = build_provider_smoke_report(provider="all", mode="dry-run")

    assert report["ok"] is True
    assert report["schema"] == "ProviderSmokeResult"
    assert report["summary"]["provider_count"] >= 1
    assert report["summary"]["no_order_guarantee"] is True
    assert report["summary"]["redaction_check"] is True
    assert all(result["live_readonly_status"] == "not_requested" for result in report["results"])
    assert all("present_env_values" not in result for result in report["results"])
    assert all(isinstance(name, str) for result in report["results"] for name in result["missing_env_names"])


def test_provider_smoke_live_readonly_missing_env_is_skipped(monkeypatch):
    monkeypatch.delenv("ROSSKO_KEY1", raising=False)
    monkeypatch.delenv("ROSSKO_KEY2", raising=False)

    report = build_provider_smoke_report(provider="rossko", mode="live-readonly")
    result = report["results"][0]

    assert result["provider"] == "rossko"
    assert result["configured"] is False
    assert result["live_readonly_status"] == "missing_env"
    assert result["no_order_guarantee"] is True


def test_provider_smoke_live_readonly_allows_local_rules():
    report = build_provider_smoke_report(provider="local_platform_rules", mode="live-readonly")
    result = report["results"][0]

    assert result["provider"] == "local_platform_rules"
    assert result["configured"] is True
    assert result["live_readonly_status"] == "ready"
    assert report["summary"]["blocked_count"] == 0


def test_provider_smoke_rejects_unknown_provider():
    report = build_provider_smoke_report(provider="missing_provider", mode="dry-run")

    assert report["ok"] is False
    assert report["error"] == "unknown provider"
