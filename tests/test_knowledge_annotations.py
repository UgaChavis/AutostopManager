from __future__ import annotations

import autostop_manager.knowledge_base as knowledge_base_module
from autostop_manager.knowledge_base import (
    audit_knowledge_base,
    probe_knowledge_base,
    search_knowledge_base,
    sync_knowledge_base,
)
from autostop_manager.storage import ManagerMemoryStore


def test_sync_indexes_knowledge_annotations(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    result = sync_knowledge_base(store)

    assert result["annotations_indexed"] > 0
    audit = audit_knowledge_base(store)
    assert audit["annotations_indexed"] == result["annotations_indexed"]
    assert audit["warnings"] == []


def test_annotation_boost_routes_memory_quality_queries(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "улучшить память индексацию аннотации качество знаний", limit=5)

    assert result["has_knowledge"] is True
    assert result["best_domain"] == "knowledge_intake"
    assert result["confidence"] >= 0.45
    assert any("knowledge_annotations.jsonl" in path for path in result["source_of_truth"])


def test_search_uses_annotations_for_compact_answers(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = search_knowledge_base(store, "устаревшие воспоминания дубли качество памяти", limit=5)

    assert result["items"]
    assert result["items"][0]["document_type"] == "annotation"
    assert result["items"][0]["domain"] == "startup_and_identity"


def test_sync_knowledge_base_handles_unreadable_annotations_file(tmp_path, monkeypatch):
    annotations_path = tmp_path / "knowledge_annotations.jsonl"
    annotations_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(knowledge_base_module, "KNOWLEDGE_ANNOTATIONS_PATH", annotations_path)

    original_read_text = knowledge_base_module.Path.read_text

    def fake_read_text(self, encoding="utf-8-sig", *args, **kwargs):
        if self == annotations_path:
            raise OSError("permission denied")
        return original_read_text(self, encoding=encoding, *args, **kwargs)

    monkeypatch.setattr(knowledge_base_module.Path, "read_text", fake_read_text)

    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    result = sync_knowledge_base(store)

    assert result["ok"] is True
    assert result["annotations_indexed"] == 0
