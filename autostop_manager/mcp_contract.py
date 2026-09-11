from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT


MANAGER_MCP_CATALOG_PATH = PROJECT_ROOT / "docs" / "agent" / "manager_mcp_catalog.json"


class ManagerMcpContractError(RuntimeError):
    """Raised when the native Manager MCP surface does not match its manifest."""


def mcp_schema_fingerprint(tool_schemas: Mapping[str, Any]) -> str:
    """Fingerprint an MCP tool surface without including runtime data."""

    surface = [{"name": str(name), "inputSchema": tool_schemas[name]} for name in sorted(tool_schemas)]
    canonical = json.dumps(surface, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def validate_manager_mcp_surface(
    tool_schemas: Mapping[str, Any],
    *,
    manifest_path: Path | str = MANAGER_MCP_CATALOG_PATH,
) -> dict[str, Any]:
    """Compare the actual registered Manager tools to the tracked public manifest.

    The result intentionally carries only tool names and schema hashes.  It is
    safe to use in startup checks and transport diagnostics because it never
    includes arguments, provider responses, credentials, or customer data.
    """

    path = Path(manifest_path)
    base: dict[str, Any] = {
        "path": str(path),
        "diagnostic": "manifest_invalid",
        "ok": False,
        "expected_tool_count": None,
        "registered_tool_count": len(tool_schemas),
        "schema_fingerprint": None,
        "registered_schema_fingerprint": None,
        "missing_registered_tools": [],
        "unexpected_registered_tools": [],
        "warnings": [],
    }
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {**base, "warnings": ["manager_mcp_manifest_missing"]}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {**base, "warnings": ["manager_mcp_manifest_unreadable"]}
    if not isinstance(payload, dict):
        return {**base, "warnings": ["manager_mcp_manifest_invalid_structure"]}

    expected_raw = payload.get("expected_tool_names")
    expected = sorted({str(name) for name in expected_raw}) if isinstance(expected_raw, list) else []
    declared_count = payload.get("expected_tool_count")
    manifest_fingerprint = str(payload.get("schema_fingerprint") or "")
    manifest_warnings: list[str] = []
    if payload.get("format") != "mcp_surface_manifest_v1":
        manifest_warnings.append("manager_mcp_manifest_format_mismatch")
    if not expected or declared_count != len(expected):
        manifest_warnings.append("manager_mcp_manifest_tool_count_mismatch")
    if re.fullmatch(r"[0-9a-f]{64}", manifest_fingerprint) is None:
        manifest_warnings.append("manager_mcp_manifest_fingerprint_invalid")
    if manifest_warnings:
        return {
            **base,
            "expected_tool_count": declared_count,
            "schema_fingerprint": manifest_fingerprint or None,
            "warnings": manifest_warnings,
        }

    actual = {str(name): schema for name, schema in tool_schemas.items()}
    try:
        actual_fingerprint = mcp_schema_fingerprint(actual)
    except (TypeError, ValueError):
        return {
            **base,
            "expected_tool_count": declared_count,
            "schema_fingerprint": manifest_fingerprint,
            "warnings": ["manager_mcp_registered_schema_unserializable"],
        }
    missing_registered_tools = sorted(set(expected).difference(actual))
    unexpected_registered_tools = sorted(set(actual).difference(expected))
    if missing_registered_tools or unexpected_registered_tools:
        return {
            **base,
            "diagnostic": "tool_not_registered",
            "expected_tool_count": declared_count,
            "schema_fingerprint": manifest_fingerprint,
            "registered_schema_fingerprint": actual_fingerprint,
            "missing_registered_tools": missing_registered_tools,
            "unexpected_registered_tools": unexpected_registered_tools,
            "warnings": ["manager_mcp_surface_tool_set_mismatch"],
        }
    if manifest_fingerprint != actual_fingerprint:
        return {
            **base,
            "diagnostic": "schema_mismatch",
            "expected_tool_count": declared_count,
            "schema_fingerprint": manifest_fingerprint,
            "registered_schema_fingerprint": actual_fingerprint,
            "warnings": ["manager_mcp_surface_schema_mismatch"],
        }
    return {
        **base,
        "diagnostic": "ok",
        "ok": True,
        "expected_tool_count": declared_count,
        "schema_fingerprint": manifest_fingerprint,
        "registered_schema_fingerprint": actual_fingerprint,
    }


def assert_manager_mcp_surface(
    server: Any,
    *,
    manifest_path: Path | str = MANAGER_MCP_CATALOG_PATH,
) -> dict[str, Any]:
    """Fail closed before serving a stale or partially registered MCP surface."""

    tool_manager = getattr(server, "_tool_manager", None)
    tools = getattr(tool_manager, "_tools", None)
    if not isinstance(tools, Mapping):
        raise ManagerMcpContractError("manager_mcp_contract_registry_unavailable")
    tool_schemas = {str(name): getattr(tool, "parameters", None) for name, tool in tools.items()}
    if any(schema is None for schema in tool_schemas.values()):
        raise ManagerMcpContractError("manager_mcp_contract_schema_unavailable")
    result = validate_manager_mcp_surface(tool_schemas, manifest_path=manifest_path)
    if not result["ok"]:
        raise ManagerMcpContractError(f"manager_mcp_contract_{result['diagnostic']}")
    return result
