"""Finite history transport recovery uses real inbox state and synthetic clients."""

from __future__ import annotations

import asyncio
import errno
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from test_telegram_bridge_durable import Client, _job, _message, _runtime

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_inbox import InboxError


class HistoryClient(Client):
    def __init__(self, messages, failures=()):
        super().__init__(messages)
        self.failures = list(failures)
        self.history_attempts = 0

    async def iter_messages(self, entity, *, min_id, reverse, limit):
        self.history_attempts += 1
        error = self.failures.pop(0) if self.failures else None
        if error is not None:
            raise error
        async for message in super().iter_messages(entity, min_id=min_id, reverse=reverse, limit=limit):
            yield message


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    config, monitor = _runtime(tmp_path, max_events=1)
    monitor.transport_ready = True
    phases = asyncio.Queue()
    original_notifications = bridge._deliver_incident_notifications
    monkeypatch.setattr(bridge, "_load_reconciliation_state_request", object)
    monkeypatch.setattr(bridge, "_owner_peer_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bridge, "_MUTATION_LOCK", asyncio.Lock())

    async def completed_iteration(*args, **kwargs):
        progress = await original_notifications(*args, **kwargs)
        phases.put_nowait(
            {
                "health": monitor.inbox.health().get("intake"),
                "requested": monitor.reconcile_requested.is_set(),
                "stopped": monitor.reconciliation_stopped,
                "retries": monitor.reconciliation_transport_retries,
                "fence_restarts": monitor.reconciliation_restarts,
                "windows": monitor.inbox.duty_windows(),
                "events": monitor.inbox.events(),
                "checkpoint": monitor.inbox.checkpoint(42),
            }
        )
        return progress

    monkeypatch.setattr(bridge, "_deliver_incident_notifications", completed_iteration)
    try:
        yield config, monitor, phases
    finally:
        monitor.inbox.close()


@asynccontextmanager
async def running_loop(client, config, monitor):
    task = asyncio.create_task(bridge._reconcile_incoming_loop(client, config, monitor))
    try:
        yield task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge._effect_executor().close()


async def phase(phases):
    return await asyncio.wait_for(phases.get(), 2)


async def finish_first(client, config, monitor):
    job = _job(monitor)
    done = (
        await bridge._handle_job_operation(
            client,
            config,
            {
                "operation": "job_result",
                "job_id": job["job_id"],
                "lease": job["lease"],
                "expected_revision": job["revision"],
                "outcome": "no_reply_needed",
                "reason_code": "not_actionable",
                "event_ids": job["event_ids"],
                "receipt_keys": [],
            },
            monitor,
        )
    )["job"]
    proof = await bridge._handle_job_control(
        client,
        config,
        {
            "operation": "job_execution_proof",
            "job_id": done["job_id"],
            "lease": done["lease"],
            "expected_revision": done["revision"],
            "proof_kind": "terminal",
            "local_tasks_done": True,
            "thread_id": done["thread_id"],
            "turn_id": done["turn_id"],
            "terminal_status": "completed",
        },
        monitor,
    )
    return proof["job"]


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError("synthetic transport failure"),
        TimeoutError(),
        OSError(errno.ENETUNREACH, "synthetic network"),
    ],
)
def test_capacity_result_recovers_after_one_transient_without_new_updates(runtime, error):
    config, monitor, phases = runtime

    async def scenario():
        source, missing = _message(100), _message(101)
        client = HistoryClient([source, missing])
        await bridge._capture_incoming_event(monitor, source)
        await bridge._capture_incoming_event(monitor, missing)
        first_ref = monitor.events()[0]["event_id"]
        monitor.reconcile_requested.clear()
        await bridge._reconcile_incoming_history(client, monitor)
        assert monitor.inbox.checkpoint(42) == 100 and monitor.inbox.gaps()[0]["message_id"] == 101
        done = await finish_first(client, config, monitor)
        assert done["execution_proof"] == "completed" and monitor.inbox.execution_slot() is None
        assert monitor.inbox.status()["open_events"] == 0 and monitor.reconcile_requested.is_set()
        client.failures = [error]
        async with running_loop(client, config, monitor):
            failed = await phase(phases)
            recovered = await phase(phases)
            assert failed["health"]["error_code"] == "history_reconciliation_failed"
            assert failed["requested"] and failed["retries"] == 1
            assert recovered["health"] == {"state": "ready", "error_code": None}
            assert not recovered["requested"] and not recovered["stopped"]
            assert client.history_attempts == 3 and client.sent == 0
            assert monitor.inbox.gaps() == [] and monitor.inbox.checkpoint(42) == 101
            assert monitor.inbox.event_state(first_ref) == "no_reply_needed"
            refs = [monitor.resolve(row["event_id"]) for row in monitor.events() if row["state"] == "open"]
            assert len(refs) == 1 and refs[0].message_id == 101
            assert _job(monitor, running=False)["event_ids"] == ["inbound-" + str(refs[0].sequence)]

    asyncio.run(scenario())


