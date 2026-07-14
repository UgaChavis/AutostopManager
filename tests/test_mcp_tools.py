from __future__ import annotations

import json
import inspect
from pathlib import Path

from autostop_manager import config as manager_config
import autostop_manager.mcp_tools as mcp_tools_module
from autostop_manager.mcp_tools import register_manager_memory_tools
from autostop_manager.storage import ManagerMemoryStore


ROOT = Path(__file__).resolve().parents[1]
PARTSAPI_ENV_NAMES = [
    "PARTSAPI_KEY",
    "PARTSAPI_VINDECODE_KEY",
    "PARTSAPI_VINDECODE_OE_KEY",
    "PARTSAPI_PARTS_BY_VIN_KEY",
    "PARTSAPI_OE_APPLICABILITY_KEY",
    "PARTSAPI_CROSSES_KEY",
    "PARTSAPI_CROSSES_WITH_BRAND_KEY",
    "PARTSAPI_CROSSES_TITLE_KEY",
    "PARTSAPI_ARTICLE_CROSSES_KEY",
    "PARTSAPI_SEARCH_ARTICLES_KEY",
    "PARTSAPI_GET_ENGINE_KEY",
    "PARTSAPI_SEARCH_TREE_KEY",
    "PARTSAPI_ARTICLES_KEY",
    "PARTSAPI_ARTICLE_KEY",
    "PARTSAPI_ARTICLE_CRITERIA_KEY",
    "PARTSAPI_BASE_URL",
]


