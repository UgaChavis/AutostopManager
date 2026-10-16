"""Observe the real worker at the submission-outcome gate.

Only a named second Thread.start raises. Observation wraps the real Lock/Event
objects created by listing_executor for that one rejected invocation; all actual
acquire/release/wait/set calls delegate unchanged. The stdlib pool, queue,
WorkItem, Future and accepted sibling remain real and are never replaced.

While Thread.start has not returned, the fault hook releases the accepted
sibling. Its existing worker dequeues the rejected WorkItem. The hook waits for
a positive observation: an unset Event wait / submitter-owned Lock acquire in
that worker, or an actual backend entry. No sleep and no negative timing window.
A deadline failure means the instrumentation did not observe a supported gate;
it is not evidence that a backend ran. This challenge recognizes Event/Lock
admission gates; another implementation needs a separately reviewed observer.

The result proves that the real worker reaches a blocked gate before submit
outcome, and that the rejected backend never runs after that outcome or drain.
It does not simulate thread termination, resource exhaustion or a live incident.
"""

from __future__ import annotations

import asyncio
from functools import partial
import threading

from autostop_manager import listing_executor
from autostop_manager.listing_executor import BoundedListingExecutor, ListingExecutionError


class _ObservedLock:
    def __init__(self, submitter, observe):
        self.actual = threading.Lock()
        self.owner = None
        self.submitter = submitter
        self.observe = observe

    def acquire(self, *args, **kwargs):
        # Observe the real acquire's precondition without acquiring another lock.
        if self.actual.locked() and self.owner == self.submitter:
            self.observe("submitter_owned_lock_gate")
        acquired = self.actual.acquire(*args, **kwargs)
        if acquired:
            self.owner = threading.get_ident()
        return acquired

    def release(self):
        self.owner = None
        return self.actual.release()

    def locked(self):
        return self.actual.locked()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_args):
        self.release()


class _ObservedEvent:
    def __init__(self, observe):
        self.actual = threading.Event()
        self.observe = observe

    def wait(self, *args, **kwargs):
        if not self.actual.is_set():
            self.observe("unset_event_gate")
        return self.actual.wait(*args, **kwargs)

    def set(self):
        return self.actual.set()

    def clear(self):
        return self.actual.clear()

    def is_set(self):
        return self.actual.is_set()


class _ThreadingObservation:
    # The stdlib pool, Future and accepted sibling keep the real threading module.
    def __init__(self, submitter, observe):
        self.Lock = partial(_ObservedLock, submitter, observe)
        self.Event = partial(_ObservedEvent, observe)

    def __getattr__(self, name):
        return getattr(threading, name)


def test_existing_worker_observes_submit_outcome_gate_before_backend(monkeypatch):
    prefix = "preoutcome-disposable-worker"
    pool = BoundedListingExecutor(max_workers=2, max_waiting_calls=2, timeout_seconds=30)
    pool._pool._thread_name_prefix = prefix
    real_start = threading.Thread.start
    real_lock = threading.Lock
    real_event = threading.Event
    submitter = threading.get_ident()
    observation_ready = real_event()
    release_sibling = real_event()
    observation_lock = real_lock()
    observed = []
    calls = []
    faulted = []
    unresolved = False

    def observe(kind):
        if unresolved and threading.current_thread().name == prefix + "_0":
            with observation_lock:
                if not observed:
                    observed.append(kind)
                    observation_ready.set()

    def start(thread, *args, **kwargs):
        nonlocal unresolved
        if thread.name == prefix + "_1" and not faulted:
            faulted.append(thread.name)
            unresolved = True
            release_sibling.set()
            try:
                assert observation_ready.wait(5), (
                    "observer did not see an Event/Lock gate or backend entry; no race result is established"
                )
            finally:
                unresolved = False
            raise RuntimeError("synthetic_second_worker_start_failure")
        return real_start(thread, *args, **kwargs)

    async def scenario():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def accepted():
            calls.append("accepted")
            loop.call_soon_threadsafe(entered.set)
            assert release_sibling.wait(10), "accepted sibling was not released"
            return {"ok": True, "marker": "accepted-result"}

        def rejected():
            calls.append("rejected")
            observe("backend_entered_before_submit_outcome")
            return {"ok": True, "marker": "rejected"}

        first = asyncio.create_task(pool.run(accepted))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert pool.pending_count == 1
            with monkeypatch.context() as observation:
                observation.setattr(listing_executor, "threading", _ThreadingObservation(submitter, observe))
                try:
                    result = {"result": await pool.run(rejected)}
                except ListingExecutionError as error:
                    result = {
                        "error": error.code,
                        "stage": error.stage,
                        "adapter_started": error.adapter_started,
                        "adapter_may_continue": error.adapter_may_continue,
                    }
            accepted_result = await asyncio.wait_for(first, timeout=5)
            return result, accepted_result
        finally:
            release_sibling.set()
            await asyncio.gather(first, return_exceptions=True)

    try:
        with monkeypatch.context() as fault:
            fault.setattr(threading.Thread, "start", start)
            result, accepted_result = asyncio.run(scenario())
    finally:
        release_sibling.set()
        pool.close()

    assert faulted == [prefix + "_1"]
    assert observed in (["submitter_owned_lock_gate"], ["unset_event_gate"])
    assert result == {
        "error": "provider_executor_unavailable",
        "stage": "admission",
        "adapter_started": False,
        "adapter_may_continue": False,
    }
    assert accepted_result == {"ok": True, "marker": "accepted-result"}
    assert calls == ["accepted"]
    assert pool.pending_count == 0
    assert all(not thread.is_alive() for thread in pool._pool._threads)
