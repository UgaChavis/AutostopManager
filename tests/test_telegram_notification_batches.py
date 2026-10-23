import asyncio

import pytest
from test_telegram_notification_backpressure import notification_runtime as notification_runtime

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_effects import EffectExecutor


def _closed_incidents(monitor, count):
    incidents = []
    for number in range(count):
        incident = monitor.inbox.open_incident(f"synthetic_batch_{number}")
        monitor.inbox.recover_incident(incident["incident_id"])
        incidents.append(incident)
    return incidents


async def _stop_delivery(monitor, executor):
    if monitor.notification_task is not None:
        monitor.notification_task.cancel()
        await asyncio.gather(monitor.notification_task, return_exceptions=True)
    await executor.close()


@pytest.mark.parametrize("incident_count", [16, 17], ids=["32_notifications", "34_notifications"])
def test_verified_batch_drains_tail_and_yields_to_queued_work(notification_runtime, monkeypatch, incident_count):
    config, monitor, client = notification_runtime
    _closed_incidents(monitor, incident_count)

    async def scenario():
        async with asyncio.timeout(12):
            executor = EffectExecutor()
            monkeypatch.setattr(bridge, "_effect_executor", lambda: executor)
            last_send, release = asyncio.Event(), asyncio.Event()
            send = client.send_message

            async def pause_last_send(*args, **kwargs):
                result = await send(*args, **kwargs)
                if client.sent == 32:
                    last_send.set()
                    await release.wait()
                return result

            async def customer_work():
                # The next notification batch must join behind admitted work.
                assert client.sent == 32
                return "customer completed"

            monkeypatch.setattr(client, "send_message", pause_last_send)
            waiting = None
            try:
                bridge._schedule_incident_notifications(client, config, monitor)
                await last_send.wait()
                waiting = asyncio.create_task(executor.submit(customer_work, timeout=5))
                await asyncio.sleep(0)
                assert executor.queued == 1
                release.set()
                assert await waiting == "customer completed"
                await monitor.notification_task
                assert monitor.inbox.pending_notifications() == []
                assert client.sent == 2 * incident_count
                ledger = bridge._load_idempotency(config.state_dir / "idempotency.json")
                assert len(ledger) == client.sent
                assert all(entry["readback_verified"] is True for entry in ledger.values())
            finally:
                release.set()
                await _stop_delivery(monitor, executor)
                if waiting is not None:
                    await asyncio.gather(waiting, return_exceptions=True)

    asyncio.run(scenario())


def test_continuation_stops_at_unknown_send_in_second_batch(notification_runtime, monkeypatch):
    config, monitor, client = notification_runtime
    incidents = _closed_incidents(monitor, 17)

    async def scenario():
        async with asyncio.timeout(12):
            executor = EffectExecutor()
            monkeypatch.setattr(bridge, "_effect_executor", lambda: executor)
            send = client.send_message

            async def lose_33rd_response(*args, **kwargs):
                result = await send(*args, **kwargs)
                if client.sent == 33:
                    raise TimeoutError("synthetic response loss after acceptance")
                return result

            monkeypatch.setattr(client, "send_message", lose_33rd_response)
            try:
                bridge._schedule_incident_notifications(client, config, monitor)
                await monitor.notification_task
                assert client.sent == 33
                path = config.state_dir / "idempotency.json"
                before = path.read_bytes()
                ledger = bridge._load_idempotency(path)
                assert len(ledger) == 33
                assert sum(entry["readback_verified"] is True for entry in ledger.values()) == 32
                unknown = [entry for entry in ledger.values() if entry["readback_verified"] is False]
                assert len(unknown) == 1 and unknown[0]["message_id"] == 0
                assert monitor.inbox.read_incident(incidents[-1]["incident_id"])["opened_receipt"] is None
                # Progress and explicit rescheduling must neither resend nor forge a receipt.
                await executor.submit(lambda: asyncio.sleep(0), timeout=1)
                bridge._schedule_incident_notifications(client, config, monitor)
                await monitor.notification_task
                assert client.sent == 33 and path.read_bytes() == before
                assert monitor.inbox.read_incident(incidents[-1]["incident_id"])["opened_receipt"] is None
                assert len(monitor.inbox.pending_notifications()) == 1
            finally:
                await _stop_delivery(monitor, executor)

    asyncio.run(scenario())
