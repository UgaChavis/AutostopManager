"""Bounded executor recovery over isolated local sockets and durable ledgers."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.server import unix_serve

from autostop_manager import telegram_wake as wake
from test_telegram_wake_durable import FakeJobs, REVISION, new_inbox, stop_dispatcher
from test_telegram_wake_durable import bundle as bundle
from telegram_wake_fakes import THREAD, MockServer, until
from telegram_wake_fakes import quick_recovery as quick_recovery


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
                    writer.write(json.dumps({"ok": True, "generation": snapshots}).encode() + b"\n")
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
            jobs = wake.BridgeJobs(os.geteuid(), path)
            dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), jobs)
            jobs.start()
            dispatcher._start_worker()
            try:
                if not cold_start:
                    await until(lambda: dispatcher.status()["ready"])
                    fail_status = True
                    subscribers[-1].write(b'{"ok":true,"generation":20}\n')
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
        server = MockServer(complete=False)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(
                wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
            )
            dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
            dispatcher.start()
            try:
                await until(lambda: bool(inbox.active_jobs()) and inbox.active_jobs()[0]["state"] == "running")
                owned = app.active_turn
                await dispatcher.pause()
                interrupts = [call for call in server.calls if call.get("method") == "turn/interrupt"]
                assert len(interrupts) == 1
                assert interrupts[0]["params"] == {"threadId": THREAD, "turnId": owned}
                assert server.turn_statuses[owned] == "interrupted"
                assert server.metadata_calls >= 2
                assert app.active_turn is None and not app.outcome_unknown
                assert not dispatcher.enabled and not dispatcher.active
                assert dispatcher.execution_state == "paused"
                assert inbox.active_jobs()[0]["state"] == "needs_review"
                assert inbox.resolve(event_id) is not None
                await dispatcher.resume_execution()
                await until(lambda: dispatcher.execution_state == "held")
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
                turn_id="other" if binding == "other_turn" else "manual",
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
            await dispatcher.pause()
            app.interrupt.assert_not_awaited()
            app._turn_metadata.assert_not_awaited()
            assert app.active_turn == "manual" and app.outcome_unknown
            assert not dispatcher.enabled and inbox.active_jobs()
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
        server = MockServer()
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
        job = inbox.start_intent(
            job_id=job["job_id"], lease=job["lease"], expected_revision=job["revision"], thread_id=THREAD
        )
        job = inbox.bind_turn(
            job_id=job["job_id"], lease=job["lease"], expected_revision=job["revision"], turn_id="owned"
        )
        app = wake.AppServer(wake.WakeConfig(THREAD, instruction_root=str(bundle), instruction_revision=REVISION))
        app.active_turn = "owned"
        app.outcome_unknown = True
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
