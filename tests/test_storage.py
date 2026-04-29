from __future__ import annotations

from autostop_manager.storage import ManagerMemoryStore


def test_remember_and_recall(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    created = store.remember("Rent is paid before day 5", kind="fact", tags=["rent"])
    assert created["ok"] is True

    result = store.recall("Rent")
    assert result["ok"] is True
    assert result["items"][0]["kind"] == "fact"
    assert result["items"][0]["tags"] == ["rent"]


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
