from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_inbox import InboxError, TelegramInbox


REVISION = "a" * 40


def _message(identifier: int, *, peer: int = 42, out: bool = False, text: str = "request", at=None, reply=0):
    message = SimpleNamespace(
        id=identifier,
        chat_id=peer,
        is_private=True,
        out=out,
        message=text,
        date=at or datetime.now(UTC),
        media=None,
        reply_to_msg_id=reply,
    )

    async def input_chat():
        return peer

    message.get_input_chat = input_chat
    return message


class Client:
    def __init__(self, messages):
        self.messages = {message.id: message for message in messages}
        self.sent = 0
        self.fail_send = False

    async def get_messages(self, _entity, *, ids=None, limit=None):
        if ids is not None:
            return self.messages.get(ids)
        rows = sorted(self.messages.values(), key=lambda message: message.id, reverse=True)
        return rows[:limit]

    async def send_message(self, entity, text, **kwargs):
        self.sent += 1
        if self.fail_send:
            raise OSError("network lost")
        identifier = max(self.messages, default=0) + 1
        result = _message(identifier, peer=entity, out=True, text=text, reply=kwargs.get("reply_to", 0))
        self.messages[identifier] = result
        return result

    async def iter_dialogs(self, *, limit, **kwargs):
        peers = sorted({message.chat_id for message in self.messages.values()})
        offset = kwargs.get("offset_peer")
        if offset:
            peers = [peer for peer in peers if peer > offset]
        for peer in peers[:limit]:
            latest = max(
                (message for message in self.messages.values() if message.chat_id == peer), key=lambda item: item.id
            )
            yield SimpleNamespace(id=peer, is_user=True, entity=peer, message=latest)

    async def iter_messages(self, entity, *, min_id, reverse, limit):
        assert reverse is True
        rows = sorted(
            (message for message in self.messages.values() if message.chat_id == entity and message.id > min_id),
            key=lambda item: item.id,
        )
        for message in rows[:limit]:
            yield message


