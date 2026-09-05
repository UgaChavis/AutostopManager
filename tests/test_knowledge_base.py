from __future__ import annotations

import json

import pytest

import autostop_manager.knowledge_base as kb
from autostop_manager.knowledge_base import (
    audit_knowledge_base,
    probe_knowledge_base,
    search_knowledge_base,
    sync_knowledge_base,
)
from autostop_manager.storage import ManagerMemoryStore


def test_sync_indexes_current_combined_domains(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    result = sync_knowledge_base(store)

    assert result["ok"] is True
    assert {"service_case", "business_documents", "startup_and_identity"}.issubset(result["domains"])
    assert result["documents_indexed"] > 0
    assert result["sections_indexed"] > 0
    assert result["missing_files"] == []
    assert isinstance(result["optional_missing_files"], list)


def test_sync_does_not_index_paths_outside_active_root(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    docs_agent = root / "docs" / "agent"
    docs_agent.mkdir(parents=True)
    playbook = docs_agent / "safe.md"
    playbook.write_text("# Safe\n\nSafe route text.\n", encoding="utf-8")
    outside = tmp_path / "outside-secret.md"
    outside.write_text("# Outside\n\nLEAKED-PRIVATE-CONTENT-98765\n", encoding="utf-8")
    knowledge_map = docs_agent / "knowledge_map.json"
    knowledge_map.write_text(
        json.dumps(
            {
                "domains": {
                    "safe_domain": {
                        "title": "Safe domain",
                        "use_when": ["test"],
                        "aliases": ["safe"],
                        "keywords": ["safe"],
                        "questions": ["safe?"],
                        "source_of_truth_files": ["docs/agent/safe.md"],
                        "primary_files": ["docs/agent/safe.md", str(outside), "../outside-secret.md"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(kb, "KNOWLEDGE_MAP_PATH", knowledge_map)
    store = ManagerMemoryStore(root / "memory.sqlite3")

    sync = sync_knowledge_base(store)
    search = search_knowledge_base(store, "LEAKED-PRIVATE-CONTENT-98765", limit=5)
    audit = audit_knowledge_base(store)

    assert sync["ok"] is True
    assert str(outside) in sync["missing_files"]
    assert "../outside-secret.md" in sync["missing_files"]
    assert search["items"] == []
    assert audit["ok"] is False


def test_sync_fails_closed_on_empty_map_without_wiping_existing_index(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    docs_agent = root / "docs" / "agent"
    docs_agent.mkdir(parents=True)
    (docs_agent / "safe.md").write_text("# Safe\n\nSafe route text.\n", encoding="utf-8")
    knowledge_map = docs_agent / "knowledge_map.json"
    knowledge_map.write_text(
        json.dumps({"domains": {"safe_domain": {"title": "Safe domain", "primary_files": ["docs/agent/safe.md"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(kb, "KNOWLEDGE_MAP_PATH", knowledge_map)
    store = ManagerMemoryStore(root / "memory.sqlite3")
    assert sync_knowledge_base(store)["ok"] is True
    knowledge_map.write_text(json.dumps({"domains": {}}), encoding="utf-8")

    result = sync_knowledge_base(store)
    with store.connect() as conn:
        documents = conn.execute("SELECT COUNT(*) AS count FROM knowledge_documents").fetchone()["count"]

    assert result["ok"] is False
    assert result["error"] == "knowledge_map.json has no valid domains"
    assert documents > 0
    assert "knowledge_map_has_no_valid_domains" in audit_knowledge_base(store)["warnings"]


def test_fts_rebuild_is_idempotent_and_audit_detects_corruption(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    first = sync_knowledge_base(store)
    second = sync_knowledge_base(store)
    assert second["sections_indexed"] == first["sections_indexed"]

    with store.connect() as conn:
        conn.execute("DELETE FROM knowledge_sections_fts")
        sections = conn.execute("SELECT COUNT(*) AS count FROM knowledge_sections").fetchone()["count"]

    audit = audit_knowledge_base(store)
    assert sections > 0
    assert audit["ok"] is False
    assert audit["sections_fts_indexed"] == 0
    assert "knowledge_sections_fts_count_mismatch" in audit["warnings"]


@pytest.mark.parametrize(
    ("query", "domain"),
    [
        ("BMW F15 N63 BDC", "service_case"),
        ("создай счет без карточки CRM в стандартном шаблоне AutoStop PDF", "business_documents"),
        ("подготовь менеджера к работе и почитай документацию", "startup_and_identity"),
    ],
)
def test_probe_routes_current_combined_domains(tmp_path, query, domain):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, query, limit=5)

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == domain
    assert result["next_action"] == "open_source_of_truth"


def test_search_filters_service_case_documents(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = search_knowledge_base(store, "моторное масло Toyota", domain="service_case", limit=5)

    assert result["ok"] is True
    assert result["items"]
    assert all(item["domain"] == "service_case" for item in result["items"])


def test_probe_unknown_vehicle_stays_external(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "Citroen C5 Hydractive sphere pressure")

    assert result["ok"] is True
    assert result["has_knowledge"] is False
    assert result["next_action"] == "route_external_sources"


def test_audit_matches_indexed_route_cards(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = audit_knowledge_base(store)

    assert result["ok"] is True
    assert result["route_cards_indexed"] == result["domain_count"]
    assert result["sections_fts_indexed"] == result["sections_indexed"]
    assert result["missing_files"] == []
