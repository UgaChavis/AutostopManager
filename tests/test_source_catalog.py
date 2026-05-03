from __future__ import annotations

from autostop_manager.source_catalog import recommend_automotive_sources


def test_recommend_sources_intersects_brand_and_data_type():
    result = recommend_automotive_sources(brand="Toyota", data_type="repair_manuals", limit=5)

    assert result["ok"] is True
    assert result["matched_brand_key"] == "Toyota"
    assert result["matched_data_type_key"] == "repair_manuals"
    assert result["sources"][0]["source_id"] == "toyota_tis_na"
    assert result["sources"][0]["brand_match"] is True
    assert result["sources"][0]["data_type_match"] is True


def test_open_only_filters_licensed_sources():
    result = recommend_automotive_sources(data_type="recalls", include_licensed=False, limit=20)

    assert result["sources"]
    assert all(source["requires_license"] is False for source in result["sources"])
    assert any(source["source_id"].startswith("nhtsa") for source in result["sources"])


def test_safety_critical_data_type_adds_warning():
    result = recommend_automotive_sources(brand="Honda", data_type="wiring_diagrams", limit=5)

    assert any("Safety-critical" in warning for warning in result["warnings"])
