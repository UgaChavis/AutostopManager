from __future__ import annotations

from autostop_manager.crm_health import build_crm_health_plan


def test_crm_health_plan_reports_overloaded_columns_and_qa_noise():
    board_review = {
        "ok": True,
        "data": {
            "by_column": [
                {"column_id": "column_2", "label": "Запись на ремонт", "count": 11},
                {"column_id": "column_3", "label": "Готовые автомобили", "count": 18},
                {"column_id": "column_4", "label": "Диагностика", "count": 3},
            ],
            "recent_events": [
                {
                    "actor_name": "Codex MCP QA",
                    "type": "repair_order_updated",
                    "card_short_id": "C-TEST",
                    "timestamp": "2026-05-11T13:16:01+00:00",
                    "text": "Codex MCP QA обновил заказ-наряд",
                }
            ],
        },
    }
    today_context = {
        "ok": True,
        "tasks": [
            {
                "id": 7,
                "title": "Проверить зависшую задачу",
                "status": "open",
                "due_at": "2026-05-04T10:00:00+07:00",
            }
        ],
    }

    result = build_crm_health_plan(
        board_review=board_review,
        today_context=today_context,
        now="2026-05-11T15:40:00+07:00",
    )

    assert result["ok"] is True
    assert result["mode"] == "read_only"
    assert {item["label"] for item in result["overloaded_columns"]} == {"Запись на ремонт", "Готовые автомобили"}
    assert result["event_noise"][0]["actor_name"] == "Codex MCP QA"
    assert result["stale_tasks"][0]["id"] == 7
    assert result["suggested_actions"]
    assert result["verification"]["cards_moved"] == 0
    assert result["verification"]["cards_archived"] == 0


def test_crm_health_plan_accepts_board_context_column_shape():
    board_context = {
        "columns": [
            {"id": "todo", "name": "Запись на ремонт", "cards": [{"id": "1"} for _ in range(9)]},
            {"id": "done", "name": "Готовые автомобили", "cards": [{"id": "2"} for _ in range(2)]},
        ]
    }

    result = build_crm_health_plan(board_context=board_context, now="2026-05-11T15:40:00+07:00")

    assert [item["column_id"] for item in result["overloaded_columns"]] == ["todo"]
    assert result["verification"]["crm_writes"] == 0


def test_crm_health_plan_accepts_manager_board_scan_sections():
    board_scan = {
        "ok": True,
        "data": {
            "summary": {
                "active_cards": 32,
                "archived_cards": 4,
                "missing_manager_data": 2,
                "ready_unpaid_cards": 1,
                "inbox_cards": 1,
                "repair_order_issues": 1,
            },
            "sections": {
                "overdue": [{"id": "card-overdue", "heading": "Просрочена диагностика"}],
                "critical": [{"id": "card-critical", "heading": "Критичный таймер"}],
                "missing_manager_data": [{"id": "card-missing", "missing": ["board_summary"]}],
                "ready_unpaid": [{"id": "card-ready", "heading": "Готов без оплаты"}],
                "inbox": [{"id": "card-inbox", "heading": "Новая заявка"}],
                "repair_order_consistency": [{"card_id": "card-ready", "code": "ready_without_closed_order"}],
                "overloaded_columns": [
                    {"column": "ready", "column_label": "Готовые автомобили", "active_cards": 13}
                ],
            },
            "meta": {"response_mode": "manager_board_scan", "view_mode": "compact"},
        },
    }

    result = build_crm_health_plan(board_review=board_scan, now="2026-05-11T15:40:00+07:00")

    assert result["manager_summary"]["active_cards"] == 32
    assert result["overloaded_columns"][0]["column_id"] == "ready"
    assert result["overloaded_columns"][0]["label"] == "Готовые автомобили"
    assert result["overloaded_columns"][0]["count"] == 13
    assert result["manager_signals"]["ready_unpaid"][0]["id"] == "card-ready"
    assert result["manager_signals"]["inbox"][0]["id"] == "card-inbox"
    assert result["manager_signals"]["missing_manager_data"][0]["missing"] == ["board_summary"]
    assert result["manager_signals"]["repair_order_consistency"][0]["code"] == "ready_without_closed_order"
    assert {
        action["category"]
        for action in result["suggested_actions"]
        if action["category"] != "overloaded_column"
    } >= {"ready_unpaid", "inbox", "missing_manager_data", "overdue", "critical", "repair_order_consistency"}


def test_crm_health_plan_accepts_focused_manager_diagnostic_payloads():
    ready_unpaid = {
        "ok": True,
        "data": {
            "cards": [{"id": "card-ready", "heading": "Готов без оплаты"}],
            "meta": {"response_mode": "ready_unpaid_cards", "view_mode": "compact"},
        },
    }
    inbox = {
        "ok": True,
        "data": {
            "cards": [{"id": "card-inbox", "triage_bucket": "needs_data"}],
            "meta": {"response_mode": "inbox_triage", "view_mode": "compact"},
        },
    }
    repair_order_consistency = {
        "ok": True,
        "data": {
            "items": [{"card_id": "card-ready", "code": "ready_without_payment"}],
            "meta": {"response_mode": "repair_order_consistency_audit", "view_mode": "compact"},
        },
    }

    result = build_crm_health_plan(
        board_context=ready_unpaid,
        board_review=inbox,
        today_context=repair_order_consistency,
        now="2026-05-11T15:40:00+07:00",
    )

    assert result["manager_signals"]["ready_unpaid"][0]["id"] == "card-ready"
    assert result["manager_signals"]["inbox"][0]["id"] == "card-inbox"
    assert result["manager_signals"]["repair_order_consistency"][0]["code"] == "ready_without_payment"
    assert {action["category"] for action in result["suggested_actions"]} >= {
        "ready_unpaid",
        "inbox",
        "repair_order_consistency",
    }
