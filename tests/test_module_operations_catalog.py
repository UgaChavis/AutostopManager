"""Keep stable operation-card inventories aligned with the callable surface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from autostop_manager import cli, telegram_bridge
from autostop_manager.automation_control import OPERATIONS
from autostop_manager.automation_registry import AUTOMATION_TEMPLATES
from autostop_manager.automation_timers import SYSTEM_TIMER_ALLOWLIST


ROOT = Path(__file__).resolve().parents[1]
CARDS = ROOT / "docs" / "agent" / "module_operations"


def _subcommands(parser: argparse.ArgumentParser) -> set[str]:
    return {
        name for action in parser._actions if isinstance(action, argparse._SubParsersAction) for name in action.choices
    }


def test_manager_cli_and_mcp_tool_names_have_operation_cards():
    card = (CARDS / "manager_codex_mcp.md").read_text(encoding="utf-8")
    manifest = json.loads((ROOT / "docs/agent/manager_mcp_catalog.json").read_text(encoding="utf-8"))
    for name in _subcommands(cli.build_parser()) | set(manifest["expected_tool_names"]):
        assert f"`{name}`" in card, name


def test_bridge_and_automation_command_names_have_operation_cards():
    card = (CARDS / "telegram_automation.md").read_text(encoding="utf-8")
    for name in (
        _subcommands(telegram_bridge.build_parser())
        | set(OPERATIONS)
        | set(AUTOMATION_TEMPLATES)
        | set(SYSTEM_TIMER_ALLOWLIST)
    ):
        assert f"`{name}`" in card, name


def test_all_manager_scripts_and_units_are_classified():
    card = (CARDS / "manager_codex_mcp.md").read_text(encoding="utf-8")
    for path in (ROOT / "scripts").iterdir():
        if path.is_file():
            assert f"`scripts/{path.name}`" in card, path.name
    for path in (ROOT / "deploy/systemd").glob("autostop-*.service"):
        assert f"`{path.name}`" in card, path.name
