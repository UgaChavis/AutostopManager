from __future__ import annotations

from autostop_manager.knowledge_base import audit_knowledge_base, probe_knowledge_base, search_knowledge_base, sync_knowledge_base
from autostop_manager.storage import ManagerMemoryStore


def test_sync_indexes_domains_documents_and_sections(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    result = sync_knowledge_base(store)

    assert result["ok"] is True
    assert result["documents_indexed"] > 0
    assert result["sections_indexed"] > 0
    assert "bmw_f15_n63" in result["domains"]


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

    result = probe_knowledge_base(store, "Приберись уборка доски CRM board cleanup autopilot")

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


def test_probe_routes_knowledge_organization_request_to_shelf_guide(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = probe_knowledge_base(store, "систематизируй базу знаний разметка полки инструкции")

    assert result["ok"] is True
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "knowledge_intake"
    assert any("knowledge_shelves.md" in path for path in result["source_of_truth"])


def test_search_finds_ai_parts_pack_for_local_vendor_scoring(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    result = search_knowledge_base(
        store,
        "рейка Красноярск vendor discovery offer scoring call confirmation",
        domain="parts_sourcing",
        limit=8,
    )

    assert result["ok"] is True
    assert any("ai_parts_krasnoyarsk_project_pack" in item["path"] for item in result["items"])


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
    assert result["missing_files"] == []
