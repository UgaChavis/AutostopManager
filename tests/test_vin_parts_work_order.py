from __future__ import annotations

import json

from autostop_manager import config as manager_config
from autostop_manager.catalog_clients import PARTSAPI_METHOD_KEY_ENV_NAMES
from autostop_manager.vin_parts_work_order import build_vin_parts_work_order


PARTSAPI_ENV_NAMES = sorted(
    {
        "PARTSAPI_BASE_URL",
        "PARTSAPI_KEY",
        *PARTSAPI_METHOD_KEY_ENV_NAMES.values(),
    }
)


def _clear_partsapi_env(monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/tmp/autostop-manager-test-empty.env")
    monkeypatch.setattr(manager_config, "_ENV_LOADED", False)
    for name in PARTSAPI_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_vin_parts_work_order_builds_actionable_routes_without_raw_identifier(monkeypatch):
    _clear_partsapi_env(monkeypatch)
    for name in [
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
    assert any(
        route["name"].startswith("BMW ETK")
        for route in result["items"][0]["oem_lookup_routes"]["brand_or_market_manual"]
    )
    assert any(
        route["source_id"] == "honda_brand_epc_manual"
        for route in result["items"][1]["oem_lookup_routes"]["brand_or_market_manual"]
    )
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


def test_vin_parts_work_order_marks_position_clarification_for_generic_brake_pads():
    result = build_vin_parts_work_order(
        [
            {
                "identifier": "1C4RJFCT9CC000000",
                "make": "Jeep",
                "model": "Grand Cherokee",
                "model_year": 2012,
                "engine": "5.7 V8",
                "requested_part": "тормозные колодки",
            }
        ],
        requested_part="тормозные колодки",
        live_vpic=False,
    )

    assert result["work_order_summary"]["needs_part_position_clarification_count"] == 1
    assert result["items"][0]["status"] == "needs_part_position_clarification_before_catalog_search"
    assert result["items"][0]["requested_part"]["intent_id"] == "brake_pads_unspecified_axle"
    assert result["items"][0]["requested_part"]["clarification_fields"] == ["axle"]


def test_vin_parts_work_order_marks_read_only_candidate_lookup_needs_manual_confirmation(monkeypatch):
    monkeypatch.setattr(
        "autostop_manager.vin_parts_work_order.benchmark_vin_parts_lookup",
        lambda *args, **kwargs: {
            "summary": {"count": 1, "full_auto_lookup_count": 0},
            "privacy": {"raw_identifier_redacted_from_output": True},
            "items": [
                {
                    "index": 1,
                    "identifier": {"redacted": {"display": "1HG***352"}, "kind": "vin"},
                    "identity": {
                        "ready_for_oem_lookup": True,
                        "ready_for_oem_candidate_lookup": True,
                        "ready_for_crm_writeback": False,
                        "vehicle_profile": {"make": "HONDA", "model": "Accord"},
                    },
                    "requested_part": {
                        "recognized": True,
                        "intent_id": "front_brake_pads",
                        "catalog_search_terms": ["передние колодки"],
                        "clarification_required": False,
                    },
                    "live_capability": {"can_complete_full_auto_lookup_now": False},
                    "blockers": [],
                    "prepared_calls": {},
                    "manual_public_search": {"queries": []},
                }
            ],
        },
    )

    result = build_vin_parts_work_order([], requested_part="передние колодки")

    assert result["work_order_summary"]["ready_for_oem_candidate_lookup_needs_manual_confirmation_count"] == 1
    assert result["items"][0]["status"] == "ready_for_oem_candidate_lookup_needs_manual_confirmation"
    assert result["items"][0]["crm_writeback_gate"]["can_run_read_only_oem_candidate_lookup"] is True
    assert result["items"][0]["crm_writeback_gate"]["requires_manual_confirmation_before_writeback"] is True


def test_vin_parts_work_order_uses_oem_resolution_status(monkeypatch):
    monkeypatch.setattr(
        "autostop_manager.vin_parts_work_order.benchmark_vin_parts_lookup",
        lambda *args, **kwargs: {
            "summary": {"count": 1, "full_auto_lookup_count": 0},
            "privacy": {"raw_identifier_redacted_from_output": True},
            "items": [
                {
                    "index": 1,
                    "identifier": {"redacted": {"display": "1HG***352"}, "kind": "vin"},
                    "identity": {"vehicle_profile": {"make": "HONDA"}, "ready_for_oem_candidate_lookup": True},
                    "requested_part": {
                        "recognized": True,
                        "catalog_search_terms": ["передние колодки"],
                        "clarification_required": False,
                    },
                    "live_capability": {},
                    "blockers": [],
                    "prepared_calls": {},
                    "manual_public_search": {"queries": []},
                    "oem_resolution": {
                        "status": "ready_for_live_oem_candidate_lookup",
                        "manual_actions": [{"code": "run_live_get_parts_by_vin", "message": "call"}],
                        "readiness": {"ready_for_oem_candidate_lookup": True},
                        "crm_writeback_gate": {
                            "can_write_final_material_line_now": False,
                            "can_prepare_manual_writeback": False,
                            "requires_manual_confirmation_before_writeback": True,
                        },
                    },
                }
            ],
        },
    )

    result = build_vin_parts_work_order([], requested_part="передние колодки", resolve_oem=True)

    assert result["work_order_summary"]["ready_for_live_oem_candidate_lookup_count"] == 1
    assert result["items"][0]["status"] == "ready_for_live_oem_candidate_lookup"
    assert result["items"][0]["next_manual_actions"][0]["code"] == "run_live_get_parts_by_vin"


def test_vin_parts_work_order_reports_no_items_next_decision():
    result = build_vin_parts_work_order([], requested_part="передние колодки", live_vpic=False, use_vpic_batch=False)

    assert result["work_order_summary"]["count"] == 0
    assert result["benchmark_summary"]["benchmark_status"] == "no_items"
    assert (
        result["next_decision"]
        == "No VIN/frame items supplied; add at least one item before planning OEM and supplier lookup."
    )
