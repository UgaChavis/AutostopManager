from __future__ import annotations

import autostop_manager.context as context
from autostop_manager.context import prepare_manager_context
from autostop_manager.knowledge_base import sync_knowledge_base
from autostop_manager.storage import ManagerMemoryStore


def _store(tmp_path) -> ManagerMemoryStore:
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)
    return store


def test_prepare_context_leaves_missing_fact_judgment_to_the_agent(tmp_path):
    result = prepare_manager_context(_store(tmp_path), "BMW F15 N63 BDC fault", limit=5)

    assert result["command_routes"] == []
    assert result["knowledge"]["best_domain"] == "service_case"
    assert "missing_context" not in result


def test_agent_brief_offers_composable_suggestions_without_permissions(tmp_path):
    result = context.build_agent_brief(
        _store(tmp_path),
        "Найди компанию Horizon в CRM, выставь счёт и отправь на эту почту",
    )

    route = result["route"]
    assert {step["command_id"] for step in route["steps"]} == {
        "crm_operations",
        "documents_and_mail",
    }
    assert route["selection_mode"] == "recommended"
    assert all(step["effects"] == [] for step in route["steps"])
    assert route["write_domains"] == []
    assert route["external_connectors"] == ["gmail"]
    assert result["verification"] == ["Confirm the result solves the request."]


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


def test_agent_brief_selects_relevant_sources_for_multi_domain_suggestion(tmp_path):
    result = context.build_agent_brief(_store(tmp_path), "Подготовь счёт и отправь его по Gmail")

    route = result["route"]
    assert [step["command_id"] for step in route["steps"]] == ["documents_and_mail"]
    assert {
        "docs/agent/business_document_quality_playbook.md",
        "docs/agent/gmail_workflow_playbook.md",
    } <= set(route["source_of_truth"] + route["reference_files"])


def test_agent_brief_does_not_load_unrelated_sibling_domain(tmp_path):
    document = context.build_agent_brief(_store(tmp_path), "Создай документ")
    remote = context.build_agent_brief(_store(tmp_path), "Проверь managed-pc")

    assert document["route"]["steps"][0]["knowledge_domains"] == ["business_documents"]
    assert document["route"]["external_connectors"] == []
    assert remote["route"]["steps"][0]["knowledge_domains"] == ["remote_codex_access"]
    assert "docs/agent/deployment_runbook.md" not in remote["route"]["source_of_truth"]


def test_agent_brief_preserves_exact_sources_for_legacy_intents(tmp_path):
    store = _store(tmp_path)
    gmail = context.build_agent_brief(store, "unrelated words", intent="crm_gmail_workflow")
    cleanup = context.build_agent_brief(store, "unrelated words", intent="board_cleanup")
    inbox = context.build_agent_brief(store, "unrelated words", intent="inbox_triage")
    remote = context.build_agent_brief(store, "unrelated words", intent="remote_codex_access_change")

    assert gmail["route"]["steps"][0]["knowledge_domains"] == ["gmail_operations"]
    assert gmail["route"]["external_connectors"] == ["gmail"]
    assert cleanup["route"]["steps"][0]["knowledge_domains"] == ["board_cleanup_autopilot"]
    assert cleanup["route"]["steps"][0]["open_first"] == "docs/agent/board_cleanup_autopilot_playbook.md"
    assert inbox["route"]["steps"][0]["knowledge_domains"] == ["board_cleanup_autopilot"]
    assert remote["route"]["steps"][0]["knowledge_domains"] == ["remote_codex_access", "deployment"]


def test_agent_brief_keeps_store_work_as_guidance_only(tmp_path):
    store = _store(tmp_path)
    intake = context.build_agent_brief(store, "Обработай новую заявку магазина")
    publish = context.build_agent_brief(store, "Опубликуй предложение по заявке магазина")

    assert intake["route"]["steps"][0]["workflow_id"] == "store_management_workflow"
    assert publish["route"]["steps"][0]["effects"] == []
    assert publish["route"]["write_domains"] == []


def test_agent_brief_without_command_signal_remains_exploratory(tmp_path):
    result = context.build_agent_brief(_store(tmp_path), "Как выставить ГРМ на Mercedes M274?", limit=8)

    route = result["route"]
    assert route["selection_mode"] == "explore"
    assert route["steps"] == []
    assert route["candidates"]
