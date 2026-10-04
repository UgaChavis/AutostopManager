from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from autostop_manager import listing_executor
from autostop_manager.listing_executor import BoundedListingExecutor, ListingExecutionError


async def _pending(executor: BoundedListingExecutor, count: int) -> None:
    async with asyncio.timeout(5):
        while executor.pending_count != count:
            await asyncio.sleep(0)


def _blocked(loop, entered, release, calls, name):
    def operation():
        calls.append(name)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "test_worker_was_not_released"
        return {"ok": True, "name": name}

    return operation


def _record(calls: list[str], name: str) -> dict[str, Any]:
    calls.append(name)
    return {"ok": True}


def test_two_actual_workers_and_a_bounded_waiting_admission():
    executor = BoundedListingExecutor(max_workers=2, max_waiting_calls=1, timeout_seconds=5)
    release = threading.Event()
    calls: list[str] = []

    async def run():
        loop = asyncio.get_running_loop()
        entered = [asyncio.Event(), asyncio.Event()]
        running = [
            asyncio.create_task(executor.run(_blocked(loop, entered[index], release, calls, str(index))))
            for index in range(2)
        ]
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)), timeout=5)
        queued = asyncio.create_task(executor.run(lambda: _record(calls, "queued")))
        await _pending(executor, 3)
        with pytest.raises(ListingExecutionError) as busy:
            await executor.run(lambda: _record(calls, "overflow"))
        assert busy.value.code == "provider_busy"
        assert busy.value.stage == "admission"
        assert sorted(calls) == ["0", "1"]
        release.set()
        assert all(result["ok"] for result in await asyncio.gather(*running, queued))
        await _pending(executor, 0)
        assert sorted(calls) == ["0", "1", "queued"]

    try:
        asyncio.run(run())
    finally:
        release.set()
        executor.close()


def test_running_cancellation_retains_the_real_worker_slot():
    executor = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=5)
    release = threading.Event()
    calls: list[str] = []

    async def run():
        entered = asyncio.Event()
        task = asyncio.create_task(executor.run(_blocked(asyncio.get_running_loop(), entered, release, calls, "first")))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert executor.pending_count == 1
        with pytest.raises(ListingExecutionError, match="provider_busy"):
            await executor.run(lambda: _record(calls, "extra"))
        assert calls == ["first"]
        release.set()
        await _pending(executor, 0)
        assert (await executor.run(lambda: {"ok": True}))["ok"] is True

    try:
        asyncio.run(run())
    finally:
        release.set()
        executor.close()


def test_cancelled_queued_work_retains_admission_and_never_calls_adapter():
    executor = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=5)
    release = threading.Event()
    calls: list[str] = []

    async def run():
        entered = asyncio.Event()
        first = asyncio.create_task(
            executor.run(_blocked(asyncio.get_running_loop(), entered, release, calls, "first"))
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        queued = asyncio.create_task(executor.run(lambda: _record(calls, "cancelled")))
        await _pending(executor, 2)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert executor.pending_count == 2
        with pytest.raises(ListingExecutionError, match="provider_busy"):
            await executor.run(lambda: _record(calls, "extra"))
        release.set()
        assert (await first)["ok"] is True
        await _pending(executor, 0)
        assert calls == ["first"]

    try:
        asyncio.run(run())
    finally:
        release.set()
        executor.close()


def test_total_wait_deadline_skips_queued_adapter_and_keeps_running_admission(monkeypatch):
    executor = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=1)
    monkeypatch.setattr(listing_executor, "_EXECUTOR", executor)
    release = threading.Event()
    calls: list[str] = []

    async def run():
        entered = asyncio.Event()
        first = asyncio.create_task(
            listing_executor.run_listing_request(_blocked(asyncio.get_running_loop(), entered, release, calls, "first"))
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        queued = asyncio.create_task(listing_executor.run_listing_request(lambda: _record(calls, "expired")))
        await _pending(executor, 2)
        running_error, queued_error = await asyncio.gather(first, queued)
        assert running_error == {
            "ok": False,
            "source": "avito",
            "error": "provider_wait_timeout",
            "stage": "worker_wait",
            "adapter_started": True,
            "adapter_may_continue": True,
            "retry_attempted": False,
        }
        assert queued_error == {
            **running_error,
            "stage": "admission",
            "adapter_started": False,
            "adapter_may_continue": False,
        }
        assert executor.pending_count == 2
        overflow = await listing_executor.run_listing_request(lambda: _record(calls, "overflow"))
        assert overflow["error"] == "provider_busy"
        release.set()
        await _pending(executor, 0)
        assert calls == ["first"]

    try:
        asyncio.run(run())
    finally:
        release.set()
        executor.close()


@pytest.mark.parametrize("exception_type", [RuntimeError, TimeoutError])
def test_unexpected_worker_exception_is_sanitized_once_and_releases_capacity(monkeypatch, exception_type):
    executor = BoundedListingExecutor(max_workers=1, max_waiting_calls=0)
    monkeypatch.setattr(listing_executor, "_EXECUTOR", executor)
    calls: list[str] = []

    def failed():
        calls.append("failed")
        raise exception_type("fixture-private-provider-response")

    async def run():
        response = await listing_executor.run_listing_request(failed)
        assert response["error"] == "provider_execution_failed"
        assert response["stage"] == "worker_wait"
        assert response["adapter_started"] is True
        assert response["adapter_may_continue"] is False
        assert response["retry_attempted"] is False
        assert "fixture-private-provider-response" not in repr(response)
        await _pending(executor, 0)
        assert (await listing_executor.run_listing_request(lambda: {"ok": True}))["ok"] is True
        assert calls == ["failed"]

    try:
        asyncio.run(run())
    finally:
        executor.close()


def test_worker_completion_releases_capacity_after_its_callers_event_loop_has_closed():
    executor = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=5)
    release = threading.Event()
    calls: list[str] = []

    async def first_loop():
        entered = asyncio.Event()
        task = asyncio.create_task(executor.run(_blocked(asyncio.get_running_loop(), entered, release, calls, "first")))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def second_loop():
        with pytest.raises(ListingExecutionError, match="provider_busy"):
            await executor.run(lambda: _record(calls, "extra"))
        release.set()
        await _pending(executor, 0)
        assert (await executor.run(lambda: {"ok": True}))["ok"] is True
        assert calls == ["first"]

    try:
        asyncio.run(first_loop())
        assert executor.pending_count == 1
        asyncio.run(second_loop())
    finally:
        release.set()
        executor.close()


def test_closed_executor_refuses_submission_without_leaking_admission():
    executor = BoundedListingExecutor(max_workers=1, max_waiting_calls=0)
    executor.close()
    with pytest.raises(ListingExecutionError) as unavailable:
        asyncio.run(executor.run(lambda: {"ok": True}))
    assert unavailable.value.code == "provider_executor_unavailable"
    assert unavailable.value.adapter_started is False
    assert executor.pending_count == 0
