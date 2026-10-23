import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest
from test_telegram_bridge_durable import Client
from test_telegram_notification_backpressure import notification_runtime as notification_runtime

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_effects import EffectExecutor


async def _wait_until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0.001)


async def _stop_new_notifications(previous):
    tasks = set(bridge._BACKGROUND_NOTIFICATIONS) - previous
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def test_health_signal_after_factory_drained_before_waiter_resumes_is_not_lost(notification_runtime, monkeypatch):
    config, monitor, client = notification_runtime

    async def scenario():
        async with asyncio.timeout(3):
            executor = EffectExecutor()
            monkeypatch.setattr(bridge, "_effect_executor", lambda: executor)
            previous = set(bridge._BACKGROUND_NOTIFICATIONS)
            drained, resume = asyncio.Event(), asyncio.Event()
            original_submit = executor.submit
            first = True

            async def delayed_return(*args, **kwargs):
                nonlocal first
                result = await original_submit(*args, **kwargs)
                if first:
                    first = False
                    drained.set()
                    await resume.wait()
                return result

            monkeypatch.setattr(executor, "submit", delayed_return)
            try:
                await bridge._handle_operation(
                    client,
                    config,
                    {"operation": "job_health", "component": "codex", "state": "held", "error_code": "first_failure"},
                    inbound_monitor=monitor,
                )
                await drained.wait()
                assert client.sent == 1 and monitor.inbox.pending_notifications() == []
                first_incident = next(iter(bridge._load_idempotency(config.state_dir / "idempotency.json")))
                # The first network factory has committed its receipt, but its owned delivery task is still active.
                await bridge._handle_operation(
                    client,
                    config,
                    {"operation": "job_health", "component": "codex", "state": "held", "error_code": "second_failure"},
                    inbound_monitor=monitor,
                )
                pending = monitor.inbox.pending_notifications()
                assert len(pending) == 2 and {item["kind"] for item in pending} == {"error", "recovery"}
                resume.set()
                await _wait_until(lambda: not monitor.inbox.pending_notifications())
                assert client.sent == 3
                ledger = bridge._load_idempotency(config.state_dir / "idempotency.json")
                assert first_incident in ledger and len(ledger) == 3
                assert {entry["message_id"] for entry in ledger.values()} == {1, 2, 3}
                assert all(entry["readback_verified"] is True for entry in ledger.values())
                for item in pending:
                    incident = monitor.inbox.read_incident(item["incident_id"])
                    field = "recovery_receipt" if item["kind"] == "recovery" else "opened_receipt"
                    assert incident[field] in {"owner-2", "owner-3"}
            finally:
                resume.set()
                await _stop_new_notifications(previous)
                await executor.close()

    asyncio.run(scenario())


def test_unknown_owner_send_is_not_retried_when_another_effect_completes(notification_runtime, monkeypatch):
    config, monitor, client = notification_runtime
    monitor.inbox.set_health("codex", "held", "unknown_send")
    pending = monitor.inbox.pending_notifications()

    async def scenario():
        async with asyncio.timeout(3):
            executor = EffectExecutor()
            monkeypatch.setattr(bridge, "_effect_executor", lambda: executor)
            previous = set(bridge._BACKGROUND_NOTIFICATIONS)
            finished = asyncio.Event()
            original_send, original_submit = client.send_message, executor.submit
            notification_submissions = 0

            async def accepted_without_response(*args, **kwargs):
                await original_send(*args, **kwargs)
                raise TimeoutError("synthetic response loss after acceptance")

            async def unrelated():
                return "local progress"

            async def observed_submit(factory, **kwargs):
                nonlocal notification_submissions
                if factory is not unrelated:
                    notification_submissions += 1
                try:
                    return await original_submit(factory, **kwargs)
                finally:
                    if factory is not unrelated:
                        finished.set()

            monkeypatch.setattr(client, "send_message", accepted_without_response)
            monkeypatch.setattr(executor, "submit", observed_submit)
            try:
                bridge._schedule_incident_notifications(client, config, monitor)
                await finished.wait()
                await asyncio.sleep(0)
                assert client.sent == notification_submissions == 1
                path = config.state_dir / "idempotency.json"
                before = path.read_bytes()
                entry = next(iter(bridge._load_idempotency(path).values()))
                assert entry["message_id"] == 0 and entry["readback_verified"] is False
                assert await executor.submit(unrelated, timeout=1) == "local progress"
                await asyncio.sleep(0.05)
                assert client.sent == notification_submissions == 1
                assert path.read_bytes() == before and monitor.inbox.pending_notifications() == pending
                assert not executor.failed.is_set()
            finally:
                await _stop_new_notifications(previous)
                await executor.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("fatal", [False, True], ids=["healthy_lane", "uncooperative_effect"])
