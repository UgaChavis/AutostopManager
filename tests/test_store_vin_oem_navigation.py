from __future__ import annotations

from autostop_manager.context import build_agent_brief


def test_store_vin_oem_request_gets_sources_without_a_rigid_second_route():
    brief = build_agent_brief(
        None,
        "Обработай заявку на проценку: есть VIN и фото маркировки, подбери OEM-номер детали.",
    )

    steps = brief["route"]["steps"]
    assert [step["command_id"] for step in steps] == ["store_quote"]
    assert steps[0]["knowledge_domains"] == ["store_management"]
    assert steps[0]["effects"] == []
    assert steps[0]["dependencies"] == []
    assert {
        "docs/agent/vin_oem_sources.json",
        "docs/agent/partsapi_category_index.json",
        "docs/agent/automotive_sources/automotive_repair_sources_catalog.json",
        "docs/agent/automotive_sources/open_dataset_endpoints.json",
    } <= set(brief["route"]["reference_files"])
