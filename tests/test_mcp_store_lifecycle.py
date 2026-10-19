"""Store drain evidence from real executors and owned, inert worker threads."""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from urllib.request import Request

import pytest

from autostop_manager import bounded_http_read, store_api
from autostop_manager.bounded_http_read import TrackedTransportAdmission
from autostop_manager.listing_executor import BoundedListingExecutor
from autostop_manager.mcp_store_executor import StoreWorkers, offload_store


@pytest.fixture(autouse=True)
def no_application_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("Store lifecycle unit test attempted application network")

    # The Windows event loop owns an internal loopback socketpair.
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def workers(*, timeout=3, waiting=2):
    return StoreWorkers(
        reads=BoundedListingExecutor(max_workers=1, max_waiting_calls=waiting, timeout_seconds=timeout),
        stateful=BoundedListingExecutor(max_workers=1, max_waiting_calls=waiting, timeout_seconds=timeout),
        transport=TrackedTransportAdmission(3),
    )


def close(owner):
    owner.reads.close()
    owner.stateful.close()


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


def assert_snapshot(snapshot, owner, *, status, pending):
    assert snapshot["format"] == "store_worker_lifecycle_v1"
    assert snapshot["instance_id"] == owner.instance_id
    assert snapshot["pid"] == os.getpid()
    assert snapshot["status"] == status
    assert snapshot["accepting"] is (status == "accepting")
    assert snapshot["drained"] is (status == "drained")
    assert snapshot["pending"] == pending


def test_idle_drain_is_permanent_idempotent_and_never_invokes_late_work():
    owner = workers()
    calls = []
    read = offload_store(lambda: calls.append("read") or {"ok": True}, "store_search", workers=owner)
    write = offload_store(lambda: calls.append("write") or {"ok": True}, "store_management_action", workers=owner)
    zero = {"read": 0, "stateful": 0, "transport": 0}

    async def scenario():
        assert_snapshot(owner.status(), owner, status="accepting", pending=zero)
        for _ in range(2):
            assert_snapshot(owner.begin_drain(), owner, status="drained", pending=zero)
            outcome = await owner.drain(0.1)
            assert outcome["ok"] is True
            assert_snapshot(outcome, owner, status="drained", pending=zero)
            for operation in (read, write):
                result = await operation()
                assert result["ok"] is False
                assert result["meta"]["request_dispatched"] is False
                assert result["meta"]["outcome_uncertain"] is False
            assert not owner.transport.acquire(blocking=False)
        assert calls == []

    try:
        asyncio.run(scenario())
    finally:
        close(owner)


