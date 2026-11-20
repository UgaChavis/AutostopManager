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

    async def read_control():
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

    status, readiness = asyncio.run(read_control())
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
