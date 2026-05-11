from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_board_cleanup_docs_do_not_reintroduce_old_archive_or_description_preview_policy():
    checked_paths = [
        ROOT / "docs" / "agent" / "board_cleanup_autopilot_playbook.md",
        ROOT / "docs" / "agent" / "autostop_manager_skill.md",
        ROOT / "docs" / "agent" / "command_routes.json",
        ROOT / "docs" / "agent" / "knowledge_annotations.jsonl",
        ROOT / "docs" / "agent" / "manager_mcp_catalog.json",
        ROOT / "docs" / "agent" / "manager_rules.json",
        ROOT / "docs" / "agent" / "operating_playbook.json",
    ]
    stale_fragments = [
        "archive completed cards when safe",
        "archive completed cards only when safe",
        "safe archive",
        "first five visible lines",
    ]

    combined = "\n".join(path.read_text(encoding="utf-8").casefold() for path in checked_paths)

    for fragment in stale_fragments:
        assert fragment not in combined
    assert "board_summary" in combined
    assert "separate explicit owner command" in combined
