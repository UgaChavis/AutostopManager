from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT


INTEGRATION_AUDIT_FORMAT = "autostop_integration_audit_v1"
GMAIL_PROOF_FORMAT = "autostop_gmail_integration_proof_v1"
DEFAULT_CRM_ROOT = Path("/opt/autostopcrm")
DEFAULT_GMAIL_PLUGIN_ROOT = Path("/root/.codex/plugins/cache/openai-curated-remote/gmail")
DEFAULT_GMAIL_PROOF_PATH = Path("/var/lib/autostop-manager/integration/gmail-proof.json")
EXPECTED_GATEWAY_TOOLS = frozenset(
    {
        "agent_board_digest",
        "agent_board_workflow",
        "agent_bootstrap",
        "agent_document_workflow",
        "agent_entity_context",
        "agent_finance_workflow",
        "agent_inventory_workflow",
        "agent_search",
        "call_raw_capability",
        "complete_external_step",
        "discover_raw_capabilities",
        "get_connector_identity",
        "get_raw_capability_schema",
        "get_runtime_status",
        "list_agent_workflows",
        "ping_connector",
        "prepare_action_contract",
        "start_workflow",
        "workflow_cancel",
        "workflow_checkpoint",
        "workflow_resume",
        "workflow_status",
        "workflow_transition",
        "workflow_wait_for_external",
    }
)
EXPECTED_WEB_CAPABILITIES = frozenset({"search_web_multi", "fetch_page_excerpt", "fetch_page_browser"})
_GMAIL_REQUIRED_PROOF_CHECKS = frozenset(
    {"profile_read", "labels_read", "search_read", "self_delivery_readback", "self_delivery_cleanup"}
)


