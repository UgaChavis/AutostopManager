from __future__ import annotations

import json

import autostop_manager.knowledge_base as kb
from autostop_manager.knowledge_base import (
    audit_knowledge_annotations,
    probe_knowledge_base,
    search_knowledge_base,
    sync_knowledge_base,
)
from autostop_manager.storage import ManagerMemoryStore


PRIVATE_RUNTIME_FILES = [
    "data/private_knowledge/business_identity_current.json",
    "data/private_knowledge/business_documents_inventory.json",
]


def test_probe_routes_ip_requisites_to_private_business_identity(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "актуальные реквизиты ИП карточка предприятия", limit=5)

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "business_identity"
    assert result["open_first"] == "docs/agent/business_identity_playbook.md"
    assert any("business_identity_playbook.md" in path for path in result["source_of_truth"])
    assert any("business_identity_current.json" in path for path in result["optional_runtime_files"])


def test_business_identity_search_returns_public_route_when_private_runtime_files_are_missing(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = search_knowledge_base(store, "ОГРНИП ИНН ОКВЭД ИП", domain="business_identity", limit=5)

    assert result["ok"] is True
    assert result["items"]
    assert any(
        item["path"] == "knowledge_map:business_identity"
        or "business_identity_playbook.md" in item["path"]
        or item["document_type"] == "annotation"
        for item in result["items"]
    )


def test_business_identity_reports_missing_private_runtime_files_as_optional(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = kb.audit_knowledge_base(store)

    assert result["ok"] is True
    assert result["missing_files"] == []
    assert set(PRIVATE_RUNTIME_FILES).issubset(set(result["optional_missing_files"]))


def test_business_identity_probe_reports_optional_runtime_status(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "актуальные реквизиты ИП ОГРНИП", limit=5)

    assert result["ok"] is True
    assert result["best_domain"] == "business_identity"
    route = result["routes"][0]
    assert set(PRIVATE_RUNTIME_FILES).issubset(set(route["optional_runtime_files"]))
    assert set(PRIVATE_RUNTIME_FILES).issubset(set(route["optional_missing_files"]))
    assert route["optional_available_files"] == []
    assert route["optional_runtime_available"] is False
    assert "unavailable" in route["optional_runtime_note"]
    assert set(PRIVATE_RUNTIME_FILES).issubset(set(result["optional_missing_files"]))


def test_business_identity_indexes_existing_optional_runtime_file(tmp_path, monkeypatch):
    playbook = tmp_path / "business_identity_playbook.md"
    playbook.write_text("# Business identity\n\nUse local private runtime facts when present.\n", encoding="utf-8")
    private_file = tmp_path / "business_identity_current.json"
    private_file.write_text(
        json.dumps(
            {
                "source": "synthetic test fixture",
                "ogrnip": "TEST-RUNTIME-OGRNIP",
                "settlement_account": "TEST-RUNTIME-ACCOUNT",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    map_path = tmp_path / "knowledge_map.json"
    map_path.write_text(
        json.dumps(
            {
                "domains": {
                    "business_identity": {
                        "title": "Synthetic business identity route",
                        "use_when": ["testing optional runtime private knowledge"],
                        "aliases": ["business identity", "реквизиты ИП"],
                        "keywords": ["business_identity_current", "TEST-RUNTIME-OGRNIP"],
                        "questions": ["какие актуальные реквизиты"],
                        "source_of_truth_files": [str(playbook)],
                        "primary_files": [str(playbook)],
                        "optional_runtime_files": [str(private_file)],
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(kb, "KNOWLEDGE_MAP_PATH", map_path)
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    sync_result = kb.sync_knowledge_base(store)
    result = kb.search_knowledge_base(store, "TEST-RUNTIME-OGRNIP расчетный счет", domain="business_identity", limit=5)

    assert sync_result["ok"] is True
    assert sync_result["missing_files"] == []
    assert sync_result["optional_missing_files"] == []
    assert any(item["path"] == str(private_file) for item in result["items"])


def test_annotation_audit_covers_business_identity_domain(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = audit_knowledge_annotations(store)

    assert result["ok"] is True
    assert result["domains"]["business_identity"] == 1
