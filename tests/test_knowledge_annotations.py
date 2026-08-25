from __future__ import annotations

from autostop_manager.knowledge_base import (
    audit_knowledge_annotations,
    search_knowledge_base,
    sync_knowledge_base,
)
from autostop_manager.storage import ManagerMemoryStore


def test_annotation_audit_is_metadata_compatibility_surface(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    synced = sync_knowledge_base(store)
    audit = audit_knowledge_annotations(store)

    assert synced["annotations_indexed"] == 0
    assert audit["ok"] is True
    assert audit["compatibility_mode"] == "knowledge_map_document_metadata"
    assert audit["annotations_indexed"] == 0
    assert audit["documents_declared"] > 0


def test_search_results_come_from_documents_not_annotations(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = search_knowledge_base(store, "BMW F15 N63", limit=5)

    assert result["items"]
    assert all(item["document_type"] != "annotation" for item in result["items"])
