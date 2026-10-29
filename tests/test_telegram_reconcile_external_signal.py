"""An observed private update must not disappear behind a moving history fence."""

import asyncio
from datetime import UTC, datetime, timedelta

from test_telegram_bridge_durable import DateOrderedClient, _message, _runtime

from autostop_manager import telegram_bridge as bridge


def test_callbacks_during_unstable_sweep_supply_one_bounded_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_load_reconciliation_state_request", object)
    monkeypatch.setattr(bridge, "MAX_RECONCILE_DIALOGS", 2)
    _config, monitor = _runtime(tmp_path)
    monitor.self_peer_id = 999
    monitor.transport_ready = True
    monitor.reconciliation_transport_hook = lambda: True
    now = datetime.now(UTC).replace(microsecond=0)
    messages = [
        _message(200, peer=42, out=True, at=now - timedelta(minutes=10)),
        _message(190, peer=43, out=True, at=now - timedelta(minutes=15)),
    ]
    for peer in (44, 45, 46):
        messages += [
            _message(peer, peer=peer, at=now - timedelta(minutes=45)),
            _message(peer + 100, peer=peer, out=True, at=now - timedelta(minutes=20)),
        ]
    client = DateOrderedClient(messages)

    async def sweep():
        monitor.reconcile_requested.clear()
        await bridge._reconcile_incoming_history(client, monitor)

    async def update(peer, identifier, at):
        message = _message(identifier, peer=peer, out=True, at=at)
        client.messages[identifier] = message
        client.update_revision += 1
        await bridge._capture_incoming_event(monitor, message)

    async def scenario():
        monitor.request_reconciliation()
        assert monitor.status()["intake_reconciliation"] == "in_progress"
        await sweep()
        for index, peer in enumerate((44, 45, 46)):
            await update(peer, 300 + index, now - timedelta(seconds=10 - index))
        assert monitor.reconciliation_external_pending
        await sweep()
        assert monitor.inbox.health()["intake"]["error_code"] == "history_reconciliation_changed"
        assert monitor.status()["intake_reconciliation"] == "in_progress"
        await sweep()
        await update(44, 310, now)
        # Coalesced callbacks do not replenish the transport retry budget.
        monitor.reconciliation_transport_retries = 2
        await sweep()
        assert monitor.reconcile_requested.is_set() and not monitor.reconciliation_stopped
        assert monitor.reconciliation_transport_retries == 2
        assert not monitor.reconciliation_external_pending
        assert monitor.status()["intake_reconciliation"] == "in_progress"
        count = 0
        while monitor.reconcile_requested.is_set():
            count += 1
            assert count <= 5
            await sweep()
        assert monitor.inbox.health()["intake"]["state"] == "ready"
        assert monitor.status()["intake_reconciliation"] == "complete"
        assert monitor.inbox.checkpoint(44) >= 44
        assert any(monitor.resolve(ref["event_id"]).message_id == 44 for ref in monitor.inbox.events())

    try:
        asyncio.run(scenario())
    finally:
        monitor.inbox.close()


def test_disabled_intake_does_not_buffer_private_updates(tmp_path):
    _config, monitor = _runtime(tmp_path)
    monitor.capture_enabled = False
    monitor.reconciliation_fence = (1, 0, 1)
    try:
        asyncio.run(bridge._capture_incoming_event(monitor, _message(101)))
        assert not monitor.reconciliation_external_pending
        assert not monitor.reconcile_requested.is_set()
    finally:
        monitor.inbox.close()


def test_new_external_request_consumes_buffer_without_leaving_extra_pass(tmp_path):
    _config, monitor = _runtime(tmp_path)
    monitor.reconciliation_external_pending = True
    try:
        monitor.request_reconciliation()
        assert monitor.reconcile_requested.is_set()
        assert not monitor.reconciliation_external_pending
    finally:
        monitor.inbox.close()


def test_capture_during_first_fence_request_is_buffered(tmp_path):
    _config, monitor = _runtime(tmp_path)
    monitor.reconciliation_running = True
    assert monitor.reconciliation_fence is None
    try:
        asyncio.run(bridge._capture_incoming_event(monitor, _message(101)))
        assert monitor.reconciliation_external_pending
    finally:
        monitor.inbox.close()


