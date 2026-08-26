from __future__ import annotations

import json

import pytest

import autostop_manager.knowledge_base as knowledge_base
from autostop_manager.context import build_agent_brief
from autostop_manager.knowledge_base import (
    plan_command_routes,
    probe_knowledge_base,
    sync_knowledge_base,
)
from autostop_manager.storage import ManagerMemoryStore


def _workflows(query: str, *, intent: str | None = None) -> list[str]:
    return [route["workflow_id"] for route in plan_command_routes(query, intent=intent)]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Найди компанию Горизонт и покажи её данные", ["crm_record_workflow"]),
        ("В Audi Q7 поставь клиентом компанию Horizon", ["crm_record_workflow"]),
        ("Есть ли у компании Horizon почта?", ["crm_record_workflow"]),
        ("Измени клиента в заказ-наряде", ["crm_record_workflow"]),
        ("Выставь счёт из CRM", ["business_document_workflow"]),
        (
            "Выставь счёт и отправь на эту почту",
            ["business_document_workflow", "crm_gmail_workflow"],
        ),
        (
            "Найди компанию Horizon, назначь её клиентом Audi Q7, проверь email, выставь счёт из CRM и отправь его на эту почту",
            ["crm_record_workflow", "business_document_workflow", "crm_gmail_workflow"],
        ),
        (
            "В карточке CRM по VIN найди OEM фильтра и запиши в карточку",
            ["crm_vin_oem_parts_lookup", "crm_record_workflow"],
        ),
        ("Приберись", ["board_cleanup_autopilot"]),
        ("Устрани техдолг в проекте AutoStopManager", ["ecosystem_capability_parity"]),
        (
            "Сократи код и почисти документацию проекта Автостоп Менеджер",
            ["ecosystem_capability_parity", "manager_documentation_hygiene"],
        ),
        ("Покажи состояние склада", ["store_read_workflow"]),
    ],
)
def test_command_route_regression_table(query, expected):
    assert _workflows(query) == expected


def test_email_lookup_does_not_select_external_send():
    routes = plan_command_routes("Проверь, есть ли у компании Horizon email")

    assert [route["workflow_id"] for route in routes] == ["crm_record_workflow"]
    assert all("external_send" not in route["effects"] for route in routes)


def test_repair_order_client_change_never_becomes_parts_sourcing():
    route = plan_command_routes("Обнови клиента в заказ-наряде")[0]

    assert route is not None
    assert route["workflow_id"] == "crm_record_workflow"
    assert "parts_sourcing" not in route["knowledge_domains"]


def test_company_vehicle_and_address_values_are_not_signals():
    for value in ("Horizon", "Audi Q7", "7701234567", "client@example.com"):
        assert plan_command_routes(value) == []


def test_real_parts_and_oem_requests_remain_distinct():
    generic = _workflows("Найди запчасти по VIN для BMW N63")
    oem = _workflows("Найди оригинальный номер детали по VIN")
    crosses = _workflows("Проверь кроссы и аналоги по номеру детали")
    writeback = _workflows("В карточке CRM по VIN найди OEM фильтра и запиши в карточку")

    assert generic == ["vehicle_identity_decode", "parts_oem_lookup"]
    assert oem == ["vehicle_identity_decode", "parts_oem_lookup"]
    assert crosses == ["parts_oem_lookup"]
    assert writeback[0] == "crm_vin_oem_parts_lookup"


def test_explicit_intent_is_deterministic():
    route = plan_command_routes("письмо про оплату и документ", intent="crm_finance_operation")[0]

    assert route is not None
    assert route["workflow_id"] == "crm_finance_operation"
    assert route["score"] == 1000
    assert _workflows("письмо про оплату и документ", intent="crm_finance_operation") == ["crm_finance_operation"]


def test_explicit_store_opt_out_removes_store_routes():
    query = "Покажи активные заказы магазина, но без Store — магазин не трогать"

    assert plan_command_routes(query) == []
    assert plan_command_routes(query, intent="store_read") == []


def test_project_maintenance_respects_store_opt_out():
    routes = plan_command_routes("Устрани техдолг Автостоп Менеджера без работы со Store")

    assert [route["workflow_id"] for route in routes] == ["ecosystem_capability_parity"]


