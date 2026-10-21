"""Draft regressions using only a synthetic source-routing registry."""

from __future__ import annotations

import json

import pytest

from autostop_manager import source_catalog as catalog


BRANDS = [
    pytest.param("Citroen", id="ascii"),
    pytest.param("Citro\u00ebn", id="precomposed"),
    pytest.param("Citroe\u0308n", id="decomposed"),
]
OEM = "citroen_servicebox"
PARTS = "elcats_catalog"
SHARED = "synthetic_shared_open_route"
OTHER = "synthetic_other_oem"
BRAND_IDS = {OEM, PARTS, SHARED}


def source(source_id, brands, data_types, *, category, priority, licensed=True):
    return {
        "id": source_id,
        "name": f"Synthetic {source_id}",
        "brands": brands,
        "data_types": data_types,
        "category": category,
        "priority_score_1_5": priority,
        "access": "paid_subscription" if licensed else "open",
        "legal_ingestion_status": "licensed_subscription_required" if licensed else "public_metadata_or_link_only",
        "url": f"https://{source_id}.invalid/",
    }


@pytest.fixture
def registry(tmp_path, monkeypatch):
    rows = [
        source(OEM, ["Citro\u00ebn"], ["repair_manuals", "parts_catalog"], category="oem_service_portal", priority=4),
        source(PARTS, ["Citroen"], ["parts_catalog"], category="oem_catalog", priority=3),
        source(OTHER, ["Other Make"], ["repair_manuals"], category="oem_service_portal", priority=5),
        source(
            SHARED,
            ["Citroen", "Citro\u00ebn"],
            ["parts_catalog"],
            category="open_reference",
            priority=2,
            licensed=False,
        ),
    ]
    source_path = tmp_path / "synthetic-source-registry.json"
    endpoint_path = tmp_path / "synthetic-endpoints.json"
    endpoint_path.write_text('{"endpoints": []}', encoding="utf-8")
    monkeypatch.setattr(catalog, "SOURCE_CATALOG_PATH", source_path)
    monkeypatch.setattr(catalog, "OPEN_DATASET_ENDPOINTS_PATH", endpoint_path)
    loaders = (
        catalog.load_source_catalog,
        catalog.load_brand_source_map,
        catalog.load_data_type_source_map,
        catalog.load_open_dataset_endpoints,
    )

    def install(*, reverse=False):
        for loader in loaders:
            loader.cache_clear()
        ordered = list(reversed(rows)) if reverse else rows
        source_path.write_text(json.dumps({"source_count": len(ordered), "sources": ordered}), encoding="utf-8")

    try:
        install()
        yield install
    finally:
        for loader in loaders:
            loader.cache_clear()


@pytest.mark.parametrize("brand", BRANDS)
def test_equivalent_brand_names_include_all_matching_groups(registry, brand):
    result = catalog.recommend_automotive_sources(brand=brand, limit=10)

    assert result["ok"] is True
    assert result["matched_brand_key"] in {"Citroen", "Citro\u00ebn"}
    assert result["warnings"] == []
    by_id = {row["source_id"]: row for row in result["sources"]}
    assert set(by_id) == BRAND_IDS
    assert len(result["sources"]) == len(BRAND_IDS)
    assert all(row["brand_match"] is True for row in result["sources"])
    assert all(row["data_type_match"] is False for row in result["sources"])
    assert by_id[OEM]["requires_license"] is True
    assert by_id[PARTS]["requires_license"] is True
    assert by_id[SHARED]["requires_license"] is False


@pytest.mark.parametrize("brand", BRANDS)
def test_equivalent_brand_names_prioritize_own_oem_at_limit_one(registry, brand):
    result = catalog.recommend_automotive_sources(brand=brand, data_type="repair_manuals", limit=1)

    assert result["matched_data_type_key"] == "repair_manuals"
    assert len(result["sources"]) == 1
    chosen = result["sources"][0]
    assert chosen["source_id"] == OEM
    assert chosen["brand_match"] is True
    assert chosen["data_type_match"] is True
    assert chosen["requires_license"] is True
    assert chosen["citation"] == {
        "source_id": OEM,
        "source_url": f"https://{OEM}.invalid/",
        "document_type": "oem_service_portal",
        "license_status": "licensed_subscription_required",
    }


def test_equivalent_group_order_preserves_routes_and_deduplicates_source_id(registry):
    expected = [OEM, PARTS, SHARED]
    initial = catalog.recommend_automotive_sources(brand="Citroen", limit=10)
    registry(reverse=True)
    reversed_groups = catalog.recommend_automotive_sources(brand="Citroen", limit=10)

    for result in (initial, reversed_groups):
        ids = [row["source_id"] for row in result["sources"]]
        assert ids == expected
        assert ids.count(SHARED) == 1
        assert all(row["brand_match"] is True for row in result["sources"])


def test_license_filter_applies_after_equivalent_groups_are_combined(registry):
    result = catalog.recommend_automotive_sources(brand="Citroen", include_licensed=False, limit=10)

    assert [row["source_id"] for row in result["sources"]] == [SHARED]
    assert result["sources"][0]["brand_match"] is True
    assert result["sources"][0]["requires_license"] is False
    assert result["sources"][0]["citation"]["license_status"] == "public_metadata_or_link_only"


@pytest.mark.parametrize(
    ("brand", "expected_key"),
    [
        pytest.param("CITROEN", "Citroen", id="ascii-case-insensitive"),
        pytest.param("CITRO\u00cbN", "Citro\u00ebn", id="precomposed-case-insensitive"),
        pytest.param("Citroe\u0308n", "Citro\u00ebn", id="decomposed-nfc-equivalent"),
    ],
)
def test_brand_label_preserves_existing_exact_spelling_key(registry, brand, expected_key):
    result = catalog.recommend_automotive_sources(brand=brand, limit=10)

    assert result["matched_brand_key"] == expected_key


def test_fuzzy_brand_choice_collects_all_equivalent_groups(registry):
    result = catalog.recommend_automotive_sources(brand="Citro", limit=10)

    assert result["matched_brand_key"] == "Citro\u00ebn"
    assert {row["source_id"] for row in result["sources"]} == BRAND_IDS
    assert len(result["sources"]) == len(BRAND_IDS)
    assert all(row["brand_match"] is True for row in result["sources"])


@pytest.fixture
def interleaved_equal_score_registry(registry):
    # Reuse the existing fixture's private paths and cache cleanup. The IDs
    # deliberately differ from lexical order, and every route has equal score.
    ids = ["synthetic_tie_z", "synthetic_tie_b", "synthetic_tie_y", "synthetic_tie_a"]
    brands = ["Citro\u00ebn", "Citroen", "Citro\u00ebn", "Citroen"]
    rows = [
        source(source_id, [brand], ["repair_manuals"], category="open_reference", priority=2, licensed=False)
        for source_id, brand in zip(ids, brands, strict=True)
    ]
    catalog.SOURCE_CATALOG_PATH.write_text(json.dumps({"source_count": len(rows), "sources": rows}), encoding="utf-8")
    return ids


def test_equivalent_brand_groups_preserve_interleaved_registry_order_at_equal_score(interleaved_equal_score_registry):
    result = catalog.recommend_automotive_sources(brand="Citroen", limit=10)

    assert [row["source_id"] for row in result["sources"]] == interleaved_equal_score_registry
    assert all(row["brand_match"] is True for row in result["sources"])
