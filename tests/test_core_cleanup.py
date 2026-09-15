from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from autostop_manager import cli, diagnostics
from autostop_manager.catalog_adapters import catalog_provider_status
from autostop_manager.mcp_server import build_server
from autostop_manager.storage import StoreState

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hook_removal", ROOT / "scripts/remove-learning-hooks.py")
assert SPEC and SPEC.loader
hooks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hooks)


@pytest.fixture
def docs(tmp_path):
    for name in diagnostics.TEXT_DOCUMENTS:
        dest = tmp_path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, dest)
    return tmp_path


def test_documents_fit_budget_and_have_only_two_skills():
    assert diagnostics.audit_documentation()["ok"]
    assert len(list((ROOT / ".agents/skills").glob("*/SKILL.md"))) == 2


@pytest.mark.parametrize("fault", ["missing", "empty", "link", "outside", "large", "extra", "name", "description"])
def test_document_checks_fail_on_broken_instructions(docs, fault):
    path = docs / "AGENTS.md"
    if fault == "missing":
        path.unlink()
    elif fault == "empty":
        path.write_text("")
    elif fault == "link":
        path.write_text("[bad](missing.md)")
    elif fault == "outside":
        path.write_text("[bad](../elsewhere.md)")
    elif fault == "large":
        path.write_text("x" * 13000)
    elif fault == "extra":
        (docs / "docs/agent/extra.md").write_text("# extra")
    else:
        path = docs / diagnostics.TEXT_DOCUMENTS[1]
        path.write_text(path.read_text().replace(f"{fault}:", "wrong:", 1))
    assert not diagnostics.audit_documentation(docs)["ok"]


def test_compatibility_cli_checks_never_create_database(tmp_path, monkeypatch, capsys):
    db = tmp_path / "not-created.sqlite3"
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(db))
    for command in ("knowledge-sync", "knowledge-audit", "doctor"):
        assert cli.main([command]) == 0
        assert json.loads(capsys.readouterr().out)["ok"]
    assert not db.exists()


def test_cli_reports_real_failure(monkeypatch, capsys):
    monkeypatch.setattr(cli, "audit_documentation", lambda: {"ok": False})
    assert cli.main(["knowledge-sync"]) == 1
    assert not json.loads(capsys.readouterr().out)["ok"]


def test_retired_commands_are_not_available():
    for command in ("agent-brief", "knowledge-probe", "memory-review", "control-report", "integration-audit"):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args([command])


def test_integrations_only_run_when_requested(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "autostop_manager.integration_audit.build_integration_audit", lambda **kw: calls.append(kw) or {"ok": True}
    )
    monkeypatch.setattr(diagnostics, "watchdog_status", lambda: {"ok": True})
    assert diagnostics.diagnose()["ok"]
    assert calls == []
    assert not diagnostics.diagnose(full=True)["ok"]
    assert calls == []
    assert diagnostics.diagnose(integrations=True, full=True)["ok"]
    assert calls == [{"full": True}]


@pytest.mark.parametrize("state,code,expected", [("not-found", 0, True), ("loaded", 0, False), ("not-found", 1, False)])
def test_watchdog_has_one_absent_policy(monkeypatch, state, code, expected):
    monkeypatch.setattr(diagnostics.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=state, returncode=code))
    assert diagnostics.watchdog_status()["ok"] is expected


def test_watchdog_failure_is_not_healthy(monkeypatch):
    def fail(*a, **kw):
        raise OSError("offline")

    monkeypatch.setattr(diagnostics.subprocess, "run", fail)
    assert not diagnostics.watchdog_status()["ok"]


def test_new_database_contains_only_store_state(tmp_path):
    store = StoreState(tmp_path / "state.sqlite3")
    store.initialize()
    with store.connect() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {
        "sqlite_sequence",
        "manager_runs",
        "manager_run_events",
        "manager_run_external_steps",
        "store_checkpoints",
    }