def test_exhausted_retries_stop_until_a_new_valid_update_grants_fresh_recovery(runtime):
    config, monitor, phases = runtime

    async def scenario():
        client = HistoryClient([_message(100)], [ConnectionResetError() for _ in range(6)])
        monitor.request_reconciliation()
        async with running_loop(client, config, monitor):
            observations = [await phase(phases) for _ in range(3)]
            assert client.history_attempts == 3
            assert [row["retries"] for row in observations] == [1, 2, 2]
            assert observations[-1]["stopped"] and not observations[-1]["requested"]
            assert monitor.inbox.events() == [] and monitor.inbox.checkpoint(42) == 0
            assert monitor.inbox.health()["intake"]["error_code"] == "history_reconciliation_failed"
            client.failures.clear()
            outgoing = _message(101, out=True)
            client.messages[101] = outgoing
            await bridge._capture_incoming_event(monitor, outgoing)
            recovered = await phase(phases)
            assert recovered["health"] == {"state": "ready", "error_code": None}
            assert recovered["retries"] == 0 and not recovered["stopped"]
            assert client.history_attempts == 4 and monitor.inbox.checkpoint(42) == 101
            assert len(monitor.events()) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error",
    [
        PermissionError(errno.EACCES, "synthetic permission failure"),
        FileNotFoundError(errno.ENOENT, "synthetic missing file"),
        OSError(errno.ENOSPC, "synthetic storage failure"),
        OSError("unclassified OSError"),
        InboxError("inbox_storage_failed"),
        bridge.BridgeError("telegram_reconciliation_contract_unavailable"),
        bridge.BridgeError("history_state_invalid"),
        bridge.BridgeError("auth_key_unregistered"),
        bridge.BridgeError("flood_wait"),
    ],
)
def test_permanent_or_unclassified_failures_never_retry(runtime, error):
    config, monitor, phases = runtime

    async def scenario():
        client = HistoryClient([_message(100)], [error])
        monitor.request_reconciliation()
        async with running_loop(client, config, monitor) as task:
            failed = await phase(phases)
            assert client.history_attempts == 1 and not task.done()
            assert not failed["requested"] and not failed["stopped"] and failed["retries"] == 0
            assert failed["health"]["error_code"] == "history_reconciliation_failed"
            assert monitor.inbox.events() == [] and monitor.inbox.checkpoint(42) == 0

    asyncio.run(scenario())


def test_cancellation_during_retry_starts_no_further_history_work(runtime):
    config, monitor, phases = runtime
    entered = asyncio.Event()

    class BlockRetry(HistoryClient):
        async def iter_messages(self, entity, *, min_id, reverse, limit):
            if self.history_attempts == 1:
                self.history_attempts += 1
                entered.set()
                await asyncio.Event().wait()
            async for message in super().iter_messages(entity, min_id=min_id, reverse=reverse, limit=limit):
                yield message

    async def scenario():
        client = BlockRetry([_message(100)], [ConnectionResetError()])
        monitor.request_reconciliation()
        async with running_loop(client, config, monitor) as task:
            await phase(phases)
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert task.cancelled() and client.history_attempts == 2
            assert monitor.reconciliation_transport_retries == 1 and not monitor.reconcile_requested.is_set()
            assert monitor.inbox.events() == [] and monitor.inbox.checkpoint(42) == 0

    asyncio.run(scenario())


def test_duty_off_during_failed_read_prevents_retry(runtime):
    config, monitor, phases = runtime

    class DisableDuty(HistoryClient):
        async def iter_messages(self, entity, *, min_id, reverse, limit):
            self.history_attempts += 1
            await bridge._handle_job_control(self, config, {"operation": "duty_set", "enabled": False}, monitor)
            raise TimeoutError
            yield  # pragma: no cover - this mock implements the async iterator contract.

    async def scenario():
        client = DisableDuty([_message(100)])
        monitor.request_reconciliation()
        async with running_loop(client, config, monitor):
            failed = await phase(phases)
            assert client.history_attempts == 1 and not monitor.capture_enabled
            assert failed["retries"] == 0 and not failed["requested"]
            assert monitor.inbox.events() == []

    asyncio.run(scenario())


