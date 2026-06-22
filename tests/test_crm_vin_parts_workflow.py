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
        "quote matrix",
        "replace_repair_order_materials",
        "update_card",
        "get_card_context",
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
    assert "OEM reference" in result["crm_note_template"]
    assert "Selected parts" in result["crm_note_template"]
    assert "price_procurement" in result["pipeline"][5]["schema"]
    assert "price_public_retail" in result["pipeline"][5]["schema"]
    assert "price_client_sale" in result["pipeline"][5]["schema"]
    assert result["material_line_rule"]["write_to_materials"] == "selected part with selected price only"
    assert result["procurement_backlog_candidates"][0]["source_id"] == "rossko"
    assert result["procurement_backlog_candidates"][0]["env"]
    assert result["procurement_backlog_candidates"][0]["acceptance"]


def test_rules_forbid_hallucinated_oem_and_require_price_separation():
    rules = json.loads(_read("docs/agent/manager_rules.json"))
    combined = "\n".join(rule["rule"].casefold() for rule in rules["rules"])

    assert "never invent oem" in combined
    assert "high confidence" in combined
    assert "vin/frame-specific" in combined
    assert "oem reference" in combined
    assert "selected part" in combined
    assert "procurement price" in combined
    assert "client sale price" in combined
    assert "raw customer vin" in combined


def test_integration_backlog_names_required_catalog_cross_and_price_sources():
    vin_sources = json.loads(_read("docs/agent/vin_oem_sources.json"))
    price_sources = json.loads(_read("docs/agent/procurement_price_sources.json"))

    catalog_ids = {item["source_id"] for item in vin_sources["crm_vin_oem_parts_lookup_backlog"]}
    assert {
        "parts_catalogs_api",
        "partsapi_ru",
        "vin17_api",
        "autopoisk",
        "partsouq_manual",
        "epc_data_manual",
    }.issubset(catalog_ids)

    price_ids = {item["source_id"] for item in price_sources["crm_vin_oem_parts_pricing_backlog"]}
    assert {"rossko", "autoeuro_api", "zzap", "armtek", "autopiter", "emex", "exist", "autodoc"}.issubset(price_ids)

    for row in vin_sources["crm_vin_oem_parts_lookup_backlog"] + price_sources["crm_vin_oem_parts_pricing_backlog"]:
        assert row["role"]
        assert row["mvp_priority"] in {"high", "medium", "low"}
        assert row["acceptance"] if "acceptance" in row else row["test_vin_checks"]


def test_command_route_and_annotation_point_to_crm_vin_domain():
    command_routes = json.loads(_read("docs/agent/command_routes.json"))
    annotations = [
        json.loads(line)
        for line in _read("docs/agent/knowledge_annotations.jsonl").splitlines()
        if line.strip()
    ]

    route = next(route for route in command_routes["routes"] if route["command_id"] == "crm_vin_oem_parts_lookup")
    annotation = next(item for item in annotations if item["domain"] == "crm_vin_oem_parts_lookup")

    assert route["domain"] == "crm_vin_oem_parts_lookup"
    assert route["open_first"] == "docs/agent/crm_vin_oem_parts_lookup_playbook.md"
    assert "writeback" in " ".join(route["keywords"]).casefold()
    assert "do_not_invent_oem" in annotation["safety_flags"]
    assert "separate_procurement_retail_client_price" in annotation["safety_flags"]


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
