from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from autostop_manager.integration_audit import (
    EXPECTED_GATEWAY_TOOLS,
    EXPECTED_WEB_CAPABILITIES,
    GMAIL_PROOF_FORMAT,
    audit_docs_runtime_contract,
    audit_gmail_connector,
    build_integration_audit,
)


def _write_catalog(root) -> None:
    path = root / "docs" / "agent" / "crm_mcp_catalog.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "production_tools_verified": sorted(EXPECTED_GATEWAY_TOOLS),
                "agent_gateway_v2": {"web_research_capabilities": sorted(EXPECTED_WEB_CAPABILITIES)},
                "tool_counts": {"autostop_manager_tools_in_raw_registry": 72},
                "tool_families": {
                    "optional_manager_memory_and_routing": [
                        "store_owner_capabilities",
                        "store_owner_api",
                    ]
                },
            }
        ),
        encoding="utf-8",
    )


def _write_gmail_runtime(plugin_root, proof_path) -> None:
    plugin_root.mkdir(parents=True)
    (plugin_root / ".codex-remote-plugin-install.json").write_text("{}", encoding="utf-8")
    skill = plugin_root / "1.0.0" / "skills" / "gmail" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Gmail", encoding="utf-8")
    proof_path.parent.mkdir(parents=True, exist_ok=True)
    proof_path.write_text(
        json.dumps(
            {
                "format": GMAIL_PROOF_FORMAT,
                "ok": True,
                "generated_at": datetime.now(UTC).isoformat(),
                "checks": {
                    "profile_read": True,
                    "labels_read": True,
                    "search_read": True,
                    "self_delivery_readback": True,
                    "self_delivery_cleanup": True,
                },
            }
        ),
        encoding="utf-8",
    )


def _write_parity_checker(root, name) -> None:
    script = root / "scripts" / name
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("", encoding="utf-8")


def _parity_payload(format_name: str) -> dict:
    return {
        "ok": True,
        "format": format_name,
        "summary": {
            "actions": 10,
            "covered": 8,
            "gaps": 0,
            "intentional_exemptions": 2,
            "parity_complete": True,
            "inventory_valid": True,
        },
    }


def test_docs_runtime_contract_requires_exact_tools_and_web_capabilities(tmp_path):
    _write_catalog(tmp_path)

    result = audit_docs_runtime_contract(tmp_path)

    assert result["ok"] is True
    assert all(result["checks"].values())


def test_gmail_connector_full_mode_requires_fresh_ref_only_proof(tmp_path):
    plugin_root = tmp_path / "gmail"
    proof_path = tmp_path / "gmail-proof.json"
    _write_gmail_runtime(plugin_root, proof_path)

    result = audit_gmail_connector(
        plugin_root=plugin_root,
        proof_path=proof_path,
        require_live_proof=True,
        max_age=timedelta(days=30),
    )

    assert result["ok"] is True
    assert result["proof"]["required_checks_passed"] is True


def test_full_integration_audit_runs_local_and_public_without_exposing_token(tmp_path):
    manager_root = tmp_path / "manager"
    crm_root = tmp_path / "crm"
    store_root = tmp_path / "store"
    plugin_root = tmp_path / "gmail"
    proof_path = tmp_path / "gmail-proof.json"
    _write_catalog(manager_root)
    _write_gmail_runtime(plugin_root, proof_path)
    python = crm_root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    script = crm_root / "scripts" / "check_agent_gateway_v2.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    _write_parity_checker(crm_root, "crm_capability_parity.py")
    _write_parity_checker(store_root, "store_capability_parity.py")
    (crm_root / ".env").write_text("MINIMAL_KANBAN_MCP_BEARER_TOKEN=test-secret\n", encoding="utf-8")
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command[1].endswith("crm_capability_parity.py"):
            payload = _parity_payload("autostopcrm_capability_parity_v1")
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        if command[1].endswith("store_capability_parity.py"):
            payload = _parity_payload("autostop_store_capability_parity_v1")
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        payload = {
            "ok": True,
            "checks": {"tool_count_exactly_24": True, "search_web_multi_call_ok": True},
            "metrics": {"tool_count": 24},
            "failed_invocations": [],
        }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    result = build_integration_audit(
        full=True,
        crm_root=crm_root,
        store_root=store_root,
        manager_root=manager_root,
        gmail_plugin_root=plugin_root,
        gmail_proof_path=proof_path,
        command_runner=runner,
    )

    assert result["ok"] is True
    assert len(calls) == 4
    gateway_calls = [(command, kwargs) for command, kwargs in calls if "--mcp-url" in command]
    assert len(gateway_calls) == 2
    assert all("--require-store" in command and "--require-web" in command for command, _ in gateway_calls)
    assert all("--exhaustive" in command for command, _ in gateway_calls)
    assert all(kwargs["env"]["MINIMAL_KANBAN_MCP_BEARER_TOKEN"] == "test-secret" for _, kwargs in gateway_calls)
    assert "test-secret" not in json.dumps(result)


