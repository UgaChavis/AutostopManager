from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from autostop_manager.automation_registry import AutomationError, AutomationStore
from autostop_manager.automation_control import AUTOMATION_CONTROL_PROTOCOL, AutomationControlService
from autostop_manager.automation_timers import SystemTimerController, SystemTimerPolicy


def fake_systemctl(command):
    assert command[:2] == ["systemctl", "show"]
    return subprocess.CompletedProcess(
        command,
        0,
        stdout=(
            "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n"
            "NextElapseUSecRealtime=Mon 2026-09-21 12:00:00 UTC\n"
            "LastTriggerUSec=Mon 2026-09-21 11:45:00 UTC\nResult=success\n"
            "TimersCalendar=\nTimersMonotonic={ OnUnitActiveUSec=15min ; next_elapse=15min }\n"
        ),
        stderr="",
    )


def test_system_timer_allowlist_locks_read_only_and_adopts_current(tmp_path: Path):
    policies = {
        "example": SystemTimerPolicy(
            timer_id="example",
            unit_name="autostop-example.timer",
            display_name="Example",
            control_mode="read_only",
        )
    }
    path = tmp_path / "manager.sqlite3"
    store = AutomationStore(path)
    store.initialize()
    controller = SystemTimerController(policies=policies, runner=fake_systemctl, store=store)

    status = controller.inspect("example")
    adopted = controller.adopt_current("example")

    assert status["locked"] is True
    assert status["state"]["period_minutes"] == 15
    assert adopted["state"]["active_state"] == "active"
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT unit_name, control_mode FROM manager_automation_system_timers").fetchone()
    assert row == ("autostop-example.timer", "read_only")
    with pytest.raises(AutomationError, match="system_timer_read_only"):
        controller.render_interval_dropin("example", every_minutes=20)
    with pytest.raises(AutomationError, match="system_timer_not_allowed"):
        controller.inspect("arbitrary")


def test_restart_preserves_adopted_desired_and_status_exposes_external_drift(tmp_path: Path):
    policies = {
        "managed_pc_health": SystemTimerPolicy(
            timer_id="managed_pc_health",
            unit_name="autostop-managed-pc-health.timer",
            display_name="Health",
            control_mode="managed",
        )
    }
    path = tmp_path / "manager.sqlite3"
    store = AutomationStore(path)
    first = SystemTimerController(policies=policies, runner=fake_systemctl, store=store)
    adopted = first.adopt_current("managed_pc_health")

    def drifted(command):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n"
                "NextElapseUSecRealtime=\nLastTriggerUSec=\nResult=success\n"
                "TimersCalendar=\nTimersMonotonic={ OnUnitActiveUSec=30min ; next_elapse=30min }\n"
            ),
            stderr="",
        )

    restarted = SystemTimerController(policies=policies, runner=drifted, store=store)
    readopted = restarted.adopt_current("managed_pc_health")
    status = AutomationControlService(store, timer_controller=restarted).handle(
        {
            "protocol": AUTOMATION_CONTROL_PROTOCOL,
            "request_id": str(uuid4()),
            "operation": "status",
            "actor": {"kind": "codex", "id": "timer-test", "is_admin": True},
            "payload": {},
        }
    )["system_timers"][0]

    assert adopted["adopted"] is True
    assert readopted["adopted"] is False
    assert readopted["revision"] == adopted["revision"] == 1
    assert status["desired_state"] == "on"
    assert status["actual_state"] == "inactive"
    assert status["period_minutes"] == 15
    assert status["actual_period_minutes"] == 30
    assert status["reconcile_state"] == "drift"
    assert status["error_code"] == "system_timer_drift"


