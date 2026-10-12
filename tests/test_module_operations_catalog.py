"""Keep stable operation-card inventories aligned with the callable surface."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pytest

from autostop_manager import cli, telegram_bridge
from autostop_manager.automation_control import OPERATIONS
from autostop_manager.automation_registry import AUTOMATION_TEMPLATES
from autostop_manager.automation_timers import SYSTEM_TIMER_ALLOWLIST
from autostop_manager.document_links import parse_local_document_link, validate_document_reference
from autostop_manager.markdown_links import visible_markdown_links


ROOT = Path(__file__).resolve().parents[1]
CARDS = ROOT / "docs" / "agent" / "references"


def _subcommands(parser: argparse.ArgumentParser) -> set[str]:
    return {
        name for action in parser._actions if isinstance(action, argparse._SubParsersAction) for name in action.choices
    }


def _documented_names(text: str) -> set[str]:
    return {name for span in re.findall(r"`([^`\n]+)`", text) for name in re.findall(r"[A-Za-z0-9_-]+", span)}


def _operation_documents(start: Path, *, root: Path = ROOT) -> dict[Path, str]:
    """Follow the same local instruction links an agent uses, excluding catalogs."""
    root = root.resolve()
    pending = [start.resolve()]
    documents: dict[Path, str] = {}
    while pending:
        path = pending.pop()
        if path in documents:
            continue
        text = path.read_text(encoding="utf-8")
        documents[path] = text
        for target in visible_markdown_links(text):
            reference = parse_local_document_link(target)
            if reference is None:
                continue
            linked = (path.parent / reference.path).resolve() if reference.path else path
            if not linked.is_relative_to(root) or linked.suffix != ".md":
                continue
            assert linked.is_file(), f"Missing instruction: {linked}"
            validate_document_reference(reference, linked, linked.read_text(encoding="utf-8"))
            # An index lists files but does not supply their operating contracts.
            if linked.name not in {"A4.md", "A5.md"}:
                pending.append(linked)
    return documents


def test_manager_cli_and_mcp_tool_names_have_operation_cards():
    runtime = CARDS / "manager-runtime.md"
    card = runtime.read_text(encoding="utf-8")
    manifest = json.loads((ROOT / "docs/agent/manager_mcp_catalog.json").read_text(encoding="utf-8"))
    missing_cli = _subcommands(cli.build_parser()) - _documented_names(card)
    assert not missing_cli, sorted(missing_cli)
    documented = set().union(*(_documented_names(text) for text in _operation_documents(runtime).values()))
    missing_tools = set(manifest["expected_tool_names"]) - documented
    assert not missing_tools, sorted(missing_tools)


def test_operation_documents_follow_profile_links_and_command_spans(tmp_path):
    runtime = tmp_path / "runtime.md"
    module = tmp_path / "module.md"
    catalog = tmp_path / "A5.md"
    unreachable = tmp_path / "unreachable.md"
    runtime.write_text("[Module](module.md#order) [Catalog](A5.md) [Web](https://example.com/remote.md)")
    module.write_text("# Order\n\n[Runtime](runtime.md) `store_quote_conductor order` and `waiting_payment`")
    catalog.write_text("[Unused](unreachable.md)")
    unreachable.write_text("`undocumented_tool`")
    documents = _operation_documents(runtime, root=tmp_path)
    assert set(documents) == {runtime, module}
    assert _documented_names(documents[module]) == {"store_quote_conductor", "order", "waiting_payment"}
    assert "undocumented_tool" not in set().union(*map(_documented_names, documents.values()))


def test_operation_documents_reject_broken_profile_links(tmp_path):
    runtime = tmp_path / "runtime.md"
    runtime.write_text("[Missing module](missing.md)")
    with pytest.raises(AssertionError, match="Missing instruction"):
        _operation_documents(runtime, root=tmp_path)


def test_operation_navigation_uses_reference_links_and_ignores_literal_examples(tmp_path):
    runtime = tmp_path / "runtime.md"
    module = tmp_path / "module.md"
    runtime.write_text(
        "[Module][selected]\n\n[selected]: module.md:3#order\n\n"
        "`[Fake](missing-inline.md)`\n\n```markdown\n[Fake](missing-fenced.md)\n```\n\n"
        "[unused]: missing-unused.md\n"
    )
    module.write_text("Intro\n\n# Order\n\n`store_quote_conductor`\n")
    assert set(_operation_documents(runtime, root=tmp_path)) == {runtime, module}


def test_bridge_and_automation_command_names_have_operation_cards():
    card = (CARDS / "telegram-runtime.md").read_text(encoding="utf-8")
    for name in (
        _subcommands(telegram_bridge.build_parser())
        | set(OPERATIONS)
        | set(AUTOMATION_TEMPLATES)
        | set(SYSTEM_TIMER_ALLOWLIST)
    ):
        assert f"`{name}`" in card, name


def test_all_manager_scripts_and_units_are_classified():
    runtime = CARDS / "manager-runtime.md"
    card = runtime.read_text(encoding="utf-8")
    for path in (ROOT / "scripts").iterdir():
        if path.is_file():
            assert f"`scripts/{path.name}`" in card, path.name
    service_cards = "\n".join(_operation_documents(runtime).values())
    for path in (ROOT / "deploy/systemd").glob("autostop-*.service"):
        assert f"`{path.name}`" in service_cards, path.name
