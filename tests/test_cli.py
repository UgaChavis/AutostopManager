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

    args = parser.parse_args(["store-conductor-release-gate"])
    assert args.command == "store-conductor-release-gate"

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


def test_cli_dispatches_safe_commands_and_writes_requested_reports(tmp_path, monkeypatch, capsys):
    store = object()
    monkeypatch.setattr(cli, "ManagerMemoryStore", lambda: store)
    monkeypatch.setattr(cli, "sync_knowledge_base", lambda current: {"ok": current is store})
    monkeypatch.setattr(cli, "probe_knowledge_base", lambda current, query, limit: {"ok": True, "query": query})
    monkeypatch.setattr(cli, "audit_knowledge_base", lambda current: {"ok": current is store})
    monkeypatch.setattr(cli, "build_cleanup_audit", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(cli, "build_system_audit", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(cli, "audit_skill_registry", lambda: {"ok": True})
    monkeypatch.setattr(cli, "audit_memory", lambda current: {"ok": current is store})
    monkeypatch.setattr(cli, "build_agent_brief", lambda current, query, **_kwargs: {"ok": True, "query": query})
    monkeypatch.setattr(
        cli,
        "build_integration_audit",
        lambda **kwargs: {"ok": True, "full": kwargs["full"], "scope": {"store": True}},
    )
    monkeypatch.setattr(cli, "build_control_report", lambda **_kwargs: {"ok": True, "summary": "ready"})
    monkeypatch.setattr(cli, "format_control_report_markdown", lambda _report: "# Ready\n")

    for argv in (
        ["knowledge-sync"],
        ["knowledge-probe", "clutch", "--limit", "2"],
        ["knowledge-audit"],
        ["cleanup-audit"],
        ["doctor"],
        ["skills-audit"],
        ["memory-review"],
        ["agent-brief", "prepare", "--intent", "maintenance", "--limit", "2"],
        ["integration-audit"],
    ):
        assert cli.main(argv) == 0

    integration_path = tmp_path / "integration.json"
    json_path = tmp_path / "control.json"
    markdown_path = tmp_path / "control.md"
    assert cli.main(["integration-audit", "--full", "--output", str(integration_path)]) == 0
    assert cli.main(["control-report", "--output", str(json_path)]) == 0
    assert cli.main(["control-report", "--format", "markdown", "--output", str(markdown_path)]) == 0

    assert json.loads(integration_path.read_text(encoding="utf-8"))["full"] is True
    assert json.loads(integration_path.read_text(encoding="utf-8"))["scope"]["store"] is True
    assert json.loads(json_path.read_text(encoding="utf-8"))["summary"] == "ready"
    assert markdown_path.read_text(encoding="utf-8") == "# Ready\n"
    assert capsys.readouterr().out


def test_every_top_level_cli_command_has_working_help(capsys):
    parser = cli.build_parser()
    command_action = next(action for action in parser._actions if action.dest == "command")

    for command in sorted(command_action.choices):
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args([command, "--help"])
        assert exc_info.value.code == 0, command

    assert "usage:" in capsys.readouterr().out


def test_store_conductor_release_gate_reports_store_readiness(monkeypatch, capsys):
    class _Store:
        def store_quote_conductor_release_readiness(self):
            return {"ok": True, "read_only": True}

    monkeypatch.setattr(cli, "ManagerMemoryStore", _Store)

    assert cli.main(["store-conductor-release-gate"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "read_only": True}
