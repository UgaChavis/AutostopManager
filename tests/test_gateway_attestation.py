from __future__ import annotations

import json
import subprocess
from pathlib import Path

from autostop_manager.cli import build_parser
from autostop_manager.gateway_attestation import (
    GATEWAY_ATTESTATION_FORMAT,
    build_gateway_attestation_command,
    run_gateway_attestation,
)


RUN_ID = "AST-GWAT-20260728T165722Z"


def _write_runner(crm_root: Path) -> None:
    python = crm_root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    script = crm_root / "scripts" / "attest_agent_gateway_v2.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")


def test_command_builder_routes_one_explicit_action() -> None:
    command = build_gateway_attestation_command(
        action="case",
        run_id=RUN_ID,
        crm_root="/opt/autostopcrm",
        case_id="public:agent_bootstrap",
        apply_synthetic=True,
    )

    assert command[:2] == [
        "/opt/autostopcrm/.venv/bin/python",
        "/opt/autostopcrm/scripts/attest_agent_gateway_v2.py",
    ]
    assert command[-3:] == [
        "--case",
        "public:agent_bootstrap",
        "--apply-synthetic",
    ]
    assert "MINIMAL_KANBAN_MCP_BEARER_TOKEN" not in command


def test_command_builder_rejects_inapplicable_safety_flags() -> None:
    for action in ("next", "resume", "retry", "cleanup", "summary"):
        try:
            build_gateway_attestation_command(
                action=action,
                run_id=RUN_ID,
                force=True,
            )
        except ValueError as exc:
            assert str(exc) == "gateway_attestation_force_action_invalid"
        else:
            raise AssertionError(action)

    for action in ("inventory", "cleanup", "summary"):
        try:
            build_gateway_attestation_command(
                action=action,
                run_id=RUN_ID,
                apply_synthetic=True,
            )
        except ValueError as exc:
            assert str(exc) == "gateway_attestation_apply_action_invalid"
        else:
            raise AssertionError(action)


def test_runner_passes_token_only_in_environment_and_returns_safe_contract(tmp_path) -> None:
    crm_root = tmp_path / "crm"
    _write_runner(crm_root)
    (crm_root / ".env").write_text(
        "MINIMAL_KANBAN_MCP_BEARER_TOKEN=hidden-token\n",
        encoding="utf-8",
    )
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "format": GATEWAY_ATTESTATION_FORMAT,
                    "run_id": RUN_ID,
                    "status": "ready",
                    "summary": {"passed": 1, "pending": 69, "blocked": 0},
                    "data_included": False,
                }
            ),
            stderr="",
        )

    result = run_gateway_attestation(
        action="next",
        run_id=RUN_ID,
        crm_root=crm_root,
        output_root=tmp_path / "reports",
        command_runner=runner,
    )

    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert calls[0][1]["env"]["MINIMAL_KANBAN_MCP_BEARER_TOKEN"] == "hidden-token"
    assert "hidden-token" not in json.dumps(result)
    assert "hidden-token" not in " ".join(calls[0][0])


def test_runner_fails_closed_without_printing_subprocess_output(tmp_path) -> None:
    crm_root = tmp_path / "crm"
    _write_runner(crm_root)
    (crm_root / ".env").write_text(
        "MINIMAL_KANBAN_MCP_BEARER_TOKEN=hidden-token\n",
        encoding="utf-8",
    )

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            2,
            stdout="private CRM payload",
            stderr="secret transport diagnostics",
        )

    result = run_gateway_attestation(
        action="next",
        run_id=RUN_ID,
        crm_root=crm_root,
        command_runner=runner,
    )

    assert result["ok"] is False
    assert result["blocked"]["error_code"] == "crm_gateway_attestation_output_invalid"
    assert "private CRM payload" not in json.dumps(result)
    assert "secret transport diagnostics" not in json.dumps(result)


def test_runner_rejects_mismatched_run_id_or_data_contract(tmp_path) -> None:
    crm_root = tmp_path / "crm"
    _write_runner(crm_root)
    (crm_root / ".env").write_text(
        "MINIMAL_KANBAN_MCP_BEARER_TOKEN=hidden-token\n",
        encoding="utf-8",
    )

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "format": GATEWAY_ATTESTATION_FORMAT,
                    "run_id": "AST-GWAT-20260728T165722Z-other",
                    "status": "completed",
                    "data_included": True,
                }
            ),
            stderr="",
        )

    result = run_gateway_attestation(
        action="summary",
        run_id=RUN_ID,
        crm_root=crm_root,
        command_runner=runner,
    )

    assert result["ok"] is False
    assert result["blocked"]["error_code"] == "crm_gateway_attestation_contract_invalid"


def test_runner_rejects_exit_status_that_disagrees_with_payload(tmp_path) -> None:
    crm_root = tmp_path / "crm"
    _write_runner(crm_root)
    (crm_root / ".env").write_text(
        "MINIMAL_KANBAN_MCP_BEARER_TOKEN=hidden-token\n",
        encoding="utf-8",
    )

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            2,
            stdout=json.dumps(
                {
                    "ok": True,
                    "format": GATEWAY_ATTESTATION_FORMAT,
                    "run_id": RUN_ID,
                    "status": "completed",
                    "data_included": False,
                }
            ),
            stderr="",
        )

    result = run_gateway_attestation(
        action="cleanup",
        run_id=RUN_ID,
        crm_root=crm_root,
        command_runner=runner,
    )

    assert result["ok"] is False
    assert result["blocked"]["error_code"] == "crm_gateway_attestation_exit_status_mismatch"


def test_cli_exposes_gateway_attestation_without_default_apply() -> None:
    parsed = build_parser().parse_args(["crm-gateway-attest", "inventory", "--run-id", RUN_ID])

    assert parsed.command == "crm-gateway-attest"
    assert parsed.action == "inventory"
    assert parsed.apply_synthetic is False
