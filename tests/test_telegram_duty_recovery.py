"""Exercise duty controls against disposable shell mocks, including non-root CI."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def duty_fixture(tmp_path, state):
    releases = tmp_path / "releases"
    release = releases / "revision"
    release.mkdir(parents=True)
    current = releases / "current"
    current.symlink_to(release)
    monitor = tmp_path / "monitor.env"
    intent = (
        "AUTOSTOP_WORK_TELEGRAM_MONITOR_INCOMING=1\n"
        "AUTOSTOP_WORK_TELEGRAM_WAKE_SOCKET=/run/autostop-codex-wake/wake.sock\n"
    )
    monitor.write_text(intent)
    config = tmp_path / "wake.json"
    config.write_text("{}")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls"
    mocks = {
        "wake-python": '#!/bin/sh\nprintf "%s\\n" "$WAKE_STATE"\n',
        "systemctl": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$FAKE_LOG"\nexit 0\n',
        "sudo": (
            '#!/bin/sh\nprintf "%s\\n" '
            "'"
            + json.dumps(
                {
                    "ok": True,
                    "transport_ready": True,
                    "inbound_enabled": True,
                    "enabled": True,
                    "retention": "memory_only",
                }
            )
            + "'\n"
        ),
        "stat": '#!/bin/sh\nprintf "%s\\n" root:root:644\n',
        "chown": "#!/bin/sh\nexit 0\n",
        "sleep": "#!/bin/sh\nexit 0\n",
    }
    for name, text in mocks.items():
        path = bin_dir / name
        path.write_text(text)
        path.chmod(0o755)
    source = (ROOT / "scripts/set-work-telegram-duty.sh").read_text()
    # Production's root gate and file ownership check are separate contracts;
    # replace only the root gate so the behavioral fixture also runs in CI.
    replacements = {
        'if [[ "${EUID}" -ne 0 ]]; then': "if false; then",
        'release_link="/opt/autostop-work-telegram-releases/current"': f'release_link="{current}"',
        'venv_python="/opt/autostop-work-telegram-venv/bin/python"': f'venv_python="{sys.executable}"',
        'wake_python="/opt/AutostopManager/.venv/bin/python"': f'wake_python="{bin_dir / "wake-python"}"',
        'wake_config="/etc/autostop-work-telegram/wake.json"': f'wake_config="{config}"',
        'monitor_env="/etc/autostop-work-telegram/monitor.env"': f'monitor_env="{monitor}"',
        'control_lock="/run/autostop-work-telegram-control.lock"': f'control_lock="{tmp_path / "lock"}"',
        "/opt/autostop-work-telegram-releases/*": f"{releases}/*",
    }
    for old, new in replacements.items():
        assert old in source
        source = source.replace(old, new, 1)
    script = tmp_path / "duty.sh"
    script.write_text(source)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_LOG": str(log),
        "WAKE_STATE": json.dumps(state),
    }
    return script, env, monitor, intent, log


@pytest.mark.parametrize("state", ["starting", "reconnecting", "reconciling"])
def test_repeated_enable_preserves_recovering_duty(tmp_path, state):
    script, env, monitor, intent, log = duty_fixture(
        tmp_path,
        {"ok": True, "enabled": True, "connected": False, "ready": False, "recovery_state": state, "queued": 3},
    )
    result = subprocess.run(["bash", str(script), "--enable"], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert monitor.read_text() == intent
    calls = log.read_text()
    assert "restart" not in calls and "stop " not in calls and "disable" not in calls


def test_repeated_enable_rejects_blocked_without_destroying_existing_duty(tmp_path):
    script, env, monitor, intent, log = duty_fixture(
        tmp_path,
        {
            "ok": True,
            "enabled": False,
            "connected": True,
            "ready": False,
            "recovery_state": "blocked",
            "outcome_unknown": True,
        },
    )
    result = subprocess.run(["bash", str(script), "--enable"], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "work_telegram_duty_enable_failed=true" in result.stderr
    assert monitor.read_text() == intent
    calls = log.read_text()
    assert "restart" not in calls and "stop " not in calls and "disable" not in calls
    assert not list(tmp_path.glob(".monitor.env.*"))


@pytest.mark.parametrize("connected,state", [(False, "reconnecting"), (True, "reconciling"), (True, "blocked")])
def test_status_reports_actual_wake_readiness(tmp_path, connected, state):
    script, env, _, _, _ = duty_fixture(
        tmp_path,
        {"ok": True, "enabled": state != "blocked", "connected": connected, "ready": False, "recovery_state": state},
    )
    result = subprocess.run(["bash", str(script), "--status"], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    assert status["wake_active"] and status["inbound_enabled"]
    assert status["wake_connected"] is connected
    assert not status["wake_ready"]
    assert status["wake_recovery_state"] == state


@pytest.mark.parametrize("response", ["not-json", "[]", '{"recovery_state":"private-payload"}'])
def test_status_rejects_malformed_wake_state_without_exposing_payload(tmp_path, response):
    script, env, _, _, _ = duty_fixture(tmp_path, {})
    env["WAKE_STATE"] = response
    result = subprocess.run(["bash", str(script), "--status"], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    assert not status["wake_ready"] and not status["wake_connected"]
    assert status["wake_recovery_state"] == "unavailable"
    assert "private-payload" not in result.stdout
