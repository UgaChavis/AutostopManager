from __future__ import annotations

import hashlib
import json
from pathlib import Path

from autostop_manager.mcp_tools import register_manager_memory_tools
from autostop_manager.storage import ManagerMemoryStore
from autostop_manager.system_audit import _mcp_schema_fingerprint, build_system_audit


ROOT = Path(__file__).resolve().parents[1]


def _manifest(names: list[str], *, count: int | None = None, fingerprint: str | None = None) -> dict:
    names = sorted(names)
    digest = hashlib.sha256(json.dumps(names, separators=(",", ":")).encode()).hexdigest()
    return {
        "format": "mcp_surface_manifest_v1",
        "source": "test",
        "expected_tool_count": len(names) if count is None else count,
        "expected_tool_names": names,
        "schema_fingerprint": digest if fingerprint is None else fingerprint,
        "verified_at": "2026-08-25",
    }


class _FakeServer:
    def __init__(self):
        self.tools = {}

    def tool(self, name: str, description: str = "", **_kwargs):
        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


def test_system_audit_returns_health_summary_and_sqlite_stats(tmp_path):
    server = _FakeServer()
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    register_manager_memory_tools(server, store)

    result = build_system_audit(store=store, registered_tool_names=list(server.tools))

    assert result["ok"] is True
    assert result["summary"]["knowledge_ok"] is True
    assert result["summary"]["annotations_ok"] is True
    assert result["summary"]["skills_ok"] is True
    assert result["summary"]["cleanup_candidate_count"] >= 0
    assert result["summary"]["local_sqlite_size_bytes"] > 0
    assert "rules" in result["summary"]["local_memory_sections"]
    assert result["summary"]["manager_mcp_catalog_ok"] is True
    assert result["summary"]["tests_status"] == "external"
    assert result["checks"]["cleanup_audit"]["mode"] == "dry_run"


def test_system_audit_flags_broken_manager_mcp_catalog_count(tmp_path):
    catalog_path = tmp_path / "manager_mcp_catalog.json"
    catalog_path.write_text(
        json.dumps(_manifest(["system_audit", "cleanup_audit"], count=3)),
        encoding="utf-8",
    )

    result = build_system_audit(
        store=ManagerMemoryStore(tmp_path / "memory.sqlite3"),
        manager_mcp_catalog_path=catalog_path,
        registered_tool_names=["system_audit", "cleanup_audit"],
    )

    assert result["ok"] is False
    assert result["summary"]["manager_mcp_catalog_ok"] is False
    assert "manager_mcp_catalog_tool_count_mismatch" in result["warnings"]


def test_system_audit_flags_stale_surface_fingerprint(tmp_path):
    catalog_path = tmp_path / "manager_mcp_catalog.json"
    catalog_path.write_text(
        json.dumps(_manifest(["system_audit", "cleanup_audit"], fingerprint="0" * 64)),
        encoding="utf-8",
    )

    result = build_system_audit(
        store=ManagerMemoryStore(tmp_path / "memory.sqlite3"),
        manager_mcp_catalog_path=catalog_path,
        registered_tool_names=["system_audit", "cleanup_audit"],
        registered_tool_schemas={"cleanup_audit": {"type": "object"}},
    )

    assert result["ok"] is False
    assert "manager_mcp_catalog_fingerprint_mismatch" in result["warnings"]


def test_system_audit_accepts_matching_live_schema_fingerprint(tmp_path):
    schemas = {"cleanup_audit": {"properties": {}, "type": "object"}}
    catalog_path = tmp_path / "manager_mcp_catalog.json"
    catalog_path.write_text(
        json.dumps(_manifest(["cleanup_audit"], fingerprint=_mcp_schema_fingerprint(schemas))),
        encoding="utf-8",
    )

    result = build_system_audit(
        store=ManagerMemoryStore(tmp_path / "memory.sqlite3"),
        manager_mcp_catalog_path=catalog_path,
        registered_tool_names=["cleanup_audit"],
        registered_tool_schemas=schemas,
    )

    assert result["checks"]["manager_mcp_catalog"]["warnings"] == ["manager_mcp_catalog_missing_required_health_tools"]


def test_system_audit_handles_invalid_manager_mcp_catalog_structure(tmp_path):
    catalog_path = tmp_path / "manager_mcp_catalog.json"
    catalog_path.write_text("[]", encoding="utf-8")

    result = build_system_audit(
        store=ManagerMemoryStore(tmp_path / "memory.sqlite3"),
        manager_mcp_catalog_path=catalog_path,
        registered_tool_names=["system_audit", "cleanup_audit"],
    )

    assert result["ok"] is False
    assert result["summary"]["manager_mcp_catalog_ok"] is False
    assert "manager_mcp_catalog" in result["checks"]
    assert result["checks"]["manager_mcp_catalog"]["warnings"] == ["manager_mcp_catalog_invalid_structure"]
