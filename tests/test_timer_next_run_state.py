"""Only an actively running timer can supply an inferred next execution."""

from __future__ import annotations

import socket
import subprocess

import pytest

from autostop_manager.automation_control import AutomationControlService
from autostop_manager.automation_registry import AutomationStore
from autostop_manager.automation_timers import SystemTimerController, SystemTimerPolicy


LAST_RUN = "2026-10-26T12:00:00Z"
NEXT_RUN = "2026-10-26T12:15:00Z"


@pytest.fixture(autouse=True)
def forbid_effects(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("timer next-run checks must not use network, processes or registry")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(subprocess, "run", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(AutomationStore, "connect", denied)


def inspect(tmp_path, *, active_state, next_run="", last_run=LAST_RUN, unit_state="enabled", failure=None):
    policy = SystemTimerPolicy("synthetic", "synthetic.timer", "Synthetic timer", "read_only")
    calls = []

    def runner(command):
        calls.append(tuple(command))
        assert command[:3] == ["systemctl", "show", policy.unit_name]
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, timeout=5)
        if failure == "unavailable":
            raise FileNotFoundError("synthetic systemctl unavailable")
        state_property = "" if active_state is None else f"ActiveState={active_state}\n"
        return subprocess.CompletedProcess(
            command,
            1 if failure == "nonzero" else 0,
            stdout=(
                f"LoadState=loaded\n{state_property}UnitFileState={unit_state}\n"
                f"LastTriggerUSec={last_run}\nNextElapseUSecRealtime={next_run}\n"
                "NextElapseUSecMonotonic=0\nResult=success\n"
                "TimersMonotonic={ OnUnitActiveUSec=15min ; next_elapse=0 }\n"
            ),
            stderr="",
        )

    store = AutomationStore(tmp_path / "unused.sqlite3")
    controller = SystemTimerController(
        policies={policy.timer_id: policy}, runner=runner, dropin_root=tmp_path, store=store
    )
    service = AutomationControlService(store=store, timer_controller=controller)
    inspected = controller.inspect(policy.timer_id)
    projected = service._timer_statuses([])[0]
    assert len(calls) == 2
    assert not store.path.exists()
    assert list(tmp_path.iterdir()) == []
    assert projected["next_run_at"] == inspected["state"]["next_run_at"]
    return inspected


@pytest.mark.parametrize("active_state", ["inactive", "failed", "unknown", None])
@pytest.mark.parametrize("next_run", ["", NEXT_RUN, "Mon 2026-10-26 12:15:00 UTC"])
def test_nonactive_timer_has_no_inferred_next_run_but_keeps_explicit_readback(tmp_path, active_state, next_run):
    result = inspect(tmp_path, active_state=active_state, next_run=next_run, unit_state="disabled")
    assert result["inspection_ok"] is True
    assert result["state"]["next_run_at"] == (next_run or None)
    assert result["state"]["last_run_at"] == LAST_RUN
    assert result["state"]["period_minutes"] == 15
    assert result["state"]["active_state"] == (active_state or "unknown")


@pytest.mark.parametrize("unit_state", ["enabled", "enabled-runtime", "disabled"])
@pytest.mark.parametrize("last_run", [LAST_RUN, "Mon 2026-10-26 12:00:00 UTC"])
def test_active_timer_inference_uses_activity_not_autostart_scope(tmp_path, unit_state, last_run):
    result = inspect(tmp_path, active_state="active", unit_state=unit_state, last_run=last_run)
    assert result["inspection_ok"] is True
    assert result["state"]["next_run_at"] == NEXT_RUN
    assert result["state"]["last_run_at"] == last_run
    assert result["state"]["unit_file_state"] == unit_state


@pytest.mark.parametrize("failure", ["timeout", "unavailable", "nonzero"])
def test_failed_inspection_cannot_infer_a_run_from_unusable_output(tmp_path, failure):
    result = inspect(tmp_path, active_state="active", next_run=NEXT_RUN, failure=failure)
    assert result["inspection_ok"] is False
    assert result["state"]["next_run_at"] is None
    assert result["state"]["actual_state"] == "unknown"
