from __future__ import annotations

import json

from autostop_manager.vin_parts_work_order import build_vin_parts_work_order


def test_vin_parts_work_order_builds_actionable_routes_without_raw_identifier(monkeypatch):
    for name in [
        "PARTSAPI_KEY",
        "PARTSAPI_BASE_URL",
        "PARTS_CATALOGS_API_KEY",
        "PARTS_CATALOGS_BASE_URL",
        "VIN17_ACCOUNT",
        "VIN17_SECRET",
        "ROSSKO_KEY1",
        "ROSSKO_KEY2",
    ]:
        monkeypatch.delenv(name, raising=False)

    result = build_vin_parts_work_order(
        [
            {
                "identifier": "X4XJD19410WE00000",
                "make": "BMW",
                "model": "530 D XDRIVE",
                "model_year": 2019,
                "requested_part": "передние колодки",
            },
            {
                "identifier": "ES19999999",
                "make": "Honda",
                "model": "Civic",
                "requested_part": "фильтр АКПП",
            },
        ],
        requested_part="передние колодки",
        live_vpic=False,
    )

    assert result["ok"] is True
    assert result["work_order_summary"]["count"] == 2
    assert result["benchmark_summary"]["part_intent_recognized_count"] == 2
    assert result["items"][0]["status"] == "ready_for_manual_epc_and_market_search_but_live_credentials_missing"
    assert any(route["name"].startswith("BMW ETK") for route in result["items"][0]["oem_lookup_routes"]["brand_or_market_manual"])
    assert any(route["source_id"] == "honda_brand_epc_manual" for route in result["items"][1]["oem_lookup_routes"]["brand_or_market_manual"])
    assert "PARTSAPI_KEY" in result["items"][0]["oem_lookup_routes"]["missing_live_env_names"]
    assert "ROSSKO_KEY1" in result["items"][0]["procurement_lookup_routes"]["missing_live_env_names"]
    assert result["items"][0]["crm_writeback_gate"]["can_write_final_material_line_now"] is False
    assert result["items"][0]["search_terms"]

    rendered = json.dumps(result, ensure_ascii=False)
    assert "X4XJD19410WE00000" not in rendered
    assert "ES19999999" not in rendered


def test_vin_parts_work_order_marks_part_clarification_gap():
    result = build_vin_parts_work_order(
        [
            {
                "identifier": "WVWZZZAUZFP000000",
                "make": "Volkswagen",
                "model": "Golf",
                "model_year": 2014,
                "requested_part": "редкая штука",
            }
        ],
        requested_part="передние колодки",
        live_vpic=False,
    )

    assert result["work_order_summary"]["needs_part_intent_clarification_count"] == 1
    assert result["items"][0]["status"] == "needs_part_intent_clarification_before_catalog_search"
    assert result["items"][0]["requested_part"]["intent_id"] == "unknown"