def test_old_database_history_is_preserved_without_being_read(tmp_path):
    path = tmp_path / "old.sqlite3"
    retired = ("notes", "facts", "lessons", "tasks", "journal", "director_journal", "agent_turns", "agent_improvements")
    with sqlite3.connect(path) as conn:
        for table in retired:
            conn.execute(f'CREATE TABLE "{table}" (value TEXT)')
            conn.execute(f'INSERT INTO "{table}" VALUES (?)', ("synthetic",))
    StoreState(path).initialize()
    with sqlite3.connect(path) as conn:
        for table in retired:
            assert conn.execute(f'SELECT * FROM "{table}"').fetchall() == [("synthetic",)]


def test_retired_tools_and_providers_are_absent():
    tools = build_server()._tool_manager._tools
    assert len(tools) == 27
    assert (
        not {
            "remember",
            "agent_mode",
            "post_run_review",
            "agent_bootstrap",
            "start_workflow",
            "agent_case_resolver",
            "vin17_decode_vehicle",
        }
        & tools.keys()
    )
    assert {
        "store_quote_conductor",
        "prepare_action_contract",
        "partsapi_catalog_lookup",
        "exist_price_lookup",
    } <= tools.keys()
    ids = {p["source_id"] for p in catalog_provider_status()["providers"]}
    assert (
        not {
            "vin17_api",
            "fapi_catalog",
            "rossko",
            "autoeuro_api",
            "autopiter",
            "armtek",
            "zzap",
            "autopoisk",
            "partslink24_or_oem_epc",
        }
        & ids
    )


HOOK_CONFIG = """# retained comment
allow_managed_hooks_only = true
[features]
hooks = true
[hooks]
managed_dir = "/opt/autostop-managed-hooks"
[[hooks.Stop]]
matcher = "*"
hooks = [{ type = "command", command = "/opt/autostop-managed-hooks/run_learning_hook.sh" },
         { type = "command", command = "/opt/unrelated-hook", timeout = 3 }]
[[hooks.PreToolUse]]
hooks = [{ type = "command", command = "/opt/autostop-managed-hooks/run_learning_hook.sh" }]
[unrelated]
name = "preserve"
"""


def test_hook_migration_dry_run_apply_repeat_restore(tmp_path):
    config = tmp_path / "requirements.toml"
    config.write_text(HOOK_CONFIG)
    config.chmod(0o640)
    assert hooks.migrate(config)["change_needed"]
    assert config.read_text() == HOOK_CONFIG
    result = hooks.migrate(config, apply=True)
    backup = Path(result["backup"])
    assert backup.stat().st_mode & 0o777 == 0o600
    assert config.stat().st_mode & 0o777 == 0o640
    payload = hooks.tomllib.loads(config.read_text())
    assert payload["hooks"]["Stop"][0]["hooks"] == [{"type": "command", "command": "/opt/unrelated-hook", "timeout": 3}]
    assert "PreToolUse" not in payload["hooks"]
    assert payload["unrelated"] == {"name": "preserve"}
    assert config.read_text().startswith("# retained comment")
    assert not hooks.migrate(config, apply=True)["changed"]
    hooks.migrate(config, apply=True, restore=backup)
    assert config.read_text() == HOOK_CONFIG
    assert not hooks.migrate(config, apply=True, restore=backup)["changed"]


def test_restore_refuses_intervening_changes(tmp_path):
    config = tmp_path / "requirements.toml"
    config.write_text(HOOK_CONFIG)
    result = hooks.migrate(config, apply=True)
    config.write_text(config.read_text() + "\n# subsequent owner change\n")
    with pytest.raises(ValueError, match="configuration_changed_since"):
        hooks.migrate(config, apply=True, restore=Path(result["backup"]))


def test_hook_migration_refuses_symlinks(tmp_path):
    target = tmp_path / "target"
    target.write_text(HOOK_CONFIG)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="regular_file"):
        hooks.migrate(link, apply=True)
    assert target.read_text() == HOOK_CONFIG
