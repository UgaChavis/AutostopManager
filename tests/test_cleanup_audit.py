from __future__ import annotations

import subprocess

import autostop_manager.cleanup_audit as cleanup_audit_module
from autostop_manager.cleanup_audit import build_cleanup_audit
from autostop_manager.storage import ManagerMemoryStore


def test_git_path_helpers_preserve_unicode_paths(monkeypatch, tmp_path):
    def fake_run(command, **_kwargs):
        if "--others" in command:
            stdout = "отчёты/черновик.pdf\0".encode()
        else:
            stdout = "документы/правило.md\0ordinary.py\0".encode()
        return subprocess.CompletedProcess(command, 0, stdout=stdout)

    monkeypatch.setattr(cleanup_audit_module.subprocess, "run", fake_run)

    assert cleanup_audit_module._git_tracked_paths(tmp_path) == ["документы/правило.md", "ordinary.py"]
    assert cleanup_audit_module._git_untracked_paths(tmp_path) == ["отчёты/черновик.pdf"]


def test_cleanup_audit_reports_safe_dry_run_candidates(tmp_path):
    root = tmp_path / "repo"
    (root / ".pytest_cache").mkdir(parents=True)
    (root / "autostop_manager" / "__pycache__").mkdir(parents=True)
    (root / ".venv" / "Lib" / "site-packages" / "demo" / "__pycache__").mkdir(parents=True)
    docs_agent = root / "docs" / "agent"
    docs_agent.mkdir(parents=True, exist_ok=True)
    (docs_agent / "knowledge_map.json").write_text(
        '{"domains":{"startup":{'
        '"primary_files":["docs/agent/known.md","docs/agent/partsapi_category_index.json"],'
        '"reference_files":["docs/agent/reference_only.md"]'
        "}}}",
        encoding="utf-8",
    )
    (docs_agent / "known.md").write_text("# Known\n", encoding="utf-8")
    (docs_agent / "reference_only.md").write_text("# Reference\n", encoding="utf-8")
    (docs_agent / "partsapi_category_index.json").write_text('{"categories":[]}\n', encoding="utf-8")
    (docs_agent / "unused.md").write_text("# Unused\n", encoding="utf-8")
    workspace_pdf = root / "out" / "repair-orders" / "sample.pdf"
    workspace_pdf.parent.mkdir(parents=True, exist_ok=True)
    workspace_pdf.write_bytes(b"%PDF-1.4")
    report_md = root / "reports" / "summary.md"
    report_md.parent.mkdir(parents=True, exist_ok=True)
    report_md.write_text("# Summary\n", encoding="utf-8")
    tmp_artifact = root / "tmp" / "scratch" / "sample.txt"
    tmp_artifact.parent.mkdir(parents=True, exist_ok=True)
    tmp_artifact.write_text("scratch\n", encoding="utf-8")
    backup_artifact = root / "data" / "backups" / "autostop_manager.sqlite3.20260519T123129Z.bak"
    backup_artifact.parent.mkdir(parents=True, exist_ok=True)
    backup_artifact.write_bytes(b"SQLite 3\x00")
    store = ManagerMemoryStore(root / "data" / "autostop_manager.sqlite3")
    store.initialize()

    result = build_cleanup_audit(project_root=root, store=store)

    assert result["ok"] is True
    categories = {item["category"] for item in result["candidates"]}
    assert {
        "ignored_cache",
        "generated_workspace_artifact",
        "unreferenced_agent_doc",
    }.issubset(categories)
    retained_categories = {item["category"] for item in result["retained_items"]}
    assert "local_db" in retained_categories
    allowed_actions = {
        "keep",
        "link_to_knowledge_map",
        "delete_after_approval",
        "review",
    }
    assert ("ob" + "sidian_duplicate") not in categories
    assert all(item["requires_approval"] is True for item in result["candidates"])
    assert all(item["recommended_action"] in allowed_actions for item in result["candidates"])
    ignored_cache_paths = {item["path"] for item in result["candidates"] if item["category"] == "ignored_cache"}
    assert "autostop_manager/__pycache__" in ignored_cache_paths
    assert not any(path.startswith(".venv/") for path in ignored_cache_paths)
    assert not any(item["path"] == "docs/agent/reference_only.md" for item in result["candidates"])
    assert not any(item["path"] == "docs/agent/partsapi_category_index.json" for item in result["candidates"])
    workspace_artifacts = [item for item in result["candidates"] if item["category"] == "generated_workspace_artifact"]
    assert {item["path"] for item in workspace_artifacts} == {"out", "reports", "tmp"}
    assert all(item["recommended_action"] == "review" for item in workspace_artifacts)
    backup = next(item for item in result["retained_items"] if item["path"] == "data/backups")
    assert backup["risk"] == "high" and backup["recommended_action"] == "keep"
    assert backup_artifact.read_bytes() == b"SQLite 3\x00"


def test_cleanup_audit_handles_invalid_knowledge_map_structure(tmp_path):
    root = tmp_path / "repo"
    docs_agent = root / "docs" / "agent"
    docs_agent.mkdir(parents=True)
    (docs_agent / "knowledge_map.json").write_text("[]", encoding="utf-8")

    result = build_cleanup_audit(project_root=root, store=ManagerMemoryStore(root / "data.sqlite3"))

    assert result["ok"] is True
    assert result["summary"]["candidate_count"] == 0


def test_cleanup_audit_reports_project_footprint_and_large_module_growth(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    source = root / "autostop_manager" / "large.py"
    source.parent.mkdir(parents=True)
    source.write_text("first\nsecond\n", encoding="utf-8")
    documentation = root / "docs" / "agent" / "route.md"
    documentation.parent.mkdir(parents=True)
    documentation.write_text("# Route\n", encoding="utf-8")

    monkeypatch.setattr(
        cleanup_audit_module,
        "_git_tracked_paths",
        lambda _root: ["autostop_manager/large.py", "docs/agent/route.md"],
    )
    monkeypatch.setattr(
        cleanup_audit_module,
        "_git_diff_numstat",
        lambda _root: [("autostop_manager/large.py", 501, 0)],
    )

    result = build_cleanup_audit(project_root=root, store=ManagerMemoryStore(root / "data.sqlite3"))

    footprint = result["project_footprint"]
    assert footprint["tracked_file_count"] == 2
    assert footprint["python_line_count"] == 2
    assert footprint["documentation_line_count"] == 1
    assert footprint["working_tree_diff"]["net_lines"] == 501
    assert footprint["warnings"] == [
        {
            "code": "large_production_file_growth",
            "path": "autostop_manager/large.py",
            "net_lines": 501,
            "threshold_net_lines": 500,
        }
    ]


def test_cleanup_audit_counts_untracked_source_growth(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    source = root / "autostop_manager" / "new_module.py"
    source.parent.mkdir(parents=True)
    source.write_text("one\ntwo\n", encoding="utf-8")

    monkeypatch.setattr(cleanup_audit_module, "_git_tracked_paths", lambda _root: [])
    monkeypatch.setattr(
        cleanup_audit_module,
        "_git_untracked_paths",
        lambda _root: ["autostop_manager/new_module.py"],
    )

    footprint = build_cleanup_audit(
        project_root=root,
        store=ManagerMemoryStore(root / "data.sqlite3"),
    )["project_footprint"]

    assert footprint["tracked_file_count"] == 0
    assert footprint["untracked_file_count"] == 1
    assert footprint["worktree_file_count"] == 1
    assert footprint["python_line_count"] == 2
    assert footprint["working_tree_diff"]["added_lines"] == 2
