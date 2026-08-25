from __future__ import annotations

import autostop_manager.context as context
from autostop_manager.context import prepare_manager_context
from autostop_manager.knowledge_base import sync_knowledge_base
from autostop_manager.storage import ManagerMemoryStore


def _store(tmp_path) -> ManagerMemoryStore:
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.seed_default_rules()
    sync_knowledge_base(store)
    return store


def test_prepare_context_keeps_command_plan_and_document_selection_separate(tmp_path):
    result = prepare_manager_context(_store(tmp_path), "Приберись", intent="board_cleanup", limit=8)

    assert result["ok"] is True
    assert result["command_route"]["workflow_id"] == "board_cleanup_autopilot"
    assert result["selected_workflows"] == ["board_cleanup_autopilot"]
    assert result["knowledge"]["best_domain"] == "board_cleanup_autopilot"
    assert result["knowledge"]["open_first"] == "docs/agent/board_cleanup_autopilot_playbook.md"


def test_prepare_context_flags_vehicle_specific_missing_context(tmp_path):
    result = prepare_manager_context(_store(tmp_path), "BMW F15 N63 BDC fault", limit=5)

    assert result["command_routes"] == []
    assert result["knowledge"]["best_domain"] == "bmw_f15_n63"
    assert "VIN or chassis" in result["missing_context"]


def test_agent_brief_preserves_first_workflow_scalars_and_full_steps(tmp_path):
    result = context.build_agent_brief(
        _store(tmp_path),
        "Найди компанию Horizon, выставь счёт и отправь на эту почту",
    )

    assert result["format"] == "agent_brief_v1"
    assert result["route"]["workflow_id"] == "crm_record_workflow"
    assert result["route"]["write_domains"] == ["crm"]
    assert result["route"]["external_connectors"] == []
    assert result["route"]["selected_workflows"] == [
        "crm_record_workflow",
        "business_document_workflow",
        "crm_gmail_workflow",
    ]
    assert result["route"]["steps"][2]["effects"] == ["external_send"]
    assert any("monetary or tax mismatch" in item for item in result["forbidden_actions"])


def test_agent_brief_policy_comes_from_effects_not_knowledge_domain(tmp_path):
    cleanup = context.build_agent_brief(_store(tmp_path), "Приберись")
    read_only = context.build_agent_brief(_store(tmp_path), "Сколько посетителей сегодня")

    assert cleanup["route"]["domain"] == "board_cleanup_autopilot"
    assert any("proof-bound apply" in rule for rule in cleanup["hot_rules"])
    assert cleanup["route"]["write_domains"] == ["crm"]
    assert read_only["route"]["domain"] == "store_analytics_reporting"
    assert read_only["route"]["write_domains"] == []
    assert not any("proof-bound apply" in rule for rule in read_only["hot_rules"])


def test_agent_brief_without_command_signal_is_explore_even_with_documents(tmp_path):
    result = context.build_agent_brief(_store(tmp_path), "Как выставить ГРМ на Mercedes M274?", limit=8)

    assert result["route"]["selection_mode"] == "explore"
    assert result["route"]["workflow_id"] is None
    assert result["route"]["domain"] is None
    assert result["route"]["open_first"] is None
    assert result["route"]["confidence"] < 0.5
    assert result["route"]["candidates"]


def test_agent_brief_document_route_uses_document_and_finance_gates(tmp_path):
    result = context.build_agent_brief(_store(tmp_path), "Выставь счёт из CRM")

    assert result["route"]["workflow_id"] == "business_document_workflow"
    assert result["route"]["domain"] == "business_documents"
    assert result["route"]["open_first"] == "docs/agent/business_document_quality_playbook.md"
    assert any("document_guard" in check for check in result["verification"])
    assert any("monetary or tax mismatch" in item for item in result["forbidden_actions"])


def test_agent_brief_store_route_has_no_embedded_operation_dsl(tmp_path):
    result = context.build_agent_brief(_store(tmp_path), "Покажи состояние склада")

    assert result["route"]["workflow_id"] == "store_read_workflow"
    assert result["route"]["domain"] == "store_management"
    assert result["route"]["read_entity_selection"] == {}
    assert result["route"]["operation_selection"] == {}
    assert result["route"]["selected_operation"] is None


def test_agent_brief_exposes_optional_navigation_for_routed_vin_writeback(tmp_path):
    result = context.build_agent_brief(
        _store(tmp_path),
        "В карточке CRM по VIN найти OEM фильтра и записать в карточку",
    )

    assert result["route"]["workflow_id"] == "crm_vin_oem_parts_lookup"
    assert result["route"]["domain"] == "crm_vin_oem_parts_lookup"
    assert "reference_files" in result["route"]
    assert result["route"]["steps"][0]["knowledge_domains"][0] == "crm_vin_oem_parts_lookup"
