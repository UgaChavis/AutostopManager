"""A running timer can still have the wrong persistent enablement state."""

import subprocess

import pytest

from autostop_manager.automation_control import AutomationControlService
from autostop_manager.automation_timers import SystemTimerController, SystemTimerPolicy


@pytest.mark.parametrize(
    ("saved", "active", "unit_file", "expected"),
    [
        ("on", "active", "enabled", "in_sync"),
        ("on", "active", "enabled-runtime", "in_sync"),
        ("on", "active", "disabled", "drift"),
        ("off", "inactive", "disabled", "in_sync"),
        ("off", "inactive", "enabled", "drift"),
    ],
)
def test_timer_status_checks_enablement_separately_from_running_state(
    tmp_path, monkeypatch, saved, active, unit_file, expected
):
    def runner(command):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"LoadState=loaded\nActiveState={active}\nUnitFileState={unit_file}\nResult=success\n",
            stderr="",
        )

    policy = SystemTimerPolicy("example", "example.timer", "Example", "managed")
    controller = SystemTimerController(
        policies={"example": policy}, runner=runner, dropin_root=tmp_path, store=object()
    )
    service = AutomationControlService(store=object(), timer_controller=controller)
    row = service._timer_statuses([{"timer_id": "example", "desired_state": saved, "revision": 1}])[0]

    assert row["actual_state"] == active
    assert row["reconcile_state"] == expected
    assert row["error_code"] == ("system_timer_drift" if expected == "drift" else None)

    monkeypatch.setattr(
        service,
        "_execution_packet",
        lambda *_: {"dependencies": {"instruction_navigation": "ready"}, "outbox": {"blocked_count": 0}},
    )
    readiness = service._readiness_from_status(
        {"readiness": {"ready": True}, "system_timers": [row], "jobs": [], "global_hold": {}}
    )
    assert readiness["ready"] is (expected == "in_sync")
    if expected == "drift":
        assert readiness["checks"]["system_timer_reconciliation"] == "degraded"
