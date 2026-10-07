"""Offline regression checks for bounded display and pre-request validation."""

from __future__ import annotations

import json

import pytest
from mcp.server.fastmcp import FastMCP

from autostop_manager import catalog_adapters, catalog_clients, config, mcp_tools
from autostop_manager.catalog_adapters import CATALOG_STAGES
from autostop_manager.catalog_presentation import catalog_execution, present_catalog
from autostop_manager.storage import StoreState


class FakeServer:
    def __init__(self):
        self.tools = {}

    def tool(self, name, **_kwargs):
        def register(function):
            self.tools[name] = function
            return function

        return register


@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setattr(config, "_ENV_LOADED", True)
    server = FakeServer()
    mcp_tools.register_manager_tools(server, StoreState(tmp_path / "state.sqlite3"))
    return server.tools


def test_stage_rejection_precedes_environment_read(monkeypatch):
    def forbidden():
        pytest.fail("invalid stage must not read provider configuration")

    monkeypatch.setattr(catalog_adapters, "load_runtime_env", forbidden)
    for stage in ("oem", "", "unknown"):
        result = catalog_adapters.catalog_provider_status(stage=stage)
        assert result["outcome"] == "invalid_stage"
        assert result["ok"] is False
        assert result["available_stages"] == list(CATALOG_STAGES)


def test_stage_and_detail_schema_share_canonical_values(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_ENV_LOADED", True)
    server = FastMCP("offline-schema-test")
    mcp_tools.register_manager_tools(server, StoreState(tmp_path / "schema.sqlite3"))
    schemas = {name: tool.parameters for name, tool in server._tool_manager._tools.items()}
    stage = schemas["catalog_provider_status"]["properties"]["stage"]
    assert next(option["enum"] for option in stage["anyOf"] if "enum" in option) == list(CATALOG_STAGES)
    for name in ("catalog_provider_status", "partsapi_catalog_lookup"):
        assert schemas[name]["properties"]["detail"]["enum"] == ["summary", "full"]
        assert schemas[name]["properties"]["detail"]["default"] == "full"


@pytest.mark.parametrize("name", ["catalog_provider_status", "partsapi_catalog_lookup"])
def test_invalid_detail_never_invokes_backend(tools, monkeypatch, name):
    def forbidden(**_kwargs):
        pytest.fail("invalid detail reached backend")

    monkeypatch.setattr(mcp_tools, "_catalog_provider_status", forbidden)
    monkeypatch.setattr(mcp_tools, "partsapi_catalog_lookup", forbidden)
    args = {"operation": "vin_decode"} if name == "partsapi_catalog_lookup" else {}
    result = tools[name](**args, detail="unknown")
    if name == "catalog_provider_status":
        result = result.structuredContent
    assert result["ok"] is False
    assert result["outcome"] == "invalid_detail"
    if name == "partsapi_catalog_lookup":
        assert result["execution"]["network_calls"] == 0


def test_status_full_preserves_both_compact_channels_and_summary_bounds(tools, monkeypatch):
    payload = {
        "ok": True,
        "providers": [{"source_id": str(index), "capabilities": list(range(40))} for index in range(30)],
        "stage_matrix": [],
    }
    monkeypatch.setattr(mcp_tools, "_catalog_provider_status", lambda **_kwargs: payload)
    full = tools["catalog_provider_status"]()
    assert full.structuredContent == payload
    assert json.loads(full.content[0].text) == payload
    assert full.content[0].text == json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    summary = tools["catalog_provider_status"](detail="summary").structuredContent
    assert len(summary["providers"]) == 25
    assert len(summary["providers"][0]["capabilities"]) == 25
    assert summary["presentation"]["lists"]["providers"] == {"total": 30, "returned": 25, "truncated": True}
    assert summary["presentation"]["lists"]["providers[0].capabilities"]["total"] == 40
    assert len(payload["providers"]) == 30


def test_summary_keeps_safe_directory_preview_without_raw_secrets():
    payload = {
        "ok": True,
        "outcome": "success",
        "payload": {
            "data": [{"MAKE_ID": index, "MAKE_NAME": "TEST", "VIN": "A" * 17, "key": "secret"} for index in range(60)]
        },
        "vehicle_profiles": [],
    }
    summary = present_catalog(payload, "summary")
    assert "payload" not in summary
    assert summary["data_preview"]["source_total"] == 60
    assert len(summary["data_preview"]["records"]) == 25
    assert summary["presentation"]["lists"]["data_preview.records"]["total"] == 60
    assert set(summary["data_preview"]["records"][0]) == {"MAKE_ID", "MAKE_NAME"}
    assert "secret" not in str(summary) and "A" * 17 not in str(summary)
    normalized = present_catalog({**payload, "vehicle_profiles": [{"make": "TEST"}]}, "summary")
    assert "data_preview" not in normalized


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return json.dumps(self.payload).encode()


def test_conflict_beyond_display_limit_still_rejects_full_normalized_response(tools, monkeypatch):
    monkeypatch.setenv("PARTSAPI_BASE_URL", "https://partsapi.example.invalid")
    monkeypatch.setenv("PARTSAPI_VINDECODE_KEY", "test-only-key")
    rows = [{"vin": "A" * 17, "carId": index + 1, "brand": "TEST", "model": "MODEL"} for index in range(26)]
    rows[-1]["vin"] = "B" * 17
    calls = []

    def respond(*_args, **_kwargs):
        calls.append(True)
        return Response({"result": rows})

    monkeypatch.setattr(catalog_clients, "urlopen", respond)
    result = tools["partsapi_catalog_lookup"](operation="vin_decode", identifier="A" * 17, detail="summary")
    assert result["ok"] is False
    assert result["outcome"] == "identifier_mismatch"
    assert result["identifier_matches_request"] is False
    assert result["execution"]["network_calls"] == len(calls) == 1
    assert len(result["vehicle_profiles"]) == 25
    assert result["presentation"]["lists"]["vehicle_profiles"]["total"] == 26
    assert "payload" not in result
    assert "data_preview" not in result


@pytest.mark.parametrize("dry_run,reused", [(True, False), (False, True)])
def test_dry_or_reused_results_do_not_infer_network(dry_run, reused):
    row = catalog_execution({"ok": True, "attempt_count": 7, "reused": reused}, elapsed_ms=2, dry_run=dry_run)
    assert row["execution"]["network_calls"] == 0
    assert row["processing"]["elapsed_ms"] == 2


def test_backend_execution_and_bindings_take_precedence():
    payload = {
        "ok": True,
        "outcome": "partial",
        "attempt_count": 8,
        "catalog_binding": {"status": "validated_reference", "fitment_confirmed": False},
        "execution": {
            "network_calls": 2,
            "elapsed_ms": 15,
            "attempts": [{"ok": False}],
            "reused": True,
            "completeness": "partial",
        },
    }
    row = catalog_execution(payload, elapsed_ms=900)
    assert row["execution"]["network_calls"] == 2
    assert row["execution"]["elapsed_ms"] == 15
    assert row["execution"]["attempts"] == [{"ok": False}]
    assert row["processing"]["completeness"] == "partial"
    assert row["catalog_binding"] == payload["catalog_binding"]
