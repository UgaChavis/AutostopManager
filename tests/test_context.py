from __future__ import annotations

import pytest

import autostop_manager.context as context
from autostop_manager.context import prepare_manager_context
from autostop_manager.knowledge_base import sync_knowledge_base
from autostop_manager.storage import ManagerMemoryStore


def _store(tmp_path) -> ManagerMemoryStore:
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
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
    assert result["knowledge"]["best_domain"] == "automotive_repair"
    assert "VIN or chassis" in result["missing_context"]


@pytest.mark.parametrize(
    "query",
    [
        "Проверь свежесть и восстановимость резервных копий AutoStop перед обновлением",
        "Проверь цифровую инфраструктуру AutoStop перед обновлением",
        "Проверь серверную инфраструктуру AutoStop перед обновлением",
    ],
)
def test_prepare_context_opens_deployment_docs_for_release_readiness_audit(tmp_path, query):
    result = prepare_manager_context(
        _store(tmp_path),
        query,
        limit=5,
    )

    assert result["command_routes"] == []
    assert result["knowledge"]["best_domain"] == "deployment"
    assert result["knowledge"]["open_first"] == "docs/agent/deployment_runbook.md"


def test_agent_brief_preserves_first_workflow_scalars_and_full_steps(tmp_path):
    result = context.build_agent_brief(
        _store(tmp_path),
        "Найди компанию Horizon, выставь счёт и отправь на эту почту",
    )

    assert result["format"] == "agent_brief_v1"
    assert result["route"]["workflow_id"] == "crm_record_workflow"
    assert result["route"]["write_domains"] == ["crm"]
    assert result["route"]["external_connectors"] == ["gmail"]
    assert result["route"]["selected_workflows"] == [
        "crm_record_workflow",
        "business_document_workflow",
        "crm_gmail_workflow",
    ]
    assert result["route"]["steps"][2]["effects"] == ["external_send"]
    assert any("monetary or tax mismatch" in item for item in result["forbidden_actions"])