def test_daemon_stays_live_for_healthy_lane_but_stops_for_actual_failed_executor(tmp_path, monkeypatch, fatal):
    async def scenario(socket_dir):
        async with asyncio.timeout(4):

            class Transport(Client):
                disconnected_count = 0

                def __init__(self, *_args, **_kwargs):
                    super().__init__([])
                    self.session = SimpleNamespace(save_entities=True)
                    self._sender = SimpleNamespace(_auto_reconnect_callback=self.reconnect)
                    self.disconnected = asyncio.get_running_loop().create_future()

                async def reconnect(self):
                    pass

                def add_event_handler(self, *_args):
                    pass

                async def connect(self):
                    pass

                async def is_user_authorized(self):
                    return True

                async def disconnect(self):
                    type(self).disconnected_count += 1
                    if not self.disconnected.done():
                        self.disconnected.set_result(None)

            owner_path = tmp_path / "owner.json"
            monkeypatch.setenv(bridge.WORK_DURABLE_ENVIRONMENT, "1")
            monkeypatch.setattr(bridge, "WORK_OWNER_CONFIG_PATH", owner_path)
            monkeypatch.setattr(bridge, "_load_telethon", lambda: (Transport, object(), object()))
            monkeypatch.setattr(
                bridge, "_load_telegram_events", lambda: SimpleNamespace(NewMessage=lambda **_kwargs: object())
            )
            monkeypatch.setattr(bridge, "_load_reconciliation_state_request", object)
            monkeypatch.setattr(bridge, "_owner_peer_id", lambda *_args, **_kwargs: None)
            monkeypatch.setattr(
                bridge,
                "_configure_durable_instructions",
                lambda inbox: inbox.configure_instructions(str(tmp_path), "a" * 40),
            )
            config = bridge.TelegramConfig(
                123456,
                "a" * 32,
                state_dir=tmp_path / "state",
                session_path=tmp_path / "state" / "account",
                socket_path=Path(socket_dir) / "bridge.sock",
                owner_config_path=owner_path,
            )
            executor = EffectExecutor(queue_seconds=0.02, cleanup_seconds=0.02)
            monkeypatch.setattr(bridge, "_effect_executor", lambda: executor)
            entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
            ready = asyncio.Event()
            start_server = asyncio.start_unix_server

            async def server(*args, **kwargs):
                result = await start_server(*args, **kwargs)
                ready.set()
                return result

            async def owned_effect():
                entered.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        cancelled.set()
                        if not fatal:
                            raise

            monkeypatch.setattr(bridge.asyncio, "start_unix_server", server)
            active = asyncio.create_task(executor.submit(owned_effect, timeout=0.02 if fatal else 3))
            daemon = None
            try:
                await entered.wait()
                # Disable the process-exit watchdog for this cooperative fake-transport unit test only.
                daemon = asyncio.create_task(
                    bridge.run_daemon(config, monitor_incoming=True, _cleanup_watchdog=SimpleNamespace(ident=1))
                )
                await ready.wait()
                if fatal:
                    with pytest.raises(bridge.BridgeError, match=r"^bridge_effects_held$"):
                        await asyncio.wait_for(asyncio.shield(daemon), 1)
                    assert executor.failed.is_set() and cancelled.is_set()
                    assert Transport.disconnected_count == 1 and not config.socket_path.exists()
                else:
                    await asyncio.sleep(0.06)
                    assert not daemon.done() and not executor.failed.is_set() and not cancelled.is_set()
                    assert config.socket_path.exists() and Transport.disconnected_count == 0
                    release.set()
                    await active
                    assert not daemon.done()
            finally:
                release.set()
                if daemon is not None:
                    daemon.cancel()
                    await asyncio.gather(daemon, return_exceptions=True)
                await executor.close()
                await asyncio.gather(active, return_exceptions=True)
                assert Transport.disconnected_count == 1 and not config.socket_path.exists()

    with TemporaryDirectory(prefix="tg-notification-") as socket_dir:
        asyncio.run(scenario(socket_dir))
