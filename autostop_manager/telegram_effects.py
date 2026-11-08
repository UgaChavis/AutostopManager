"""Independent client effect lanes; local control never waits for network work."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


def server_budget(request: dict[str, Any]) -> float:
    operation = str(request.get("operation") or "")
    pages = request.get("history_pages", 1)
    if request.get("job_id") and operation in {"send", "send_photo", "send_document", "job_handoff"}:
        if type(pages) is not int or pages < 1:
            pages = 1
        base = 20.0 if request.get("mode") == "dry_run" else 150.0
        return base + 40.0 * pages
    if operation in {"monitor_voice_stage", "owner_event_voice_stage"}:
        return 120.0
    if operation == "job_result" and request.get("reason_code") == "human_answered":
        return 20.0
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
    return 5.0 if budget == 2.0 else budget + (10.0 if budget == 20.0 else 15.0)


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


class _EffectLane:
    """One owned consumer, request deadlines and bounded cleanup.

    Disconnecting a waiter never cancels an already issued external effect.
    An uncooperative task stops the lane instead of overlapping another send.
    """

    def __init__(
        self, *, capacity: int | None = None, queue_seconds: float | None = None, cleanup_seconds: float = 10.0
    ) -> None:
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
        if self.active and self.capacity is not None and len(self._pending) >= self.capacity:
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
            item.queued_until = loop.time() + min(self.queue_seconds or timeout, timeout)
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


class EffectExecutor:
    """Serialize each job independently, retaining a separate service lane.

    A disconnected caller cannot cancel a started effect. A client whose
    operation ignores cancellation holds only its own lane until readback.
    """

    def __init__(
        self, *, capacity: int | None = None, queue_seconds: float | None = None, cleanup_seconds: float = 10.0
    ) -> None:
        self.capacity = capacity
        self.queue_seconds = queue_seconds
        self.cleanup_seconds = cleanup_seconds
        self._service = self._new_lane()
        self._lanes: dict[str, _EffectLane] = {}
        self.closed = False

    def _new_lane(self) -> _EffectLane:
        return _EffectLane(
            capacity=self.capacity, queue_seconds=self.queue_seconds, cleanup_seconds=self.cleanup_seconds
        )

    @property
    def failed(self) -> asyncio.Event:
        return self._service.failed

    @property
    def progress_event(self) -> asyncio.Event:
        return self._service.progress_event

    @property
    def active(self) -> bool:
        return self._service.active or any(lane.active for lane in self._lanes.values())

    @property
    def queued(self) -> int:
        return self._service.queued + sum(lane.queued for lane in self._lanes.values())

    def has_job_work(self, job_id: str) -> bool:
        return self._service.has_job_work(job_id) or any(lane.has_job_work(job_id) for lane in self._lanes.values())

    async def submit(
        self,
        factory: Callable[[], Awaitable[Any]],
        *,
        timeout: float,
        job_id: str | None = None,
        lane_key: str | None = None,
    ) -> Any:
        if self.closed or self.failed.is_set():
            raise EffectQueueError("bridge_effects_held")
        key = lane_key or (f"job:{job_id}" if job_id else None)
        lane = self._service if key is None else self._lanes.setdefault(key, self._new_lane())
        try:
            return await lane.submit(factory, timeout=timeout, job_id=job_id)
        finally:
            if key is not None and lane._worker is not None:

                def prune(_task: asyncio.Task[None]) -> None:
                    if not lane.active and not lane.failed.is_set() and self._lanes.get(key) is lane:
                        self._lanes.pop(key, None)

                lane._worker.add_done_callback(prune)

    async def close(self) -> None:
        self.closed = True
        await asyncio.gather(self._service.close(), *(lane.close() for lane in self._lanes.values()))
