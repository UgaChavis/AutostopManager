from __future__ import annotations

import json
import sqlite3

import pytest

from autostop_manager import storage as storage_module
from autostop_manager.storage import ManagerMemoryStore


def _insert_store_conductor_run(
    store: ManagerMemoryStore,
    *,
    status: str,
    checkpoint: dict | str,
    external_step_status: str | None = None,
) -> None:
    store.initialize()
    now = storage_module._now()
    checkpoint_json = checkpoint if isinstance(checkpoint, str) else json.dumps(checkpoint)
    with store.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO manager_runs (intent, workflow_id, status, checkpoint_json, started_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("store_quote_conductor", "store_quote_conductor", status, checkpoint_json, now, now),
        )
        if external_step_status is not None:
            conn.execute(
                """
                INSERT INTO manager_run_external_steps
                    (run_id, step_id, connector, action, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (cursor.lastrowid, "legacy-step", "telegram", "send", external_step_status, now, now),
            )


def test_store_quote_conductor_release_readiness_is_aggregate_and_read_only(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    store = ManagerMemoryStore(db_path)
    store.initialize()
    before = db_path.read_bytes()

    ready = store.store_quote_conductor_release_readiness()

    assert ready == {
        "ok": True,
        "format": "store_quote_conductor_release_readiness_v1",
        "read_only": True,
        "active_total": 0,
        "legacy_active_total": 0,
        "active_by_status": {},
        "active_by_phase": {},
        "legacy_by_phase": {},
        "runs_with_external_steps": 0,
        "pending_external_steps": 0,
        "legacy_checkpoint_count": 0,
        "blocking_reasons": [],
    }
    assert db_path.read_bytes() == before


def test_store_quote_conductor_release_readiness_blocks_active_legacy_state(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    _insert_store_conductor_run(
        store,
        status="external_wait",
        checkpoint={"phase": "clarifying", "telegram_context_hash": "a" * 64},
        external_step_status="pending",
    )

    result = store.store_quote_conductor_release_readiness()

    assert result["ok"] is False
    assert result["active_total"] == result["legacy_active_total"] == 1
    assert result["active_by_status"] == {"external_wait": 1}
    assert result["legacy_by_phase"] == {"clarifying": 1}
    assert result["runs_with_external_steps"] == result["pending_external_steps"] == 1
    assert result["legacy_checkpoint_count"] == 1
    assert result["blocking_reasons"] == [
        "legacy_store_quote_conductor_active_runs",
        "legacy_store_quote_conductor_external_wait_runs",
        "legacy_store_quote_conductor_checkpoints",
        "legacy_store_quote_conductor_external_steps",
    ]


def test_store_quote_conductor_release_readiness_accepts_current_and_terminal_legacy_runs(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    _insert_store_conductor_run(store, status="planned", checkpoint={"phase": "new"})
    _insert_store_conductor_run(store, status="completed", checkpoint={"phase": "clarifying"})

    result = store.store_quote_conductor_release_readiness()

    assert result["ok"] is True
    assert result["active_total"] == 1
    assert result["active_by_phase"] == {"new": 1}


def test_store_quote_conductor_release_readiness_blocks_a_malformed_active_checkpoint(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    _insert_store_conductor_run(store, status="executing", checkpoint="not-json")

    result = store.store_quote_conductor_release_readiness()

    assert result["ok"] is False
    assert result["legacy_by_phase"] == {"missing": 1}
    assert result["legacy_checkpoint_count"] == 1
    assert "legacy_store_quote_conductor_checkpoints" in result["blocking_reasons"]


def test_store_quote_conductor_release_readiness_fails_closed_without_a_database(tmp_path):
    path = tmp_path / "missing.sqlite3"

    result = ManagerMemoryStore(path).store_quote_conductor_release_readiness()

    assert result == {
        "ok": False,
        "read_only": True,
        "error": "store_quote_conductor_release_database_unavailable",
        "blocking_reasons": ["store_quote_conductor_release_database_unavailable"],
    }
    assert not path.exists()


def test_store_connect_context_closes_connection(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    with store.connect() as conn:
        conn.execute("SELECT 1").fetchone()

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_initialize_removes_legacy_director_journal_artifacts(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE director_journal (id INTEGER PRIMARY KEY, event TEXT NOT NULL);
            CREATE INDEX idx_director_journal_event ON director_journal(event);
            INSERT INTO director_journal (event) VALUES ('legacy');
            """
        )

    store = ManagerMemoryStore(db_path)
    store.initialize()
    store.initialize()

    with sqlite3.connect(db_path) as conn:
        legacy_artifacts = conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'director_journal%' OR name LIKE 'idx_director_journal%'"
        ).fetchall()
        journal_exists = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'journal'"
        ).fetchone()[0]

    assert legacy_artifacts == []
    assert journal_exists == 1


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


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("remember", {"content": "VIN JTEBU3FJX05027767"}),
        ("remember", {"content": "Связаться по +7 999 123-45-67"}),
        ("learn_from_feedback", {"content": "token=sk-secret-value"}),
    ],
)
def test_durable_memory_rejects_sensitive_values_before_persistence(tmp_path, method, kwargs):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    result = getattr(store, method)(**kwargs)

    assert result == {"ok": False, "error": "unsafe_durable_memory_value"}
    assert store.recall(kind="note")["items"] == []
    assert store.recall(kind="fact")["items"] == []
    assert store.recall(kind="lesson")["items"] == []


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
    store.remember(
        "Очень важная заметка про уборку карточек CRM и форматирование описания.",
        kind="note",
        category="board_cleanup",
        tags=["crm", "card_description"],
        importance=5.0,
    )

    result = store.recall("source boundaries service records", limit=5)

    titles = [str(item.get("title") or "") for item in result["items"]]
    assert titles[0] == "source-and-privacy"
    assert all(item["matched_fields"] for item in result["items"])
    assert not any(item.get("category") == "board_cleanup" for item in result["items"])


