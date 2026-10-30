"""Legacy recovery must look beyond already-reviewed active jobs."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from autostop_manager import telegram_wake as wake


def job(index, *, proof="not_started"):
    return {
        "job_id": f"job-{index:032x}",
        "lease": "synthetic-lease",
        "revision": 1,
        "event_ids": [f"inbound-{index + 1}"],
        "state": "needs_review",
        "context_mode": "legacy",
        "execution_proof": proof,
    }


def dispatcher_for(pages):
    calls = []

    async def request(operation, **kwargs):
        if operation == "job_execution_slot":
            return {"job": None}
        assert operation == "job_active"
        calls.append(kwargs["after_job_id"])
        return pages[kwargs["after_job_id"]]

    dispatcher = object.__new__(wake.DurableWakeDispatcher)
    dispatcher.jobs = SimpleNamespace(request=request, changed=asyncio.Event())
    dispatcher.inbox_status = {"compatibility_hold": True}
    dispatcher._reconcile_job = AsyncMock()
    dispatcher.stop_proof_verified = True
    return dispatcher, calls


def test_reconciles_unproved_legacy_job_after_first_page():
    async def scenario():
        reviewed = [job(index) for index in range(50)]
        pending = job(50, proof=None)
        cursor = reviewed[-1]["job_id"]
        dispatcher, calls = dispatcher_for(
            {None: {"jobs": reviewed, "next_job_id": cursor}, cursor: {"jobs": [pending], "next_job_id": None}}
        )

        await dispatcher._reconcile_active()

        assert calls == [None, cursor]
        dispatcher._reconcile_job.assert_awaited_once_with(pending)
        assert dispatcher.jobs.changed.is_set()

    asyncio.run(scenario())


def test_cycle_and_page_limit_stop_without_reconciling(monkeypatch):
    async def scenario():
        first, second = job(0), job(1)
        a, b = first["job_id"], second["job_id"]
        pages = {
            None: {"jobs": [first], "next_job_id": a},
            a: {"jobs": [second], "next_job_id": b},
            b: {"jobs": [first], "next_job_id": a},
        }
        dispatcher, calls = dispatcher_for(pages)
        with pytest.raises(wake.WakeError, match="wake_job_status_invalid"):
            await dispatcher._reconcile_active()
        assert calls == [None, a, b]
        dispatcher._reconcile_job.assert_not_awaited()

        monkeypatch.setattr(wake, "MAX_ACTIVE_JOB_PAGES", 2)
        dispatcher, calls = dispatcher_for(pages)
        with pytest.raises(wake.WakeError, match="wake_job_status_invalid"):
            await dispatcher._reconcile_active()
        assert calls == [None, a]
        dispatcher._reconcile_job.assert_not_awaited()

    asyncio.run(scenario())


def test_stale_hold_without_unproved_legacy_remains_held():
    async def scenario():
        dispatcher, calls = dispatcher_for({None: {"jobs": [job(0)], "next_job_id": None}})
        with pytest.raises(wake.WakeError, match="wake_legacy_context_requires_review"):
            await dispatcher._reconcile_active()
        assert calls == [None]
        dispatcher._reconcile_job.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["transport", "schema"])
def test_second_page_failure_never_reconciles_or_retries(failure):
    async def scenario():
        first = job(0)
        cursor = first["job_id"]
        dispatcher, calls = dispatcher_for(
            {None: {"jobs": [first], "next_job_id": cursor}, cursor: {"jobs": "invalid", "next_job_id": None}}
        )
        original_request = dispatcher.jobs.request

        async def request(operation, **kwargs):
            if operation == "job_active" and kwargs["after_job_id"] == cursor and failure == "transport":
                calls.append(cursor)
                raise wake.TransportLost("wake_bridge_disconnected")
            return await original_request(operation, **kwargs)

        dispatcher.jobs.request = request
        error = wake.TransportLost if failure == "transport" else wake.WakeError
        with pytest.raises(error):
            await dispatcher._reconcile_active()
        assert calls == [None, cursor]
        dispatcher._reconcile_job.assert_not_awaited()
        assert not dispatcher.jobs.changed.is_set()

    asyncio.run(scenario())
