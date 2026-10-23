import asyncio

import pytest
from test_telegram_notification_backpressure import notification_runtime as notification_runtime

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_effects import EffectExecutor


def test_deferred_notification_observes_progress_before_background_wait(notification_runtime, monkeypatch):
    config, monitor, client = notification_runtime
    monitor.inbox.set_health("codex", "held", "synthetic_failure")

    async def scenario():
        executor = EffectExecutor(capacity=0)
        monkeypatch.setattr(bridge, "_effect_executor", lambda: executor)
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked():
            entered.set()
            await release.wait()

        active = asyncio.create_task(executor.submit(blocked, timeout=2))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            progress = await bridge._deliver_incident_notifications(client, config, monitor)
            assert isinstance(progress, asyncio.Event)
            release.set()
            await active
            # Progress has already happened before the background waiter exists.
            bridge._schedule_incident_notifications(client, config, monitor, retry_after=progress)
            await asyncio.wait_for(monitor.notification_task, 1)
            assert client.sent == 1 and monitor.inbox.pending_notifications() == []
            assert not monitor.reconcile_requested.is_set()
        finally:
            release.set()
            if monitor.notification_task is not None:
                monitor.notification_task.cancel()
                await asyncio.gather(monitor.notification_task, return_exceptions=True)
            await executor.close()
            await asyncio.gather(active, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("shutdown", ["close_executor", "cancel_waiter"])
def test_deferred_notification_shutdown_never_starts_delivery(notification_runtime, monkeypatch, shutdown):
    config, monitor, client = notification_runtime
    monitor.inbox.set_health("codex", "held", "synthetic_failure")
    pending = monitor.inbox.pending_notifications()

    async def scenario():
        executor = EffectExecutor(capacity=0)
        monkeypatch.setattr(bridge, "_effect_executor", lambda: executor)
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked():
            entered.set()
            await release.wait()

        active = asyncio.create_task(executor.submit(blocked, timeout=2))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            progress = await bridge._deliver_incident_notifications(client, config, monitor)
            assert isinstance(progress, asyncio.Event)
            bridge._schedule_incident_notifications(client, config, monitor, retry_after=progress)
            task = monitor.notification_task
            assert task is not None
            await asyncio.sleep(0)
            if shutdown == "close_executor":
                await executor.close()
                await asyncio.wait_for(task, 1)
            else:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            release.set()
            await asyncio.gather(active, return_exceptions=True)
            assert task.done() and client.sent == 0
            assert monitor.inbox.pending_notifications() == pending
            assert not (config.state_dir / "idempotency.json").exists()
        finally:
            release.set()
            if monitor.notification_task is not None:
                monitor.notification_task.cancel()
                await asyncio.gather(monitor.notification_task, return_exceptions=True)
            await executor.close()
            await asyncio.gather(active, return_exceptions=True)

    asyncio.run(scenario())
