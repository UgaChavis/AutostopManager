"""Regression cases found by independent review of PR 43."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_telegram_wake import THREAD

from autostop_manager import telegram_bridge as bridge
from autostop_manager import telegram_wake as wake


def test_prestart_resume_from_previous_connection_cannot_authorize_new_socket():
    async def scenario():
        app = wake.AppServer(wake.WakeConfig(THREAD))
        app.managed = app.connected = True
        app.ready_event.set()
        app.generation = 1
        replaced = asyncio.Event()
        requests = []
        resumes = []

        async def resume():
            resumes.append(app.generation)
            if len(resumes) == 1:
                # The old resume reply was received before replacement, but its
                # waiter is scheduled after the new socket starts initialization.
                app.generation += 1
                app.ready_event.clear()
                app.recovery_state = "reconnecting"
                replaced.set()

        async def request(method, params):
            requests.append(method)
            if not app.ready_event.is_set():
                raise wake.RPCRejected("new connection is not initialized")
            turn = {"id": "synthetic-turn", "status": "completed"}
            for notification in ("turn/started", "turn/completed"):
                app.events.put_nowait({"method": notification, "params": {"turn": turn}})
            return {"turn": turn}

        app.resume = AsyncMock(side_effect=resume)
        app.request = AsyncMock(side_effect=request)
        task = asyncio.create_task(app.run_turn("synthetic request"))
        try:
            await asyncio.wait_for(replaced.wait(), 1)
            await asyncio.sleep(0)
            assert requests == [], "turn/start reached an uninitialized replacement socket"
            assert not task.done()
            app.ready_event.set()
            app.recovery_state = "ready"
            await asyncio.wait_for(task, 1)
            assert resumes == [1, 2]
            assert requests == ["turn/start"]
            assert not app.outcome_unknown and app.active_turn is None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_recovery_burst_preserves_every_accepted_bridge_reference(monkeypatch, capsys):
    async def scenario():
        monitor = bridge.InboundMonitor()
        app = wake.AppServer(wake.WakeConfig(THREAD))
        dispatcher = wake.WakeDispatcher(app, 123)
        release = asyncio.Event()
        accepted = []
        processed = []

        async def deliver(request, path):
            try:
                result = dispatcher.accept(request, 123)
            except wake.WakeError as exc:
                return {"ok": False, "error": str(exc)}
            accepted.append(request["event_id"])
            return result

        async def process(text):
            await release.wait()
            event_id = next(word.rstrip(";") for word in text.split() if word.startswith("inbound-"))
            monitor.require_open(event_id)
            monitor.mark_no_reply_needed(event_id)
            processed.append(event_id)

        monkeypatch.setenv(bridge.WORK_WAKE_ENVIRONMENT, "/synthetic/wake.sock")
        monkeypatch.setattr(wake, "local_request", deliver)
        app.run_turn = AsyncMock(side_effect=process)
        dispatcher.worker = asyncio.create_task(dispatcher.work())
        try:
            for number in range(1, bridge.MAX_INBOUND_MONITOR_EVENTS + 3):
                await bridge._capture_incoming_event(
                    monitor, SimpleNamespace(is_private=True, out=False, chat_id=123, id=number)
                )
                await asyncio.sleep(0)
            assert dispatcher.active and dispatcher.queue.qsize() == 31
            assert len(accepted) == 32
            assert monitor.status()["dropped_open_events"] == 0
            assert monitor.status()["rejected_events"] == 2
            for event_id in accepted:
                monitor.require_open(event_id)
            release.set()
            await asyncio.wait_for(dispatcher.queue.join(), 1)
            assert processed == accepted and dispatcher.completed == 32 and dispatcher.failed == 0
            # A deliberately closed reference can be recycled; recovery of
            # retained requests does not leave admission permanently full.
            await bridge._capture_incoming_event(
                monitor, SimpleNamespace(is_private=True, out=False, chat_id=123, id=35)
            )
            await asyncio.wait_for(dispatcher.queue.join(), 1)
            assert len(processed) == 33 and monitor.status()["dropped_open_events"] == 0
        finally:
            await dispatcher.pause()

    asyncio.run(scenario())
    assert capsys.readouterr().err == "work_telegram_monitor_full=true\n" * 2


def test_full_wake_monitor_keeps_duplicates_and_closed_slot_reuse():
    monitor = bridge.InboundMonitor(max_events=1)
    event = SimpleNamespace(is_private=True, out=False, chat_id=123, id=1)
    original = monitor.record(event, preserve_open=True)
    assert original is not None
    assert monitor.record(event, preserve_open=True) is None
    with pytest.raises(bridge.BridgeError, match="inbound_monitor_full"):
        monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=123, id=2), preserve_open=True)
    assert monitor.require_open(original).message_id == 1
    monitor.mark_no_reply_needed(original)
    replacement = monitor.record(SimpleNamespace(is_private=True, out=False, chat_id=123, id=2), preserve_open=True)
    assert replacement is not None and replacement != original
    assert monitor.require_open(replacement).message_id == 2
    assert monitor.status()["dropped_open_events"] == 0


def test_known_turn_reconciliation_waits_for_full_replacement_initialization():
    async def scenario():
        app = wake.AppServer(wake.WakeConfig(THREAD))
        app.managed = app.connected = True
        app.generation = 2
        app.active_turn = "synthetic-turn"
        app.outcome_unknown = True
        app._turn_metadata = AsyncMock(return_value="completed")
        task = asyncio.create_task(app._wait_turn(1))
        try:
            await asyncio.sleep(0)
            app._turn_metadata.assert_not_awaited()
            assert not task.done()
            app.ready_event.set()
            await asyncio.wait_for(task, 1)
            app._turn_metadata.assert_awaited_once()
            assert not app.outcome_unknown and app.active_turn is None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_pause_does_not_restart_metadata_recovery_on_replacement_socket():
    async def scenario():
        app = wake.AppServer(wake.WakeConfig(THREAD))
        app.managed = app.connected = app.pausing = True
        app.generation = 2
        app.active_turn = "synthetic-turn"
        app.outcome_unknown = True
        app.recovery_state = "paused"
        app._turn_metadata = AsyncMock(return_value="inProgress")
        app.events.put_nowait(
            {"method": "turn/completed", "params": {"turn": {"id": "synthetic-turn", "status": "interrupted"}}}
        )
        with pytest.raises(wake.WakeError, match="codex_turn_failed"):
            await asyncio.wait_for(app._wait_turn(1), 1)
        app._turn_metadata.assert_not_awaited()
        assert app.recovery_state == "paused" and not app.outcome_unknown

    asyncio.run(scenario())
