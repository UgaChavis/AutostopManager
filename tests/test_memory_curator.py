from __future__ import annotations

import json

from autostop_manager.memory_curator import audit_memory, curate_memory
from autostop_manager.storage import ManagerMemoryStore


def test_recall_ranks_tags_importance_and_priority_above_plain_recency(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.remember("Generic unrelated card cleanup note", title="old", tags=["misc"])
    important = store.remember(
        "During board cleanup, never move cards between columns.",
        title="board cleanup no movement",
        tags=["board-cleanup", "Приберись", "карточки"],
        importance=0.95,
    )

    result = store.recall("Приберись карточки", limit=5)

    assert result["items"][0]["id"] == important["id"]
    assert result["items"][0]["kind"] == "note"
    assert result["items"][0]["score"] > 0
    assert result["items"][0]["last_used_at"]


def test_memory_audit_finds_duplicates_expired_and_superseded(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    first = store.remember("Supplier passwords must never be stored.", kind="fact", tags=["security"])
    duplicate = store.remember("Supplier passwords must never be stored.", kind="fact", tags=["security"])
    expired = store.remember(
        "Temporary supplier quote expires tomorrow.",
        kind="fact",
        tags=["quote"],
        expires_at="2000-01-01T00:00:00+00:00",
    )
    old = store.remember("Old cleanup command may move cards.", title="old cleanup rule", tags=["board-cleanup"])
    replacement = store.remember(
        "Cleanup command must not move cards.",
        title="new cleanup rule",
        tags=["board-cleanup"],
        supersedes_id=old["id"],
    )

    result = audit_memory(store)

    assert result["ok"] is True
    assert any(item["ids"] == [first["id"], duplicate["id"]] for item in result["duplicates"])
    assert all(item["content_included"] is False for item in result["duplicates"])
    assert any(item["id"] == expired["id"] for item in result["expired"])
    assert any(item["id"] == old["id"] and item["superseded_by"] == replacement["id"] for item in result["superseded"])
    assert result["privacy"]["content_preview_included"] is False


def test_memory_audit_does_not_return_raw_private_content(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sensitive = "Client email test@example.com phone +7 999 123-45-67 VIN WBA00000000000000"
    store.remember(sensitive, kind="fact", title="private")
    store.remember(sensitive, kind="fact", title="private")
    store.remember(
        "Temporary token sk-testsecret123456789 expires.", kind="fact", expires_at="2000-01-01T00:00:00+00:00"
    )

    result = audit_memory(store)
    rendered = json.dumps(result, ensure_ascii=False)

    assert result["duplicates"][0]["content_included"] is False
    assert result["expired"][0]["content_included"] is False
    assert "test@example.com" not in rendered
    assert "+7 999 123-45-67" not in rendered
    assert "WBA00000000000000" not in rendered
    assert "sk-testsecret" not in rendered


def test_memory_audit_flags_sensitive_candidates_without_exposing_content(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sensitive = "Client email test@example.com phone +7 999 123-45-67 VIN WBA00000000000000 C-ABCDEF12"
    record = store.remember(sensitive, kind="fact", title="private")

    result = audit_memory(store)
    rendered = json.dumps(result, ensure_ascii=False)

    candidate = next(item for item in result["sensitive_candidates"] if item["id"] == record["id"])
    assert {"email", "phone", "vin", "crm_entity_ref"}.issubset(candidate["detectors"])
    assert candidate["content_included"] is False
    assert result["privacy"]["sensitive_candidate_content_redacted"] is True
    assert result["privacy"]["sensitive_candidate_count"] == 1
    assert "test@example.com" not in rendered
    assert "+7 999 123-45-67" not in rendered
    assert "WBA00000000000000" not in rendered
    assert "C-ABCDEF12" not in rendered


def test_curate_memory_can_mark_duplicates_archived_without_deleting(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.remember("Duplicate operational note", tags=["ops"])
    second = store.remember("Duplicate operational note", tags=["ops"])

    result = curate_memory(store, apply=True)
    recalled = store.recall("Duplicate operational note", limit=5)

    assert result["archived_duplicates"] == [second["id"]]
    assert all(item["id"] != second["id"] for item in recalled["items"])
