"""An intake hold must remain visible even when the transport and worker are ready."""

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from types import ModuleType, SimpleNamespace

import pytest
from telegram_duty_fakes import duty_fixture
from test_telegram_bridge_durable import Client, _message, _runtime

from autostop_manager import telegram_bridge as bridge


def test_durable_status_exposes_recorded_health_without_claiming_delivery(tmp_path):
    config, monitor = _runtime(tmp_path)
    client = Client([])
    try:
        monitor.transport_ready = True
        for state, code in [("held", "history_reconciliation_changed"), ("ready", None)]:
            monitor.inbox.set_health("intake", state, code)
            expected = {"state": state, "error_code": code}
            status = asyncio.run(
                bridge._handle_operation(client, config, {"operation": "status"}, inbound_monitor=monitor)
            )
            monitor_status = asyncio.run(
                bridge._handle_operation(client, config, {"operation": "monitor_status"}, inbound_monitor=monitor)
            )
            job_status = asyncio.run(
                bridge._handle_operation(client, config, {"operation": "job_status"}, inbound_monitor=monitor)
            )
            assert status["transport_ready"] is True and status["inbound_enabled"] is True
            assert status["intake_health"] == monitor_status["monitor"]["intake_health"] == expected
            assert job_status["status"]["health"]["intake"] == expected
            assert status["intake_reconciliation"] == ("held" if state == "held" else "unknown")
            assert monitor_status["monitor"]["intake_reconciliation"] == status["intake_reconciliation"]
    finally:
        monitor.inbox.close()