def _clear_partsapi_env(monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/tmp/autostop-manager-test-empty.env")
    monkeypatch.setattr(manager_config, "_ENV_LOADED", False)
    for name in PARTSAPI_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


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


def test_decode_vehicle_identity_tool_is_registered(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "decode_vehicle_identity" in server.tools
    result = server.tools["decode_vehicle_identity"](
        "MR41S123456",
        make="Suzuki",
        model="Hustler",
        model_year=2018,
        live_vpic=False,
    )
    assert result["vehicle_profile"]["make"] == "Suzuki"
    assert result["diagnostics"]["frame_query_hint"] == "MR4***456"
    assert any(source["source_id"] == "parts_catalogs_api" for source in result["required_next_sources"])


def test_decode_vehicle_identity_tool_forwards_live_wmi_toggle(tmp_path, monkeypatch):
    captured = {}

    def fake_decode_vehicle_identity(identifier, **kwargs):
        captured["identifier"] = identifier
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(mcp_tools_module, "decode_vehicle_identity", fake_decode_vehicle_identity)
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)
    result = server.tools["decode_vehicle_identity"]("WBA00000000000000", live_vpic=False, live_wmi=False)

    assert result["ok"] is True
    assert captured["identifier"] == "WBA00000000000000"
    assert captured["live_vpic"] is False
    assert captured["live_wmi"] is False


def test_decode_vehicle_identities_tool_is_registered(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "decode_vehicle_identities" in server.tools
    result = server.tools["decode_vehicle_identities"](
        [{"identifier": "MR41S123456", "make": "Suzuki", "model": "Hustler", "model_year": 2018}],
        live_vpic=False,
    )
    assert result["count"] == 1
    assert result["medium_confidence_count"] == 1
    assert result["results"][0]["vehicle_profile"]["platform"] == "MR41S"


def test_catalog_provider_tools_are_registered(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "catalog_provider_status" in server.tools
    assert "plan_oem_parts_providers" in server.tools
    status = server.tools["catalog_provider_status"](stage="oem_catalog")
    assert status["ok"] is True
    assert any(provider["source_id"] == "parts_catalogs_api" for provider in status["providers"])
    plan = server.tools["plan_oem_parts_providers"]("MR41S123456", "колодки")
    assert plan["identifier"]["redacted"]["display"] == "MR4***456"
    assert any(step["step"] == "find_oem_candidates" for step in plan["pipeline"])


def test_control_center_and_review_tools_are_registered(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    expected = {
        "control_report",
        "memory_review",
        "memory_review_apply",
        "knowledge_intake_plan",
        "provider_smoke_report",
    }
    assert expected.issubset(server.tools)

    control = server.tools["control_report"]()
    assert control["schema"] == "ControlReportV1"
    assert control["privacy"]["secrets_redacted"] is True
    assert "server_environment" in control
    assert "codex_readiness" in control
    assert "runtime_readiness" in control
    assert "production_ops" in control
    assert control["provider_readiness"]["safety"]["orders_blocked"] is True

    review = server.tools["memory_review"]()
    assert review["schema"] == "MemoryReviewItem"

    intake = server.tools["knowledge_intake_plan"]("docs/agent/knowledge_map.json")
    assert intake["schema"] == "KnowledgeIntakeDraft"

    smoke = server.tools["provider_smoke_report"](provider="all", mode="dry-run")
    assert smoke["schema"] == "ProviderSmokeResult"
    assert smoke["summary"]["no_order_guarantee"] is True


def test_vin17_adapter_tools_are_registered(tmp_path, monkeypatch):
    monkeypatch.delenv("VIN17_ACCOUNT", raising=False)
    monkeypatch.delenv("VIN17_SECRET", raising=False)
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "vin17_decode_vehicle" in server.tools
    assert "vin17_search_part_number_by_vin" in server.tools
    decode = server.tools["vin17_decode_vehicle"]("LFMGJE720DS070251", dry_run=True)
    assert decode["ok"] is False
    assert decode["missing_env_names"] == ["VIN17_ACCOUNT", "VIN17_SECRET"]


def test_partsapi_adapter_tool_is_registered(tmp_path, monkeypatch):
    _clear_partsapi_env(monkeypatch)
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "partsapi_catalog_lookup" in server.tools
    result = server.tools["partsapi_catalog_lookup"](operation="vin_decode_oe", identifier="MR41S123456", dry_run=True)
    assert result["ok"] is False
    assert result["missing_env_names"] == ["PARTSAPI_KEY", "PARTSAPI_BASE_URL"]

    assert "search_partsapi_category_index" in server.tools
    category = server.tools["search_partsapi_category_index"]("передние колодки", intent_id="front_brake_pads")
    assert category["matches"][0]["cat_id"].isdigit()
    assert "validate_partsapi_category_index" in server.tools
    assert server.tools["validate_partsapi_category_index"]()["ok"] is True


def test_public_aftermarket_catalog_tool_is_registered(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "public_aftermarket_catalog_lookup" in server.tools
    result = server.tools["public_aftermarket_catalog_lookup"](
        provider="all",
        part_number="90919-01275",
        dry_run=True,
    )
    assert result["ok"] is True
    assert [item["provider"] for item in result["results"]] == ["mann_filter_catalog", "denso_aftermarket_catalog"]


def test_exist_price_lookup_tool_is_registered(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "exist_price_lookup" in server.tools
    result = server.tools["exist_price_lookup"](part_number="9091901164", dry_run=True)
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["benchmark_kind"] == "public_retail_reference"
    assert result["request_plan"]["office_cookie"] == "_go=905"


def test_oem_catalog_lookup_tool_is_registered(tmp_path, monkeypatch):
    _clear_partsapi_env(monkeypatch)
    monkeypatch.setenv("PARTS_CATALOGS_API_KEY", "pc-secret")
    monkeypatch.setenv("PARTS_CATALOGS_BASE_URL", "https://api.parts-catalogs.example/v1")
    monkeypatch.setenv("PARTSAPI_KEY", "partsapi-secret")
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example/api")
    monkeypatch.setenv("VIN17_ACCOUNT", "vin17-user")
    monkeypatch.setenv("VIN17_SECRET", "vin17-secret")
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "lookup_oem_catalog_candidates" in server.tools
    result = server.tools["lookup_oem_catalog_candidates"](
        identifier="JTEBU3FJX05027767",
        requested_part="передние колодки",
        catalog_id="toyota",
        car_id="car-1",
        group_id="front-brake",
        epc="toyota",
        dry_run=True,
    )
    assert result["ok"] is True
    assert result["provider_count"] == 3

    assert "resolve_vin_oem_parts" in server.tools
    resolved = server.tools["resolve_vin_oem_parts"](
        identifier="JTEBU3FJX05027767",
        requested_part="передние колодки",
        live_vpic=False,
        dry_run=True,
    )
    assert resolved["schema"] == "VinOemResolution"
    assert resolved["privacy"]["secret_exposed"] is False


def test_lookup_oem_catalog_candidates_tool_forwards_custom_category_index(tmp_path, monkeypatch):
    captured = {}

    def fake_lookup_oem_catalog_candidates(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(mcp_tools_module, "lookup_oem_catalog_candidates", fake_lookup_oem_catalog_candidates)
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)
    result = server.tools["lookup_oem_catalog_candidates"](
        identifier="JTEBU3FJX05027767",
        requested_part="передние колодки",
        partsapi_category_index_path="data/custom_partsapi_index.json",
        dry_run=True,
    )

    assert result["ok"] is True
    assert captured["partsapi_category_index_path"] == "data/custom_partsapi_index.json"
    assert captured["dry_run"] is True


def test_plan_crm_vin_oem_parts_lookup_tool_is_registered(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "plan_crm_vin_oem_parts_lookup" in server.tools
    result = server.tools["plan_crm_vin_oem_parts_lookup"](
        card_id="card_123",
        requested_part="фильтр масляный",
        frame="GXE10-0088644",
        make="Toyota",
        vehicle="Toyota Altezza",
    )
    assert result["playbook"] == "docs/agent/crm_vin_oem_parts_lookup_playbook.md"
    assert result["identifier_lookup"]["identifier"]["kind"] == "frame_number"
    assert any(step["step"] == "write_structured_result_to_crm_card" for step in result["pipeline"])


def test_benchmark_vin_parts_lookup_tool_is_registered(tmp_path, monkeypatch):
    _clear_partsapi_env(monkeypatch)
    monkeypatch.delenv("VIN17_ACCOUNT", raising=False)
    monkeypatch.delenv("VIN17_SECRET", raising=False)
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "benchmark_vin_parts_lookup" in server.tools
    result = server.tools["benchmark_vin_parts_lookup"](
        [{"identifier": "MR41S123456", "make": "Suzuki", "model": "Hustler", "model_year": 2018}],
        requested_part="передние колодки",
        live_vpic=False,
    )
    rendered = json.dumps(result, ensure_ascii=False)

    assert result["summary"]["count"] == 1
    assert result["summary"]["part_intent_recognized_count"] == 1
    assert "PARTSAPI_KEY" in result["summary"]["missing_env_names"]
    assert "MR41S123456" not in rendered
    assert "MR41S-123456" not in rendered


def test_benchmark_vin_parts_lookup_tool_forwards_dry_run_controls(tmp_path, monkeypatch):
    captured = {}

    def fake_benchmark_vin_parts_lookup(items, **kwargs):
        captured["items"] = items
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(mcp_tools_module, "benchmark_vin_parts_lookup", fake_benchmark_vin_parts_lookup)
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)
    result = server.tools["benchmark_vin_parts_lookup"](
        [{"identifier": "MR41S123456"}],
        requested_part="передние колодки",
        include_oem_catalog_dry_run=False,
        partsapi_timeout=7.5,
    )

    assert result["ok"] is True
    assert captured["include_oem_catalog_dry_run"] is False
    assert captured["partsapi_timeout"] == 7.5


def test_build_vin_parts_work_order_tool_is_registered(tmp_path, monkeypatch):
    _clear_partsapi_env(monkeypatch)
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "build_vin_parts_work_order" in server.tools
    result = server.tools["build_vin_parts_work_order"](
        [{"identifier": "WVWZZZAUZFP000000", "make": "Volkswagen", "model": "Golf", "model_year": 2014}],
        requested_part="фильтр АКПП",
        live_vpic=False,
    )

    assert result["work_order_summary"]["count"] == 1
    assert result["items"][0]["oem_lookup_routes"]["automated_first"]
    assert any(
        route["name"].startswith("partslink24")
        for route in result["items"][0]["oem_lookup_routes"]["brand_or_market_manual"]
    )


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


def test_agent_gateway_v2_tools_are_registered_and_use_compact_envelopes(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    register_manager_memory_tools(server, store)

    expected = {
        "agent_bootstrap",
        "list_agent_workflows",
        "prepare_action_contract",
        "start_workflow",
        "workflow_status",
        "workflow_transition",
        "workflow_checkpoint",
        "workflow_wait_for_external",
        "complete_external_step",
        "workflow_resume",
        "workflow_cancel",
    }
    assert expected.issubset(server.tools)

    bootstrap = server.tools["agent_bootstrap"](
        "проведи оплату в CRM",
        intent="crm_finance_operation",
    )
    assert bootstrap["format"] == "agent_envelope_v2"
    assert bootstrap["summary"]["selected_workflow"]["workflow_id"] == "crm_finance_operation"

    started = server.tools["start_workflow"](
        workflow_id="crm_finance_operation",
        intent="crm_finance_operation",
        idempotency_key="mcp-finance-v2",
        query="проведи оплату в CRM",
    )
    assert started["ok"] is True
    assert started["status"] == "planned"
    run_id = started["run_id"]
    status = server.tools["workflow_status"](run_id)
    assert status["format"] == "agent_envelope_v2"
    assert status["summary"]["idempotency_key"] == "mcp-finance-v2"

    for tool_name in {
        "workflow_transition",
        "workflow_checkpoint",
        "workflow_wait_for_external",
        "complete_external_step",
        "workflow_resume",
        "workflow_cancel",
    }:
        assert "expected_state_version" in inspect.signature(server.tools[tool_name]).parameters


def test_prepare_crm_card_action_returns_strict_write_and_verification_contract(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    assert "prepare_crm_card_action" in server.tools
    result = server.tools["prepare_crm_card_action"](
        card_id="card-123",
        expected_updated_at="2026-06-08T10:00:00+07:00",
        description="  **Важно:** проверить течь\n\n✅ Машина ждет диагностику  ",
        vehicle_profile={
            "engine_model": "N63TU",
            "autofilled_fields": ["engine_model"],
            "tentative_fields": ["engine_model"],
            "field_sources": {"engine_model": "card_description"},
            "source_summary": "Из описания карточки",
            "source_confidence": "medium",
        },
        board_summary="Проверить течь\nЖдет диагностику",
        current_card={
            "id": "card-123",
            "updated_at": "2026-06-08T10:00:00+07:00",
            "description": "старое описание",
            "vehicle_profile": {"vin": "WBAN63TEST0000001", "manual_fields": ["vin"]},
        },
        intent="board_cleanup",
        dry_run=True,
    )

    assert result["ok"] is True
    assert result["format"] == "crm_card_action_v1"
    assert result["card_id"] == "card-123"
    assert result["write_contract"]["tool"] == "update_card"
    assert result["write_contract"]["expected_updated_at"] == "2026-06-08T10:00:00+07:00"
    assert result["write_contract"]["response_mode"] == "compact"
    assert result["planned_patch"]["description"] == "  **Важно:** проверить течь\n\n✅ Машина ждет диагностику  "
    assert result["planned_patch"]["vehicle_profile"]["manual_fields"] == ["vin"]
    assert "description_exact" in result["verification_spec"]["checks"]
    assert "description_visible_text" in result["verification_spec"]["checks"]
    assert "vehicle_profile_field_level" in result["verification_spec"]["checks"]
    assert "board_summary_stale_false" in result["verification_spec"]["checks"]
    assert result["ledger_event_schema"] == [
        "pre_state_ref",
        "planned_patch",
        "write_result",
        "post_state",
        "diff",
        "verification_checks",
        "warnings",
    ]
    assert result["tool_sequence"] == [
        "start_manager_run",
        "agent_brief",
        "get_card_context",
        "prepare_crm_card_action",
        "update_card",
        "set_card_board_summary",
        "get_card_context",
        "record_manager_run_event",
        "finish_manager_run",
    ]


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
    assert "AutoStopCRM-V1 repo" in catalog["source_documents_scope"]
    assert catalog["tool_counts"]["crm_legacy_tools_hidden_by_gateway"] == 92
    assert catalog["tool_counts"]["autostop_manager_tools_in_raw_registry"] == 67
    assert catalog["tool_counts"]["production_visible_agent_gateway_v2"] == 24
    assert catalog["agent_gateway_v2"]["startup"] == "agent_bootstrap"
    assert "call_raw_capability" in catalog["agent_gateway_v2"]["raw_escape"]
    assert "manager_board_scan" in catalog["tool_families"]["manager_operations"]
    assert "bulk_set_deadline_if_below" in catalog["tool_families"]["manager_operations"]
    assert "apply_ready_unpaid_followups" in catalog["tool_families"]["manager_operations"]
    assert len(catalog["live_tools_verified"]) == 148
    assert "create_document_without_card_pdf" in catalog["tool_families"]["repair_order"]
    assert "create_document_without_card_pdf" in catalog["live_tools_verified"]
    assert "tax_label" in catalog["schema_notes"]["autostop_document_printing"]
    assert "Без НДС" in catalog["schema_notes"]["autostop_document_printing"]
    assert "prepare_crm_card_action" in catalog["live_tools_verified"]
    assert catalog["pending_local_manager_tools"] == []
    assert "estimate_repair_work_cost" in catalog["tool_families"]["optional_manager_memory_and_routing"]
    assert "decode_vehicle_identity" in catalog["tool_families"]["optional_manager_memory_and_routing"]
    assert "decode_vehicle_identities" in catalog["tool_families"]["optional_manager_memory_and_routing"]
    assert "catalog_provider_status" in catalog["tool_families"]["optional_manager_memory_and_routing"]
    assert "plan_oem_parts_providers" in catalog["tool_families"]["optional_manager_memory_and_routing"]
    assert "vin17_decode_vehicle" in catalog["tool_families"]["optional_manager_memory_and_routing"]
    assert "vin17_search_part_number_by_vin" in catalog["tool_families"]["optional_manager_memory_and_routing"]
    assert "partsapi_catalog_lookup" in catalog["tool_families"]["optional_manager_memory_and_routing"]
    assert "resolve_vin_oem_parts" in catalog["tool_families"]["optional_manager_memory_and_routing"]
    assert "search_partsapi_category_index" in catalog["tool_families"]["optional_manager_memory_and_routing"]
    assert "validate_partsapi_category_index" in catalog["tool_families"]["optional_manager_memory_and_routing"]
    assert "public_aftermarket_catalog_lookup" in catalog["tool_families"]["optional_manager_memory_and_routing"]
    assert "exist_price_lookup" in catalog["tool_families"]["optional_manager_memory_and_routing"]
    assert "plan_crm_vin_oem_parts_lookup" in catalog["tool_families"]["optional_manager_memory_and_routing"]
    assert "benchmark_vin_parts_lookup" in catalog["tool_families"]["optional_manager_memory_and_routing"]
    assert "build_vin_parts_work_order" in catalog["tool_families"]["optional_manager_memory_and_routing"]
    assert "control_report" in catalog["tool_families"]["optional_manager_memory_and_routing"]
    assert "memory_review" in catalog["tool_families"]["optional_manager_memory_and_routing"]
    assert "memory_review_apply" in catalog["tool_families"]["optional_manager_memory_and_routing"]
    assert "knowledge_intake_plan" in catalog["tool_families"]["optional_manager_memory_and_routing"]
    assert "provider_smoke_report" in catalog["tool_families"]["optional_manager_memory_and_routing"]
    assert any("decode_vehicle_identity" in note for note in catalog["operation_notes"])
    assert any("plan_crm_vin_oem_parts_lookup" in note for note in catalog["operation_notes"])
    assert any("estimate_repair_work_cost" in note for note in catalog["operation_notes"])
    assert "cleanup_card_content" in catalog["not_mcp_runtime_tools"]
