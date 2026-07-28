from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


GATEWAY_ATTESTATION_FORMAT = "gateway_attestation_v1"
DEFAULT_CRM_ROOT = Path("/opt/autostopcrm")
DEFAULT_MCP_URL = "http://127.0.0.1:8001/mcp"
DEFAULT_OUTPUT_ROOT = Path("/var/lib/autostop-manager/integration/gateway-attestation")
DEFAULT_TOKEN_NAME = "MINIMAL_KANBAN_MCP_BEARER_TOKEN"
RUN_ID_RE = re.compile(r"^AST-GWAT-[0-9]{8}T[0-9]{6}Z(?:-[A-Za-z0-9]{4,16})?$")
ALLOWED_ACTIONS = frozenset(
    {
        "inventory",
        "next",
        "resume",
        "case",
        "retry",
        "cleanup",
        "summary",
    }
)


def default_gateway_attestation_run_id() -> str:
    return datetime.now(UTC).strftime("AST-GWAT-%Y%m%dT%H%M%SZ")


def _read_env_value(path: Path, name: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return ""
    prefix = f"{name}="
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or not stripped.startswith(prefix):
            continue
        value = stripped[len(prefix) :].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value.strip()
    return ""


def build_gateway_attestation_command(
    *,
    action: str,
    run_id: str,
    crm_root: Path | str = DEFAULT_CRM_ROOT,
    mcp_url: str = DEFAULT_MCP_URL,
    output_root: Path | str = DEFAULT_OUTPUT_ROOT,
    case_id: str = "",
    apply_synthetic: bool = False,
    force: bool = False,
) -> list[str]:
    normalized_action = str(action or "").strip().casefold()
    if normalized_action not in ALLOWED_ACTIONS:
        raise ValueError("unsupported_gateway_attestation_action")
    if force and normalized_action not in {"inventory", "case"}:
        raise ValueError("gateway_attestation_force_action_invalid")
    if apply_synthetic and normalized_action in {
        "inventory",
        "cleanup",
        "summary",
    }:
        raise ValueError("gateway_attestation_apply_action_invalid")
    normalized_run_id = str(run_id or "").strip()
    if not RUN_ID_RE.fullmatch(normalized_run_id):
        raise ValueError("invalid_gateway_attestation_run_id")
    crm_path = Path(crm_root)
    command = [
        str(crm_path / ".venv" / "bin" / "python"),
        str(crm_path / "scripts" / "attest_agent_gateway_v2.py"),
        "--run-id",
        normalized_run_id,
        "--mcp-url",
        str(mcp_url),
        "--output-root",
        str(output_root),
    ]
    if normalized_action == "case":
        normalized_case = str(case_id or "").strip()
        if not normalized_case:
            raise ValueError("gateway_attestation_case_id_required")
        command.extend(["--case", normalized_case])
    else:
        command.append(f"--{normalized_action}")
    if apply_synthetic:
        command.append("--apply-synthetic")
    if force:
        command.append("--force-case" if normalized_action == "case" else "--force-inventory")
    return command


def run_gateway_attestation(
    *,
    action: str,
    run_id: str,
    crm_root: Path | str = DEFAULT_CRM_ROOT,
    mcp_url: str = DEFAULT_MCP_URL,
    output_root: Path | str = DEFAULT_OUTPUT_ROOT,
    case_id: str = "",
    apply_synthetic: bool = False,
    force: bool = False,
    timeout_seconds: int = 180,
    command_runner: Any = subprocess.run,
) -> dict[str, Any]:
    normalized_action = str(action or "").strip().casefold()
    crm_path = Path(crm_root)
    command = build_gateway_attestation_command(
        action=action,
        run_id=run_id,
        crm_root=crm_path,
        mcp_url=mcp_url,
        output_root=output_root,
        case_id=case_id,
        apply_synthetic=apply_synthetic,
        force=force,
    )
    python = Path(command[0])
    script = Path(command[1])
    if not python.is_file() or not script.is_file():
        return {
            "ok": False,
            "format": GATEWAY_ATTESTATION_FORMAT,
            "run_id": run_id,
            "status": "blocked_on_command",
            "blocked": {
                "case_id": case_id or action,
                "error_code": "crm_gateway_attestation_runner_missing",
                "classification": "routing",
            },
            "data_included": False,
        }
    token = _read_env_value(crm_path / ".env", DEFAULT_TOKEN_NAME)
    if not token and normalized_action != "summary":
        return {
            "ok": False,
            "format": GATEWAY_ATTESTATION_FORMAT,
            "run_id": run_id,
            "status": "blocked_on_command",
            "blocked": {
                "case_id": case_id or action,
                "error_code": "crm_gateway_token_missing",
                "classification": "transport_auth",
            },
            "data_included": False,
        }
    environment = os.environ.copy()
    if token:
        environment[DEFAULT_TOKEN_NAME] = token
    try:
        completed = command_runner(
            command,
            cwd=crm_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=max(30, min(int(timeout_seconds), 600)),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {
            "ok": False,
            "format": GATEWAY_ATTESTATION_FORMAT,
            "run_id": run_id,
            "status": "blocked_on_command",
            "blocked": {
                "case_id": case_id or action,
                "error_code": "crm_gateway_attestation_runner_failed",
                "classification": "transport_auth",
            },
            "data_included": False,
        }
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError):
        return {
            "ok": False,
            "format": GATEWAY_ATTESTATION_FORMAT,
            "run_id": run_id,
            "status": "blocked_on_command",
            "blocked": {
                "case_id": case_id or action,
                "error_code": "crm_gateway_attestation_output_invalid",
                "classification": "verification",
            },
            "data_included": False,
        }
    if (
        not isinstance(payload, dict)
        or payload.get("format") != GATEWAY_ATTESTATION_FORMAT
        or not isinstance(payload.get("ok"), bool)
        or str(payload.get("run_id") or "") != run_id
        or not str(payload.get("status") or "")
        or payload.get("data_included") is not False
    ):
        return {
            "ok": False,
            "format": GATEWAY_ATTESTATION_FORMAT,
            "run_id": run_id,
            "status": "blocked_on_command",
            "blocked": {
                "case_id": case_id or action,
                "error_code": "crm_gateway_attestation_contract_invalid",
                "classification": "schema",
            },
            "data_included": False,
        }
    if bool(payload.get("ok")) is not (completed.returncode == 0):
        return {
            "ok": False,
            "format": GATEWAY_ATTESTATION_FORMAT,
            "run_id": run_id,
            "status": "blocked_on_command",
            "blocked": {
                "case_id": case_id or normalized_action,
                "error_code": "crm_gateway_attestation_exit_status_mismatch",
                "classification": "verification",
            },
            "data_included": False,
        }
    payload["exit_code"] = completed.returncode
    payload["data_included"] = False
    return payload
