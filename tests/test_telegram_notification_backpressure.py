import asyncio
from types import SimpleNamespace

import pytest
from test_telegram_bridge_durable import Client, _runtime

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_effects import EffectExecutor, EffectQueueError


@pytest.fixture
def notification_runtime(tmp_path, monkeypatch):
    config, monitor = _runtime(tmp_path)
    monitor.transport_ready = True
    monkeypatch.setattr(bridge, "_MUTATION_LOCK", asyncio.Lock())
    monkeypatch.setattr(bridge, "_load_reconciliation_state_request", object)
    monkeypatch.setattr(bridge, "_owner_peer_id", lambda *_args, **_kwargs: 84)

    async def owner(_client, _config):
        return 84, {"id": 84, "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_owner_target", owner)
    try:
        yield config, monitor, Client([])
    finally:
        monitor.inbox.close()


@pytest.mark.parametrize("unavailable", ["transport", "owner", "pending"])
def test_empty_incident_delivery_never_submits_to_effect_lane(notification_runtime, monkeypatch, unavailable):
    config, monitor, client = notification_runtime
    if unavailable != "pending":
        monitor.inbox.set_health("codex", "held", "synthetic_failure")
    if unavailable == "transport":
        monitor.transport_ready = False
    if unavailable == "owner":
        monkeypatch.setattr(bridge, "_owner_peer_id", lambda *_args, **_kwargs: None)

    async def forbidden_submit(*_args, **_kwargs):
        pytest.fail("Unavailable notification work must not enter the shared effect lane")

    monkeypatch.setattr(bridge, "_effect_executor", lambda: SimpleNamespace(submit=forbidden_submit))
    before = monitor.inbox.pending_notifications()
    asyncio.run(bridge._deliver_incident_notifications(client, config, monitor))
    assert monitor.inbox.pending_notifications() == before and client.sent == 0


@pytest.mark.parametrize("backpressure", ["timeout", "capacity"])
@pytest.mark.parametrize("recover_during_wait", [False, True], ids=["incident", "incident_and_recovery"])
def test_notification_backpressure_preserves_intent_and_delivers_after_progress(
    notification_runtime, monkeypatch, backpressure, recover_during_wait
):
    config, monitor, client = notification_runtime
    monitor.inbox.set_health("codex", "held", "synthetic_failure")
    pending = monitor.inbox.pending_notifications()
    assert len(pending) == 1

    async def scenario():
        async with asyncio.timeout(3):
            executor = EffectExecutor(queue_seconds=0.02 if backpressure == "timeout" else 5)
            monkeypatch.setattr(bridge, "_effect_executor", lambda: executor)
            entered, release = asyncio.Event(), asyncio.Event()
            iterations = asyncio.Queue()
            deliver = bridge._deliver_incident_notifications

            async def observed_delivery(*args, **kwargs):
                try:
                    return await deliver(*args, **kwargs)
                finally:
                    iterations.put_nowait(None)

            async def blocked():
                entered.set()
                await release.wait()

            async def queued():
                return None

            monkeypatch.setattr(bridge, "_deliver_incident_notifications", observed_delivery)
            active = asyncio.create_task(executor.submit(blocked, timeout=2))
            waiting, loop = [], None
            try:
                await entered.wait()
                if backpressure == "capacity":
                    waiting = [asyncio.create_task(executor.submit(queued, timeout=2)) for _ in range(8)]
                    await asyncio.sleep(0)
                    assert executor.queued == 8
                monitor.request_reconciliation()
                loop = asyncio.create_task(bridge._reconcile_incoming_loop(client, config, monitor))
                await asyncio.wait_for(iterations.get(), 1)
                await asyncio.sleep(0)
                assert not loop.done() and not executor.failed.is_set()
                assert monitor.inbox.pending_notifications() == pending and client.sent == 0
                assert not (config.state_dir / "idempotency.json").exists()
                if recover_during_wait:
                    await bridge._handle_operation(
                        client,
                        config,
                        {"operation": "job_health", "component": "codex", "state": "ready"},
                        inbound_monitor=monitor,
                    )
                release.set()
                await active
                await asyncio.gather(*waiting)
                await asyncio.wait_for(iterations.get(), 1)
                expected_sends = 2 if recover_during_wait else 1
                assert not loop.done() and client.sent == expected_sends
                assert monitor.inbox.pending_notifications() == []
                incident = monitor.inbox.read_incident(pending[0]["incident_id"])
                assert incident["opened_receipt"] == "owner-1"
                if recover_during_wait:
                    assert incident["recovery_receipt"] == "owner-2"
                ledger_path = config.state_dir / "idempotency.json"
                ledger = bridge._load_idempotency(ledger_path)
                assert len(ledger) == expected_sends
                assert {entry["message_id"] for entry in ledger.values()} == set(range(1, expected_sends + 1))
                assert all(entry["readback_verified"] is True for entry in ledger.values())
                before = ledger_path.read_bytes()
                monitor.request_reconciliation()
                await asyncio.wait_for(iterations.get(), 1)
                assert not loop.done() and client.sent == expected_sends and ledger_path.read_bytes() == before
            finally:
                release.set()
                tasks = [active, *waiting, *([loop] if loop is not None else [])]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await executor.close()

    asyncio.run(scenario())


def test_terminal_effect_hold_still_terminates_reconciliation(notification_runtime, monkeypatch):
    config, monitor, client = notification_runtime
    monitor.inbox.set_health("codex", "held", "synthetic_failure")
    pending = monitor.inbox.pending_notifications()

    async def scenario():
        executor = EffectExecutor()
        executor.failed.set()
        monkeypatch.setattr(bridge, "_effect_executor", lambda: executor)
        monitor.request_reconciliation()
        try:
            with pytest.raises(EffectQueueError, match=r"^bridge_effects_held$"):
                await asyncio.wait_for(bridge._reconcile_incoming_loop(client, config, monitor), 1)
            assert monitor.inbox.pending_notifications() == pending and client.sent == 0
        finally:
            await executor.close()

    asyncio.run(scenario())