def _runtime(tmp_path, *, max_events=10000):
    state = tmp_path / "state"
    inbox = TelegramInbox(state / "inbound.sqlite3", max_open_events=max_events)
    inbox.duty_set(True, at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
    monitor = bridge.DurableInboundMonitor(inbox, enabled=True)
    config = bridge.TelegramConfig(
        123456, "a" * 32, state_dir=state, session_path=state / "account", socket_path=tmp_path / "bridge.sock"
    )
    return config, monitor


def _job(monitor, *, running=True):
    job = monitor.inbox.claim(instruction_root="/test/snapshot", instruction_revision=REVISION)
    assert job is not None
    if running:
        job = monitor.inbox.start_intent(
            job_id=job["job_id"], lease=job["lease"], expected_revision=job["revision"], thread_id="test-thread"
        )
        job = monitor.inbox.bind_turn(
            job_id=job["job_id"], lease=job["lease"], expected_revision=job["revision"], turn_id="test-turn"
        )
    return job


async def _send(client, config, monitor, job, *, kind, text, mode="dry_run", dry=None, slot=0):
    request = {
        "job_id": job["job_id"],
        "lease": job["lease"],
        "revision": job["revision"],
        "kind": kind,
        "effect_slot": slot,
    }
    if dry:
        request["contract_token"] = dry["contract_token"]
    return await bridge._handle_send_text_to_entity(
        client,
        config,
        request,
        entity=42,
        target={"id": 42, "kind": "private"},
        text=text,
        mode=mode,
        idempotency_key=dry["idempotency_key"] if dry else "",
        reply_to_message_id=100,
        inbound_monitor=monitor,
    )


def test_committed_event_survives_without_wake_delivery(tmp_path, monkeypatch):
    config, monitor = _runtime(tmp_path)
    monkeypatch.setenv(bridge.WORK_WAKE_ENVIRONMENT, "/no/such/wake.sock")
    asyncio.run(bridge._capture_incoming_event(monitor, _message(100)))
    event = monitor.events()[0]
    before = monitor.generation
    asyncio.run(bridge._capture_incoming_event(monitor, _message(100)))
    assert monitor.generation == before
    monitor.inbox.close()
    reopened = TelegramInbox(config.state_dir / "inbound.sqlite3")
    assert reopened.resolve(event["event_id"]).message_id == 100
    assert reopened.status()["open_events"] == 1
    assert b"request" not in (config.state_dir / "inbound.sqlite3").read_bytes()
    reopened.close()


def test_acknowledgement_keeps_event_and_voice_open(tmp_path):
    config, monitor = _runtime(tmp_path)
    source = _message(100)
    monitor.record(source)
    job = _job(monitor)
    client = Client([source])
    dry = asyncio.run(_send(client, config, monitor, job, kind="ack", text="Понял, проверю"))
    applied = asyncio.run(_send(client, config, monitor, job, kind="ack", text="Понял, проверю", mode="apply", dry=dry))
    assert applied["monitor_event_closed"] is False
    ref = monitor.require_open(job["event_ids"][0])
    monitor.reserve_voice_stage(ref)
    with pytest.raises(InboxError, match="inbox_result_receipt_required"):
        monitor.inbox.result(
            job_id=job["job_id"],
            lease=job["lease"],
            expected_revision=job["revision"],
            outcome="answered",
            event_ids=job["event_ids"],
            receipt_keys=[applied["receipt_key"]],
        )
    monitor.inbox.close()


def test_exact_result_receipt_finishes_all_covered_events(tmp_path):
    config, monitor = _runtime(tmp_path)
    source = _message(100)
    monitor.record(source)
    job = _job(monitor)
    client = Client([source])
    dry = asyncio.run(_send(client, config, monitor, job, kind="result", text="Готово"))
    applied = asyncio.run(_send(client, config, monitor, job, kind="result", text="Готово", mode="apply", dry=dry))
    result = asyncio.run(
        bridge._handle_job_operation(
            client,
            config,
            {
                "operation": "job_result",
                "job_id": job["job_id"],
                "lease": job["lease"],
                "expected_revision": job["revision"],
                "outcome": "answered",
                "event_ids": job["event_ids"],
                "receipt_keys": [applied["receipt_key"]],
            },
            monitor,
        )
    )
    assert result["job"]["state"] == "completed"
    assert monitor.status()["open_events"] == 0
    assert client.sent == 1
    monitor.inbox.close()


def test_incoming_present_before_dryrun_refuses_stale_answer(tmp_path):
    config, monitor = _runtime(tmp_path)
    source = _message(100)
    monitor.record(source)
    job = _job(monitor)
    client = Client([source, _message(101, text="Actually four")])
    with pytest.raises(InboxError, match="inbox_context_stale"):
        asyncio.run(_send(client, config, monitor, job, kind="result", text="Two items"))
    assert client.sent == 0
    assert monitor.status()["open_events"] == 2
    monitor.inbox.close()


def test_lost_send_result_blocks_new_slot_and_keeps_original_key(tmp_path):
    config, monitor = _runtime(tmp_path)
    source = _message(100)
    monitor.record(source)
    job = _job(monitor)
    client = Client([source])
    dry = asyncio.run(_send(client, config, monitor, job, kind="result", text="Ready"))
    client.fail_send = True
    with pytest.raises(OSError):
        asyncio.run(_send(client, config, monitor, job, kind="result", text="Ready", mode="apply", dry=dry))
    with pytest.raises(InboxError, match="inbox_effect_outcome_unknown"):
        asyncio.run(_send(client, config, monitor, job, kind="result", text="Another attempt", slot=1))
    with pytest.raises(bridge.BridgeError, match="send_outcome_uncertain"):
        asyncio.run(_send(client, config, monitor, job, kind="result", text="Ready", mode="apply", dry=dry))
    assert client.sent == 1
    assert monitor.require_open(job["event_ids"][0])
    monitor.inbox.close()


def test_prepared_answer_can_be_replaced_after_context_refresh(tmp_path):
    config, monitor = _runtime(tmp_path)
    source = _message(100)
    monitor.record(source)
    job = _job(monitor)
    client = Client([source])
    old_dry = asyncio.run(_send(client, config, monitor, job, kind="result", text="Two items"))
    new_source = _message(101, text="Four items")
    client.messages[101] = new_source
    new_id = monitor.record(new_source)
    job = monitor.inbox.refresh_job(
        job_id=job["job_id"],
        lease=job["lease"],
        expected_revision=job["revision"],
        event_ids=[*job["event_ids"], new_id],
    )
    new_dry = asyncio.run(_send(client, config, monitor, job, kind="result", text="Four items"))
    assert new_dry["idempotency_key"] != old_dry["idempotency_key"]
    applied = asyncio.run(
        _send(client, config, monitor, job, kind="result", text="Four items", mode="apply", dry=new_dry)
    )
    finished = monitor.inbox.result(
        job_id=job["job_id"],
        lease=job["lease"],
        expected_revision=job["revision"],
        outcome="answered",
        event_ids=job["event_ids"],
        receipt_keys=[applied["receipt_key"]],
    )
    assert finished["state"] == "completed"
    assert client.sent == 1
    monitor.inbox.close()


def test_missing_ledger_cannot_replay_attempted_effect(tmp_path):
    config, monitor = _runtime(tmp_path)
    source = _message(100)
    monitor.record(source)
    job = _job(monitor)
    client = Client([source])
    dry = asyncio.run(_send(client, config, monitor, job, kind="result", text="Ready"))
    client.fail_send = True
    with pytest.raises(OSError):
        asyncio.run(_send(client, config, monitor, job, kind="result", text="Ready", mode="apply", dry=dry))
    (config.state_dir / "idempotency.json").unlink()
    client.fail_send = False
    with pytest.raises(bridge.BridgeError, match="send_outcome_uncertain"):
        asyncio.run(_send(client, config, monitor, job, kind="result", text="Ready", mode="apply", dry=dry))
    assert client.sent == 1
    current = monitor.inbox.read_job(job["job_id"])
    assert current["state"] == "needs_review"
    assert current["reason_code"] == "send_ledger_unavailable"
    monitor.inbox.close()


def test_history_respects_enabled_windows_and_off_messages(tmp_path):
    _config, monitor = _runtime(tmp_path)
    now = datetime.now(UTC)
    monitor.inbox.duty_set(False, at=(now - timedelta(minutes=30)).isoformat())
    monitor.inbox.duty_set(True, at=(now - timedelta(minutes=10)).isoformat())
    client = Client(
        [
            _message(90, at=now - timedelta(hours=2)),
            _message(100, at=now - timedelta(minutes=45)),
            _message(101, at=now - timedelta(minutes=20)),
            _message(102, at=now - timedelta(minutes=5)),
            _message(103, out=True),
        ]
    )
    asyncio.run(bridge._reconcile_incoming_history(client, monitor))
    assert [monitor.resolve(row["event_id"]).message_id for row in monitor.events()] == [100, 102]
    assert monitor.inbox.checkpoint(42) == 103
    assert monitor.inbox.health()["intake"]["state"] == "ready"
    monitor.inbox.close()


def test_capacity_keeps_checkpoint_before_missing_message(tmp_path):
    _config, monitor = _runtime(tmp_path, max_events=1)
    client = Client([_message(100), _message(101)])
    asyncio.run(bridge._reconcile_incoming_history(client, monitor))
    assert monitor.inbox.checkpoint(42) == 100
    assert monitor.status()["open_events"] == 1
    assert monitor.inbox.gaps()[0]["message_id"] == 101
    assert monitor.reconcile_requested.is_set() is False
    monitor.inbox.close()


def test_history_duty_opening_second_is_held_without_replay(tmp_path):
    at = datetime.now(UTC).replace(microsecond=700000)
    inbox = TelegramInbox(tmp_path / "state" / "inbound.sqlite3")
    inbox.configure_instructions("/test/snapshot", REVISION)
    inbox.duty_set(True, at=at.isoformat())
    monitor = bridge.DurableInboundMonitor(inbox, enabled=True)
    client = Client([_message(100, at=at.replace(microsecond=0))])
    asyncio.run(bridge._reconcile_incoming_history(client, monitor))
    assert monitor.status()["open_events"] == 1
    assert inbox.active_jobs()[0]["state"] == "needs_review"
    assert inbox.active_jobs()[0]["reason_code"] == "duty_boundary_ambiguous"
    assert inbox.claim(instruction_root="/test/snapshot", instruction_revision=REVISION) is None
    inbox.close()


def test_dialogue_cursor_resumes_past_bounded_page(tmp_path, monkeypatch):
    config, monitor = _runtime(tmp_path)
    monkeypatch.setattr(bridge, "MAX_RECONCILE_DIALOGS", 2)
    client = Client([_message(100, peer=42), _message(101, peer=43), _message(102, peer=44)])
    asyncio.run(bridge._reconcile_incoming_history(client, monitor))
    cursor = monitor.inbox.reconciliation_cursor()
    assert cursor["peer_id"] == 43
    assert monitor.status()["open_events"] == 2
    monitor.inbox.close()
    restarted = bridge.DurableInboundMonitor(TelegramInbox(config.state_dir / "inbound.sqlite3"), enabled=True)
    asyncio.run(bridge._reconcile_incoming_history(client, restarted))
    assert restarted.status()["open_events"] == 3
    assert restarted.inbox.reconciliation_cursor() is None
    assert restarted.inbox.health()["intake"]["state"] == "ready"
    restarted.inbox.close()


def test_message_budget_replays_remaining_prefix(tmp_path, monkeypatch):
    _config, monitor = _runtime(tmp_path)
    monkeypatch.setattr(bridge, "MAX_RECONCILE_MESSAGES", 2)
    client = Client([_message(100), _message(101), _message(102)])
    asyncio.run(bridge._reconcile_incoming_history(client, monitor))
    assert monitor.inbox.checkpoint(42) == 101
    assert monitor.status()["open_events"] == 2
    asyncio.run(bridge._reconcile_incoming_history(client, monitor))
    assert monitor.status()["open_events"] == 3
    assert monitor.inbox.gaps() == []
    monitor.inbox.close()


def test_subscription_actual_socket_snapshot_changes_and_denial(tmp_path, monkeypatch):
    config, monitor = _runtime(tmp_path)
    monkeypatch.setattr(bridge, "_require_root_client", lambda _writer: None)

    async def scenario():
        server = await asyncio.start_unix_server(
            lambda reader, writer: bridge._serve_client(object(), config, reader, writer, inbound_monitor=monitor),
            path=str(config.socket_path),
        )
        async with server:
            reader, writer = await asyncio.open_unix_connection(str(config.socket_path))
            writer.write(b'{"operation":"job_subscribe"}\n')
            await writer.drain()
            first = json.loads(await reader.readline())
            await bridge._capture_incoming_event(monitor, _message(100))
            second = json.loads(await asyncio.wait_for(reader.readline(), 1))
            assert set(first) == {"ok", "generation"}
            assert second["generation"] > first["generation"]
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0)
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())
    monitor.inbox.close()


