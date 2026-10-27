"""Check duty and rollback behavior with disposable service and bridge mocks."""

import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys

from telegram_duty_fakes import duty_fixture

ROOT = Path(__file__).resolve().parents[1]


def test_work_bridge_ready_uses_release_not_cwd_after_sudo_environment_reset(tmp_path):
    source = (ROOT / "scripts/deploy_telegram_bridge.sh").read_text()
    match = re.search(r"(?ms)^bridge_ready\(\) \{\n.*?^\}", source)
    assert match is not None
    release = tmp_path / "release"
    package = release / "autostop_manager"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "telegram_bridge.py").write_text(
        'print(\'{"authorized":true,"transport_ready":true,"inbound_enabled":true}\')\n'
    )
    hostile = tmp_path / "cwd"
    shadow = hostile / "autostop_manager"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("")
    marker = tmp_path / "unsafe-import"
    (shadow / "telegram_bridge.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise SystemExit(1)\n"
    )
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    venv_python.chmod(0o755)
    shell = (
        "systemctl() { return 0; };\n"
        'sudo() { shift 2; /usr/bin/env -i PATH="$PATH" "$@"; };\n' + match[0] + "\nbridge_ready\n"
    )
    result = subprocess.run(
        ["bash", "-c", shell],
        cwd=hostile,
        env={
            **os.environ,
            "account": "work",
            "service_unit": "fake.service",
            "service_user": "fake-user",
            "current_link": str(release),
            "venv_root": str(venv_python.parents[1]),
            "work_inbound_expected": "1",
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_durable_enable_keeps_intake_when_execution_is_held(tmp_path):
    script, env, _, _, log = duty_fixture(
        tmp_path,
        {
            "ok": True,
            "enabled": False,
            "connected": True,
            "ready": False,
            "retention": "durable_refs",
            "recovery_state": "held",
            "execution_state": "held",
            "needs_review": 1,
        },
    )
    source = tmp_path / "releases/revision/autostop_manager"
    source.mkdir()
    (source / "telegram_inbox.py").write_text("")
    bridge = tmp_path / "bin/bridge-python"
    bridge.write_text(
        '#!/bin/sh\nif [ "$1" = -m ] && [ "$2" = autostop_manager.telegram_bridge ]; then\n'
        '  printf "%s\\n" "$*" >> "$FAKE_LOG"\n'
        "  printf '%s\\n' '{\"ok\":true}'\n"
        "else\n  exec " + shlex.quote(sys.executable) + ' "$@"\nfi\n'
    )
    bridge.chmod(0o755)
    script.write_text(script.read_text().replace(f'venv_python="{sys.executable}"', f'venv_python="{bridge}"'))
    status = json.loads(env["BRIDGE_STATE"])
    status["retention"] = "durable_refs"
    env["BRIDGE_STATE"] = json.dumps(status)
    result = subprocess.run(["bash", str(script), "--enable"], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "retention=durable_refs" in result.stdout
    calls = log.read_text()
    assert "duty-set --enabled" in calls
    assert "restart" not in calls and "stop " not in calls


def test_rollback_refuses_ram_only_release_before_stopping_current_bridge(tmp_path):
    source = (ROOT / "scripts/deploy_telegram_bridge.sh").read_text()
    match = re.search(r"(?ms)^rollback\(\) \{\n.*?^\}", source)
    assert match is not None
    database = tmp_path / "inbound.sqlite3"
    database.write_text("")
    old = tmp_path / "old"
    old.mkdir()
    function = match[0].replace("/var/lib/autostop-work-telegram/inbound.sqlite3", str(database))
    helper = re.search(r"(?ms)^durable_inbox_compatible\(\) \{\n.*?^\}", source)
    assert helper is not None
    shell = (
        "systemctl() { echo unexpected_service_mutation; };\n"
        + helper[0].replace("/var/lib/autostop-work-telegram/inbound.sqlite3", str(database))
        + "\n"
        + function
        + "\nrollback\n"
    )
    result = subprocess.run(
        ["bash", "-c", shell],
        env={**os.environ, "account": "work", "previous_release": str(old), "service_unit": "fake.service"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "durable_inbox_rollback_incompatible=true" in result.stderr
    assert "unexpected_service_mutation" not in result.stdout


def test_durable_compatibility_ignores_cwd_and_pythonpath(tmp_path):
    source = (ROOT / "scripts/deploy_telegram_bridge.sh").read_text()
    helper = re.search(r"(?ms)^durable_inbox_compatible\(\) \{\n.*?^\}", source)
    assert helper is not None
    database = tmp_path / "inbound.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        connection.execute("INSERT INTO metadata VALUES ('schema_version','2')")
    package = tmp_path / "candidate/autostop_manager"
    package.mkdir(parents=True)
    module = package / "telegram_inbox.py"
    hostile = tmp_path / "cwd"
    hostile.mkdir()
    clean = tmp_path / "clean"
    clean.mkdir()
    marker = tmp_path / "unsafe-import"
    (hostile / "ast.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise SystemExit(0)\n")
    shell = helper[0].replace("/var/lib/autostop-work-telegram/inbound.sqlite3", str(database))
    for cwd, pythonpath in ((hostile, ""), (clean, str(hostile))):
        env = {**os.environ, "PYTHONPATH": pythonpath}
        env.pop("PYTHONSAFEPATH", None)
        for versions, expected in (((1, 2), 0), ((1,), 1)):
            module.write_text(f"SUPPORTED_INBOX_SCHEMA_VERSIONS = {versions!r}\n")
            result = subprocess.run(
                ["bash", "-c", shell + '\ndurable_inbox_compatible "$1"\n', "test", str(package.parent)],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == expected, result.stderr
            assert not marker.exists()
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == "2"


def test_rollback_checks_registry_version_before_mutating_a_durable_pair(tmp_path):
    source = (ROOT / "scripts/deploy_telegram_bridge.sh").read_text()
    helper = re.search(r"(?ms)^durable_inbox_compatible\(\) \{\n.*?^\}", source)
    rollback = re.search(r"(?ms)^rollback\(\) \{\n.*?^\}", source)
    assert helper is not None and rollback is not None
    database = tmp_path / "inbound.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        connection.execute("INSERT INTO metadata VALUES ('schema_version','2')")
        connection.execute("INSERT INTO metadata VALUES ('execution_paused','1')")
    old = tmp_path / "old"
    package = old / "autostop_manager"
    package.mkdir(parents=True)
    module = package / "telegram_inbox.py"
    helper_body = helper[0].replace("/var/lib/autostop-work-telegram/inbound.sqlite3", str(database))
    shell = (
        "systemctl() { echo service_mutation; };\n"
        "restore_previous_release_assets() { echo assets_restored; };\n"
        "bridge_ready() { return 0; };\n" + helper_body + "\n" + rollback[0] + "\nrollback\n"
    )
    environment = {**os.environ, "account": "work", "previous_release": str(old), "service_unit": "fake.service"}
    for contract in ("# historical durable module, no version contract\n", "SUPPORTED_INBOX_SCHEMA_VERSIONS = (1,)\n"):
        module.write_text(contract)
        rejected = subprocess.run(["bash", "-c", shell], env=environment, capture_output=True, text=True)
        assert rejected.returncode == 1
        assert "durable_inbox_rollback_incompatible=true" in rejected.stderr
        assert "service_mutation" not in rejected.stdout and "assets_restored" not in rejected.stdout
    module.write_text("SUPPORTED_INBOX_SCHEMA_VERSIONS = (1, 2)\n")
    accepted = subprocess.run(["bash", "-c", shell], env=environment, capture_output=True, text=True)
    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout.count("service_mutation") == 2 and "assets_restored" in accepted.stdout
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT value FROM metadata WHERE key='execution_paused'").fetchone()[0] == "1"


def test_status_sanitizes_non_string_execution_states(tmp_path):
    script, env, *_ = duty_fixture(tmp_path, {"ok": True, "execution_state": [], "recovery_state": {}})
    result = subprocess.run(["bash", str(script), "--status"], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["execution_state"] == "unavailable"


def test_explicit_downgrade_is_rejected_before_current_service_stops(tmp_path):
    source = (ROOT / "scripts/deploy_telegram_bridge.sh").read_text()
    helper = re.search(r"(?ms)^durable_inbox_compatible\(\) \{\n.*?^\}", source)
    assert helper is not None
    start = source.index('if [[ "${account}" == "work" ]] && ! durable_inbox_compatible "${release_dir}"; then')
    stop = source.index("if ! activate_new_release_assets; then", start)
    database = tmp_path / "inbound.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        connection.execute("INSERT INTO metadata VALUES ('schema_version','2')")
    candidate = tmp_path / "candidate/autostop_manager"
    candidate.mkdir(parents=True)
    (candidate / "telegram_inbox.py").write_text("SUPPORTED_INBOX_SCHEMA_VERSIONS = (1,)\n")
    shell = (
        "systemctl() { echo unexpected_service_mutation; };\n"
        + helper[0].replace("/var/lib/autostop-work-telegram/inbound.sqlite3", str(database))
        + "\n"
        + source[start:stop]
    )
    result = subprocess.run(
        ["bash", "-c", shell],
        env={
            **os.environ,
            "account": "work",
            "release_dir": str(candidate.parent),
            "work_service_was_active": "1",
            "service_unit": "fake.service",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1 and "durable_inbox_candidate_incompatible=true" in result.stderr
    assert "unexpected_service_mutation" not in result.stdout