@pytest.mark.parametrize("failure", ["timeout", "unavailable"])
def test_timer_transport_failure_preserves_status_and_blocks_readiness(tmp_path: Path, failure: str):
    policies = {
        timer_id: SystemTimerPolicy(timer_id, f"autostop-{timer_id}.timer", timer_id, "managed")
        for timer_id in ("failed", "healthy")
    }
    store = AutomationStore(tmp_path / "registry.sqlite3")
    controller = SystemTimerController(policies=policies, runner=fake_systemctl, store=store)
    controller.adopt_all_current()
    calls = []

    def failing_transport(command):
        calls.append(command)
        if command[2] == "autostop-failed.timer":
            if failure == "timeout":
                return subprocess.run(
                    [sys.executable, "-c", "import time; time.sleep(10)"],
                    capture_output=True,
                    text=True,
                    timeout=0.05,
                    check=False,
                )
            return subprocess.run([str(tmp_path / "missing-systemctl")], check=False)
        return fake_systemctl(command)

    controller.runner = failing_transport
    service = AutomationControlService(store, timer_controller=controller)
    status = control_request(service, "status", {}, None, None)
    failed, healthy = status["system_timers"]
    assert failed["reconcile_state"] == "inspection_error"
    assert failed["error_code"] == "system_timer_inspection_failed"
    assert failed["desired_state"] == "on"
    assert failed["actual_state"] == "unknown"
    assert healthy["reconcile_state"] == "in_sync"
    readiness = control_request(service, "readiness", {}, None, None)
    assert readiness["ready"] is False
    assert readiness["checks"]["system_timer_reconciliation"] == "degraded"
    with pytest.raises(AutomationError, match="system_timer_inspection_failed"):
        controller.set_enabled("failed", enabled=False)
    assert all(command[:2] == ["systemctl", "show"] for command in calls)


def test_managed_timer_dropin_is_staged_without_systemctl_side_effect(tmp_path: Path):
    policies = {
        "example": SystemTimerPolicy(
            timer_id="example",
            unit_name="autostop-example.timer",
            display_name="Example",
            control_mode="managed",
        )
    }
    calls = []

    def runner(command):
        calls.append(command)
        return fake_systemctl(command)

    controller = SystemTimerController(policies=policies, runner=runner, dropin_root=tmp_path)

    result = controller.stage_interval_dropin("example", every_minutes=20)
    content = Path(result["path"]).read_text(encoding="utf-8")

    assert calls == []
    assert "OnCalendar=\n" in content
    assert "OnUnitActiveSec=20min" in content
    assert result["daemon_reload_required"] is True
    assert result["unit_state_changed"] is False


class FakeTimerController:
    def __init__(self):
        self.calls = []

    def list_status(self):
        return []

    def set_enabled(self, timer_id, *, enabled):
        self.calls.append(("set_enabled", timer_id, enabled))
        return {
            "state": {
                "load_state": "loaded",
                "unit_file_state": "enabled" if enabled else "disabled",
                "desired_state": "on" if enabled else "off",
                "actual_state": "active" if enabled else "inactive",
                "period_minutes": 15,
                "next_run_at": None,
                "last_run_at": None,
                "error_code": None,
            }
        }

    def set_schedule(self, timer_id, *, every_minutes, expected_dropin_sha256=None):
        self.calls.append(("set_schedule", timer_id, every_minutes, expected_dropin_sha256))
        return {
            "dropin_sha256": "a" * 64,
            "state": {
                "load_state": "loaded",
                "unit_file_state": "enabled",
                "desired_state": "on",
                "actual_state": "active",
                "period_minutes": every_minutes,
                "next_run_at": None,
                "last_run_at": None,
                "error_code": None,
            },
        }


def control_request(service, operation, payload, key, revision):
    return service.handle(
        {
            "protocol": AUTOMATION_CONTROL_PROTOCOL,
            "request_id": str(uuid4()),
            "operation": operation,
            "actor": {"kind": "codex", "id": "timer-test", "is_admin": True},
            "payload": payload,
            "idempotency_key": key,
            "expected_revision": revision,
        }
    )


def test_managed_and_locked_timers_use_same_cas_control_contract(tmp_path: Path):
    store = AutomationStore(tmp_path / "manager.sqlite3")
    store.initialize()
    base_state = {
        "load_state": "loaded",
        "unit_file_state": "enabled",
        "desired_state": "on",
        "actual_state": "active",
        "period_minutes": 15,
        "next_run_at": None,
        "last_run_at": None,
        "error_code": None,
    }
    store.adopt_system_timer(
        timer_id="managed_pc_health",
        unit_name="autostop-managed-pc-health.timer",
        control_mode="managed",
        state=base_state,
    )
    store.adopt_system_timer(
        timer_id="database_backup",
        unit_name="autostop24-db-backup.timer",
        control_mode="read_only",
        state=base_state,
    )
    timers = FakeTimerController()
    service = AutomationControlService(store, timer_controller=timers)

    scheduled = control_request(
        service,
        "set_schedule",
        {"timer_id": "managed_pc_health", "schedule": {"every_minutes": 20}},
        "timer-schedule-0001",
        1,
    )

    assert scheduled["system_timer"]["period_minutes"] == 20
    assert scheduled["system_timer"]["revision"] == 2
    replay = control_request(
        service,
        "set_schedule",
        {"timer_id": "managed_pc_health", "schedule": {"every_minutes": 20}},
        "timer-schedule-0001",
        1,
    )
    assert replay["idempotent_replay"] is True
    assert len(timers.calls) == 1
    with pytest.raises(AutomationError, match="system_timer_read_only"):
        control_request(
            service,
            "set_enabled",
            {"timer_id": "database_backup", "enabled": False},
            "timer-disable-0001",
            1,
        )
    assert len(timers.calls) == 1