def test_gateway_timeout_fails_closed_without_subprocess_details(tmp_path):
    manager_root = tmp_path / "manager"
    crm_root = tmp_path / "crm"
    plugin_root = tmp_path / "gmail"
    store_root = tmp_path / "store"
    _write_catalog(manager_root)
    _write_gmail_runtime(plugin_root, tmp_path / "proof.json")
    python = crm_root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    script = crm_root / "scripts" / "check_agent_gateway_v2.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    _write_parity_checker(crm_root, "crm_capability_parity.py")
    _write_parity_checker(store_root, "store_capability_parity.py")
    (crm_root / ".env").write_text("MINIMAL_KANBAN_MCP_BEARER_TOKEN=hidden\n", encoding="utf-8")

    def timeout_runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output="sensitive-output")

    result = build_integration_audit(
        crm_root=crm_root,
        store_root=store_root,
        manager_root=manager_root,
        gmail_plugin_root=plugin_root,
        gmail_proof_path=tmp_path / "proof.json",
        command_runner=timeout_runner,
    )

    assert result["ok"] is False
    assert result["checks"]["gateway_local"]["error"] == "crm_gateway_checker_failed_to_run"
    assert result["checks"]["gateway_local"]["attempts"] == 2
    assert "sensitive-output" not in json.dumps(result)


def test_gateway_check_retries_once_and_reports_safe_recovery(tmp_path):
    manager_root = tmp_path / "manager"
    crm_root = tmp_path / "crm"
    plugin_root = tmp_path / "gmail"
    store_root = tmp_path / "store"
    proof_path = tmp_path / "gmail-proof.json"
    _write_catalog(manager_root)
    _write_gmail_runtime(plugin_root, proof_path)
    python = crm_root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    script = crm_root / "scripts" / "check_agent_gateway_v2.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    _write_parity_checker(crm_root, "crm_capability_parity.py")
    _write_parity_checker(store_root, "store_capability_parity.py")
    (crm_root / ".env").write_text("MINIMAL_KANBAN_MCP_BEARER_TOKEN=retry-secret\n", encoding="utf-8")
    calls = 0

    def runner(command, **kwargs):
        nonlocal calls
        if command[1].endswith("crm_capability_parity.py"):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(_parity_payload("autostopcrm_capability_parity_v1")),
                stderr="",
            )
        if command[1].endswith("store_capability_parity.py"):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(_parity_payload("autostop_store_capability_parity_v1")),
                stderr="",
            )
        calls += 1
        if calls == 1:
            payload = {
                "ok": False,
                "error": "token environment variable is missing: PRIVATE_NAME",
            }
            return subprocess.CompletedProcess(command, 2, stdout=json.dumps(payload), stderr="")
        payload = {
            "ok": True,
            "checks": {"tool_count_exactly_24": True},
            "metrics": {"tool_count": 24},
            "failed_invocations": [],
        }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    result = build_integration_audit(
        crm_root=crm_root,
        store_root=store_root,
        manager_root=manager_root,
        gmail_plugin_root=plugin_root,
        gmail_proof_path=proof_path,
        command_runner=runner,
    )

    gateway = result["checks"]["gateway_local"]
    assert result["ok"] is True
    assert calls == 2
    assert gateway["attempts"] == 2
    assert gateway["recovered_after_retry"] is True
    assert gateway["warnings"] == ["crm_gateway_check_recovered_after_retry"]
    assert "retry-secret" not in json.dumps(result)


def test_systemd_monitor_is_hardened_and_hourly():
    root = Path(__file__).resolve().parents[1]
    service = (root / "deploy/systemd/autostop-integration-audit.service").read_text()
    timer = (root / "deploy/systemd/autostop-integration-audit.timer").read_text()
    installer = (root / "scripts/install-integration-audit-timer.sh").read_text()

    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=strict" in service
    assert "--output /var/lib/autostop-manager/integration/latest.json" in service
    assert "OnUnitActiveSec=1h" in timer
    assert "Persistent=true" in timer
    assert "systemctl enable --now autostop-integration-audit.timer" in installer
    assert "WorkingDirectory=/opt/autostop-manager-releases/current" in service
    assert "WorkingDirectory=/opt/AutostopManager" not in service
