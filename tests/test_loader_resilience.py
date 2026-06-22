from __future__ import annotations

import json
import importlib

import pytest

from autostop_manager.cleanup_audit import build_cleanup_audit
from autostop_manager.service_management import build_service_management_plan
from autostop_manager.storage import ManagerMemoryStore


CASES = [
    ("autostop_manager.source_catalog", "load_source_catalog", "SOURCE_CATALOG_PATH", {"sources": [], "source_count": 0}),
    ("autostop_manager.source_catalog", "load_brand_source_map", "BRAND_SOURCE_MAP_PATH", {}),
    ("autostop_manager.source_catalog", "load_data_type_source_map", "DATA_TYPE_SOURCE_MAP_PATH", {}),
    ("autostop_manager.source_catalog", "load_open_dataset_endpoints", "OPEN_DATASET_ENDPOINTS_PATH", {"endpoints": []}),
    ("autostop_manager.vin_sources", "load_source_registry", "REGISTRY_PATH", {"version": 0, "purpose": "missing", "sources": []}),
    ("autostop_manager.fluid_maintenance", "load_fluid_source_catalog", "FLUID_SOURCE_PATH", {}),
    ("autostop_manager.service_management", "load_service_management_catalog", "SERVICE_MANAGEMENT_SOURCE_PATH", {"sources": [], "areas": {}}),
    ("autostop_manager.knowledge_intake", "_load_domains", "KNOWLEDGE_MAP_PATH", {}),
    ("autostop_manager.knowledge_base", "_load_knowledge_map", "KNOWLEDGE_MAP_PATH", {}),
    ("autostop_manager.knowledge_base", "_load_command_routes", "COMMAND_ROUTES_PATH", {"routes": []}),
]


@pytest.mark.parametrize("module_name, loader_name, path_attr, expected", CASES)
def test_json_loaders_handle_invalid_top_level_payload(tmp_path, monkeypatch, module_name, loader_name, path_attr, expected):
    module = importlib.import_module(module_name)
    loader = getattr(module, loader_name)
    if hasattr(loader, "cache_clear"):
        loader.cache_clear()

    bad_path = tmp_path / "broken.json"
    bad_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(module, path_attr, bad_path)

    if hasattr(loader, "cache_clear"):
        loader.cache_clear()

    assert loader() == expected

    if hasattr(loader, "cache_clear"):
        loader.cache_clear()