@pytest.mark.parametrize(
    "failure_phase,expected_error",
    [
        ("daemon_reload", "system_timer_apply_failed"),
        ("restart", "system_timer_apply_failed"),
        ("readback", "system_timer_readback_failed"),
    ],
)
def test_schedule_apply_failures_restore_exact_prior_absence(tmp_path: Path, failure_phase: str, expected_error: str):
    policies = {
        "example": SystemTimerPolicy(
            timer_id="example",
            unit_name="autostop-example.timer",
            display_name="Example",
            control_mode="managed",
        )
    }
    target = tmp_path / "autostop-example.timer.d" / "50-autostop-automation.conf"

    class Runner:
        def __init__(self):
            self.reloads = 0
            self.restarts = 0
            self.bad_readback_done = False

        def __call__(self, command):
            if command[1] == "daemon-reload":
                self.reloads += 1
                failed = failure_phase == "daemon_reload" and self.reloads == 1
                return subprocess.CompletedProcess(command, 1 if failed else 0, stdout="", stderr="")
            if command[1] == "restart":
                self.restarts += 1
                failed = failure_phase == "restart" and self.restarts == 1
                return subprocess.CompletedProcess(command, 1 if failed else 0, stdout="", stderr="")
            assert command[1] == "show"
            period = 20 if target.exists() else 15
            if failure_phase == "readback" and target.exists() and not self.bad_readback_done:
                period = 99
                self.bad_readback_done = True
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n"
                    "NextElapseUSecRealtime=\nLastTriggerUSec=\nResult=success\n"
                    f"TimersCalendar=\nTimersMonotonic={{ OnUnitActiveUSec={period}min ; next_elapse={period}min }}\n"
                ),
                stderr="",
            )

    runner = Runner()
    controller = SystemTimerController(
        policies=policies,
        runner=runner,
        dropin_root=tmp_path,
        store=AutomationStore(tmp_path / "registry.sqlite3"),
    )

    with pytest.raises(AutomationError, match=expected_error):
        controller.set_schedule("example", every_minutes=20)

    assert not target.exists()
    assert runner.reloads == 2
    assert runner.restarts >= 1


def test_enable_readback_failure_restores_previous_enabled_and_active_state(tmp_path: Path):
    policies = {
        "example": SystemTimerPolicy(
            timer_id="example",
            unit_name="autostop-example.timer",
            display_name="Example",
            control_mode="managed",
        )
    }

    class Runner:
        def __init__(self):
            self.desired = "on"
            self.actual = "active"
            self.first_readback = True
            self.calls = []

        def __call__(self, command):
            self.calls.append(tuple(command[1:]))
            action = command[1]
            if action == "disable":
                self.desired = "off"
                self.actual = "inactive"
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            if action == "enable":
                self.desired = "on"
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            if action == "start":
                self.actual = "active"
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            if action == "show":
                actual = self.actual
                if self.desired == "off" and self.first_readback:
                    actual = "active"
                    self.first_readback = False
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=(
                        f"LoadState=loaded\nActiveState={actual}\n"
                        f"UnitFileState={'enabled' if self.desired == 'on' else 'disabled'}\n"
                        "NextElapseUSecRealtime=\nLastTriggerUSec=\nResult=success\n"
                        "TimersCalendar=\nTimersMonotonic={ OnUnitActiveUSec=15min ; next_elapse=15min }\n"
                    ),
                    stderr="",
                )
            if action == "stop":
                self.actual = "inactive"
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            raise AssertionError(command)

    runner = Runner()
    controller = SystemTimerController(policies=policies, runner=runner, dropin_root=tmp_path)

    with pytest.raises(AutomationError, match="system_timer_readback_failed"):
        controller.set_enabled("example", enabled=False)

    assert runner.desired == "on"
    assert runner.actual == "active"
    assert any(call[0] == "enable" for call in runner.calls)
    assert any(call[0] == "start" for call in runner.calls)
