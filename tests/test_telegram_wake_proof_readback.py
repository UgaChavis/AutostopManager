"""Lost stop-proof replies are reconciled without replaying any external effect."""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from unittest.mock import AsyncMock

from telegram_wake_fakes import THREAD
from test_telegram_wake_durable import FakeJobs, bind_test_peer

from autostop_manager import telegram_wake as wake
from autostop_manager.telegram_inbox import TelegramInbox


class LostProofJobs(FakeJobs):
    def __init__(self, inbox, *, failure):
        super().__init__(inbox)
        self.failure = failure
        self.proof_lost = False
        self.proof_attempts = 0

    async def request(self, operation, **arguments):
        if operation == "job_execution_proof":
            self.proof_attempts += 1
            if self.failure not in {"uncommitted", "missing_slot_job"}:
                await super().request(operation, **arguments)
            self.proof_lost = True
            raise wake.TransportLost("wake_bridge_disconnected")
        if operation == "job_read" and self.proof_lost and self.failure == "read_unavailable":
            raise wake.TransportLost("wake_bridge_disconnected")
        if operation == "job_execution_slot" and self.proof_lost and self.failure == "missing_slot_job":
            return {"ok": True}
        return await super().request(operation, **arguments)


@contextmanager
def stopped_request(tmp_path, *, failure, manual=False):
    with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
        inbox.duty_set(True)
        first = inbox.record(123, 1)
        inbox.record(124, 2)
        job = inbox.claim(instruction_root=str(tmp_path), instruction_revision="a" * 40)
        bind_test_peer(inbox, job)
        job = inbox.start_intent(**wake.DurableWakeDispatcher._guard(job), thread_id=THREAD)
        job = inbox.bind_turn(**wake.DurableWakeDispatcher._guard(job), turn_id="owned")
        if manual:
            job = inbox.needs_review(**wake.DurableWakeDispatcher._guard(job), reason_code="codex_turn_failed")
            job = inbox.finish_execution(
                **wake.DurableWakeDispatcher._guard(job),
                proof_kind="terminal",
                thread_id=THREAD,
                turn_id="owned",
                terminal_status="failed",
                local_tasks_done=True,
            )
            inbox.set_execution_paused(paused=True, expected_revision=inbox.status()["execution_revision"])
            job = inbox.resolve_job(
                **wake.DurableWakeDispatcher._guard(job), decision="continue_bound_turn", effects_reviewed=True
            )
        key = inbox.reserve_effect(
            **wake.DurableWakeDispatcher._guard(job),
            kind="result",
            text_sha256="b" * 64,
            peer_id=123,
            reply_to_message_id=1,
        )
        unknown = inbox.mark_effect_intent(key)
        app = wake.AppServer(wake.WakeConfig(THREAD))
        app.connected = True
        app.active_turn = "owned"
        app.active_event_id = first
        app.outcome_unknown = True
        app._turn_metadata = AsyncMock(return_value="failed")
        app.request = AsyncMock(side_effect=AssertionError("private stop-proof QA must not call a model"))
        jobs = LostProofJobs(inbox, failure=failure)
        dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), jobs)
        dispatcher.current_job = job
        yield inbox, job, unknown, dispatcher, jobs
        assert inbox.read_effect(key) == unknown
        assert inbox.event_state(first) == "open"
        assert jobs.proof_attempts == 1
        app.request.assert_not_awaited()


def test_lost_committed_proof_reply_advances_other_peer_without_replaying_unknown_effect(tmp_path):
    async def scenario():
        with stopped_request(tmp_path, failure="committed") as (inbox, job, _unknown, dispatcher, _jobs):
            await dispatcher._finish_request(job, "codex_turn_failed")
            assert dispatcher.stop_proof_verified and not dispatcher.terminal_stop_pending
            assert dispatcher.execution_error is None and dispatcher.last_error is None
            assert inbox.execution_slot() is None
            stored = inbox.read_job(job["job_id"])
            assert stored["state"] == "needs_review" and stored["execution_proof"] == "failed"
            following = inbox.claim(instruction_root=str(tmp_path), instruction_revision="a" * 40)
            assert following["peer_id"] == 124

    asyncio.run(scenario())


def test_uncommitted_manual_stop_cannot_reuse_the_previous_turn_proof(tmp_path):
    async def scenario():
        with stopped_request(tmp_path, failure="uncommitted", manual=True) as (inbox, job, _unknown, dispatcher, _jobs):
            await dispatcher._finish_request(job, "codex_turn_failed")
            assert not dispatcher.stop_proof_verified and dispatcher.terminal_stop_pending
            assert dispatcher.execution_error == "wake_pause_outcome_unknown"
            assert inbox.execution_slot()["job_id"] == job["job_id"]
            assert inbox.execution_slot()["slot_phase"] == "manual"
            assert inbox.read_job(job["job_id"])["execution_proof"] == "failed"

    asyncio.run(scenario())


def test_lost_proof_reply_with_unavailable_exact_readback_keeps_executor_held(tmp_path):
    async def scenario():
        with stopped_request(tmp_path, failure="read_unavailable") as (inbox, job, _unknown, dispatcher, _jobs):
            await dispatcher._finish_request(job, "codex_turn_failed")
            assert not dispatcher.stop_proof_verified and dispatcher.terminal_stop_pending
            assert dispatcher.execution_error == "wake_pause_outcome_unknown"
            assert inbox.execution_slot() is None
            assert inbox.read_job(job["job_id"])["execution_proof"] == "failed"

    asyncio.run(scenario())


def test_missing_slot_job_is_not_evidence_that_manual_execution_stopped(tmp_path):
    async def scenario():
        with stopped_request(tmp_path, failure="missing_slot_job", manual=True) as (
            inbox,
            job,
            _unknown,
            dispatcher,
            _jobs,
        ):
            await dispatcher._finish_request(job, "codex_turn_failed")
            assert not dispatcher.stop_proof_verified and dispatcher.terminal_stop_pending
            assert dispatcher.execution_error == "wake_pause_outcome_unknown"
            assert inbox.execution_slot()["job_id"] == job["job_id"]
            assert inbox.execution_slot()["slot_phase"] == "manual"
            assert inbox.read_job(job["job_id"])["execution_proof"] == "failed"

    asyncio.run(scenario())
