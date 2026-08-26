from __future__ import annotations

import json

import pytest

from autostop_manager import cli


def test_cli_parser_has_core_commands():
    parser = cli.build_parser()

    args = parser.parse_args(
        [
            "integration-audit",
            "--full",
            "--gmail-proof",
            "/tmp/proof.json",
            "--output",
            "/tmp/report.json",
        ]
    )
    assert args.command == "integration-audit"
    assert args.full is True
    assert args.gmail_proof == "/tmp/proof.json"
    assert args.output == "/tmp/report.json"

    args = parser.parse_args(["control-report", "--format", "markdown", "--output", "reports/control-report.md"])
    assert args.command == "control-report"
    assert args.format == "markdown"
    assert args.output == "reports/control-report.md"

    args = parser.parse_args(["memory-review"])
    assert args.command == "memory-review"

    args = parser.parse_args(["knowledge-intake", "--path", "docs/agent/knowledge_map.json", "--dry-run"])
    assert args.command == "knowledge-intake"
    assert args.path == "docs/agent/knowledge_map.json"
    assert args.dry_run is True
    assert args.apply is False

    args = parser.parse_args(["knowledge-sync"])
    assert args.command == "knowledge-sync"

    args = parser.parse_args(["knowledge-probe", "clutch gearbox", "--limit", "3"])
    assert args.command == "knowledge-probe"
    assert args.query == "clutch gearbox"
    assert args.limit == 3

    args = parser.parse_args(["knowledge-audit"])
    assert args.command == "knowledge-audit"

    args = parser.parse_args(["cleanup-audit"])
    assert args.command == "cleanup-audit"

    args = parser.parse_args(["doctor"])
    assert args.command == "doctor"

    args = parser.parse_args(["agent-brief", "Приберись", "--intent", "board_cleanup", "--limit", "5"])
    assert args.command == "agent-brief"
    assert args.query == "Приберись"
    assert args.intent == "board_cleanup"
    assert args.limit == 5

    args = parser.parse_args(["skills-audit"])
    assert args.command == "skills-audit"

    command_action = next(action for action in parser._actions if action.dest == "command")
    for retired_command in {"run-start", "run-event", "run-finish", "run-list"}:
        assert retired_command not in command_action.choices


def test_doctor_returns_nonzero_when_audit_fails(monkeypatch, capsys):
    monkeypatch.setattr(cli, "build_system_audit", lambda **_kwargs: {"ok": False, "warnings": ["broken"]})

    exit_code = cli.main(["doctor"])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_doctor_returns_zero_when_audit_passes(monkeypatch, capsys):
    monkeypatch.setattr(cli, "build_system_audit", lambda **_kwargs: {"ok": True, "warnings": []})

    exit_code = cli.main(["doctor"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_every_top_level_cli_command_has_working_help(capsys):
    parser = cli.build_parser()
    command_action = next(action for action in parser._actions if action.dest == "command")

    for command in sorted(command_action.choices):
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args([command, "--help"])
        assert exc_info.value.code == 0, command

    assert "usage:" in capsys.readouterr().out
