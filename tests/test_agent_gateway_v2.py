from __future__ import annotations

from autostop_manager.agent_gateway import build_agent_bootstrap, list_agent_workflows
from autostop_manager.knowledge_base import find_command_route
from autostop_manager.storage import ManagerMemoryStore


def test_named_registry_resolves_integration_finance_documents_and_crm_gmail():
    cases = {
        "проанализируй связь Codex с Автостоп CRM, MCP команды, тестовый профиль и перегруженные ответы": "crm_agent_integration_audit",
        "проведи оплату в CRM": "crm_finance_operation",
        "проведи оплату по заказ-наряду": "crm_finance_operation",
        "создай документ в CRM": "business_document_workflow",
        "обработай письмо и обнови CRM": "crm_gmail_workflow",
        "сколько посетителей сегодня": "store_analytics_reporting",
        "какие товары смотрели за неделю": "store_analytics_reporting",
        "куда чаще нажимают": "store_analytics_reporting",
        "сколько времени проводят на сайте": "store_analytics_reporting",
        "какая конверсия в корзину и заказ": "store_analytics_reporting",
        "покажи аналитику сайта за неделю": "store_analytics_reporting",
        "сколько у сайта было посетителей сегодня?": "store_analytics_reporting",
    }

    for query, workflow_id in cases.items():
        route = find_command_route(query)
        assert route is not None
        assert route["workflow_id"] == workflow_id

    store_orders = find_command_route("покажи заказы на сайте")
    assert store_orders is None or store_orders["workflow_id"] != "store_analytics_reporting"


def test_explicit_intent_wins_deterministically_over_query_keywords():
    route = find_command_route(
        "письмо про оплату и документ",
        intent="crm_finance_operation",
    )

    assert route is not None
    assert route["workflow_id"] == "crm_finance_operation"
    assert route["score"] == 1000


def test_agent_bootstrap_is_compact_and_exposes_unfinished_resume_point(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    started = store.start_workflow_run(
        workflow_id="crm_finance_operation",
        intent="crm_finance_operation",
        query="проведи оплату в CRM",
        idempotency_key="bootstrap-finance-v1",
        scope={"repair_order_id": "ro-7"},
    )
    store.transition_workflow_run(started["id"], status="executing")
    store.checkpoint_workflow_run(
        started["id"],
        checkpoint={"phase": "preflight", "next_action": "reread repair order"},
    )

    result = build_agent_bootstrap(
        store,
        query="проведи оплату в CRM",
        intent="crm_finance_operation",
    )

    assert result["ok"] is True
    assert result["format"] == "agent_envelope_v2"
    assert result["summary"]["selected_workflow"]["workflow_id"] == "crm_finance_operation"
    assert result["summary"]["unfinished_runs"][0]["run_id"] == started["id"]
    assert result["summary"]["unfinished_runs"][0]["checkpoint"]["phase"] == "preflight"
    assert result["summary"]["policy"]["owner_confirmation_state"] is False


def test_workflow_registry_response_stays_named_and_compact():
    result = list_agent_workflows(query="свяжи Gmail и CRM")

    assert result["ok"] is True
    assert result["format"] == "agent_envelope_v2"
    assert result["summary"]["selected_workflow_id"] == "crm_gmail_workflow"
    assert result["summary"]["workflow_count"] >= 12
    assert len(str(result).encode("utf-8")) < 20_000
