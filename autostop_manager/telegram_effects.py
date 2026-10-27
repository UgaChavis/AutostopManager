"""Bounded serial effect execution; local control never waits for this lane."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


def server_budget(request: dict[str, Any]) -> float:
    operation = str(request.get("operation") or "")
    if operation in {"monitor_voice_stage", "owner_event_voice_stage"}:
        return 120.0
    if operation in {"send_photo", "send_document", "send_voice", "download", "job_effect_readback"}:
        return 20.0 if request.get("mode") == "dry_run" else 150.0
    if operation in {"send", "send_owner_notification", "notify_owner", "job_handoff"}:
        return 20.0 if request.get("mode") == "dry_run" else 60.0
    if operation in {
        "probe",
        "read",
        "search",
        "dialogs",
        "resolve_phone",
        "monitor_read",
        "monitor_context",
        "monitor_target",
        "owner_target",
        "owner_event_read",
        "owner_notification_readback",
        "owner_notification_idempotency_readback",
    }:
        return 20.0
    return 2.0


def rpc_budget(request: dict[str, Any]) -> float:
    budget = server_budget(request)
    return {2.0: 5.0, 20.0: 30.0, 60.0: 75.0, 120.0: 135.0, 150.0: 165.0}[budget]


class EffectQueueError(ValueError):
    """Fixed, content-free admission/timeout error."""


@dataclass
class _Pending:
    factory: Callable[[], Awaitable[Any]]
    future: asyncio.Future[Any]
    deadline: float
    job_id: str | None = None
    started: bool = False
    expiry: asyncio.TimerHandle | None = None
    queued_until: float | None = None


def _observe(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()


class EffectExecutor:
    """One owned consumer, eight queued requests, bounded wait and cleanup.

    Disconnecting a waiter never cancels an already issued external effect.
    An uncooperative task stops the lane instead of overlapping another send.
    """

    def __init__(self, *, capacity: int = 8, queue_seconds: float = 5.0, cleanup_seconds: float = 10.0) -> None:
        self.capacity = capacity
        self.queue_seconds = queue_seconds
        self.cleanup_seconds = cleanup_seconds
        self._pending: deque[_Pending] = deque()
        self._worker: asyncio.Task[None] | None = None
        self._operation: asyncio.Task[Any] | None = None
        self.failed = asyncio.Event()
        self.closed = False
        self.active_job_id: str | None = None
        self._progress_event = asyncio.Event()

    @property
    def progress_event(self) -> asyncio.Event:
        """Capture a signal before admission so concurrent progress is not lost."""
        return self._progress_event

    def _signal_progress(self) -> None:
        previous = self._progress_event
        self._progress_event = asyncio.Event()
        previous.set()

    @property
    def queued(self) -> int:
        return sum(not item.future.done() for item in self._pending)

    @property
    def active(self) -> bool:
        return (self._worker is not None and not self._worker.done()) or (
            self._operation is not None and not self._operation.done()
        )

    def has_job_work(self, job_id: str) -> bool:
        return (self.active and self.active_job_id == job_id) or any(
            item.job_id == job_id and not item.future.done() for item in self._pending
        )

    async def submit(self, factory: Callable[[], Awaitable[Any]], *, timeout: float, job_id: str | None = None) -> Any:
        if self.closed or self.failed.is_set():
            raise EffectQueueError("bridge_effects_held")
        self._pending = deque(item for item in self._pending if not item.future.done())
        if self.active and len(self._pending) >= self.capacity:
            raise EffectQueueError("bridge_busy")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        future.add_done_callback(_observe)
        item = _Pending(factory, future, loop.time() + timeout, job_id)
        if not self.active:
            self.active_job_id = job_id
            self._worker = asyncio.create_task(self._consume(item), name="telegram-effects")
        else:
            self._pending.append(item)
            item.queued_until = loop.time() + min(self.queue_seconds, timeout)
            item.expiry = loop.call_at(item.queued_until, self._expire, item)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if not item.started:
                future.cancel()
            raise

    @staticmethod
    def _expire(item: _Pending) -> None:
        if not item.started and not item.future.done():
            item.future.set_exception(EffectQueueError("bridge_queue_timeout"))

    async def _consume(self, item: _Pending) -> None:
        try:
            while True:
                if item.expiry is not None:
                    item.expiry.cancel()
                if item.queued_until is not None and asyncio.get_running_loop().time() >= item.queued_until:
                    self._expire(item)
                if not item.future.done():
                    item.started = True
                    self.active_job_id = item.job_id
                    await self._execute(item)
                    self._signal_progress()
                if self.closed or self.failed.is_set() or not self._pending:
                    return
                item = self._pending.popleft()
        finally:
            if self._operation is None or self._operation.done():
                self.active_job_id = None
            while self._pending:
                waiting = self._pending.popleft()
                if waiting.expiry is not None:
                    waiting.expiry.cancel()
                if not waiting.future.done():
                    waiting.future.set_exception(EffectQueueError("bridge_effects_held"))
            self._signal_progress()

    async def _execute(self, item: _Pending) -> None:
        if asyncio.get_running_loop().time() >= item.deadline:
            item.future.set_exception(EffectQueueError("bridge_effect_timeout"))
            return

        async def invoke() -> Any:
            return await item.factory()

        operation: asyncio.Task[Any] = asyncio.create_task(invoke(), name="telegram-effect")
        operation.add_done_callback(_observe)
        self._operation = operation
        try:
            remaining = max(0.0, item.deadline - asyncio.get_running_loop().time())
            done, _ = await asyncio.wait({operation}, timeout=remaining)
            if not done:
                operation.cancel()
                done, _ = await asyncio.wait({operation}, timeout=self.cleanup_seconds)
                if not done:
                    self.failed.set()
                if not item.future.done():
                    item.future.set_exception(EffectQueueError("bridge_effect_timeout"))
                return
            if not item.future.done():
                if operation.cancelled():
                    item.future.set_exception(EffectQueueError("bridge_effect_cancelled"))
                elif (error := operation.exception()) is not None:
                    item.future.set_exception(error)
                else:
                    item.future.set_result(operation.result())
        except asyncio.CancelledError:
            operation.cancel()
            if not item.future.done():
                item.future.set_exception(EffectQueueError("bridge_effect_cancelled"))
            raise
        finally:
            if operation.done():
                self._operation = None

    async def close(self) -> None:
        self.closed = True
        self._signal_progress()
        if self._operation is not None:
            self._operation.cancel()
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            done, _ = await asyncio.wait({self._worker}, timeout=self.cleanup_seconds)
            if not done:
                self.failed.set()
        if self._operation is not None and not self._operation.done():
            done, _ = await asyncio.wait({self._operation}, timeout=self.cleanup_seconds)
            if not done:
                self.failed.set()