def test_drain_keeps_accepted_queue_and_finishes_each_operation_once():
    owner = workers()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def backend(marker: str):
        calls.append(marker)
        if marker == "first":
            entered.set()
            assert release.wait(5)
        return {"ok": True, "marker": marker}

    operation = offload_store(backend, "store_management_action", workers=owner)

    async def scenario():
        first = asyncio.create_task(operation("first"))
        queued = None
        try:
            await until(entered.is_set)
            queued = asyncio.create_task(operation("accepted-queued"))
            await until(lambda: owner.stateful.pending_count == 2)
            assert_snapshot(
                owner.begin_drain(), owner, status="draining", pending={"read": 0, "stateful": 2, "transport": 0}
            )
            result = await operation("late")
            assert result["ok"] is False and result["meta"]["request_dispatched"] is False
            first_wait = await owner.drain(0.03)
            assert first_wait["ok"] is False and first_wait["error_code"] == "store_drain_timeout"
            assert calls == ["first"] and not first.done() and not queued.done()
            release.set()
            assert [row["marker"] for row in await asyncio.gather(first, queued)] == ["first", "accepted-queued"]
            finished = await owner.drain(1)
            assert finished["ok"] is True
            assert calls == ["first", "accepted-queued"]
            assert_snapshot(finished, owner, status="drained", pending={"read": 0, "stateful": 0, "transport": 0})
        finally:
            release.set()
            await asyncio.gather(first, *([queued] if queued is not None else []), return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        close(owner)


def test_cancelled_running_and_queued_callers_do_not_falsely_drain_or_start_queued_write():
    owner = workers()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def backend(marker: str):
        calls.append(marker)
        entered.set()
        assert release.wait(5)
        calls.append("readback-finished")
        return {"ok": True}

    operation = offload_store(backend, "store_management_action", workers=owner)

    async def scenario():
        running = asyncio.create_task(operation("one-post"))
        queued = None
        try:
            await until(entered.is_set)
            queued = asyncio.create_task(operation("abandoned-queued-post"))
            await until(lambda: owner.stateful.pending_count == 2)
            for task in (running, queued):
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            owner.begin_drain()
            result = await owner.drain(0.03)
            assert result["ok"] is False
            assert result["pending"]["stateful"] == 2
            assert calls == ["one-post"]
            release.set()
            assert (await owner.drain(1))["ok"] is True
            assert calls == ["one-post", "readback-finished"]
        finally:
            release.set()
            await asyncio.gather(running, *([queued] if queued is not None else []), return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        close(owner)


def test_executor_timeout_and_cancelled_drain_never_release_running_work_or_reopen():
    owner = workers(timeout=0.05)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def backend():
        entered.set()
        calls.append("one-post")
        assert release.wait(5)
        calls.append("readback-finished")
        return {"ok": True}

    operation = offload_store(backend, "store_management_action", workers=owner)

    async def scenario():
        task = asyncio.create_task(operation())
        try:
            await until(entered.is_set)
            result = await task
            assert result["status"] == "compensating"
            assert result["meta"]["adapter_may_continue"] is True
            owner.begin_drain()
            drain_task = asyncio.create_task(owner.drain(2))
            await asyncio.sleep(0)
            drain_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await drain_task
            assert owner.status()["accepting"] is False
            assert owner.status()["pending"]["stateful"] == 1
            started = time.monotonic()
            for _ in range(2):
                timed_out = await owner.drain(0.03)
                assert timed_out["ok"] is False
                assert timed_out["error_code"] == "store_drain_timeout"
                assert timed_out["pending"]["stateful"] == 1
            assert time.monotonic() - started < 0.8
            assert calls == ["one-post"]
            release.set()
            assert (await owner.drain(1))["ok"] is True
            assert calls == ["one-post", "readback-finished"]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        close(owner)


def test_drain_counts_reserved_work_before_thread_pool_submit_finishes(monkeypatch):
    owner = workers()
    entered_submit, release_submit, finished = threading.Event(), threading.Event(), threading.Event()
    calls, results, errors = [], [], []
    original_submit = owner.stateful._pool.submit

    def delayed_submit(*args, **kwargs):
        entered_submit.set()
        assert release_submit.wait(5)
        return original_submit(*args, **kwargs)

    monkeypatch.setattr(owner.stateful._pool, "submit", delayed_submit)
    operation = offload_store(
        lambda: calls.append("accepted-write") or {"ok": True}, "store_management_action", workers=owner
    )

    def caller():
        try:
            results.append(asyncio.run(operation()))
        except BaseException as error:  # noqa: BLE001 - surface every owned-thread failure in the test assertion.
            errors.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=caller, name="store-drain-reserved-caller")
    thread.start()
    try:
        assert entered_submit.wait(2)
        snapshot = owner.begin_drain()
        assert snapshot["pending"]["stateful"] == 1 and snapshot["drained"] is False
        timed_out = asyncio.run(owner.drain(0.03))
        assert timed_out["ok"] is False and timed_out["pending"]["stateful"] == 1
        assert calls == []
        release_submit.set()
        assert finished.wait(2)
        assert not errors and results == [{"ok": True}]
        assert calls == ["accepted-write"]
        assert asyncio.run(owner.drain(1))["ok"] is True
    finally:
        release_submit.set()
        thread.join(timeout=3)
        close(owner)
    assert not thread.is_alive()


def test_real_http_worker_cleanup_remains_pending_after_adapter_returns(monkeypatch):
    owner = workers()
    cleanup_entered, release_cleanup = threading.Event(), threading.Event()

    def blocked_cleanup():
        cleanup_entered.set()
        assert release_cleanup.wait(5)

    async def fake_fetch(_request, *, deadline, **_kwargs):
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            await asyncio.to_thread(blocked_cleanup)

    monkeypatch.setattr(bounded_http_read, "_fetch", fake_fetch)

    def backend():
        assert store_api.store_transport_admission.get() is owner.transport
        with pytest.raises(TimeoutError):
            store_api.urlopen(Request("http://127.0.0.1/inert-cleanup"), timeout=0.1)
        return {"ok": False, "synthetic": "caller-deadline-expired"}

    operation = offload_store(backend, "store_search", workers=owner)

    async def scenario():
        try:
            result = await operation()
            assert result["synthetic"] == "caller-deadline-expired" and cleanup_entered.is_set()
            await until(lambda: owner.reads.pending_count == 0)
            assert owner.transport.pending_count == 1
            assert_snapshot(
                owner.begin_drain(), owner, status="draining", pending={"read": 0, "stateful": 0, "transport": 1}
            )
            # Capacity is three, so refusal proves closure, not mere saturation.
            assert not owner.transport.acquire(blocking=False)
            result = await owner.drain(0.03)
            assert result["ok"] is False and result["pending"]["transport"] == 1
            release_cleanup.set()
            assert (await owner.drain(2))["ok"] is True
            assert owner.transport.pending_count == 0
        finally:
            release_cleanup.set()

    try:
        asyncio.run(scenario())
    finally:
        release_cleanup.set()
        close(owner)


def test_two_server_owners_and_the_callers_transport_context_stay_isolated():
    first, second = workers(), workers()
    previous = TrackedTransportAdmission(3)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def backend(owner, marker):
        observed = store_api.store_transport_admission.get()
        assert observed is owner.transport
        assert observed.acquire(blocking=False)
        try:
            calls.append(marker)
            if marker == "first":
                entered.set()
                assert release.wait(5)
            return {"ok": True, "marker": marker}
        finally:
            observed.release()

    one = offload_store(lambda: backend(first, "first"), "store_management_action", workers=first)
    two = offload_store(lambda: backend(second, "second"), "store_management_action", workers=second)

    async def scenario():
        token = store_api.store_transport_admission.set(previous)
        task = asyncio.create_task(one())
        try:
            await until(entered.is_set)
            assert (await two())["ok"] is True
            first.begin_drain()
            assert (await one())["meta"]["request_dispatched"] is False
            assert second.status()["accepting"] is True
            assert (await two())["ok"] is True
            assert store_api.store_transport_admission.get() is previous
            release.set()
            assert (await task)["ok"] is True
            assert (await first.drain(1))["ok"] is True
            assert second.status()["accepting"] is True
            assert first.instance_id != second.instance_id
            assert calls == ["first", "second", "second"]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            store_api.store_transport_admission.reset(token)

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        close(first)
        close(second)


def test_transport_admission_close_is_irreversible_and_release_keeps_actual_count():
    gate = TrackedTransportAdmission(2)
    assert gate.acquire(blocking=False)
    assert gate.acquire(blocking=False)
    assert gate.pending_count == 2
    assert not gate.acquire(blocking=False)
    gate.begin_drain()
    gate.begin_drain()
    assert not gate.acquire(blocking=False)
    gate.release()
    assert gate.pending_count == 1 and not gate.acquire(blocking=False)
    gate.release()
    assert gate.pending_count == 0 and not gate.acquire(blocking=False)


def test_http_worker_start_failure_releases_tracked_reservation_without_dispatch(monkeypatch):
    owner = workers()
    original_start = threading.Thread.start
    starts = []

    def start(thread, *args, **kwargs):
        if thread.name == "autostop-bounded-http":
            starts.append(thread.name)
            raise RuntimeError("synthetic owned HTTP worker could not start")
        return original_start(thread, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "start", start)
    client = store_api.StoreApiClient(
        api_url="http://127.0.0.1/inert-worker-start", read_token="synthetic-read", manage_token=""
    )
    operation = offload_store(lambda: client.search(entity="store_part"), "store_search", workers=owner)

    async def scenario():
        result = await operation()
        assert result["summary"]["error_code"] == "store_transport_unavailable"
        assert result["meta"]["request_dispatched"] is False
        assert result["meta"]["attempt_count"] == 0
        assert starts == ["autostop-bounded-http"]
        assert owner.transport.pending_count == 0
        outcome = await owner.drain(1)
        assert outcome["ok"] is True
        assert outcome["pending"] == {"read": 0, "stateful": 0, "transport": 0}

    try:
        asyncio.run(scenario())
    finally:
        close(owner)