def test_memory_context_uses_cross_system_rule_without_domain_procedure_noise(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
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
        "For BMW F15 N63 tasks, verify VIN, market, grade, and OEM part conclusions.",
        kind="note",
        title="BMW F15 N63 knowledge route",
        category="automotive_repair",
        tags=["bmw", "vin", "oem"],
        importance=5.0,
    )

    result = store.memory_context_for("в карточке CRM VIN найти OEM фильтра", limit=5)

    assert result["preferences_or_facts"]
    assert result["preferences_or_facts"][0]["kind"] == "rule"
    assert result["preferences_or_facts"][0]["title"] == "source-and-privacy"
    context_text = "\n".join(
        str(item.get("title") or item.get("content") or item.get("rule") or "")
        for item in result["preferences_or_facts"]
    ).casefold()
    assert "board-cleanup" not in context_text
    assert "приберись" not in context_text
    assert "оформление описаний" not in context_text
    assert "bmw f15 n63" not in context_text


def test_memory_context_keeps_board_cleanup_rules_for_explicit_cleanup_query(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
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


def test_memory_context_keeps_command_knowledge_boundary_for_routing_query(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    result = store.memory_context_for("command routing knowledge lookup write policy", limit=20)

    context_text = "\n".join(
        str(item.get("title") or item.get("content") or item.get("rule") or "")
        for item in result["preferences_or_facts"]
    ).casefold()
    assert "route-and-authority" in context_text


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
    assert "recommended_flow" not in memory_map

    topics = store.memory_topics()
    assert topics["categories"]["style"]["count"] >= 1
    assert topics["tags"]["карточки"]["count"] >= 2

    context = store.memory_context_for("уборка crm карточек живым языком")
    assert context["query"] == "уборка crm карточек живым языком"
    assert context["lessons"]
    assert context["preferences_or_facts"]
    assert context["source_boundaries"]

    gaps = store.memory_gaps()
    assert "conflicts" in gaps


def test_today_context_returns_due_tasks_and_journal(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    store.add_task("Check overdue CRM cards")
    store.journal("Started manager memory")

    context = store.today_context()
    assert context["ok"] is True
    assert context["tasks"][0]["title"] == "Check overdue CRM cards"
    assert "reminders" not in context
    assert context["recent_journal"][0]["event"] == "Started manager memory"
    assert context["manager_rules"]
    assert "memory_use_order" not in context


def test_generic_journal_is_private_and_size_bounded(tmp_path, monkeypatch):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    monkeypatch.setattr(storage_module, "GENERIC_JOURNAL_MAX_ENTRIES", 2)

    assert store.journal("Связаться по +7 999 123-45-67")["ok"] is False
    assert store.journal("первое безопасное событие")["ok"] is True
    assert store.journal("второе безопасное событие")["ok"] is True
    assert store.journal("третье безопасное событие")["ok"] is True

    context = store.today_context(limit=10)
    assert [item["event"] for item in context["recent_journal"]] == [
        "третье безопасное событие",
        "второе безопасное событие",
    ]


def test_manager_rules_are_read_live_from_json(tmp_path, monkeypatch):
    rules_path = tmp_path / "manager_rules.json"
    monkeypatch.setattr(storage_module, "MANAGER_RULES_PATH", rules_path)
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    rules_path.write_text('{"rules":[{"id":"live","priority":10,"rule":"first"}]}', encoding="utf-8")
    assert store.recall("first", kind="rule")["items"][0]["rule"] == "first"

    rules_path.write_text('{"rules":[{"id":"live","priority":10,"rule":"second"}]}', encoding="utf-8")
    assert store.today_context()["manager_rules"][0]["rule"] == "second"


def test_today_context_reports_unavailable_rule_file(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module, "MANAGER_RULES_PATH", tmp_path / "missing.json")
    result = ManagerMemoryStore(tmp_path / "memory.sqlite3").today_context()

    assert result["warnings"] == ["manager_rules_unavailable"]
