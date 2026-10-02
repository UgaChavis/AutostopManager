"""Bounded turn notifications and connection-loss isolation on real Unix sockets."""

from __future__ import annotations

import asyncio
import json

import pytest
from test_telegram_wake import THREAD, MockServer
from test_telegram_wake_reconnect import event, stop, until
from test_telegram_wake_reconnect import quick_recovery as quick_recovery
from websockets.asyncio.server import unix_serve

from autostop_manager import telegram_wake as wake


async def notify(socket, ident, *, method="turn/completed", status="completed", thread=THREAD):
    await socket.send(
        json.dumps({"method": method, "params": {"threadId": thread, "turn": {"id": ident, "status": status}}})
    )


def test_500_idle_manual_turns_leave_no_notifications_and_next_wake_runs(tmp_path):
    async def scenario():
        server = MockServer()
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
            dispatcher = wake.WakeDispatcher(app, 123)
            try:
                await app.connect()
                await app.resume()
                assert app.events.maxsize == 32
                for number in range(500):
                    await notify(server.sockets[-1], f"manual-{number}", method="turn/started", status="inProgress")
                    await notify(server.sockets[-1], f"manual-{number}")
                # The reply is behind every notification on this connection.
                await app.request("barrier", {})
                assert app.events.empty() and server.counter == server.metadata_calls == 0
                dispatcher.worker = asyncio.create_task(dispatcher.work())
                dispatcher.accept(event(1), 123)
                await asyncio.wait_for(dispatcher.queue.join(), 3)
                assert dispatcher.completed == server.counter == 1 and dispatcher.failed == 0
                assert app.last_turn_started and app.events.empty() and server.metadata_calls == 0
            finally:
                await stop(dispatcher)

    asyncio.run(scenario())


def test_known_active_turn_drops_500_wrong_ids_and_wrong_threads_before_waiter_runs(tmp_path):
    async def scenario():
        server = MockServer(complete=False)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
            try:
                await app.connect()
                generation = await app._start_turn("synthetic event")
                assert app.active_turn == "1" and app.events.qsize() == 1
                for number in range(500):
                    await notify(server.sockets[-1], f"unrelated-{number}")
                    await notify(server.sockets[-1], "1", thread="00000000-0000-4000-8000-000000000002")
                await app.request("barrier", {})
                assert not app.blocked and app.events.qsize() == 1
                await notify(server.sockets[-1], "1")
                await asyncio.wait_for(app._wait_turn(generation), 3)
                assert app.last_turn_started and app.active_turn is None and not app.outcome_unknown
                assert app.events.empty() and server.counter == 1 and server.metadata_calls == 0
            finally:
                await app.close()

    asyncio.run(scenario())


def test_older_connection_loss_signal_does_not_reconcile_current_turn(tmp_path, quick_recovery):
    async def scenario():
        server = MockServer(complete=False)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
            dispatcher = wake.WakeDispatcher(app, 123)
            dispatcher.start()
            try:
                await asyncio.wait_for(app.ready_event.wait(), 3)
                dispatcher.accept(event(1), 123)
                await until(lambda: app.last_turn_started)
                app.events.put_nowait({"method": "connection_lost", "generation": app.generation - 1})
                await notify(server.sockets[-1], "1")
                await asyncio.wait_for(dispatcher.queue.join(), 3)
                assert server.metadata_calls == 0 and server.counter == dispatcher.completed == 1
                assert dispatcher.failed == 0 and app.last_turn_started and app.events.empty()
                assert app.active_turn is None and not app.outcome_unknown and dispatcher.enabled
            finally:
                await stop(dispatcher)

    asyncio.run(scenario())


@pytest.mark.parametrize("own_started", [True, False])
def test_notification_burst_before_start_rpc_reply_preserves_start_observation(tmp_path, own_started):
    class StartBurstSocket:
        def __init__(self, socket):
            self.socket = socket

        def __aiter__(self):
            return self.socket.__aiter__()

        async def send(self, raw):
            message = json.loads(raw)
            turn = message.get("result", {}).get("turn")
            if turn is not None:
                for number in range(4):
                    ident = turn["id"] if own_started else f"unrelated-{number}"
                    await notify(self.socket, ident, method="turn/started", status="inProgress")
                await notify(self.socket, turn["id"])
            await self.socket.send(raw)

        async def close(self):
            await self.socket.close()

    async def scenario():
        server = MockServer(complete=False)
        server.emit_started = False

        async def handle(socket):
            await server.handle(StartBurstSocket(socket))

        path = str(tmp_path / "app.sock")
        async with unix_serve(handle, path, compression=None):
            app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
            try:
                await app.connect()
                if own_started:
                    await asyncio.wait_for(app.run_turn("synthetic event"), 3)
                else:
                    with pytest.raises(wake.WakeError, match=r"^codex_turn_start_not_observed$"):
                        await asyncio.wait_for(app.run_turn("synthetic event"), 3)
                assert app.last_turn_started is own_started
                assert app.active_turn is None and not app.outcome_unknown
                assert app.events.empty() and server.counter == 1 and server.metadata_calls == 0
            finally:
                await app.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("known_id", [True, False])
