from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock

import pytest
from test_telegram_wake import THREAD, MockServer
from websockets.asyncio.server import unix_serve
from websockets.frames import OP_PING

from autostop_manager import telegram_wake as wake


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.fixture
def quick_recovery(monkeypatch):
    monkeypatch.setattr(wake, "RECONNECT_DELAYS", (0.01,))
    monkeypatch.setattr(wake, "RECOVERY_POLL_INTERVAL", 0.01)


def event(number):
    return {"operation": "event", "event_id": f"inbound-{number}"}


async def stop(dispatcher):
    try:
        await dispatcher.pause()
    except wake.WakeError:
        # Unknown side effects must remain visible; cleanup never retries them.
        pass
    finally:
        await dispatcher.app.close()


@pytest.mark.parametrize("frame", [{"id": [], "result": {}}, {"method": []}, ["private-payload"]])
def test_malformed_rpc_frame_blocks_and_releases_readiness_waiter(frame):
    class InvalidSocket:
        close_code = None

        async def __aiter__(self):
            yield json.dumps(frame)

    async def scenario():
        app = wake.AppServer(wake.WakeConfig(THREAD))
        app.managed = True
        app.connected = True
        waiter = asyncio.create_task(app.ensure_ready())
        await app._read(InvalidSocket(), app.generation)
        with pytest.raises(wake.WakeError, match="codex_rpc_frame_invalid"):
            await asyncio.wait_for(waiter, 1)
        status = wake.WakeDispatcher(app, 123).status()
        assert status["recovery_state"] == "blocked" and not status["enabled"]
        assert not status["ready"] and not status["connected"]
        assert "private-payload" not in json.dumps(status)

    asyncio.run(scenario())


def test_offline_start_preserves_fifo_deduplicates_and_recovers_without_idle_model_calls(tmp_path, quick_recovery):
    async def scenario():
        path = str(tmp_path / "app.sock")
        app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
        dispatcher = wake.WakeDispatcher(app, 123)
        dispatcher.start()
        try:
            await until(lambda: app.reconnect_attempts >= 1)
            for number in range(1, 5):
                assert dispatcher.accept(event(number), 123)["ok"]
            assert dispatcher.accept(event(1), 123)["duplicate"]
            assert dispatcher.enabled and not dispatcher.status()["ready"]
            server = MockServer()
            async with unix_serve(server.handle, path, compression=None):
                await asyncio.wait_for(dispatcher.queue.join(), 3)
                assert dispatcher.completed == 4 and dispatcher.failed == 0
                inputs = [call["params"]["input"][0]["text"] for call in server.calls if call["method"] == "turn/start"]
                assert inputs == [wake.WAKE_INSTRUCTION.format(event_id=f"inbound-{number}") for number in range(1, 5)]
                count = len(server.calls)
                await asyncio.sleep(0.04)
                assert len(server.calls) == count
                assert dispatcher.status()["ready"]
                assert all(
                    call["params"].get("excludeTurns") is True
                    for call in server.calls
                    if call["method"] == "thread/resume"
                )
        finally:
            await stop(dispatcher)

    asyncio.run(scenario())


def test_idle_disconnect_reopens_replaced_socket_without_starting_model(tmp_path, quick_recovery):
    async def scenario():
        path = str(tmp_path / "app.sock")
        old = MockServer()
        app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
        dispatcher = wake.WakeDispatcher(app, 123)
        old_server = await unix_serve(old.handle, path, compression=None)
        dispatcher.start()
        try:
            await asyncio.wait_for(app.ready_event.wait(), 3)
            old_server.close()
            await old_server.wait_closed()
            await until(lambda: not app.connected)
            assert dispatcher.enabled and not dispatcher.status()["ready"]
            new = MockServer()
            async with unix_serve(new.handle, path, compression=None):
                await until(lambda: dispatcher.status()["ready"])
                assert [call["method"] for call in new.calls] == ["initialize", "initialized", "thread/resume"]
                assert old.counter == new.counter == 0
                assert all(call["method"] != "thread/turns/list" for call in new.calls)
                status = dispatcher.status()
                assert status["last_disconnect_at"] and isinstance(status["last_disconnect_code"], int)
                assert THREAD not in json.dumps(status)
                dispatcher.accept(event(1), 123)
                await asyncio.wait_for(dispatcher.queue.join(), 3)
                assert dispatcher.completed == new.counter == 1
        finally:
            old_server.close()
            await old_server.wait_closed()
            await stop(dispatcher)

    asyncio.run(scenario())


