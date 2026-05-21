from __future__ import annotations

import json
from pathlib import Path

from autostop_manager.mcp_tools import register_manager_memory_tools
from autostop_manager.storage import ManagerMemoryStore


ROOT = Path(__file__).resolve().parents[1]


class _FakeServer:
    def __init__(self):
        self.tools = {}

    def tool(self, name: str, description: str = ""):
        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


def test_lookup_original_parts_tool_is_registered(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "lookup_original_parts" in server.tools
    result = server.tools["lookup_original_parts"]("GXE10-0088644")
    assert result["identifier"]["kind"] == "frame_number"
    assert result["steps"]
    assert result["catalog_routes"] == result["steps"]
    assert "oem_candidates" in result
    assert "fitment_confidence" in result

    captured = server.tools["lookup_original_parts"](
        "GXE10-0088644",
        make_hint="Toyota",
        part_name="сальник",
        captured_oem_number="90311-89014",
        captured_source="Toyota EPC Mirror",
    )
    assert captured["oem_candidates"][0]["normalized_number"] == "9031189014"


def test_recommend_automotive_sources_tool_is_registered(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "recommend_automotive_sources" in server.tools
    result = server.tools["recommend_automotive_sources"](brand="Toyota", data_type="repair_manuals")
    assert result["ok"] is True
    assert result["sources"]
    assert any(source["source_id"] == "toyota_tis_na" for source in result["sources"])


def test_recommend_fluid_maintenance_sources_tool_is_registered(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "recommend_fluid_maintenance_sources" in server.tools
    result = server.tools["recommend_fluid_maintenance_sources"](
        brand="Toyota",
        unit="engine_oil",
        year=2019,
        model="Camry",
        engine_code="A25A-FKS",
        market="Russia",
    )
    assert result["ok"] is True
    assert result["unit"] == "engine_oil"
    assert result["lubricant_product_selectors"]


def test_recommend_service_management_actions_tool_is_registered(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "recommend_service_management_actions" in server.tools
    result = server.tools["recommend_service_management_actions"](
        area="parts",
        city="Красноярск",
        vehicle="Lexus RX200T",
        part_number="90311-89014",
        urgency="today",
    )
    assert result["ok"] is True
    assert result["area"] == "parts_procurement"
    assert any(source["source_id"] == "drom_parts" for source in result["sources"])


def test_estimate_repair_work_cost_tool_is_registered(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "estimate_repair_work_cost" in server.tools
    result = server.tools["estimate_repair_work_cost"](
        vehicle="BMW X5",
        vin="WBA00000000000000",
        work_items=["замена рулевой рейки"],
        quotes_json=[
            {
                "source": "sto-a",
                "city": "Москва",
                "operation_name": "замена рулевой рейки",
                "price_rub": 10000,
                "includes_parts": False,
                "captured_at": "2026-05-21",
            },
            {
                "source": "sto-b",
                "city": "Красноярск",
                "operation_name": "рулевая рейка снять/поставить",
                "price_rub": 12000,
                "includes_parts": False,
                "captured_at": "2026-05-21",
            },
            {
                "source": "sto-c",
                "city": "Новосибирск",
                "operation_name": "поменять рейку",
                "price_rub": 11000,
                "includes_parts": False,
                "captured_at": "2026-05-21",
            },
        ],
    )
    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["russia_average_rub"] == 11000
    assert result["autostop_price_rub"] == 16500
    assert "labor_time_analysis" in result
    assert "pricing_basis" in result


def test_knowledge_base_tools_are_registered(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "sync_knowledge_base" in server.tools
    assert "probe_knowledge_base" in server.tools
    assert "search_knowledge_base" in server.tools
    assert "audit_knowledge_base" in server.tools

    sync_result = server.tools["sync_knowledge_base"]()
    assert sync_result["ok"] is True

    probe_result = server.tools["probe_knowledge_base"]("Toyota Yaris GR clutch", limit=3)
    assert probe_result["ok"] is True
    assert probe_result["best_domain"] == "toyota_gr_yaris"

    search_result = server.tools["search_knowledge_base"]("BMW F15 N63", domain="bmw_f15_n63", limit=5)
    assert search_result["ok"] is True
    assert search_result["items"]

    audit_result = server.tools["audit_knowledge_base"]()
    assert audit_result["ok"] is True
    assert "audit_knowledge_annotations" in server.tools
    annotation_result = server.tools["audit_knowledge_annotations"]()
    assert annotation_result["ok"] is True
    assert annotation_result["annotations_indexed"] > 0


def test_manager_mcp_catalog_matches_registered_tools(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    catalog = json.loads((ROOT / "docs/agent/manager_mcp_catalog.json").read_text(encoding="utf-8"))
    assert catalog["tool_count"] == len(server.tools)
    assert set(catalog["all_tools"]) == set(server.tools)


def test_manager_context_skill_and_run_tools_are_registered(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "prepare_manager_context" in server.tools
    context = server.tools["prepare_manager_context"]("Приберись", intent="board_cleanup", limit=5)
    assert context["ok"] is True
    assert context["command_route"]["command_id"] == "board_cleanup_autopilot"

    assert "agent_brief" in server.tools
    brief = server.tools["agent_brief"]("Приберись", intent="board_cleanup", limit=5)
    assert brief["ok"] is True
    assert brief["format"] == "agent_brief_v1"
    assert brief["route"]["domain"] == "board_cleanup_autopilot"
    assert "set_card_board_summary" in brief["allowed_actions"]

    assert "audit_skill_registry" in server.tools
    skills = server.tools["audit_skill_registry"]()
    assert skills["ok"] is True

    assert "cleanup_audit" in server.tools
    assert "system_audit" in server.tools
    assert "crm_health_plan" in server.tools
    cleanup = server.tools["cleanup_audit"]()
    assert cleanup["ok"] is True
    assert cleanup["mode"] == "dry_run"
    system = server.tools["system_audit"]()
    assert system["ok"] is True
    crm_health = server.tools["crm_health_plan"](
        board_review={
            "by_column": [{"column_id": "column_2", "label": "Запись на ремонт", "count": 10}],
            "recent_events": [{"actor_name": "Codex MCP QA", "type": "test"}],
        }
    )
    assert crm_health["mode"] == "read_only"
    assert crm_health["verification"]["cards_moved"] == 0

    assert "start_manager_run" in server.tools
    assert "record_manager_run_event" in server.tools
    assert "finish_manager_run" in server.tools
    assert "list_manager_runs" in server.tools

    started = server.tools["start_manager_run"](intent="board_cleanup", query="Приберись", dry_run=True)
    server.tools["record_manager_run_event"](started["id"], event_type="planned_action", message="No card moves")
    server.tools["finish_manager_run"](started["id"], status="completed", verification={"cards_moved": 0})
    runs = server.tools["list_manager_runs"](limit=3, include_events=True)

    assert runs["items"][0]["id"] == started["id"]
    assert runs["items"][0]["events"]


def test_memory_curator_tools_are_registered(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.remember("Duplicate operational memory", tags=["ops"])
    store.remember("Duplicate operational memory", tags=["ops"])

    register_manager_memory_tools(server, store)

    assert "audit_memory" in server.tools
    audit = server.tools["audit_memory"]()
    assert audit["ok"] is True
    assert audit["duplicates"]

    assert "curate_memory" in server.tools
    curated = server.tools["curate_memory"](apply=True)
    assert curated["ok"] is True
    assert curated["archived_duplicates"]


def test_memory_tools_support_relevance_filters_and_confidence(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    created = server.tools["remember"](
        "В карточках писать живым человеческим языком.",
        kind="fact",
        category="style",
        tags=["карточки"],
        confidence=0.9,
    )
    assert created["confidence"] == 0.9

    result = server.tools["recall"]("живым языком", kind="fact", category="style", tags=["карточки"])

    assert result["ok"] is True
    assert result["total_matches"] == 1
    assert result["items"][0]["kind"] == "fact"
    assert result["items"][0]["score"] > 0


def test_learning_and_navigation_tools_are_registered(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    for name in [
        "learn_from_feedback",
        "recall_lessons",
        "memory_map",
        "memory_topics",
        "memory_context_for",
        "memory_gaps",
    ]:
        assert name in server.tools

    lesson = server.tools["learn_from_feedback"](
        "Писать карточки живее",
        applies_to="crm_cleanup",
        signal="owner_praise",
        recommendation="Оставлять короткий человеческий следующий шаг.",
        avoid="Не писать длинный шаблон.",
        tags=["карточки"],
    )
    assert lesson["kind"] == "lesson"

    assert server.tools["recall_lessons"]("человеческий", applies_to="crm_cleanup")["items"]
    assert server.tools["memory_map"]()["sections"]["lessons"]["count"] == 1
    assert server.tools["memory_topics"]()["tags"]["карточки"]["count"] == 1
    assert server.tools["memory_context_for"]("crm карточки")["lessons"]
    assert "empty_sections" in server.tools["memory_gaps"]()


def test_crm_mcp_catalog_counts_are_current():
    catalog = json.loads((ROOT / "docs/agent/crm_mcp_catalog.json").read_text(encoding="utf-8"))

    assert catalog["source_branch"] == "autostopcrm-v1"
    assert catalog["tool_counts"]["crm_base_tools"] == 71
    assert catalog["tool_counts"]["optional_autostop_manager_tools"] == 33
    assert catalog["tool_counts"]["production_tools_with_manager_mounted"] == 104
    assert len(catalog["live_tools_verified"]) == 104
    assert "cleanup_card_content" in catalog["not_mcp_runtime_tools"]
