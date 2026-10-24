"""Idle notices trigger exact, bounded recovery reads over isolated sockets."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager

import pytest
from telegram_wake_fakes import THREAD, MockServer, connected_app, until
from test_telegram_wake_durable import REVISION, FakeJobs, stop_dispatcher
from test_telegram_wake_durable import bundle as bundle
from websockets.asyncio.server import unix_serve

from autostop_manager import telegram_wake as wake
from autostop_manager.telegram_inbox import TelegramInbox

TURN = "known-turn"


async def notice(socket, *, thread_id=THREAD, kind="idle"):
    status = {"type": kind}
    if kind == "active":
        status["activeFlags"] = []
    await socket.send(
        json.dumps({"method": "thread/status/changed", "params": {"threadId": thread_id, "status": status}})
    )


async def roundtrip(app):
    # A response on this socket follows previously sent status notifications.
    await app.request("thread/read", {"threadId": app.config.thread_id, "includeTurns": False})


@asynccontextmanager
async def known_turn(tmp_path):
    server = MockServer(complete=False)
    server.turn_statuses[TURN] = "inProgress"
    snapshots = asyncio.Queue()
    tasks = []
    async with connected_app(tmp_path, server) as app:
        await app.resume(allow_active=True)
        app.managed = True
        app.ready_event.set()
        app.recovery_state = "ready"
        app.active_turn = TURN
        app.active_event_id = "inbound-1"
        app.outcome_unknown = True
        original_metadata = app._turn_metadata

        async def observed_metadata(turn_id=None):
            status = await original_metadata(turn_id)
            snapshots.put_nowait(status)
            return status

        app._turn_metadata = observed_metadata

        def start(*, recovered=True):
            task = asyncio.create_task(app._wait_turn(app.generation - int(recovered), notification_only=True))
            tasks.append(task)
            return task

        try:
            yield app, server, snapshots, start
        finally:
            server.release_metadata.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def test_idle_inprogress_waits_for_a_fresh_transition_without_duplicate_reads(tmp_path):
    async def scenario():
        async with known_turn(tmp_path) as (app, server, snapshots, start):
            waiter = start()
            assert await asyncio.wait_for(snapshots.get(), 2) == "inProgress"
            await notice(server.sockets[-1])
            assert await asyncio.wait_for(snapshots.get(), 2) == "inProgress"
            for _ in range(100):
                await notice(server.sockets[-1])
            await roundtrip(app)
            assert server.metadata_calls == 2 and not waiter.done()
            assert app.active_turn == TURN and not app.blocked and app.events.empty()
            server.turn_statuses[TURN] = "completed"
            await notice(server.sockets[-1], kind="active")
            await notice(server.sockets[-1])
            await asyncio.wait_for(waiter, 2)
            assert server.metadata_calls == 3 and app.active_turn is None

    asyncio.run(scenario())


@pytest.mark.parametrize("timing", ["before_waiter", "during_snapshot"])
def test_early_idle_survives_an_inprogress_first_snapshot(tmp_path, timing):
    async def scenario():
        async with known_turn(tmp_path) as (app, server, snapshots, start):
            # The first RPC has an older snapshot. Later reads see completed.
            server.metadata_response = {"data": [{"id": TURN, "status": "inProgress"}], "nextCursor": None}
            original_metadata = app._turn_metadata

            async def release_old_snapshot(turn_id=None):
                status = await original_metadata(turn_id)
                server.metadata_response = None
                return status

            app._turn_metadata = release_old_snapshot
            if timing == "before_waiter":
                await notice(server.sockets[-1])
                await roundtrip(app)
                assert app.events.empty(), "healthy execution must not queue recovery work"
                server.turn_statuses[TURN] = "completed"
                waiter = start()
            else:
                server.release_metadata.clear()
                waiter = start()
                await asyncio.wait_for(server.metadata_requested.wait(), 2)
                sequence = app.thread_notification_sequence
                server.turn_statuses[TURN] = "completed"
                for _ in range(100):
                    await notice(server.sockets[-1])
                await until(lambda: app.thread_notification_sequence == sequence + 100)
                assert app.events.qsize() == 1 and not app.blocked
                server.release_metadata.set()
            await asyncio.wait_for(waiter, 2)
            assert [snapshots.get_nowait(), snapshots.get_nowait()] == ["inProgress", "completed"]
            assert server.metadata_calls == 2 and app.active_turn is None
            assert server.counter == 0

    asyncio.run(scenario())


def test_foreign_and_stale_reader_idle_do_not_trigger_metadata(tmp_path):
    class OldSocket:
        close_code = 1000

        async def __aiter__(self):
            yield json.dumps(
                {"method": "thread/status/changed", "params": {"threadId": THREAD, "status": {"type": "idle"}}}
            )

    async def scenario():
        async with known_turn(tmp_path) as (app, server, snapshots, start):
            waiter = start()
            await asyncio.wait_for(snapshots.get(), 2)
            await notice(server.sockets[-1], thread_id="foreign-thread")
            await roundtrip(app)
            await app._read(OldSocket(), app.generation - 1)
            await roundtrip(app)
            assert server.metadata_calls == 1 and not waiter.done()
            assert app.connected and not app.events.qsize()
            server.turn_statuses[TURN] = "completed"
            await notice(server.sockets[-1])
            await asyncio.wait_for(waiter, 2)
            assert server.metadata_calls == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("stale", ["generation", "turn"])
def test_stale_marker_cannot_clear_a_current_pending_marker(tmp_path, stale):
    async def scenario():
        async with known_turn(tmp_path) as (app, _server, _snapshots, _start):
            app.recovery_state = "reconciling"
            app._notice_thread_status({"threadId": THREAD, "status": {"type": "idle"}})
            current = app.events.get_nowait()
            pending = app._idle_reconcile_pending
            obsolete = current | {
                "generation": app.generation - 1 if stale == "generation" else app.generation,
                "turn_id": TURN if stale == "generation" else "old-turn",
            }
            app.events.put_nowait(obsolete)
            barrier = {"method": "turn/started", "params": {"turn": {"id": TURN}}}
            app.events.put_nowait(barrier)
            assert await app._next_turn_event(recovered=True, notification_only=True) == barrier
            assert app._idle_reconcile_pending == pending
            app.events.put_nowait(current)
            assert await app._next_turn_event(recovered=True, notification_only=True) == current
            assert app._idle_reconcile_pending is None

    asyncio.run(scenario())


@pytest.mark.parametrize("started", [True, False])
def test_healthy_idle_does_not_read_metadata_or_bypass_start_notification(tmp_path, started):
    async def scenario():
        async with known_turn(tmp_path) as (app, server, _snapshots, start):
            waiter = start(recovered=False)
            server.turn_statuses[TURN] = "completed"
            await notice(server.sockets[-1])
            await roundtrip(app)
            assert server.metadata_calls == 0 and not waiter.done()
            if started:
                await server.sockets[-1].send(
                    json.dumps(
                        {
                            "method": "turn/started",
                            "params": {"threadId": THREAD, "turn": {"id": TURN, "status": "inProgress"}},
                        }
                    )
                )
            await server.sockets[-1].send(
                json.dumps(
                    {
                        "method": "turn/completed",
                        "params": {"threadId": THREAD, "turn": {"id": TURN, "status": "completed"}},
                    }
                )
            )
            if started:
                await asyncio.wait_for(waiter, 2)
            else:
                with pytest.raises(wake.WakeError, match=r"^codex_turn_start_not_observed$"):
                    await asyncio.wait_for(waiter, 2)
            assert server.metadata_calls == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("terminal", ["completed", "failed", "interrupted"])
def test_durable_reconnect_idle_proves_exact_turn_and_advances_other_peer(tmp_path, bundle, monkeypatch, terminal):
    class CompleteLaterJobs(FakeJobs):
        async def request(self, operation, **arguments):
            if operation == "job_turn_bound":
                self.finish = terminal == "completed" or self.inbox.read_job(arguments["job_id"])["peer_id"] != 101
            return await super().request(operation, **arguments)

    async def scenario():
        server = MockServer(peer_threads=True, complete=False)
        server.disconnect_after_start_response = True
        server.release_start.clear()
        inbox = TelegramInbox(tmp_path / "db" / "synthetic.sqlite3")
        inbox.duty_set(True)
        first_event = inbox.record(101, 1)
        second_event = inbox.record(202, 1)
        jobs = CompleteLaterJobs(inbox)
        recovered = asyncio.Event()
        original_metadata = wake.AppServer._turn_metadata

        async def observed_metadata(app, turn_id=None):
            status = await original_metadata(app, turn_id)
            if status == "inProgress" and app.events.empty():
                recovered.set()
            return status

        monkeypatch.setattr(wake.AppServer, "_turn_metadata", observed_metadata)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            bootstrap = wake.AppServer(
                wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
            )
            dispatcher = wake.DurableWakeDispatcher(bootstrap, os.geteuid(), jobs)
            dispatcher.start()
            try:
                await asyncio.wait_for(server.started.wait(), 3)
                server.thread_statuses[server.turn_threads["1"]] = "active"
                server.release_start.set()
                await asyncio.wait_for(recovered.wait(), 3)
                app = dispatcher.app
                first = inbox.execution_slot()
                assert app.reconnect_attempts >= 1 and first["turn_id"] == "1"
                assert first["execution_proof"] is None and inbox.resolve(second_event).peer_id == 202
                assert server.counter == 1
                unknown = None
                if terminal != "completed":
                    unknown = inbox.reserve_effect(
                        job_id=first["job_id"],
                        lease=first["lease"],
                        expected_revision=first["revision"],
                        kind="result",
                        text_sha256="a" * 64,
                        peer_id=101,
                        reply_to_message_id=1,
                    )
                    inbox.mark_effect_intent(unknown)
                server.turn_statuses["1"] = terminal
                server.thread_statuses[app.config.thread_id] = "idle"
                server.complete = True
                await notice(server.sockets[-1], thread_id=app.config.thread_id)
                expected = 2 if terminal == "completed" else 1
                await until(lambda: dispatcher.completed == expected and inbox.execution_slot() is None)
                stored = inbox.read_job(first["job_id"])
                assert stored["execution_proof"] == terminal
                assert server.counter == 2
                starts = [call for call in server.calls if call.get("method") == "turn/start"]
                assert sum(call["params"]["threadId"] == first["thread_id"] for call in starts) == 1
                if terminal == "completed":
                    assert stored["state"] == "completed" and inbox.event_state(first_event) == "no_reply_needed"
                else:
                    assert stored["state"] == "needs_review" and stored["reason_code"] == "codex_turn_failed"
                    assert inbox.event_state(first_event) == "open"
                    assert inbox.read_effect(unknown)["attempted"] == 1
                    assert inbox.read_effect(unknown)["verified"] == 0
            finally:
                server.release_start.set()
                await stop_dispatcher(dispatcher)
                inbox.close()

    monkeypatch.setattr(wake, "RECONNECT_DELAYS", (0.001,))
    asyncio.run(scenario())