def test_known_turn_completed_during_disconnect_is_reconciled_without_replay(tmp_path, quick_recovery):
    async def scenario():
        server = MockServer(complete=False)
        server.disconnect_after_start_response = True
        server.emit_started = False
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
            dispatcher = wake.WakeDispatcher(app, 123)
            dispatcher.start()
            try:
                await asyncio.wait_for(app.ready_event.wait(), 3)
                dispatcher.accept(event(1), 123)
                await until(lambda: server.counter == 1 and not app.connected)
                server.turn_statuses["1"] = "completed"
                await asyncio.wait_for(dispatcher.queue.join(), 3)
                assert server.metadata_calls >= 1 and len(server.sockets) >= 2
                assert server.counter == dispatcher.completed == 1 and dispatcher.failed == 0
                assert dispatcher.enabled and not app.outcome_unknown and app.active_turn is None
                metadata = [call["params"] for call in server.calls if call["method"] == "thread/turns/list"]
                assert all(
                    params["threadId"] == THREAD
                    and params["itemsView"] == "notLoaded"
                    and params["sortDirection"] == "desc"
                    and params["limit"] == 20
                    for params in metadata
                )
                assert not any(call["method"] in {"thread/read", "turn/interrupt"} for call in server.calls)
            finally:
                await stop(dispatcher)

    asyncio.run(scenario())


def test_known_inprogress_turn_waits_for_completion_notification_then_processes_queue(tmp_path, quick_recovery):
    async def scenario():
        server = MockServer(complete=False)
        server.disconnect_after_start_response = True
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
            dispatcher = wake.WakeDispatcher(app, 123)
            dispatcher.start()
            try:
                await asyncio.wait_for(app.ready_event.wait(), 3)
                dispatcher.accept(event(1), 123)
                dispatcher.accept(event(2), 123)
                await until(lambda: server.metadata_calls >= 1)
                assert server.counter == 1 and dispatcher.completed == 0
                assert app.active_turn == "1" and app.outcome_unknown
                server.complete = True
                params = {"threadId": THREAD, "turn": {"id": "1", "status": "completed"}}
                await server.sockets[-1].send(json.dumps({"method": "turn/completed", "params": params}))
                await asyncio.wait_for(dispatcher.queue.join(), 3)
                assert server.counter == dispatcher.completed == 2 and dispatcher.failed == 0
            finally:
                await stop(dispatcher)

    asyncio.run(scenario())


def test_reconciliation_transport_failure_reconnects_without_replaying_turn(tmp_path, quick_recovery):
    async def scenario():
        server = MockServer(complete=False)
        server.disconnect_after_start_response = True
        server.disconnect_metadata_once = True
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
            dispatcher = wake.WakeDispatcher(app, 123)
            dispatcher.start()
            try:
                await asyncio.wait_for(app.ready_event.wait(), 3)
                dispatcher.accept(event(1), 123)
                await until(lambda: server.counter == 1 and not app.connected)
                server.turn_statuses["1"] = "completed"
                await asyncio.wait_for(dispatcher.queue.join(), 3)
                assert server.metadata_calls == 2 and len(server.sockets) >= 3
                assert server.counter == dispatcher.completed == 1 and dispatcher.failed == 0
            finally:
                await stop(dispatcher)

    asyncio.run(scenario())


