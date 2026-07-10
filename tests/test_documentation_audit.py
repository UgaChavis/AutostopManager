from __future__ import annotations

import json

from autostop_manager.documentation_audit import audit_documentation


def test_repository_documentation_contract_is_consistent():
    result = audit_documentation()

    assert result["ok"] is True, result
    assert result["broken_links"] == []
    assert result["broken_doc_refs"] == []
    assert result["duplicate_ids"] == {}
    assert result["route_errors"] == []
    assert result["annotation_errors"] == []


def test_documentation_audit_detects_broken_routes_duplicates_and_links(tmp_path):
    docs = tmp_path / "docs" / "agent"
    docs.mkdir(parents=True)
    (tmp_path / "README.md").write_text("[missing](missing.md) `docs/agent/missing.md`", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# Agent", encoding="utf-8")
    (docs / "route.md").write_text("# Route", encoding="utf-8")
    (docs / "knowledge_map.json").write_text(
        json.dumps({"domains": {"general": {"primary_files": ["docs/agent/route.md"]}}}), encoding="utf-8"
    )
    (docs / "command_routes.json").write_text(
        json.dumps(
            {
                "routes": [
                    {"command_id": "same", "domain": "missing", "open_first": "docs/agent/missing.md"},
                    {"command_id": "same", "domain": "general", "open_first": "docs/agent/route.md"},
                ]
            }
        ),
        encoding="utf-8",
    )
    (docs / "manager_rules.json").write_text(json.dumps({"rules": []}), encoding="utf-8")
    (docs / "manager_mcp_catalog.json").write_text(json.dumps({"all_tools": []}), encoding="utf-8")
    (docs / "knowledge_annotations.jsonl").write_text("", encoding="utf-8")

    result = audit_documentation(tmp_path)

    assert result["ok"] is False
    assert result["broken_links"]
    assert result["broken_doc_refs"]
    assert result["duplicate_ids"]["command_routes"] == ["same"]
    assert result["route_errors"]
