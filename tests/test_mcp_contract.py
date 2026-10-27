from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from autostop_manager.mcp_contract import (
    ManagerMcpContractError,
    assert_manager_mcp_surface,
    mcp_schema_fingerprint,
    validate_manager_mcp_surface,
)
from autostop_manager.mcp_server import build_server


def _write_manifest(path, names: list[str], schemas: dict[str, object]) -> None:
    path.write_text(
        json.dumps(
            {
                "format": "mcp_surface_manifest_v1",
                "expected_tool_count": len(names),
                "expected_tool_names": names,
                "schema_fingerprint": mcp_schema_fingerprint(schemas),
            }
        ),
        encoding="utf-8",
    )


def test_native_manager_contract_matches_the_real_registered_source_surface():
    server = build_server()
    schemas = {name: tool.parameters for name, tool in server._tool_manager._tools.items()}

    report = validate_manager_mcp_surface(schemas)

    assert report["ok"] is True
    assert report["diagnostic"] == "ok"
    assert report["expected_tool_count"] == len(schemas)
    assert report["registered_tool_count"] == len(schemas)
    assert assert_manager_mcp_surface(server)["ok"] is True


def test_contract_distinguishes_an_advertised_but_unregistered_tool(tmp_path):
    manifest = tmp_path / "manager_mcp_catalog.json"
    expected_schemas = {"registered_tool": {"type": "object"}, "advertised_missing": {"type": "object"}}
    _write_manifest(manifest, list(expected_schemas), expected_schemas)

    report = validate_manager_mcp_surface({"registered_tool": {"type": "object"}}, manifest_path=manifest)

    assert report["ok"] is False
    assert report["diagnostic"] == "tool_not_registered"
    assert report["missing_registered_tools"] == ["advertised_missing"]
    assert report["unexpected_registered_tools"] == []


def test_contract_distinguishes_schema_drift_from_tool_registration(tmp_path):
    manifest = tmp_path / "manager_mcp_catalog.json"
    _write_manifest(manifest, ["registered_tool"], {"registered_tool": {"type": "object"}})

    report = validate_manager_mcp_surface(
        {"registered_tool": {"type": "object", "properties": {"changed": {"type": "string"}}}},
        manifest_path=manifest,
    )

    assert report["ok"] is False
    assert report["diagnostic"] == "schema_mismatch"
    assert report["missing_registered_tools"] == []
    assert report["unexpected_registered_tools"] == []


def test_server_startup_fails_closed_when_registry_is_not_available():
    server = SimpleNamespace(_tool_manager=None)

    with pytest.raises(ManagerMcpContractError, match="registry_unavailable"):
        assert_manager_mcp_surface(server)


@pytest.mark.parametrize(
    "names,count,tool_name",
    [
        (["fixture", "fixture"], 1, "fixture"),
        (["fixture"], True, "fixture"),
        (["fixture"], 1.0, "fixture"),
        ([123], 1, "123"),
        ([None], 1, "None"),
        ([""], 1, ""),
        ([" "], 1, " "),
        ([], 0, "fixture"),
        ("fixture", 1, "fixture"),
    ],
)
def test_contract_rejects_original_manifest_names_and_count_types(tmp_path, names, count, tool_name):
    manifest = tmp_path / "manager_mcp_catalog.json"
    schemas = {tool_name: {"type": "object"}}
    manifest.write_text(
        json.dumps(
            {
                "format": "mcp_surface_manifest_v1",
                "expected_tool_count": count,
                "expected_tool_names": names,
                "schema_fingerprint": mcp_schema_fingerprint(schemas),
            }
        ),
        encoding="utf-8",
    )

    report = validate_manager_mcp_surface(schemas, manifest_path=manifest)

    assert report["ok"] is False
    assert report["diagnostic"] == "manifest_invalid"
    assert report["warnings"]


def test_role_tools_have_explicit_read_and_control_contracts():
    tools = build_server()._tool_manager._tools
    reader = tools["manager_roles"]
    controller = tools["manager_role_action"]
    assert reader.annotations.readOnlyHint is True
    assert reader.annotations.destructiveHint is False
    assert reader.annotations.idempotentHint is True
    assert controller.annotations.readOnlyHint is False
    assert controller.annotations.destructiveHint is True
    assert controller.annotations.idempotentHint is False
    assert {"operation", "role", "actor", "idempotency_key"} <= set(controller.parameters["required"])
    assert {"start", "stop", "stop_all", "task_return", "approval_consume"} <= set(
        controller.parameters["properties"]["operation"]["enum"]
    )
