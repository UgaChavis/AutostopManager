"""Forward disable and rollback keep persistent/runtime enablement scopes distinct."""

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
        self.runtime_disable_seen = False
        self.runtime_disable_fault_seen = False
        self.readback_fault_seen = False
        self.calls = []

    def state(self):
        return self.persistent, self.runtime, self.active

    def __call__(self, command):
        self.calls.append(tuple(command))
        action = command[1]
        if action == "show":
            if self.failure == "readback" and self.runtime_disable_seen and not self.readback_fault_seen:
                self.readback_fault_seen = True
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="synthetic inspection failure")
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
                if action == "disable" and self.failure == "scope-before":
                    self.runtime_disable_fault_seen = True
                    raise OSError("synthetic runtime disable failure before changing links")
                self.runtime = action == "enable"
                if action == "disable":
                    self.runtime_disable_seen = True
                    self.apply_snapshot = self.state()
                    if self.failure == "scope-after":
                        self.runtime_disable_fault_seen = True
                        raise OSError("synthetic runtime disable failure after changing links")
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

    # The readback failure is explicitly injected after both scopes are off;
    # remaining runtime links are not treated as an intentional apply failure.
    with pytest.raises(AutomationError, match=rf"^{expected}$"):
        owned.set_enabled("synthetic", enabled=False)

    assert runner.apply_snapshot == ((False, True, False) if failure == "transport" else (False, False, False))
    assert runner.readback_fault_seen is (failure == "readback")
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


@pytest.mark.parametrize(
    "persistent,runtime",
    [(False, True), (True, True), (True, False)],
    ids=["runtime-only", "both-scopes", "persistent-only"],
)
def test_disable_clears_both_enablement_scopes_before_success(tmp_path, persistent, runtime):
    runner = ScopedTimer(persistent=persistent, runtime=runtime, failure=None)
    owned = controller(tmp_path, runner)

    result = owned.set_enabled("synthetic", enabled=False)

    assert result["state"]["desired_state"] == "off"
    assert result["state"]["actual_state"] == "inactive"
    assert result["state"]["unit_file_state"] == "disabled"
    assert runner.state() == (False, False, False)
    assert not any(command[1] in {"enable", "start"} for command in runner.calls)
    assert not (tmp_path / "unused.sqlite3").exists()


@pytest.mark.parametrize("failure", ["scope-before", "scope-after"])
def test_second_disable_scope_failure_uses_exact_runtime_rollback(tmp_path, failure):
    runner = ScopedTimer(failure=failure)
    owned = controller(tmp_path, runner)

    with pytest.raises(AutomationError, match=r"^system_timer_apply_failed$"):
        owned.set_enabled("synthetic", enabled=False)

    assert runner.runtime_disable_fault_seen
    assert runner.apply_snapshot == ((False, True, False) if failure == "scope-before" else (False, False, False))
    assert runner.state() == (False, True, True)
    assert owned.inspect("synthetic")["state"]["unit_file_state"] == "enabled-runtime"
    failed_step = next(
        index for index, command in enumerate(runner.calls) if command[1] == "disable" and "--runtime" in command
    )
    assert all("--now" not in command for command in runner.calls[failed_step + 1 :])
    assert not (tmp_path / "unused.sqlite3").exists()