def test_lost_start_response_blocks_without_guessing_turn_id_or_retry(tmp_path, quick_recovery):
    async def scenario():
        server = MockServer(disconnect=True)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
            dispatcher = wake.WakeDispatcher(app, 123)
            dispatcher.start()
            try:
                await asyncio.wait_for(app.ready_event.wait(), 3)
                dispatcher.accept(event(1), 123)
                dispatcher.accept(event(2), 123)
                await until(lambda: not dispatcher.enabled)
                assert app.outcome_unknown and app.active_turn is None
                assert dispatcher.failed == 1 and dispatcher.completed == 0 and dispatcher.queue.qsize() == 1
                await asyncio.sleep(0.04)
                assert server.counter == 1 and server.metadata_calls == 0
                assert dispatcher.status()["recovery_state"] == "blocked"
                with pytest.raises(wake.WakeError, match="wake_not_ready"):
                    dispatcher.accept(event(3), 123)
            finally:
                await stop(dispatcher)

    asyncio.run(scenario())


def test_exact_known_turn_can_be_found_on_fifth_metadata_page(tmp_path, quick_recovery):
    async def scenario():
        server = MockServer(complete=False)
        server.disconnect_after_start_response = True
        server.metadata_pages = [
            {"data": [{"id": f"unrelated-{page}", "status": "completed"}], "nextCursor": str(page + 1)}
            for page in range(4)
        ] + [{"data": [{"id": "1", "status": "completed"}], "nextCursor": None}]
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
            dispatcher = wake.WakeDispatcher(app, 123)
            dispatcher.start()
            try:
                await asyncio.wait_for(app.ready_event.wait(), 3)
                dispatcher.accept(event(1), 123)
                await asyncio.wait_for(dispatcher.queue.join(), 3)
                assert server.metadata_calls == 5 and server.counter == dispatcher.completed == 1
                assert dispatcher.failed == 0 and not app.outcome_unknown
            finally:
                await stop(dispatcher)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "metadata",
    [
        {"data": [], "nextCursor": None},
        {"data": [{"id": "1", "status": "failed"}], "nextCursor": None},
        {"data": [{"id": "1", "status": "interrupted"}], "nextCursor": None},
        {"data": [{"id": "1", "status": "unexpected"}], "nextCursor": None},
        {"data": [{"id": 1, "status": "completed"}], "nextCursor": None},
        {"data": "private invalid payload", "nextCursor": None},
        {"data": [], "nextCursor": 123},
        {"data": [], "nextCursor": "0"},
    ],
)
def test_unresolvable_known_turn_blocks_queue_without_replay(tmp_path, quick_recovery, metadata):
    async def scenario():
        server = MockServer(complete=False)
        server.disconnect_after_start_response = True
        server.metadata_response = metadata
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
            dispatcher = wake.WakeDispatcher(app, 123)
            dispatcher.start()
            try:
                await asyncio.wait_for(app.ready_event.wait(), 3)
                dispatcher.accept(event(1), 123)
                dispatcher.accept(event(2), 123)
                await until(lambda: not dispatcher.enabled)
                assert server.counter == 1 and dispatcher.completed == 0
                assert dispatcher.failed == 1 and dispatcher.queue.qsize() == 1
                assert dispatcher.status()["recovery_state"] == "blocked"
                assert server.metadata_calls <= 5
                assert "private" not in json.dumps(dispatcher.status())
            finally:
                await stop(dispatcher)

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["backoff", "initialize", "resume"])
def test_pause_during_recovery_cannot_be_undone_by_late_connection(tmp_path, quick_recovery, phase):
    async def scenario():
        server = MockServer()
        if phase == "initialize":
            server.release_initialize.clear()
        elif phase == "resume":
            server.release_resume.clear()
        path = str(tmp_path / "app.sock")
        app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
        dispatcher = wake.WakeDispatcher(app, 123)
        socket_server = None
        if phase != "backoff":
            socket_server = await unix_serve(server.handle, path, compression=None)
        dispatcher.start()
        try:
            if phase == "backoff":
                await until(lambda: app.reconnect_attempts >= 1)
            else:
                marker = server.initializing if phase == "initialize" else server.resuming
                await asyncio.wait_for(marker.wait(), 3)
            dispatcher.accept(event(1), 123)
            await asyncio.wait_for(dispatcher.pause(), 3)
            server.release_initialize.set()
            server.release_resume.set()
            await asyncio.sleep(0.04)
            assert not dispatcher.enabled and not dispatcher.status()["ready"]
            assert dispatcher.status()["recovery_state"] == "paused"
            assert dispatcher.queue.empty() and server.counter == 0
            with pytest.raises(wake.WakeError, match="wake_not_ready"):
                dispatcher.accept(event(2), 123)
        finally:
            await app.close()
            if socket_server:
                socket_server.close()
                await socket_server.wait_closed()

    asyncio.run(scenario())