def test_knowledge_base_string_list_fields_do_not_char_split(tmp_path, monkeypatch):
    module = importlib.import_module("autostop_manager.knowledge_base")
    docs_agent = tmp_path / "repo" / "docs" / "agent"
    docs_agent.mkdir(parents=True)
    demo_path = docs_agent / "demo.md"
    reference_path = docs_agent / "reference.md"
    demo_path.write_text("# Demo\n", encoding="utf-8")
    reference_path.write_text("# Reference\n", encoding="utf-8")
    knowledge_map_path = docs_agent / "knowledge_map.json"
    knowledge_map_path.write_text(
        json.dumps(
            {
                "domains": {
                    "demo_domain": {
                        "title": "Demo Domain",
                        "use_when": "when demo",
                        "aliases": "demo alias",
                        "keywords": "demo keyword",
                        "questions": "demo question",
                        "source_of_truth_files": str(demo_path),
                        "primary_files": str(demo_path),
                        "reference_files": str(reference_path),
                        "required_context": "demo context",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "KNOWLEDGE_MAP_PATH", knowledge_map_path)

    store = ManagerMemoryStore(tmp_path / "repo" / "memory.sqlite3")
    result = module.sync_knowledge_base(store)
    audit = module.audit_knowledge_base(store)

    assert result["ok"] is True
    assert result["route_cards_indexed"] == 1
    assert result["missing_files"] == []
    assert audit["ok"] is True
    assert audit["missing_files"] == []


def test_command_routes_string_lists_are_normalized(tmp_path, monkeypatch):
    module = importlib.import_module("autostop_manager.knowledge_base")
    command_routes_path = tmp_path / "command_routes.json"
    command_routes_path.write_text(
        json.dumps(
            {
                "routes": [
                    {
                        "command_id": "demo_route",
                        "intent": "demo_intent",
                        "domain": "demo_domain",
                        "open_first": "docs/agent/demo.md",
                        "aliases": "demo alias",
                        "keywords": "demo keyword",
                        "memory_queries": "board cleanup",
                        "next_actions": "open source",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "COMMAND_ROUTES_PATH", command_routes_path)
    module._load_command_routes.cache_clear()

    route = module.find_command_route("demo alias")

    assert route is not None
    assert route["memory_queries"] == ["board cleanup"]
    assert route["next_actions"] == ["open source"]
    assert route["aliases"] == ["demo alias"]
    assert route["keywords"] == ["demo keyword"]

    module._load_command_routes.cache_clear()


def test_skill_registry_string_lists_are_normalized(tmp_path, monkeypatch):
    module = importlib.import_module("autostop_manager.skill_registry")
    knowledge_map_path = tmp_path / "knowledge_map.json"
    skill_path = tmp_path / "skills" / "demo" / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text("# Demo\n", encoding="utf-8")
    knowledge_map_path.write_text(
        json.dumps(
            {
                "domains": {
                    "demo_domain": {
                        "source_of_truth_files": str(skill_path),
                        "primary_files": str(skill_path),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "KNOWLEDGE_MAP_PATH", knowledge_map_path)

    registry = module.load_skill_registry(skill_root=tmp_path / "missing-skills")

    assert registry["ok"] is True
    assert registry["skills"]
    assert registry["skills"][0]["skill_id"] == "demo"
    assert registry["skills"][0]["path"] == str(skill_path)


def test_service_management_string_lists_are_normalized(tmp_path, monkeypatch):
    module = importlib.import_module("autostop_manager.service_management")
    catalog_path = tmp_path / "service_management_sources.json"
    catalog_path.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "source_id": "demo",
                        "name": "Demo Source",
                        "city_focus": "Красноярск",
                    }
                ],
                "areas": {
                    "daily_control": {
                        "source_ids": "demo",
                        "required_context": "crm_board_state",
                        "actions": "check board",
                        "crm_tools": "today_context",
                        "kpis": "uptime",
                        "memory_rules": "store rule",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "SERVICE_MANAGEMENT_SOURCE_PATH", catalog_path)
    module.load_service_management_catalog.cache_clear()

    result = build_service_management_plan(area="daily_control", city="Красноярск")

    assert result["ok"] is True
    assert result["required_context"] == ["crm_board_state"]
    assert result["actions"] == ["check board"]
    assert result["crm_tools"] == ["today_context"]
    assert result["kpis"] == ["uptime"]
    assert result["memory_rules"] == ["store rule"]
    assert any(source["source_id"] == "demo" for source in result["sources"])

    module.load_service_management_catalog.cache_clear()


def test_knowledge_intake_string_lists_are_normalized(tmp_path, monkeypatch):
    module = importlib.import_module("autostop_manager.knowledge_intake")
    knowledge_map_path = tmp_path / "knowledge_map.json"
    source_path = tmp_path / "notes.md"
    source_path.write_text("demo intake keyword", encoding="utf-8")
    knowledge_map_path.write_text(
        json.dumps(
            {
                "domains": {
                    "demo_domain": {
                        "title": "Demo Domain",
                        "aliases": "demo intake",
                        "keywords": "demo intake keyword",
                        "source_of_truth_files": "docs/agent/demo.md",
                        "primary_files": "docs/agent/demo.md",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "KNOWLEDGE_MAP_PATH", knowledge_map_path)

    plan = module.build_knowledge_intake_plan(source_path, project_root=tmp_path)

    assert plan["ok"] is True
    assert plan["domain"] == "demo_domain"


def test_vin_sources_inputs_and_backlogs_are_normalized(tmp_path, monkeypatch):
    vin_module = importlib.import_module("autostop_manager.vin_sources")
    crm_module = importlib.import_module("autostop_manager.crm_vin_parts")

    registry_path = tmp_path / "vin_oem_sources.json"
    registry_path.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "name": "Demo VIN Source",
                        "inputs": "vin",
                    }
                ],
                "integration_backlog": "backlog item",
            }
        ),
        encoding="utf-8",
    )
    procurement_path = tmp_path / "procurement_price_sources.json"
    procurement_path.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "source_id": "demo",
                        "name": "Demo Procurement Source",
                        "mvp_priority": "high",
                    }
                ],
                "integration_backlog": "procurement backlog",
                "crm_vin_oem_parts_pricing_backlog": [{"source_id": "demo", "mvp_priority": "high"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(vin_module, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(crm_module, "VIN_OEM_SOURCES_PATH", registry_path)
    monkeypatch.setattr(crm_module, "PROCUREMENT_SOURCES_PATH", procurement_path)
    vin_module.load_source_registry.cache_clear()
    crm_module._load_vin_oem_sources.cache_clear()
    crm_module._load_procurement_sources.cache_clear()

    assert len(vin_module.sources_for_inputs("vin")) == 1
    assert vin_module.sources_for_inputs("vin")[0]["name"] == "Demo VIN Source"

    vin_oem_registry = crm_module._load_vin_oem_sources()
    procurement_registry = crm_module._load_procurement_sources()

    assert vin_oem_registry["integration_backlog"] == ["backlog item"]
    assert procurement_registry["integration_backlog"] == ["procurement backlog"]
    assert procurement_registry["crm_vin_oem_parts_pricing_backlog"] == [{"source_id": "demo", "mvp_priority": "high"}]

    vin_module.load_source_registry.cache_clear()
    crm_module._load_vin_oem_sources.cache_clear()
    crm_module._load_procurement_sources.cache_clear()


def test_cleanup_audit_string_route_lists_keep_referenced_docs(tmp_path):
    root = tmp_path / "repo"
    docs_agent = root / "docs" / "agent"
    docs_agent.mkdir(parents=True)
    (docs_agent / "known.md").write_text("# Known\n", encoding="utf-8")
    (docs_agent / "unused.md").write_text("# Unused\n", encoding="utf-8")
    (docs_agent / "knowledge_annotations.jsonl").write_text("", encoding="utf-8")
    (docs_agent / "knowledge_map.json").write_text(
        json.dumps(
            {
                "domains": {
                    "demo_domain": {
                        "primary_files": "docs/agent/known.md",
                        "source_of_truth_files": "docs/agent/known.md",
                        "reference_files": "docs/agent/known.md",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = build_cleanup_audit(project_root=root, store=ManagerMemoryStore(root / "data.sqlite3"))

    assert result["ok"] is True
    assert not any(item["path"] == "docs/agent/known.md" for item in result["candidates"])
    assert any(item["path"] == "docs/agent/unused.md" for item in result["candidates"])
