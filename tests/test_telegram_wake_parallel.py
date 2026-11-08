"""Concurrent client execution against an isolated ledger and Codex socket."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from websockets.asyncio.server import unix_serve

from autostop_manager import telegram_wake as wake
from autostop_manager.telegram_inbox import TelegramInbox
from telegram_wake_fakes import THREAD, MockServer, until
from test_telegram_wake_durable import FakeJobs, REVISION
from test_telegram_wake_durable import bundle as bundle


def test_five_clients_run_together_and_same_peer_waits(tmp_path, bundle):
    async def scenario():
        server = MockServer(peer_threads=True, strict_peer_lifecycle=True, complete=False)
        path = str(tmp_path / "app.sock")
        with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
            inbox.duty_set(True)
            for peer in range(101, 106):
                inbox.record(peer, 1)
            jobs = FakeJobs(inbox, finish=True)
            async with unix_serve(server.handle, path, compression=None):
                app = wake.AppServer(
                    wake.WakeConfig(
                        THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION
                    )
                )
                dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), jobs)
                dispatcher.start()
                try:
                    await until(lambda: server.counter == 5 and len(dispatcher.executions) == 5)
                    await until(lambda: all(slot["state"] == "completed" for slot in inbox.execution_slots()))
                    slots = inbox.execution_slots()
                    assert len(slots) == 5
                    assert len({slot["peer_id"] for slot in slots}) == 5
                    assert len({slot["thread_id"] for slot in slots}) == 5
                    second_event = inbox.record(101, 2)
                    jobs.changed.set()
                    await asyncio.sleep(0.05)
                    assert server.counter == 5 and len(inbox.execution_slots()) == 5
                    assert not any(second_event in slot["event_ids"] for slot in inbox.execution_slots())

                    for turn_id, thread_id in list(server.turn_threads.items()):
                        server.turn_statuses[turn_id] = "completed"
                        socket = next(sock for sock, selected in server.socket_threads.items() if selected == thread_id)
                        await socket.send(
                            json.dumps(
                                {
                                    "method": "turn/completed",
                                    "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed"}},
                                }
                            )
                        )
                    try:
                        await until(lambda: server.counter == 6)
                    except TimeoutError:
                        raise AssertionError((dispatcher.status(), inbox.execution_slots(), jobs.calls[-15:])) from None
                    starts = [call["params"]["threadId"] for call in server.calls if call.get("method") == "turn/start"]
                    assert len(set(starts[:5])) == 5
                    first = next(slot for slot in slots if slot["peer_id"] == 101)
                    assert starts[-1] == first["thread_id"]
                finally:
                    # The sixth turn is intentionally still active; shutdown
                    # must prove its exact stop without affecting other peers.
                    await dispatcher.close()

    asyncio.run(scenario())


def test_one_peer_failure_does_not_stop_other_clients(tmp_path, bundle, monkeypatch):
    original = wake.DurableWakeDispatcher._select_peer

    async def select_peer(self, job):
        if job["peer_id"] == 101:
            raise wake.WakeError("codex_thread_busy")
        await original(self, job)

    monkeypatch.setattr(wake.DurableWakeDispatcher, "_select_peer", select_peer)

    async def scenario():
        server = MockServer(peer_threads=True, strict_peer_lifecycle=True, complete=True)
        path = str(tmp_path / "app.sock")
        with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
            inbox.duty_set(True)
            for peer in range(101, 106):
                inbox.record(peer, 1)
            jobs = FakeJobs(inbox, finish=True)
            async with unix_serve(server.handle, path, compression=None):
                app = wake.AppServer(
                    wake.WakeConfig(
                        THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION
                    )
                )
                dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), jobs)
                dispatcher.start()
                try:
                    await until(lambda: dispatcher.completed == 4 and dispatcher.failed == 1)
                    assert server.counter == 4
                    assert dispatcher.execution_error is None and dispatcher.enabled
                    assert inbox.status()["held_peers"] == 1
                    assert len(inbox.execution_slots()) == 0
                finally:
                    await dispatcher.close()

    asyncio.run(scenario())


def test_unknown_proof_holds_only_its_peer(tmp_path, bundle, monkeypatch):
    original = wake.DurableWakeDispatcher._proof

    async def proof(self, job, deadline, code=None):
        if job["peer_id"] == 101:
            raise wake.WakeError("wake_pause_outcome_unknown")
        await original(self, job, deadline, code)

    monkeypatch.setattr(wake.DurableWakeDispatcher, "_proof", proof)

    async def scenario():
        server = MockServer(peer_threads=True, strict_peer_lifecycle=True, complete=True)
        socket = str(tmp_path / "app.sock")
        with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
            inbox.duty_set(True)
            inbox.record(101, 1)
            inbox.record(102, 1)
            jobs = FakeJobs(inbox, finish=True)
            async with unix_serve(server.handle, socket, compression=None):
                app = wake.AppServer(
                    wake.WakeConfig(
                        THREAD, app_socket=socket, instruction_root=str(bundle), instruction_revision=REVISION
                    )
                )
                dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), jobs)
                dispatcher.start()
                try:
                    await until(lambda: dispatcher.completed == 1 and bool(dispatcher.unsettled))
                    held = inbox.execution_slot(peer_id=101)
                    assert held is not None and held["execution_proof"] is None
                    assert dispatcher.execution_error is None and dispatcher.enabled
                    inbox.record(101, 2)
                    inbox.record(103, 1)
                    jobs.changed.set()
                    await until(lambda: dispatcher.completed == 2)
                    assert server.counter == 3
                    assert inbox.execution_slot(peer_id=101)["job_id"] == held["job_id"]
                    assert len([row for row in inbox.execution_slots() if row["peer_id"] == 101]) == 1
                finally:
                    await dispatcher.close()

    asyncio.run(scenario())


def test_restart_reconciles_each_claim_without_replaying_it(tmp_path, bundle):
    async def scenario():
        path = tmp_path / "inbox.sqlite3"
        with TelegramInbox(path) as inbox:
            inbox.duty_set(True)
            inbox.record(101, 1)
            inbox.record(102, 1)
            old = [inbox.claim(instruction_root=str(bundle), instruction_revision=REVISION) for _ in range(2)]
            assert all(old)
        server = MockServer(peer_threads=True, strict_peer_lifecycle=True, complete=True)
        socket = str(tmp_path / "app.sock")
        with TelegramInbox(path) as inbox:
            inbox.record(103, 1)
            jobs = FakeJobs(inbox, finish=True)
            async with unix_serve(server.handle, socket, compression=None):
                app = wake.AppServer(
                    wake.WakeConfig(
                        THREAD, app_socket=socket, instruction_root=str(bundle), instruction_revision=REVISION
                    )
                )
                dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), jobs)
                dispatcher.start()
                try:
                    await until(lambda: dispatcher.completed == 1 and not inbox.execution_slots())
                    for job in old:
                        recovered = inbox.read_job(job["job_id"])
                        assert recovered["state"] == "needs_review"
                        assert recovered["execution_proof"] == "not_started"
                    assert server.counter == 1
                finally:
                    await dispatcher.close()

    asyncio.run(scenario())


def test_pause_proves_every_owned_turn_before_resume(tmp_path, bundle):
    class InterruptServer(MockServer):
        async def _complete_turn(self, socket, method, thread_id):
            if method == "turn/interrupt":
                turn = next(key for key, value in self.turn_threads.items() if value == thread_id)
                self.turn_statuses[turn] = "interrupted"
                await socket.send(
                    json.dumps(
                        {
                            "method": "turn/completed",
                            "params": {"threadId": thread_id, "turn": {"id": turn, "status": "interrupted"}},
                        }
                    )
                )

    async def scenario():
        server = InterruptServer(peer_threads=True, strict_peer_lifecycle=True, complete=False)
        socket = str(tmp_path / "app.sock")
        with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
            inbox.duty_set(True)
            for peer in (101, 102):
                inbox.record(peer, 1)
            jobs = FakeJobs(inbox)
            async with unix_serve(server.handle, socket, compression=None):
                app = wake.AppServer(
                    wake.WakeConfig(
                        THREAD, app_socket=socket, instruction_root=str(bundle), instruction_revision=REVISION
                    )
                )
                dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), jobs)
                dispatcher.start()
                try:
                    await until(
                        lambda: (
                            len(inbox.execution_slots()) == 2
                            and all(row["state"] == "running" for row in inbox.execution_slots())
                        )
                    )
                    slots = inbox.execution_slots()
                    await dispatcher.pause()
                    assert not inbox.execution_slots() and not dispatcher.unsettled
                    assert all(inbox.read_job(row["job_id"])["execution_proof"] == "interrupted" for row in slots)
                    assert all(inbox.read_job(row["job_id"])["state"] == "needs_review" for row in slots)
                    interrupts = [call["params"] for call in server.calls if call.get("method") == "turn/interrupt"]
                    assert {(row["thread_id"], row["turn_id"]) for row in slots} == {
                        (item["threadId"], item["turnId"]) for item in interrupts
                    }
                    await dispatcher.resume_execution()
                    assert dispatcher.enabled
                finally:
                    await dispatcher.close()

    asyncio.run(scenario())


def test_old_worker_protocol_fails_closed():
    with pytest.raises(wake.WakeError, match="wake_worker_protocol_mismatch"):
        wake._job_status_entity({"status": {"worker_protocol": 2, "context_mode": "serial_peer_v1"}})


def test_manual_slot_is_not_replayed_on_restart(monkeypatch):
    async def scenario():
        job = {
            "job_id": "job-1",
            "lease": "lease",
            "revision": 1,
            "event_ids": ["inbound-1"],
            "manual_recovery": 1,
            "execution_proof": "interrupted",
        }

        class Jobs:
            async def request(self, operation, **_kwargs):
                assert operation == "job_execution_slots"
                return {"jobs": [job]}

        dispatcher = wake.DurableWakeDispatcher(wake.AppServer(wake.WakeConfig(THREAD)), os.geteuid(), Jobs())
        monkeypatch.setattr(dispatcher, "_launch", lambda *_args, **_kwargs: pytest.fail("manual turn replayed"))
        await dispatcher._reconcile_active()
        await dispatcher._reconcile_active()
        assert not dispatcher.executions

    asyncio.run(scenario())


def test_deadline_extends_only_on_progress_from_exact_turn(monkeypatch):
    async def scenario():
        monkeypatch.setattr(wake, "PROGRESS_RENEW_INTERVAL_SECONDS", 0.01)
        app = wake.AppServer(wake.WakeConfig(THREAD))
        app.active_turn = "turn-1"
        old = datetime.now(UTC) + timedelta(seconds=1)
        job = {
            "job_id": "job-1",
            "lease": "lease",
            "revision": 1,
            "event_ids": ["inbound-1"],
            "deadline_at": old.isoformat(),
        }

        class Jobs:
            def __init__(self):
                self.calls = []

            async def request(self, operation, **kwargs):
                self.calls.append((operation, kwargs))
                return {"job": job | {"deadline_at": (old + timedelta(seconds=10)).isoformat()}}

        jobs = Jobs()
        dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), jobs)
        stop = asyncio.Event()
        dispatcher.execution_task = asyncio.create_task(stop.wait())
        dispatcher.request_deadline = asyncio.get_running_loop().time() + 1
        waiter = asyncio.create_task(dispatcher._wait_execution(job))
        await asyncio.sleep(0)
        app._notice_progress({"method": "item/started", "params": {"threadId": THREAD, "turnId": "other"}})
        app._notice_progress({"method": "item/started", "params": {"threadId": "other", "turnId": "turn-1"}})
        await asyncio.sleep(0.03)
        assert not jobs.calls
        app._notice_progress({"method": "item/started", "params": {"threadId": THREAD, "turnId": "turn-1"}})
        await until(lambda: bool(jobs.calls))
        assert jobs.calls == [("job_progress", {"job_id": "job-1", "lease": "lease"})]
        assert dispatcher.request_deadline > asyncio.get_running_loop().time() + 5
        stop.set()
        await waiter

    asyncio.run(scenario())


def test_deadline_still_stops_a_turn_without_progress(monkeypatch):
    async def scenario():
        monkeypatch.setattr(wake, "PROGRESS_RENEW_INTERVAL_SECONDS", 0.01)
        app = wake.AppServer(wake.WakeConfig(THREAD))
        dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), object())
        task = asyncio.create_task(asyncio.Event().wait())
        dispatcher.execution_task = task
        dispatcher.request_deadline = asyncio.get_running_loop().time() + 0.03
        with pytest.raises(TimeoutError):
            await dispatcher._wait_execution({"job_id": "job-1", "lease": "lease"})
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