def test_new_external_signal_during_failure_does_not_spend_its_retry_budget(runtime):
    config, monitor, phases = runtime
    entered, release = asyncio.Event(), asyncio.Event()

    class NewSignalClient(HistoryClient):
        async def iter_messages(self, entity, *, min_id, reverse, limit):
            self.history_attempts += 1
            if self.history_attempts == 1:
                entered.set()
                await release.wait()
            raise ConnectionResetError
            yield  # pragma: no cover - this mock implements the async iterator contract.

    async def scenario():
        client = NewSignalClient([_message(100)])
        monitor.request_reconciliation()
        async with running_loop(client, config, monitor):
            await asyncio.wait_for(entered.wait(), 2)
            monitor.request_reconciliation()
            release.set()
            observations = [await phase(phases) for _ in range(4)]
            assert [row["retries"] for row in observations] == [0, 1, 2, 2]
            assert client.history_attempts == 4
            assert observations[-1]["stopped"] and not observations[-1]["requested"]

    asyncio.run(scenario())


def test_partial_replay_keeps_identity_checkpoint_and_closed_window_until_verified_complete(runtime):
    config, monitor, phases = runtime
    now = datetime.now(UTC).replace(microsecond=0)
    monitor.inbox._max_open_events = 3
    monitor.inbox.duty_set(False, at=(now - timedelta(minutes=30)).isoformat())
    monitor.inbox.duty_set(True, at=(now - timedelta(minutes=20)).isoformat())

    class PrefixFailure(HistoryClient):
        async def iter_messages(self, entity, *, min_id, reverse, limit):
            self.history_attempts += 1
            async for message in Client.iter_messages(self, entity, min_id=min_id, reverse=reverse, limit=limit):
                yield message
                if self.history_attempts == 1:
                    raise ConnectionResetError

    async def scenario():
        client = PrefixFailure(
            [_message(100, at=now - timedelta(minutes=40)), _message(101, at=now - timedelta(minutes=10))]
        )
        monitor.request_reconciliation()
        async with running_loop(client, config, monitor):
            failed = await phase(phases)
            recovered = await phase(phases)
            assert failed["checkpoint"] == 0 and failed["windows"][0]["recovered_at"] is None
            assert len(failed["events"]) == 1
            assert recovered["health"] == {"state": "ready", "error_code": None}
            refs = monitor.events()
            assert len(refs) == 2 and refs[0]["event_id"] == failed["events"][0]["event_id"]
            assert [monitor.resolve(row["event_id"]).message_id for row in refs] == [100, 101]
            assert monitor.inbox.checkpoint(42) == 101
            assert monitor.inbox.duty_windows()[0]["recovered_at"] is not None

    asyncio.run(scenario())


def test_internal_retry_does_not_reset_the_existing_unstable_fence_budget(runtime):
    config, monitor, phases = runtime

    class ChangingClient(HistoryClient):
        async def __call__(self, request):
            self.update_revision += 1
            return await super().__call__(request)

    async def scenario():
        client = ChangingClient([_message(100)], [ConnectionResetError()])
        monitor.request_reconciliation()
        monitor.reconciliation_restarts = 1
        async with running_loop(client, config, monitor):
            failed = await phase(phases)
            unstable = await phase(phases)
            assert failed["fence_restarts"] == unstable["fence_restarts"] == 1
            assert unstable["health"]["error_code"] == "history_reconciliation_changed"
            assert unstable["stopped"] and not unstable["requested"]
            assert client.history_attempts == 2

    asyncio.run(scenario())


def test_pagination_does_not_refresh_the_transport_retry_budget(runtime, monkeypatch):
    config, monitor, phases = runtime
    monitor.inbox._max_open_events = 4
    monkeypatch.setattr(bridge, "MAX_RECONCILE_MESSAGES", 1)

    async def scenario():
        client = HistoryClient(
            [_message(100), _message(101), _message(102)],
            [ConnectionResetError(), None, ConnectionResetError(), None, ConnectionResetError()],
        )
        monitor.request_reconciliation()
        async with running_loop(client, config, monitor):
            observations = [await phase(phases) for _ in range(5)]
            assert [row["retries"] for row in observations] == [1, 1, 2, 2, 2]
            assert client.history_attempts == 5
            assert observations[-1]["stopped"] and not observations[-1]["requested"]
            assert monitor.inbox.checkpoint(42) == 101 and len(monitor.events()) == 2

    asyncio.run(scenario())
