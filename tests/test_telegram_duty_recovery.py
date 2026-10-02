"""Exercise duty controls against disposable shell mocks, including non-root CI."""

import json
import subprocess

import pytest
from telegram_duty_fakes import duty_fixture


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