def test_prestart_transport_loss_can_retry_target_resolution_without_duplicate_turn(tmp_path, quick_recovery):
    async def scenario():
        server = MockServer()
        server.disconnect_resume_on_call = 2
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
            dispatcher = wake.WakeDispatcher(app, 123)
            dispatcher.start()
            try:
                await asyncio.wait_for(app.ready_event.wait(), 3)
                dispatcher.accept(event(1), 123)
                await asyncio.wait_for(dispatcher.queue.join(), 3)
                assert len(server.sockets) == 2 and server.counter == dispatcher.completed == 1
                assert dispatcher.failed == 0 and dispatcher.enabled and not app.outcome_unknown
            finally:
                await stop(dispatcher)

    asyncio.run(scenario())


def test_metadata_and_notification_complete_same_turn_once_and_ignore_late_duplicate(tmp_path, quick_recovery):
    async def scenario():
        server = MockServer(complete=False)
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
                dispatcher.accept(event(2), 123)
                await asyncio.wait_for(server.metadata_requested.wait(), 3)
                assert server.counter == 1
                notification = {
                    "method": "turn/completed",
                    "params": {"threadId": THREAD, "turn": {"id": "1", "status": "completed"}},
                }
                server.turn_statuses["1"] = "completed"
                await server.sockets[-1].send(json.dumps(notification))
                server.complete = True
                server.release_metadata.set()
                await asyncio.wait_for(dispatcher.queue.join(), 3)
                await server.sockets[-1].send(json.dumps(notification))
                await asyncio.sleep(0.02)
                assert server.counter == dispatcher.completed == 2 and dispatcher.failed == 0
                dispatcher.accept(event(3), 123)
                await asyncio.wait_for(dispatcher.queue.join(), 3)
                assert server.counter == dispatcher.completed == 3 and dispatcher.failed == 0
            finally:
                server.release_metadata.set()
                await stop(dispatcher)

    asyncio.run(scenario())


def test_metadata_rpc_rejection_blocks_and_retains_exact_known_id(tmp_path, quick_recovery):
    async def scenario():
        server = MockServer(complete=False)
        server.disconnect_after_start_response = True
        server.reject_method = "thread/turns/list"
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
            dispatcher = wake.WakeDispatcher(app, 123)
            dispatcher.start()
            try:
                await asyncio.wait_for(app.ready_event.wait(), 3)
                dispatcher.accept(event(1), 123)
                dispatcher.accept(event(2), 123)
                await until(lambda: not dispatcher.enabled)
                assert app.active_turn == "1" and app.outcome_unknown
                assert dispatcher.failed == 1 and dispatcher.queue.qsize() == 1
                assert dispatcher.status()["rpc_error_code"] == -32600
                assert "private" not in json.dumps(dispatcher.status())
                assert server.counter == 1
                assert sum(call["method"] == "thread/turns/list" for call in server.calls) == 1
            finally:
                await stop(dispatcher)

    asyncio.run(scenario())


def test_foreign_owner_turn_is_never_interrupted_or_queued_behind_by_recovery(tmp_path, quick_recovery):
    async def scenario():
        server = MockServer()
        server.thread_status = "active"
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
            dispatcher = wake.WakeDispatcher(app, 123)
            dispatcher.start()
            try:
                await asyncio.wait_for(app.ready_event.wait(), 3)
                assert dispatcher.status()["ready"]
                dispatcher.accept(event(1), 123)
                dispatcher.accept(event(2), 123)
                await until(lambda: not dispatcher.enabled)
                assert dispatcher.last_error == "codex_thread_busy"
                assert not app.outcome_unknown and app.active_turn is None
                assert dispatcher.failed == 1 and dispatcher.queue.qsize() == 1
                assert not any(call["method"] in {"turn/start", "turn/interrupt"} for call in server.calls)
            finally:
                await stop(dispatcher)

    asyncio.run(scenario())


