"""Exercise Bridge latency at the real history, effect and terminal operations."""

import asyncio

import pytest
from test_telegram_bridge_durable import Client, _job, _message, _runtime, _send

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_inbox import InboxError
from autostop_manager.telegram_timings import StageTimings


class Clock:
    def __init__(self):
        self.seconds = 0
        self.broken = False

    def __call__(self):
        if self.broken:
            raise RuntimeError("synthetic clock failure")
        return self.seconds


class TimedClient(Client):
    def __init__(self, clock, messages):
        super().__init__(messages)
        self.clock = clock
        self.failure = None

    async def get_messages(self, entity, *, ids=None, limit=None, offset_id=None):
        result = await super().get_messages(entity, ids=ids, limit=limit, offset_id=offset_id)
        if limit == 2:
            self.clock.seconds += 2
        if type(ids) is int and result is not None and result.out:
            self.clock.seconds += 5
            if self.failure == "readback":
                raise OSError("synthetic readback failure")
        return result

    async def send_message(self, entity, text, **kwargs):
        self.clock.seconds += 3
        result = await super().send_message(entity, text, **kwargs)
        if self.failure == "send":
            raise OSError("synthetic response loss")
        if self.failure == "cancel":
            raise asyncio.CancelledError
        return result


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(bridge, "StageTimings", lambda **options: StageTimings(clock, **options))
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")

    async def resolve(_client, peer):
        return int(peer), {"id": int(peer), "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)
    config, monitor = _runtime(tmp_path)
    try:
        source = _message(100)
        monitor.record(source)
        yield config, monitor, _job(monitor), TimedClient(clock, [source]), clock
    finally:
        monitor.inbox.close()


def _binding(job):
    return {"job_id": job["job_id"], "lease": job["lease"], "expected_revision": job["revision"]}


async def _request(runtime, operation, **parameters):
    config, monitor, _job, client, _clock = runtime
    return await bridge._handle_operation(
        client, config, {"operation": operation, **parameters}, inbound_monitor=monitor
    )


async def _status(runtime):
    return (await _request(runtime, "job_status"))["status"]["job_latency"]


def test_job_history_times_each_page_and_first_confirmation(runtime):
    _config, _monitor, job, client, clock = runtime
    client.messages[99] = _message(99)
    read = _binding(job)
    read["revision"] = read.pop("expected_revision")
    read.update(peer=str(job["peer_id"]), limit=1)

    async def scenario():
        await _request(runtime, "read", peer=read["peer"], limit=1)
        page = await _request(runtime, "read", **read)
        assert page["has_more"] and (await _status(runtime))["new"]["history"]["count"] == 0
        clock.seconds += 7
        with pytest.raises(bridge.BridgeError, match="history_sequence_invalid"):
            await _request(runtime, "read", **read, before_message_id=page["next_before_message_id"])
        page = await _request(
            runtime,
            "read",
            **read,
            before_message_id=page["next_before_message_id"],
            history_receipt=page["history_receipt"],
        )
        assert not page["has_more"]
        clock.seconds += 11
        result = await _request(
            runtime, "job_history_confirm", **_binding(job), history_receipt=page["history_receipt"]
        )
        assert result["history_pages"] == 2
        await _request(runtime, "job_history_confirm", **_binding(job), history_receipt=page["history_receipt"])
        stages = (await _status(runtime))["new"]
        assert stages["history"]["returned"] == 1 and stages["history"]["sum_seconds"] == 22
        assert stages["history_page"]["returned"] == 2 and stages["history_page"]["sum_seconds"] == 4

    asyncio.run(scenario())


def test_history_confirmation_cannot_use_previous_context_timing(runtime):
    _config, monitor, job, _client, clock = runtime

    async def page(current):
        return await _request(
            runtime,
            "read",
            job_id=current["job_id"],
            lease=current["lease"],
            revision=current["revision"],
            peer=str(current["peer_id"]),
            limit=1,
        )

    async def scenario():
        previous = await page(job)
        monitor.inbox.invalidate_context(job["peer_id"], message_ids=[100])
        with pytest.raises(InboxError, match="inbox_context_stale"):
            await _request(runtime, "job_history_confirm", **_binding(job), history_receipt=previous["history_receipt"])
        assert (await _status(runtime))["new"]["history"]["count"] == 0
        refreshed = monitor.inbox.refresh_job(**_binding(job), event_ids=job["event_ids"])
        clock.seconds += 13
        current = await page(refreshed)
        clock.seconds += 3
        await _request(
            runtime, "job_history_confirm", **_binding(refreshed), history_receipt=current["history_receipt"]
        )
        stages = (await _status(runtime))["new"]
        assert stages["history"]["returned"] == 1 and stages["history"]["sum_seconds"] == 5
        assert stages["history_page"]["returned"] == 2

    asyncio.run(scenario())


def test_job_result_times_physical_send_receipt_and_terminal_proof_only_once(runtime):
    config, monitor, job, client, clock = runtime

    async def scenario():
        ack = await _send(client, config, monitor, job, kind="ack", text="Received")
        await _send(client, config, monitor, job, kind="ack", text="Received", mode="apply", dry=ack)
        draft = await _send(client, config, monitor, job, kind="result", text="Ready")
        receipt = await _send(client, config, monitor, job, kind="result", text="Ready", mode="apply", dry=draft)
        clock.seconds += 7
        replay = await _send(client, config, monitor, job, kind="result", text="Ready", mode="apply", dry=draft)
        assert replay["replayed"] and replay["receipt_key"] == receipt["receipt_key"] and client.sent == 2
        clock.seconds += 11
        result = await _request(
            runtime,
            "job_result",
            **_binding(job),
            outcome="answered",
            event_ids=job["event_ids"],
            receipt_keys=[receipt["receipt_key"]],
        )
        assert (await _status(runtime))["new"]["reply_to_terminal"]["count"] == 0
        clock.seconds += 13
        current = result["job"]
        proof = {
            "proof_kind": "terminal",
            "thread_id": current["thread_id"],
            "turn_id": current["turn_id"],
            "terminal_status": "completed",
            "local_tasks_done": True,
        }
        await _request(runtime, "job_execution_proof", **_binding(current), **proof)
        await _request(runtime, "job_execution_proof", **_binding(current), **proof)
        assert not monitor.inbox.execution_slots()
        stages = (await _status(runtime))["new"]
        assert stages["send"]["returned"] == 1 and stages["send"]["sum_seconds"] == 3
        assert stages["readback"]["returned"] == 1 and stages["readback"]["sum_seconds"] == 5
        assert stages["reply_to_terminal"]["returned"] == 1 and stages["reply_to_terminal"]["sum_seconds"] == 36

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["send", "cancel", "readback", "clock"])
def test_latency_does_not_change_send_and_readback_outcomes(runtime, failure):
    config, monitor, job, client, clock = runtime

    async def scenario():
        draft = await _send(client, config, monitor, job, kind="result", text="Ready")
        client.failure = failure
        if failure == "clock":
            clock.broken = True
            result = await _send(client, config, monitor, job, kind="result", text="Ready", mode="apply", dry=draft)
            assert result["verified"] and result["receipt_key"] == draft["idempotency_key"]
            clock.broken = False
        else:
            exception = asyncio.CancelledError if failure == "cancel" else OSError
            with pytest.raises(exception):
                await _send(client, config, monitor, job, kind="result", text="Ready", mode="apply", dry=draft)
        key = draft["idempotency_key"]
        effect = monitor.inbox.read_effect(key)
        assert client.sent == effect["attempted"] == 1 and effect["verified"] == (failure == "clock")
        snapshot = await _status(runtime)
        stages = snapshot["new"]
        assert stages["reply_to_terminal"]["count"] == 0
        assert stages["send"]["in_flight"] == stages["readback"]["in_flight"] == 0
        if failure == "clock":
            assert snapshot["dropped_samples"] == 2 and stages["send"]["count"] == stages["readback"]["count"] == 0
        else:
            assert snapshot["dropped_samples"] == 0
            assert stages["send"][{"cancel": "cancelled", "send": "raised", "readback": "returned"}[failure]] == 1
            assert stages["readback"]["raised"] == (failure == "readback")
        if failure in {"send", "cancel"}:
            with pytest.raises(bridge.BridgeError, match="send_outcome_uncertain"):
                await _send(client, config, monitor, job, kind="result", text="Ready", mode="apply", dry=draft)
            assert client.sent == 1 and (await _status(runtime)) == snapshot

    asyncio.run(scenario())