def test_root_gate_rejects_nonroot_peer_credentials():
    class Connection:
        def getsockopt(self, *_args):
            import struct

            return struct.pack("3i", 1, 1001, 1001)

    writer = SimpleNamespace(get_extra_info=lambda _key: Connection())
    with pytest.raises(bridge.BridgeError, match="job_control_permission_denied"):
        bridge._require_root_client(writer)


def test_migration_import_is_atomic_and_held(tmp_path):
    config, monitor = _runtime(tmp_path)
    references = [
        {
            "event_id": "inbound-123",
            "peer_id": 42,
            "message_id": 100,
            "received_at": datetime.now(UTC).isoformat(),
            "state": "reply_verified",
        }
    ]
    response = asyncio.run(
        bridge._handle_job_operation(
            object(),
            config,
            {
                "operation": "job_import_legacy",
                "events": references,
                "instruction_root": "/test/snapshot",
                "instruction_revision": REVISION,
            },
            monitor,
        )
    )
    assert response["imported"] == 1
    assert monitor.resolve("inbound-123").message_id == 100
    assert monitor.inbox.active_jobs()[0]["state"] == "needs_review"
    assert monitor.require_open("inbound-123")
    monitor.inbox.close()


def test_owner_control_uses_existing_adapter_once_then_receipts(tmp_path, monkeypatch):
    config, monitor = _runtime(tmp_path)
    source = _message(100, text="/automations")
    monitor.record(source)
    job = _job(monitor, running=False)
    calls = []

    def handle(message):
        calls.append(message.message_id)
        return SimpleNamespace(handled=True, ok=True, reply_text="Ready")

    monitor.automation_coordinator = SimpleNamespace(_route_lock=asyncio.Lock(), adapter=SimpleNamespace(handle=handle))
    monkeypatch.setattr(bridge, "_owner_peer_id", lambda *_args, **_kwargs: 42)

    async def owner(_client, _config):
        return 42, {"id": 42, "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_owner_target", owner)
    client = Client([source])
    request = {
        "operation": "job_owner_control",
        "job_id": job["job_id"],
        "lease": job["lease"],
        "expected_revision": job["revision"],
    }
    result = asyncio.run(bridge._handle_job_operation(client, config, request, monitor))
    assert result["handled"] is True
    assert result["job"]["state"] == "completed"
    assert calls == [100]
    with pytest.raises(bridge.BridgeError, match="job_owner_control_state_invalid"):
        asyncio.run(bridge._handle_job_operation(client, config, request, monitor))
    assert calls == [100]
    monitor.inbox.close()