def test_callback_racing_initial_fence_is_cleared_after_stable_sweep(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_load_reconciliation_state_request", object)
    config, monitor = _runtime(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingFence(DateOrderedClient):
        async def __call__(self, request):
            if not entered.is_set():
                entered.set()
                await release.wait()
            return await super().__call__(request)

    async def no_notifications(*_args):
        return None

    monkeypatch.setattr(bridge, "_deliver_incident_notifications", no_notifications)

    async def scenario():
        client = BlockingFence([_message(100)])
        monitor.request_reconciliation()
        task = asyncio.create_task(bridge._reconcile_incoming_loop(client, config, monitor))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert monitor.reconciliation_fence is None and monitor.reconciliation_running
            await bridge._capture_incoming_event(monitor, _message(101, out=True))
            assert monitor.reconciliation_external_pending
            release.set()
            for _ in range(20):
                await asyncio.sleep(0)
                if monitor.inbox.health().get("intake", {}).get("state") == "ready":
                    break
            assert monitor.inbox.health()["intake"]["state"] == "ready"
            assert not monitor.reconciliation_external_pending
            assert not monitor.reconcile_requested.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        monitor.inbox.close()


def test_one_buffered_callback_never_causes_unbounded_rescans(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_load_reconciliation_state_request", object)
    _config, monitor = _runtime(tmp_path)

    class ChangingClient(DateOrderedClient):
        async def __call__(self, request):
            self.update_revision += 1
            return await super().__call__(request)

    client = ChangingClient([_message(100)])

    async def sweep():
        monitor.reconcile_requested.clear()
        await bridge._reconcile_incoming_history(client, monitor)

    async def scenario():
        await sweep()
        monitor.reconciliation_running = True
        await bridge._capture_incoming_event(monitor, _message(101, out=True))
        monitor.reconciliation_running = False
        await sweep()
        assert monitor.reconcile_requested.is_set()
        assert not monitor.reconciliation_external_pending
        await sweep()
        assert monitor.reconcile_requested.is_set()
        await sweep()
        assert monitor.reconciliation_stopped and not monitor.reconcile_requested.is_set()
        assert len(client.dialog_calls) == 4

    try:
        asyncio.run(scenario())
    finally:
        monitor.inbox.close()


def test_disabled_duty_never_consumes_pending_signal_for_rescan(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_load_reconciliation_state_request", object)
    _config, monitor = _runtime(tmp_path)

    class ChangingClient(DateOrderedClient):
        async def __call__(self, request):
            self.update_revision += 1
            return await super().__call__(request)

    client = ChangingClient([_message(100)])

    async def scenario():
        await bridge._reconcile_incoming_history(client, monitor)
        monitor.reconcile_requested.clear()
        monitor.reconciliation_external_pending = True
        monitor.capture_enabled = False
        await bridge._reconcile_incoming_history(client, monitor)
        assert monitor.reconciliation_stopped
        assert not monitor.reconcile_requested.is_set()

    try:
        asyncio.run(scenario())
    finally:
        monitor.inbox.close()


def test_buffered_callback_does_not_extend_transport_failure_budget(tmp_path, monkeypatch):
    config, monitor = _runtime(tmp_path)
    monitor.self_peer_id = 999
    attempts = []

    async def failing_read(_client, active_monitor):
        attempts.append(1)
        if len(attempts) == 1:
            await bridge._capture_incoming_event(active_monitor, _message(101, out=True))
            assert active_monitor.reconciliation_external_pending
        raise ConnectionResetError("synthetic network failure")

    async def no_notifications(*_args):
        return None

    monkeypatch.setattr(bridge, "_reconcile_incoming_history", failing_read)
    monkeypatch.setattr(bridge, "_deliver_incident_notifications", no_notifications)

    async def scenario():
        monitor.request_reconciliation()
        task = asyncio.create_task(bridge._reconcile_incoming_loop(None, config, monitor))
        try:
            for _ in range(20):
                await asyncio.sleep(0)
                if monitor.reconciliation_stopped:
                    break
            assert monitor.reconciliation_stopped
            assert len(attempts) == 1 + bridge.MAX_HISTORY_TRANSPORT_RETRIES
            assert not monitor.reconcile_requested.is_set()
            assert not monitor.reconciliation_external_pending
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        monitor.inbox.close()
