"""Stale telemetry keeps its last observation; missing observations stay unknown."""

import asyncio
from types import SimpleNamespace

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