def test_incident_delivery_serializes_with_customer_ledger(tmp_path, monkeypatch):
    config, monitor = _runtime(tmp_path)
    source = _message(100)
    monitor.record(source)
    job = _job(monitor)
    monitor.transport_ready = True
    monitor.inbox.set_health("codex", "held", "known_failure")
    monkeypatch.setattr(bridge, "_owner_peer_id", lambda *_args, **_kwargs: 84)

    async def owner(_client, _config):
        return 84, {"id": 84, "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_owner_target", owner)

    async def scenario():
        reached_send = asyncio.Event()
        release_send = asyncio.Event()

        class PausedClient(Client):
            async def send_message(self, entity, text, **kwargs):
                if entity == 42:
                    reached_send.set()
                    await release_send.wait()
                return await super().send_message(entity, text, **kwargs)

        client = PausedClient([source])
        dry = await _send(client, config, monitor, job, kind="result", text="Ready")

        async def customer_send():
            async with bridge._MUTATION_LOCK:
                return await _send(client, config, monitor, job, kind="result", text="Ready", mode="apply", dry=dry)

        customer = asyncio.create_task(customer_send())
        await reached_send.wait()
        notification = asyncio.create_task(bridge._deliver_incident_notifications(client, config, monitor))
        await asyncio.sleep(0)
        assert notification.done() is False
        release_send.set()
        result = await customer
        await notification
        ledger = bridge._load_idempotency(config.state_dir / "idempotency.json")
        assert ledger[result["receipt_key"]]["readback_verified"] is True
        owner_entries = [entry for entry in ledger.values() if entry.get("peer_id") == 84]
        assert len(owner_entries) == 1
        assert owner_entries[0]["readback_verified"] is True
        assert monitor.inbox.pending_notifications() == []

    # A fresh loop-bound lock makes the test independent of earlier RPC loops.
    monkeypatch.setattr(bridge, "_MUTATION_LOCK", asyncio.Lock())
    asyncio.run(scenario())
    monitor.inbox.close()


