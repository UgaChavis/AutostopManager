"""Wake shutdown owns its tasks even when bridge pause IPC fails early."""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest
from telegram_wake_fakes import THREAD, MockServer, until
from test_telegram_wake_durable import REVISION, FakeJobs
from test_telegram_wake_durable import bundle as bundle
from websockets.asyncio.server import unix_serve

from autostop_manager import telegram_wake as wake
from autostop_manager.telegram_inbox import TelegramInbox


class OfflineJobs(FakeJobs):
    unavailable = False

    async def request(self, operation, **arguments):
        if self.unavailable:
            raise OSError("synthetic bridge unavailable")
        return await super().request(operation, **arguments)

    async def close(self):
        self.connected = False
        self.changed.set()


@pytest.fixture(autouse=True)
def private_app_server_only(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    connect = wake.AppServer.connect

    async def guarded_connect(app):
        assert Path(app.config.app_socket).parent.resolve() == tmp_path.resolve(), "private Unix AppServer only"
        await connect(app)

    monkeypatch.setattr(wake.AppServer, "connect", guarded_connect)


async def cancel_owned(*tasks):
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
    await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)


@pytest.mark.parametrize("bridge_unavailable", [False, True])
def test_close_running_job_finishes_owned_tasks_and_keeps_unknown_slot(
    tmp_path, bundle, monkeypatch, bridge_unavailable
):
    async def scenario():
        server = MockServer(peer_threads=True, complete=False)
        with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
            inbox.duty_set(True)
            inbox.record(123, 1)
            jobs = OfflineJobs(inbox)
            path = str(tmp_path / "app.sock")
            async with unix_serve(server.handle, path, compression=None):
                app = wake.AppServer(
                    wake.WakeConfig(
                        THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION
                    )
                )
                dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), jobs)
                unrelated = asyncio.create_task(asyncio.Event().wait())
                dispatcher.start()
                try:
                    await until(lambda: bool(inbox.active_jobs()) and inbox.active_jobs()[0]["state"] == "running")
                    job = inbox.active_jobs()[0]
                    original_deadline = dispatcher.request_deadline
                    original_current = dispatcher.current_job
                    effect_key = None
                    if bridge_unavailable:
                        effect_key = inbox.reserve_effect(
                            **dispatcher._guard(job),
                            kind="result",
                            text_sha256="b" * 64,
                            peer_id=123,
                            reply_to_message_id=1,
                        )
                        unknown_effect = inbox.mark_effect_intent(effect_key)
                        job = inbox.read_job(job["job_id"])
                    calls_before = len(jobs.calls)
                    jobs.unavailable = bridge_unavailable
                    jobs.connected = not bridge_unavailable
                    await asyncio.wait_for(dispatcher.close(), 2)
                    await asyncio.sleep(0)
                    assert dispatcher.worker.done() and dispatcher.execution_task.done()
                    assert all(
                        task is None or task.done()
                        for task in (
                            dispatcher.lifecycle_monitor,
                            dispatcher.worker_monitor,
                            dispatcher.supervisor,
                            dispatcher.bootstrap_supervisor,
                        )
                    )
                    assert not dispatcher.cleanup_pending and not dispatcher.active
                    assert not dispatcher.app.connected and not dispatcher.bootstrap_app.connected
                    assert not unrelated.done() and server.counter == 1
                    stored = inbox.read_job(job["job_id"])
                    if bridge_unavailable:
                        assert stored == job
                        assert stored["execution_proof"] is None
                        assert inbox.execution_slot()["job_id"] == job["job_id"]
                        assert inbox.read_effect(effect_key) == unknown_effect
                        assert dispatcher.current_job is original_current
                        assert dispatcher.request_deadline == original_deadline
                        assert dispatcher.terminal_stop_pending and not dispatcher.stop_proof_verified
                        assert jobs.calls[calls_before:] == []
                        assert not any(call.get("method") == "turn/interrupt" for call in server.calls)
                    else:
                        assert stored["execution_proof"] == "interrupted"
                        assert inbox.execution_slot() is None
                        assert dispatcher.current_job is None and dispatcher.request_deadline is None
                        assert dispatcher.stop_proof_verified and not dispatcher.terminal_stop_pending
                finally:
                    await cancel_owned(dispatcher.worker, dispatcher.execution_task, unrelated)
                    await dispatcher.app.close()
                    await dispatcher.bootstrap_app.close()
            assert not any(task is not asyncio.current_task() and not task.done() for task in asyncio.all_tasks())

    monkeypatch.setattr(wake, "RECONNECT_DELAYS", (0.01,))
    asyncio.run(scenario())


