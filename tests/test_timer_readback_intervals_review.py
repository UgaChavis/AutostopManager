"""Verify timer interval evidence through inspect and status reconciliation.

Use only an injected synthetic systemctl runner; no service or DB is opened.
Twenty-three cases represent eighteen scenarios; three order and two serialization repeats.
"""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from typing import Any

import pytest

from autostop_manager.automation_control import AutomationControlService
from autostop_manager.automation_registry import AutomationStore
from autostop_manager.automation_timers import SYSTEM_TIMER_ALLOWLIST, SystemTimerController


@pytest.mark.parametrize(
    "monotonic_entries,desired_period,expected_period,expected_reconcile,read_only",
    [
        pytest.param(
            (("OnUnitActiveUSec", "15min"), ("OnUnitActiveUSec", "5min")),
            15,
            None,
            "inspection_error",
            False,
            id="conflict-15-then-5",
        ),
        pytest.param(
            (("OnUnitActiveUSec", "5min"), ("OnUnitActiveUSec", "15min")),
            15,
            None,
            "inspection_error",
            False,
            id="conflict-5-then-15",
        ),
        pytest.param(
            (("OnUnitActiveUSec", "15min"),),
            15,
            15,
            "in_sync",
            False,
            id="single-15min",
        ),
        pytest.param(
            (("OnUnitActiveUSec", "15min"), ("OnUnitActiveUSec", "900s")),
            15,
            15,
            "in_sync",
            False,
            id="equivalent-15min-and-900s",
        ),
        pytest.param(
            (("OnBootUSec", "2min"), ("OnUnitActiveUSec", "15min")),
            15,
            15,
            "in_sync",
            False,
            id="boot-plus-single-period",
        ),
        pytest.param(
            (("OnUnitActiveUSec", "1h 15min"),),
            75,
            75,
            "in_sync",
            False,
            id="compound-exact-75min",
        ),
        pytest.param(
            (("OnUnitActiveUSec", "1h nonsense"),),
            75,
            None,
            "inspection_error",
            False,
            id="malformed-period-remains-unknown",
        ),
        pytest.param(
            (("OnUnitActiveUSec", "1min 30s"),),
            15,
            None,
            "inspection_error",
            False,
            id="partial-minute-remains-unknown",
        ),
        pytest.param(
            (),
            None,
            None,
            "in_sync",
            True,
            id="readonly-calendar-without-monotonic-period",
        ),
        pytest.param(
            "{ OnUnitActiveUSec=15min ; next_elapse=1h } { OnUnitActiveUSec=5min ; next_elapse=1h }",
            15,
            None,
            "inspection_error",
            False,
            id="conflict-in-single-property",
        ),
        pytest.param(
            "{ OnUnitActiveUSec=15min ; next_elapse=1h } { OnUnitActiveUSec=900s ; next_elapse=1h }",
            15,
            15,
            "in_sync",
            False,
            id="equivalent-in-single-property",
        ),
        pytest.param(
            (("OnUnitActiveUSec", "15min"), ("OnUnitActiveUSec", "nonsense")),
            15,
            None,
            "inspection_error",
            False,
            id="valid-then-malformed",
        ),
        pytest.param(
            (("OnUnitActiveUSec", "nonsense"), ("OnUnitActiveUSec", "15min")),
            15,
            None,
            "inspection_error",
            False,
            id="malformed-then-valid",
        ),
        pytest.param(
            (("OnUnitActiveUSec", "15min"), ("OnUnitActiveUSec", "")),
            15,
            None,
            "inspection_error",
            False,
            id="valid-then-empty",
        ),
        pytest.param(
            (),
            15,
            None,
            "inspection_error",
            False,
            id="missing-period-for-managed-timer",
        ),
        pytest.param(
            "{ OnUnitActiveUSec=15min ; next_elapse=1h }" + " " * 600,
            15,
            None,
            "inspection_error",
            False,
            id="oversized-property-remains-unknown",
        ),
        pytest.param(
            (("OnUnitActiveUSec", "15min"), ("OnUnitActiveUSec", "1min" + " " * 600)),
            15,
            None,
            "inspection_error",
            False,
            id="valid-plus-oversized-property",
        ),
        pytest.param(
            "{ OnUnitActiveUSec=15min ; next_elapse=1h } { OnUnitActiveUSec=5min }",
            15,
            None,
            "inspection_error",
            False,
            id="valid-then-unterminated-period",
        ),
        pytest.param(
            "{ OnUnitActiveUSec=5min } { OnUnitActiveUSec=15min ; next_elapse=1h }",
            15,
            None,
            "inspection_error",
            False,
            id="unterminated-then-valid-period",
        ),
    ],
)
def test_timer_readback_contract(
    tmp_path: Path,
    monotonic_entries: tuple[tuple[str, str], ...] | str,
    desired_period: int | None,
    expected_period: int | None,
    expected_reconcile: str,
    read_only: bool,
) -> None:
    timer_id = "database_backup" if read_only else "managed_pc_health"
    policy = SYSTEM_TIMER_ALLOWLIST[timer_id]
    calendar = "{ OnCalendar=*-*-* 03:00:00 UTC ; next_elapse=Sat 2026-10-17 03:00:00 UTC }" if read_only else ""
    next_realtime = "Sat 2026-10-17 03:00:00 UTC" if read_only else ""
    entries = (
        [monotonic_entries]
        if isinstance(monotonic_entries, str)
        else [f"{{ {base}={duration} ; next_elapse=1h }}" for base, duration in monotonic_entries]
    )
    stdout = (
        "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n"
        f"NextElapseUSecRealtime={next_realtime}\n"
        "NextElapseUSecMonotonic=1h\nLastTriggerUSec=\nResult=success\n"
        f"TimersCalendar={calendar}\n" + "".join(f"TimersMonotonic={entry}\n" for entry in entries)
    )
    calls: list[tuple[str, ...]] = []
    expected_command = (
        "systemctl",
        "show",
        policy.unit_name,
        "--property=LoadState",
        "--property=ActiveState",
        "--property=UnitFileState",
        "--property=NextElapseUSecRealtime",
        "--property=NextElapseUSecMonotonic",
        "--property=LastTriggerUSec",
        "--property=Result",
        "--property=TimersCalendar",
        "--property=TimersMonotonic",
        "--no-pager",
    )

    def runner(command: Any) -> subprocess.CompletedProcess[str]:
        observed_command = tuple(command)
        calls.append(observed_command)
        assert observed_command == expected_command, "Only this synthetic read is permitted"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    # Constructor only: neither the service nor this readback path opens the DB.
    database_path = tmp_path / "unused-registry.sqlite3"
    dropin_root = tmp_path / "nonexistent-systemd"
    store = AutomationStore(database_path)
    controller = SystemTimerController(policies={timer_id: policy}, runner=runner, dropin_root=dropin_root, store=store)
    service = AutomationControlService(store=store, timer_controller=controller)
    saved = [{"timer_id": timer_id, "desired_state": "on", "period_minutes": desired_period, "revision": 7}]
    saved_before = copy.deepcopy(saved)

    inspected = controller.inspect(timer_id)
    statuses = service._timer_statuses(saved)

    assert calls == [expected_command, expected_command]
    assert not database_path.exists()
    assert not dropin_root.exists()
    assert saved == saved_before
    assert len(statuses) == 1
    status = statuses[0]
    assert inspected["inspection_ok"] is True
    assert inspected["locked"] is read_only
    assert status["locked"] is read_only
    assert status["control_mode"] == policy.control_mode
    assert inspected["state"]["calendar"] == (calendar or None)
    assert inspected["state"]["next_run_at"] == (next_realtime or None)
    assert status["desired_state"] == "on"
    assert status["period_minutes"] == desired_period
    assert status["revision"] == 7
    assert status["actual_state"] == "active"

    expected_error = "system_timer_inspection_failed" if expected_reconcile == "inspection_error" else None
    actual = (
        inspected["state"]["period_minutes"],
        status["actual_period_minutes"],
        status["reconcile_state"],
        status["error_code"],
    )
    assert actual == (expected_period, expected_period, expected_reconcile, expected_error)