def test_storage_failure_triggers_fatal_intake_signal(tmp_path, monkeypatch):
    import sqlite3

    _config, monitor = _runtime(tmp_path)

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(monitor.inbox, "record", fail)
    asyncio.run(bridge._capture_incoming_event(monitor, _message(100)))
    assert monitor.failure_event.is_set() is True
    with pytest.raises(bridge.BridgeError, match="inbound_storage_unavailable"):
        asyncio.run(bridge._await_intake_failure(monitor))
    monitor.inbox.close()


def test_reconnect_signal_preserves_original_callback_without_polling(tmp_path):
    _config, monitor = _runtime(tmp_path)
    calls = []

    async def original():
        calls.append("telethon")

    sender = SimpleNamespace(_auto_reconnect_callback=original)
    bridge._install_reconnect_signal(SimpleNamespace(_sender=sender), monitor)
    assert monitor.reconcile_requested.is_set() is False
    asyncio.run(sender._auto_reconnect_callback())
    assert calls == ["telethon"]
    assert monitor.reconcile_requested.is_set() is True
    monitor.inbox.close()


def test_events_pagination_and_legacy_file_reject_symlink(tmp_path):
    config, monitor = _runtime(tmp_path)
    for identifier in range(100, 104):
        monitor.record(_message(identifier))
    page = asyncio.run(
        bridge._handle_monitor_operation(object(), config, {"operation": "monitor_events", "limit": 2}, monitor)
    )
    assert page["has_more"] is True
    next_page = asyncio.run(
        bridge._handle_monitor_operation(
            object(),
            config,
            {"operation": "monitor_events", "limit": 2, "after_event_id": page["next_after_event_id"]},
            monitor,
        )
    )
    assert len(next_page["events"]) == 2
    assert next_page["has_more"] is False
    path = tmp_path / "metadata.json"
    path.write_text("[]")
    os.chmod(path, 0o600)
    link = tmp_path / "metadata-link.json"
    link.symlink_to(path)
    with pytest.raises(bridge.BridgeError, match="legacy_references_invalid"):
        bridge._read_legacy_references(link)
    monitor.inbox.close()
