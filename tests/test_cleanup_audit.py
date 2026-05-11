from __future__ import annotations

from pathlib import Path

from autostop_manager.cleanup_audit import build_cleanup_audit
from autostop_manager.storage import ManagerMemoryStore


def test_cleanup_audit_reports_safe_dry_run_candidates(tmp_path):
    root = tmp_path / "repo"
    (root / ".pytest_cache").mkdir(parents=True)
    (root / "autostop_manager" / "__pycache__").mkdir(parents=True)
    source_pack = root / "docs" / "agent" / "automotive_sources" / "source_cache" / "pack"
    (source_pack / "pdf").mkdir(parents=True)
    (source_pack / "md").mkdir(parents=True)
    (source_pack / "pdf" / "module_ru.pdf").write_bytes(b"%PDF-1.4")
    (source_pack / "md" / "module_ru.md").write_text("# Module\n", encoding="utf-8")
    docs_agent = root / "docs" / "agent"
    docs_agent.mkdir(parents=True, exist_ok=True)
    (docs_agent / "knowledge_map.json").write_text(
        '{"domains":{"startup":{"primary_files":["docs/agent/known.md"],"source_of_truth_files":["docs/agent/known.md"]}}}',
        encoding="utf-8",
    )
    (docs_agent / "known.md").write_text("# Known\n", encoding="utf-8")
    (docs_agent / "unused.md").write_text("# Unused\n", encoding="utf-8")
    (docs_agent / "knowledge_annotations.jsonl").write_text("", encoding="utf-8")
    cloud_vault = tmp_path / "cloud" / "AutostopCRM"
    desktop_vault = tmp_path / "desktop" / "AutostopCRM"
    cloud_vault.mkdir(parents=True)
    desktop_vault.mkdir(parents=True)
    (cloud_vault / "Home.md").write_text("cloud", encoding="utf-8")
    (desktop_vault / "Home.md").write_text("desktop", encoding="utf-8")
    store = ManagerMemoryStore(root / "data" / "autostop_manager.sqlite3")
    store.initialize()

    result = build_cleanup_audit(
        project_root=root,
        store=store,
        obsidian_cloud_vault=cloud_vault,
        obsidian_desktop_vault=desktop_vault,
    )

    assert result["ok"] is True
    categories = {item["category"] for item in result["candidates"]}
    assert {
        "ignored_cache",
        "tracked_pdf_duplicate",
        "unreferenced_agent_doc",
        "obsidian_duplicate",
        "local_db",
    }.issubset(categories)
    allowed_actions = {
        "keep",
        "link_to_knowledge_map",
        "exclude_from_obsidian_import",
        "move_to_archive_after_approval",
        "delete_after_approval",
    }
    assert all(item["requires_approval"] is True for item in result["candidates"])
    assert all(item["recommended_action"] in allowed_actions for item in result["candidates"])


def test_cleanup_audit_flags_source_pack_overindexed_routes(tmp_path):
    root = tmp_path / "repo"
    docs_agent = root / "docs" / "agent"
    docs_agent.mkdir(parents=True)
    primary_files = [f"docs/agent/source_cache/file_{index}.md" for index in range(26)]
    for raw_path in primary_files:
        path = root / Path(raw_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# File\n", encoding="utf-8")
    (docs_agent / "knowledge_map.json").write_text(
        '{"domains":{"parts_sourcing":{"primary_files":'
        + repr(primary_files).replace("'", '"')
        + ',"source_of_truth_files":[]}}}',
        encoding="utf-8",
    )

    result = build_cleanup_audit(project_root=root, store=ManagerMemoryStore(root / "data.sqlite3"))

    overindexed = [item for item in result["candidates"] if item["category"] == "source_pack_overindexed"]
    assert overindexed
    assert overindexed[0]["path"] == "knowledge_map:parts_sourcing"
    assert overindexed[0]["recommended_action"] == "link_to_knowledge_map"
