from __future__ import annotations

import json

import autostop_manager.knowledge_base as kb
from autostop_manager.knowledge_base import audit_knowledge_base, probe_knowledge_base, search_knowledge_base, sync_knowledge_base
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
    assert "bmw_f15_n63" in result["domains"]
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
        annotations = conn.execute(
            "SELECT COUNT(*) AS count FROM knowledge_annotations_fts WHERE knowledge_annotations_fts MATCH ?",
            ("business_identity",),
        ).fetchone()

    assert int(sections["count"]) > 0
    assert int(annotations["count"]) > 0


def test_sync_rebuilds_fast_knowledge_fts_indexes_idempotently(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    first = sync_knowledge_base(store)
    second = sync_knowledge_base(store)

    with store.connect() as conn:
        sections = conn.execute("SELECT COUNT(*) AS count FROM knowledge_sections").fetchone()
        sections_fts = conn.execute("SELECT COUNT(*) AS count FROM knowledge_sections_fts").fetchone()
        annotations = conn.execute("SELECT COUNT(*) AS count FROM knowledge_annotations").fetchone()
        annotations_fts = conn.execute("SELECT COUNT(*) AS count FROM knowledge_annotations_fts").fetchone()

    assert second["sections_indexed"] == first["sections_indexed"]
    assert int(sections_fts["count"]) == int(sections["count"])
    assert int(annotations_fts["count"]) == int(annotations["count"])


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


def test_search_finds_model_specific_route_after_sync(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = search_knowledge_base(store, "BMW F15 N63 BDC", limit=5)

    assert result["ok"] is True
    assert result["items"]
    assert result["items"][0]["domain"] == "bmw_f15_n63"
    assert result["items"][0]["path"] == "knowledge_map:bmw_f15_n63"


def test_search_can_filter_by_domain(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = search_knowledge_base(store, "engine oil capacity", domain="fluids", limit=5)

    assert result["ok"] is True
    assert result["domain"] == "fluids"
    assert result["items"]
    assert all(item["domain"] == "fluids" for item in result["items"])


def test_search_returns_route_suggestions_when_no_section_matches(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = search_knowledge_base(store, "GR Yaris G16E", limit=5)

    assert result["ok"] is True
    assert any(item["domain"] == "toyota_gr_yaris" for item in result["items"])


def test_search_routes_russian_oil_query_to_fluids(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = search_knowledge_base(store, "моторное масло Toyota", limit=5)

    assert result["ok"] is True
    assert result["items"][0]["domain"] == "fluids"


def test_sync_indexes_jsonl_rows_as_sections(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = search_knowledge_base(store, "8013FE IHKA", domain="bmw_repair", limit=5)

    assert result["ok"] is True
    assert result["items"]
    assert result["items"][0]["document_type"] == "jsonl"
    assert "8013FE" in result["items"][0]["heading"]


def test_search_routes_russian_bmw_driveline_query_to_bmw_repair(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = search_knowledge_base(store, "БМВ раздатка пинки", limit=5)

    assert result["ok"] is True
    assert result["items"][0]["domain"] == "bmw_repair"
    assert all(item["domain"] == "bmw_repair" for item in result["items"][:3])


def test_probe_routes_dsg_software_update_to_transmission(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "DSG DQ250 обновление ПО мехатроник адаптация ODIS SVM", limit=5)

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "transmission"
    assert any("dsg_transmission_playbook" in path for path in result["source_of_truth"])


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
    assert any("dsg_transmission" in item["path"] for item in result["items"])


def test_probe_routes_cluster_needle_coding_to_ecu_programming_pack(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "стрелковка KOMBI BMW приборка coding", limit=5)

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "ecu_calibration_programming"
    assert any("ecu_calibration_programming_knowledge_pack" in path for path in result["source_of_truth"])


def test_search_finds_ecu_programming_pack_kombi_content(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = search_knowledge_base(store, "KOMBI coding комбинация приборов", domain="ecu_calibration_programming", limit=5)

    assert result["ok"] is True
    assert result["items"]
    assert result["items"][0]["domain"] == "ecu_calibration_programming"
    assert any("ecu_calibration_programming_knowledge_pack" in item["path"] for item in result["items"])


def test_probe_routes_bmw_f15_n63_even_with_owner_body_code_typo(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "BMW X5 кузов E15 мотор N63 электрика")

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "bmw_f15_n63"
    assert result["routes"][0]["open_first"]
    assert any("bmw_repair" in path.replace("\\", "/") for path in result["routes"][0]["source_of_truth"])


def test_probe_routes_priberis_to_board_cleanup_autopilot(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "Приберись CRM board_cleanup_autopilot")

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "board_cleanup_autopilot"
    assert result["open_first"] == "docs/agent/board_cleanup_autopilot_playbook.md"


def test_probe_routes_toyota_gr_yaris_clutch_to_model_skill(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "подобрать сцепление Toyota Yaris GR G16E")

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "toyota_gr_yaris"
    assert any("toyota" in path.lower() for path in result["routes"][0]["source_of_truth"])


def test_probe_routes_procurement_pricing_to_parts_sourcing(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "закупочная цена запчастей наличие Красноярск заказ-наряд")

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "parts_sourcing"
    assert any("procurement_pricing_playbook" in path for path in result["routes"][0]["source_of_truth"])


def test_probe_routes_rossko_api_price_to_parts_sourcing(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "Роска Росско API закупочная цена запчастей Красноярск")

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "parts_sourcing"
    assert any("procurement_price_sources" in path for path in result["routes"][0]["source_of_truth"])


def test_probe_routes_unclear_oem_replacement_price_to_parts_sourcing(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "в заказ-наряде оригинальный номер и заменитель цена непонятна")

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "parts_sourcing"
    assert any("procurement_pricing_playbook" in path for path in result["routes"][0]["source_of_truth"])


def test_probe_routes_steering_rack_parts_request_to_ai_parts_pack(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "найти рулевую рейку в Красноярске цена наличие контрактная")

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "parts_sourcing"
    assert any("ai_parts_krasnoyarsk_project_pack" in path for path in result["source_of_truth"])


def test_probe_routes_inflected_contract_steering_rack_with_analogs_to_parts_sourcing(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "найди рулевую рейку контрактную в Красноярске и проверь аналоги")

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "parts_sourcing"
    assert any("ai_parts_krasnoyarsk_project_pack" in path for path in result["source_of_truth"])


def test_probe_routes_knowledge_organization_request_to_shelf_guide(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "систематизируй базу знаний разметка полки инструкции")

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "knowledge_intake"
    assert any("knowledge_shelves.md" in path for path in result["source_of_truth"])


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
    assert result["open_first"] == "docs/agent/knowledge_shelves.md"
    assert result["command_route"]["command_id"] == "manager_documentation_hygiene"


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
    assert any("tax_label" in item for item in result["routes"][0]["required_context"])
    search = search_knowledge_base(
        store,
        "CRM print module create_document_without_card_pdf standard AutoStop templates",
        domain="business_documents",
        limit=5,
    )
    assert any("CRM print module" in item.get("preview", "") for item in search["items"])


def test_search_finds_ai_parts_playbook_for_local_vendor_scoring(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = search_knowledge_base(
        store,
        "рейка Красноярск vendor discovery offer scoring call confirmation",
        domain="parts_sourcing",
        limit=8,
    )

    assert result["ok"] is True
    assert any("ai_parts_krasnoyarsk_playbook.md" in item["path"] for item in result["items"])
    assert not any("/docs/" in item["path"] and "ai_parts_krasnoyarsk_project_pack" in item["path"] for item in result["items"])


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
    assert result["annotations_fts_indexed"] == result["annotations_indexed"]
    assert result["missing_files"] == []
    assert set(PRIVATE_RUNTIME_FILES).issubset(set(result["optional_missing_files"]))


def test_reference_files_are_audited_but_not_fully_indexed(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    audit = audit_knowledge_base(store)
    result = search_knowledge_base(store, "data_type_source_map", domain="transmission", limit=20)

    assert audit["ok"] is True
    assert audit["missing_files"] == []
    assert not any(item["path"].endswith("data_type_source_map.json") for item in result["items"])


def test_parts_sourcing_pack_keeps_compact_manifest_without_draft_noise(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    audit = audit_knowledge_base(store)
    probe = probe_knowledge_base(store, "рейка Красноярск vendor discovery offer scoring")
    result = search_knowledge_base(store, "OpenAPI offer schema code skeleton", domain="parts_sourcing", limit=20)

    assert audit["ok"] is True
    assert audit["missing_files"] == []
    assert probe["best_domain"] == "parts_sourcing"
    assert any(item.endswith("MANIFEST.md") for item in probe["source_of_truth"])
    assert not any("openapi" in item["path"].lower() or "code_skeleton" in item["path"].lower() for item in result["items"])


def test_bmw_compacted_pack_keeps_fault_examples_searchable(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    probe = probe_knowledge_base(store, "BMW fault memory IHKA source route")
    compacted_result = search_knowledge_base(store, "public_sources_bmw_zf_nhtsa", domain="bmw_repair", limit=20)
    fault_result = search_knowledge_base(store, "8013FE IHKA", domain="bmw_repair", limit=5)

    assert probe["best_domain"] == "bmw_repair"
    assert not any(item.endswith("public_sources_bmw_zf_nhtsa.jsonl") for item in probe["reference_files"])
    assert not any(item["path"].endswith("public_sources_bmw_zf_nhtsa.jsonl") for item in compacted_result["items"])
    assert fault_result["items"][0]["document_type"] == "jsonl"
    assert "8013FE" in fault_result["items"][0]["heading"]


def test_ecu_reference_glossary_stays_linked_without_hiding_format_docs(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    probe = probe_knowledge_base(store, "A2L ODX DCM glossary ECU")
    glossary_result = search_knowledge_base(store, "glossary_ecu_programming", domain="ecu_calibration_programming", limit=20)
    format_result = search_knowledge_base(store, "A2L DCM ODX calibration format", domain="ecu_calibration_programming", limit=10)

    assert probe["best_domain"] == "ecu_calibration_programming"
    assert any(item.endswith("glossary_ecu_programming.jsonl") for item in probe["reference_files"])
    assert not any(item["path"].endswith("glossary_ecu_programming.jsonl") for item in glossary_result["items"])
    assert format_result["items"][0]["domain"] == "ecu_calibration_programming"
    assert any(
        item["path"].endswith("ecu_calibration_programming_playbook.md")
        or item["path"].endswith("data/file_format_index.csv")
        for item in format_result["items"]
    )
