from __future__ import annotations

import json
from pathlib import Path

import autostop_manager.crm_vin_parts as crm_vin_parts
from autostop_manager.crm_vin_parts import build_crm_vin_parts_lookup_pipeline
from autostop_manager.knowledge_base import audit_knowledge_base, probe_knowledge_base, sync_knowledge_base
from autostop_manager.storage import ManagerMemoryStore


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = ROOT / "docs" / "agent" / "crm_vin_oem_parts_lookup_playbook.md"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_crm_vin_oem_parts_playbook_is_indexed_and_routes(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    sync = sync_knowledge_base(store)
    result = probe_knowledge_base(store, "в карточке CRM VIN найти OEM свечей аналоги закупка записать", limit=5)
    audit = audit_knowledge_base(store)

    assert "crm_vin_oem_parts_lookup" in sync["domains"]
    assert result["has_knowledge"] is True
    assert result["best_domain"] == "crm_vin_oem_parts_lookup"
    assert result["open_first"] == "docs/agent/crm_vin_oem_parts_lookup_playbook.md"
    assert audit["ok"] is True
    assert audit["missing_files"] == []


def test_playbook_covers_identifier_markets_confidence_and_crm_writeback():
    text = PLAYBOOK.read_text(encoding="utf-8").casefold()

    for fragment in [
        "17-character vin",
        "japanese frame",
        "body number",
        "korean / kdm vin",
        "do not invent oem",
        "high confidence",
        "source",
        "procurement price",
        "public retail/market price",
        "client sale price",
        "oem reference",
        "selected part",
        "internal quote matrix",
        "agent_finance_workflow",
        "agent_board_workflow",
        "agent_entity_context",
    ]:
        assert fragment in text


def test_pipeline_returns_crm_output_format_and_frame_workflow():
    result = build_crm_vin_parts_lookup_pipeline(
        card_id="card_123",
        requested_part="колодки передние",
        frame="GXE10-0088644",
        make="Toyota",
        vehicle="Toyota Altezza",
        market="Japan",
        axle="front",
        city="Красноярск",
    )

    assert result["ok"] is True
    assert result["missing_context"] == []
    assert result["identifier_source"] == "frame"
    assert result["identifier_lookup"]["identifier"]["kind"] == "frame_number"
    assert result["requested_part_profile"]["intent_id"] == "front_brake_pads"
    assert "decode_vehicle_identity" in result["pipeline"][1]["manager_tools"]
    assert result["vehicle_identity"]["identifier"]["kind"] == "frame_number"
    assert result["provider_plan"]["live_capability"]["can_complete_full_auto_lookup_now"] is False
    assert any(step["step"] == "write_structured_result_to_crm_card" for step in result["pipeline"])
    assert any(step["step"] == "reopen_and_verify_crm_write" for step in result["pipeline"])
    assert any(step["step"] == "prepare_card_write_contract" for step in result["pipeline"])
    rendered = json.dumps(result, ensure_ascii=False)
    assert "GXE10-0088644" not in rendered
    assert "GXE100088644" not in rendered
    assert result["context"]["frame"] == "GXE***644"
    assert "OEM reference" in result["crm_note_template"]
    assert "Selected parts" in result["crm_note_template"]
    assert "не текст CRM-карточки" in result["crm_note_template"]
    write_step = next(step for step in result["pipeline"] if step["step"] == "write_structured_result_to_crm_card")
    assert any("quote matrix, sources, confidence" in rule for rule in write_step["rules"])
    assert "price_procurement" in result["pipeline"][5]["schema"]
    assert "price_public_retail" in result["pipeline"][5]["schema"]
    assert "price_client_sale" in result["pipeline"][5]["schema"]
    assert result["material_line_rule"]["write_to_materials"] == "selected part with selected price only"
    assert result["procurement_backlog_candidates"][0]["source_id"] == "rossko"
    assert result["procurement_backlog_candidates"][0]["env"]
    assert result["procurement_backlog_candidates"][0]["acceptance"]


def test_pipeline_redacts_raw_identifier_from_public_output():
    raw_vin = "JTEBU3FJX05027767"

    result = build_crm_vin_parts_lookup_pipeline(
        card_id="card_123",
        requested_part="колодки передние",
        vin=raw_vin,
        make="Toyota",
    )

    rendered = json.dumps(result, ensure_ascii=False)
    assert raw_vin not in rendered
    assert result["privacy"]["raw_identifier_redacted_from_output"] is True
    assert result["context"]["vin"] == "JTE***767"
    assert result["context"]["identifier"]["redacted"]["display"] == "JTE***767"
    assert "raw" not in result["identifier_lookup"]["identifier"]
    assert "normalized" not in result["identifier_lookup"]["identifier"]
    assert result["identifier_lookup"]["identifier"]["redacted"]["display"] == "JTE***767"
    assert result["vehicle_identity"]["identifier"]["redacted"]["display"] == "JTE***767"


def test_manual_writeback_package_respects_gate_and_selected_candidate_id():
    resolution = {
        "status": "oem_candidates_found_needs_manual_confirmation",
        "oem_candidates": [
            {"candidate_id": "oem-1", "part_number": "111", "confidence_label": "medium"},
            {"candidate_id": "oem-2", "part_number": "222", "confidence_label": "high"},
        ],
        "crm_writeback_gate": {
            "can_prepare_manual_writeback": True,
            "selected_candidate_id": "oem-2",
        },
    }

    result = build_crm_vin_parts_lookup_pipeline(
        card_id="card_123",
        requested_part="колодки передние",
        vin_oem_resolution=resolution,
    )

    package = result["manual_writeback_package"]
    assert package["can_prepare_manual_writeback"] is True
    assert package["selected_candidate"]["candidate_id"] == "oem-2"
    assert package["confidence"] == "high"
    assert [candidate["candidate_id"] for candidate in package["rejected_candidates"]] == ["oem-1"]


def test_manual_writeback_package_blocks_when_resolution_gate_disallows_prepare():
    resolution = {
        "status": "blocked",
        "oem_candidates": [{"candidate_id": "oem-1", "part_number": "111", "confidence_label": "medium"}],
        "crm_writeback_gate": {
            "can_prepare_manual_writeback": False,
            "selected_candidate_id": "oem-1",
        },
    }

    result = build_crm_vin_parts_lookup_pipeline(
        card_id="card_123",
        requested_part="колодки передние",
        vin_oem_resolution=resolution,
    )

    package = result["manual_writeback_package"]
    assert package["can_prepare_manual_writeback"] is False
    assert package["selected_candidate"] is None
    assert package["confidence"] == "blocked"


def test_pipeline_requires_v2_action_contract_before_card_writeback():
    result = build_crm_vin_parts_lookup_pipeline(
        card_id="card_123",
        requested_part="колодки передние",
        frame="GXE10-0088644",
        make="Toyota",
    )

    prepare_index = next(
        index for index, step in enumerate(result["pipeline"]) if step["step"] == "prepare_card_write_contract"
    )
    write_index = next(
        index for index, step in enumerate(result["pipeline"]) if step["step"] == "write_structured_result_to_crm_card"
    )

    assert prepare_index < write_index
    assert "prepare_action_contract" in result["pipeline"][prepare_index]["manager_tools"]
    assert any("expected_updated_at" in check for check in result["pipeline"][prepare_index]["checks"])
    assert any("prepare_action_contract" in rule for rule in result["pipeline"][write_index]["rules"])


def test_domain_playbook_forbids_hallucinated_oem_and_separates_prices():
    combined = _read("docs/agent/crm_vin_oem_parts_lookup_playbook.md").casefold()

    assert "do not invent oem" in combined
    assert "high confidence" in combined
    assert "vin/frame-specific" in combined
    assert "oem reference" in combined
    assert "selected part" in combined
    assert "procurement price" in combined
    assert "client sale price" in combined
    assert "raw customer vin" in combined


def test_provider_registries_name_required_catalog_cross_and_price_sources():
    vin_sources = json.loads(_read("docs/agent/vin_oem_sources.json"))
    price_sources = json.loads(_read("docs/agent/procurement_price_sources.json"))

    catalog_rows = [item for item in vin_sources["sources"] if item.get("mvp_priority")]
    catalog_ids = {item["source_id"] for item in catalog_rows}
    assert {
        "partsapi_ru",
        "vin17_api",
        "autopoisk",
        "partsouq_manual",
        "epc_data_manual",
    }.issubset(catalog_ids)

    price_rows = [item for item in price_sources["sources"] if item.get("integration_priority")]
    price_ids = {item["source_id"] for item in price_rows}
    assert {"rossko", "autoeuro_api", "zzap", "armtek", "autopiter", "exist", "autodoc"}.issubset(price_ids)

    for row in catalog_rows:
        assert row["role"]
        assert row["mvp_priority"] in {"high", "medium", "low"}
    for row in price_rows:
        assert row["integration_priority"] in {"high", "medium", "low"}
        assert row["verification"]

    assert "crm_vin_oem_parts_lookup_backlog" not in vin_sources
    assert "crm_vin_oem_parts_pricing_backlog" not in price_sources
    vin_source_ids = [item["source_id"] for item in vin_sources["sources"]]
    price_source_ids = [item["source_id"] for item in price_sources["sources"]]
    assert len(vin_source_ids) == len(set(vin_source_ids))
    assert len(price_source_ids) == len(set(price_source_ids))
    assert all(step["status"] for step in price_sources["integration_next_steps"])


def test_command_route_and_knowledge_map_point_to_crm_vin_domain():
    command_routes = json.loads(_read("docs/agent/command_routes.json"))
    knowledge_map = json.loads(_read("docs/agent/knowledge_map.json"))

    route = next(route for route in command_routes["routes"] if route["command_id"] == "crm_vin_oem_parts_lookup")
    domain = knowledge_map["domains"]["crm_vin_oem_parts_lookup"]

    assert route["knowledge_domains"][0] == "crm_vin_oem_parts_lookup"
    assert route["effects"] == ["crm_write"]
    assert "writeback" in str(route["signals"]).casefold()
    assert domain["primary_files"][0] == "docs/agent/crm_vin_oem_parts_lookup_playbook.md"


def test_pipeline_reports_invalid_source_registry_payloads(tmp_path, monkeypatch):
    vin_sources_path = tmp_path / "vin_oem_sources.json"
    procurement_sources_path = tmp_path / "procurement_price_sources.json"
    vin_sources_path.write_text("[]", encoding="utf-8")
    procurement_sources_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(crm_vin_parts, "VIN_OEM_SOURCES_PATH", vin_sources_path)
    monkeypatch.setattr(crm_vin_parts, "PROCUREMENT_SOURCES_PATH", procurement_sources_path)
    crm_vin_parts._load_vin_oem_sources.cache_clear()
    crm_vin_parts._load_procurement_sources.cache_clear()

    result = build_crm_vin_parts_lookup_pipeline(
        card_id="card_123",
        requested_part="колодки передние",
        vin="JTEBU3FJX05027767",
        make="Toyota",
    )

    assert result["ok"] is True
    assert any(
        warning.startswith("vin_oem_sources:vin_oem_sources_invalid_structure")
        for warning in result["source_registry_warnings"]
    )
    assert any(
        warning.startswith("procurement_sources:procurement_sources_invalid_structure")
        for warning in result["source_registry_warnings"]
    )
    assert result["catalog_backlog_candidates"] == []
    assert result["procurement_backlog_candidates"] == []
