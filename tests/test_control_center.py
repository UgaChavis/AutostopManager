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
    assert "web_search_readiness" in report
    assert "web_page_readiness" in report
    assert "server_environment" in report
    assert "codex_readiness" in report
    assert "runtime" in report["codex_readiness"]
    assert "stale_app_server_processes" in report["codex_readiness"]["runtime"]
    assert "runtime_readiness" in report
    assert "production_ops" in report
    assert "open_risk" in report
    assert "risks" in report
    assert report["summary"]["knowledge_documents"] > 0
    assert report["knowledge"]["section_count"] > 0
    assert report["mcp"]["manager"]["tool_count"] > 0
    assert report["mcp"]["crm"]["tool_count"] > 0
    assert report["server_environment"]["core_tools"]["required_present_count"] <= len(REQUIRED_CORE_TOOLS)
    assert report["provider_readiness"]["safety"]["orders_blocked"] is True
    assert report["provider_readiness"]["safety"]["basket_blocked"] is True
    assert report["web_search_readiness"]["safety"]["secrets_redacted"] is True
    assert report["web_page_readiness"]["safety"]["secrets_redacted"] is True
    assert report["production_ops"]["forbidden_without_explicit_owner_command"]

    markdown = format_control_report_markdown(report)
    assert "AutoStopManager Control Report" in markdown
    assert "Server Environment" in markdown
    assert "Runtime Readiness" in markdown
    assert "Stale app-server processes" in markdown
    assert "Production Ops" in markdown
    assert "Provider Matrix" in markdown
    assert "Web search APIs configured" in markdown
    assert "Web page extractors configured" in markdown
    assert "python -m autostop_manager.cli control-report" in markdown
    assert "autostop-" + "manager control-report" not in markdown
    assert ".venv/" + "bin/python" not in str(report["tests_doctor"]["commands"])


def test_control_report_redacts_secret_like_text():
    rendered = _redact_text(
        'OPENAI_API_KEY=sk-testsecret123456789 ghp_secretsecretsecret CRM_PASSWORD: hunter2 ROSSKO_KEY1: "quoted secret" '
        "Authorization: Bearer bearer-secret Cookie: session=secret-value\n"
        "DATABASE_URL=postgresql://manager:db-secret@db/manager github_pat_long_secret_value"
    )

    assert "sk-testsecret" not in rendered
    assert "ghp_secret" not in rendered
    assert "hunter2" not in rendered
    assert "quoted secret" not in rendered
    assert "bearer-secret" not in rendered
    assert "session=secret-value" not in rendered
    assert "db-secret" not in rendered
    assert "github_pat_long_secret_value" not in rendered
    assert "OPENAI_API_KEY=***" in rendered
    assert "CRM_PASSWORD: ***" in rendered
    assert "ROSSKO_KEY1: ***" in rendered
    assert "Authorization: Bearer ***" in rendered
    assert "Cookie: ***" in rendered
    assert "postgresql://manager:***@db/manager" in rendered


def test_codex_skill_inventory_treats_inaccessible_optional_root_as_empty(tmp_path, monkeypatch):
    inaccessible = tmp_path / "inaccessible-system-skills"
    plugin_root = tmp_path / "plugin-cache"
    plugin_root.mkdir()
    original_is_dir = Path.is_dir

    def guarded_is_dir(path):
        if path == inaccessible:
            raise PermissionError("synthetic inaccessible Codex home")
        return original_is_dir(path)

    monkeypatch.setattr(control_center_module, "CODEX_SYSTEM_SKILLS_ROOT", inaccessible)
    monkeypatch.setattr(control_center_module, "CODEX_PLUGIN_CACHE_ROOT", plugin_root)
    monkeypatch.setattr(Path, "is_dir", guarded_is_dir)

    result = control_center_module._codex_skill_inventory()

    assert result["system_skill_count"] == 0
    assert result["plugin_skill_count"] == 0
    assert result["plugin_count"] == 0


def test_env_file_status_reports_key_names_without_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("PARTSAPI_KEY=super-secret-value\nCRM_PASSWORD=hunter2\n", encoding="utf-8")

    status = _env_file_status(env_file)

    assert status["present"] is True
    assert status["key_names"] == ["CRM_PASSWORD", "PARTSAPI_KEY"]
    assert "super-secret-value" not in str(status)
    assert "hunter2" not in str(status)


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
        providers={"ok": True, "missing_provider_ids": ["vin17_api", "emex"]},
        server_environment={"ok": True, "ports": {"review_public_count": 0}},
        codex_readiness={"ok": True},
        runtime_readiness={"ok": True},
        production_ops={"ok": True},
        ports={"ok": True},
    )

    assert result == {"score": 0, "level": "green", "items": []}
