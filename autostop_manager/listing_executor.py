"""Bound caller waits without abandoning real Avito worker admissions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
import math
import threading
import time
from typing import Any

MAX_WORKERS = 2
MAX_WAITING_CALLS = 2
TOTAL_WAIT_SECONDS = 30.0


class ListingExecutionError(RuntimeError):
    def __init__(self, code: str, *, stage: str, adapter_started: bool = False, adapter_may_continue: bool = False):
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.adapter_started = adapter_started
        self.adapter_may_continue = adapter_may_continue


class BoundedListingExecutor:
    """At most two workers and two additional unfinished submissions per process.

    Cancelled callers never cancel their concurrent future: doing so would free
    admission while a running HTTP request remains, or accumulate cancelled work
    items in ThreadPoolExecutor's unbounded queue. Abandoned queued jobs instead
    finish without calling the adapter when a worker reaches them. Only actual
    completion releases admission, including after the caller's loop has closed.
    """

    def __init__(
        self,
        *,
        max_workers: int = MAX_WORKERS,
        max_waiting_calls: int = MAX_WAITING_CALLS,
        timeout_seconds: float = TOTAL_WAIT_SECONDS,
    ) -> None:
        if not 1 <= max_workers <= MAX_WORKERS or not 0 <= max_waiting_calls <= MAX_WAITING_CALLS:
            raise ValueError("listing_executor_capacity_invalid")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("listing_executor_timeout_invalid")
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="avito-listing")
        self._limit = max_workers + max_waiting_calls
        self._timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._pending = 0

    @property
    def pending_count(self) -> int:
        with self._lock:
            return self._pending

    def close(self) -> None:
        """Drain real workers; intended for lifecycle shutdown, not caller cancellation."""
        self._pool.shutdown(wait=True)

    def _completed(self, _future: Future[dict[str, Any]]) -> None:
        with self._lock:
            self._pending -= 1

    async def run(self, operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        deadline = time.monotonic() + self._timeout_seconds
        abandoned = threading.Event()
        started = threading.Event()
        job_lock = threading.Lock()
        with self._lock:
            if self._pending >= self._limit:
                raise ListingExecutionError("provider_busy", stage="admission")
            self._pending += 1

        def execute() -> dict[str, Any]:
            with job_lock:
                if abandoned.is_set() or time.monotonic() >= deadline:
                    raise ListingExecutionError("provider_wait_timeout", stage="admission")
                started.set()
            return operation()

        try:
            concurrent = self._pool.submit(execute)
        except RuntimeError:
            with self._lock:
                self._pending -= 1
            raise ListingExecutionError("provider_executor_unavailable", stage="admission") from None
        concurrent.add_done_callback(self._completed)
        wrapped = asyncio.wrap_future(concurrent)

        def consume_result(future: asyncio.Future[dict[str, Any]]) -> None:
            if not future.cancelled():
                future.exception()

        wrapped.add_done_callback(consume_result)
        timeout = asyncio.timeout(max(0.0, deadline - time.monotonic()))
        try:
            async with timeout:
                return await asyncio.shield(wrapped)
        except TimeoutError:
            if not timeout.expired():
                raise ListingExecutionError(
                    "provider_execution_failed", stage="worker_wait", adapter_started=True
                ) from None
            with job_lock:
                abandoned.set()
                adapter_started = started.is_set()
            raise ListingExecutionError(
                "provider_wait_timeout",
                stage="worker_wait" if adapter_started else "admission",
                adapter_started=adapter_started,
                adapter_may_continue=adapter_started and not concurrent.done(),
            ) from None
        except asyncio.CancelledError:
            with job_lock:
                abandoned.set()
            raise
        except ListingExecutionError:
            raise
        except Exception:  # noqa: BLE001 - sanitize unexpected adapter failures at this external-call boundary.
            raise ListingExecutionError(
                "provider_execution_failed", stage="worker_wait", adapter_started=True
            ) from None


_EXECUTOR = BoundedListingExecutor()


async def run_listing_request(operation: Callable[[], dict[str, Any]], *, dry_run: bool = False) -> dict[str, Any]:
    """Run pure validation inline, otherwise wait once for one bounded worker."""
    if dry_run:
        return operation()
    try:
        return await _EXECUTOR.run(operation)
    except ListingExecutionError as error:
        return {
            "ok": False,
            "source": "avito",
            "error": error.code,
            "stage": error.stage,
            "adapter_started": error.adapter_started,
            "adapter_may_continue": error.adapter_may_continue,
            "retry_attempted": False,
        }
