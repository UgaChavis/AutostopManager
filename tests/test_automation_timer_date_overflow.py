"""An unrepresentable inferred timer date must not discard valid inspection data."""

from types import SimpleNamespace
import socket
import subprocess

import pytest

from autostop_manager import automation_control as control
from autostop_manager.automation_control import AUTOMATION_CONTROL_PROTOCOL, AutomationControlService
from autostop_manager.automation_registry import AutomationStore
from autostop_manager.automation_timers import SystemTimerController, SystemTimerPolicy


LAST_RUN = "Mon 2026-10-26 12:00:00 UTC"
LARGE_PERIODS = [
    ("3000000d", 4_320_000_000),
    ("1000000000d", 1_440_000_000_000),
    ("999999999999999999999min", 999_999_999_999_999_999_999),
]


@pytest.fixture(autouse=True)
def forbid_external_operations(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("timer date tests must use only synthetic registry and runner")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(subprocess, "run", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(
        control,
        "get_automation_runtime_identity",
        lambda: {"manager_revision": "0" * 40, "crm_version": "synthetic", "crm_revision": "1" * 40},
    )
    monkeypatch.setattr(control, "instruction_inventory_snapshot", lambda *_args, **_kwargs: SimpleNamespace(issues=[]))
    monkeypatch.setattr(
        AutomationControlService,
        "_instruction_hashes",
        staticmethod(
            lambda _inventory=None: [{"label": "synthetic", "path_label": "synthetic.md", "sha256": "0" * 64}]
        ),
    )


@pytest.fixture
def case(tmp_path):
    states = {
        timer_id: {"period": "15min", "last": LAST_RUN, "next": "", "result": "success"}
        for timer_id in ("synthetic_long", "synthetic_healthy")
    }
    policies = {
        timer_id: SystemTimerPolicy(timer_id, f"autostop-{timer_id}.timer", timer_id, "read_only")
        for timer_id in states
    }
    units = {policy.unit_name: timer_id for timer_id, policy in policies.items()}
    calls = []

    def runner(command):
        assert command[:2] == ["systemctl", "show"]
        assert command[2] in units
        calls.append(command[2])
        state = states[units[command[2]]]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n"
                f"NextElapseUSecRealtime={state['next']}\nLastTriggerUSec={state['last']}\n"
                f"Result={state['result']}\nTimersCalendar=\n"
                f"TimersMonotonic={{ OnUnitActiveUSec={state['period']} ; next_elapse=15min }}\n"
            ),
            stderr="",
        )

    store = AutomationStore(tmp_path / "registry.sqlite3")
    controller = SystemTimerController(policies=policies, runner=runner, dropin_root=tmp_path, store=store)
    service = AutomationControlService(store, timer_controller=controller)
    return SimpleNamespace(states=states, controller=controller, service=service, calls=calls, store=store)


@pytest.mark.parametrize("last", [LAST_RUN, "2026-10-26T12:00:00Z"])
@pytest.mark.parametrize("period,minutes", LARGE_PERIODS)
def test_inferred_date_overflow_keeps_period_and_successful_inspection(case, period, minutes, last):
    case.states["synthetic_long"].update(period=period, last=last)

    result = case.controller.inspect("synthetic_long")

    assert result["inspection_ok"] is True
    assert result["state"]["period_minutes"] == minutes
    assert result["state"]["next_run_at"] is None
    assert result["state"]["last_run_at"] == last
    assert result["state"]["active_state"] == "active"
    assert result["state"]["unit_file_state"] == "enabled"
    assert result["state"]["error_code"] is None
    assert result["locked"] is True


@pytest.mark.parametrize("next_run", ["2026-10-26T12:30:00Z", "Mon 2026-10-26 12:30:00 UTC"])
def test_explicit_systemd_date_is_kept_without_fallback_computation(case, next_run):
    case.states["synthetic_long"].update(period="3000000d", next=next_run)

    result = case.controller.inspect("synthetic_long")

    assert result["inspection_ok"] is True
    assert result["state"]["next_run_at"] == next_run
    assert result["state"]["period_minutes"] == 4_320_000_000


def test_unknown_inferred_date_does_not_hide_reported_timer_failure(case):
    case.states["synthetic_long"].update(period="3000000d", result="failed")

    result = case.controller.inspect("synthetic_long")

    assert result["inspection_ok"] is True
    assert result["state"]["next_run_at"] is None
    assert result["state"]["error_code"] == "system_timer_failed"


@pytest.mark.parametrize("operation", ["status", "readiness"])
def test_public_reads_preserve_drift_and_healthy_timer_when_date_overflows(case, operation):
    case.controller.adopt_current("synthetic_long")
    case.controller.adopt_current("synthetic_healthy")
    case.states["synthetic_long"]["period"] = "3000000d"

    result = case.service.handle(
        {
            "protocol": AUTOMATION_CONTROL_PROTOCOL,
            "request_id": "00000000-0000-0000-0000-000000000001",
            "operation": operation,
            "actor": {"kind": "codex", "id": "synthetic-owner", "is_admin": True},
            "payload": {},
        }
    )

    readiness = result["readiness"] if operation == "status" else result
    assert readiness["ready"] is False
    assert readiness["checks"]["system_timer_reconciliation"] == "degraded"
    rows = result["system_timers"] if operation == "status" else result["execution_packet"]["system_timers"]
    timers = {timer["timer_id"]: timer for timer in rows}
    assert timers["synthetic_long"]["reconcile_state"] == "drift"
    assert timers["synthetic_long"]["error_code"] == "system_timer_drift"
    assert timers["synthetic_healthy"]["reconcile_state"] == "in_sync"
    assert timers["synthetic_healthy"]["error_code"] is None
    if operation == "status":
        assert timers["synthetic_long"]["period_minutes"] == 15
        assert timers["synthetic_long"]["actual_period_minutes"] == 4_320_000_000
        assert timers["synthetic_long"]["next_run_at"] is None
        assert timers["synthetic_healthy"]["next_run_at"] == "2026-10-26T12:15:00Z"


@pytest.mark.parametrize("last", [LAST_RUN, "2026-10-26T12:00:00Z"])
def test_ordinary_fifteen_minute_fallback_still_produces_the_same_date(case, last):
    case.states["synthetic_long"]["last"] = last

    result = case.controller.inspect("synthetic_long")

    assert result["inspection_ok"] is True
    assert result["state"]["period_minutes"] == 15
    assert result["state"]["next_run_at"] == "2026-10-26T12:15:00Z"
