"""Stale telemetry keeps its last observation; missing observations stay unknown."""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_wake as wake
from telegram_wake_fakes import THREAD


def stale_dispatcher(snapshot):
    jobs = SimpleNamespace(
        snapshot=snapshot,
        status_fresh=False,
        status_age=31.0,
        snapshot_at=asyncio.get_running_loop().time() - 31,
        connected=False,
        cleanup_tasks=set(),
    )
    return wake.DurableWakeDispatcher(wake.AppServer(wake.WakeConfig(THREAD)), 123, jobs)


def test_disconnected_status_keeps_newest_cached_queue_observation():
    async def scenario():
        dispatcher = stale_dispatcher(
            {"pending_events": 7, "needs_review_jobs": 3, "held_peers": 2, "oldest_pending_age_seconds": 42}
        )
        dispatcher.inbox_status = {"pending_events": 0, "needs_review_jobs": 0, "held_peers": 0}
        dispatcher.inbox_status_at = asyncio.get_running_loop().time() - 32
        status = dispatcher.status()
        assert not status["status_fresh"] and not status["queue_can_advance"]
        assert status["queued"] == 7 and status["needs_review"] == 3
        assert status["held_peers"] == 2 and status["oldest_pending_age"] == 42

    asyncio.run(scenario())


def test_newer_direct_status_is_preserved_over_an_older_subscription():
    async def scenario():
        dispatcher = stale_dispatcher({"pending_events": 7, "execution_paused": False})
        dispatcher.inbox_status = {"pending_events": 2, "execution_paused": True}
        dispatcher.inbox_status_at = asyncio.get_running_loop().time()
        status = dispatcher.status()
        assert not status["status_fresh"]
        assert status["queued"] == 2 and status["execution_paused"] is True

    asyncio.run(scenario())


def test_status_without_an_observation_does_not_report_an_empty_queue():
    async def scenario():
        status = stale_dispatcher({}).status()
        assert not status["status_fresh"] and not status["ready"]
        assert all(status[key] is None for key in ("queued", "needs_review", "held_peers", "oldest_pending_age"))

    asyncio.run(scenario())


def observed_status(generation, *, paused, pending, revision):
    status = {
        "worker_protocol": wake.WORKER_PROTOCOL,
        "context_mode": "parallel_peer_v1",
        "enabled": True,
        "execution_paused": paused,
        "execution_revision": revision,
        "pending_events": pending,
    }
    if generation is not None:
        status["generation"] = generation
    return status


@pytest.mark.skipif(os.geteuid() != 0, reason="bridge job IPC is root-only")
@pytest.mark.parametrize(
    "old_generation,new_generation,pause_request,delayed_direct",
    [
        (41, 42, False, False),
        (42, 42, False, False),
        (None, None, False, False),
        (41, None, False, False),
        (41, 42, True, False),
        (41, 42, False, True),
    ],
)
def test_late_status_reply_cannot_displace_newer_observation(
    tmp_path, old_generation, new_generation, pause_request, delayed_direct
):
    async def scenario():
        old = observed_status(old_generation, paused=False, pending=7, revision=9)
        new = observed_status(new_generation, paused=True, pending=2, revision=10)
        paused_snapshot = asyncio.Event()
        release_old = asyncio.Event()
        requests = []

        async def bridge(reader, writer):
            try:
                request = json.loads(await reader.readline())
                requests.append(request)
                if len(requests) == 1:
                    paused_snapshot.set()
                    await release_old.wait()
                    status = old
                elif pause_request and len(requests) == 2:
                    status = old
                else:
                    status = new
                writer.write(json.dumps({"ok": True, "status": status}).encode() + b"\n")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        path = tmp_path / "bridge.sock"
        async with await asyncio.start_unix_server(bridge, path=path):
            jobs = wake.BridgeJobs(os.geteuid(), path)
            jobs.connected = True
            jobs.subscription_peer_pid = os.getpid()
            jobs.frame_at = asyncio.get_running_loop().time()
            jobs.snapshot = old | {"_bridge_status_order": wake._BridgeStatusOrder(os.getpid(), 0)}
            jobs.snapshot_at = jobs.frame_at
            app = wake.AppServer(wake.WakeConfig(THREAD))
            app.connected = True
            app.ready_event.set()
            dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), jobs)
            dispatcher.execution_state = "ready"
            dispatcher.worker = asyncio.create_task(asyncio.Event().wait())
            delayed_status = dispatcher._read_status if delayed_direct else jobs._snapshot
            snapshot_task = asyncio.create_task(delayed_status())
            try:
                await asyncio.wait_for(paused_snapshot.wait(), 1)
                if pause_request:
                    await dispatcher._set_execution_paused(True)
                else:
                    await dispatcher._read_status()
                release_old.set()
                await snapshot_task
                result = dispatcher.status()
                assert result["status_fresh"]
                assert dispatcher.inbox_status["execution_revision"] == 10
                assert result["execution_paused"] is True and result["execution_revision"] == 10
                assert result["queued"] == 2 and not result["ready"] and not result["queue_can_advance"]
                assert result["queue_block_reason"] == "paused"
                expected = [{"operation": "job_status"}] * 2
                if pause_request:
                    expected.append({"operation": "job_execution_set", "paused": True, "expected_revision": 9})
                assert requests == expected
            finally:
                release_old.set()
                dispatcher.worker.cancel()
                await asyncio.gather(snapshot_task, dispatcher.worker, return_exceptions=True)

    asyncio.run(scenario())


def test_same_bridge_generation_beats_request_order():
    async def scenario():
        old = observed_status(41, paused=False, pending=7, revision=9)
        new = observed_status(42, paused=True, pending=2, revision=10)
        old["_bridge_status_order"] = wake._BridgeStatusOrder(100, 2)
        new["_bridge_status_order"] = wake._BridgeStatusOrder(100, 1)
        dispatcher = stale_dispatcher(old)
        dispatcher.inbox_status = new
        dispatcher.inbox_status_at = dispatcher.jobs.snapshot_at - 1
        assert dispatcher.status()["queued"] == 2
        assert dispatcher.status()["execution_paused"] is True

    asyncio.run(scenario())


def test_bridge_reconnect_accepts_new_pid_with_lower_generation():
    async def scenario():
        old = observed_status(42, paused=True, pending=7, revision=10)
        new = observed_status(1, paused=False, pending=2, revision=1)
        old["_bridge_status_order"] = wake._BridgeStatusOrder(100, 1)
        new["_bridge_status_order"] = wake._BridgeStatusOrder(200, 2)
        dispatcher = stale_dispatcher(old)
        dispatcher.inbox_status = new
        dispatcher.jobs.status_fresh = True
        dispatcher.jobs.subscription_peer_pid = 100
        switching = dispatcher.status()
        assert switching["queued"] == 2 and not switching["status_fresh"] and not switching["queue_can_advance"]

        dispatcher.jobs.subscription_peer_pid = 200
        dispatcher.jobs.snapshot = new
        switched = dispatcher.status()
        assert switched["queued"] == 2 and switched["status_fresh"]
        assert not switched["execution_paused"] and switched["execution_revision"] == 1

    asyncio.run(scenario())
