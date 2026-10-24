"""Exercise duty controls against disposable shell mocks, including non-root CI."""

import fcntl
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


@pytest.mark.parametrize("response", ["not-json", "[]", "null", "{}", '{"recovery_state":"private-payload"}'])
def test_status_rejects_malformed_wake_state_without_exposing_payload(tmp_path, response):
    script, env, _, _, _ = duty_fixture(tmp_path, {})
    env["WAKE_STATE"] = response
    result = subprocess.run(["bash", str(script), "--status"], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    assert not status["wake_ready"] and not status["wake_connected"]
    assert status["wake_recovery_state"] == "unavailable"
    assert status["pending"] is None and status["needs_review"] is None
    assert not status["status_fresh"]
    assert "private-payload" not in result.stdout


@pytest.mark.parametrize("value", [None, True, "0", -1])
def test_status_preserves_unknown_or_invalid_queue_counts(tmp_path, value):
    script, env, _, _, _ = duty_fixture(
        tmp_path,
        {"ok": True, "queued": value, "needs_review": value, "status_fresh": False},
    )
    result = subprocess.run(["bash", str(script), "--status"], env=env, capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    assert status["pending"] is None and status["needs_review"] is None
    assert not status["status_fresh"]


@pytest.mark.parametrize("fresh", [True, False])
@pytest.mark.parametrize("pending_key", ["pending", "queued"])
@pytest.mark.parametrize("pending,reviews", [(0, 0), (7, 3)])
def test_status_preserves_observed_counts_and_freshness(tmp_path, fresh, pending_key, pending, reviews):
    script, env, _, _, _ = duty_fixture(
        tmp_path,
        {"ok": True, pending_key: pending, "needs_review": reviews, "status_fresh": fresh},
    )
    result = subprocess.run(["bash", str(script), "--status"], env=env, capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    assert status["pending"] == pending and status["needs_review"] == reviews
    assert status["status_fresh"] is fresh


def test_status_reports_busy_control_without_waiting_or_mutating(tmp_path):
    script, env, monitor, intent, log = duty_fixture(tmp_path, {})
    with (tmp_path / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = subprocess.run(["bash", str(script), "--status"], env=env, capture_output=True, text=True, timeout=3)
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"ok": False, "error": "duty_control_busy"}
    assert monitor.read_text() == intent
    assert not log.exists()


@pytest.mark.parametrize("operation", ["--enable", "--disable"])
def test_duty_changes_still_wait_for_exclusive_control(tmp_path, operation):
    script, env, monitor, intent, log = duty_fixture(
        tmp_path, {"ok": True, "enabled": True, "connected": True, "ready": True, "recovery_state": "ready"}
    )
    with (tmp_path / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        process = subprocess.Popen(
            ["bash", str(script), operation], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.1)
            assert monitor.read_text() == intent
            assert not log.exists()
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            process.communicate(timeout=3)
