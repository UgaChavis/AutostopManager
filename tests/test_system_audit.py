from __future__ import annotations

import json
from pathlib import Path

from autostop_manager.mcp_tools import register_manager_memory_tools
from autostop_manager.storage import ManagerMemoryStore
from autostop_manager.system_audit import build_system_audit


ROOT = Path(__file__).resolve().parents[1]


class _FakeServer:
    def __init__(self):
        self.tools = {}

    def tool(self, name: str, description: str = ""):
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
    assert result["summary"]["documentation_ok"] is True
    assert result["summary"]["tests_status"] == "external"
    assert result["checks"]["cleanup_audit"]["mode"] == "dry_run"
    assert result["checks"]["documentation_audit"]["ok"] is True


def test_system_audit_flags_broken_manager_mcp_catalog_count(tmp_path):
    catalog_path = tmp_path / "manager_mcp_catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "tool_count": 3,
                "all_tools": ["system_audit", "cleanup_audit"],
            }
        ),
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
