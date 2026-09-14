from __future__ import annotations

from pathlib import Path

from autostop_manager import control_center as control_center_module
from autostop_manager.control_center import (
    REQUIRED_CORE_TOOLS,
    build_control_report,
    format_control_report_markdown,
    _env_file_status,
    _redact_text,
)
from autostop_manager.knowledge_base import sync_knowledge_base
from autostop_manager.storage import ManagerMemoryStore


def test_control_report_schema_and_markdown(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    sync_knowledge_base(store)

    report = build_control_report(store=store)

    assert report["schema"] == "ControlReportV1"
    assert report["privacy"]["secrets_redacted"] is True
    assert report["privacy"]["crm_writes"] is False
    assert "git" in report
    assert "providers" in report
    assert "provider_readiness" in report
    assert "server_environment" in report
    assert "codex_readiness" in report
    assert "runtime" in report["codex_readiness"]
    assert "stale_app_server_processes" in report["codex_readiness"]["runtime"]
    assert "runtime_readiness" in report
    assert "production_ops" in report
    assert "work_telegram_bridge" in report["production_ops"]
    assert "open_risk" in report
    assert "risks" in report
    assert report["summary"]["knowledge_documents"] > 0
    assert report["knowledge"]["section_count"] > 0
    assert report["mcp"]["manager"]["tool_count"] > 0
    assert report["mcp"]["crm"]["tool_count"] > 0
    assert report["server_environment"]["core_tools"]["required_present_count"] <= len(REQUIRED_CORE_TOOLS)
    assert report["provider_readiness"]["safety"]["orders_blocked"] is True
    assert report["provider_readiness"]["safety"]["basket_blocked"] is True
    assert report["production_ops"]["forbidden_without_explicit_owner_command"]

    markdown = format_control_report_markdown(report)
    assert "AutoStopManager Control Report" in markdown
    assert "Server Environment" in markdown
    assert "Runtime Readiness" in markdown
    assert "Stale app-server processes" in markdown
    assert "Production Ops" in markdown
    assert "Work Telegram bridge" in markdown
    assert "Provider Matrix" in markdown
    assert "python -m autostop_manager.cli control-report" in markdown
    assert "autostop-" + "manager control-report" not in markdown
    assert ".venv/" + "bin/python" not in str(report["tests_doctor"]["commands"])


def test_control_report_redacts_secret_like_text():
    rendered = _redact_text(
        'OPENAI_API_KEY=sk-testsecret123456789 ghp_secretsecretsecret CRM_PASSWORD: hunter2 ROSSKO_KEY1: "quoted secret"'
    )

    assert "sk-testsecret" not in rendered
    assert "ghp_secret" not in rendered
    assert "hunter2" not in rendered
    assert "quoted secret" not in rendered
    assert "OPENAI_API_KEY=***" in rendered
    assert "CRM_PASSWORD: ***" in rendered
    assert "ROSSKO_KEY1: ***" in rendered


def test_env_file_status_reports_key_names_without_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("PARTSAPI_KEY=super-secret-value\nCRM_PASSWORD=hunter2\n", encoding="utf-8")

    status = _env_file_status(env_file)

    assert status["present"] is True
    assert status["key_names"] == ["CRM_PASSWORD", "PARTSAPI_KEY"]
    assert "super-secret-value" not in str(status)
    assert "hunter2" not in str(status)


def test_tmp_writable_uses_an_os_managed_temporary_file(monkeypatch):
    captured = {}

    class _TemporaryFile:
        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def write(self, value):
            captured["value"] = value

    def fake_temporary_file(**kwargs):
        captured["kwargs"] = kwargs
        return _TemporaryFile()

    monkeypatch.setattr(control_center_module.tempfile, "TemporaryFile", fake_temporary_file)

    assert control_center_module._tmp_writable() is True
    assert captured == {
        "kwargs": {"mode": "w", "encoding": "utf-8", "dir": "/tmp"},
        "value": "ok",
    }


def test_tmp_writable_reports_os_errors(monkeypatch):
    def fail_temporary_file(**_kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(control_center_module.tempfile, "TemporaryFile", fail_temporary_file)

    assert control_center_module._tmp_writable() is False


def test_codex_skill_inventory_tolerates_inaccessible_root(monkeypatch):
    class _InaccessibleRoot:
        def exists(self):
            raise PermissionError("blocked by runner sandbox")

        def __str__(self):
            return "/root/.codex/skills/.system"

    monkeypatch.setattr(control_center_module, "CODEX_SYSTEM_SKILLS_ROOT", _InaccessibleRoot())

    inventory = control_center_module._codex_skill_inventory()

    assert inventory["system_skills_readable"] is False
    assert inventory["system_skill_count"] == 0
    assert inventory["system_skills"] == []


def test_read_catalog_counts_handles_invalid_structure(tmp_path, monkeypatch):
    catalog_path = tmp_path / "manager_mcp_catalog.json"
    catalog_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(control_center_module, "MANAGER_MCP_CATALOG_PATH", catalog_path)

    result = control_center_module._read_catalog_counts(catalog_path)

    assert result["ok"] is False
    assert result["warnings"] == ["invalid_catalog_structure"]


def test_classify_ports_treats_known_autostop_listeners_as_expected():
    result = control_center_module._classify_ports(
        {
            "ok": True,
            "public_listeners": [
                {"local_address": "0.0.0.0:8080"},
                {"local_address": "0.0.0.0:47895"},
                {"local_address": "0.0.0.0:10443"},
                {"local_address": "172.19.0.1:2525"},
                {
                    "line": 'udp UNCONN 0 0 0.0.0.0:38166 0.0.0.0:* users:(("codex",pid=2812,fd=33))',
                    "local_address": "0.0.0.0:38166",
                },
                {"line": "udp UNCONN 0 0 0.0.0.0:35686 0.0.0.0:*", "local_address": "0.0.0.0:35686"},
            ],
            "local_listeners": [],
        }
    )

    assert result["review_public_count"] == 0
    assert {item["risk"] for item in result["classifications"]} == {
        "expected_public_http_alt",
        "expected_public_vpn",
        "expected_vpn_telegram_relay",
        "expected_docker_bridge_relay",
        "expected_codex_runtime_socket",
        "expected_transient_udp_socket",
    }


def test_missing_external_provider_access_is_not_server_open_risk():
    result = control_center_module._open_risk_score(
        git={"dirty": False},
        providers={"ok": True, "missing_provider_ids": ["vin17_api"]},
        server_environment={"ok": True, "ports": {"review_public_count": 0}},
        codex_readiness={"ok": True},
        runtime_readiness={"ok": True},
        production_ops={"ok": True},
        ports={"ok": True},
    )

    assert result == {"score": 0, "level": "green", "items": []}


_OWNER_DISABLED_WORK_TELEGRAM = {
    "ok": True,
    "load_state": "loaded",
    "active_state": "inactive",
    "sub_state": "dead",
    "unit_file_state": "disabled",
}


def _production_ops_with_watchdog(monkeypatch, tmp_path, *, timer, service, work_telegram=None):
    compose_path = tmp_path / "docker-compose.yml"
    monkeypatch.setattr(control_center_module, "_first_existing", lambda _paths: compose_path)
    monkeypatch.setattr(
        control_center_module,
        "_run",
        lambda _command, **_kwargs: {"returncode": 0, "stdout": "ok\n", "stderr": ""},
    )
    monkeypatch.setattr(
        control_center_module,
        "_container_status",
        lambda _name, **_kwargs: {"ok": True, "state": "running", "health": "healthy"},
    )
    statuses = {
        "autostopcrm-watchdog.timer": timer,
        "autostopcrm-watchdog.service": service,
        "autostop-work-telegram.service": work_telegram or _OWNER_DISABLED_WORK_TELEGRAM,
    }
    monkeypatch.setattr(
        control_center_module,
        "_systemd_unit_status",
        lambda unit, **_kwargs: statuses[unit],
    )
    return control_center_module._production_ops(tmp_path)


def test_production_ops_accepts_absent_watchdog_units(monkeypatch, tmp_path):
    absent = {"ok": False, "load_state": "not-found", "active_state": "inactive"}

    production_ops = _production_ops_with_watchdog(
        monkeypatch,
        tmp_path,
        timer=absent,
        service=absent,
    )
    production_health = control_center_module._production_health(Path(tmp_path), production_ops=production_ops)

    assert production_ops["ok"] is True
    assert production_ops["watchdog"]["policy"] == {
        "ok": True,
        "desired_state": "absent",
        "state": "absent",
        "absent_units": ["service", "timer"],
        "installed_units": [],
        "active_units": [],
    }
    assert production_ops["work_telegram_bridge"]["ok"] is True
    assert production_ops["work_telegram_bridge"]["state"] == "owner_disabled"
    assert production_ops["work_telegram_bridge"]["monitoring"] == "off"
    assert production_ops["warnings"] == []
    assert production_health["watchdog_timer_active"] is False
    assert production_health["watchdog_policy_ok"] is True
    assert production_health["watchdog_policy_state"] == "absent"
    assert "watchdog installation or enablement" in production_ops["forbidden_without_explicit_owner_command"]
    assert all("watchdog" not in gate["operation"] for gate in production_ops["safe_operation_gates"])


def test_work_telegram_lifecycle_flags_enabled_but_inactive(monkeypatch, tmp_path):
    enabled_but_inactive = {**_OWNER_DISABLED_WORK_TELEGRAM, "unit_file_state": "enabled"}
    lifecycle = control_center_module._work_telegram_bridge_lifecycle(enabled_but_inactive)
    orphan_worker = control_center_module._work_telegram_bridge_lifecycle(
        _OWNER_DISABLED_WORK_TELEGRAM,
        media_workers={"ok": True, "state": "active", "active_count": 1},
    )
    production_ops = _production_ops_with_watchdog(
        monkeypatch,
        tmp_path,
        timer={"ok": False, "load_state": "not-found", "active_state": "inactive"},
        service={"ok": False, "load_state": "not-found", "active_state": "inactive"},
        work_telegram=enabled_but_inactive,
    )
    assert (lifecycle["ok"], lifecycle["state"], lifecycle["monitoring"]) == (
        False,
        "enabled_but_inactive",
        "not_probed",
    )
    assert (orphan_worker["ok"], orphan_worker["state"]) == (False, "media_worker_active")
    assert (production_ops["ok"], production_ops["warnings"]) == (False, ["work_telegram_bridge_enabled_but_inactive"])


def test_work_media_worker_status_counts_a_deactivating_worker(monkeypatch, tmp_path):
    monkeypatch.setattr(control_center_module.shutil, "which", lambda _command: "/bin/systemctl")
    monkeypatch.setattr(
        control_center_module,
        "_run",
        lambda _command, **_kwargs: {
            "returncode": 0,
            "stdout": "autostop-work-telegram-media-1.service loaded deactivating stop\n",
            "stderr": "",
        },
    )

    assert control_center_module._work_telegram_media_worker_status(cwd=tmp_path) == {
        "ok": True,
        "state": "active",
        "active_count": 1,
    }


def test_production_ops_rejects_active_watchdog_and_adds_risk(monkeypatch, tmp_path):
    production_ops = _production_ops_with_watchdog(
        monkeypatch,
        tmp_path,
        timer={"ok": True, "load_state": "loaded", "active_state": "active"},
        service={"ok": True, "load_state": "loaded", "active_state": "inactive"},
    )

    assert production_ops["ok"] is False
    assert production_ops["watchdog"]["policy"]["state"] == "active"
    assert production_ops["watchdog"]["policy"]["active_units"] == ["timer"]
    assert production_ops["warnings"] == [
        "autostopcrm_watchdog_unit_active",
        "autostopcrm_watchdog_legacy_units_present",
    ]

    risk = control_center_module._open_risk_score(
        git={"dirty": False},
        providers={"ok": True},
        server_environment={"ok": True, "ports": {"review_public_count": 0}},
        codex_readiness={"ok": True},
        runtime_readiness={"ok": True},
        production_ops=production_ops,
        ports={"ok": True},
    )
    assert risk == {
        "score": 15,
        "level": "yellow",
        "items": [
            {
                "points": 15,
                "category": "production_ops",
                "reason": "production compose/nginx/watchdog/container readiness needs attention",
            }
        ],
    }


def test_production_ops_rejects_inactive_legacy_watchdog_units(monkeypatch, tmp_path):
    legacy_inactive = {"ok": True, "load_state": "loaded", "active_state": "inactive"}

    production_ops = _production_ops_with_watchdog(
        monkeypatch,
        tmp_path,
        timer=legacy_inactive,
        service=legacy_inactive,
    )

    assert production_ops["ok"] is False
    assert production_ops["watchdog"]["policy"]["state"] == "legacy_units_present"
    assert production_ops["watchdog"]["policy"]["active_units"] == []
    assert production_ops["warnings"] == ["autostopcrm_watchdog_legacy_units_present"]