def test_crm_failure_is_not_treated_as_negative_scope():
    assert _workflows("CRM не работает") == ["crm_agent_integration_audit"]


def test_command_routes_are_read_live_without_sync_or_restart(tmp_path, monkeypatch):
    route_path = tmp_path / "command_routes.json"
    monkeypatch.setattr(knowledge_base, "COMMAND_ROUTES_PATH", route_path)

    def write_route(workflow_id: str) -> None:
        route_path.write_text(
            json.dumps(
                {
                    "routes": [
                        {
                            "workflow_id": workflow_id,
                            "intent": workflow_id,
                            "phase": 10,
                            "priority": 1,
                            "knowledge_domains": ["startup_and_identity"],
                            "effects": [],
                            "signals": {"phrases": ["live route"]},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    write_route("first")
    assert _workflows("live route") == ["first"]
    write_route("second")
    assert _workflows("live route") == ["second"]


def test_candidates_use_relevance_while_steps_use_phase(tmp_path, monkeypatch):
    route_path = tmp_path / "command_routes.json"
    monkeypatch.setattr(knowledge_base, "COMMAND_ROUTES_PATH", route_path)
    route_path.write_text(
        json.dumps(
            {
                "routes": [
                    {
                        "workflow_id": "first_step",
                        "phase": 10,
                        "priority": 1,
                        "knowledge_domains": ["startup_and_identity"],
                        "effects": [],
                        "signals": {"all": [["exact"], ["route"]]},
                    },
                    {
                        "workflow_id": "best_match",
                        "phase": 30,
                        "priority": 1,
                        "knowledge_domains": ["startup_and_identity"],
                        "effects": [],
                        "signals": {"phrases": ["exact route"]},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    brief = build_agent_brief(ManagerMemoryStore(tmp_path / "memory.sqlite3"), "exact route")

    assert brief["route"]["selected_workflows"] == ["first_step", "best_match"]
    assert [item["workflow_id"] for item in brief["route"]["candidates"]] == [
        "best_match",
        "first_step",
    ]


def test_probe_is_document_only_and_never_returns_command_route(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "Приберись", limit=5)

    assert result["has_knowledge"] is True
    assert result["best_domain"] == "board_cleanup_autopilot"
    assert result["command_route"] is None


def test_command_registry_and_knowledge_map_are_independent(tmp_path, monkeypatch):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)
    original_routes = knowledge_base._load_command_routes

    monkeypatch.setattr(knowledge_base, "KNOWLEDGE_MAP_PATH", tmp_path / "missing-map.json")
    assert plan_command_routes("Приберись")[0]["workflow_id"] == "board_cleanup_autopilot"

    monkeypatch.setattr(knowledge_base, "_load_command_routes", lambda: {"routes": []})
    monkeypatch.undo()
    monkeypatch.setattr(knowledge_base, "_load_command_routes", lambda: {"routes": []})
    result = probe_knowledge_base(store, "BMW F15 N63", limit=5)
    assert result["best_domain"] == "bmw_f15_n63"
    monkeypatch.setattr(knowledge_base, "_load_command_routes", original_routes)


def test_agent_brief_without_command_match_is_bounded_exploration(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    brief = build_agent_brief(store, "Как выставить ГРМ на Mercedes M274?", limit=5)

    assert brief["route"]["selection_mode"] == "explore"
    assert brief["route"]["workflow_id"] is None
    assert brief["route"]["steps"] == []
    assert brief["route"]["confidence"] < 0.5
    assert all(candidate["confidence"] < 0.5 for candidate in brief["route"]["candidates"])


def test_full_flow_brief_exposes_ordered_steps_and_effect_policies(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)
    query = "Найди компанию Horizon, выставь счёт и отправь на эту почту"

    brief = build_agent_brief(store, query, limit=8)

    assert brief["route"]["selected_workflows"] == [
        "crm_record_workflow",
        "business_document_workflow",
        "crm_gmail_workflow",
    ]
    assert [step["phase"] for step in brief["route"]["steps"]] == [10, 20, 30]
    assert brief["route"]["write_domains"] == ["crm"]
    assert brief["route"]["external_connectors"] == ["gmail"]
    assert brief["route"]["steps"][2]["effects"] == ["external_send"]
    assert any("monetary or tax mismatch" in item for item in brief["forbidden_actions"])
