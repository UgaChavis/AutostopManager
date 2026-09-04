from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .cleanup_audit import build_cleanup_audit
from .config import PROJECT_ROOT
from .knowledge_base import audit_knowledge_base
from .skill_registry import audit_skill_registry
from .storage import ManagerMemoryStore, _now


MANAGER_MCP_CATALOG_PATH = PROJECT_ROOT / "docs" / "agent" / "manager_mcp_catalog.json"
REQUIRED_HEALTH_TOOLS = {"agent_bootstrap", "agent_brief", "cleanup_audit", "system_audit"}


def build_system_audit(
    *,
    store: ManagerMemoryStore | None = None,
    project_root: Path | str = PROJECT_ROOT,
    manager_mcp_catalog_path: Path | str = MANAGER_MCP_CATALOG_PATH,
    registered_tool_names: list[str] | None = None,
    registered_tool_schemas: dict[str, Any] | None = None,
) -> dict[str, Any]:
    memory = store or ManagerMemoryStore()
    memory.initialize()

    knowledge = audit_knowledge_base(memory)
    skills = audit_skill_registry()
    cleanup = build_cleanup_audit(project_root=project_root, store=memory)
    sqlite_stats = build_sqlite_stats(memory)
    memory_sections = _local_memory_sections(memory)
    catalog = audit_manager_mcp_catalog(
        Path(manager_mcp_catalog_path),
        registered_tool_names=registered_tool_names,
        registered_tool_schemas=registered_tool_schemas,
    )

    warnings = _collect_warnings(
        {
            "knowledge_audit": knowledge,
            "skills_audit": skills,
            "cleanup_audit": cleanup,
            "manager_mcp_catalog": catalog,
        }
    )
    summary = {
        "knowledge_ok": bool(knowledge.get("ok")),
        "skills_ok": bool(skills.get("ok")),
        "cleanup_candidate_count": int((cleanup.get("summary") or {}).get("candidate_count") or 0),
        "local_sqlite_size_bytes": int(sqlite_stats.get("size_bytes") or 0),
        "local_memory_sections": memory_sections,
        "manager_mcp_catalog_ok": bool(catalog.get("ok")),
        "tests_status": "external",
    }
    ok = all(
        [
            summary["knowledge_ok"],
            summary["skills_ok"],
            bool(cleanup.get("ok")),
            summary["manager_mcp_catalog_ok"],
        ]
    )
    return {
        "ok": ok,
        "generated_at": _now(),
        "summary": summary,
        "checks": {
            "knowledge_audit": knowledge,
            "skills_audit": skills,
            "cleanup_audit": cleanup,
            "sqlite_stats": sqlite_stats,
            "manager_mcp_catalog": catalog,
            "tests": {"status": "external", "note": "system_audit does not run pytest in v1"},
        },
        "warnings": warnings,
    }


def build_sqlite_stats(store: ManagerMemoryStore) -> dict[str, Any]:
    store.initialize()
    path = store.path
    table_counts: dict[str, int] = {}
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name ASC"
        ).fetchall()
        for (table_name,) in rows:
            try:
                count = int(conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0] or 0)
            except sqlite3.DatabaseError:
                count = 0
            table_counts[str(table_name)] = count
    return {
        "ok": True,
        "path": str(path),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "tables": table_counts,
    }


def audit_manager_mcp_catalog(
    path: Path = MANAGER_MCP_CATALOG_PATH,
    *,
    registered_tool_names: list[str] | None = None,
    registered_tool_schemas: dict[str, Any] | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    if not path.exists():
        return {
            "ok": False,
            "path": str(path),
            "warnings": ["manager_mcp_catalog_missing"],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {
            "ok": False,
            "path": str(path),
            "warnings": ["manager_mcp_catalog_invalid_json"],
        }
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "path": str(path),
            "warnings": ["manager_mcp_catalog_invalid_structure"],
        }

    all_tools = sorted({str(item) for item in payload.get("expected_tool_names") or []})
    declared_count = payload.get("expected_tool_count")
    if payload.get("format") != "mcp_surface_manifest_v1":
        warnings.append("manager_mcp_catalog_format_mismatch")
    if declared_count != len(all_tools):
        warnings.append("manager_mcp_catalog_tool_count_mismatch")
    manifest_fingerprint = str(payload.get("schema_fingerprint") or "")
    fingerprint = _mcp_schema_fingerprint(registered_tool_schemas) if registered_tool_schemas is not None else None
    if re.fullmatch(r"[0-9a-f]{64}", manifest_fingerprint) is None or (
        fingerprint is not None and manifest_fingerprint != fingerprint
    ):
        warnings.append("manager_mcp_catalog_fingerprint_mismatch")

    missing_required_tools = sorted(REQUIRED_HEALTH_TOOLS.difference(all_tools))
    if missing_required_tools:
        warnings.append("manager_mcp_catalog_missing_required_health_tools")

    registered_count = None
    missing_registered_tools: list[str] = []
    unknown_catalog_tools: list[str] = []
    if registered_tool_names is not None:
        registered = sorted({str(name) for name in registered_tool_names})
        registered_count = len(registered)
        missing_registered_tools = sorted(set(registered).difference(all_tools))
        unknown_catalog_tools = sorted(set(all_tools).difference(registered))
        if missing_registered_tools:
            warnings.append("manager_mcp_catalog_missing_registered_tools")
        if unknown_catalog_tools:
            warnings.append("manager_mcp_catalog_unknown_tools")

    return {
        "ok": not warnings,
        "path": str(path),
        "tool_count": declared_count,
        "all_tools_count": len(all_tools),
        "schema_fingerprint": manifest_fingerprint,
        "registered_schema_fingerprint": fingerprint,
        "registered_tool_count": registered_count,
        "missing_required_tools": missing_required_tools,
        "missing_registered_tools": missing_registered_tools,
        "unknown_catalog_tools": unknown_catalog_tools,
        "warnings": warnings,
    }


def _mcp_schema_fingerprint(tool_schemas: dict[str, Any]) -> str:
    surface = [{"name": name, "inputSchema": tool_schemas[name]} for name in sorted(tool_schemas)]
    canonical = json.dumps(surface, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _local_memory_sections(memory: ManagerMemoryStore) -> dict[str, int]:
    memory_map = memory.memory_map()
    sections = memory_map.get("sections") or {}
    return {
        str(name): int((summary or {}).get("count") or 0)
        for name, summary in sections.items()
        if isinstance(summary, dict)
    }


def _collect_warnings(checks: dict[str, dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for name, payload in checks.items():
        for warning in payload.get("warnings") or []:
            warning_text = str(warning)
            if name == "manager_mcp_catalog":
                warnings.append(warning_text)
            else:
                warnings.append(f"{name}:{warning_text}")
    return warnings
