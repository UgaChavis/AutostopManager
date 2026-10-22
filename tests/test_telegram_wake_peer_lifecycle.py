"""New peer persistence and inherited settings use isolated sockets and inboxes."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from telegram_wake_fakes import THREAD, MockServer, connected_app, until
from test_telegram_wake_durable import REVISION, FakeJobs
from test_telegram_wake_durable import bundle as bundle

from autostop_manager import telegram_wake as wake
from autostop_manager.telegram_inbox import TelegramInbox


@asynccontextmanager
async def peer_case(tmp_path, bundle, server):
    with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
        inbox.duty_set(True)
        inbox.record(123, 1)
        job = inbox.claim(instruction_root=str(bundle), instruction_revision=REVISION)
        async with connected_app(tmp_path, server) as app:
            await app.resume()
            dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), FakeJobs(inbox, finish=True))
            try:
                yield app, dispatcher, inbox, job
            finally:
                await dispatcher.close()


def inherited_server():
    server = MockServer(peer_threads=True, strict_peer_lifecycle=True)
    server.thread_settings[THREAD] = {
        "reasoningEffort": "max",
        "serviceTier": "priority",
        "activePermissionProfile": {"id": ":danger-full-access", "extends": None},
    }
    return server


def test_new_peer_is_bound_and_materialized_before_any_model_turn(tmp_path, bundle):
    async def scenario():
        server = inherited_server()
        async with peer_case(tmp_path, bundle, server) as (bootstrap, dispatcher, inbox, job):

            def check_bound(thread_id):
                mapping = inbox.read_peer_thread(job["job_id"])
                assert mapping["state"] == "ready" and mapping["thread_id"] == thread_id
                assert dispatcher.jobs.calls[-1][0] == "job_peer_thread_read"
                assert inbox.read_job(job["job_id"])["thread_id"] is None
                assert server.counter == 0

            server.archive_observer = check_bound
            await dispatcher._select_peer(job)
            peer = dispatcher.app
            assert peer.resumed_settings == bootstrap.resumed_settings
            assert server.counter == 0
            assert server.created_threads == [peer.config.thread_id]
            lifecycle = [
                (call["method"], call["params"]["threadId"])
                for call in server.calls
                if call["method"] in {"thread/archive", "thread/unarchive"}
            ]
            assert lifecycle == [("thread/archive", peer.config.thread_id), ("thread/unarchive", peer.config.thread_id)]
            assert peer.config.thread_id in server.materialized_threads
            assert peer.resume_overrides["config"] == {"model_reasoning_effort": "max"}
            assert peer.resume_overrides["permissions"] == ":danger-full-access"
            assert "ephemeral" not in peer.resume_overrides
            assert bootstrap.resume_overrides == {} and bootstrap.expected_resume_settings is None

            await dispatcher._execute(job)
            assert server.counter == 1 and inbox.read_job(job["job_id"])["state"] == "completed"
            assert sum(call["method"] == "thread/archive" for call in server.calls) == 1
            starts = [call for call in server.calls if call["method"] == "turn/start"]
            assert starts[0]["params"]["threadId"] == peer.config.thread_id

    asyncio.run(scenario())


def test_empty_peer_requires_materialization_and_bare_resume_loses_settings(tmp_path):
    async def scenario():
        server = inherited_server()
        async with connected_app(tmp_path, server) as bootstrap:
            await bootstrap.resume()
            created = await bootstrap.request("thread/start", bootstrap.peer_start_params())
            thread_id = created["thread"]["id"]
            peer = wake.AppServer(replace(bootstrap.config, thread_id=thread_id))
            try:
                await peer.connect()
                with pytest.raises(wake.WakeError, match=r"^codex_thread_resume_rejected$"):
                    await peer.resume()
                await bootstrap.request("thread/archive", {"threadId": thread_id})
                await bootstrap.request("thread/unarchive", {"threadId": thread_id})
                assert await peer.resume() == "idle"
                assert peer.resumed_settings["reasoningEffort"] is None
                assert peer.resumed_settings["activePermissionProfile"] is None
                assert server.counter == 0
            finally:
                await peer.close()

    asyncio.run(scenario())


def test_peer_resume_keeps_overrides_after_reconnect_and_blocks_settings_drift(tmp_path, bundle, monkeypatch):
    async def scenario():
        server = inherited_server()
        async with peer_case(tmp_path, bundle, server) as (bootstrap, dispatcher, _inbox, job):
            await dispatcher._select_peer(job)
            peer = dispatcher.app
            await peer.websocket.close()
            await until(lambda: peer.reconnect_attempts >= 1 and peer.ready_event.is_set())
            assert peer.resumed_settings == bootstrap.resumed_settings
            server.peer_resume_overrides["reasoningEffort"] = "low"
            await peer.websocket.close()
            await until(lambda: peer.blocked)
            assert peer.recovery_error == "wake_worker_settings_mismatch"
            assert server.counter == 0
            resumes = [
                call["params"]
                for call in server.calls
                if call["method"] == "thread/resume" and call["params"]["threadId"] == peer.config.thread_id
            ]
            assert len(resumes) >= 3
            assert all(params["config"] == {"model_reasoning_effort": "max"} for params in resumes)
            assert all(params["permissions"] == ":danger-full-access" for params in resumes)
            assert sum(call["method"] == "thread/archive" for call in server.calls) == 1

    monkeypatch.setattr(wake, "RECONNECT_DELAYS", (0.001,))
    asyncio.run(scenario())


def test_proof_peer_resume_does_not_apply_bootstrap_overrides(tmp_path, bundle):
    async def scenario():
        server = inherited_server()
        async with peer_case(tmp_path, bundle, server) as (_bootstrap, dispatcher, _inbox, job):
            await dispatcher._select_peer(job)
            thread_id = dispatcher.app.config.thread_id
            await dispatcher._use_thread(thread_id, verify_settings=False)
            assert dispatcher.app.resume_overrides == {}
            assert dispatcher.app.expected_resume_settings is None
            params = [call for call in server.calls if call["method"] == "thread/resume"][-1]["params"]
            assert params == {"threadId": thread_id, "cwd": wake.PROJECT_DIR, "excludeTurns": True}
            assert server.counter == 0
            assert sum(call["method"] == "thread/archive" for call in server.calls) == 1

    asyncio.run(scenario())


def test_existing_ready_peer_never_runs_materialization_again(tmp_path, bundle):
    async def scenario():
        server = inherited_server()
        async with peer_case(tmp_path, bundle, server) as (_bootstrap, dispatcher, _inbox, job):
            await dispatcher._select_peer(job)
            thread_id = dispatcher.app.config.thread_id
            await dispatcher._retire_peer(asyncio.get_running_loop().time() + 2)
            calls = len(server.calls)
            await dispatcher._select_peer(job)
            assert dispatcher.app.config.thread_id == thread_id
            assert not any(
                call["method"] in {"thread/start", "thread/archive", "thread/unarchive"}
                for call in server.calls[calls:]
            )
            assert server.counter == 0

    asyncio.run(scenario())


def test_initial_peer_settings_mismatch_holds_before_model_start(tmp_path, bundle):
    async def scenario():
        server = inherited_server()
        server.peer_resume_overrides["model"] = "unexpected-model"
        async with peer_case(tmp_path, bundle, server) as (_bootstrap, dispatcher, inbox, job):
            await dispatcher._execute(job)
            assert dispatcher.execution_error == "wake_worker_settings_mismatch"
            assert inbox.status()["execution_paused"]
            assert inbox.read_job(job["job_id"])["execution_proof"] == "not_started"
            assert server.counter == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["thread/archive", "thread/unarchive"])
def test_materialization_rejection_never_recreates_or_starts_model(tmp_path, bundle, method):
    async def scenario():
        server = inherited_server()
        server.reject_method = method
        async with peer_case(tmp_path, bundle, server) as (_bootstrap, dispatcher, inbox, job):
            await dispatcher._execute(job)
            assert dispatcher.execution_error == "wake_shared_backend_rejected"
            assert inbox.status()["execution_paused"]
            assert inbox.read_job(job["job_id"])["execution_proof"] == "not_started"
            assert inbox.read_peer_thread(job["job_id"])["state"] == "ready"
            assert len(server.created_threads) == 1 and server.counter == 0
            assert sum(call["method"] == method for call in server.calls) == 1

    asyncio.run(scenario())