@pytest.mark.parametrize(
    "durations,next_realtime,expected_period,expected_next",
    [
        pytest.param(
            ("15min", "5min"),
            "Mon 2026-09-21 12:05:00 UTC",
            None,
            "Mon 2026-09-21 12:05:00 UTC",
            id="explicit-next-run-survives-conflict",
        ),
        pytest.param(("15min", "5min"), "", None, None, id="conflict-has-no-derived-next-run"),
        pytest.param(("15min", "900s"), "", 15, "2026-09-21T12:00:00Z", id="equivalent-derived-next-run"),
        pytest.param(("0s",), "", 0, "2026-09-21T11:45:00Z", id="zero-period-keeps-existing-readback"),
    ],
)
def test_timer_next_run_uses_only_known_interval(
    tmp_path: Path,
    durations: tuple[str, ...],
    next_realtime: str,
    expected_period: int | None,
    expected_next: str | None,
) -> None:
    policy = SYSTEM_TIMER_ALLOWLIST["managed_pc_health"]
    last_run = "Mon 2026-09-21 11:45:00 UTC"
    calls = []

    def runner(command: Any) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(command))
        assert command[:3] == ["systemctl", "show", policy.unit_name]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n"
                f"LastTriggerUSec={last_run}\nNextElapseUSecRealtime={next_realtime}\nResult=success\n"
                + "".join(
                    f"TimersMonotonic={{ OnUnitActiveUSec={duration} ; next_elapse=1h }}\n" for duration in durations
                )
            ),
            stderr="",
        )

    database_path = tmp_path / "unused-next-run.sqlite3"
    dropin_root = tmp_path / "nonexistent-systemd"
    controller = SystemTimerController(
        policies={"managed_pc_health": policy},
        runner=runner,
        dropin_root=dropin_root,
        store=AutomationStore(database_path),
    )

    inspected = controller.inspect("managed_pc_health")

    assert len(calls) == 1
    assert inspected["inspection_ok"] is True
    assert inspected["state"]["period_minutes"] == expected_period
    assert inspected["state"]["last_run_at"] == last_run
    assert inspected["state"]["next_run_at"] == expected_next
    assert not database_path.exists()
    assert not dropin_root.exists()
