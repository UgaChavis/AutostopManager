"""Monotonic stage observations over synthetic clocks and isolated durable jobs."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from telegram_wake_fakes import THREAD, MockServer
from test_telegram_wake_deadline_diagnostics import claimed_request
from test_telegram_wake_durable import REVISION, FakeJobs, bind_test_peer, stop_dispatcher
from test_telegram_wake_durable import bundle as bundle
from websockets.asyncio.server import unix_serve

from autostop_manager import telegram_wake as wake
from autostop_manager.telegram_inbox import TelegramInbox


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def test_fixed_aggregates_distinguish_returns_exceptions_and_cancellation():
    clock = Clock()
    timings = wake._StageTimings(clock)
    for outcome, elapsed in ((None, 2), (ValueError("private detail"), 3), (asyncio.CancelledError(), 5)):
        try:
            with timings.measure("new", "peer_prepare"):
                assert timings.snapshot()["new"]["peer_prepare"]["in_flight"] == 1
                clock.advance(elapsed)
                if outcome is not None:
                    raise outcome
        except (ValueError, asyncio.CancelledError) as exc:
            assert exc is outcome
    snapshot = timings.snapshot()
    assert snapshot["new"]["peer_prepare"] == {
        "count": 3,
        "sum_seconds": 10,
        "max_seconds": 5,
        "returned": 1,
        "raised": 1,
        "cancelled": 1,
        "in_flight": 0,
    }
    assert snapshot["observed_seconds"] == 10
    assert snapshot["recovery"]["peer_prepare"]["count"] == 0
    assert snapshot["recovery"]["peer_prepare"]["sum_seconds"] is None
    snapshot["new"]["peer_prepare"]["count"] = 999
    assert timings.snapshot()["new"]["peer_prepare"]["count"] == 3
    fresh = wake._StageTimings(clock).snapshot()
    assert fresh["observed_seconds"] == 0
    assert all(cell["count"] == 0 and cell["max_seconds"] is None for cell in fresh["new"].values())


@pytest.mark.parametrize("ended", [float("nan"), float("inf"), -1])
def test_invalid_clock_drops_observation_without_poisoning_json(ended):
    clock = Clock()
    timings = wake._StageTimings(clock)
    with timings.measure("new", "idle_wait"):
        clock.value = ended
    snapshot = timings.snapshot()
    assert snapshot["dropped_samples"] == 1 and snapshot["observed_seconds"] is None
    assert snapshot["new"]["idle_wait"]["count"] == 0
    assert snapshot["new"]["idle_wait"]["in_flight"] == 0
    json.dumps(snapshot, allow_nan=False)


def test_finite_samples_cannot_overflow_sum_or_observer_age():
    clock = Clock()
    clock.value = -1e308
    timings = wake._StageTimings(clock)
    clock.value = 0
    for _ in range(2):
        with timings.measure("new", "peer_prepare"):
            clock.value = 1e308
        clock.value = 0
    clock.value = 1e308
    snapshot = timings.snapshot()
    assert snapshot["new"]["peer_prepare"]["count"] == 1
    assert snapshot["new"]["peer_prepare"]["sum_seconds"] == 1e308
    assert snapshot["dropped_samples"] == 1 and snapshot["observed_seconds"] is None
    json.dumps(snapshot, allow_nan=False)


@pytest.mark.parametrize("fault", ["clock", "record", "unsettled"])
def test_ordinary_observer_fault_does_not_replace_execution_or_cancellation(monkeypatch, fault):
    timings = wake._StageTimings(Clock())

    def fail(*_args):
        raise RuntimeError("private observer exception")

    if fault != "unsettled":
        monkeypatch.setattr(timings, "_clock" if fault == "clock" else "_record", fail)
    for outcome in (None, ValueError("original private outcome"), asyncio.CancelledError()):
        try:
            with timings.measure("new", "finish_request", unsettled=fail if fault == "unsettled" else None):
                if outcome is not None:
                    raise outcome
        except (ValueError, asyncio.CancelledError) as exc:
            assert exc is outcome
    snapshot = timings.snapshot()
    assert snapshot["new"]["finish_request"]["in_flight"] == 0
    assert snapshot["dropped_samples"] == (1 if fault == "unsettled" else 3)
    assert "private" not in json.dumps(snapshot, allow_nan=False)


@asynccontextmanager
async def durable_request(tmp_path, bundle, server, *, finish=True):
    with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
        inbox.duty_set(True)
        inbox.record(123, 1)
        job = inbox.claim(instruction_root=str(bundle), instruction_revision=REVISION)
        jobs = FakeJobs(inbox, finish=finish)
        clock = Clock()
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            bootstrap = wake.AppServer(
                wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
            )
            dispatcher = wake.DurableWakeDispatcher(bootstrap, os.geteuid(), jobs)
            dispatcher._stage_timings = wake._StageTimings(clock)
            try:
                await bootstrap.connect()
                await bootstrap.resume(allow_active=True)
                bootstrap.ready_event.set()
                yield inbox, job, dispatcher, clock
            finally:
                await stop_dispatcher(dispatcher)


@pytest.mark.parametrize("reconnect", [False, True])
def test_new_durable_execution_measures_boundaries_without_replaying_on_reconnect(
    tmp_path, bundle, monkeypatch, reconnect
):
    async def scenario():
        server = MockServer(peer_threads=True, strict_peer_lifecycle=True, complete=not reconnect)
        server.disconnect_after_start_response = reconnect
        snapshot_observed = asyncio.Event()
        metadata = wake.AppServer._turn_metadata

        async def observed_metadata(app, turn_id=None):
            status = await metadata(app, turn_id)
            if status == "inProgress":
                snapshot_observed.set()
            return status

        monkeypatch.setattr(wake.AppServer, "_turn_metadata", observed_metadata)
        async with durable_request(tmp_path, bundle, server) as (inbox, job, dispatcher, clock):
            for owner, method, elapsed, stage in (
                (dispatcher, "_select_peer", 2, "peer_prepare"),
                (wake.AppServer, "wait_idle", 3, "idle_wait"),
                (wake.AppServer, "run_job_turn", 5, "turn_lifecycle"),
                (dispatcher, "_finish_request", 7, "finish_request"),
            ):
                original = getattr(owner, method)

                async def measured(*args, _original=original, _elapsed=elapsed, _stage=stage, **kwargs):
                    assert dispatcher.status()["stage_timings"]["new"][_stage]["in_flight"] == 1
                    clock.advance(_elapsed)
                    return await _original(*args, **kwargs)

                monkeypatch.setattr(owner, method, measured)
            health = dispatcher._health

            async def unmeasured_health(state, *args):
                if state in {"waiting_thread", "running"}:
                    clock.advance(100)
                return await health(state, *args)

            monkeypatch.setattr(dispatcher, "_health", unmeasured_health)
            executing = asyncio.create_task(dispatcher._execute(job))
            try:
                if reconnect:
                    await asyncio.wait_for(snapshot_observed.wait(), 3)
                    assert dispatcher.app.reconnect_attempts >= 1
                    assert dispatcher.status()["stage_timings"]["new"]["turn_lifecycle"]["in_flight"] == 1
                    server.turn_statuses["1"] = "completed"
                    await server.sockets[-1].send(
                        json.dumps(
                            {
                                "method": "thread/status/changed",
                                "params": {"threadId": dispatcher.app.config.thread_id, "status": {"type": "idle"}},
                            }
                        )
                    )
                await asyncio.wait_for(executing, 3)
                observed = dispatcher.status()["stage_timings"]
                for stage, elapsed in (
                    ("peer_prepare", 2),
                    ("idle_wait", 3),
                    ("turn_lifecycle", 5),
                    ("finish_request", 7),
                ):
                    cell = observed["new"][stage]
                    assert cell["count"] == cell["returned"] == 1
                    assert cell["sum_seconds"] == cell["max_seconds"] == elapsed
                    assert cell["raised"] == cell["cancelled"] == cell["in_flight"] == 0
                assert observed["new"]["finish_request"]["unsettled"] == 0
                assert observed["observed_seconds"] == 217
                assert all(cell["count"] == 0 for cell in observed["recovery"].values())
                assert observed["dropped_samples"] == 0
                stored = inbox.read_job(job["job_id"])
                assert stored["state"] == "completed" and stored["execution_proof"] == "completed"
                assert inbox.execution_slot() is None and dispatcher.request_deadline is None
                assert not dispatcher.deadline_exceeded and dispatcher.stop_proof_verified
                assert server.counter == 1
                assert server.metadata_calls == (2 if reconnect else 0) + 1
                operations = [name for name, _args in dispatcher.jobs.calls]
                assert operations.count("job_start_intent") == operations.count("job_turn_bound") == 1
                assert operations.count("job_execution_proof") == 1
                assert "job_release" not in operations
            finally:
                if not executing.done():
                    executing.cancel()
                await asyncio.gather(executing, return_exceptions=True)

    monkeypatch.setattr(wake, "RECONNECT_DELAYS", (0,))
    asyncio.run(scenario())


@pytest.mark.parametrize("terminal", ["completed", "failed", "interrupted"])
def test_known_recovery_measures_only_observed_remainder_and_preserves_terminal_proof(
    tmp_path, bundle, monkeypatch, terminal
):
    async def scenario():
        server = MockServer(peer_threads=True, complete=False)
        server.turn_statuses["known-turn"] = "inProgress"
        first_snapshot = asyncio.Event()
        metadata = wake.AppServer._turn_metadata

        async def observed_metadata(app, turn_id=None):
            status = await metadata(app, turn_id)
            if status == "inProgress":
                first_snapshot.set()
            return status

        monkeypatch.setattr(wake.AppServer, "_turn_metadata", observed_metadata)
        async with durable_request(tmp_path, bundle, server, finish=False) as (inbox, job, dispatcher, clock):
            bind_test_peer(inbox, job)
            job = inbox.start_intent(**dispatcher._guard(job), thread_id=THREAD)
            job = inbox.bind_turn(**dispatcher._guard(job), turn_id="known-turn")
            original_deadline = job["deadline_at"]
            if terminal == "completed":
                job = inbox.result(
                    **dispatcher._guard(job),
                    outcome="no_reply_needed",
                    event_ids=job["event_ids"],
                    receipt_keys=[],
                    reason_code="not_actionable",
                )
            use_thread = dispatcher._use_thread

            async def preparation(*args, **kwargs):
                clock.advance(2)
                await use_thread(*args, **kwargs)

            monkeypatch.setattr(dispatcher, "_use_thread", preparation)
            executing = asyncio.create_task(dispatcher._execute(job, recovering=True))
            try:
                await asyncio.wait_for(first_snapshot.wait(), 3)
                active = dispatcher.status()["stage_timings"]["recovery"]
                assert active["turn_lifecycle"]["in_flight"] == 1 and active["turn_lifecycle"]["count"] == 0
                assert inbox.execution_slot()["execution_proof"] is None
                clock.advance(6)
                server.turn_statuses["known-turn"] = terminal
                await server.sockets[-1].send(
                    json.dumps(
                        {"method": "thread/status/changed", "params": {"threadId": THREAD, "status": {"type": "idle"}}}
                    )
                )
                await asyncio.wait_for(executing, 3)
                observed = dispatcher.status()["stage_timings"]
                recovery = observed["recovery"]
                assert recovery["peer_prepare"]["count"] == 1 and recovery["peer_prepare"]["sum_seconds"] == 2
                assert recovery["turn_lifecycle"]["count"] == 1 and recovery["turn_lifecycle"]["sum_seconds"] == 6
                assert recovery["turn_lifecycle"]["returned" if terminal == "completed" else "raised"] == 1
                assert recovery["idle_wait"]["count"] == 0 and recovery["idle_wait"]["sum_seconds"] is None
                assert recovery["finish_request"]["returned"] == 1 and recovery["finish_request"]["unsettled"] == 0
                assert all(cell["count"] == 0 for cell in observed["new"].values())
                stored = inbox.read_job(job["job_id"])
                assert stored["deadline_at"] == original_deadline and stored["execution_proof"] == terminal
                assert (
                    stored["state"]
                    == {"completed": "completed", "failed": "needs_review", "interrupted": "needs_review"}[terminal]
                )
                if terminal != "completed":
                    assert stored["reason_code"] == "codex_turn_failed"
                assert inbox.execution_slot() is None and dispatcher.stop_proof_verified
                assert not dispatcher.deadline_exceeded and server.counter == 0 and server.metadata_calls == 3
            finally:
                if not executing.done():
                    executing.cancel()
                await asyncio.gather(executing, return_exceptions=True)

    asyncio.run(scenario())


def test_clock_failure_keeps_local_timeout_diagnostics_and_status_available(tmp_path, monkeypatch):
    async def scenario():
        with claimed_request(tmp_path, monkeypatch) as (inbox, job, app, dispatcher):

            def faulty_clock():
                raise RuntimeError("private clock payload")

            dispatcher._stage_timings = wake._StageTimings(faulty_clock)
            dispatcher._select_peer = AsyncMock(side_effect=TimeoutError("private transport payload"))
            await dispatcher._execute(job)
            stored = inbox.read_job(job["job_id"])
            assert stored["reason_code"] == "codex_transport_failed_or_unknown"
            assert stored["deadline_at"] == job["deadline_at"] and stored["execution_proof"] == "not_started"
            assert not dispatcher.deadline_exceeded and inbox.execution_slot() is None
            status = dispatcher.status()
            assert status["stage_timings"]["dropped_samples"] == 2
            assert status["stage_timings"]["observed_seconds"] is None
            assert all(cell["count"] == 0 for cell in status["stage_timings"]["new"].values())
            assert "private" not in json.dumps(status, allow_nan=False)
            app.request.assert_not_awaited()

    asyncio.run(scenario())


def test_unknown_recovery_effect_keeps_slot_and_marks_returned_finish_unsettled(tmp_path, monkeypatch):
    async def scenario():
        with claimed_request(tmp_path, monkeypatch) as (inbox, job, app, dispatcher):
            bind_test_peer(inbox, job)
            job = inbox.start_intent(**dispatcher._guard(job), thread_id=THREAD)
            job = inbox.bind_turn(**dispatcher._guard(job), turn_id="known-turn")
            key = inbox.reserve_effect(
                **dispatcher._guard(job),
                kind="result",
                text_sha256="b" * 64,
                peer_id=123,
                reply_to_message_id=1,
            )
            effect = inbox.mark_effect_intent(key)
            dispatcher._stage_timings = wake._StageTimings(Clock())
            dispatcher._use_thread = AsyncMock()
            app.connected = True
            app.ready_event.set()
            app._wait_turn = AsyncMock(side_effect=TimeoutError("private transport detail"))
            app._turn_metadata = AsyncMock(side_effect=TimeoutError("private proof detail"))
            await dispatcher._execute(job, recovering=True)
            observed = dispatcher.status()["stage_timings"]["recovery"]
            assert observed["peer_prepare"]["returned"] == observed["turn_lifecycle"]["raised"] == 1
            assert observed["finish_request"]["returned"] == observed["finish_request"]["unsettled"] == 1
            assert observed["idle_wait"]["count"] == 0
            stored = inbox.read_job(job["job_id"])
            assert stored["deadline_at"] == job["deadline_at"] and stored["execution_proof"] is None
            assert stored["reason_code"] == "codex_transport_failed_or_unknown"
            assert inbox.execution_slot()["job_id"] == job["job_id"] and inbox.read_effect(key) == effect
            assert not dispatcher.deadline_exceeded and dispatcher.terminal_stop_pending
            assert not dispatcher.stop_proof_verified
            app.request.assert_not_awaited()

    asyncio.run(scenario())


def test_finish_health_failure_remains_unsettled_even_after_verified_proof(tmp_path, bundle, monkeypatch):
    async def scenario():
        async with durable_request(tmp_path, bundle, MockServer(peer_threads=True)) as (inbox, job, dispatcher, _clock):
            original_health = dispatcher._health

            async def failed_ready(state, *args):
                if state == "ready":
                    raise OSError("private health publication failure")
                await original_health(state, *args)

            monkeypatch.setattr(dispatcher, "_health", failed_ready)
            await dispatcher._execute(job)
            cell = dispatcher.status()["stage_timings"]["new"]["finish_request"]
            assert cell["count"] == cell["returned"] == cell["unsettled"] == 1
            assert dispatcher.stop_proof_verified and dispatcher.terminal_stop_pending
            assert dispatcher.status()["queue_block_reason"] == "stop_pending"
            assert inbox.read_job(job["job_id"])["execution_proof"] == "completed"
            assert inbox.execution_slot() is None

    asyncio.run(scenario())


def test_retained_cleanup_stays_inflight_until_real_exit_and_still_needs_proof(tmp_path, monkeypatch):
    async def scenario():
        with claimed_request(tmp_path, monkeypatch) as (inbox, job, app, dispatcher):
            entered, cleanup_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
            clock = Clock()
            dispatcher._stage_timings = wake._StageTimings(clock)

            async def preparation(_job):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup_entered.set()
                    await release.wait()

            dispatcher._select_peer = preparation
            original_await = wake._await_until
            original_proof = dispatcher._proof

            async def unavailable_proof(*_args):
                clock.advance(3)
                raise TimeoutError("synthetic unavailable proof")

            async def stop_at_gate(operation, deadline, retained):
                if operation is dispatcher.execution_task:
                    await entered.wait()
                    clock.advance(5)
                    operation.cancel()
                    wake._retain_cleanup(operation, retained)
                    await cleanup_entered.wait()
                    raise TimeoutError
                return await original_await(operation, deadline, retained)

            monkeypatch.setattr(wake, "_await_until", stop_at_gate)
            monkeypatch.setattr(dispatcher, "_proof", unavailable_proof)
            try:
                await asyncio.wait_for(dispatcher._execute(job), 2)
                observed = dispatcher.status()["stage_timings"]["new"]
                assert observed["peer_prepare"]["in_flight"] == 1 and observed["peer_prepare"]["count"] == 0
                assert observed["finish_request"]["sum_seconds"] == 3 and observed["finish_request"]["unsettled"] == 1
                assert dispatcher.cleanup_pending and dispatcher.terminal_stop_pending
                assert inbox.execution_slot()["job_id"] == job["job_id"]
                assert not dispatcher.deadline_exceeded
                clock.advance(7)
                drained = asyncio.Event()
                dispatcher.execution_task.add_done_callback(lambda _task: drained.set())
                release.set()
                await asyncio.wait_for(drained.wait(), 2)
                cell = dispatcher.status()["stage_timings"]["new"]["peer_prepare"]
                assert cell["count"] == cell["cancelled"] == 1 and cell["in_flight"] == 0
                assert cell["sum_seconds"] == cell["max_seconds"] == 15
                assert not dispatcher.cleanup_pending and dispatcher.terminal_stop_pending
                assert inbox.read_job(job["job_id"])["execution_proof"] is None
                monkeypatch.setattr(dispatcher, "_proof", original_proof)
                await dispatcher.pause(persist=False)
                assert inbox.execution_slot() is None
                stored = inbox.read_job(job["job_id"])
                assert stored["execution_proof"] == "not_started" and stored["deadline_at"] == job["deadline_at"]
                assert dispatcher.stop_proof_verified and not dispatcher.terminal_stop_pending
                assert dispatcher.status()["stage_timings"]["new"]["finish_request"]["count"] == 1
                app.request.assert_not_awaited()
            finally:
                release.set()
                if dispatcher.execution_task is not None:
                    await asyncio.gather(dispatcher.execution_task, return_exceptions=True)

    asyncio.run(scenario())


def test_caller_cancellation_does_not_fabricate_finish_sample(tmp_path, monkeypatch):
    async def scenario():
        with claimed_request(tmp_path, monkeypatch) as (inbox, job, _app, dispatcher):
            entered = asyncio.Event()
            dispatcher._stage_timings = wake._StageTimings(Clock())

            async def preparation(_job):
                entered.set()
                await asyncio.Event().wait()

            dispatcher._select_peer = preparation
            task = asyncio.create_task(dispatcher._execute(job))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await asyncio.gather(dispatcher.execution_task, return_exceptions=True)
                observed = dispatcher.status()["stage_timings"]["new"]
                assert observed["peer_prepare"]["cancelled"] == 1 and observed["peer_prepare"]["in_flight"] == 0
                assert observed["finish_request"]["count"] == 0 and observed["finish_request"]["sum_seconds"] is None
                assert inbox.execution_slot()["job_id"] == job["job_id"]
                assert inbox.read_job(job["job_id"])["execution_proof"] is None
                assert dispatcher.terminal_stop_pending
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.skipif(os.geteuid() != 0, reason="local status is root-only")
def test_full_status_roundtrip_stays_private_and_within_existing_reader_limit(tmp_path, monkeypatch):
    async def scenario():
        with claimed_request(tmp_path, monkeypatch) as (_inbox, _job, app, dispatcher):
            clock = Clock()
            timings = dispatcher._stage_timings = wake._StageTimings(clock)
            for mode in timings.MODES:
                for stage in timings.STAGES:
                    with timings.measure(mode, stage, unsettled=(lambda: True) if stage == "finish_request" else None):
                        clock.advance(1200)
                    # Fill all fixed cells with realistic large aggregates, not billions of test awaits.
                    cell = timings._cells[mode][stage]
                    cell.update(
                        count=10**9,
                        sum_seconds=1e12 + 0.5,
                        max_seconds=1200.999999,
                        returned=10**9 - 3,
                        raised=1,
                        cancelled=2,
                        in_flight=1,
                    )
            dispatcher.execution_error = dispatcher.last_error = "wake_shared_backend_schema_invalid"
            dispatcher.execution_state = "waiting_thread"
            dispatcher.terminal_stop_pending = dispatcher.deadline_exceeded = True
            dispatcher.request_deadline = asyncio.get_running_loop().time() - 1
            app.recovery_state = "reconciling"
            app.last_disconnect_at = "2026-10-24T23:59:59.123456+00:00"
            app.last_disconnect_code = 1006
            app.last_rpc_error_code = -32603
            app.reconnect_attempts = 10**9
            app.active_turn = "secret-client-turn"
            app.active_event_id = "secret-client-event"
            dispatcher.current_job = {"job_id": "secret-client-job", "prompt": "secret-client-body"}
            path = str(tmp_path / "status.sock")
            async with await asyncio.start_unix_server(dispatcher.serve, path=path):
                response = await wake.local_request({"operation": "status"}, path)
            assert response["ok"] and response["queue_block_reason"] == "stop_pending"
            snapshot = response["stage_timings"]
            assert snapshot == timings.snapshot()
            assert set(snapshot) == {"scope", "observed_seconds", "dropped_samples", "new", "recovery"}
            assert snapshot["scope"] == "process"
            for mode in timings.MODES:
                assert set(snapshot[mode]) == set(timings.STAGES)
                for stage, cell in snapshot[mode].items():
                    keys = {"count", "sum_seconds", "max_seconds", "returned", "raised", "cancelled", "in_flight"}
                    assert set(cell) == keys | ({"unsettled"} if stage == "finish_request" else set())
                    assert all(type(value) in {int, float} or value is None for value in cell.values())
            serialized = json.dumps(response, allow_nan=False).encode() + b"\n"
            assert len(serialized) < 4096
            assert b"secret-client" not in serialized
            app.request.assert_not_awaited()

    asyncio.run(scenario())
