from pathlib import Path

import pytest

from autostop_manager import diagnostics
from autostop_manager.instruction_inventory import (
    MAX_ISSUES,
    collect_instruction_inventory,
    require_instruction_inventory,
)


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_graph_ignores_unlinked_history_and_generated_catalog_edges(tmp_path: Path) -> None:
    write(tmp_path / "AGENTS.md", "[Guide](docs/live.md)\n")
    write(tmp_path / "docs/live.md", "[Back](../AGENTS.md)\n[Schema](live.json)\n")
    write(tmp_path / "docs/live.json", "{}\n")
    write(tmp_path / "docs/agent/modules/A5.md", "[Old](../drafts/old.md)\n")
    for name in ("docs/old-roadmap.md", "docs/agent/drafts/old.md", "docs/history/old.md", "docs/reports/old.md"):
        path = write(tmp_path / name, "unlinked")
        path.write_bytes(b"\xff")

    inventory = collect_instruction_inventory(tmp_path)

    assert not inventory.issues
    assert {path.relative_to(tmp_path).as_posix() for path in inventory.paths} == {
        "AGENTS.md",
        "docs/live.md",
        "docs/live.json",
        "docs/agent/modules/A5.md",
    }


@pytest.mark.parametrize("target", ["docs/drafts/old.md", "docs/archive/old.md", "docs/history/old.md"])
def test_linked_retired_documents_are_rejected_without_reading(tmp_path: Path, target: str) -> None:
    write(tmp_path / "AGENTS.md", f"[Retired]({target})\n")
    path = write(tmp_path / target, "retired")
    path.write_bytes(b"\xff")

    inventory = collect_instruction_inventory(tmp_path)

    assert inventory.issues[0].code == "retired"
    assert path not in inventory.paths
    with pytest.raises(ValueError, match="Retired"):
        require_instruction_inventory(inventory)


@pytest.mark.parametrize("escape", ["outside", "retired", "loop"])
def test_instruction_aliases_cannot_escape_or_reactivate_history(tmp_path: Path, escape: str) -> None:
    project = tmp_path / "project"
    write(project / "AGENTS.md", "[Alias](alias.md)\n")
    alias = project / "alias.md"
    if escape == "outside":
        target = write(tmp_path / "outside.md", "outside")
    elif escape == "retired":
        target = write(project / "docs/drafts/old.md", "retired")
    else:
        target = alias
    alias.symlink_to(target)

    inventory = collect_instruction_inventory(project)

    assert inventory.issues[0].code == {"outside": "outside", "retired": "retired", "loop": "unreadable"}[escape]
    assert inventory.paths == (project / "AGENTS.md",)
    with pytest.raises(ValueError):
        require_instruction_inventory(inventory)


def test_declared_automotive_cards_are_active_without_a_markdown_link(tmp_path: Path) -> None:
    write(tmp_path / "AGENTS.md", "# Start\n")
    catalog = write(
        tmp_path / "docs/agent/automotive_tools.json",
        '{"tools": [{"instruction_ref": "docs/agent/tools/card.md"}]}\n',
    )
    card = write(tmp_path / "docs/agent/tools/card.md", "[Contract](contract.json)\n")
    contract = write(tmp_path / "docs/agent/tools/contract.json", "{}\n")

    inventory = collect_instruction_inventory(tmp_path)
    assert not inventory.issues
    assert {catalog, card, contract}.issubset(inventory.paths)
    card.unlink()
    inventory = collect_instruction_inventory(tmp_path)
    assert any(
        item.code == "missing" and item.source == "docs/agent/automotive_tools.json" for item in inventory.issues
    )
    with pytest.raises(ValueError, match="Missing"):
        require_instruction_inventory(inventory)


def test_inventory_limits_error_details_without_treating_failure_as_success(tmp_path: Path) -> None:
    write(tmp_path / "AGENTS.md", "\n".join(f"[Missing{i}](docs/missing-{i}.md)" for i in range(MAX_ISSUES + 10)))

    inventory = collect_instruction_inventory(tmp_path)

    assert len(inventory.issues) == MAX_ISSUES
    assert inventory.issues[-1].code == "instruction_inventory_limit"
    with pytest.raises(ValueError):
        require_instruction_inventory(inventory)


def test_only_path_listing_can_fall_back_when_the_markdown_dependency_is_absent(tmp_path: Path, monkeypatch) -> None:
    write(tmp_path / "AGENTS.md", "# Start\n")
    card = "docs/agent/tools/card.md"
    write(tmp_path / card, "# Card\n")
    write(tmp_path / "docs/agent/automotive_tools.json", '{"tools": [{"instruction_ref": "' + card + '"}]}')
    write(tmp_path / "docs/drafts/old.md", "# Retired\n")
    real_collect = diagnostics.collect_instruction_inventory

    def absent_parser(root, **options):
        if options.get("follow_links", True):
            raise ModuleNotFoundError("Synthetic absent parser", name="markdown_it")
        return real_collect(root, **options)

    monkeypatch.setattr(diagnostics, "collect_instruction_inventory", absent_parser)

    assert card in diagnostics.instruction_paths(tmp_path)
    assert "docs/drafts/old.md" not in diagnostics.instruction_paths(tmp_path)
    assert diagnostics.audit_documentation(tmp_path, check_external_links=False)["ok"] is False
