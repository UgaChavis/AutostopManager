"""Bounded executor recovery over isolated local sockets and durable ledgers."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.server import unix_serve

from autostop_manager import telegram_wake as wake
from autostop_manager.telegram_inbox import TelegramInbox
from test_telegram_wake_durable import FakeJobs, REVISION, bind_test_peer, new_inbox, stop_dispatcher
from test_telegram_wake_durable import bundle as bundle
from telegram_wake_fakes import THREAD, MockServer, until
from telegram_wake_fakes import quick_recovery as quick_recovery


@pytest.fixture(autouse=True)
def private_app_server_only(tmp_path, monkeypatch):
    connect = wake.AppServer.connect

    async def guarded_connect(app):
        if Path(app.config.app_socket).parent.resolve() != tmp_path.resolve():
            raise AssertionError("lifecycle tests must use their private App Server socket")
        await connect(app)

    monkeypatch.setattr(wake.AppServer, "connect", guarded_connect)


async def wait_ready(dispatcher):
    try:
        await until(lambda: dispatcher.status()["ready"])
    except TimeoutError as exc:
        raise AssertionError(
            (
                dispatcher.status(),
                dispatcher.bootstrap_app.ready_event.is_set(),
                dispatcher.last_health,
                [(f.f_code.co_name, f.f_lineno) for f in dispatcher.worker.get_stack()] if dispatcher.worker else None,
                dispatcher.jobs.calls[-12:] if hasattr(dispatcher.jobs, "calls") else None,
            )
        ) from exc


@asynccontextmanager
async def response_server(path, payload):
    async def handle(reader, writer):
        try:
            await reader.readline()
            writer.write(payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_unix_server(handle, path=str(path)):
        yield wake.BridgeJobs(os.geteuid(), path)


@pytest.mark.skipif(os.geteuid() != 0, reason="production bridge IPC is root-only")
@pytest.mark.parametrize(
    ("payload", "error_type", "code"),
    [
        (b"", wake.TransportLost, "wake_bridge_disconnected"),
        (b'{"ok":true}', wake.TransportLost, "wake_bridge_disconnected"),
        (b"{private broken json}\n", wake.WakeError, "wake_job_response_invalid"),
        (b"\xff\n", wake.WakeError, "wake_job_response_invalid"),
        (b"[]\n", wake.WakeError, "wake_job_response_invalid"),
        (b"x" * (wake.MAX_JOB_RESPONSE_BYTES + 1) + b"\n", wake.WakeError, "wake_job_response_too_large"),
        (b'{"ok":false,"error":"account_not_authorized"}\n', wake.WakeError, "account_not_authorized"),
        (b'{"ok":false,"error":"provider_quota_exceeded"}\n', wake.WakeError, "provider_quota_exceeded"),
    ],
    ids=["eof", "partial", "invalid_json", "invalid_utf8", "invalid_shape", "too_large", "auth", "quota"],
)
def test_bridge_request_normalizes_transport_and_protocol_failures(tmp_path, payload, error_type, code):
    async def scenario():
        async with response_server(tmp_path / "bridge.sock", payload) as jobs:
            with pytest.raises(error_type, match=f"^{code}$"):
                await jobs.request("job_status")

    asyncio.run(scenario())


@pytest.mark.parametrize("cold_start", [False, True])
def test_rpc_eof_reconnects_subscription_and_keeps_worker_alive(tmp_path, bundle, quick_recovery, cold_start):
    async def scenario():
        subscribers = []
        fail_status = cold_start
        dropped = asyncio.Event()
        snapshots = 0
        calls = []

        async def bridge(reader, writer):
            nonlocal fail_status, snapshots
            try:
                request = json.loads(await reader.readline())
                operation = request["operation"]
                calls.append(operation)
                if operation == "job_subscribe":
                    snapshots += 1
                    subscribers.append(writer)
                    writer.write(json.dumps({"ok": True, "generation": snapshots, "heartbeat": False}).encode() + b"\n")
                    await writer.drain()
                    await reader.read()
                    return
                if operation == "job_status":
                    if fail_status:
                        fail_status = False
                        dropped.set()
                        return
                    response = {
                        "ok": True,
                        "status": {
                            "enabled": True,
                            "execution_paused": False,
                            "execution_revision": 1,
                            "active_jobs": 0,
                            "pending": 0,
                            "worker_protocol": 2,
                            "context_mode": "serial_peer_v1",
                            "global_slot": None,
                            "compatibility_hold": False,
                        },
                    }
                elif operation == "job_active":
                    response = {"ok": True, "jobs": [], "next_job_id": None}
                else:
                    response = {"ok": True, "job": None}
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        path = tmp_path / "bridge.sock"
        async with await asyncio.start_unix_server(bridge, path=str(path)):
            app = wake.AppServer(wake.WakeConfig(THREAD, instruction_root=str(bundle), instruction_revision=REVISION))
            app.connected = True
            app.ready_event.set()
            app.resume = AsyncMock(return_value="idle")
            jobs = wake.BridgeJobs(os.geteuid(), path)
            dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), jobs)
            jobs.start()
            dispatcher._start_worker()
            try:
                if not cold_start:
                    await wait_ready(dispatcher)
                    fail_status = True
                    subscribers[-1].write(b'{"ok":true,"generation":20,"heartbeat":false}\n')
                    await subscribers[-1].drain()
                await asyncio.wait_for(dropped.wait(), 1)
                await until(lambda: snapshots >= 2 and dispatcher.status()["ready"])
                assert not dispatcher.worker.done()
                assert dispatcher.execution_error is None
                assert calls.count("job_subscribe") == 2
                await asyncio.sleep(0.02)  # let the final ready health publication finish
                before = len(calls)
                await asyncio.sleep(0.02)
                assert len(calls) == before
            finally:
                await stop_dispatcher(dispatcher)

    asyncio.run(scenario())


@pytest.mark.parametrize("code", ["wake_job_response_invalid", "account_not_authorized", "provider_quota_exceeded"])
def test_permanent_bridge_failure_holds_until_explicit_resume(tmp_path, bundle, code):
    async def scenario():
        inbox, _event = new_inbox(tmp_path)
        jobs = FakeJobs(inbox)
        original = jobs.request
        attempts = 0

        async def broken_status(operation, **arguments):
            nonlocal attempts
            if operation == "job_status":
                attempts += 1
                raise wake.WakeError(code)
            return await original(operation, **arguments)

        jobs.request = broken_status
        app = wake.AppServer(wake.WakeConfig(THREAD, instruction_root=str(bundle), instruction_revision=REVISION))
        app.connected = True
        app.ready_event.set()
        dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
        dispatcher._start_worker()
        jobs.changed.set()
        try:
            await until(lambda: dispatcher.execution_error == code)
            for _hint in range(3):
                jobs.changed.set()
                await asyncio.sleep(0.005)
            assert attempts == 1 and not dispatcher.status()["ready"]
            assert not dispatcher.worker.done() and inbox.active_jobs() == []
            jobs.request = original
            inbox.duty_set(False)
            await dispatcher.resume_execution()
            await until(lambda: dispatcher.execution_state == "paused")
            assert dispatcher.execution_error is None and not dispatcher.worker.done()
        finally:
            await stop_dispatcher(dispatcher)
            inbox.close()

    asyncio.run(scenario())


def test_unexpected_worker_exit_is_observed_and_never_reports_ready(tmp_path, bundle):
    async def scenario():
        inbox, _event = new_inbox(tmp_path)
        jobs = FakeJobs(inbox)
        app = wake.AppServer(wake.WakeConfig(THREAD, instruction_root=str(bundle), instruction_revision=REVISION))
        app.connected = True
        app.ready_event.set()
        dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
        dispatcher.execution_state = "ready"
        original_work = dispatcher.work

        async def fail():
            raise RuntimeError("private details must never become diagnostics")

        dispatcher.work = fail
        dispatcher._start_worker()
        try:
            await until(lambda: dispatcher.execution_error == "wake_worker_failed")
            assert not dispatcher.status()["ready"]
            assert dispatcher.last_error == "wake_worker_failed"
            assert jobs.health[-1]["state"] == "held"
            assert inbox.active_jobs() == []
            old_worker = dispatcher.worker
            dispatcher.work = original_work
            inbox.duty_set(False)
            await dispatcher.resume_execution()
            await until(lambda: dispatcher.execution_state == "paused")
            assert dispatcher.worker is not old_worker and not dispatcher.worker.done()
            assert dispatcher.execution_error is None
        finally:
            await stop_dispatcher(dispatcher)
            inbox.close()

    asyncio.run(scenario())


def test_pause_interrupts_only_confirmed_owned_turn_and_reads_terminal_metadata(tmp_path, bundle):
    async def scenario():
        inbox, event_id = new_inbox(tmp_path)
        jobs = FakeJobs(inbox)
        server = MockServer(peer_threads=True, complete=False)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(
                wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
            )
            dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
            dispatcher.start()
            try:
                await until(lambda: bool(inbox.active_jobs()) and inbox.active_jobs()[0]["state"] == "running")
                owned_app = dispatcher.app
                owned = owned_app.active_turn
                thread = owned_app.config.thread_id
                await dispatcher.pause()
                interrupts = [call for call in server.calls if call.get("method") == "turn/interrupt"]
                assert len(interrupts) == 1
                assert interrupts[0]["params"] == {"threadId": thread, "turnId": owned}
                assert server.turn_statuses[owned] == "interrupted"
                assert server.metadata_calls >= 2
                assert owned_app.active_turn is None and not owned_app.outcome_unknown
                assert not dispatcher.enabled and not dispatcher.active
                assert dispatcher.execution_state == "paused"
                assert inbox.active_jobs()[0]["state"] == "needs_review"
                assert inbox.resolve(event_id) is not None
                await dispatcher.resume_execution()
                await wait_ready(dispatcher)
                assert server.counter == 1  # resume does not replay the held job
            finally:
                await stop_dispatcher(dispatcher)
                inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("binding", ["manual", "unbound", "other_thread", "other_turn"])
def test_pause_does_not_interrupt_unconfirmed_turn(tmp_path, bundle, binding):
    async def scenario():
        inbox, _event = new_inbox(tmp_path)
        job = inbox.claim(instruction_root=str(bundle), instruction_revision=REVISION)
        bind_test_peer(inbox, job, thread_id="other" if binding == "other_thread" else THREAD)
        job = inbox.start_intent(
            job_id=job["job_id"],
            lease=job["lease"],
            expected_revision=job["revision"],
            thread_id="other" if binding == "other_thread" else THREAD,
        )
        if binding != "unbound":
            job = inbox.bind_turn(
                job_id=job["job_id"],
                lease=job["lease"],
                expected_revision=job["revision"],
                turn_id="other" if binding in {"manual", "other_turn"} else "manual",
            )
        if binding == "manual":
            job = inbox.needs_review(
                job_id=job["job_id"],
                lease=job["lease"],
                expected_revision=job["revision"],
                reason_code="codex_execution_stopped",
            )
        jobs = FakeJobs(inbox)
        app = wake.AppServer(wake.WakeConfig(THREAD, instruction_root=str(bundle), instruction_revision=REVISION))
        app.active_turn = "manual"
        app.outcome_unknown = True
        app.interrupt = AsyncMock()
        app._turn_metadata = AsyncMock()
        dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
        dispatcher.current_job = None if binding == "manual" else job
        try:
            with pytest.raises(wake.WakeError, match=r"^wake_pause_outcome_unknown$"):
                await dispatcher.pause()
            app.interrupt.assert_not_awaited()
            app._turn_metadata.assert_not_awaited()
            assert app.active_turn == "manual" and app.outcome_unknown
            assert not dispatcher.enabled and inbox.active_jobs()
            assert dispatcher.execution_state == "held"
            assert inbox.active_jobs()[0]["state"] == "needs_review"
        finally:
            await stop_dispatcher(dispatcher)
            inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(("replace_with_manual_turn", "padded_page"), [(False, False), (False, True), (True, False)])
def test_pause_recovers_committed_binding_after_lost_reply(tmp_path, bundle, replace_with_manual_turn, padded_page):
    class LoseBindingReply(FakeJobs):
        async def request(self, operation, **arguments):
            if operation == "job_active" and padded_page and arguments.get("after_job_id") is None:
                self.calls.append((operation, arguments))
                sample = self.inbox.active_jobs()[0]
                page = [sample | {"job_id": f"job-{index:032x}", "thread_id": "other"} for index in range(1, 51)]
                return {"ok": True, "jobs": page, "next_job_id": page[-1]["job_id"]}
            if operation == "job_active" and padded_page:
                arguments = arguments | {"after_job_id": None}
            response = await super().request(operation, **arguments)
            if operation == "job_turn_bound":
                self.bound_app = self.dispatcher.app
                if replace_with_manual_turn:
                    self.bound_app.active_turn = "manual"
                raise OSError("binding reply lost after commit")
            return response

    async def scenario():
        inbox, _event = new_inbox(tmp_path)
        jobs = LoseBindingReply(inbox)
        server = MockServer(peer_threads=True, complete=False)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(
                wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
            )
            await app.connect()
            await app.resume()
            app.ready_event.set()
            dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
            jobs.dispatcher = dispatcher
            job = inbox.claim(instruction_root=str(bundle), instruction_revision=REVISION)
            execution = asyncio.create_task(dispatcher._execute(job))
            try:
                await until(lambda: hasattr(jobs, "bound_app"))
                await asyncio.sleep(0.01)
                committed = inbox.read_job(job["job_id"])
                owned_app = jobs.bound_app
                owned = committed["turn_id"]
                thread = committed["thread_id"]
                assert thread != THREAD and owned is not None
                assert committed["state"] == "running" and not execution.done()
                if replace_with_manual_turn:
                    with pytest.raises(wake.WakeError, match=r"^wake_pause_outcome_unknown$"):
                        await dispatcher.pause()
                    assert not any(call.get("method") == "turn/interrupt" for call in server.calls)
                    assert server.turn_statuses[owned] == "inProgress"
                    assert dispatcher.execution_state == "held" and owned_app.outcome_unknown
                    assert inbox.execution_slot() is not None
                else:
                    await dispatcher.pause()
                    interrupts = [call for call in server.calls if call.get("method") == "turn/interrupt"]
                    assert [call["params"] for call in interrupts] == [{"threadId": thread, "turnId": owned}]
                    assert server.turn_statuses[owned] == "interrupted"
                    assert owned_app.active_turn is None and not owned_app.outcome_unknown
                    assert dispatcher.execution_state == "paused"
                    assert inbox.execution_slot() is None
                assert server.counter == 1 and inbox.status()["execution_paused"] is True
                assert inbox.read_job(job["job_id"])["state"] == "needs_review"
                assert any(operation in {"job_active", "job_execution_slot"} for operation, _args in jobs.calls)
            finally:
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                await stop_dispatcher(dispatcher)
                inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("thread_status", ["systemError", "notLoaded"])
def test_empty_queue_thread_system_error_holds_once_and_not_loaded_is_valid(tmp_path, bundle, thread_status):
    class RecordingHealthJobs(FakeJobs):
        async def request(self, operation, **arguments):
            if operation == "job_health":
                self.inbox.set_health(**arguments)
            return await super().request(operation, **arguments)

    async def scenario():
        inbox = TelegramInbox(tmp_path / "db" / "inbound.sqlite3")
        inbox.duty_set(True)
        jobs = RecordingHealthJobs(inbox)
        server = MockServer(peer_threads=True)
        server.thread_status = thread_status
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(
                wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
            )
            dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
            dispatcher.start()
            try:
                if thread_status == "systemError":
                    await until(lambda: app.blocked and bool(inbox.pending_notifications()))
                    assert not dispatcher.status()["ready"] and dispatcher.execution_state == "held"
                    assert dispatcher.last_error == "codex_thread_system_error"
                    assert jobs.health == [
                        {"component": "codex", "state": "held", "error_code": "codex_thread_system_error"}
                    ]
                    app.block("codex_thread_system_error")
                    jobs.changed.set()
                    await asyncio.sleep(0.02)
                    assert len(inbox.pending_notifications()) == 1 and len(jobs.health) == 1
                    assert server.resume_calls == 1
                    await dispatcher.pause()
                    assert jobs.health[-1] == {
                        "component": "codex",
                        "state": "paused",
                        "error_code": "codex_thread_system_error",
                    }
                    assert len(inbox.pending_notifications()) == 1
                    assert inbox.pending_notifications()[0]["recovered_at"] is None
                else:
                    await until(lambda: dispatcher.status()["ready"] and bool(jobs.health))
                    assert not app.blocked and inbox.pending_notifications() == []
                assert inbox.events() == [] and server.counter == 0
            finally:
                await stop_dispatcher(dispatcher)
                inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("held_job", [False, True])
def test_pause_keeps_unresolved_incident_until_verified_resolution_and_resume(tmp_path, bundle, held_job):
    class RecordingHealthJobs(FakeJobs):
        async def request(self, operation, **arguments):
            if operation == "job_health":
                self.inbox.set_health(**arguments)
            return await super().request(operation, **arguments)

    async def scenario():
        inbox = TelegramInbox(tmp_path / "db" / "inbound.sqlite3")
        inbox.duty_set(True)
        job = None
        if held_job:
            inbox.record(123, 1)
            job = inbox.claim(instruction_root=str(bundle), instruction_revision=REVISION)
            bind_test_peer(inbox, job)
            job = inbox.start_intent(
                job_id=job["job_id"], lease=job["lease"], expected_revision=job["revision"], thread_id=THREAD
            )
            job = inbox.bind_turn(
                job_id=job["job_id"], lease=job["lease"], expected_revision=job["revision"], turn_id="terminal"
            )
            job = inbox.needs_review(
                job_id=job["job_id"],
                lease=job["lease"],
                expected_revision=job["revision"],
                reason_code="codex_job_requires_review",
            )
        jobs = RecordingHealthJobs(inbox)
        app = wake.AppServer(wake.WakeConfig(THREAD, instruction_root=str(bundle), instruction_revision=REVISION))
        app.connected = True
        app.ready_event.set()
        app.resume = AsyncMock(return_value="idle")
        app._turn_metadata = AsyncMock(return_value="completed")
        dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
        try:
            if held_job:
                dispatcher.last_error = "codex_job_requires_review"
                await dispatcher._health("held", dispatcher.last_error)
            await dispatcher.pause()
            if held_job:
                assert jobs.health[-1]["error_code"] == "codex_job_requires_review"
                assert len(inbox.pending_notifications()) == 1
                assert inbox.pending_notifications()[0]["recovered_at"] is None
                job = inbox.read_job(job["job_id"])
                inbox.result(
                    job_id=job["job_id"],
                    lease=job["lease"],
                    expected_revision=job["revision"],
                    outcome="no_reply_needed",
                    event_ids=job["event_ids"],
                    receipt_keys=[],
                    reason_code="already_answered",
                )
            else:
                assert jobs.health[-1] == {"component": "codex", "state": "paused", "error_code": None}
                assert inbox.pending_notifications() == []
            await dispatcher.resume_execution()
            await wait_ready(dispatcher)
            assert jobs.health[-1]["state"] == "ready"
            assert dispatcher.last_error is None and jobs.health[-1]["error_code"] is None
            if held_job:
                assert inbox.pending_notifications()[0]["recovered_at"] is not None
            else:
                assert inbox.pending_notifications() == []
        finally:
            await stop_dispatcher(dispatcher)
            inbox.close()

    asyncio.run(scenario())


def test_unchanged_unbound_review_job_does_not_repeat_incident_notifications(tmp_path, bundle):
    class RecordingHealthJobs(FakeJobs):
        async def request(self, operation, **arguments):
            if operation == "job_health":
                self.inbox.set_health(**arguments)
            return await super().request(operation, **arguments)

    async def scenario():
        inbox = TelegramInbox(tmp_path / "db" / "inbound.sqlite3")
        inbox.duty_set(True)
        inbox.record(123, 1)
        job = inbox.claim(instruction_root=str(bundle), instruction_revision=REVISION)
        bind_test_peer(inbox, job)
        job = inbox.start_intent(
            job_id=job["job_id"], lease=job["lease"], expected_revision=job["revision"], thread_id=THREAD
        )
        inbox.needs_review(
            job_id=job["job_id"],
            lease=job["lease"],
            expected_revision=job["revision"],
            reason_code="codex_turn_outcome_unknown",
        )
        jobs = RecordingHealthJobs(inbox)
        app = wake.AppServer(wake.WakeConfig(THREAD, instruction_root=str(bundle), instruction_revision=REVISION))
        app.connected = True
        app.ready_event.set()
        dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
        dispatcher._start_worker()
        try:
            jobs.changed.set()
            await until(lambda: dispatcher.last_health == ("held", "codex_turn_outcome_unknown"))
            incident = inbox.pending_notifications()[0]
            inbox.notification_verified(incident["notification_id"], "receipt-error")
            for _ in range(5):
                jobs.changed.set()
                await asyncio.sleep(0.02)
            assert jobs.health == [{"component": "codex", "state": "held", "error_code": "codex_turn_outcome_unknown"}]
            assert inbox.pending_notifications() == []
            assert inbox.read_incident(incident["incident_id"])["recovered_at"] is None
            assert inbox.active_jobs()[0]["state"] == "needs_review"
        finally:
            await stop_dispatcher(dispatcher)
            inbox.close()

    asyncio.run(scenario())


def test_warm_thread_error_holds_once_ignores_foreign_and_recovers_only_on_resume(tmp_path, bundle):
    class RecordingHealthJobs(FakeJobs):
        async def request(self, operation, **arguments):
            if operation == "job_health":
                self.inbox.set_health(**arguments)
            return await super().request(operation, **arguments)

    async def scenario():
        inbox = TelegramInbox(tmp_path / "db" / "inbound.sqlite3")
        inbox.duty_set(True)
        jobs = RecordingHealthJobs(inbox)
        server = MockServer(peer_threads=True)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(
                wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
            )
            dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
            dispatcher.start()

            async def notify(status, *, thread_id=THREAD):
                await server.sockets[-1].send(
                    json.dumps({"method": "thread/status/changed", "params": {"threadId": thread_id, "status": status}})
                )

            try:
                await until(lambda: dispatcher.status()["ready"] and bool(jobs.health))
                sequence = app.thread_notification_sequence
                await notify({"type": "systemError"}, thread_id="foreign")
                await notify({"type": "active", "activeFlags": "private invalid body"}, thread_id="foreign")
                valid = [
                    {"type": "notLoaded"},
                    {"type": "active", "activeFlags": []},
                    {"type": "active", "activeFlags": ["waitingOnUserInput", "waitingOnApproval"]},
                    {"type": "idle"},
                ]
                for status in valid:
                    await notify(status)
                await until(lambda: app.thread_notification_sequence >= sequence + len(valid))
                assert not app.blocked and dispatcher.status()["ready"]
                assert inbox.pending_notifications() == [] and len(jobs.health) == 1
                server.thread_status = "systemError"
                await notify({"type": "systemError"})
                await until(lambda: app.blocked and len(jobs.health) == 2)
                assert not dispatcher.status()["ready"] and dispatcher.execution_state == "held"
                assert jobs.health[-1] == {
                    "component": "codex",
                    "state": "held",
                    "error_code": "codex_thread_system_error",
                }
                sequence = app.thread_notification_sequence
                await notify({"type": "systemError"})
                await notify({"type": "systemError"})
                await until(lambda: app.thread_notification_sequence >= sequence + 4)
                assert len(inbox.pending_notifications()) == 1 and len(jobs.health) == 2
                sequence = app.thread_notification_sequence
                await notify({"type": "idle"})
                await until(lambda: app.thread_notification_sequence > sequence)
                assert app.blocked and not dispatcher.status()["ready"]
                with pytest.raises(wake.WakeError, match=r"^codex_thread_system_error$"):
                    await dispatcher.resume_execution()
                assert app.blocked and server.counter == 0
                server.thread_status = "idle"
                await dispatcher.resume_execution()
                await wait_ready(dispatcher)
                assert jobs.health[-1]["state"] == "ready"
                assert [entry["state"] for entry in jobs.health] == ["ready", "held", "ready"]
                assert inbox.pending_notifications()[0]["recovered_at"] is not None
                assert dispatcher.last_error is None and server.counter == 0
            finally:
                await stop_dispatcher(dispatcher)
                inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status",
    [None, {"type": "unknown"}, {"type": "active"}, {"type": "active", "activeFlags": ["private unknown flag"]}],
)
def test_malformed_current_thread_status_fails_closed_with_fixed_code(tmp_path, bundle, status):
    async def scenario():
        inbox = TelegramInbox(tmp_path / "db" / "inbound.sqlite3")
        inbox.duty_set(True)
        jobs = FakeJobs(inbox)
        server = MockServer(peer_threads=True)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(
                wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
            )
            dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
            dispatcher.start()
            try:
                await until(lambda: dispatcher.status()["ready"] and bool(jobs.health))
                await server.sockets[-1].send(
                    json.dumps({"method": "thread/status/changed", "params": {"threadId": THREAD, "status": status}})
                )
                await until(lambda: app.blocked and len(jobs.health) == 2)
                assert not dispatcher.status()["ready"]
                assert dispatcher.last_error == "codex_thread_status_invalid"
                assert jobs.health[-1] == {
                    "component": "codex",
                    "state": "held",
                    "error_code": "codex_thread_status_invalid",
                }
                assert inbox.active_jobs() == [] and server.counter == 0
            finally:
                await stop_dispatcher(dispatcher)
                inbox.close()

    asyncio.run(scenario())


def test_warm_error_while_start_intent_commits_prevents_model_start(tmp_path, bundle):
    async def scenario():
        inbox, _event = new_inbox(tmp_path)
        server = MockServer(peer_threads=True)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(
                wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
            )

            class ErrorBeforeIntentReply(FakeJobs):
                async def request(self, operation, **arguments):
                    response = await super().request(operation, **arguments)
                    if operation == "job_start_intent":
                        server.thread_statuses[arguments["thread_id"]] = "systemError"
                        await server.sockets[-1].send(
                            json.dumps(
                                {
                                    "method": "thread/status/changed",
                                    "params": {"threadId": arguments["thread_id"], "status": {"type": "systemError"}},
                                }
                            )
                        )
                        await until(lambda: dispatcher.app.blocked)
                    return response

            jobs = ErrorBeforeIntentReply(inbox)
            dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
            dispatcher.start()
            try:
                await until(lambda: bool(inbox.active_jobs()) and inbox.active_jobs()[0]["state"] == "needs_review")
                job = inbox.active_jobs()[0]
                assert job["thread_id"] != THREAD and job["turn_id"] is None
                assert dispatcher.app.recovery_error == "codex_thread_system_error"
                assert not dispatcher.status()["ready"] and server.counter == 0
                assert not any(call.get("method") == "turn/start" for call in server.calls)
            finally:
                await stop_dispatcher(dispatcher)
                inbox.close()

    asyncio.run(scenario())


def test_duty_off_cold_start_and_resume_preserve_pending_without_launch(tmp_path, bundle):
    async def scenario():
        inbox, event_id = new_inbox(tmp_path)
        inbox.duty_set(False)
        jobs = FakeJobs(inbox)
        app = wake.AppServer(wake.WakeConfig(THREAD, instruction_root=str(bundle), instruction_revision=REVISION))
        app.connected = True
        app.ready_event.set()
        app.run_job_turn = AsyncMock()
        dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
        dispatcher._start_worker()
        jobs.changed.set()
        try:
            await until(lambda: dispatcher.execution_state == "paused")
            await dispatcher.resume_execution()
            await asyncio.sleep(0.02)
            app.run_job_turn.assert_not_awaited()
            assert not any(operation == "job_claim" for operation, _args in jobs.calls)
            assert not dispatcher.status()["ready"]
            assert inbox.active_jobs() == [] and inbox.resolve(event_id) is not None
        finally:
            await stop_dispatcher(dispatcher)
            inbox.close()

    asyncio.run(scenario())


def test_explicit_pause_survives_wake_restart_until_explicit_resume(tmp_path, bundle):
    async def scenario():
        inbox, event_id = new_inbox(tmp_path)
        first_app = wake.AppServer(wake.WakeConfig(THREAD, instruction_root=str(bundle), instruction_revision=REVISION))
        first = wake.DurableWakeDispatcher(first_app, 123, FakeJobs(inbox))
        await first.pause()
        await first.close()
        assert inbox.status()["execution_paused"] is True
        server = MockServer(peer_threads=True)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(
                wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
            )
            jobs = FakeJobs(inbox, finish=True)
            second = wake.DurableWakeDispatcher(app, 123, jobs)
            second.start()
            try:
                await until(lambda: second.execution_state == "paused" and app.ready_event.is_set())
                assert server.counter == 0 and inbox.active_jobs() == []
                assert inbox.resolve(event_id) is not None and not second.status()["ready"]
                await second.resume_execution()
                await until(lambda: second.completed == 1)
                assert inbox.status()["execution_paused"] is False
                assert server.counter == 1
            finally:
                await stop_dispatcher(second)
                inbox.close()

    asyncio.run(scenario())


def test_shutdown_preserves_enabled_execution_for_transport_restart(tmp_path, bundle):
    async def scenario():
        inbox, event_id = new_inbox(tmp_path)
        app = wake.AppServer(wake.WakeConfig(THREAD, instruction_root=str(bundle), instruction_revision=REVISION))
        dispatcher = wake.DurableWakeDispatcher(app, 123, FakeJobs(inbox))
        try:
            await dispatcher.close()
            assert inbox.status()["execution_paused"] is False
            assert inbox.resolve(event_id) is not None
        finally:
            inbox.close()

    asyncio.run(scenario())


def test_owned_pause_has_bounded_unknown_readback_and_preserves_hold(tmp_path, bundle, monkeypatch):
    monkeypatch.setattr(wake, "FAILED_RECOVERY_TIMEOUT", 0.05)

    async def scenario():
        inbox, _event = new_inbox(tmp_path)
        job = inbox.claim(instruction_root=str(bundle), instruction_revision=REVISION)
        bind_test_peer(inbox, job)
        job = inbox.start_intent(
            job_id=job["job_id"], lease=job["lease"], expected_revision=job["revision"], thread_id=THREAD
        )
        job = inbox.bind_turn(
            job_id=job["job_id"], lease=job["lease"], expected_revision=job["revision"], turn_id="owned"
        )
        app = wake.AppServer(wake.WakeConfig(THREAD, instruction_root=str(bundle), instruction_revision=REVISION))
        app.active_turn = "owned"
        app.outcome_unknown = True
        app.connected = True
        app.interrupt = AsyncMock()

        async def stuck_metadata(_turn_id):
            await asyncio.Event().wait()

        app._turn_metadata = stuck_metadata
        dispatcher = wake.DurableWakeDispatcher(app, 123, FakeJobs(inbox))
        dispatcher.current_job = job
        try:
            with pytest.raises(wake.WakeError, match=r"^wake_pause_outcome_unknown$"):
                await asyncio.wait_for(dispatcher.pause(), 0.5)
            assert inbox.status()["execution_paused"] is True
            assert inbox.active_jobs()[0]["state"] == "needs_review"
            assert app.active_turn == "owned" and app.outcome_unknown
            assert not dispatcher.enabled and not dispatcher.status()["ready"]
        finally:
            dispatcher.current_job = None
            await stop_dispatcher(dispatcher)
            inbox.close()

    asyncio.run(scenario())