@contextmanager
def claimed_offline_case(tmp_path):
    with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
        inbox.duty_set(True)
        inbox.record(123, 1)
        job = inbox.claim(instruction_root=str(tmp_path), instruction_revision=REVISION)
        jobs = OfflineJobs(inbox)
        jobs.unavailable = True
        jobs.connected = False
        app = wake.AppServer(wake.WakeConfig(THREAD))
        dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), jobs)
        dispatcher.current_job = job
        dispatcher.request_deadline = dispatcher._deadline(job)
        yield inbox, job, dispatcher


def test_close_retains_cancellation_resistant_execution_without_releasing_slot(tmp_path):
    async def scenario():
        with claimed_offline_case(tmp_path) as (inbox, job, dispatcher):
            entered, cleanup_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def execution():
                try:
                    entered.set()
                    await asyncio.Event().wait()
                finally:
                    cleanup_entered.set()
                    while not release.is_set():
                        with suppress(asyncio.CancelledError):
                            await release.wait()

            dispatcher.execution_task = asyncio.create_task(execution())
            await entered.wait()
            unrelated = asyncio.create_task(asyncio.Event().wait())
            try:
                # Exercise the actual existing 5s shutdown budget and retention.
                await asyncio.wait_for(dispatcher.close(), 7)
                assert cleanup_entered.is_set() and not dispatcher.execution_task.done()
                assert dispatcher.execution_task in dispatcher.cleanup_tasks and dispatcher.cleanup_pending
                assert not unrelated.done()
                assert dispatcher.current_job is job and dispatcher.terminal_stop_pending
                assert inbox.read_job(job["job_id"]) == job and inbox.execution_slot() is not None
                with pytest.raises(wake.WakeError, match="wake_cleanup_pending"):
                    await dispatcher.resume_execution()
                assert jobs_no_proof(dispatcher)
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(dispatcher.execution_task, return_exceptions=True), 1)
                await cancel_owned(unrelated)
                await asyncio.sleep(0)
            assert not dispatcher.cleanup_pending
            assert inbox.execution_slot() is not None and inbox.read_job(job["job_id"])["execution_proof"] is None

    asyncio.run(scenario())


def jobs_no_proof(dispatcher):
    return not any(
        operation in {"job_execution_proof", "job_release"} for operation, _arguments in dispatcher.jobs.calls
    )


def test_close_external_cancellation_propagates_and_retains_owned_cleanup(tmp_path):
    async def scenario():
        with claimed_offline_case(tmp_path) as (inbox, job, dispatcher):
            entered, cleanup_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def execution():
                try:
                    entered.set()
                    await asyncio.Event().wait()
                finally:
                    cleanup_entered.set()
                    await release.wait()

            dispatcher.execution_task = asyncio.create_task(execution())
            await entered.wait()
            unrelated = asyncio.create_task(asyncio.Event().wait())
            closing = asyncio.create_task(dispatcher.close())
            try:
                await asyncio.wait_for(cleanup_entered.wait(), 1)
                closing.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await closing
                assert dispatcher.execution_task in dispatcher.cleanup_tasks and dispatcher.cleanup_pending
                assert not dispatcher.execution_task.done() and not unrelated.done()
                assert dispatcher.current_job is job and dispatcher.terminal_stop_pending
                assert inbox.read_job(job["job_id"]) == job and inbox.execution_slot() is not None
                assert jobs_no_proof(dispatcher)
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(dispatcher.execution_task, return_exceptions=True), 1)
                await cancel_owned(closing, unrelated)
                await asyncio.sleep(0)
            assert not dispatcher.cleanup_pending

    asyncio.run(scenario())
