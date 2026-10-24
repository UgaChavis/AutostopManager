"""Local transport timeouts do not imply the durable request budget expired."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
import os
from unittest.mock import AsyncMock

from telegram_wake_fakes import THREAD
from test_telegram_wake_durable import FakeJobs, bind_test_peer

from autostop_manager import telegram_wake as wake
from autostop_manager.telegram_inbox import TelegramInbox


@contextmanager
def claimed_request(tmp_path, monkeypatch, *, remaining=1199):
    claimed_at = datetime.now(UTC)

    class Clock:
        fromisoformat = staticmethod(datetime.fromisoformat)

        @staticmethod
        def now(_tz):
            return claimed_at + timedelta(seconds=wake.REQUEST_TIMEOUT_SECONDS - remaining)

    monkeypatch.setattr(wake, "datetime", Clock)
    with TelegramInbox(tmp_path / "inbox.sqlite3", clock=lambda: claimed_at) as inbox:
        inbox.duty_set(True)
        inbox.record(123, 1)
        job = inbox.claim(instruction_root=str(tmp_path), instruction_revision="a" * 40)
        assert job is not None
        app = wake.AppServer(wake.WakeConfig(THREAD))
        app.request = AsyncMock(side_effect=AssertionError("synthetic timeout cases must not contact Codex"))
        dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), FakeJobs(inbox))
        yield inbox, job, app, dispatcher


def test_local_preparation_timeout_keeps_budget_and_requires_review_without_replay(tmp_path, monkeypatch):
    async def scenario():
        with claimed_request(tmp_path, monkeypatch) as (inbox, job, app, dispatcher):
            original_deadline = job["deadline_at"]
            request = dispatcher.jobs.request
            attempts = []

            async def timeout_preparation(operation, **arguments):
                if operation == "job_peer_thread_prepare":
                    attempts.append(operation)
                    remaining = dispatcher.request_deadline - asyncio.get_running_loop().time()
                    assert 1190 < remaining <= 1199
                    raise TimeoutError("synthetic private RPC timeout")
                return await request(operation, **arguments)

            monkeypatch.setattr(dispatcher.jobs, "request", timeout_preparation)
            await asyncio.wait_for(dispatcher._execute(job), 1)

            stored = inbox.read_job(job["job_id"])
            assert stored["state"] == "needs_review"
            assert stored["reason_code"] == "codex_transport_failed_or_unknown"
            assert stored["deadline_at"] == original_deadline
            assert stored["execution_proof"] == "not_started"
            assert not dispatcher.deadline_exceeded
            assert dispatcher.failed == 1 and dispatcher.completed == 0
            assert dispatcher.stop_proof_verified and not dispatcher.terminal_stop_pending
            assert dispatcher.request_deadline is None
            assert inbox.execution_slot() is None
            assert inbox.claim(instruction_root=str(tmp_path), instruction_revision="a" * 40) is None
            assert attempts == ["job_peer_thread_prepare"]
            app.request.assert_not_awaited()

    asyncio.run(scenario())


def test_local_recovery_timeout_keeps_unknown_effect_and_execution_slot(tmp_path, monkeypatch):
    async def scenario():
        with claimed_request(tmp_path, monkeypatch) as (inbox, job, app, dispatcher):
            bind_test_peer(inbox, job)
            job = inbox.start_intent(**dispatcher._guard(job), thread_id=THREAD)
            job = inbox.bind_turn(**dispatcher._guard(job), turn_id="known-turn")
            effect_key = inbox.reserve_effect(
                **dispatcher._guard(job),
                kind="result",
                text_sha256="b" * 64,
                peer_id=123,
                reply_to_message_id=1,
            )
            unknown_effect = inbox.mark_effect_intent(effect_key)
            original_deadline = job["deadline_at"]
            app.connected = True
            app.ready_event.set()
            dispatcher._use_thread = AsyncMock()
            app._wait_turn = AsyncMock(side_effect=TimeoutError("synthetic local transport timeout"))
            app._turn_metadata = AsyncMock(side_effect=TimeoutError("synthetic unavailable stop proof"))
            app.interrupt = AsyncMock(side_effect=AssertionError("unknown metadata cannot prove an interrupt target"))

            await asyncio.wait_for(dispatcher._execute(job, recovering=True), 1)

            stored = inbox.read_job(job["job_id"])
            assert stored["state"] == "needs_review"
            assert stored["reason_code"] == "codex_transport_failed_or_unknown"
            assert stored["deadline_at"] == original_deadline
            assert stored["execution_proof"] is None
            assert inbox.read_effect(effect_key) == unknown_effect
            assert unknown_effect["attempted"] and not unknown_effect["verified"]
            assert inbox.execution_slot()["job_id"] == job["job_id"]
            inbox.record(124, 2)
            assert inbox.claim(instruction_root=str(tmp_path), instruction_revision="a" * 40) is None
            assert not dispatcher.deadline_exceeded
            assert not dispatcher.stop_proof_verified and dispatcher.terminal_stop_pending
            assert dispatcher.request_deadline - asyncio.get_running_loop().time() > 1190
            assert dispatcher.status()["queue_block_reason"] == "stop_pending"
            assert dispatcher.failed == 1
            assert not any(name in {"job_execution_proof", "job_release"} for name, _arguments in dispatcher.jobs.calls)
            app._wait_turn.assert_awaited_once()
            app.interrupt.assert_not_awaited()
            app.request.assert_not_awaited()

    asyncio.run(scenario())


def test_overall_expiry_keeps_slot_until_late_cleanup_and_explicit_stop_proof(tmp_path, monkeypatch):
    async def scenario():
        with claimed_request(tmp_path, monkeypatch, remaining=0.04) as (inbox, job, app, dispatcher):
            monkeypatch.setattr(wake, "FAILED_RECOVERY_TIMEOUT", 0.04)
            original_deadline = job["deadline_at"]
            cleanup_entered = asyncio.Event()
            release_cleanup = asyncio.Event()

            async def preparation(_job):
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup_entered.set()
                    while not release_cleanup.is_set():
                        with suppress(asyncio.CancelledError):
                            await release_cleanup.wait()

            dispatcher._select_peer = preparation
            running = asyncio.create_task(dispatcher._execute(job))
            try:
                await asyncio.wait_for(cleanup_entered.wait(), 1)
                await asyncio.wait_for(running, 1)
                monotonic_deadline = dispatcher.request_deadline
                stored = inbox.read_job(job["job_id"])
                assert stored["state"] == "needs_review"
                assert stored["reason_code"] == "wake_request_deadline_exceeded"
                assert stored["deadline_at"] == original_deadline
                assert stored["execution_proof"] is None
                assert dispatcher.deadline_exceeded and dispatcher.failed == 1
                assert monotonic_deadline <= asyncio.get_running_loop().time()
                assert dispatcher.cleanup_pending and dispatcher.terminal_stop_pending
                assert not dispatcher.stop_proof_verified
                assert inbox.execution_slot()["job_id"] == job["job_id"]
                inbox.record(124, 2)
                assert inbox.claim(instruction_root=str(tmp_path), instruction_revision="a" * 40) is None

                release_cleanup.set()
                await asyncio.wait_for(asyncio.gather(dispatcher.execution_task, return_exceptions=True), 1)
                await asyncio.sleep(0)
                assert not dispatcher.cleanup_pending
                assert dispatcher.request_deadline == monotonic_deadline
                assert dispatcher.terminal_stop_pending
                assert inbox.execution_slot()["job_id"] == job["job_id"]
                assert inbox.read_job(job["job_id"])["execution_proof"] is None

                monkeypatch.setattr(wake, "FAILED_RECOVERY_TIMEOUT", 1)
                await dispatcher.pause(persist=False)
                stored = inbox.read_job(job["job_id"])
                assert stored["execution_proof"] == "not_started"
                assert stored["reason_code"] == "wake_request_deadline_exceeded"
                assert stored["deadline_at"] == original_deadline
                assert dispatcher.stop_proof_verified and not dispatcher.terminal_stop_pending
                assert dispatcher.request_deadline is None
                assert inbox.execution_slot() is None
                assert dispatcher.accepted == 1
                app.request.assert_not_awaited()
            finally:
                release_cleanup.set()
                await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 1)
                if dispatcher.execution_task is not None:
                    await asyncio.wait_for(asyncio.gather(dispatcher.execution_task, return_exceptions=True), 1)

    asyncio.run(scenario())
