from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from autostop_manager.telegram_effects import EffectExecutor, EffectQueueError


def test_five_client_effects_overlap_and_each_job_stays_serial():
    async def scenario():
        executor = EffectExecutor()
        entered = set()
        ready, release = asyncio.Event(), asyncio.Event()
        second_started = asyncio.Event()

        async def work(number):
            entered.add(number)
            if len(entered) == 5:
                ready.set()
            await release.wait()
            return number

        async def second():
            second_started.set()
            return "second"

        clients = [
            asyncio.create_task(executor.submit(lambda n=n: work(n), timeout=2, job_id=f"job-{n}")) for n in range(5)
        ]
        try:
            await asyncio.wait_for(ready.wait(), 1)
            next_request = asyncio.create_task(executor.submit(second, timeout=2, job_id="job-0"))
            await asyncio.sleep(0)
            assert executor.queued == 1 and not second_started.is_set()
            assert all(executor.has_job_work(f"job-{n}") for n in range(5))
            release.set()
            assert await asyncio.gather(*clients) == list(range(5))
            assert await next_request == "second"
        finally:
            release.set()
            await executor.close()

    asyncio.run(scenario())


def test_stuck_client_does_not_hold_other_client_or_service_lane():
    async def scenario():
        executor = EffectExecutor(cleanup_seconds=0.01)
        release = asyncio.Event()

        async def stuck():
            with suppress(asyncio.CancelledError):
                await asyncio.Event().wait()
            await release.wait()

        async def healthy():
            return "verified"

        try:
            with pytest.raises(EffectQueueError, match="bridge_effect_timeout"):
                await executor.submit(stuck, timeout=0.01, job_id="stuck")
            assert executor.has_job_work("stuck")
            assert await executor.submit(healthy, timeout=1, job_id="healthy") == "verified"
            assert await executor.submit(healthy, timeout=1) == "verified"
            with pytest.raises(EffectQueueError, match="bridge_effects_held"):
                await executor.submit(healthy, timeout=1, job_id="stuck")
        finally:
            release.set()
            await executor.close()

    asyncio.run(scenario())


def test_default_queue_has_no_eight_request_limit_and_cancelled_waiters_never_run():
    async def scenario():
        executor = EffectExecutor()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def first():
            entered.set()
            await release.wait()

        async def waiting(number):
            calls.append(number)

        first_request = asyncio.create_task(executor.submit(first, timeout=2, job_id="same"))
        await entered.wait()
        pending = [
            asyncio.create_task(executor.submit(lambda n=n: waiting(n), timeout=2, job_id="same")) for n in range(12)
        ]
        try:
            await asyncio.sleep(0)
            assert executor.queued == 12
            pending[0].cancel()
            with suppress(asyncio.CancelledError):
                await pending[0]
            release.set()
            await asyncio.gather(first_request, *pending[1:])
            assert calls == list(range(1, 12))
        finally:
            release.set()
            await executor.close()

    asyncio.run(scenario())
