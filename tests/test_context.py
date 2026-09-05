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


def test_agent_brief_preserves_multi_step_effect_composition(tmp_path):
    result = context.build_agent_brief(
        _store(tmp_path),
        "Найди компанию Horizon, выставь счёт и отправь на эту почту",
    )

    route = result["route"]
    assert route["steps"][0]["workflow_id"] == "crm_record_workflow"
    assert {step["workflow_id"] for step in route["steps"]} == {
        "crm_record_workflow",
        "business_document_workflow",
        "crm_gmail_workflow",
    }
    assert any("external_send" in step["effects"] for step in route["steps"])
    assert "Reconcile the amounts and resulting business state." in result["verification"]


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


def test_agent_brief_business_documents_keeps_document_and_finance_gates(tmp_path):
    result = context.build_agent_brief(_store(tmp_path), "Выставь счёт из CRM")

    route = result["route"]
    assert route["steps"][0]["workflow_id"] == "business_document_workflow"
    assert route["steps"][0]["domain"] == "business_documents"
    assert route["steps"][0]["effects"] == ["document", "finance"]
    assert "Inspect the generated document before use." in result["verification"]
    assert "Reconcile the amounts and resulting business state." in result["verification"]


def test_agent_brief_vin_lookup_stays_in_service_case_until_route_is_confident(tmp_path):
    result = context.build_agent_brief(
        _store(tmp_path),
        "В карточке CRM по VIN найти OEM фильтра и записать в карточку",
    )

    route = result["route"]
    assert route["selection_mode"] == "explore"
    assert route["steps"] == []
    assert route["write_domains"] == []
    assert route["candidates"][0]["domain"] == "service_case"


def test_agent_brief_keeps_store_intake_read_only_and_publish_explicit(tmp_path):
    store = _store(tmp_path)
    intake = context.build_agent_brief(store, "Обработай новую заявку магазина")
    publish = context.build_agent_brief(store, "Опубликуй предложение по заявке магазина", limit=1)

    assert intake["route"]["steps"][0]["workflow_id"] == "store_quote_conductor"
    assert intake["route"]["write_domains"] == []
    assert publish["route"]["steps"][0]["effects"] == ["store_write"]
    assert "Confirm the intended Store record reflects the change." in publish["verification"]


def test_agent_brief_without_command_signal_remains_exploratory(tmp_path):
    result = context.build_agent_brief(_store(tmp_path), "Как выставить ГРМ на Mercedes M274?", limit=8)

    route = result["route"]
    assert route["selection_mode"] == "explore"
    assert route["steps"] == []
    assert route["candidates"]
