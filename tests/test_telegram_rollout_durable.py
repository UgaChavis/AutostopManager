"""Check duty and rollback behavior with disposable service and bridge mocks."""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

from telegram_duty_fakes import duty_fixture

ROOT = Path(__file__).resolve().parents[1]


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
    shell = "systemctl() { echo unexpected_service_mutation; };\n" + function + "\nrollback\n"
    result = subprocess.run(
        ["bash", "-c", shell],
        env={**os.environ, "account": "work", "previous_release": str(old), "service_unit": "fake.service"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "durable_inbox_rollback_incompatible=true" in result.stderr
    assert "unexpected_service_mutation" not in result.stdout


def test_status_sanitizes_non_string_execution_states(tmp_path):
    script, env, *_ = duty_fixture(tmp_path, {"ok": True, "execution_state": [], "recovery_state": {}})
    result = subprocess.run(["bash", str(script), "--status"], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["execution_state"] == "unavailable"
