from __future__ import annotations

import json
from pathlib import Path

from autostop_manager.source_catalog import recommend_automotive_sources


ROOT = Path(__file__).resolve().parents[1]


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


def test_open_only_filters_license_dependent_sources():
    result = recommend_automotive_sources(data_type="repair_manuals", include_licensed=False, limit=20)

    rendered_statuses = {source["legal_ingestion_status"] for source in result["sources"]}
    assert "licensed_or_link_only" not in rendered_statuses
    assert all(source["requires_license"] is False for source in result["sources"])
    assert any("open-only source route" in warning for warning in result["warnings"])


def test_source_route_does_not_match_short_substrings():
    result = recommend_automotive_sources(brand="BM", data_type="air", limit=5)

    assert result["matched_brand_key"] is None
    assert result["matched_data_type_key"] is None
    assert result["sources"] == []
    assert any("No exact brand route" in warning for warning in result["warnings"])
    assert any("No exact data-type route" in warning for warning in result["warnings"])


def test_safety_critical_data_type_adds_warning():
    result = recommend_automotive_sources(brand="Honda", data_type="wiring_diagrams", limit=5)

    assert any("Safety-critical" in warning for warning in result["warnings"])


def test_nhtsa_tsbs_routes_use_current_received_zip_sources():
    checked_paths = [
        ROOT / "docs" / "agent" / "automotive_sources" / "open_dataset_endpoints.json",
        ROOT / "docs" / "agent" / "automotive_sources" / "automotive_repair_sources_catalog.json",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in checked_paths)

    assert "FLAT_TSBS" not in combined
    assert "nhtsa_tsbs_flat" not in combined
    assert "http://www-odi.nhtsa.dot.gov/downloads/folders/TSBS" not in combined
    assert "https://static.nhtsa.gov/odi/ffdd/tsbs/TSBS_RECEIVED_2025-2026.zip" in combined
    assert "https://www.nhtsa.gov/nhtsa-datasets-and-apis" in combined

    for path in checked_paths:
        json.loads(path.read_text(encoding="utf-8"))


def test_source_maps_are_derived_from_the_canonical_catalog():
    from autostop_manager.source_catalog import load_brand_source_map, load_data_type_source_map

    catalog_path = ROOT / "docs" / "agent" / "automotive_sources" / "automotive_repair_sources_catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog_ids = [row["id"] for row in catalog["sources"]]
    by_id = {row["id"]: row for row in catalog["sources"]}
    projected_fields = ["name", "category", "access", "priority_score_1_5", "legal_ingestion_status", "url"]

    assert catalog["source_count"] == len(catalog_ids) == len(set(catalog_ids))
    for actual, dimension in [
        (load_brand_source_map(), "brands"),
        (load_data_type_source_map(), "data_types"),
    ]:
        expected: dict[str, list[dict[str, object]]] = {}
        for source_id in catalog_ids:
            source = by_id[source_id]
            row = {"source_id": source_id, **{field: source[field] for field in projected_fields}}
            for key in source[dimension]:
                expected.setdefault(key, []).append(row)

        assert actual == expected


def test_source_catalog_has_specific_publishers_and_current_verified_routes():
    catalog_path = ROOT / "docs" / "agent" / "automotive_sources" / "automotive_repair_sources_catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in catalog["sources"]}

    assert all(row["publisher"] != "Public source route" for row in catalog["sources"])
    assert by_id["hyundai_oem_parts"]["url"].startswith("https://www.hyundai.com/eu/")
    assert by_id["toyota_jp_owner_manuals"]["brands"] == ["Toyota"]
    assert by_id["toyota_jp_owner_manuals"]["regions"] == ["Japan"]
    assert "fluids" in by_id["toyota_jp_owner_manuals"]["data_types"]


def test_open_dataset_endpoints_are_normalized_for_source_routing():
    result = recommend_automotive_sources(data_type="vin_decode", include_licensed=False)
    endpoints = result["open_dataset_endpoints"]

    assert endpoints
    assert all(endpoint.get("source_id") for endpoint in endpoints)
    assert all(endpoint.get("url") for endpoint in endpoints)
    assert any(endpoint["source_id"] == "nhtsa_vpic_decodevinvalues" for endpoint in endpoints)
