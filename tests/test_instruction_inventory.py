from pathlib import Path
import os

import pytest

from autostop_manager import diagnostics
from autostop_manager import instruction_inventory as inventory_module
from autostop_manager.instruction_inventory import (
    MAX_INSTRUCTION_BYTES,
    MAX_ISSUES,
    collect_instruction_inventory,
    require_instruction_inventory,
    read_instruction_bytes,
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


@pytest.mark.parametrize("outside", [False, True])
def test_parent_traversal_preserves_symlink_semantics(tmp_path: Path, outside: bool) -> None:
    project = tmp_path / "project"
    write(project / "AGENTS.md", "[Target](docs/jump/../target.md)\n")
    actual_parent = tmp_path / "outside" if outside else project / "docs/actual"
    (actual_parent / "child").mkdir(parents=True)
    target = write(actual_parent / "target.md", "# Actual target\n")
    shadow = write(project / "docs/target.md", "# Lexically collapsed target\n")
    (project / "docs/jump").symlink_to(actual_parent / "child", target_is_directory=True)

    inventory = collect_instruction_inventory(project)

    assert shadow not in inventory.paths
    if outside:
        assert any(item.code == "outside" for item in inventory.issues)
        assert target not in inventory.contents
    else:
        assert not inventory.issues
        assert target in inventory.paths


def test_lexical_retired_path_cannot_be_hidden_by_parent_traversal(tmp_path: Path) -> None:
    write(tmp_path / "AGENTS.md", "[Target](docs/drafts/../live.md)\n")
    (tmp_path / "docs/drafts").mkdir(parents=True)
    live = write(tmp_path / "docs/live.md", "# Current\n")

    inventory = collect_instruction_inventory(tmp_path)

    assert inventory.issues[0].code == "retired"
    assert live not in inventory.contents


def test_inventory_retains_missing_declared_card_for_path_listing(tmp_path: Path) -> None:
    write(tmp_path / "AGENTS.md", "# Start\n")
    card = "docs/agent/tools/missing.md"
    write(tmp_path / "docs/agent/automotive_tools.json", '{"tools": [{"instruction_ref": "' + card + '"}]}')

    inventory = collect_instruction_inventory(tmp_path)

    assert inventory.unavailable[tmp_path / card] == "unavailable"
    assert card in diagnostics.instruction_paths(tmp_path)


@pytest.mark.parametrize("special", ["large", "fifo"])
def test_instruction_reader_rejects_unbounded_or_special_files(tmp_path: Path, special: str) -> None:
    target = tmp_path / "target.md"
    if special == "large":
        target.write_bytes(b"x" * (MAX_INSTRUCTION_BYTES + 1))
        expected = ValueError
    else:
        os.mkfifo(target)
        expected = OSError
    with pytest.raises(expected):
        read_instruction_bytes(target)


def test_inventory_reads_shared_targets_once_and_does_not_cache_across_operations(tmp_path: Path, monkeypatch) -> None:
    write(tmp_path / "AGENTS.md", "[First](docs/live.md) [Second](docs/alias.md)\n")
    live = write(tmp_path / "docs/live.md", "# Before\n")
    (tmp_path / "docs/alias.md").symlink_to(live)
    original = inventory_module.read_instruction_bytes
    calls = []

    def counted(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(inventory_module, "read_instruction_bytes", counted)
    before = collect_instruction_inventory(tmp_path)
    assert calls.count(live) == 1
    live.write_text("# After\n")
    after = collect_instruction_inventory(tmp_path)

    assert before.contents[live] == b"# Before\n"
    assert after.contents[live] == b"# After\n"
    assert calls.count(live) == 2


@pytest.mark.parametrize("leaf", ["docs/agent/modules/A4.md", "docs/agent/modules/A5.md", "docs/schema.json"])
def test_text_leaves_with_invalid_encoding_fail_closed(tmp_path: Path, leaf: str) -> None:
    write(tmp_path / "AGENTS.md", f"[Leaf]({leaf})\n")
    target = write(tmp_path / leaf, "# Synthetic\n")
    target.write_bytes(b"\xff")

    inventory = collect_instruction_inventory(tmp_path)

    assert any(item.code == "unreadable" and item.target == leaf for item in inventory.issues)
    assert inventory.unavailable[target] == "unavailable"
    with pytest.raises(ValueError, match="Invalid or unreadable"):
        require_instruction_inventory(inventory)
