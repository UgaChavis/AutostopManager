from __future__ import annotations

import sqlite3

import pytest

from autostop_manager import storage as storage_module
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


def test_remember_clamps_importance_and_confidence(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    created = store.remember("Very important bounded fact", kind="fact", importance=50, confidence=2)
    recalled = store.recall("Very important", kind="fact")["items"][0]

    assert created["confidence"] == 1.0
    assert recalled["importance"] == 1.0
    assert recalled["confidence"] == 1.0


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


def test_recall_query_requires_text_match_before_importance_boost(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.seed_default_rules()
    store.remember(
        "Очень важная заметка про уборку карточек CRM и форматирование описания.",
        kind="note",
        category="board_cleanup",
        tags=["crm", "card_description"],
        importance=5.0,
    )

    result = store.recall("VIN OEM фильтра", limit=5)

    titles = [str(item.get("title") or "") for item in result["items"]]
    assert "vin-oem-lookup-workflow" in titles
    assert all(item["matched_fields"] for item in result["items"])
    assert not any(item.get("category") == "board_cleanup" for item in result["items"])


def test_memory_context_uses_focused_vin_oem_query_before_generic_crm_noise(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.seed_default_rules()
    store.remember(
        "Очень важная заметка про уборку карточек CRM и форматирование описания.",
        kind="note",
        category="board_cleanup",
        tags=["crm", "card_description"],
        importance=5.0,
    )
    store.remember(
        "По команде владельца «Приберись» нужно проверять VIN и профиль автомобиля в CRM.",
        kind="fact",
        category="crm_operations",
        tags=["crm", "vin"],
        importance=5.0,
    )
    store.remember(
        "Когда владелец просит оформить описание карточки, нужно не мешать это с VIN/OEM подбором.",
        kind="note",
        title="Оформление описаний карточек",
        category="crm_style",
        tags=["crm", "карточки"],
        importance=5.0,
    )
    store.remember(
        "For Toyota GR Yaris tasks, verify VIN, market, grade, and OEM part conclusions.",
        kind="note",
        title="Toyota GR Yaris knowledge route",
        category="automotive_repair",
        tags=["toyota", "vin", "oem"],
        importance=5.0,
    )

    result = store.memory_context_for("в карточке CRM VIN найти OEM фильтра", limit=5)

    assert result["preferences_or_facts"]
    assert result["preferences_or_facts"][0]["kind"] == "rule"
    assert result["preferences_or_facts"][0]["title"] == "vin-oem-lookup-workflow"
    context_text = "\n".join(
        str(item.get("title") or item.get("content") or item.get("rule") or "")
        for item in result["preferences_or_facts"]
    ).casefold()
    assert "board-cleanup" not in context_text
    assert "приберись" not in context_text
    assert "оформление описаний" not in context_text
    assert "knowledge-catalog-sync" not in context_text
    assert "github-publication-privacy" not in context_text
    assert "toyota gr yaris" not in context_text


def test_memory_context_keeps_board_cleanup_rules_for_explicit_cleanup_query(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.seed_default_rules()
    store.remember(
        "При команде «Приберись» карточку нужно оформить кратко и сохранить важные факты.",
        kind="note",
        title="Команда «Приберись»: оформление описаний карточек",
        category="crm_style",
        tags=["crm", "карточки"],
        importance=5.0,
    )

    result = store.memory_context_for("Приберись: оформи описание CRM карточки с VIN", limit=20)

    context_text = "\n".join(
        str(item.get("title") or item.get("content") or item.get("rule") or "")
        for item in result["preferences_or_facts"]
    ).casefold()
    assert "board-cleanup" in context_text or "приберись" in context_text


def test_archived_lessons_are_not_recalled_or_used_for_context(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    lesson = store.learn_from_feedback(
        "Старая инструкция по оформлению карточек.",
        applies_to="crm_cleanup",
        signal="owner_correction",
        recommendation="Не использовать после архивирования.",
        tags=["карточки"],
    )
    with store.connect() as conn:
        conn.execute("UPDATE lessons SET archived_at = updated_at WHERE id = ?", (lesson["id"],))

    recalled = store.recall_lessons("Старая инструкция", limit=5)
    context = store.memory_context_for("оформи описание CRM карточки", limit=5)
    context_text = "\n".join(
        str(item.get("title") or item.get("content") or item.get("recommendation") or "") for item in context["lessons"]
    )

    assert recalled["items"] == []
    assert "Старая инструкция" not in context_text


def test_memory_context_keeps_admin_rules_for_explicit_knowledge_query(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.seed_default_rules()

    result = store.memory_context_for("обнови базу знаний и синхронизируй catalog", limit=20)

    context_text = "\n".join(
        str(item.get("title") or item.get("content") or item.get("rule") or "")
        for item in result["preferences_or_facts"]
    ).casefold()
    assert "knowledge-catalog-sync" in context_text


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


def test_seed_default_rules_removes_only_obsolete_docs_seeded_rules(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.seed_default_rules()

    with store.connect() as conn:
        now = "2026-07-14T00:00:00+00:00"
        conn.execute(
            """
            INSERT INTO manager_rules (title, rule, scope, priority, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("obsolete-doc-rule", "stale", "general", 100, "docs/agent/manager_rules.json", now, now),
        )
        conn.execute(
            """
            INSERT INTO manager_rules (title, rule, scope, priority, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("custom-rule", "keep", "general", 100, "owner", now, now),
        )

    result = store.seed_default_rules()

    assert result["removed"] == 1
    with store.connect() as conn:
        titles = {row["title"] for row in conn.execute("SELECT title FROM manager_rules")}
    assert "obsolete-doc-rule" not in titles
    assert "custom-rule" in titles


def test_seed_default_rules_handles_invalid_manager_rules_payload(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    rules_path = root / "docs" / "agent" / "manager_rules.json"
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(storage_module, "PROJECT_ROOT", root)

    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    result = store.seed_default_rules()

    assert result["ok"] is False
    assert result["error"] == "manager_rules.json invalid_structure"
    assert result["error_detail"] == "list"


def test_today_context_reports_failed_default_rule_seed(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    rules_path = root / "docs" / "agent" / "manager_rules.json"
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(storage_module, "PROJECT_ROOT", root)

    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    result = store.today_context()

    assert result["ok"] is True
    assert result["warnings"] == ["manager_rules_seed_failed: manager_rules.json invalid_structure"]
