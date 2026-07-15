from __future__ import annotations

import json

from autostop_manager.memory_review import (
    apply_memory_review_item,
    build_memory_review,
    memory_review_payload_has_raw_private_data,
)
from autostop_manager.storage import ManagerMemoryStore


def test_memory_review_detects_duplicate_without_raw_content(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.remember("Reusable operating lesson", kind="note", title="same")
    store.remember("Reusable operating lesson", kind="note", title="same")

    review = build_memory_review(store)

    duplicate = next(item for item in review["items"] if item["kind"] == "duplicate")
    rendered = json.dumps(duplicate, ensure_ascii=False)
    assert duplicate["source_ref"].startswith("note:")
    assert duplicate["proposal"]["content_included"] is False
    assert "Reusable operating lesson" not in rendered
    assert review["summary"]["privacy_check"] is True


def test_memory_review_apply_archives_duplicate_without_deleting(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.remember("Duplicate candidate", kind="fact", category="ops")
    store.remember("Duplicate candidate", kind="fact", category="ops")
    review = build_memory_review(store)
    duplicate = next(item for item in review["items"] if item["kind"] == "duplicate")

    result = apply_memory_review_item(duplicate["id"], "archive_duplicate", store=store)

    assert result["ok"] is True
    assert result["source_records_deleted"] is False
    assert result["archived_duplicate_ids"]
    with store.connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS count FROM facts").fetchone()["count"]
        archived = conn.execute("SELECT COUNT(*) AS count FROM facts WHERE archived_at IS NOT NULL").fetchone()["count"]
    assert total == 2
    assert archived == 1


def test_memory_review_apply_reject_cannot_be_replayed_as_archive(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.remember("Duplicate candidate", kind="note")
    store.remember("Duplicate candidate", kind="note")
    duplicate = next(item for item in build_memory_review(store)["items"] if item["kind"] == "duplicate")

    rejected = apply_memory_review_item(duplicate["id"], "reject", store=store)
    replayed = apply_memory_review_item(duplicate["id"], "archive_duplicate", store=store)

    assert rejected["ok"] is True
    assert replayed["ok"] is False
    assert replayed["error"] == "memory review item already decided"
    with store.connect() as conn:
        archived = conn.execute("SELECT COUNT(*) AS count FROM notes WHERE archived_at IS NOT NULL").fetchone()["count"]
    assert archived == 0


def test_memory_review_archive_duplicate_fails_closed_for_stale_item(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    first = store.remember("Duplicate candidate", kind="note")
    store.remember("Duplicate candidate", kind="note")
    duplicate = next(item for item in build_memory_review(store)["items"] if item["kind"] == "duplicate")
    with store.connect() as conn:
        conn.execute("UPDATE notes SET archived_at = updated_at WHERE id = ?", (first["id"],))

    result = apply_memory_review_item(duplicate["id"], "archive_duplicate", store=store)

    assert result["ok"] is False
    assert result["error"] == "duplicate review item is stale"
    with store.connect() as conn:
        archived = conn.execute("SELECT COUNT(*) AS count FROM notes WHERE archived_at IS NOT NULL").fetchone()["count"]
    assert archived == 1


def test_memory_review_private_data_validator():
    assert memory_review_payload_has_raw_private_data({"proposal": "email test@example.com"})
    assert not memory_review_payload_has_raw_private_data(
        {"source_ref": "fact:1", "proposal": {"content_included": False}}
    )
