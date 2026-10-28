from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
import json
import socket
import subprocess
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_effects import EffectExecutor, EffectQueueError, rpc_budget, server_budget
from autostop_manager.telegram_inbox import InboxError


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("real network, SDK or process forbidden")

    for name in ("connect", "connect_ex", "bind"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(bridge, "_load_telethon", forbidden)
    monkeypatch.setattr(bridge, "_config_for_account", forbidden)


def test_effect_queue_has_one_active_eight_fifo_and_rejects_overflow():
    async def scenario():
        executor = EffectExecutor()
        entered, release = asyncio.Event(), asyncio.Event()
        order = []

        async def perform(number):
            order.append(number)
            if number == 0:
                entered.set()
                await release.wait()
            return number

        first = asyncio.create_task(executor.submit(lambda: perform(0), timeout=2))
        await entered.wait()
        waiting = [asyncio.create_task(executor.submit(lambda n=n: perform(n), timeout=2)) for n in range(1, 9)]
        await asyncio.sleep(0)
        assert executor.queued == 8 and order == [0]
        with pytest.raises(EffectQueueError, match="bridge_busy"):
            await executor.submit(lambda: perform(9), timeout=2)
        release.set()
        assert await asyncio.gather(first, *waiting) == list(range(9))
        assert order == list(range(9))
        await executor.close()

    asyncio.run(scenario())


def test_expired_queue_entry_never_starts_an_effect():
    async def scenario():
        executor = EffectExecutor(queue_seconds=0.02)
        entered, release = asyncio.Event(), asyncio.Event()
        performed = []

        async def slow():
            entered.set()
            await release.wait()

        async def unwanted():
            performed.append("unexpected")

        first = asyncio.create_task(executor.submit(slow, timeout=1))
        await entered.wait()
        with pytest.raises(EffectQueueError, match="bridge_queue_timeout"):
            await executor.submit(unwanted, timeout=1)
        assert not performed and executor.queued == 0
        release.set()
        await first
        assert not performed
        await executor.close()

    asyncio.run(scenario())


def test_disconnected_active_waiter_does_not_cancel_receipt_work():
    async def scenario():
        executor = EffectExecutor()
        entered, release, saved = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def send():
            entered.set()
            await release.wait()
            saved.set()

        caller = asyncio.create_task(executor.submit(send, timeout=1, job_id="job-A"))
        await entered.wait()
        caller.cancel()
        with suppress(asyncio.CancelledError):
            await caller
        assert executor.has_job_work("job-A")
        release.set()
        await asyncio.wait_for(saved.wait(), 1)
        await executor.close()

    asyncio.run(scenario())


def test_cancelled_waiter_is_removed_before_send():
    async def scenario():
        executor = EffectExecutor()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def first():
            entered.set()
            await release.wait()

        async def second():
            calls.append(1)

        active = asyncio.create_task(executor.submit(first, timeout=1))
        await entered.wait()
        waiter = asyncio.create_task(executor.submit(second, timeout=1))
        await asyncio.sleep(0)
        waiter.cancel()
        with suppress(asyncio.CancelledError):
            await waiter
        release.set()
        await active
        assert not calls
        await executor.close()

    asyncio.run(scenario())


def test_cancellation_resistant_send_holds_lane_until_actual_cleanup(monkeypatch):
    async def scenario():
        executor = EffectExecutor(cleanup_seconds=0.02)
        release, cancelled = asyncio.Event(), asyncio.Event()

        async def uncooperative():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()

        with pytest.raises(EffectQueueError, match="bridge_effect_timeout"):
            await executor.submit(uncooperative, timeout=0.02, job_id="job-A")
        assert cancelled.is_set() and executor.failed.is_set()
        assert executor.has_job_work("job-A")
        monkeypatch.setattr(bridge, "_effect_executor", lambda: executor)
        monitor = SimpleNamespace(inbox=SimpleNamespace())
        with pytest.raises(bridge.BridgeError, match="inbox_execution_effect_active"):
            bridge._job_execution_control({"operation": "job_execution_proof", "job_id": "job-A"}, monitor)
        with pytest.raises(EffectQueueError, match="bridge_effects_held"):
            await executor.submit(uncooperative, timeout=1)
        release.set()
        await executor.close()

    asyncio.run(scenario())


class _Writer:
    def __init__(self):
        self.output = bytearray()

    def write(self, data):
        self.output.extend(data)

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


def test_local_control_is_available_while_another_chat_send_is_slow(tmp_path, monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        config = bridge.TelegramConfig(123456, "0" * 32, state_dir=tmp_path, socket_path=tmp_path / "bridge.sock")

        async def operation(_client, _config, request, **_kwargs):
            calls.append(request["operation"])
            if request["operation"] == "send":
                entered.set()
                await release.wait()
            return {"ok": True, "operation": request["operation"]}

        monkeypatch.setattr(bridge, "_handle_operation", operation)
        monkeypatch.setattr(bridge, "_require_root_client", lambda _writer: None)

        async def request(payload):
            reader, writer = asyncio.StreamReader(), _Writer()
            reader.feed_data(json.dumps(payload).encode() + b"\n")
            await bridge._serve_client_request(object(), config, reader, writer)
            return json.loads(writer.output)

        sender = asyncio.create_task(request({"operation": "send", "mode": "apply", "peer": "42"}))
        await entered.wait()
        assert (await asyncio.wait_for(request({"operation": "job_status"}), 0.5))["ok"]
        assert calls == ["send", "job_status"] and not sender.done()
        release.set()
        assert (await sender)["ok"]
        await bridge._effect_executor().close()

    asyncio.run(scenario())


def test_voice_stage_and_outgoing_send_share_one_serial_lane(tmp_path, monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        order = []
        config = bridge.TelegramConfig(123456, "0" * 32, state_dir=tmp_path, socket_path=tmp_path / "bridge.sock")

        async def operation(_client, _config, payload, **_kwargs):
            order.append(payload["operation"])
            if payload["operation"] == "monitor_voice_stage":
                entered.set()
                await release.wait()
            return {"ok": True}

        monkeypatch.setattr(bridge, "_handle_operation", operation)

        async def call(payload):
            reader, writer = asyncio.StreamReader(), _Writer()
            reader.feed_data(json.dumps(payload).encode() + b"\n")
            await bridge._serve_client_request(object(), config, reader, writer)
            return json.loads(writer.output)

        stage = asyncio.create_task(call({"operation": "monitor_voice_stage", "event_id": "inbound-1"}))
        await entered.wait()
        sender = asyncio.create_task(call({"operation": "send", "mode": "apply", "peer": "84"}))
        await asyncio.sleep(0)
        assert order == ["monitor_voice_stage"]
        assert bridge._effect_executor().queued == 1
        release.set()
        assert all(item["ok"] for item in await asyncio.gather(stage, sender))
        assert order == ["monitor_voice_stage", "send"]
        await bridge._effect_executor().close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("payload", "server", "rpc"),
    [
        ({"operation": "job_status"}, 2, 5),
        ({"operation": "read"}, 20, 30),
        ({"operation": "send", "mode": "apply"}, 60, 75),
        ({"operation": "send_document", "mode": "apply"}, 150, 165),
        ({"operation": "monitor_voice_stage"}, 120, 135),
    ],
)
def test_operation_budgets_keep_rpc_margin(payload, server, rpc):
    assert server_budget(payload) == server
    assert rpc_budget(payload) == rpc


@pytest.mark.parametrize("protocol", [None, 1, True, "2"])
def test_old_worker_protocol_cannot_claim(protocol):
    async def scenario():
        def forbidden(**_kwargs):
            pytest.fail("incompatible worker reached claim")

        monitor = SimpleNamespace(inbox=SimpleNamespace(claim=forbidden), changed=lambda: None)
        with pytest.raises(bridge.BridgeError, match="worker_protocol_unsupported"):
            await bridge._handle_job_operation(
                object(), object(), {"operation": "job_claim", "worker_protocol": protocol}, monitor
            )

    asyncio.run(scenario())


def test_manual_result_cannot_release_slot_while_bridge_work_is_pending(monkeypatch):
    async def scenario():
        executor = SimpleNamespace(failed=asyncio.Event(), has_job_work=lambda _job_id: True)
        monkeypatch.setattr(bridge, "_effect_executor", lambda: executor)

        def forbidden(**_kwargs):
            pytest.fail("manual slot was released with an in-flight effect")

        monitor = SimpleNamespace(
            config=None,
            inbox=SimpleNamespace(
                read_job=lambda _job_id: {
                    "manual_recovery": True,
                    "peer_id": 42,
                    "client_control": {"guidance_pending": 0},
                },
                result=forbidden,
            ),
        )
        with pytest.raises(bridge.BridgeError, match="inbox_execution_effect_active"):
            await bridge._handle_job_operation(
                object(), object(), {"operation": "job_result", "job_id": "job-A", "receipt_keys": []}, monitor
            )

    asyncio.run(scenario())


def _job_runtime(tmp_path, monkeypatch):
    from test_telegram_bridge_durable import Client, _job, _message, _runtime

    config, monitor = _runtime(tmp_path)
    source = _message(100)
    monitor.record(source)
    job = _job(monitor)

    async def resolve(_client, peer):
        assert peer == "42"
        return 42, {"id": 42, "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)
    payload = {
        "operation": "send",
        "peer": "42",
        "text": "Synthetic reply",
        "mode": "dry_run",
        "job_id": job["job_id"],
        "lease": job["lease"],
        "revision": job["revision"],
        "kind": "result",
        "effect_slot": 0,
        "reply_to_message_id": 100,
    }
    return config, monitor, job, Client([source]), payload


def _voice_runtime(tmp_path, monkeypatch, *, manual=False):
    config, monitor, job, client, _payload = _job_runtime(tmp_path, monkeypatch)
    source = client.messages[100]
    attribute = type("DocumentAttributeAudio", (), {})()
    attribute.voice, attribute.duration = True, 8
    source.media = SimpleNamespace()
    source.document = SimpleNamespace(attributes=[attribute])
    source.file = SimpleNamespace(mime_type="audio/ogg", name="", size=len(b"OggSsynthetic"))
    monkeypatch.setattr(bridge, "_schedule_monitor_voice_stage_expiry", lambda *_args: None)
    if manual:
        arguments = {"job_id": job["job_id"], "lease": job["lease"], "expected_revision": job["revision"]}
        job = monitor.inbox.needs_review(**arguments, reason_code="synthetic_stop")
        arguments["expected_revision"] = job["revision"]
        job = monitor.inbox.finish_execution(
            **arguments,
            proof_kind="terminal",
            local_tasks_done=True,
            thread_id=job["thread_id"],
            turn_id=job["turn_id"],
            terminal_status="failed",
        )
        monitor.inbox.set_execution_paused(paused=True, expected_revision=monitor.inbox.status()["execution_revision"])
        arguments["expected_revision"] = job["revision"]
        job = monitor.inbox.resolve_job(**arguments, decision="continue_bound_turn", effects_reviewed=True)
    return config, monitor, job, client


async def _local_request(config, monitor, client, payload):
    reader, writer = asyncio.StreamReader(), _Writer()
    reader.feed_data(json.dumps(payload).encode() + b"\n")
    await bridge._serve_client_request(client, config, reader, writer, inbound_monitor=monitor)
    return json.loads(writer.output)


@pytest.mark.parametrize("manual", [False, True])
def test_event_only_voice_stage_blocks_slot_release_after_caller_disconnect(tmp_path, monkeypatch, manual):
    config, monitor, job, client = _voice_runtime(tmp_path, monkeypatch, manual=manual)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def download(_message, *, file):
            assert file is bytes
            entered.set()
            await release.wait()
            return b"OggSsynthetic"

        monkeypatch.setattr(client, "download_media", download, raising=False)
        caller = asyncio.create_task(
            _local_request(
                config, monitor, client, {"operation": "monitor_voice_stage", "event_id": job["event_ids"][0]}
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 1)
            caller.cancel()
            with suppress(asyncio.CancelledError):
                await caller
            request = {"job_id": job["job_id"], "lease": job["lease"], "expected_revision": job["revision"]}
            with pytest.raises(bridge.BridgeError, match="inbox_execution_effect_active"):
                if manual:
                    await bridge._handle_job_operation(
                        client,
                        config,
                        request
                        | {
                            "operation": "job_result",
                            "outcome": "no_reply_needed",
                            "reason_code": "duplicate",
                            "event_ids": job["event_ids"],
                            "receipt_keys": [],
                        },
                        monitor,
                    )
                else:
                    bridge._job_execution_control(
                        request
                        | {
                            "operation": "job_execution_proof",
                            "proof_kind": "terminal",
                            "local_tasks_done": True,
                            "thread_id": job["thread_id"],
                            "turn_id": job["turn_id"],
                            "terminal_status": "failed",
                        },
                        monitor,
                    )
            assert bridge._effect_executor().has_job_work(job["job_id"])
            assert monitor.inbox.execution_slot()["job_id"] == job["job_id"]
        finally:
            release.set()
            await bridge._effect_executor().close()
            if not caller.done():
                caller.cancel()
                with suppress(asyncio.CancelledError):
                    await caller

    try:
        asyncio.run(scenario())
    finally:
        monitor.inbox.close()


@pytest.mark.parametrize("operation", ["monitor_voice_stage", "monitor_read"])
def test_event_network_admission_uses_exact_job_remaining_budget(tmp_path, monkeypatch, operation):
    config, monitor, job, client = _voice_runtime(tmp_path, monkeypatch)
    now = datetime.fromisoformat(job["deadline_at"]) - timedelta(seconds=0.25)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(bridge, "datetime", Clock)
    monkeypatch.setattr(bridge, "_require_root_client", lambda _writer: None)
    budgets = []

    async def operation_stub(_client, _config, request, **_kwargs):
        assert request["job_id"] == job["job_id"]
        assert request["lease"] == job["lease"] and request["revision"] == job["revision"]
        return {"ok": True}

    # Observe the actual admission timeout, without waiting for a real deadline.
    real_timeout = asyncio.timeout

    def timeout(seconds):
        budgets.append(seconds)
        return real_timeout(seconds)

    async def submit(factory, *, timeout, job_id):
        budgets.append(timeout)
        assert job_id == job["job_id"]
        return await factory()

    monkeypatch.setattr(bridge, "_handle_operation", operation_stub)
    monkeypatch.setattr(bridge.asyncio, "timeout", timeout)
    if operation == "monitor_voice_stage":
        monkeypatch.setattr(bridge, "_effect_executor", lambda: SimpleNamespace(submit=submit))
    try:
        result = asyncio.run(
            _local_request(config, monitor, client, {"operation": operation, "event_id": job["event_ids"][0]})
        )
        assert result["ok"]
        assert 0.25 in budgets and not any(value in {20.0, 120.0} for value in budgets)
    finally:
        monitor.inbox.close()


@pytest.mark.parametrize("operation", ["job_status", "job_execution_proof", "job_effect_readback"])
def test_expired_job_preserves_local_control_and_exact_readback_budgets(tmp_path, monkeypatch, operation):
    _config, monitor, job, _client = _voice_runtime(tmp_path, monkeypatch)
    expired = datetime.fromisoformat(job["deadline_at"]) + timedelta(seconds=1)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return expired

    monkeypatch.setattr(bridge, "datetime", Clock)
    try:
        assert bridge._request_budget({"operation": operation, "job_id": job["job_id"]}, monitor) == server_budget(
            {"operation": operation}
        )
    finally:
        monitor.inbox.close()


def test_queued_voice_keeps_admission_revision_and_does_not_adopt_refresh(tmp_path, monkeypatch):
    config, monitor, job, client = _voice_runtime(tmp_path, monkeypatch)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        downloads = []

        async def busy():
            entered.set()
            await release.wait()

        async def download(_message, *, file):
            downloads.append(file)
            return b"OggSsynthetic"

        monkeypatch.setattr(client, "download_media", download, raising=False)
        active = asyncio.create_task(bridge._effect_executor().submit(busy, timeout=1))
        await entered.wait()
        caller = asyncio.create_task(
            _local_request(
                config, monitor, client, {"operation": "monitor_voice_stage", "event_id": job["event_ids"][0]}
            )
        )
        try:
            await asyncio.sleep(0)
            assert bridge._effect_executor().queued == 1
            monitor.inbox.refresh_job(job_id=job["job_id"], lease=job["lease"], expected_revision=job["revision"])
            release.set()
            await active
            response = await caller
            assert response == {"ok": False, "error": "inbox_revision_conflict"}
            assert not downloads
        finally:
            release.set()
            await bridge._effect_executor().close()

    try:
        asyncio.run(scenario())
    finally:
        monitor.inbox.close()


def test_voice_pause_after_source_read_prevents_download(tmp_path, monkeypatch):
    config, monitor, job, client = _voice_runtime(tmp_path, monkeypatch)
    original = client.get_messages
    downloads = []

    async def changed(*args, **kwargs):
        message = await original(*args, **kwargs)
        monitor.inbox.set_execution_paused(paused=True, expected_revision=monitor.inbox.status()["execution_revision"])
        return message

    async def download(_message, *, file):
        downloads.append(file)
        return b"OggSsynthetic"

    monkeypatch.setattr(client, "get_messages", changed)
    monkeypatch.setattr(client, "download_media", download, raising=False)

    async def scenario():
        try:
            result = await _local_request(
                config, monitor, client, {"operation": "monitor_voice_stage", "event_id": job["event_ids"][0]}
            )
            assert result == {"ok": False, "error": "inbox_execution_paused"}
            assert not downloads
            monitor.reserve_voice_stage(monitor.require_open(job["event_ids"][0]))
        finally:
            await bridge._effect_executor().close()

    try:
        asyncio.run(scenario())
    finally:
        monitor.inbox.close()


@pytest.mark.parametrize("operation", ["monitor_voice_stage", "monitor_read"])
def test_expired_event_network_request_does_not_start_sdk_work(tmp_path, monkeypatch, operation):
    config, monitor, job, client = _voice_runtime(tmp_path, monkeypatch)
    expired = datetime.fromisoformat(job["deadline_at"]) + timedelta(seconds=1)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return expired

    monkeypatch.setattr(bridge, "datetime", Clock)

    async def forbidden(*_args, **_kwargs):
        pytest.fail("expired event began new network work")

    monkeypatch.setattr(client, "get_messages", forbidden)
    monkeypatch.setattr(client, "download_media", forbidden, raising=False)

    async def scenario():
        try:
            result = await _local_request(
                config, monitor, client, {"operation": operation, "event_id": job["event_ids"][0]}
            )
            assert result["ok"] is False
            assert result["error"] in {"bridge_effect_timeout", "inbox_execution_deadline"}
            assert monitor.inbox.execution_slot()["job_id"] == job["job_id"]
        finally:
            await bridge._effect_executor().close()

    try:
        asyncio.run(scenario())
    finally:
        monitor.inbox.close()


def test_uncovered_same_peer_event_does_not_inherit_execution_binding(tmp_path, monkeypatch):
    from test_telegram_bridge_durable import _message

    config, monitor, _job, client = _voice_runtime(tmp_path, monkeypatch)
    event_id = monitor.record(_message(101))
    recorded = []

    async def operation(_client, _config, request, **_kwargs):
        assert "job_id" not in request and request["event_id"] == event_id
        return {"ok": True}

    async def submit(factory, *, timeout, job_id):
        recorded.append((timeout, job_id))
        return await factory()

    monkeypatch.setattr(bridge, "_handle_operation", operation)
    monkeypatch.setattr(bridge, "_effect_executor", lambda: SimpleNamespace(submit=submit))
    try:
        response = asyncio.run(
            _local_request(config, monitor, client, {"operation": "monitor_voice_stage", "event_id": event_id})
        )
        assert response["ok"] and recorded == [(120.0, None)]
    finally:
        monitor.inbox.close()


@pytest.mark.parametrize("change", ["pause", "refresh"])
def test_authority_is_rechecked_after_network_read_before_send(tmp_path, monkeypatch, change):
    config, monitor, job, client, payload = _job_runtime(tmp_path, monkeypatch)

    async def scenario():
        dry = await bridge._handle_operation(client, config, payload, inbound_monitor=monitor)
        original = client.get_messages

        async def changed(entity, *, ids=None, limit=None):
            result = await original(entity, ids=ids, limit=limit)
            if ids == 100:
                if change == "pause":
                    monitor.inbox.set_execution_paused(
                        paused=True, expected_revision=monitor.inbox.status()["execution_revision"]
                    )
                else:
                    monitor.inbox.refresh_job(
                        job_id=job["job_id"], lease=job["lease"], expected_revision=job["revision"]
                    )
            return result

        monkeypatch.setattr(client, "get_messages", changed)
        with pytest.raises(InboxError, match=r"inbox_(execution_paused|revision_conflict|execution_stale)"):
            await bridge._handle_operation(
                client,
                config,
                payload
                | {"mode": "apply", "contract_token": dry["contract_token"], "idempotency_key": dry["idempotency_key"]},
                inbound_monitor=monitor,
            )
        assert client.sent == 0 and not (config.state_dir / "idempotency.json").exists()
        assert not any(effect["attempted"] for effect in monitor.inbox.read_job(job["job_id"])["effects"])

    try:
        asyncio.run(scenario())
    finally:
        monitor.inbox.close()


def test_late_delivery_proof_survives_pause_and_stale_job_revision(tmp_path, monkeypatch):
    config, monitor, job, client, payload = _job_runtime(tmp_path, monkeypatch)

    async def scenario():
        dry = await bridge._handle_operation(client, config, payload, inbound_monitor=monitor)
        original = client.send_message

        async def late(entity, text, **kwargs):
            sent = await original(entity, text, **kwargs)
            monitor.inbox.set_execution_paused(
                paused=True, expected_revision=monitor.inbox.status()["execution_revision"]
            )
            monitor.inbox.needs_review(
                job_id=job["job_id"],
                lease=job["lease"],
                expected_revision=job["revision"],
                reason_code="synthetic_pause",
            )
            return sent

        monkeypatch.setattr(client, "send_message", late)
        proof = await bridge._handle_operation(
            client,
            config,
            payload
            | {"mode": "apply", "contract_token": dry["contract_token"], "idempotency_key": dry["idempotency_key"]},
            inbound_monitor=monitor,
        )
        entry = bridge._load_idempotency(config.state_dir / "idempotency.json")[dry["idempotency_key"]]
        assert entry["message_id"] == proof["message_id"] == 101 and entry["readback_verified"]
        assert monitor.inbox.read_effect(dry["idempotency_key"])["verified"]
        assert monitor.inbox.read_job(job["job_id"])["state"] == "needs_review"
        assert monitor.status()["open_events"] == 1 and client.sent == 1

    try:
        asyncio.run(scenario())
    finally:
        monitor.inbox.close()


def test_receipt_merge_preserves_another_entry_written_during_readback(tmp_path, monkeypatch):
    config, monitor, _job, client, payload = _job_runtime(tmp_path, monkeypatch)

    async def scenario():
        dry = await bridge._handle_operation(client, config, payload, inbound_monitor=monitor)
        original = client.get_messages
        path = config.state_dir / "idempotency.json"

        async def concurrent(entity, *, ids=None, limit=None):
            result = await original(entity, ids=ids, limit=limit)
            if ids == 101:
                latest = bridge._load_idempotency(path)
                latest["independent-key"] = {"operation": "send_text", "message_id": 202, "peer_id": 84}
                bridge._save_idempotency(path, latest)
            return result

        monkeypatch.setattr(client, "get_messages", concurrent)
        proof = await bridge._handle_operation(
            client,
            config,
            payload
            | {"mode": "apply", "contract_token": dry["contract_token"], "idempotency_key": dry["idempotency_key"]},
            inbound_monitor=monitor,
        )
        latest = bridge._load_idempotency(path)
        assert latest["independent-key"]["message_id"] == 202
        assert latest[dry["idempotency_key"]]["readback_verified"] and proof["verified"]

    try:
        asyncio.run(scenario())
    finally:
        monitor.inbox.close()


def test_subscription_heartbeats_do_not_manufacture_generation(monkeypatch):
    async def scenario():
        monkeypatch.setattr(bridge, "JOB_SUBSCRIPTION_HEARTBEAT_SECONDS", 0.02)
        reader = asyncio.StreamReader()
        frames = []

        class Writer(_Writer):
            def write(self, data):
                frames.append(json.loads(data))
                if len(frames) == 2:
                    reader.feed_eof()

        monitor = SimpleNamespace(subscribers=set(), generation=17)
        await asyncio.wait_for(bridge._serve_job_subscription(reader, Writer(), monitor, heartbeat=True), 1)
        assert frames[:2] == [
            {"ok": True, "generation": 17, "heartbeat": False},
            {"ok": True, "generation": 17, "heartbeat": True},
        ]
        assert not monitor.subscribers

    asyncio.run(scenario())


def test_local_rpc_has_one_deadline_even_if_every_read_makes_progress(tmp_path, monkeypatch):
    elapsed = [0.0]

    class Socket:
        reads = 0
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

        def settimeout(self, remaining):
            assert 0 < remaining <= 1

        def connect(self, _path):
            pass

        def sendall(self, _content):
            elapsed[0] += 0.4

        def recv(self, _size):
            self.reads += 1
            elapsed[0] += 0.4
            return b"x"

    connection = Socket()
    monkeypatch.setattr(bridge.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(bridge.socket, "socket", lambda *_args, **_kwargs: connection)
    with pytest.raises(bridge.BridgeError, match="bridge_unavailable"):
        bridge.send_local_request(tmp_path / "bridge.sock", {"operation": "read"}, timeout_seconds=1)
    assert connection.closed and connection.reads == 2
