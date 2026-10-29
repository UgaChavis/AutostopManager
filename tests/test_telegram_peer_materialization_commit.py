"""Faults around peer persistence use only private ledgers and fake App Server."""

from __future__ import annotations

import asyncio

import pytest
from test_telegram_wake_durable import bundle as bundle
from test_telegram_wake_peer_lifecycle import inherited_server, peer_case

from autostop_manager import telegram_wake as wake


def test_crash_before_materialization_keeps_exact_unready_thread(tmp_path, bundle):
    async def scenario():
        server = inherited_server()
        async with peer_case(tmp_path, bundle, server) as (_app, dispatcher, inbox, job):

            async def crash(_thread):
                raise asyncio.CancelledError

            dispatcher._materialize_new_peer = crash
            with pytest.raises(asyncio.CancelledError):
                await dispatcher._select_peer(job)
            mapping = inbox.read_peer_thread(job["job_id"])
            assert mapping["state"] == "creating"
            assert mapping["thread_id"] == server.created_threads[0]
            inbox.recover_on_startup()
            recovered = inbox.read_peer_thread(job["job_id"])
            assert recovered["state"] == "creation_unknown"
            assert recovered["thread_id"] == mapping["thread_id"]
            assert len(server.created_threads) == 1 and server.counter == 0

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
