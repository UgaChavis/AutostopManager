from __future__ import annotations

import json

import autostop_manager.knowledge_base as kb
from autostop_manager.knowledge_base import (
    audit_knowledge_base,
    probe_knowledge_base,
    search_knowledge_base,
    sync_knowledge_base,
)
from autostop_manager.storage import ManagerMemoryStore


PRIVATE_RUNTIME_FILES = [
    "data/private_knowledge/business_identity_current.json",
    "data/private_knowledge/business_documents_inventory.json",
]


def test_sync_indexes_domains_documents_and_sections(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    result = sync_knowledge_base(store)

    assert result["ok"] is True
    assert result["documents_indexed"] > 0
    assert result["sections_indexed"] > 0
    assert "automotive_repair" in result["domains"]
    assert result["missing_files"] == []
    assert set(PRIVATE_RUNTIME_FILES).issubset(set(result["optional_missing_files"]))


def test_sync_does_not_index_knowledge_paths_outside_active_root(tmp_path, monkeypatch):
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

    sync = kb.sync_knowledge_base(store)
    search = kb.search_knowledge_base(store, "LEAKED-PRIVATE-CONTENT-98765", limit=5)
    audit = kb.audit_knowledge_base(store)

    assert sync["ok"] is True
    assert str(outside) in sync["missing_files"]
    assert "../outside-secret.md" in sync["missing_files"]
    assert search["items"] == []
    assert audit["ok"] is False
    assert str(outside) in audit["missing_files"]


def test_sync_fails_closed_on_empty_knowledge_map_without_wiping_existing_index(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    docs_agent = root / "docs" / "agent"
    docs_agent.mkdir(parents=True)
    playbook = docs_agent / "safe.md"
    playbook.write_text("# Safe\n\nSafe route text.\n", encoding="utf-8")
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
                        "primary_files": ["docs/agent/safe.md"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(kb, "KNOWLEDGE_MAP_PATH", knowledge_map)
    store = ManagerMemoryStore(root / "memory.sqlite3")
    first = kb.sync_knowledge_base(store)
    knowledge_map.write_text(json.dumps({"domains": {}}), encoding="utf-8")

    second = kb.sync_knowledge_base(store)
    audit = kb.audit_knowledge_base(store)

    with store.connect() as conn:
        documents = conn.execute("SELECT COUNT(*) AS count FROM knowledge_documents").fetchone()

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["error"] == "knowledge_map.json has no valid domains"
    assert int(documents["count"]) > 0
    assert audit["ok"] is False
    assert "knowledge_map_has_no_valid_domains" in audit["warnings"]


def test_sync_populates_fast_knowledge_fts_indexes(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    with store.connect() as conn:
        sections = conn.execute(
            "SELECT COUNT(*) AS count FROM knowledge_sections_fts WHERE knowledge_sections_fts MATCH ?",
            ("KOMBI",),
        ).fetchone()

    assert int(sections["count"]) > 0


def test_sync_rebuilds_fast_knowledge_fts_indexes_idempotently(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    first = sync_knowledge_base(store)
    second = sync_knowledge_base(store)

    with store.connect() as conn:
        sections = conn.execute("SELECT COUNT(*) AS count FROM knowledge_sections").fetchone()
        sections_fts = conn.execute("SELECT COUNT(*) AS count FROM knowledge_sections_fts").fetchone()

    assert second["sections_indexed"] == first["sections_indexed"]
    assert int(sections_fts["count"]) == int(sections["count"])


def test_audit_reports_broken_fast_knowledge_fts_indexes(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)
    with store.connect() as conn:
        conn.execute("DELETE FROM knowledge_sections_fts")

    result = audit_knowledge_base(store)

    assert result["ok"] is False
    assert result["sections_fts_indexed"] == 0
    assert result["sections_indexed"] > 0
    assert "knowledge_sections_fts_count_mismatch" in result["warnings"]


def test_probe_routes_bmw_to_general_automotive_repair(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "BMW F15 N63 BDC", limit=5)

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "automotive_repair"
    assert result["open_first"] == "docs/agent/automotive_repair_source_playbook.md"


def test_search_can_filter_by_domain(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = search_knowledge_base(store, "engine oil capacity", domain="fluids", limit=5)

    assert result["ok"] is True
    assert result["domain"] == "fluids"
    assert result["items"]
    assert all(item["domain"] == "fluids" for item in result["items"])


def test_search_routes_russian_oil_query_to_fluids(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = search_knowledge_base(store, "моторное масло Toyota", limit=5)

    assert result["ok"] is True
    assert result["items"][0]["domain"] == "fluids"


def test_probe_routes_dsg_software_update_to_transmission(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "DSG DQ250 обновление ПО мехатроник адаптация ODIS SVM", limit=5)

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "transmission"
    assert any("transmission_playbook" in path for path in result["source_of_truth"])


def test_search_finds_dsg_mechatronic_software_update_guidance(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = search_knowledge_base(
        store,
        "DQ200 мехатроник basic settings software update SVM ODIS",
        domain="transmission",
        limit=8,
    )

    assert result["ok"] is True
    assert any("transmission_playbook" in item["path"] for item in result["items"])


def test_probe_routes_cluster_coding_to_general_automotive_repair(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "стрелковка KOMBI BMW приборка coding", limit=5)

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "automotive_repair"
    assert result["open_first"] == "docs/agent/automotive_repair_source_playbook.md"


def test_probe_routes_priberis_to_board_cleanup_autopilot(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "Приберись CRM board_cleanup_autopilot")

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "board_cleanup_autopilot"
    assert result["open_first"] == "docs/agent/board_cleanup_autopilot_playbook.md"


def test_probe_routes_clutch_to_transmission(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "подобрать сцепление для механической коробки")

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "transmission"
    assert any("transmission" in path.lower() for path in result["routes"][0]["source_of_truth"])


def test_probe_routes_procurement_pricing_to_parts_sourcing(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "закупочная цена запчастей наличие Красноярск заказ-наряд")

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "parts_sourcing"
    assert any("parts_search_playbook" in path for path in result["routes"][0]["source_of_truth"])


def test_probe_routes_knowledge_organization_request_to_shelf_guide(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "систематизируй базу знаний разметка полки инструкции")

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "knowledge_intake"
    assert "AGENTS.md" in result["source_of_truth"]


def test_probe_routes_prepare_for_work_to_agent_entrypoint(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "подготовь менеджера к работе и почитай документацию")

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "startup_and_identity"
    assert result["open_first"] == "AGENTS.md"
    assert result.get("command_route") is None


def test_probe_routes_pdf_catalog_knowledge_update_to_intake(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "обнови базу знаний добавь PDF каталог")

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "knowledge_intake"
    assert result["open_first"] == "AGENTS.md"
    assert result["command_route"] is None


def test_probe_routes_autostop_document_without_card_to_crm_print_module(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(
        store,
        "создай счет без карточки CRM в стандартном шаблоне AutoStop PDF",
    )

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "business_documents"
    assert result["open_first"] == "docs/agent/business_document_quality_playbook.md"
    assert result["routes"][0]["required_context"] == []
    search = search_knowledge_base(
        store,
        "CRM print module create_document_without_card_pdf standard AutoStop templates",
        domain="business_documents",
        limit=5,
    )
    assert any("CRM print module" in item.get("preview", "") for item in search["items"])


def test_search_finds_parts_playbook_for_local_vendor_scoring(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = search_knowledge_base(
        store,
        "рейка Красноярск vendor discovery offer scoring call confirmation",
        domain="parts_sourcing",
        limit=8,
    )

    assert result["ok"] is True
    assert any("parts_search_playbook.md" in item["path"] for item in result["items"])
    assert not any(
        "/docs/" in item["path"] and "ai_parts_krasnoyarsk_project_pack" in item["path"] for item in result["items"]
    )


def test_probe_returns_low_confidence_for_unknown_vehicle_corpus(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "Citroen C5 Hydractive sphere pressure")

    assert result["ok"] is True
    assert result["has_knowledge"] is False
    assert result["confidence"] < 0.45
    assert result["next_action"] == "route_external_sources"


def test_audit_reports_route_cards_and_no_missing_files_after_sync(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = audit_knowledge_base(store)

    assert result["ok"] is True
    assert result["route_cards_indexed"] == result["domain_count"]
    assert result["documents_indexed"] > 0
    assert result["sections_indexed"] > 0
    assert result["sections_fts_indexed"] == result["sections_indexed"]
    assert result["missing_files"] == []
    assert set(PRIVATE_RUNTIME_FILES).issubset(set(result["optional_missing_files"]))


def test_reference_files_are_audited_but_not_fully_indexed(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    audit = audit_knowledge_base(store)
    result = search_knowledge_base(store, "automotive_repair_sources_catalog", domain="transmission", limit=20)

    assert audit["ok"] is True
    assert audit["missing_files"] == []
    assert not any(item["path"].endswith("automotive_repair_sources_catalog.json") for item in result["items"])
