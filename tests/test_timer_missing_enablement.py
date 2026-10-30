"""An unavailable systemd enablement state cannot prove a timer is off."""

import subprocess

import pytest

from autostop_manager.automation_control import AutomationControlService
from autostop_manager.automation_registry import AutomationError
from autostop_manager.automation_timers import SystemTimerController, SystemTimerPolicy


@pytest.mark.parametrize("unit_line", ["", "UnitFileState=\n", "UnitFileState=unknown\n"])
def test_missing_unit_file_state_does_not_report_off_timer_in_sync(tmp_path, monkeypatch, unit_line):
    def runner(command):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"LoadState=loaded\nActiveState=inactive\n{unit_line}Result=success\n",
            stderr="",
        )

    policy = SystemTimerPolicy("example", "example.timer", "Example", "managed")
    controller = SystemTimerController(
        policies={"example": policy}, runner=runner, dropin_root=tmp_path, store=object()
    )
    service = AutomationControlService(store=object(), timer_controller=controller)
    assert controller.inspect("example")["inspection_ok"] is False
    with pytest.raises(AutomationError, match="system_timer_inspection_failed"):
        controller.adopt_current("example")
    row = service._timer_statuses([{"timer_id": "example", "desired_state": "off", "revision": 1}])[0]
    assert row["reconcile_state"] == "inspection_error"
    assert row["error_code"] == "system_timer_inspection_failed"

    monkeypatch.setattr(
        service,
        "_execution_packet",
        lambda *_: {"dependencies": {"instruction_navigation": "ready"}, "outbox": {"blocked_count": 0}},
    )
    ready = service._readiness_from_status(
        {"readiness": {"ready": True}, "system_timers": [row], "jobs": [], "global_hold": {}}
    )
    assert ready["ready"] is False
    assert ready["checks"]["system_timer_reconciliation"] == "degraded"