def build_integration_audit(
    *,
    full: bool = False,
    crm_root: Path | str = DEFAULT_CRM_ROOT,
    manager_root: Path | str = PROJECT_ROOT,
    local_mcp_url: str = "http://127.0.0.1:8001/mcp",
    public_mcp_url: str = "https://crm.autostopcrm.ru/mcp",
    gmail_plugin_root: Path | str = DEFAULT_GMAIL_PLUGIN_ROOT,
    gmail_proof_path: Path | str = DEFAULT_GMAIL_PROOF_PATH,
    gmail_proof_max_age_days: int = 30,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    started = time.monotonic()
    crm_path = Path(crm_root)
    manager_path = Path(manager_root)
    checks: dict[str, dict[str, Any]] = {}

    checks["docs_runtime_contract"] = audit_docs_runtime_contract(manager_path)
    checks["gmail_connector"] = audit_gmail_connector(
        plugin_root=Path(gmail_plugin_root),
        proof_path=Path(gmail_proof_path),
        require_live_proof=full,
        max_age=timedelta(days=max(1, int(gmail_proof_max_age_days))),
    )

    token = _read_env_value(crm_path / ".env", "MINIMAL_KANBAN_MCP_BEARER_TOKEN")
    if not token:
        checks["gateway_local"] = _failed_check("crm_gateway_token_missing")
    else:
        checks["gateway_local"] = _run_gateway_check(
            crm_path=crm_path,
            mcp_url=local_mcp_url,
            token=token,
            exhaustive=full,
            command_runner=command_runner,
        )
        if full:
            checks["gateway_public"] = _run_gateway_check(
                crm_path=crm_path,
                mcp_url=public_mcp_url,
                token=token,
                exhaustive=True,
                command_runner=command_runner,
            )

    failed = sorted(name for name, payload in checks.items() if not payload.get("ok"))
    return {
        "ok": not failed,
        "format": INTEGRATION_AUDIT_FORMAT,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "full" if full else "quick",
        "summary": {
            "checks_total": len(checks),
            "checks_passed": len(checks) - len(failed),
            "checks_failed": len(failed),
            "failed_checks": failed,
            "duration_ms": round((time.monotonic() - started) * 1000),
        },
        "checks": checks,
        "data_included": False,
    }


def audit_docs_runtime_contract(manager_root: Path) -> dict[str, Any]:
    catalog_path = manager_root / "docs" / "agent" / "crm_mcp_catalog.json"
    try:
        payload = json.loads(catalog_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _failed_check("crm_mcp_catalog_unreadable")
    tools = payload.get("production_tools_verified")
    gateway = payload.get("agent_gateway_v2")
    documented_web = gateway.get("web_research_capabilities") if isinstance(gateway, dict) else None
    tool_set = {str(item) for item in tools} if isinstance(tools, list) else set()
    web_set = {str(item) for item in documented_web} if isinstance(documented_web, list) else set()
    validations = {
        "visible_tool_count_exactly_24": len(tool_set) == 24,
        "visible_tool_names_exact": tool_set == EXPECTED_GATEWAY_TOOLS,
        "web_capabilities_documented": web_set == EXPECTED_WEB_CAPABILITIES,
    }
    return {
        "ok": all(validations.values()),
        "status": "healthy" if all(validations.values()) else "failed",
        "checks": validations,
        "warnings": sorted(name for name, ok in validations.items() if not ok),
    }


def audit_gmail_connector(
    *,
    plugin_root: Path,
    proof_path: Path,
    require_live_proof: bool,
    max_age: timedelta,
) -> dict[str, Any]:
    install_metadata = plugin_root / ".codex-remote-plugin-install.json"
    skill_files = sorted(plugin_root.glob("*/skills/gmail/SKILL.md"))
    checks = {
        "plugin_install_metadata_present": install_metadata.is_file(),
        "gmail_skill_present": any(path.is_file() for path in skill_files),
    }
    warnings: list[str] = []
    proof_summary = {"required": require_live_proof, "present": proof_path.is_file()}
    if proof_path.is_file():
        proof = _read_gmail_proof(proof_path, max_age=max_age)
        proof_summary.update(proof)
        checks["live_proof_valid"] = bool(proof.get("ok"))
    elif require_live_proof:
        checks["live_proof_valid"] = False
        warnings.append("gmail_live_proof_missing")
    else:
        warnings.append("gmail_live_proof_not_required_in_quick_mode")
    ok = all(checks.values())
    return {
        "ok": ok,
        "status": "healthy" if ok else "failed",
        "checks": checks,
        "proof": proof_summary,
        "warnings": warnings,
    }


def _read_gmail_proof(path: Path, *, max_age: timedelta) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        generated_at = datetime.fromisoformat(str(payload.get("generated_at") or ""))
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=UTC)
        age = datetime.now(UTC) - generated_at.astimezone(UTC)
        proof_checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
        required_ok = all(bool(proof_checks.get(name)) for name in _GMAIL_REQUIRED_PROOF_CHECKS)
        valid = (
            payload.get("format") == GMAIL_PROOF_FORMAT
            and payload.get("ok") is True
            and required_ok
            and timedelta(0) <= age <= max_age
        )
        return {
            "ok": valid,
            "age_seconds": max(0, round(age.total_seconds())),
            "required_checks_passed": required_ok,
        }
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return {"ok": False, "error": "gmail_live_proof_invalid"}


def _run_gateway_check(
    *,
    crm_path: Path,
    mcp_url: str,
    token: str,
    exhaustive: bool,
    command_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, Any]:
    python = crm_path / ".venv" / "bin" / "python"
    script = crm_path / "scripts" / "check_agent_gateway_v2.py"
    if not python.is_file() or not script.is_file():
        return _failed_check("crm_gateway_checker_missing")
    command = [
        str(python),
        str(script),
        "--mcp-url",
        mcp_url,
        "--require-store",
        "--require-web",
    ]
    if exhaustive:
        command.append("--exhaustive")
    environment = os.environ.copy()
    environment["MINIMAL_KANBAN_MCP_BEARER_TOKEN"] = token
    audit_started = time.monotonic()
    last_result = _failed_check("crm_gateway_check_failed")
    for attempt in (1, 2):
        try:
            completed = command_runner(
                command,
                cwd=crm_path,
                env=environment,
                capture_output=True,
                text=True,
                timeout=180 if exhaustive else 90,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            last_result = _failed_check("crm_gateway_checker_failed_to_run")
        else:
            try:
                payload = json.loads(completed.stdout)
            except (TypeError, json.JSONDecodeError):
                last_result = {
                    **_failed_check("crm_gateway_checker_invalid_output"),
                    "exit_code": completed.returncode,
                }
            else:
                safe_checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
                safe_metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
                failed_invocations = [str(item) for item in payload.get("failed_invocations") or []]
                ok = completed.returncode == 0 and payload.get("ok") is True
                last_result = {
                    "ok": ok,
                    "status": "healthy" if ok else "failed",
                    "exit_code": completed.returncode,
                    "checks": {str(name): bool(value) for name, value in safe_checks.items()},
                    "metrics": {
                        str(name): value
                        for name, value in safe_metrics.items()
                        if isinstance(value, (int, float, bool)) and not isinstance(value, str)
                    },
                    "failed_invocations": failed_invocations,
                    "data_included": False,
                }
                if not ok:
                    error_code = _gateway_checker_error_code(
                        payload,
                        returncode=completed.returncode,
                        failed_invocations=failed_invocations,
                    )
                    last_result["error"] = error_code
                    last_result["warnings"] = [error_code]
        if last_result.get("ok"):
            last_result["attempts"] = attempt
            last_result["recovered_after_retry"] = attempt > 1
            if attempt > 1:
                last_result["warnings"] = ["crm_gateway_check_recovered_after_retry"]
            break
    last_result["duration_ms"] = round((time.monotonic() - audit_started) * 1000)
    last_result.setdefault("attempts", 2)
    last_result.setdefault("recovered_after_retry", False)
    return last_result


def _gateway_checker_error_code(
    payload: dict[str, Any],
    *,
    returncode: int,
    failed_invocations: list[str],
) -> str:
    error = str(payload.get("error") or "").strip().casefold()
    if error.startswith("token environment variable is missing"):
        return "crm_gateway_checker_token_environment_missing"
    if failed_invocations:
        return "crm_gateway_tool_invocation_failed"
    if returncode != 0 and not payload.get("checks"):
        return "crm_gateway_checker_reported_failure"
    return "crm_gateway_check_failed"


def _read_env_value(path: Path, key: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return ""
    prefix = f"{key}="
    for line in lines:
        if not line.startswith(prefix):
            continue
        value = line[len(prefix) :].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value
    return ""


def _failed_check(error: str) -> dict[str, Any]:
    return {"ok": False, "status": "failed", "error": error, "warnings": [error]}


__all__ = [
    "GMAIL_PROOF_FORMAT",
    "INTEGRATION_AUDIT_FORMAT",
    "audit_docs_runtime_contract",
    "audit_gmail_connector",
    "build_integration_audit",
]
