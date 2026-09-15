from __future__ import annotations

import json
import importlib

import pytest


CASES = [
    (
        "autostop_manager.source_catalog",
        "load_source_catalog",
        "SOURCE_CATALOG_PATH",
        {"sources": [], "source_count": 0},
    ),
    (
        "autostop_manager.source_catalog",
        "load_open_dataset_endpoints",
        "OPEN_DATASET_ENDPOINTS_PATH",
        {"endpoints": []},
    ),
    (
        "autostop_manager.vin_sources",
        "load_source_registry",
        "REGISTRY_PATH",
        {"version": 0, "purpose": "missing", "sources": []},
    ),
]


def test_source_maps_fail_closed_with_invalid_canonical_catalog(tmp_path, monkeypatch):
    module = importlib.import_module("autostop_manager.source_catalog")
    bad_path = tmp_path / "broken.json"
    bad_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(module, "SOURCE_CATALOG_PATH", bad_path)
    for loader in (module.load_source_catalog, module.load_brand_source_map, module.load_data_type_source_map):
        loader.cache_clear()

    assert module.load_brand_source_map() == {}
    assert module.load_data_type_source_map() == {}

    for loader in (module.load_source_catalog, module.load_brand_source_map, module.load_data_type_source_map):
        loader.cache_clear()


@pytest.mark.parametrize("module_name, loader_name, path_attr, expected", CASES)
def test_json_loaders_handle_invalid_top_level_payload(
    tmp_path, monkeypatch, module_name, loader_name, path_attr, expected
):
    module = importlib.import_module(module_name)
    loader = getattr(module, loader_name)
    if hasattr(loader, "cache_clear"):
        loader.cache_clear()

    bad_path = tmp_path / "broken.json"
    bad_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(module, path_attr, bad_path)

    if hasattr(loader, "cache_clear"):
        loader.cache_clear()

    assert loader() == expected

    if hasattr(loader, "cache_clear"):
        loader.cache_clear()


def test_vin_sources_inputs_are_normalized(tmp_path, monkeypatch):
    vin_module = importlib.import_module("autostop_manager.vin_sources")

    registry_path = tmp_path / "vin_oem_sources.json"
    registry_path.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "name": "Demo VIN Source",
                        "inputs": "vin",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(vin_module, "REGISTRY_PATH", registry_path)
    vin_module.load_source_registry.cache_clear()

    assert len(vin_module.sources_for_inputs("vin")) == 1
    assert vin_module.sources_for_inputs("vin")[0]["name"] == "Demo VIN Source"

    vin_module.load_source_registry.cache_clear()