def test_own_turn_notification_overflow_releases_worker_and_preserves_pending_event(
    tmp_path, quick_recovery, monkeypatch, caplog, known_id
):
    async def scenario():
        server = MockServer(complete=False)
        if not known_id:
            server.release_start.clear()
            server.emit_started = False
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
            dispatcher = wake.WakeDispatcher(app, 123)
            waiting = asyncio.Event()
            release_waiter = asyncio.Event()
            queue_get = app.events.get

            async def delayed_get():
                waiting.set()
                await release_waiter.wait()
                return await queue_get()

            if known_id:
                monkeypatch.setattr(app.events, "get", delayed_get)
            dispatcher.start()
            try:
                await asyncio.wait_for(app.ready_event.wait(), 3)
                dispatcher.accept(event(1), 123)
                dispatcher.accept(event(2), 123)
                await asyncio.wait_for((waiting if known_id else server.started).wait(), 3)
                expected_turn = "1" if known_id else None
                assert app.active_turn == expected_turn and app.outcome_unknown
                for _ in range(app.events.maxsize + (not known_id)):
                    await notify(server.sockets[-1], "1", method="turn/started", status="inProgress")
                await until(lambda: app.blocked)
                release_waiter.set()
                await asyncio.wait_for(dispatcher.worker, 3)
                assert dispatcher.last_error == app.recovery_error == "codex_turn_notifications_full"
                assert app.active_turn == expected_turn and app.outcome_unknown
                assert dispatcher.failed == 1 and dispatcher.completed == 0 and dispatcher.queue.qsize() == 1
                assert not dispatcher.status()["enabled"] and dispatcher.status()["recovery_state"] == "blocked"
                assert server.counter == 1 and server.metadata_calls == 0
                assert THREAD not in json.dumps(dispatcher.status()) and THREAD not in caplog.text
            finally:
                release_waiter.set()
                server.release_start.set()
                await stop(dispatcher)

    asyncio.run(scenario())


def test_known_turn_completed_in_metadata_does_not_force_next_turn_metadata(tmp_path, quick_recovery):
    async def scenario():
        server = MockServer(complete=False)
        server.emit_started = False
        server.disconnect_after_start_response = True
        server.release_metadata.clear()
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
            dispatcher = wake.WakeDispatcher(app, 123)
            dispatcher.start()
            try:
                await asyncio.wait_for(app.ready_event.wait(), 3)
                dispatcher.accept(event(1), 123)
                await asyncio.wait_for(server.metadata_requested.wait(), 3)
                server.turn_statuses["1"] = "completed"
                server.release_metadata.set()
                await asyncio.wait_for(dispatcher.queue.join(), 3)
                metadata_calls = server.metadata_calls
                assert metadata_calls == 1 and dispatcher.completed == 1
                server.complete = server.emit_started = True
                dispatcher.accept(event(2), 123)
                await asyncio.wait_for(dispatcher.queue.join(), 3)
                assert server.metadata_calls == metadata_calls
                assert server.counter == dispatcher.completed == 2 and dispatcher.failed == 0
                assert app.last_turn_started and app.events.empty() and app.active_turn is None
            finally:
                server.release_metadata.set()
                await stop(dispatcher)

    asyncio.run(scenario())


def test_twelve_known_turn_recoveries_with_varied_terminal_order_never_replay(tmp_path, quick_recovery):
    async def scenario():
        server = MockServer(complete=False)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
            dispatcher = wake.WakeDispatcher(app, 123)
            dispatcher.start()
            try:
                await asyncio.wait_for(app.ready_event.wait(), 3)
                for cycle, terminal in enumerate(("metadata", "notification", "both") * 4):
                    server.complete = server.emit_started = False
                    server.disconnect_after_start_response = True
                    server.metadata_requested.clear()
                    server.release_metadata.clear()
                    dispatcher.accept(event(cycle * 2 + 1), 123)
                    await asyncio.wait_for(server.metadata_requested.wait(), 3)
                    ident = str(cycle * 2 + 1)
                    assert app.active_turn == ident and server.counter == cycle * 2 + 1
                    server.turn_statuses[ident] = "inProgress" if terminal == "notification" else "completed"
                    if terminal in {"notification", "both"}:
                        await notify(server.sockets[-1], ident)
                    server.release_metadata.set()
                    await asyncio.wait_for(dispatcher.queue.join(), 3)
                    assert app.active_turn is None and not app.outcome_unknown and dispatcher.enabled
                    metadata_calls = server.metadata_calls
                    server.complete = server.emit_started = True
                    dispatcher.accept(event(cycle * 2 + 2), 123)
                    await asyncio.wait_for(dispatcher.queue.join(), 3)
                    assert server.metadata_calls == metadata_calls
                    assert server.counter == dispatcher.completed == cycle * 2 + 2 and dispatcher.failed == 0
                    assert app.last_turn_started and app.events.empty()
                assert server.counter == 24 and server.metadata_calls >= 12
                assert len(server.sockets) == 13
            finally:
                server.release_metadata.set()
                await stop(dispatcher)

    asyncio.run(scenario())
