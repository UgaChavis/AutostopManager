from __future__ import annotations

import json
import inspect
from pathlib import Path

from autostop_manager import config as manager_config
import autostop_manager.mcp_tools as mcp_tools_module
from autostop_manager.mcp_server import build_server
from autostop_manager.mcp_tools import register_manager_memory_tools
from autostop_manager.storage import ManagerMemoryStore
from autostop_manager.system_audit import _mcp_schema_fingerprint


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
        self.descriptions = {}
        self.options = {}

    def tool(self, name: str, description: str = "", **kwargs):
        def decorator(func):
            self.tools[name] = func
            self.descriptions[name] = description
            self.options[name] = kwargs
            return func

        return decorator


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


def test_vehicle_and_catalog_reads_have_read_only_annotations(tmp_path):
    server = _FakeServer()
    register_manager_memory_tools(server, ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    for name in ("decode_vehicle_identity", "partsapi_catalog_lookup", "lookup_oem_catalog_candidates"):
        annotations = server.options[name]["annotations"]
        assert annotations.readOnlyHint is True
        assert annotations.destructiveHint is False


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


def test_oem_lookup_compatibility_names_use_canonical_resolver(tmp_path):
    server = _FakeServer()
    register_manager_memory_tools(server, ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    canonical = server.tools["resolve_vin_oem_parts"]
    for name in ("lookup_oem_catalog_candidates", "plan_crm_vin_oem_parts_lookup"):
        assert server.tools[name] is canonical
        assert inspect.signature(server.tools[name]) == inspect.signature(canonical)

    live_tools = build_server()._tool_manager._tools
    for name in ("lookup_oem_catalog_candidates", "plan_crm_vin_oem_parts_lookup"):
        assert live_tools[name].parameters == live_tools["resolve_vin_oem_parts"].parameters


def test_fluid_source_name_is_general_source_router_alias(tmp_path):
    server = _FakeServer()
    register_manager_memory_tools(server, ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    alias = server.tools["recommend_fluid_maintenance_sources"]
    canonical = server.tools["recommend_automotive_sources"]
    assert alias is canonical
    assert inspect.signature(alias) == inspect.signature(canonical)
    assert inspect.signature(alias).parameters["data_type"].default is None
    live_tools = build_server()._tool_manager._tools
    assert (
        live_tools["recommend_fluid_maintenance_sources"].parameters
        == live_tools["recommend_automotive_sources"].parameters
    )


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


def test_benchmark_vin_parts_lookup_tool_forwards_timeout(tmp_path, monkeypatch):
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
        partsapi_timeout=7.5,
    )

    assert result["ok"] is True
    assert captured["partsapi_timeout"] == 7.5


def test_lookup_public_automotive_evidence_tool_is_registered(tmp_path, monkeypatch):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    monkeypatch.setattr(
        mcp_tools_module,
        "lookup_public_automotive_evidence",
        lambda **kwargs: {"ok": True, "input_context": kwargs, "evidence": []},
    )

    register_manager_memory_tools(server, store)

    assert "lookup_public_automotive_evidence" in server.tools
    result = server.tools["lookup_public_automotive_evidence"](
        make="Mercedes-Benz",
        model="C-Class",
        model_year=2020,
        topics=["recalls"],
    )
    assert result["ok"] is True
    assert result["input_context"]["make"] == "Mercedes-Benz"
    assert result["input_context"]["topics"] == ["recalls"]


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

    probe_result = server.tools["probe_knowledge_base"]("clutch gearbox", limit=3)
    assert probe_result["ok"] is True
    assert probe_result["best_domain"] == "automotive_repair"

    search_result = server.tools["search_knowledge_base"]("BMW F15 N63", domain="automotive_repair", limit=5)
    assert search_result["ok"] is True
    assert search_result["items"]

    audit_result = server.tools["audit_knowledge_base"]()
    assert audit_result["ok"] is True
    assert "audit_knowledge_annotations" in server.tools
    annotation_result = server.tools["audit_knowledge_annotations"]()
    assert annotation_result["ok"] is True
    assert not (ROOT / "docs/agent/knowledge_annotations.jsonl").exists()


def test_selective_registration_keeps_only_requested_tools(tmp_path):
    server = _FakeServer()

    register_manager_memory_tools(
        server,
        ManagerMemoryStore(tmp_path / "memory.sqlite3"),
        include_tools={"agent_bootstrap", "store_runtime_status"},
    )

    assert set(server.tools) == {"agent_bootstrap", "store_runtime_status"}


def test_manager_mcp_catalog_matches_registered_tools(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    register_manager_memory_tools(server, store)

    catalog = json.loads((ROOT / "docs/agent/manager_mcp_catalog.json").read_text(encoding="utf-8"))
    assert catalog["expected_tool_count"] == len(server.tools) == 77
    assert set(catalog["expected_tool_names"]) == set(server.tools)


def test_manager_mcp_catalog_fingerprint_matches_live_input_schemas():
    server = build_server()
    schemas = {name: tool.parameters for name, tool in server._tool_manager._tools.items()}
    catalog = json.loads((ROOT / "docs/agent/manager_mcp_catalog.json").read_text(encoding="utf-8"))

    assert len(schemas) == 77
    assert catalog["schema_fingerprint"] == _mcp_schema_fingerprint(schemas)


def test_manager_journal_supports_bounded_director_workflow(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    register_manager_memory_tools(server, store)

    created = server.tools["manager_journal"](
        operation="director_create",
        event="обнаружен повторяющийся обезличенный операционный сигнал",
        category="process_improvement",
        decision="проверить результат на следующем обзоре",
        status="waiting",
        next_review_at="2030-01-02T03:04:05+00:00",
    )
    assert created["ok"] is True

    readback = server.tools["manager_journal"](
        operation="director_read",
        status="active",
        category="process_improvement",
    )
    assert readback["entries"][0]["id"] == created["entry"]["id"]

    all_active = server.tools["manager_journal"](operation="director_read", status="active")
    assert all_active["entries"][0]["id"] == created["entry"]["id"]

    stats = server.tools["manager_journal"](operation="director_stats")
    assert stats["total_entries"] == 1
    assert stats["limits"]["max_entries"] == 400


def test_manager_context_skill_and_gateway_tools_are_registered(tmp_path):
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
    assert brief["route"]["selected_workflows"] == ["board_cleanup_autopilot"]
    assert brief["route"]["steps"][0]["effects"] == ["crm_write"]
    assert "crm_card_description_standard" in brief["route"]["steps"][0]["knowledge_domains"]

    compatibility = server.tools["recommend_service_management_actions"]
    assert compatibility is server.tools["agent_brief"]
    assert inspect.signature(compatibility) == inspect.signature(server.tools["agent_brief"])
    compatibility_brief = compatibility("Приберись", intent="board_cleanup", limit=5)
    assert compatibility_brief["format"] == "agent_brief_v1"
    assert compatibility_brief["route"]["selected_workflows"] == ["board_cleanup_autopilot"]

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
    crm_health = server.tools["crm_health_plan"]
    assert crm_health is server.tools["agent_bootstrap"]
    assert inspect.signature(crm_health) == inspect.signature(server.tools["agent_bootstrap"])
    live_tools = build_server()._tool_manager._tools
    assert live_tools["crm_health_plan"].parameters == live_tools["agent_bootstrap"].parameters

    for retired_tool in {
        "start_manager_run",
        "record_manager_run_event",
        "finish_manager_run",
        "list_manager_runs",
    }:
        assert retired_tool not in server.tools


def test_internal_store_adapter_tools_are_registered_with_stable_schemas(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    register_manager_memory_tools(server, store)

    expected_parameters = {
        "store_runtime_status": ["live", "bootstrap_snapshot"],
        "store_digest": ["baseline", "since", "cursor", "ack_token", "limit", "stream"],
        "store_search": ["entity", "query", "filters", "cursor", "limit"],
        "store_entity_context": ["entity", "entity_id", "detail"],
        "download_store_quote_vin_photo": ["quote_request_id", "expected_photo_sha256"],
        "store_management_action": [
            "domain",
            "action",
            "target_id",
            "planned_changes",
            "owner_intent",
            "expected_updated_at",
            "idempotency_key",
            "correlation_id",
            "mode",
        ],
    }

    for tool, parameters in expected_parameters.items():
        assert tool in server.tools
        assert list(inspect.signature(server.tools[tool]).parameters) == parameters
        assert "INTERNAL_ONLY" in server.descriptions[tool]

    status = server.tools["store_runtime_status"]()
    assert status["format"] == "store_agent_v1"
    assert status["status"] == "degraded"


def test_store_analytics_tool_is_registered_as_read_only_raw_and_uses_internal_runtime_config(
    tmp_path,
    monkeypatch,
):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    captured = {}

    monkeypatch.setattr(
        mcp_tools_module,
        "get_store_api_url",
        lambda: "http://autostop-app:8000/internal/agent/v1",
    )
    monkeypatch.setattr(mcp_tools_module, "get_store_read_token", lambda: "runtime-secret")

    def fake_report(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "format": "store_analytics_report_v1"}

    monkeypatch.setattr(mcp_tools_module, "get_store_analytics_report", fake_report)
    register_manager_memory_tools(server, store)

    result = server.tools["get_store_analytics_report"](
        query="сколько посетителей сегодня",
        period="auto",
        top_limit=5,
    )

    assert result["ok"] is True
    assert "READ_ONLY RAW_CAPABILITY" in server.descriptions["get_store_analytics_report"]
    assert captured == {
        "api_url": "http://autostop-app:8000/internal/agent/v1",
        "read_token": "runtime-secret",
        "query": "сколько посетителей сегодня",
        "period": "auto",
        "date_from": None,
        "date_to": None,
        "top_limit": 5,
    }


def test_store_owner_tools_are_guarded_and_forward_schema_bound_contract(tmp_path, monkeypatch):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    captured = {}

    class FakeOwnerClient:
        def __init__(self, **kwargs):
            captured["config"] = kwargs

        def list_capabilities(self, **kwargs):
            captured["list"] = kwargs
            return {"ok": True, "format": "autostop_store_owner_api_v1", "items": []}

        def prepare_invocation(self, operation_id, **kwargs):
            if operation_id == "get_part":
                method, risk = "GET", "read"
            else:
                method, risk = "PATCH", "write"
            return {
                "ok": True,
                "summary": {
                    "operation_id": operation_id,
                    "method": method,
                    "path": "/api/v1/parts/{id}",
                    "concrete_path": f"/api/v1/parts/{kwargs['path_parameters']['id']}",
                    "risk": risk,
                    "schema_hash": "a" * 64,
                    "path_parameters": ["id"],
                    "query_fields": [],
                    "query_sha256": "b" * 64,
                    "request_sha256": "c" * 64,
                    "plan_hash": "d" * 64,
                    "verification_class": "exact_entity" if method != "GET" else "operation_specific_state",
                    "revision_required": method != "GET",
                },
            }

        def invoke(self, **kwargs):
            captured.setdefault("invoke", []).append(kwargs)
            return {
                "ok": True,
                "format": "autostop_store_owner_api_v1",
                "status": "completed" if kwargs["mode"] == "read" else "compensating",
                "meta": {"readback_required": kwargs["mode"] != "read"},
            }

    monkeypatch.setattr(mcp_tools_module, "StoreOwnerApiClient", FakeOwnerClient)
    monkeypatch.setattr(
        mcp_tools_module,
        "get_store_api_url",
        lambda: "http://autostop-app:8000/internal/agent/v1",
    )
    monkeypatch.setattr(mcp_tools_module, "get_store_owner_token", lambda: "owner-runtime-secret")
    register_manager_memory_tools(server, store)

    assert "READ_ONLY RAW_CAPABILITY" in server.descriptions["store_owner_capabilities"]
    assert "OWNER_SCOPED RAW_CAPABILITY" in server.descriptions["store_owner_api"]
    assert "schema-bound dry-run proof" in server.descriptions["store_owner_api"]
    assert captured["config"] == {
        "agent_api_url": "http://autostop-app:8000/internal/agent/v1",
        "owner_token": "owner-runtime-secret",
    }
    assert server.tools["store_owner_capabilities"](query="parts", limit=10)["ok"] is True

    read = server.tools["store_owner_api"](
        operation_id="get_part",
        mode="read",
        path_parameters={"id": "part-1"},
    )
    assert read["status"] == "completed"
    assert read["meta"]["contract_id"] is None

    mismatch = server.tools["store_owner_api"](
        operation_id="update_part",
        mode="dry_run",
        target_id="part-other",
        path_parameters={"id": "part-1"},
        body={"name": "Updated"},
        owner_intent="Обновить точную карточку товара",
        idempotency_key="store-owner-update-mismatch-001",
        expected_revision="2026-07-21T00:00:00Z",
    )
    assert mismatch["error"]["code"] == "store_owner_target_binding_mismatch"

    missing_correlation = server.tools["store_owner_api"](
        operation_id="update_part",
        mode="dry_run",
        target_id="part-1",
        path_parameters={"id": "part-1"},
        body={"name": "Updated"},
        owner_intent="Обновить точную карточку товара",
        idempotency_key="store-owner-update-derived-001",
        expected_revision="2026-07-21T00:00:00Z",
    )
    assert missing_correlation["error"]["code"] == "store_owner_correlation_id_required"

    dry_prepare = server.tools["store_owner_api"](
        operation_id="update_part",
        mode="prepare",
        target_id="part-1",
        path_parameters={"id": "part-1"},
        body={"name": "Updated"},
        owner_intent="Обновить точную карточку товара",
        idempotency_key="store-owner-update-planned-001",
        correlation_id="store-owner-update-flow-001",
        expected_revision="2026-07-21T00:00:00Z",
    )
    assert dry_prepare["status"] == "validated"
    assert dry_prepare["summary"]["prepared_for_mode"] == "dry_run"
    assert dry_prepare["meta"]["request_dispatched"] is False
    dry_contract_id = dry_prepare["meta"]["contract_id"]
    contract_mismatch = server.tools["store_owner_api"](
        operation_id="update_part",
        mode="dry_run",
        target_id="part-1",
        path_parameters={"id": "part-1"},
        body={"name": "Updated"},
        owner_intent="Обновить точную карточку товара",
        idempotency_key="store-owner-update-planned-001",
        correlation_id="store-owner-update-flow-001",
        expected_revision="2026-07-21T00:00:00Z",
        expected_contract_id="ac_" + "0" * 20,
    )
    assert contract_mismatch["error"]["code"] == "store_owner_action_contract_mismatch"

    planned = server.tools["store_owner_api"](
        operation_id="update_part",
        mode="dry_run",
        target_id="part-1",
        path_parameters={"id": "part-1"},
        body={"name": "Updated"},
        owner_intent="Обновить точную карточку товара",
        idempotency_key="store-owner-update-planned-001",
        correlation_id="store-owner-update-flow-001",
        expected_revision="2026-07-21T00:00:00Z",
        expected_contract_id=dry_contract_id,
    )
    assert planned["status"] == "compensating"
    assert captured["invoke"][-1]["correlation_id"] == "store-owner-update-flow-001"

    apply_prepare = server.tools["store_owner_api"](
        operation_id="update_part",
        mode="prepare",
        target_id="part-1",
        path_parameters={"id": "part-1"},
        body={"name": "Updated"},
        owner_intent="Обновить точную карточку товара",
        idempotency_key="store-owner-update-part-001",
        correlation_id="store-owner-update-part-001",
        expected_revision="2026-07-21T00:00:00Z",
        prepare_for_mode="apply",
    )
    assert apply_prepare["summary"]["prepared_for_mode"] == "apply"
    apply_contract_id = apply_prepare["meta"]["contract_id"]
    write = server.tools["store_owner_api"](
        operation_id="update_part",
        mode="apply",
        target_id="part-1",
        path_parameters={"id": "part-1"},
        body={"name": "Updated"},
        owner_intent="Обновить точную карточку товара",
        idempotency_key="store-owner-update-part-001",
        correlation_id="store-owner-update-part-001",
        expected_revision="2026-07-21T00:00:00Z",
        expected_contract_id=apply_contract_id,
        dry_run_proof="b" * 64,
    )

    assert write["status"] == "compensating"
    assert write["meta"]["contract_id"]
    assert write["meta"]["operation_id"] == "update_part"
    assert len(write["meta"]["target_ref_sha256"]) == 64
    assert len(write["meta"]["expected_revision_sha256"]) == 64
    assert write["meta"]["request_sha256"] == "c" * 64
    assert write["meta"]["schema_hash"] == "a" * 64
    assert captured["invoke"][-1]["dry_run_proof"] == "b" * 64


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


def test_prepare_crm_card_action_is_canonical_action_contract_alias(tmp_path):
    server = _FakeServer()
    register_manager_memory_tools(server, ManagerMemoryStore(tmp_path / "memory.sqlite3"))

    assert server.tools["prepare_crm_card_action"] is server.tools["prepare_action_contract"]
    assert inspect.signature(server.tools["prepare_crm_card_action"]) == inspect.signature(
        server.tools["prepare_action_contract"]
    )
    live_tools = build_server()._tool_manager._tools
    assert live_tools["prepare_crm_card_action"].parameters == live_tools["prepare_action_contract"].parameters


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

    assert catalog["format"] == "mcp_surface_manifest_v1"
    assert catalog["expected_tool_count"] == len(catalog["expected_tool_names"]) == 24
    assert "agent_bootstrap" in catalog["expected_tool_names"]
    assert "call_raw_capability" in catalog["expected_tool_names"]
    assert "agent_document_workflow" in catalog["expected_tool_names"]
    assert "agent_finance_workflow" in catalog["expected_tool_names"]
    assert "prepare_action_contract" in catalog["expected_tool_names"]
    assert "prepare_crm_card_action" not in catalog["expected_tool_names"]
