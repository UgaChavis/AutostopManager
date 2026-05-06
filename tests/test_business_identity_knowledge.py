from __future__ import annotations

from autostop_manager.knowledge_base import audit_knowledge_annotations, probe_knowledge_base, search_knowledge_base, sync_knowledge_base
from autostop_manager.storage import ManagerMemoryStore


def test_probe_routes_ip_requisites_to_private_business_identity(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    result_sync = sync_knowledge_base(store)

    result = probe_knowledge_base(store, "актуальные реквизиты ИП Гришкявичус карточка предприятия", limit=5)

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "business_identity"
    assert result["open_first"] == "docs/agent/business_identity_playbook.md"
    assert "data/private_knowledge/business_identity_current.json" in result_sync["missing_optional_files"]


def test_business_identity_search_uses_private_current_knowledge(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = search_knowledge_base(store, "ОГРНИП ИНН ОКВЭД ИП Гришкевичус", domain="business_identity", limit=5)

    assert result["ok"] is True
    assert result["items"]
    assert any("business_identity_playbook.md" in item["path"] for item in result["items"])


def test_annotation_audit_covers_business_identity_domain(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = audit_knowledge_annotations(store)

    assert result["ok"] is True
    assert result["domains"]["business_identity"] == 1
