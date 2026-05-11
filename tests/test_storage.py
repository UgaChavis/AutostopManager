from __future__ import annotations

import sqlite3

import pytest

from autostop_manager.storage import ManagerMemoryStore


def test_store_connect_context_closes_connection(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    with store.connect() as conn:
        conn.execute("SELECT 1").fetchone()

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_remember_and_recall(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    created = store.remember("Rent is paid before day 5", kind="fact", tags=["rent"], confidence=0.8)
    assert created["ok"] is True
    assert created["confidence"] == 0.8

    result = store.recall("Rent")
    assert result["ok"] is True
    assert result["items"][0]["kind"] == "fact"
    assert result["items"][0]["tags"] == ["rent"]
    assert result["items"][0]["score"] > 0
    assert "content" in result["items"][0]["matched_fields"]


def test_recall_filters_and_scores_russian_memory(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    store.remember(
        "В карточках писать живым человеческим языком, не роботизированным шаблоном.",
        kind="fact",
        category="style",
        tags=["карточки", "стиль"],
    )
    store.remember("Use dry technical notes only when the owner asks.", kind="fact", category="style", tags=["style"])
    store.add_task("Проверить стиль записок в CRM", tags=["карточки"])

    result = store.recall("живым языком", kind="fact", category="style", tags=["карточки"])

    assert result["ok"] is True
    assert result["total_matches"] == 1
    assert result["items"][0]["kind"] == "fact"
    assert result["items"][0]["category"] == "style"
    assert result["items"][0]["matched_fields"]


def test_learn_from_feedback_creates_searchable_lesson(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    lesson = store.learn_from_feedback(
        "В карточках писать живо и коротко",
        applies_to="crm_cleanup",
        signal="owner_correction",
        recommendation="Писать одну человеческую строку со статусом и следующим шагом.",
        avoid="Не писать длинный сухой AI-шаблон.",
        importance=0.9,
        confidence=1.0,
        tags=["карточки", "стиль"],
    )

    assert lesson["kind"] == "lesson"
    assert lesson["applies_to"] == "crm_cleanup"
    assert lesson["confidence"] == 1.0
    assert lesson["importance"] == 0.9

    result = store.recall_lessons("человеческую строку", applies_to="crm_cleanup", tags=["стиль"])
    assert result["total_matches"] == 1
    assert result["items"][0]["id"] == lesson["id"]
    assert result["items"][0]["score"] > 0
    assert "recommendation" in result["items"][0]["matched_fields"]


def test_memory_navigation_map_topics_context_and_gaps(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.remember("Владелец любит живой тон в карточках", kind="fact", category="style", tags=["карточки"])
    store.add_task("Проверить счет на оплату", tags=["счета"])
    store.learn_from_feedback(
        "Не писать роботизированные заметки",
        applies_to="crm_cleanup",
        signal="owner_correction",
        recommendation="Писать как помощник управляющего.",
        avoid="Не использовать сухой шаблон.",
        tags=["карточки", "стиль"],
    )

    memory_map = store.memory_map()
    assert memory_map["sections"]["lessons"]["count"] == 1
    assert memory_map["sections"]["facts"]["count"] == 1
    assert "memory_context_for" in memory_map["recommended_flow"]

    topics = store.memory_topics()
    assert topics["categories"]["style"]["count"] >= 1
    assert topics["tags"]["карточки"]["count"] >= 2

    context = store.memory_context_for("уборка crm карточек живым языком")
    assert context["query"] == "уборка crm карточек живым языком"
    assert context["lessons"]
    assert context["preferences_or_facts"]
    assert context["source_boundaries"]

    gaps = store.memory_gaps()
    assert gaps["empty_sections"]["reminders"] >= 0
    assert "conflicts" in gaps


def test_today_context_returns_due_items(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    store.add_task("Check overdue CRM cards")
    store.add_reminder("Call landlord", remind_at="2000-01-01T00:00:00+00:00")
    store.journal("Started manager memory")

    context = store.today_context()
    assert context["ok"] is True
    assert context["tasks"][0]["title"] == "Check overdue CRM cards"
    assert context["reminders"][0]["title"] == "Call landlord"
    assert context["recent_journal"][0]["event"] == "Started manager memory"
    assert context["manager_rules"]
    assert context["memory_use_order"][0] == "today_context"


def test_seed_default_rules_updates_existing_rule(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.seed_default_rules()

    with store.connect() as conn:
        conn.execute(
            "UPDATE manager_rules SET rule = ?, priority = ? WHERE title = ?",
            ("stale", 999, "crm-source-of-truth"),
        )

    result = store.seed_default_rules()

    assert result["ok"] is True
    assert result["updated"] >= 1
    context = store.recall("crm-source-of-truth")
    rule = next(item for item in context["items"] if item["kind"] == "rule" and item["title"] == "crm-source-of-truth")
    assert rule["rule"] != "stale"
    assert rule["priority"] == 10
