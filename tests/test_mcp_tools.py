from __future__ import annotations

import json
import inspect
from pathlib import Path

from autostop_manager import config as manager_config
import autostop_manager.mcp_tools as mcp_tools_module
from autostop_manager.mcp_server import build_server
from autostop_manager.mcp_tools import register_manager_tools
from autostop_manager.storage import StoreState
from autostop_manager.mcp_contract import mcp_schema_fingerprint as _mcp_schema_fingerprint


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
    store = StoreState(tmp_path / "memory.sqlite3")

    register_manager_tools(server, store)
    result = server.tools["decode_vehicle_identity"]("WBA00000000000000", live_vpic=False, live_wmi=False)

    assert result["ok"] is True
    assert captured["identifier"] == "WBA00000000000000"
    assert captured["live_vpic"] is False
    assert captured["live_wmi"] is False


def test_vehicle_and_catalog_reads_have_read_only_annotations(tmp_path):
    server = _FakeServer()
    register_manager_tools(server, StoreState(tmp_path / "memory.sqlite3"))

    for name in (
        "decode_vehicle_identity",
        "partsapi_catalog_lookup",
        "resolve_vin_oem_parts",
        "lookup_original_parts",
        "verify_oem_candidates_web",
        "catalog_provider_status",
        "plan_oem_parts_providers",
    ):
        annotations = server.options[name]["annotations"]
        assert annotations.readOnlyHint is True
        assert annotations.destructiveHint is False


def test_benchmark_vin_parts_lookup_tool_is_registered(tmp_path, monkeypatch):
    _clear_partsapi_env(monkeypatch)
    monkeypatch.delenv("VIN17_ACCOUNT", raising=False)
    monkeypatch.delenv("VIN17_SECRET", raising=False)
    server = _FakeServer()
    store = StoreState(tmp_path / "memory.sqlite3")

    register_manager_tools(server, store)

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
    store = StoreState(tmp_path / "memory.sqlite3")

    register_manager_tools(server, store)
    result = server.tools["benchmark_vin_parts_lookup"](
        [{"identifier": "MR41S123456"}],
        requested_part="передние колодки",
        partsapi_timeout=7.5,
    )

    assert result["ok"] is True
    assert captured["partsapi_timeout"] == 7.5


def test_lookup_public_automotive_evidence_tool_is_registered(tmp_path, monkeypatch):
    server = _FakeServer()
    store = StoreState(tmp_path / "memory.sqlite3")
    monkeypatch.setattr(
        mcp_tools_module,
        "lookup_public_automotive_evidence",
        lambda **kwargs: {"ok": True, "input_context": kwargs, "evidence": []},
    )

    register_manager_tools(server, store)

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


def test_verify_oem_candidates_web_tool_is_registered_and_read_only(tmp_path, monkeypatch):
    server = _FakeServer()
    store = StoreState(tmp_path / "memory.sqlite3")
    captured = {}
    monkeypatch.setattr(
        mcp_tools_module,
        "verify_oem_candidates_web",
        lambda **kwargs: captured.update(kwargs) or {"ok": True, "status": "prepared_no_network"},
    )

    register_manager_tools(server, store)
    result = server.tools["verify_oem_candidates_web"](
        candidates=[{"part_number": "4H0 615 301", "brand": "AUDI"}],
        make="Audi",
        model_year=2016,
        engine="3.0 TDI",
        axle="front",
        side="left",
        position="inner",
    )

    assert result["ok"] is True
    assert captured["candidates"][0]["part_number"] == "4H0 615 301"
    assert captured["axle"] == "front"
    assert captured["side"] == "left"
    assert captured["position"] == "inner"
    annotations = server.options["verify_oem_candidates_web"]["annotations"]
    assert annotations.readOnlyHint is True
    assert annotations.destructiveHint is False


def test_selective_registration_keeps_only_requested_tools(tmp_path):
    server = _FakeServer()

    register_manager_tools(
        server,
        StoreState(tmp_path / "memory.sqlite3"),
        include_tools={
            "store_owner_api",
            "store_owner_capabilities",
            "store_runtime_status",
        },
    )

    assert set(server.tools) == {
        "store_owner_api",
        "store_owner_capabilities",
        "store_runtime_status",
    }


def test_manager_mcp_catalog_matches_registered_tools(tmp_path):
    server = _FakeServer()
    store = StoreState(tmp_path / "memory.sqlite3")

    register_manager_tools(server, store)

    catalog = json.loads((ROOT / "docs/agent/manager_mcp_catalog.json").read_text(encoding="utf-8"))
    assert catalog["expected_tool_count"] == len(catalog["expected_tool_names"]) == len(server.tools)
    assert set(catalog["expected_tool_names"]) == set(server.tools)


def test_manager_mcp_catalog_fingerprint_matches_live_input_schemas():
    server = build_server()
    schemas = {name: tool.parameters for name, tool in server._tool_manager._tools.items()}
    catalog = json.loads((ROOT / "docs/agent/manager_mcp_catalog.json").read_text(encoding="utf-8"))

    assert len(schemas) == catalog["expected_tool_count"]
    assert catalog["schema_fingerprint"] == _mcp_schema_fingerprint(schemas)


def test_partsapi_description_advertises_only_accepted_operations():
    from autostop_manager.catalog_clients import PARTSAPI_OPERATIONS

    server = build_server()
    description = server._tool_manager._tools["partsapi_catalog_lookup"].description
    advertised = description.split("Valid operation values: ", 1)[1].split(".", 1)[0]
    assert set(advertised.split(", ")) == set(PARTSAPI_OPERATIONS)


def test_internal_store_adapter_tools_are_registered_with_stable_schemas(tmp_path):
    server = _FakeServer()
    store = StoreState(tmp_path / "memory.sqlite3")
    register_manager_tools(server, store)

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
        "store_quote_conductor": [
            "operation",
            "quote_request_id",
            "run_id",
            "expected_state_version",
            "expected_revision",
            "idempotency_key",
            "correlation_id",
            "entries",
            "coverage",
            "customer_response",
            "evidence",
            "consent_context_hash",
            "published_snapshot_hash",
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
    store = StoreState(tmp_path / "memory.sqlite3")
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
    register_manager_tools(server, store)

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
    store = StoreState(tmp_path / "memory.sqlite3")
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
    register_manager_tools(server, store)

    assert "READ_ONLY RAW_CAPABILITY" in server.descriptions["store_owner_capabilities"]
    assert "OWNER_SCOPED RAW_CAPABILITY" in server.descriptions["store_owner_api"]
    assert "dry-run proof" in server.descriptions["store_owner_api"]
    assert captured["config"] == {
        "agent_api_url": "http://autostop-app:8000/internal/agent/v1",
        "owner_token": "owner-runtime-secret",
    }
    assert (
        server.tools["store_owner_capabilities"](
            query="parts",
            limit=10,
            operation_id="update_part",
        )["ok"]
        is True
    )
    assert captured["list"] == {
        "query": "parts",
        "limit": 10,
        "operation_id": "update_part",
    }

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
