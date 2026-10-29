"""Faults around peer persistence use only private ledgers and fake App Server."""

from __future__ import annotations

import asyncio
import os

import pytest
from test_telegram_wake_durable import FakeJobs
from test_telegram_wake_durable import bundle as bundle
from test_telegram_wake_peer_lifecycle import inherited_server, peer_case

from autostop_manager import telegram_wake as wake


@pytest.mark.parametrize("cleanup_pending", [False, True])
def test_restart_reconciliation_fences_peer_only_after_local_cleanup(tmp_path, bundle, cleanup_pending):
    async def scenario():
        server = inherited_server()
        async with peer_case(tmp_path, bundle, server) as (app, dispatcher, inbox, job):

            async def crash(_thread):
                raise asyncio.CancelledError

            dispatcher._materialize_new_peer = crash
            with pytest.raises(asyncio.CancelledError):
                await dispatcher._select_peer(job)
            mapping = inbox.read_peer_thread(job["job_id"])
            assert mapping["state"] == "creating"
            assert mapping["thread_id"] == server.created_threads[0]
            before_event = inbox.resolve(job["event_ids"][0])
            later = wake.DurableWakeDispatcher(wake.AppServer(app.config), os.geteuid(), FakeJobs(inbox))
            pending = asyncio.get_running_loop().create_future()
            if cleanup_pending:
                later.cleanup_tasks.add(pending)
            try:
                # The real restart path must fence creation through its stop
                # proof, without the test-only recover_on_startup shortcut.
                await later._reconcile_active()
                recovered = inbox.read_peer_thread(job["job_id"])
                assert recovered == {
                    **mapping,
                    "state": "creating" if cleanup_pending else "creation_unknown",
                    "revision": mapping["revision"] + (not cleanup_pending),
                }
                held = inbox.read_job(job["job_id"])
                assert held["state"] == "needs_review"
                assert held["execution_proof"] == (None if cleanup_pending else "not_started")
                assert held["thread_id"] is held["turn_id"] is None and held["effects"] == []
                assert (inbox.execution_slot() is not None) is cleanup_pending
                assert inbox.resolve(job["event_ids"][0]) == before_event
                assert inbox.event_state(job["event_ids"][0]) == "open"
                assert not inbox.status()["execution_paused"]
                assert len(server.created_threads) == 1 and server.counter == 0
            finally:
                pending.set_result(None)
                await later.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("committed", [False, True])
def test_ready_commit_unknown_is_resolved_by_exact_readback(tmp_path, bundle, committed):
    async def scenario():
        server = inherited_server()
        async with peer_case(tmp_path, bundle, server) as (_app, dispatcher, inbox, job):
            request = dispatcher.jobs.request

            async def lose_commit_reply(operation, **arguments):
                if operation == "job_peer_thread_bind" and arguments.get("materialized") is True:
                    if committed:
                        await request(operation, **arguments)
                    raise wake.TransportLost("wake_bridge_disconnected")
                return await request(operation, **arguments)

            dispatcher.jobs.request = lose_commit_reply
            if committed:
                await dispatcher._select_peer(job)
            else:
                with pytest.raises(wake.WakeError, match="wake_peer_thread_unknown"):
                    await dispatcher._select_peer(job)
            mapping = inbox.read_peer_thread(job["job_id"])
            assert mapping["state"] == ("ready" if committed else "creation_unknown")
            assert mapping["thread_id"] == server.created_threads[0]
            assert len(server.created_threads) == 1 and server.counter == 0

    asyncio.run(scenario())
