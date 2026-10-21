"""Private synthetic regressions for safety warnings after type resolution."""

from __future__ import annotations


import pytest

from autostop_manager import source_catalog

SAFETY_WARNING = "Safety-critical route: use OEM or licensed professional sources only."


@pytest.fixture
def synthetic_registry(monkeypatch):
    source = {
        "id": "synthetic_oem",
        "name": "Synthetic OEM service source",
        "brands": ["SYNTHETIC"],
        "data_types": ["wiring_diagrams", "fluids"],
        "category": "oem_service_portal",
        "priority_score_1_5": 5,
        "access": "paid_subscription",
        "legal_ingestion_status": "licensed_subscription_required",
        "url": "https://synthetic.invalid/",
    }
    projected = {
        "source_id": source["id"],
        **{
            key: source[key]
            for key in ("name", "category", "priority_score_1_5", "access", "legal_ingestion_status", "url")
        },
    }
    monkeypatch.setattr(source_catalog, "load_source_catalog", lambda: {"sources": [source], "source_count": 1})
    monkeypatch.setattr(source_catalog, "load_brand_source_map", lambda: {"SYNTHETIC": [projected]})
    monkeypatch.setattr(
        source_catalog, "load_data_type_source_map", lambda: {"wiring_diagrams": [projected], "fluids": [projected]}
    )
    monkeypatch.setattr(source_catalog, "load_open_dataset_endpoints", lambda: {"endpoints": []})
    return source_catalog.recommend_automotive_sources(brand="SYNTHETIC", data_type="wiring_diagrams", limit=1)


@pytest.mark.parametrize(
    "data_type, expected_key, expected_safety",
    [
        pytest.param("wiring_diagrams", "wiring_diagrams", True, id="exact"),
        pytest.param("WIRING DIAGRAMS", "wiring_diagrams", True, id="case-and-spacing"),
        pytest.param("wiring diagram", "wiring_diagrams", True, id="resolved-singular"),
        pytest.param("wiring", "wiring_diagrams", True, id="resolved-short-type"),
        pytest.param("srs", None, True, id="raw-only-critical-without-match"),
        pytest.param("fluids", "fluids", False, id="harmless-known-type"),
        pytest.param("totally_unknown", None, False, id="unknown-type"),
    ],
)
def test_resolved_type_retains_the_existing_safety_warning(
    synthetic_registry, data_type, expected_key, expected_safety
):
    result = source_catalog.recommend_automotive_sources(brand="SYNTHETIC", data_type=data_type, limit=1)

    assert result["ok"] is True
    assert result["matched_data_type_key"] == expected_key
    assert result["sources"][0]["data_type_match"] is (expected_key is not None)
    if expected_key == "wiring_diagrams":
        assert result["sources"] == synthetic_registry["sources"]
    has_unknown_warning = any("No exact data-type route" in item for item in result["warnings"])
    assert has_unknown_warning is (expected_key is None)
    critical_warnings = [item for item in result["warnings"] if item.startswith("Safety-critical route:")]
    assert critical_warnings == ([SAFETY_WARNING] if expected_safety else [])
