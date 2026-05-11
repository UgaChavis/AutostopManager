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
