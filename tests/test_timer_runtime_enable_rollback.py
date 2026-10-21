"""Restore runtime enablement with independent persistent/runtime scope state."""

from __future__ import annotations

import subprocess

import pytest

from autostop_manager.automation_registry import AutomationError, AutomationStore
from autostop_manager.automation_timers import SystemTimerController, SystemTimerPolicy


class ScopedTimer:
    def __init__(self, *, active=True, persistent=False, runtime=True, failure="transport", wrong_restore_scope=False):
        self.active = active
        self.persistent = persistent
        self.runtime = runtime
        self.failure = failure
        self.wrong_restore_scope = wrong_restore_scope
        self.wrong_restore_seen = False
        self.apply_snapshot = None
        self.calls = []

    def state(self):
        return self.persistent, self.runtime, self.active

    def __call__(self, command):
        self.calls.append(tuple(command))
        action = command[1]
        if action == "show":
            unit_file_state = "enabled" if self.persistent else "enabled-runtime" if self.runtime else "disabled"
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "LoadState=loaded\n"
                    f"ActiveState={'active' if self.active else 'inactive'}\n"
                    f"UnitFileState={unit_file_state}\n"
                    "Result=success\nTimersMonotonic={ OnUnitActiveUSec=15min ; next_elapse=15min }\n"
                ),
                stderr="",
            )
        if action in {"enable", "disable"}:
            # Ordinary commands affect only persistent links. In particular,
            # disabling them must not silently erase runtime links in this fake.
            if "--runtime" in command:
                self.runtime = action == "enable"
                if self.wrong_restore_scope and action == "enable":
                    self.persistent = True
                    self.wrong_restore_seen = True
            else:
                self.persistent = action == "enable"
            if "--now" in command:
                self.active = action == "enable"
                self.apply_snapshot = self.state()
                if self.failure == "transport":
                    raise OSError("synthetic failure after changing enablement and activity")
        elif action == "start":
            self.active = True
        elif action == "stop":
            self.active = False
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def controller(tmp_path, runner):
    policy = SystemTimerPolicy("synthetic", "autostop-synthetic.timer", "Synthetic timer", "managed")
    return SystemTimerController(
        policies={policy.timer_id: policy},
        runner=runner,
        dropin_root=tmp_path,
        store=AutomationStore(tmp_path / "unused.sqlite3"),
    )


@pytest.mark.parametrize("active", [True, False])
def test_partial_enable_removes_persistent_links_and_restores_runtime_scope(tmp_path, active):
    runner = ScopedTimer(active=active)
    owned = controller(tmp_path, runner)

    with pytest.raises(AutomationError, match=r"^system_timer_apply_failed$"):
        owned.set_enabled("synthetic", enabled=True)

    assert runner.apply_snapshot == (True, True, True)
    assert runner.state() == (False, True, active)
    assert owned.inspect("synthetic")["state"]["unit_file_state"] == "enabled-runtime"
    actions = [command for command in runner.calls if command[1] != "show"]
    assert all("--now" not in command for command in actions[1:])
    assert not (tmp_path / "unused.sqlite3").exists()


@pytest.mark.parametrize("failure", ["transport", "readback"])
def test_runtime_disable_failure_restores_original_scope_and_activity(tmp_path, failure):
    runner = ScopedTimer(failure=failure)
    owned = controller(tmp_path, runner)
    expected = "system_timer_apply_failed" if failure == "transport" else "system_timer_readback_failed"

    # A successful persistent-scope disable can leave runtime links. The apply
    # path still rejects that readback; fixing successful disable is separate.
    with pytest.raises(AutomationError, match=rf"^{expected}$"):
        owned.set_enabled("synthetic", enabled=False)

    assert runner.apply_snapshot == (False, True, False)
    assert runner.state() == (False, True, True)
    assert owned.inspect("synthetic")["state"]["unit_file_state"] == "enabled-runtime"


def test_runtime_rollback_rejects_wrong_scope_even_when_on_and_active_match(tmp_path):
    runner = ScopedTimer(wrong_restore_scope=True)
    owned = controller(tmp_path, runner)

    with pytest.raises(AutomationError, match=r"^system_timer_rollback_failed$"):
        owned.set_enabled("synthetic", enabled=True)

    assert runner.wrong_restore_seen
    after = owned.inspect("synthetic")["state"]
    assert after["desired_state"] == "on" and after["actual_state"] == "active"
    assert after["unit_file_state"] == "enabled"


def test_persistent_enablement_recovery_keeps_existing_behavior(tmp_path):
    runner = ScopedTimer(persistent=True, runtime=False)
    owned = controller(tmp_path, runner)

    with pytest.raises(AutomationError, match=r"^system_timer_apply_failed$"):
        owned.set_enabled("synthetic", enabled=False)

    assert runner.apply_snapshot == (False, False, False)
    assert runner.state() == (True, False, True)
    assert all("--runtime" not in command for command in runner.calls)