def test_agent_brief_does_not_update_memory_usage(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    created = store.remember("Reusable routing observation")
    with store.connect() as conn:
        before = conn.execute("SELECT last_used_at FROM notes WHERE id = ?", (created["id"],)).fetchone()[
            "last_used_at"
        ]

    context.build_agent_brief(store, "CRM не работает")

    with store.connect() as conn:
        after = conn.execute("SELECT last_used_at FROM notes WHERE id = ?", (created["id"],)).fetchone()["last_used_at"]
    assert before is None and after is None


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


def test_agent_brief_store_management_has_no_blanket_destructive_gate(tmp_path):
    result = context.build_agent_brief(_store(tmp_path), "Назначь заявку на подбор в магазине")

    assert result["route"]["workflow_id"] == "store_management_workflow"
    assert result["route"]["steps"][0]["effects"] == ["store_write"]
    assert result["route"]["write_domains"] == ["store"]
    assert "apply only the task-scoped Store diff" in result["allowed_actions"]
    assert not any("Resolve exact targets and recovery material" in rule for rule in result["hot_rules"])


def test_agent_brief_store_customer_response_publish_uses_external_visibility_policy(tmp_path):
    result = context.build_agent_brief(_store(tmp_path), "Ответь клиенту по заявке магазина")

    step = result["route"]["steps"][0]
    assert step["command_id"] == "store_customer_response_publish"
    assert step["workflow_id"] == "store_quote_conductor"
    assert step["effects"] == ["store_write", "external_send"]
    assert any("exact destination or target" in rule for rule in result["hot_rules"])
    assert any("customer-visible or outbound result" in check for check in result["verification"])
    assert all("monetary basis" not in check for check in result["verification"])


def test_agent_brief_effect_safety_rules_are_not_truncated_by_retrieval_limit(tmp_path):
    result = context.build_agent_brief(
        _store(tmp_path),
        "Ответь клиенту по заявке магазина",
        limit=1,
    )

    for marker in (
        "Store writes require",
        "exact destination or target",
        "External visibility or delivery requires",
    ):
        assert any(marker in rule for rule in result["hot_rules"])


def test_agent_brief_store_quote_draft_uses_conductor_without_finance_effect(tmp_path):
    result = context.build_agent_brief(
        _store(tmp_path),
        "Подготовь ответ клиенту по заявке на проценку",
    )

    step = result["route"]["steps"][0]
    assert step["command_id"] == "store_quote_draft"
    assert step["workflow_id"] == "store_quote_conductor"
    assert step["effects"] == ["store_write"]
    assert result["route"]["write_domains"] == ["store"]
    assert all("publish or send once" not in action for action in result["allowed_actions"])


def test_agent_brief_store_ready_discloses_external_effect(tmp_path):
    result = context.build_agent_brief(_store(tmp_path), "Переведи заказ магазина в ready")

    step = result["route"]["steps"][0]
    assert step["command_id"] == "store_order_ready"
    assert step["effects"] == ["store_write", "external_send", "destructive"]
    assert any("customer-visible or outbound result" in check for check in result["verification"])


@pytest.mark.parametrize("query", ["Заказ магазина в READY?", "Какой заказ магазина в READY?"])
def test_agent_brief_store_ready_question_is_read_only(tmp_path, query):
    result = context.build_agent_brief(_store(tmp_path), query)

    assert result["route"]["workflow_id"] == "store_read_workflow"
    assert result["route"]["write_domains"] == []


def test_agent_brief_store_quote_processing_loads_director_route(tmp_path):
    result = context.build_agent_brief(_store(tmp_path), "Обработай новую заявку магазина")

    assert result["role"] == "AutoStop operations director agent"
    assert result["route"]["command_id"] == "store_customer_response_publish"
    assert result["route"]["workflow_id"] == "store_quote_conductor"
    assert result["route"]["open_first"] == ".agents/skills/manage-autostop-store/SKILL.md"
    assert result["route"]["write_domains"] == ["store"]
    assert result["route"]["external_connectors"] == ["telegram", "store"]
    assert result["source_boundaries"]["telegram"] == "source of truth for raw dialogs, contacts, messages, and media"


def test_agent_brief_store_read_has_no_write_domain(tmp_path):
    result = context.build_agent_brief(_store(tmp_path), "Посмотри новый запрос на проценку")

    assert result["route"]["workflow_id"] == "store_read_workflow"
    assert result["route"]["write_domains"] == []


def test_agent_brief_telegram_read_does_not_authorize_send(tmp_path):
    result = context.build_agent_brief(_store(tmp_path), "Прочитай рабочий Телеграм")

    assert result["route"]["command_id"] == "telegram_read_operations"
    assert result["route"]["steps"][0]["effects"] == []
    assert all("publish or send once" not in action for action in result["allowed_actions"])


def test_agent_brief_telegram_authorization_does_not_authorize_send(tmp_path):
    result = context.build_agent_brief(_store(tmp_path), "Авторизуй рабочий Телеграм")

    assert result["route"]["command_id"] == "telegram_authorization"
    assert result["route"]["steps"][0]["effects"] == ["account_auth"]
    assert any("interactive authorization flow" in action for action in result["allowed_actions"])
    assert all("publish or send once" not in action for action in result["allowed_actions"])


def test_agent_brief_routes_general_site_management_request_without_blanket_write(tmp_path):
    result = context.build_agent_brief(
        _store(tmp_path),
        "Получай полную информацию и управляй нашим сайтом автозапчастей",
    )

    assert result["route"]["workflow_id"] == "store_read_workflow"
    assert result["route"]["domain"] == "store_management"
    assert result["route"]["write_domains"] == []


def test_agent_brief_remote_check_has_no_blanket_destructive_gate(tmp_path):
    result = context.build_agent_brief(_store(tmp_path), "Проверь подключение к удалённому серверу по SSH")

    assert result["route"]["workflow_id"] == "remote_codex_access"
    assert result["route"]["steps"][0]["effects"] == []
    assert not any("Resolve exact targets and recovery material" in rule for rule in result["hot_rules"])


def test_agent_brief_remote_change_keeps_destructive_gate(tmp_path):
    result = context.build_agent_brief(_store(tmp_path), "Перезапусти SSHD на удалённом сервере")

    assert result["route"]["workflow_id"] == "remote_codex_access_change"
    assert result["route"]["steps"][0]["effects"] == ["destructive"]
    assert any("Resolve exact targets and recovery material" in rule for rule in result["hot_rules"])


def test_agent_brief_exposes_optional_navigation_for_routed_vin_writeback(tmp_path):
    result = context.build_agent_brief(
        _store(tmp_path),
        "В карточке CRM по VIN найти OEM фильтра и записать в карточку",
    )

    assert result["route"]["workflow_id"] == "crm_vin_oem_parts_lookup"
    assert result["route"]["domain"] == "crm_vin_oem_parts_lookup"
    assert "reference_files" in result["route"]
    assert result["route"]["steps"][0]["knowledge_domains"][0] == "crm_vin_oem_parts_lookup"