@pytest.mark.parametrize(
    ("health", "expected"),
    [
        (
            {"state": "held", "error_code": "history_reconciliation_changed"},
            {"state": "held", "error_code": "history_reconciliation_changed"},
        ),
        ({"state": "ready", "error_code": None}, {"state": "ready", "error_code": None}),
        (None, {"state": "unknown", "error_code": None}),
        ({"state": "ready"}, {"state": "unknown", "error_code": None}),
        ({"state": "ready", "error_code": "history_reconciliation_changed"}, {"state": "unknown", "error_code": None}),
        ({"state": "held", "error_code": None}, {"state": "unknown", "error_code": None}),
        ({"state": "held", "error_code": "private-payload"}, {"state": "held", "error_code": "other"}),
    ],
)
def test_duty_status_reports_health_without_changing_wake_state(tmp_path, health, expected):
    script, env, _, _, _ = duty_fixture(
        tmp_path, {"ok": True, "enabled": True, "connected": True, "ready": True, "status_fresh": True}
    )
    bridge_state = json.loads(env["BRIDGE_STATE"])
    bridge_state["intake_health"] = health
    env["BRIDGE_STATE"] = json.dumps(bridge_state)
    result = subprocess.run(["bash", str(script), "--status"], env=env, capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    assert status["intake_health"] == expected
    assert status["intake_reconciliation"] == "unknown"
    assert status["wake_ready"] is True and status["inbound_enabled"] is True
    assert "private-payload" not in result.stdout


@pytest.mark.parametrize("field", ["intake_health", "transport_ready", "inbound_enabled"])
def test_duty_rejects_inconsistent_complete_reconciliation(tmp_path, field):
    script, env, _, _, _ = duty_fixture(tmp_path, {"ok": True})
    bridge_state = json.loads(env["BRIDGE_STATE"])
    bridge_state.update(intake_health={"state": "ready", "error_code": None}, intake_reconciliation="complete")
    bridge_state[field] = None
    env["BRIDGE_STATE"] = json.dumps(bridge_state)
    result = subprocess.run(["bash", str(script), "--status"], env=env, capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["intake_reconciliation"] == "unknown"


@pytest.mark.parametrize("state", ["unknown", "in_progress", "held", "complete"])
def test_duty_reports_reconciliation_state(tmp_path, state):
    script, env, _, _, _ = duty_fixture(tmp_path, {"ok": True})
    bridge_state = json.loads(env["BRIDGE_STATE"])
    bridge_state.update(intake_health={"state": "ready", "error_code": None}, intake_reconciliation=state)
    env["BRIDGE_STATE"] = json.dumps(bridge_state)
    result = subprocess.run(["bash", str(script), "--status"], env=env, capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["intake_reconciliation"] == state


def test_current_reconciliation_requires_new_sweep_and_invalidates_inflight_generation(tmp_path, monkeypatch):
    config, monitor = _runtime(tmp_path)
    monitor.transport_ready = True
    monitor.reconciliation_transport_hook = lambda: True
    monitor.inbox.set_health("intake", "ready", None)
    monkeypatch.setattr(bridge, "_load_reconciliation_state_request", object)
    client = Client([])

    async def scenario():
        entered, release, iteration, resume = (asyncio.Event() for _ in range(4))
        original = bridge._reconciliation_state_fence
        calls = 0

        async def blocked_fence(current):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
            return await original(current)

        async def no_notifications(*_args):
            if calls == 2:
                iteration.set()
                await resume.wait()

        monkeypatch.setattr(bridge, "_reconciliation_state_fence", blocked_fence)
        monkeypatch.setattr(bridge, "_deliver_incident_notifications", no_notifications)
        assert monitor.intake_reconciliation() == "unknown"  # persisted ready is not process evidence
        monitor.request_reconciliation()
        assert monitor.intake_reconciliation() == "in_progress"
        task = asyncio.create_task(bridge._reconcile_incoming_loop(client, config, monitor))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert monitor.intake_reconciliation() == "in_progress"
            monitor.request_reconciliation()  # new signal races with the first sweep
            release.set()
            await asyncio.wait_for(iteration.wait(), 2)
            assert monitor.intake_reconciliation() == "in_progress"
            assert monitor.reconciliation_completed_revision is None
            resume.set()
            for _ in range(1000):
                if monitor.intake_reconciliation() == "complete":
                    break
                await asyncio.sleep(0.001)
            assert monitor.intake_reconciliation() == "complete"
            restarted = bridge.DurableInboundMonitor(monitor.inbox, enabled=True)
            restarted.transport_ready = True
            assert restarted.intake_reconciliation() == "unknown"
            monitor.request_reconciliation()
            assert monitor.intake_reconciliation() == "in_progress"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        monitor.inbox.close()


@pytest.mark.parametrize("blocked_call", [1, 2])
def test_reconnect_during_sweep_never_marks_duty_window_recovered(tmp_path, monkeypatch, blocked_call):
    _config, monitor = _runtime(tmp_path)
    monitor.transport_ready = True
    now = datetime.now(UTC)
    monitor.inbox.duty_set(False, at=(now - timedelta(minutes=30)).isoformat())
    monitor.inbox.duty_set(True, at=(now - timedelta(minutes=10)).isoformat())
    entered, release_fence, entered_reconnect, release_reconnect = (asyncio.Event() for _ in range(4))
    calls = 0
    starts = []

    async def fence(_client):
        nonlocal calls
        calls += 1
        if calls == blocked_call:
            entered.set()
            await release_fence.wait()
        return (1, 0, 1)

    async def original_callback():
        entered_reconnect.set()
        await release_reconnect.wait()

    def original_start(error):
        starts.append(error)
        return "original-result"

    sender = SimpleNamespace(_auto_reconnect_callback=original_callback, _start_reconnect=original_start)
    client = Client([])
    client._sender = sender
    monkeypatch.setattr(bridge, "_verified_reconnect_start", lambda _sender, _start: True)
    monkeypatch.setattr(bridge, "_reconciliation_state_fence", fence)
    bridge._install_reconnect_signal(client, monitor)
    monitor.inbox.set_health("intake", "ready", None)
    monitor.reconciliation_completed_revision = monitor.reconciliation_signal_revision

    async def scenario():
        assert monitor.intake_reconciliation() == "complete"
        task = asyncio.create_task(bridge._reconcile_incoming_history(client, monitor))
        callback = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            error = ConnectionResetError("synthetic")
            assert sender._start_reconnect(error) == "original-result"
            assert starts == [error]
            assert monitor.intake_reconciliation() == "unknown"
            callback = asyncio.create_task(sender._auto_reconnect_callback())
            await asyncio.wait_for(entered_reconnect.wait(), 2)
            release_fence.set()
            await asyncio.wait_for(task, 2)
            assert monitor.inbox.duty_windows()[0]["recovered_at"] is None
            assert monitor.inbox.health()["intake"]["state"] == "held"
            assert not monitor.reconcile_requested.is_set()
            release_reconnect.set()
            await asyncio.wait_for(callback, 2)
            assert monitor.intake_reconciliation() == "in_progress"
            monitor.reconcile_requested.clear()
            await bridge._reconcile_incoming_history(client, monitor)
            assert monitor.inbox.duty_windows()[0]["recovered_at"] is not None
            assert monitor.intake_reconciliation() == "complete"
        finally:
            for pending in (task, callback):
                if pending is not None and not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        monitor.inbox.close()


def test_complete_requires_supported_and_unchanged_reconnect_hooks(tmp_path, monkeypatch):
    _config, monitor = _runtime(tmp_path)
    monitor.transport_ready = True
    monitor.inbox.set_health("intake", "ready", None)
    monitor.reconciliation_completed_revision = monitor.reconciliation_signal_revision

    async def original_callback():
        return None

    sender = SimpleNamespace(_auto_reconnect_callback=original_callback, _start_reconnect=lambda error: error)
    client = Client([])
    client._sender = sender
    try:
        monkeypatch.setattr(bridge, "_verified_reconnect_start", lambda _sender, _start: True)
        bridge._install_reconnect_signal(client, monitor)
        assert monitor.intake_reconciliation() == "complete"
        tracked = sender._start_reconnect
        sender._start_reconnect = lambda error: error
        assert monitor.intake_reconciliation() == "unknown"
        sender._start_reconnect = tracked
        client._sender = SimpleNamespace()
        assert monitor.intake_reconciliation() == "unknown"
        client._sender = sender
        sender._auto_reconnect_callback = original_callback
        assert monitor.intake_reconciliation() == "unknown"
    finally:
        monitor.inbox.close()


def test_unsupported_reconnect_contract_never_reports_complete(tmp_path, monkeypatch):
    _config, monitor = _runtime(tmp_path)
    monitor.transport_ready = True
    monitor.inbox.set_health("intake", "ready", None)
    monitor.reconciliation_completed_revision = monitor.reconciliation_signal_revision

    async def original_callback():
        return None

    sender = SimpleNamespace(_auto_reconnect_callback=original_callback, _start_reconnect=lambda error: error)
    client = Client([])
    client._sender = sender
    monkeypatch.setattr(bridge, "_supported_reconnect_tracking", lambda: False)
    try:
        bridge._install_reconnect_signal(client, monitor)
        assert monitor.intake_reconciliation() == "unknown"
    finally:
        monitor.inbox.close()


@pytest.mark.parametrize(("installed", "supported"), [("1.44.0", True), ("1.45.0", True), ("1.43.9", False)])
def test_reconnect_tracking_is_restricted_to_verified_versions(monkeypatch, installed, supported):
    monkeypatch.setattr(bridge, "version", lambda _name: installed)
    assert bridge._supported_reconnect_tracking() is supported


def test_preinstalled_reconnect_override_is_unverified(tmp_path, monkeypatch):
    _config, monitor = _runtime(tmp_path)
    monitor.transport_ready = True
    monitor.inbox.set_health("intake", "ready", None)
    monitor.reconciliation_completed_revision = monitor.reconciliation_signal_revision

    class FakeSender:
        def _start_reconnect(self, error):
            return error

    async def callback():
        return None

    sender = FakeSender()
    sender._auto_reconnect_callback = callback
    sender._start_reconnect = lambda error: error
    client = Client([])
    client._sender = sender
    package = ModuleType("telethon")
    package.__path__ = []
    network = ModuleType("telethon.network")
    network.__path__ = []
    module = ModuleType("telethon.network.mtprotosender")
    module.MTProtoSender = FakeSender
    monkeypatch.setitem(sys.modules, "telethon", package)
    monkeypatch.setitem(sys.modules, "telethon.network", network)
    monkeypatch.setitem(sys.modules, "telethon.network.mtprotosender", module)
    monkeypatch.setattr(bridge, "_supported_reconnect_tracking", lambda: True)
    try:
        bridge._install_reconnect_signal(client, monitor)
        assert monitor.intake_reconciliation() == "unknown"
    finally:
        monitor.inbox.close()


@pytest.mark.parametrize("missing_hook", ["unsupported", "replaced"])
def test_unverified_transport_hook_preserves_closed_windows(tmp_path, monkeypatch, missing_hook):
    _config, monitor = _runtime(tmp_path)
    monitor.transport_ready = True
    now = datetime.now(UTC)
    monitor.inbox.duty_set(False, at=(now - timedelta(minutes=30)).isoformat())
    monitor.inbox.duty_set(True, at=(now - timedelta(minutes=10)).isoformat())
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def fence(_client):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            await release.wait()
        return (1, 0, 1)

    async def callback():
        return None

    sender = SimpleNamespace(_auto_reconnect_callback=callback, _start_reconnect=lambda error: None)
    client = Client([])
    client._sender = sender
    monkeypatch.setattr(bridge, "_verified_reconnect_start", lambda _sender, _start: missing_hook != "unsupported")
    monkeypatch.setattr(bridge, "_reconciliation_state_fence", fence)
    bridge._install_reconnect_signal(client, monitor)

    async def scenario():
        task = asyncio.create_task(bridge._reconcile_incoming_history(client, monitor))
        try:
            if missing_hook == "replaced":
                await asyncio.wait_for(entered.wait(), 2)
                sender._start_reconnect = lambda error: None
                release.set()
            await asyncio.wait_for(task, 2)
            assert monitor.inbox.duty_windows()[0]["recovered_at"] is None
            assert monitor.inbox.health()["intake"] == {
                "state": "held",
                "error_code": "history_reconciliation_unverified",
            }
            assert monitor.intake_reconciliation() == "held"
            assert not monitor.reconcile_requested.is_set()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        monitor.inbox.close()


def test_reconnect_mid_peer_iterator_preserves_checkpoint_and_gap(tmp_path, monkeypatch):
    _config, monitor = _runtime(tmp_path)
    monitor.transport_ready = True
    monitor.inbox.mark_gap(
        peer_id=42, message_id=101, received_at=datetime.now(UTC).isoformat(), reason_code="capacity"
    )
    monkeypatch.setattr(bridge, "_load_reconciliation_state_request", object)
    monkeypatch.setattr(bridge, "_verified_reconnect_start", lambda _sender, _start: True)

    async def callback():
        return None

    sender = SimpleNamespace(_auto_reconnect_callback=callback, _start_reconnect=lambda error: None)

    class PartialClient(Client):
        incomplete = True

        async def iter_messages(self, entity, *, min_id, reverse, limit):
            if self.incomplete:
                self.incomplete = False
                yield self.messages[100]
                sender._start_reconnect(ConnectionResetError("synthetic"))
                # Deliberately simulate a provider omitting an intermediate
                # message without raising; this is not an observed live claim.
                yield self.messages[102]
                return
            async for message in super().iter_messages(entity, min_id=min_id, reverse=reverse, limit=limit):
                yield message

    client = PartialClient([_message(100), _message(101), _message(102)])
    client._sender = sender
    bridge._install_reconnect_signal(client, monitor)

    async def scenario():
        await bridge._reconcile_incoming_history(client, monitor)
        assert monitor.inbox.checkpoint(42) == 0
        assert monitor.inbox.gaps()[0]["message_id"] == 101
        assert monitor.inbox.health()["intake"]["state"] == "held"
        assert not monitor.reconcile_requested.is_set()
        await sender._auto_reconnect_callback()
        monitor.reconcile_requested.clear()
        await bridge._reconcile_incoming_history(client, monitor)
        assert {monitor.resolve(row["event_id"]).message_id for row in monitor.events()} == {100, 101, 102}
        assert monitor.inbox.checkpoint(42) == 102 and monitor.inbox.gaps() == []
        assert monitor.inbox.health()["intake"]["state"] == "ready"

    try:
        asyncio.run(scenario())
    finally:
        monitor.inbox.close()