def test_old_reader_generation_cannot_disconnect_or_complete_current_connection(tmp_path, quick_recovery):
    class DelayedOldSocket:
        close_code = 1000

        def __init__(self, data):
            self.data = data

        async def __aiter__(self):
            yield json.dumps(self.data)

    async def scenario():
        server = MockServer()
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
            dispatcher = wake.WakeDispatcher(app, 123)
            dispatcher.start()
            try:
                await asyncio.wait_for(app.ready_event.wait(), 3)
                generation = app.generation
                stale = {
                    "method": "turn/completed",
                    "params": {"threadId": THREAD, "turn": {"id": "1", "status": "completed"}},
                }
                await app._read(DelayedOldSocket(stale), generation - 1)
                assert app.connected and app.generation == generation
                assert app.events.empty() and not app.disconnected.is_set()
                assert dispatcher.status()["ready"] and dispatcher.completed == 0
                dispatcher.accept(event(1), 123)
                await asyncio.wait_for(dispatcher.queue.join(), 3)
                assert dispatcher.completed == server.counter == 1 and dispatcher.failed == 0
            finally:
                await stop(dispatcher)

    asyncio.run(scenario())


def test_heartbeat_timeout_reconnects_without_model_or_telegram_calls(tmp_path, quick_recovery, monkeypatch):
    monkeypatch.setattr(wake, "HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(wake, "HEARTBEAT_TIMEOUT", 0.02)

    async def scenario():
        server = MockServer()
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
            dispatcher = wake.WakeDispatcher(app, 123)
            dispatcher.start()
            try:
                await asyncio.wait_for(app.ready_event.wait(), 3)
                assert app.websocket.ping_interval == app.websocket.ping_timeout / 2 == 0.01
                protocol = server.sockets[0].protocol
                recv_frame = protocol.recv_frame

                def suppress_pong(frame):
                    if frame.opcode != OP_PING:
                        recv_frame(frame)

                monkeypatch.setattr(protocol, "recv_frame", suppress_pong)
                await until(lambda: len(server.sockets) >= 2 and dispatcher.status()["ready"])
                assert server.counter == 0 and dispatcher.completed == dispatcher.failed == 0
                assert dispatcher.status()["last_disconnect_at"]
                assert all(call["method"] in {"initialize", "initialized", "thread/resume"} for call in server.calls)
            finally:
                await stop(dispatcher)

    asyncio.run(scenario())


def test_backoff_increases_to_thirty_seconds_and_remains_capped(monkeypatch):
    async def scenario():
        app = wake.AppServer(wake.WakeConfig(THREAD))
        app.connect = AsyncMock(side_effect=OSError("private connection details"))
        delays = []

        async def capture_delay(delay):
            delays.append(delay)
            if len(delays) == 8:
                app.pausing = True

        monkeypatch.setattr(asyncio, "sleep", capture_delay)
        await app.supervise()
        assert delays == [1, 2, 4, 8, 16, 30, 30, 30]
        assert app.reconnect_attempts == app.connect.await_count == 8
        assert app.recovery_error == "codex_transport_failed_or_unknown"

    asyncio.run(scenario())


@pytest.mark.skipif(os.geteuid() != 0, reason="local status is root-only")
@pytest.mark.parametrize("phase", ["initialize", "resume"])
def test_total_setup_timeout_keeps_local_control_available_and_recovers(tmp_path, quick_recovery, monkeypatch, phase):
    monkeypatch.setattr(wake, "CONNECT_TIMEOUT", 0.03)

    async def scenario():
        server = MockServer()
        gate = server.release_initialize if phase == "initialize" else server.release_resume
        marker = server.initializing if phase == "initialize" else server.resuming
        gate.clear()
        path = str(tmp_path / "app.sock")
        local_path = tmp_path / "wake.sock"
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
            dispatcher = wake.WakeDispatcher(app, 123)
            dispatcher.start()
            local = await asyncio.start_unix_server(dispatcher.serve, path=str(local_path))
            try:
                await asyncio.wait_for(marker.wait(), 3)
                status = await wake.local_request({"operation": "status"}, local_path)
                assert status["enabled"] and not status["ready"] and status["recovery_state"] == "starting"
                dispatcher.accept(event(1), 123)
                await until(lambda: app.reconnect_attempts >= 1)
                status = await wake.local_request({"operation": "status"}, local_path)
                assert status["enabled"] and not status["ready"]
                assert status["last_error"] == "codex_transport_failed_or_unknown"
                gate.set()
                await asyncio.wait_for(dispatcher.queue.join(), 3)
                assert server.counter == dispatcher.completed == 1 and dispatcher.failed == 0
                assert dispatcher.status()["ready"]
            finally:
                gate.set()
                await stop(dispatcher)
                local.close()
                await local.wait_closed()

    asyncio.run(scenario())


def test_websocket_handshake_timeout_retries_until_socket_is_replaced(tmp_path, quick_recovery, monkeypatch):
    monkeypatch.setattr(wake, "CONNECT_TIMEOUT", 0.02)

    async def scenario():
        path = str(tmp_path / "app.sock")
        attempting = asyncio.Event()

        async def stalled_handshake(reader, writer):
            attempting.set()
            try:
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()

        stalled = await asyncio.start_unix_server(stalled_handshake, path=path)
        app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
        dispatcher = wake.WakeDispatcher(app, 123)
        dispatcher.start()
        try:
            await asyncio.wait_for(attempting.wait(), 3)
            dispatcher.accept(event(1), 123)
            await until(lambda: app.reconnect_attempts >= 1)
            assert dispatcher.enabled and not dispatcher.status()["ready"]
            stalled.close()
            await stalled.wait_closed()
            server = MockServer()
            async with unix_serve(server.handle, path, compression=None):
                await asyncio.wait_for(dispatcher.queue.join(), 3)
                assert server.counter == dispatcher.completed == 1 and dispatcher.failed == 0
        finally:
            stalled.close()
            await stalled.wait_closed()
            await stop(dispatcher)

    asyncio.run(scenario())


def test_readiness_notification_followed_by_disconnect_waits_for_next_ready_connection():
    async def scenario():
        app = wake.AppServer(wake.WakeConfig(THREAD))
        app.managed = True
        app.connected = False
        app.ready_event.set()
        waiting = asyncio.create_task(app.ensure_ready())
        try:
            await asyncio.sleep(0)
            assert not waiting.done() and not app.ready_event.is_set()
            app.connected = True
            app.ready_event.set()
            await asyncio.wait_for(waiting, 1)
        finally:
            waiting.cancel()
            await app.close()

    asyncio.run(scenario())


def test_pause_during_reconciliation_stops_queue_and_cannot_restore_readiness(tmp_path, quick_recovery):
    async def scenario():
        server = MockServer(complete=False)
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
                dispatcher.accept(event(2), 123)
                await asyncio.wait_for(server.metadata_requested.wait(), 3)
                assert app.recovery_state == "reconciling" and app.active_turn == "1"
                pause = asyncio.create_task(dispatcher.pause())
                await asyncio.sleep(0)
                assert not dispatcher.enabled and app.pausing
                server.release_metadata.set()
                await asyncio.wait_for(pause, 3)
                await asyncio.sleep(0.02)
                assert dispatcher.queue.empty() and not dispatcher.active
                assert dispatcher.supervisor.done() and dispatcher.worker.done()
                assert dispatcher.status()["recovery_state"] == "paused" and not dispatcher.status()["ready"]
                assert server.counter == 1
                assert sum(call["method"] == "turn/interrupt" for call in server.calls) == 1
            finally:
                server.release_metadata.set()
                await app.close()

    asyncio.run(scenario())
