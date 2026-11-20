"""Unavailable systemd properties cannot prove a timer is healthy or off."""

import asyncio
import os
import subprocess

import pytest

from autostop_manager.automation_control import (
    AutomationControlClient,
    AutomationControlServer,
    AutomationControlService,
)
from autostop_manager.automation_registry import AutomationError, AutomationStore
from autostop_manager.automation_timers import SystemTimerController, SystemTimerPolicy


async def _read_control(service, tmp_path):
    socket_path = tmp_path / "ipc" / "control.sock"
    server = AutomationControlServer(
        service=service, socket_path=socket_path, allowed_uids=frozenset({os.getuid()}), socket_gid=os.getgid()
    )
    await server.start()
    try:
        client = AutomationControlClient(socket_path=socket_path)
        status = await asyncio.to_thread(client.request, "status")
        readiness = await asyncio.to_thread(client.request, "readiness")
        assert status["ok"] and readiness["ok"]
        return status["data"], readiness["data"]
    finally:
        await server.close()


@pytest.mark.parametrize(
    ("load_state", "active_state", "unit_file_state", "inspection_ok"),
    [
        ("loaded", "inactive", "disabled", True),
        ("masked", "inactive", "masked", True),
        ("masked", "inactive", "masked-runtime", True),
        ("not-found", "inactive", "disabled", False),
        ("bad-setting", "inactive", "disabled", False),
        ("error", "inactive", "disabled", False),
        (None, "inactive", "disabled", False),
        ("loaded", None, "disabled", False),
        ("loaded", "", "disabled", False),
        ("loaded", "unknown", "disabled", False),
        ("loaded", "unrecognized", "disabled", False),
        ("loaded", "inactive", None, False),
        ("loaded", "inactive", "", False),
        ("loaded", "inactive", "unknown", False),
        ("loaded", "inactive", "invalid", False),
        ("loaded", "inactive", "bad", False),
    ],
)
def test_timer_status_requires_known_systemd_properties(
    tmp_path, load_state, active_state, unit_file_state, inspection_ok
):
    properties: dict[str, str | None] = {
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "UnitFileState": "disabled",
        "Result": "success",
    }

    def runner(command):
        assert command[:3] == ["systemctl", "show", "example.timer"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="".join(f"{key}={value}\n" for key, value in properties.items() if value is not None),
            stderr="",
        )

    store = AutomationStore(tmp_path / "registry.sqlite3")
    store.initialize()
    store.controller_heartbeat(owner="synthetic-controller")
    policy = SystemTimerPolicy("example", "example.timer", "Example", "managed")
    controller = SystemTimerController(policies={"example": policy}, runner=runner, dropin_root=tmp_path, store=store)
    adopted = controller.adopt_current("example")
    properties.update(LoadState=load_state, ActiveState=active_state, UnitFileState=unit_file_state)
    service = AutomationControlService(
        store,
        timer_controller=controller,
        readiness_provider=lambda: {
            "ready": True,
            "checks": {"crm_digest_executor": "ready", "notification_transport": "ready"},
        },
    )

    status, readiness = asyncio.run(_read_control(service, tmp_path))
    row = status["system_timers"][0]
    assert row["desired_state"] == "off" and row["revision"] == adopted["revision"]
    assert row["actual_state"] == (active_state or "unknown")
    assert controller.inspect("example")["inspection_ok"] is inspection_ok
    assert status["readiness"]["ready"] is readiness["ready"] is inspection_ok
    if inspection_ok:
        assert row["reconcile_state"] == "in_sync"
        assert row["error_code"] is None
        assert readiness["warnings"] == []
    else:
        assert row["reconcile_state"] == "inspection_error"
        assert row["error_code"] == "system_timer_inspection_failed"
        assert readiness["checks"]["system_timer_reconciliation"] == "degraded"
        with pytest.raises(AutomationError, match="system_timer_inspection_failed"):
            controller.adopt_current("example")
        with pytest.raises(AutomationError, match="system_timer_inspection_failed"):
            controller.set_enabled("example", enabled=True)
        with pytest.raises(AutomationError, match="system_timer_inspection_failed"):
            controller.set_schedule("example", every_minutes=20)


@pytest.mark.parametrize(
    ("sub_state", "explicit_next", "expected_next"),
    [
        ("elapsed", "", None),
        ("elapsed", "stale-synthetic-date", None),
        ("waiting", "2026-11-21T03:45:05Z", "2026-11-21T03:45:05Z"),
        ("running", "", "2026-11-21T03:45:00Z"),
        (None, "", "2026-11-21T03:45:00Z"),
    ],
)
def test_periodic_timer_elapsed_substate_blocks_status_readiness(tmp_path, sub_state, explicit_next, expected_next):
    loaded_sub_state = "waiting"
    commands = []

    def runner(command):
        commands.append(command)
        assert command[:3] == ["systemctl", "show", "example.timer"]
        show_sub_state = f"SubState={loaded_sub_state}\n" if loaded_sub_state is not None else ""
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\nResult=success\n"
                f"{show_sub_state}NextElapseUSecRealtime={explicit_next}\nLastTriggerUSec=2026-11-21T03:30:00Z\n"
                "TimersMonotonic={ OnUnitActiveUSec=15min ; next_elapse=0 }\n"
            ),
            stderr="",
        )

    store = AutomationStore(tmp_path / "registry.sqlite3")
    store.initialize()
    store.controller_heartbeat(owner="synthetic-controller")
    policy = SystemTimerPolicy("example", "example.timer", "Example", "managed")
    controller = SystemTimerController(policies={"example": policy}, runner=runner, dropin_root=tmp_path, store=store)
    adopted = controller.adopt_current("example")
    loaded_sub_state = sub_state
    service = AutomationControlService(
        store,
        timer_controller=controller,
        readiness_provider=lambda: {
            "ready": True,
            "checks": {"crm_digest_executor": "ready", "notification_transport": "ready"},
        },
    )

    status, readiness = asyncio.run(_read_control(service, tmp_path))
    (row,) = status["system_timers"]
    (packet_timer,) = readiness["execution_packet"]["system_timers"]
    raw = controller.inspect("example")

    assert len(commands) == 4 and all("--property=SubState" in command for command in commands)
    assert raw["inspection_ok"] and raw["state"]["actual_state"] == "active"
    assert row["revision"] == adopted["revision"] and row["desired_state"] == "on"
    assert raw["state"]["next_run_at"] == row["next_run_at"] == expected_next
    if sub_state is not None:
        assert raw["state"]["sub_state"] == row["sub_state"] == packet_timer["sub_state"] == sub_state
    else:
        assert "sub_state" not in raw["state"] and "sub_state" not in row and "sub_state" not in packet_timer

    elapsed = sub_state == "elapsed"
    assert status["readiness"]["ready"] is readiness["ready"] is (not elapsed)
    assert row["actual_state"] == packet_timer["actual_state"] == ("error" if elapsed else "active")
    assert row["error_code"] == packet_timer["error_code"] == ("system_timer_elapsed" if elapsed else None)
    assert row["reconcile_state"] == packet_timer["reconcile_state"] == ("drift" if elapsed else "in_sync")
