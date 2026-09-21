from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from autostop_manager import automation_release
from autostop_manager.automation_registry import AutomationError, AutomationStore
from autostop_manager.automation_timers import SYSTEM_TIMER_ALLOWLIST


ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path("/opt/AutostopManager/.venv/bin/python")


def test_release_preflight_holds_before_idempotent_off_seed(tmp_path: Path):
    db = tmp_path / "registry.sqlite3"
    socket_path = tmp_path / "missing.sock"
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "AUTOSTOP_MANAGER_ENV_FILE": "/dev/null",
        "AUTOSTOP_AUTOMATION_DB": str(db),
        "AUTOSTOP_AUTOMATION_CONTROL_SOCKET": str(socket_path),
    }
    hold_command = [
        str(PYTHON),
        "-m",
        "autostop_manager.automation_release",
        "hold",
        "--release-attempt-key",
        "release-attempt-test-1",
    ]
    seed_command = [
        str(PYTHON),
        "-m",
        "autostop_manager.automation_release",
        "seed",
        "--release-attempt-key",
        "release-attempt-test-1",
    ]
    wrong_unhold_command = [
        str(PYTHON),
        "-m",
        "autostop_manager.automation_release",
        "release-hold",
        "--release-attempt-key",
        "release-attempt-test-2",
    ]

    first = subprocess.run(hold_command, env=environment, capture_output=True, text=True, timeout=10, check=False)
    wrong_unhold = subprocess.run(
        wrong_unhold_command, env=environment, capture_output=True, text=True, timeout=10, check=False
    )
    seeded = subprocess.run(seed_command, env=environment, capture_output=True, text=True, timeout=10, check=False)
    second = subprocess.run(seed_command, env=environment, capture_output=True, text=True, timeout=10, check=False)

    assert first.returncode == 0, first.stdout + first.stderr
    assert wrong_unhold.returncode == 1
    assert json.loads(wrong_unhold.stdout)["error"] == "automation_release_hold_ownership_lost"
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    first_hold = json.loads(first.stdout)["global_hold"]
    assert first_hold["enabled"] is True
    assert first_hold["reason"] == "release"
    assert first_hold["revision"] == 1
    assert len(first_hold["attempt_hash"]) == 64
    assert json.loads(seeded.stdout)["seeded"] is True
    assert json.loads(second.stdout)["seeded"] is False
    status_code = """
from autostop_manager.automation_registry import AutomationStore
import json
print(json.dumps(AutomationStore().status()))
"""
    status_result = subprocess.run(
        [str(PYTHON), "-c", status_code],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    status = json.loads(status_result.stdout)
    assert len(status["jobs"]) == 1
    assert status["jobs"][0]["desired_state"] == "off"
    assert status["jobs"][0]["schedule"] == {
        "kind": "interval",
        "every_minutes": 20,
        "timezone": "Asia/Krasnoyarsk",
        "active_window": "24/7",
    }


def test_installer_prepares_hold_before_service_activation():
    source = (ROOT / "scripts/install-manager-automation.sh").read_text(encoding="utf-8")

    assert "--activate-under-hold" in source
    assert "git -C" not in source
    assert "--release-attempt-key" in source
    assert source.index("automation_release hold") < source.index('install -o root -g root -m 0644 "${UNIT_SOURCE}"')
    assert source.index("automation_release hold") < source.index('systemctl enable --now "${UNIT_NAME}"')
    assert source.index("automation_release adopt-current") < source.index('systemctl enable --now "${UNIT_NAME}"')
    assert source.index("automation_release seed") > source.index('systemctl enable --now "${UNIT_NAME}"')


def test_offline_timer_adoption_requires_owned_hold_and_exact_readback(monkeypatch, tmp_path: Path):
    database = tmp_path / "registry.sqlite3"
    monkeypatch.setenv("AUTOSTOP_AUTOMATION_DB", str(database))
    monkeypatch.setenv("AUTOSTOP_AUTOMATION_CONTROL_SOCKET", str(tmp_path / "missing.sock"))
    attempt = "release-adopt-test-0001"
    automation_release.hold(release_attempt_key=attempt)

    class FakeTimerController:
        def __init__(self, *, store):
            self.store = store

        @staticmethod
        def _state():
            return {
                "load_state": "loaded",
                "active_state": "active",
                "unit_file_state": "enabled",
                "desired_state": "on",
                "actual_state": "active",
                "period_minutes": 20,
                "next_run_at": None,
                "last_run_at": None,
                "error_code": None,
            }

        def adopt_all_current(self):
            return [
                self.store.adopt_system_timer(
                    timer_id=timer_id,
                    unit_name=policy.unit_name,
                    control_mode=policy.control_mode,
                    state=self._state(),
                )
                for timer_id, policy in sorted(SYSTEM_TIMER_ALLOWLIST.items())
            ]

        def list_status(self):
            return [
                {
                    "timer_id": timer_id,
                    "name": policy.display_name,
                    "unit_name": policy.unit_name,
                    "control_mode": policy.control_mode,
                    "locked": policy.control_mode == "read_only",
                    "inspection_ok": True,
                    "state": self._state(),
                }
                for timer_id, policy in sorted(SYSTEM_TIMER_ALLOWLIST.items())
            ]

    monkeypatch.setattr(automation_release, "SystemTimerController", FakeTimerController)

    result = automation_release.adopt_current(release_attempt_key=attempt)

    assert result["timers_verified"] is True
    assert len(result["timers"]) == len(SYSTEM_TIMER_ALLOWLIST)
    assert len(AutomationStore(database).status()["system_timers"]) == len(SYSTEM_TIMER_ALLOWLIST)
    with pytest.raises(AutomationError, match="automation_release_hold_ownership_lost"):
        automation_release.adopt_current(release_attempt_key="release-adopt-test-0002")
