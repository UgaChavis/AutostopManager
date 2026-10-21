from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_telegram_bridge import _minimal_pdf

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_inbox import InboxError, TelegramInbox


REVISION = "a" * 40


@pytest.fixture(autouse=True)
def synthetic_state_request(monkeypatch):
    monkeypatch.setattr(bridge, "_load_reconciliation_state_request", object)


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
        self.update_revision = 1

    async def __call__(self, _request):
        return SimpleNamespace(pts=self.update_revision, qts=0, seq=self.update_revision)

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
        self.update_revision += 1
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


class DateOrderedClient(Client):
    def __init__(self, messages):
        super().__init__(messages)
        self.dialog_calls = []

    async def iter_dialogs(self, *, limit, **kwargs):
        latest = {
            peer: max(
                (message for message in self.messages.values() if message.chat_id == peer), key=lambda row: row.id
            )
            for peer in {message.chat_id for message in self.messages.values()}
        }
        peers = sorted(latest, key=lambda peer: (latest[peer].date, latest[peer].id), reverse=True)
        if kwargs.get("offset_date") is not None:
            peers = [
                peer
                for peer in peers
                if (latest[peer].date, latest[peer].id) < (kwargs["offset_date"], kwargs["offset_id"])
            ]
        self.dialog_calls.append(peers[:limit])
        for peer in peers[:limit]:
            yield SimpleNamespace(id=peer, is_user=True, entity=peer, message=latest[peer])


class MediaClient(Client):
    async def send_file(self, entity, upload, *, caption, **kwargs):
        content = upload.read()
        message = await self.send_message(entity, caption, reply_to=kwargs.get("reply_to", 0))
        message.media = SimpleNamespace()
        message.file = SimpleNamespace(
            name=upload.name,
            mime_type="application/pdf" if kwargs.get("force_document") else "image/jpeg",
            size=len(content),
        )
        message.document = SimpleNamespace(attributes=[])
        message.synthetic_content = content
        return message

    async def download_media(self, message, *, file):
        assert file is bytes
        return message.synthetic_content


class MarkdownDefaultClient(MediaClient):
    """Emulate the SDK's default rendering unless explicitly disabled."""

    @staticmethod
    def rendered(text, kwargs):
        return text if kwargs.get("parse_mode", "markdown") is None else text.strip().replace("**", "").replace("_", "")

    async def send_message(self, entity, text, **kwargs):
        return await Client.send_message(self, entity, self.rendered(text, kwargs), **kwargs)

    async def send_file(self, entity, upload, *, caption, **kwargs):
        content = upload.read()
        message = await Client.send_message(
            self, entity, self.rendered(caption, kwargs), reply_to=kwargs.get("reply_to", 0)
        )
        message.media = SimpleNamespace()
        message.file = SimpleNamespace(
            name=upload.name,
            mime_type="application/pdf" if kwargs.get("force_document") else "image/jpeg",
            size=len(content),
        )
        message.document = SimpleNamespace(attributes=[])
        message.synthetic_content = content
        return message


class ReadbackClient(MediaClient):
    def __init__(self, messages):
        super().__init__(messages)
        self.fail_readback = False
        self.lose_text_response = False
        self.lose_file_response = False
        self.downloads = 0
        self.reads = []

    async def get_messages(self, entity, *, ids=None, limit=None):
        self.reads.append((entity, ids))
        message = await super().get_messages(entity, ids=ids, limit=limit)
        if ids is not None and message is not None and message.out and self.fail_readback:
            raise OSError("synthetic readback loss")
        return message

    async def send_message(self, entity, text, **kwargs):
        message = await super().send_message(entity, text, **kwargs)
        if self.lose_text_response:
            raise OSError("synthetic response loss")
        return message

    async def send_file(self, entity, upload, **kwargs):
        message = await super().send_file(entity, upload, **kwargs)
        if self.lose_file_response:
            raise OSError("synthetic response loss")
        return message

    async def download_media(self, message, *, file):
        self.downloads += 1
        return await super().download_media(message, file=file)

    async def iter_messages(self, *_args, **_kwargs):
        raise AssertionError("effect readback must not scan history")
        yield


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


async def _readback(client, config, monitor, job, effect_key, **params):
    return await bridge._handle_operation(
        client,
        config,
        {
            "operation": "job_effect_readback",
            "job_id": job["job_id"],
            "lease": job["lease"],
            "expected_revision": job["revision"],
            "effect_key": effect_key,
        }
        | params,
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


def test_monitor_target_rejects_completed_event_before_reading_telegram(tmp_path, monkeypatch):
    config, monitor = _runtime(tmp_path)
    source = _message(100)
    monitor.record(source)
    job = _job(monitor)
    monitor.inbox.result(
        job_id=job["job_id"],
        lease=job["lease"],
        expected_revision=job["revision"],
        outcome="no_reply_needed",
        event_ids=job["event_ids"],
        receipt_keys=[],
        reason_code="not_actionable",
    )

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("closed target must not read Telegram")

    monkeypatch.setattr(bridge, "_load_inbound_monitor_context_messages", unexpected)
    monkeypatch.setattr(bridge, "_resolve_peer", unexpected)
    try:
        with pytest.raises(bridge.BridgeError, match=r"^inbound_event_not_open$"):
            asyncio.run(
                bridge._handle_operation(
                    object(),
                    config,
                    {"operation": "monitor_target", "event_id": job["event_ids"][0]},
                    inbound_monitor=monitor,
                )
            )
    finally:
        monitor.inbox.close()


def test_monitor_target_rechecks_event_after_awaited_resolution(tmp_path, monkeypatch):
    config, monitor = _runtime(tmp_path)
    source = _message(100)
    monitor.record(source)
    job = _job(monitor)

    async def load(_client, events):
        assert [event.message_id for event in events] == [100]
        return [source]

    async def resolve(_client, peer):
        assert peer == "42"
        monitor.inbox.result(
            job_id=job["job_id"],
            lease=job["lease"],
            expected_revision=job["revision"],
            outcome="no_reply_needed",
            event_ids=job["event_ids"],
            receipt_keys=[],
            reason_code="not_actionable",
        )
        return object(), {"id": 42, "kind": "private"}

    monkeypatch.setattr(bridge, "_load_inbound_monitor_context_messages", load)
    monkeypatch.setattr(bridge, "_resolve_peer", resolve)
    monkeypatch.setattr(bridge, "_owner_peer_id", lambda _config: 42)
    try:
        with pytest.raises(bridge.BridgeError, match=r"^inbound_event_not_open$"):
            asyncio.run(
                bridge._handle_operation(
                    object(),
                    config,
                    {"operation": "monitor_target", "event_id": job["event_ids"][0]},
                    inbound_monitor=monitor,
                )
            )
    finally:
        monitor.inbox.close()


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


def test_client_send_cannot_forge_handoff_receipt(tmp_path):
    config, monitor = _runtime(tmp_path)
    source = _message(100)
    monitor.record(source)
    job = _job(monitor)
    client = Client([source])
    with pytest.raises(bridge.BridgeError, match="job_handoff_required"):
        asyncio.run(_send(client, config, monitor, job, kind="handoff", text="A human will handle it"))
    assert client.sent == 0
    assert monitor.inbox.read_job(job["job_id"])["effects"] == []
    with pytest.raises(InboxError, match="inbox_result_receipt_required"):
        monitor.inbox.result(
            job_id=job["job_id"],
            lease=job["lease"],
            expected_revision=job["revision"],
            outcome="handoff",
            event_ids=job["event_ids"],
            receipt_keys=[],
        )
    assert monitor.status()["open_events"] == 1
    monitor.inbox.close()


@pytest.mark.parametrize("mismatch", ["client_reply", "wrong_owner", "wrong_digest"])
def test_old_verified_handoff_effect_requires_exact_native_owner_ledger(tmp_path, monkeypatch, mismatch):
    config, monitor = _runtime(tmp_path)
    monitor.record(_message(100))
    job = _job(monitor)
    digest = hashlib.sha256(b"Synthetic handoff").hexdigest()
    effect_key = monitor.inbox.reserve_effect(
        job_id=job["job_id"],
        lease=job["lease"],
        expected_revision=job["revision"],
        kind="handoff",
        text_sha256=digest,
        peer_id=42,
        reply_to_message_id=100,
    )
    monitor.inbox.verify_effect(effect_key=effect_key, receipt_key=effect_key, message_id=101)
    monkeypatch.setattr(bridge, "_owner_peer_id", lambda *_args, **_kwargs: 84)
    key = bridge._ensure_private_key(config.state_dir / "contract.key")
    delivered_peer = 85 if mismatch == "wrong_owner" else 84
    binding = hmac.new(key, f"owner\0{delivered_peer}".encode(), hashlib.sha256).digest()
    entry = {
        "operation": "send_owner_notification",
        "target_role": "owner",
        "target_fingerprint": f"{int.from_bytes(binding[:8], 'big') or 1:016x}",
        "message_id": 101,
        "text_sha256": digest,
        "readback_verified": True,
    }
    if mismatch == "client_reply":
        entry.update(operation="send_text", peer_id=42, reply_to_message_id=100)
    elif mismatch == "wrong_digest":
        entry["text_sha256"] = hashlib.sha256(b"A different handoff").hexdigest()
    bridge._save_idempotency(config.state_dir / "idempotency.json", {effect_key: entry})
    request = {
        "operation": "job_result",
        "job_id": job["job_id"],
        "lease": job["lease"],
        "expected_revision": job["revision"],
        "outcome": "handoff",
        "event_ids": job["event_ids"],
        "receipt_keys": [effect_key],
    }
    with pytest.raises(bridge.BridgeError, match="job_handoff_receipt_required"):
        asyncio.run(bridge._handle_operation(object(), config, request, inbound_monitor=monitor))
    assert monitor.inbox.read_job(job["job_id"])["state"] == "running"
    assert monitor.inbox.event_state(job["event_ids"][0]) == "open"
    monitor.inbox.close()


def _media_request(config, operation):
    outbox = config.socket_path.parent / "outbox"
    outbox.mkdir(mode=0o700)
    suffix = ".pdf" if operation == "send_document" else ".jpg"
    path = outbox / ("synthetic" + suffix)
    path.write_bytes(_minimal_pdf() if suffix == ".pdf" else b"\xff\xd8synthetic\xff\xd9")
    path.chmod(0o600)
    return {
        "operation": operation,
        "peer": "42",
        "document" if suffix == ".pdf" else "photo": str(path),
        "caption": "Synthetic attachment",
        "mode": "dry_run",
    }


def test_raw_markdown_text_keeps_verified_job_receipt(tmp_path):
    config, monitor = _runtime(tmp_path)
    source = _message(100)
    monitor.record(source)
    job = _job(monitor)
    client = MarkdownDefaultClient([source])
    text = "  **literal** _text_\n "
    assert MarkdownDefaultClient.rendered(text, {}) != text
    dry = asyncio.run(_send(client, config, monitor, job, kind="result", text=text))
    sent = asyncio.run(_send(client, config, monitor, job, kind="result", text=text, mode="apply", dry=dry))
    assert sent["verified"] is True and client.messages[sent["message_id"]].message == text
    effect = monitor.inbox.read_job(job["job_id"])["effects"][0]
    assert effect["verified"] == 1 and effect["effect_key"] == sent["receipt_key"]
    monitor.inbox.close()


@pytest.mark.parametrize("operation", ["send_photo", "send_document"])
def test_raw_markdown_caption_keeps_verified_job_media_receipt(tmp_path, monkeypatch, operation):
    config, monitor = _runtime(tmp_path)
    source = _message(100)
    monitor.record(source)
    job = _job(monitor)
    client = MarkdownDefaultClient([source])

    async def resolve(_client, _peer):
        return 42, {"id": 42, "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)
    caption = "  **literal** _caption_\n "
    request = _media_request(config, operation) | {
        "caption": caption,
        "job_id": job["job_id"],
        "lease": job["lease"],
        "revision": job["revision"],
        "kind": "result",
        "reply_to_message_id": 100,
    }
    dry = asyncio.run(bridge._handle_operation(client, config, request, inbound_monitor=monitor))
    sent = asyncio.run(
        bridge._handle_operation(
            client,
            config,
            request
            | {"mode": "apply", "contract_token": dry["contract_token"], "idempotency_key": dry["idempotency_key"]},
            inbound_monitor=monitor,
        )
    )
    assert sent["verified"] is True and client.messages[sent["message_id"]].message == caption
    assert monitor.inbox.read_job(job["job_id"])["effects"][0]["verified"] == 1
    monitor.inbox.close()


def test_raw_markdown_owner_notification_keeps_verified_native_receipt(tmp_path, monkeypatch):
    config, monitor = _runtime(tmp_path)
    client = MarkdownDefaultClient([])
    monkeypatch.setattr(bridge, "_owner_peer_id", lambda *_args, **_kwargs: 84)

    async def owner(_client, _config):
        return 84, {"id": 84, "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_owner_target", owner)
    text = "  **literal** _owner_\n "
    request = {
        "operation": "notify_owner",
        "kind": "completion",
        "notification_key": "raw-owner",
        "text": text,
        "mode": "dry_run",
    }
    dry = asyncio.run(bridge._handle_operation(client, config, request, inbound_monitor=monitor))
    sent = asyncio.run(
        bridge._handle_operation(
            client,
            config,
            request | {"mode": "apply", "contract_token": dry["contract_token"]},
            inbound_monitor=monitor,
        )
    )
    assert sent["verified"] is True and client.messages[sent["message_id"]].message == text
    assert (
        next(iter(bridge._load_idempotency(config.state_dir / "idempotency.json").values()))["readback_verified"]
        is True
    )
    monitor.inbox.close()


@pytest.mark.parametrize("operation", ["send_photo", "send_document"])
@pytest.mark.parametrize("enabled", [True, False])
def test_direct_owner_media_without_inbound_job_preserves_send_policy(tmp_path, monkeypatch, operation, enabled):
    config, monitor = _runtime(tmp_path)
    if not enabled:
        monitor.inbox.duty_set(False)
        monitor.capture_enabled = False
    client = MediaClient([_message(100, out=True)])

    async def resolve(_client, _peer):
        return 42, {"id": 42, "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)
    request = _media_request(config, operation)
    dry = asyncio.run(bridge._handle_operation(client, config, request, inbound_monitor=monitor))
    apply = request | {"mode": "apply", "contract_token": dry["contract_token"], "idempotency_key": "owner-media"}
    sent = asyncio.run(bridge._handle_operation(client, config, apply, inbound_monitor=monitor))
    repeated = asyncio.run(bridge._handle_operation(client, config, apply, inbound_monitor=monitor))
    assert sent["verified"] and repeated["replayed"] and client.sent == 1
    assert "receipt_key" not in sent and monitor.inbox.active_jobs() == []
    assert monitor.status()["open_events"] == 0
    with pytest.raises(bridge.BridgeError, match="job_binding_required"):
        asyncio.run(
            bridge._handle_operation(client, config, request | {"reply_to_message_id": 100}, inbound_monitor=monitor)
        )
    monitor.inbox.close()


@pytest.mark.parametrize("operation", ["send_photo", "send_document"])
def test_job_media_verifies_result_but_cannot_forge_handoff(tmp_path, monkeypatch, operation):
    config, monitor = _runtime(tmp_path)
    source = _message(100)
    monitor.record(source)
    job = _job(monitor)
    client = MediaClient([source])

    async def resolve(_client, _peer):
        return 42, {"id": 42, "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)
    request = _media_request(config, operation) | {
        "job_id": job["job_id"],
        "lease": job["lease"],
        "revision": job["revision"],
        "kind": "handoff",
        "reply_to_message_id": 100,
    }
    with pytest.raises(bridge.BridgeError, match="job_handoff_required"):
        asyncio.run(bridge._handle_operation(client, config, request, inbound_monitor=monitor))
    assert client.sent == 0 and monitor.inbox.read_job(job["job_id"])["effects"] == []
    request["kind"] = "result"
    dry = asyncio.run(bridge._handle_operation(client, config, request, inbound_monitor=monitor))
    apply = request | {
        "mode": "apply",
        "contract_token": dry["contract_token"],
        "idempotency_key": dry["idempotency_key"],
    }
    sent = asyncio.run(bridge._handle_operation(client, config, apply, inbound_monitor=monitor))
    replay = asyncio.run(bridge._handle_operation(client, config, apply, inbound_monitor=monitor))
    assert sent["receipt_key"] == replay["receipt_key"] and client.sent == 1
    completed = monitor.inbox.result(
        job_id=job["job_id"],
        lease=job["lease"],
        expected_revision=job["revision"],
        outcome="answered",
        event_ids=job["event_ids"],
        receipt_keys=[sent["receipt_key"]],
    )
    assert completed["state"] == "completed" and monitor.status()["open_events"] == 0
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


@pytest.mark.parametrize("needs_review", [False, True])
def test_effect_readback_known_id_handles_new_context_without_finishing(tmp_path, needs_review):
    config, monitor = _runtime(tmp_path)
    source = _message(100)
    monitor.record(source)
    job = _job(monitor)
    client = ReadbackClient([source])
    dry = asyncio.run(_send(client, config, monitor, job, kind="result", text="Ready"))
    client.fail_readback = True
    with pytest.raises(OSError):
        asyncio.run(_send(client, config, monitor, job, kind="result", text="Ready", mode="apply", dry=dry))
    new_message = _message(102, text="A new clarification")
    client.messages[102] = new_message
    monitor.record(new_message)
    with pytest.raises(InboxError, match="inbox_context_stale"):
        asyncio.run(_send(client, config, monitor, job, kind="result", text="Ready", mode="apply", dry=dry))
    if needs_review:
        job = monitor.inbox.needs_review(
            job_id=job["job_id"],
            lease=job["lease"],
            expected_revision=job["revision"],
            reason_code="send_outcome_uncertain",
        )
    client.fail_readback = False
    client.reads.clear()
    proof = asyncio.run(_readback(client, config, monitor, job, dry["idempotency_key"]))
    repeated = asyncio.run(_readback(client, config, monitor, job, dry["idempotency_key"]))
    assert proof["verified"] and repeated["replayed"]
    assert proof["receipt_key"] == dry["idempotency_key"] and proof["message_id"] == 101
    assert client.reads == [(42, 101), (42, 101)] and client.sent == 1
    current = monitor.inbox.read_job(job["job_id"])
    assert current["revision"] == job["revision"] and current["context_revision"] == job["context_revision"]
    assert current["state"] == job["state"] and current["event_ids"] == job["event_ids"]
    assert current["effects"][0]["verified"] == 1
    ledger = bridge._load_idempotency(config.state_dir / "idempotency.json")[proof["receipt_key"]]
    assert ledger["message_id"] == 101 and ledger["readback_verified"] is True
    with pytest.raises(InboxError, match="inbox_context_stale"):
        monitor.inbox.result(
            job_id=job["job_id"],
            lease=job["lease"],
            expected_revision=job["revision"],
            outcome="answered",
            event_ids=job["event_ids"],
            receipt_keys=[proof["receipt_key"]],
        )
    assert monitor.status()["open_events"] == 2
    with pytest.raises(bridge.BridgeError, match="job_effect_message_id_conflict"):
        asyncio.run(
            _readback(client, config, monitor, job, proof["receipt_key"], message_id=102, effects_reviewed=True)
        )
    monitor.inbox.close()


def _unknown_text_effect(tmp_path):
    config, monitor = _runtime(tmp_path)
    source = _message(100)
    monitor.record(source)
    job = _job(monitor)
    client = ReadbackClient([source])
    dry = asyncio.run(_send(client, config, monitor, job, kind="result", text="Ready"))
    client.lose_text_response = True
    with pytest.raises(OSError):
        asyncio.run(_send(client, config, monitor, job, kind="result", text="Ready", mode="apply", dry=dry))
    client.reads.clear()
    return config, monitor, job, client, dry["idempotency_key"]


@pytest.mark.parametrize("mismatch", ["peer", "body", "reply", "incoming", "id"])
def test_unknown_effect_manual_readback_rejects_wrong_exact_message(tmp_path, mismatch):
    config, monitor, job, client, key = _unknown_text_effect(tmp_path)
    message = client.messages[101]
    if mismatch == "peer":
        message.chat_id = 43
    elif mismatch == "body":
        message.message = "An unrelated answer"
    elif mismatch == "reply":
        message.reply_to_msg_id = 99
    elif mismatch == "incoming":
        message.out = False
    else:
        message.id = 102
    with pytest.raises(bridge.BridgeError, match="job_effect_readback_failed"):
        asyncio.run(_readback(client, config, monitor, job, key, message_id=101, effects_reviewed=True))
    assert monitor.inbox.read_effect(key)["verified"] == 0 and client.sent == 1
    assert bridge._load_idempotency(config.state_dir / "idempotency.json")[key]["message_id"] == 0
    monitor.inbox.close()


def test_unknown_effect_manual_id_requires_review_and_never_resends(tmp_path):
    config, monitor, job, client, key = _unknown_text_effect(tmp_path)
    with pytest.raises(bridge.BridgeError, match="job_effect_message_id_required"):
        asyncio.run(_readback(client, config, monitor, job, key))
    with pytest.raises(bridge.BridgeError, match="job_effect_manual_review_required"):
        asyncio.run(_readback(client, config, monitor, job, key, message_id=101))
    with pytest.raises(bridge.BridgeError, match="job_effect_message_id_invalid"):
        asyncio.run(_readback(client, config, monitor, job, key, message_id=True, effects_reviewed=True))
    assert client.reads == []
    proof = asyncio.run(_readback(client, config, monitor, job, key, message_id=101, effects_reviewed=True))
    assert proof["verified"] and proof["receipt_key"] == key and client.sent == 1
    assert client.reads == [(42, 101)] and monitor.status()["open_events"] == 1
    monitor.inbox.close()


@pytest.mark.parametrize("mismatch", ["missing", "operation", "peer", "reply", "digest"])
def test_effect_readback_requires_original_consistent_native_ledger(tmp_path, mismatch):
    config, monitor, job, client, key = _unknown_text_effect(tmp_path)
    path = config.state_dir / "idempotency.json"
    ledger = bridge._load_idempotency(path)
    expected = "job_effect_ledger_conflict"
    if mismatch == "missing":
        path.unlink()
        expected = "job_effect_ledger_required"
    else:
        changes = {
            "operation": ("operation", "send_owner_notification"),
            "peer": ("peer_id", 43),
            "reply": ("reply_to_message_id", 99),
            "digest": ("text_sha256", "0" * 64),
        }
        field, value = changes[mismatch]
        ledger[key][field] = value
        bridge._save_idempotency(path, ledger)
    with pytest.raises(bridge.BridgeError, match=expected):
        asyncio.run(_readback(client, config, monitor, job, key, message_id=101, effects_reviewed=True))
    assert client.reads == [] and client.sent == 1
    assert monitor.inbox.read_effect(key)["verified"] == 0
    monitor.inbox.close()


def test_effect_readback_requires_current_exact_job_binding(tmp_path):
    config, monitor, job, client, key = _unknown_text_effect(tmp_path)
    with pytest.raises(InboxError, match="inbox_lease_mismatch"):
        asyncio.run(_readback(client, config, monitor, job | {"lease": "wrong-lease"}, key, message_id=101))
    with pytest.raises(InboxError, match="inbox_revision_conflict"):
        asyncio.run(_readback(client, config, monitor, job | {"revision": job["revision"] - 1}, key, message_id=101))
    assert client.reads == [] and monitor.inbox.read_effect(key)["verified"] == 0
    monitor.inbox.close()


@pytest.mark.parametrize("operation", ["send_photo", "send_document"])
@pytest.mark.parametrize("known", [True, False])
def test_media_effect_readback_uses_native_intent_and_exact_receipt(tmp_path, monkeypatch, operation, known):
    config, monitor = _runtime(tmp_path)
    source = _message(100)
    monitor.record(source)
    job = _job(monitor)
    client = ReadbackClient([source])

    async def resolve(_client, _peer):
        return 42, {"id": 42, "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)
    request = _media_request(config, operation) | {
        "job_id": job["job_id"],
        "lease": job["lease"],
        "revision": job["revision"],
        "kind": "result",
        "reply_to_message_id": 100,
    }
    dry = asyncio.run(bridge._handle_operation(client, config, request, inbound_monitor=monitor))
    client.fail_readback = known
    client.lose_file_response = not known
    apply = request | {
        "mode": "apply",
        "contract_token": dry["contract_token"],
        "idempotency_key": dry["idempotency_key"],
    }
    with pytest.raises(OSError):
        asyncio.run(bridge._handle_operation(client, config, apply, inbound_monitor=monitor))
    for path in (config.socket_path.parent / "outbox").iterdir():
        path.unlink()
    client.fail_readback = False
    if operation == "send_photo" and known:
        client.messages[101].synthetic_content = b"Telegram transformed image"
    params = {} if known else {"message_id": 101, "effects_reviewed": True}
    proof = asyncio.run(_readback(client, config, monitor, job, dry["idempotency_key"], **params))
    assert proof["verified"] and proof["receipt_key"] == dry["idempotency_key"] and client.sent == 1
    assert client.downloads == (0 if operation == "send_photo" and known else 1)
    assert monitor.status()["open_events"] == 1 and monitor.inbox.read_job(job["job_id"])["state"] == "running"
    monitor.inbox.close()


@pytest.mark.parametrize("operation", ["send_photo", "send_document"])
def test_unknown_media_effect_rejects_content_with_matching_metadata(tmp_path, monkeypatch, operation):
    config, monitor = _runtime(tmp_path)
    source = _message(100)
    monitor.record(source)
    job = _job(monitor)
    client = ReadbackClient([source])

    async def resolve(_client, _peer):
        return 42, {"id": 42, "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)
    request = _media_request(config, operation) | {
        "job_id": job["job_id"],
        "lease": job["lease"],
        "revision": job["revision"],
        "kind": "result",
        "reply_to_message_id": 100,
    }
    dry = asyncio.run(bridge._handle_operation(client, config, request, inbound_monitor=monitor))
    client.lose_file_response = True
    with pytest.raises(OSError):
        asyncio.run(
            bridge._handle_operation(
                client,
                config,
                request
                | {"mode": "apply", "contract_token": dry["contract_token"], "idempotency_key": dry["idempotency_key"]},
                inbound_monitor=monitor,
            )
        )
    original = client.messages[101].synthetic_content
    client.messages[101].synthetic_content = b"X" + original[1:]
    with pytest.raises(bridge.BridgeError, match="job_effect_readback_failed"):
        asyncio.run(
            _readback(client, config, monitor, job, dry["idempotency_key"], message_id=101, effects_reviewed=True)
        )
    assert monitor.inbox.read_effect(dry["idempotency_key"])["verified"] == 0 and client.sent == 1
    monitor.inbox.close()


def test_unknown_handoff_cannot_claim_another_identical_owner_message(tmp_path, monkeypatch):
    config, monitor = _runtime(tmp_path)
    source = _message(100)
    monitor.record(source)
    job = _job(monitor)
    client = Client([source])
    monkeypatch.setattr(bridge, "_owner_peer_id", lambda *_args, **_kwargs: 84)

    async def resolve(_client, peer):
        return int(peer), {"id": int(peer), "kind": "private"}

    async def owner(_client, _config):
        return 84, {"id": 84, "kind": "private"}

    async def history(entity, *, limit):
        for message in sorted(client.messages.values(), key=lambda item: item.id, reverse=True)[:limit]:
            if message.chat_id == entity:
                yield message

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)
    monkeypatch.setattr(bridge, "_resolve_owner_target", owner)
    monkeypatch.setattr(client, "iter_messages", history)
    request = {
        "operation": "job_handoff",
        "job_id": job["job_id"],
        "lease": job["lease"],
        "expected_revision": job["revision"],
        "reply_to_message_id": 100,
        "text": "Synthetic human handoff",
        "mode": "dry_run",
    }

    def call(payload):
        return asyncio.run(bridge._handle_operation(client, config, payload, inbound_monitor=monitor))

    try:
        dry = call(request)
        key = dry["idempotency_key"]
        apply = request | {"mode": "apply", "contract_token": dry["contract_token"], "idempotency_key": key}
        client.fail_send = True
        with pytest.raises(OSError):
            call(apply)
        client.fail_send = False
        other = {
            "operation": "send_owner_notification",
            "idempotency_key": "independent-owner-event",
            "text": request["text"],
            "mode": "dry_run",
        }
        other_dry = call(other)
        assert call(other | {"mode": "apply", "contract_token": other_dry["contract_token"]})["verified"]
        before = (config.state_dir / "idempotency.json").read_bytes()
        for retry in (request, apply):
            with pytest.raises(bridge.BridgeError, match="owner_notification_outcome_uncertain"):
                call(retry)
        assert client.sent == 2
        assert (config.state_dir / "idempotency.json").read_bytes() == before
        effect = monitor.inbox.read_effect(key)
        assert effect["attempted"] == 1 and effect["verified"] == 0 and effect["receipt_key"] is None
        with pytest.raises(InboxError, match="inbox_receipt_unverified"):
            call(
                request
                | {
                    "operation": "job_result",
                    "outcome": "handoff",
                    "event_ids": job["event_ids"],
                    "receipt_keys": [key],
                }
            )
        assert monitor.inbox.read_job(job["job_id"])["state"] == "running" and monitor.status()["open_events"] == 1
    finally:
        monitor.inbox.close()


@pytest.mark.parametrize("known", [True, False])
def test_handoff_effect_readback_requires_current_owner_and_exact_message(tmp_path, monkeypatch, known):
    config, monitor = _runtime(tmp_path)
    source = _message(100)
    monitor.record(source)
    job = _job(monitor)
    client = ReadbackClient([source])
    owner_id = 84
    monkeypatch.setattr(bridge, "_owner_peer_id", lambda *_args, **_kwargs: owner_id)

    async def resolve(_client, _peer):
        return 42, {"id": 42, "kind": "private"}

    async def owner(_client, _config):
        return owner_id, {"id": owner_id, "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)
    monkeypatch.setattr(bridge, "_resolve_owner_target", owner)
    request = {
        "operation": "job_handoff",
        "job_id": job["job_id"],
        "lease": job["lease"],
        "expected_revision": job["revision"],
        "reply_to_message_id": 100,
        "text": "Synthetic human handoff",
        "mode": "dry_run",
    }
    dry = asyncio.run(bridge._handle_operation(client, config, request, inbound_monitor=monitor))
    client.fail_readback = known
    client.lose_text_response = not known
    with pytest.raises(OSError):
        asyncio.run(
            bridge._handle_operation(
                client,
                config,
                request
                | {"mode": "apply", "contract_token": dry["contract_token"], "idempotency_key": dry["idempotency_key"]},
                inbound_monitor=monitor,
            )
        )
    client.fail_readback = False
    client.lose_text_response = False
    client.reads.clear()
    owner_id = 85
    with pytest.raises(bridge.BridgeError, match="job_effect_ledger_conflict"):
        asyncio.run(_readback(client, config, monitor, job, dry["idempotency_key"]))
    assert client.reads == []
    owner_id = 84
    params = {} if known else {"message_id": 101, "effects_reviewed": True}
    proof = asyncio.run(_readback(client, config, monitor, job, dry["idempotency_key"], **params))
    assert proof["verified"] and proof["message_id"] == 101 and client.sent == 1
    assert client.messages[101].chat_id == 84 and monitor.status()["open_events"] == 1
    monitor.inbox.close()


def test_job_effect_readback_cli_preserves_exact_manual_receipt_arguments():
    args = bridge.build_parser().parse_args(
        [
            "--account",
            "work",
            "job-effect-readback",
            "--job-id",
            "job-" + "a" * 32,
            "--lease",
            "exact-lease",
            "--expected-revision",
            "4",
            "--effect-key",
            "tgjob-" + "b" * 64,
            "--message-id",
            "101",
            "--effects-reviewed",
        ]
    )
    request = bridge._job_cli_request(args)
    assert request["operation"] == "job_effect_readback"
    assert request["effect_key"] == "tgjob-" + "b" * 64
    assert request["expected_revision"] == 4 and request["message_id"] == 101 and request["effects_reviewed"] is True


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
    _config, monitor = _runtime(tmp_path)
    monkeypatch.setattr(bridge, "MAX_RECONCILE_DIALOGS", 2)
    client = Client([_message(100, peer=42), _message(101, peer=43), _message(102, peer=44)])
    asyncio.run(bridge._reconcile_incoming_history(client, monitor))
    cursor = monitor.inbox.reconciliation_cursor()
    assert cursor["peer_id"] == 43
    assert monitor.status()["open_events"] == 2
    asyncio.run(bridge._reconcile_incoming_history(client, monitor))
    assert monitor.status()["open_events"] == 3
    assert monitor.inbox.reconciliation_cursor() is None
    assert monitor.inbox.health()["intake"]["state"] == "ready"
    monitor.inbox.close()


def _moving_history_runtime(tmp_path):
    config, monitor = _runtime(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    monitor.inbox.duty_set(False, at=(now - timedelta(minutes=30)).isoformat())
    monitor.inbox.duty_set(True, at=(now - timedelta(minutes=5)).isoformat())
    client = DateOrderedClient(
        [
            _message(200, peer=42, out=True, at=now - timedelta(minutes=10)),
            _message(190, peer=43, out=True, at=now - timedelta(minutes=15)),
            _message(100, peer=44, at=now - timedelta(minutes=45)),
            _message(180, peer=44, out=True, at=now - timedelta(minutes=20)),
        ]
    )
    return config, monitor, client, now


def test_moved_dialogue_rescans_prefix_before_ready_or_recovering_window(tmp_path, monkeypatch):
    _config, monitor, client, now = _moving_history_runtime(tmp_path)
    monkeypatch.setattr(bridge, "MAX_RECONCILE_DIALOGS", 2)

    async def sweep():
        monitor.reconcile_requested.clear()
        await bridge._reconcile_incoming_history(client, monitor)

    async def scenario():
        await sweep()
        assert monitor.inbox.reconciliation_cursor()["peer_id"] == 43
        assert monitor.inbox.events() == []
        client.messages[300] = _message(300, peer=44, out=True, at=now - timedelta(minutes=1))
        client.update_revision += 1
        await sweep()
        assert client.dialog_calls[1] == []
        assert monitor.inbox.events() == []
        assert monitor.inbox.health()["intake"] == {
            "state": "held",
            "error_code": "history_reconciliation_changed",
        }
        assert monitor.inbox.duty_windows()[0]["recovered_at"] is None
        assert monitor.inbox.reconciliation_cursor() is None and monitor.reconcile_requested.is_set()
        await sweep()
        assert monitor.inbox.resolve(monitor.inbox.events()[0]["event_id"]).message_id == 100
        assert monitor.inbox.duty_windows()[0]["recovered_at"] is None
        await sweep()
        assert monitor.inbox.health()["intake"]["state"] == "ready"
        assert monitor.inbox.duty_windows()[0]["recovered_at"] is not None
        assert not monitor.reconcile_requested.is_set()

    asyncio.run(scenario())
    monitor.inbox.close()


def test_restart_discards_unfenced_cursor_preserving_peer_checkpoints(tmp_path, monkeypatch):
    config, monitor, client, now = _moving_history_runtime(tmp_path)
    monkeypatch.setattr(bridge, "MAX_RECONCILE_DIALOGS", 2)
    asyncio.run(bridge._reconcile_incoming_history(client, monitor))
    assert monitor.inbox.checkpoint(42) == 200
    assert monitor.inbox.reconciliation_cursor()["peer_id"] == 43
    monitor.inbox.close()
    client.messages[300] = _message(300, peer=44, out=True, at=now - timedelta(minutes=1))
    client.update_revision += 1
    restarted = bridge.DurableInboundMonitor(TelegramInbox(config.state_dir / "inbound.sqlite3"), enabled=True)
    asyncio.run(bridge._reconcile_incoming_history(client, restarted))
    assert client.dialog_calls[1][0] == 44
    assert restarted.inbox.resolve(restarted.inbox.events()[0]["event_id"]).message_id == 100
    assert restarted.inbox.checkpoint(42) == 200
    assert restarted.inbox.health()["intake"]["state"] == "held"
    restarted.reconcile_requested.clear()
    asyncio.run(bridge._reconcile_incoming_history(client, restarted))
    assert restarted.inbox.health()["intake"]["state"] == "ready"
    assert restarted.inbox.reconciliation_cursor() is None
    restarted.inbox.close()


def test_continuously_changing_history_stops_after_one_rescan_until_new_event(tmp_path):
    _config, monitor = _runtime(tmp_path)

    class ChangingClient(DateOrderedClient):
        async def __call__(self, request):
            self.update_revision += 1
            return await super().__call__(request)

    client = ChangingClient([_message(100)])

    async def scenario():
        await bridge._reconcile_incoming_history(client, monitor)
        assert monitor.reconcile_requested.is_set()
        monitor.reconcile_requested.clear()
        await bridge._reconcile_incoming_history(client, monitor)
        assert monitor.reconciliation_stopped and not monitor.reconcile_requested.is_set()
        before = client.update_revision
        await bridge._reconcile_incoming_history(client, monitor)
        assert client.update_revision == before and len(client.dialog_calls) == 2
        assert monitor.inbox.health()["intake"]["state"] == "held"
        new_event = _message(101, out=True)
        client.messages[101] = new_event
        await bridge._capture_incoming_event(monitor, new_event)
        assert monitor.reconcile_requested.is_set() and not monitor.reconciliation_stopped
        monitor.reconcile_requested.clear()
        await bridge._reconcile_incoming_history(client, monitor)
        assert len(client.dialog_calls) == 3 and monitor.reconciliation_restarts == 1

    asyncio.run(scenario())
    monitor.inbox.close()


@pytest.mark.parametrize("value", [True, -1, 0x80000000, "1", None])
def test_history_fence_rejects_malformed_update_state(value):
    async def client(_request):
        return SimpleNamespace(pts=value, qts=0, seq=0)

    with pytest.raises(bridge.BridgeError, match="history_state_invalid"):
        asyncio.run(bridge._reconciliation_state_fence(client))


def test_history_contract_failure_holds_without_automatic_retry(tmp_path):
    config, monitor = _runtime(tmp_path)

    async def scenario():
        monitor.request_reconciliation()
        task = asyncio.create_task(bridge._reconcile_incoming_loop(object(), config, monitor))
        try:
            await asyncio.sleep(0)
            assert monitor.inbox.health()["intake"] == {
                "state": "held",
                "error_code": "history_reconciliation_failed",
            }
            assert monitor.reconciliation_fence is None and not monitor.reconcile_requested.is_set()
            assert monitor.inbox.events() == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    monitor.inbox.close()


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


def test_job_active_ipc_pages_large_held_queue_without_instruction_bodies(tmp_path, monkeypatch):
    config, monitor = _runtime(tmp_path)
    long_root = "/test/snapshot/" + "a" * 3900
    monitor.inbox.import_references(
        [
            {
                "event_id": f"inbound-{index + 1}",
                "peer_id": index + 1,
                "message_id": index + 1,
                "received_at": datetime.now(UTC).isoformat(),
                "state": "open",
            }
            for index in range(200)
        ],
        instruction_root=long_root,
        instruction_revision=REVISION,
    )
    monkeypatch.setattr(bridge, "_require_root_client", lambda _writer: None)

    async def scenario():
        server = await asyncio.start_unix_server(
            lambda reader, writer: bridge._serve_client(object(), config, reader, writer, inbound_monitor=monitor),
            path=str(config.socket_path),
        )
        seen = []
        cursor = None
        async with server:
            while True:
                reader, writer = await asyncio.open_unix_connection(str(config.socket_path))
                writer.write(bridge._canonical_json({"operation": "job_active", "after_job_id": cursor}) + b"\n")
                await writer.drain()
                payload = await reader.readline()
                response = json.loads(payload)
                assert response["ok"] is True and len(response["jobs"]) == 50
                assert len(payload) < bridge.MAX_REQUEST_BYTES and long_root.encode() not in payload
                assert all("instruction_root" not in row and "effects" not in row for row in response["jobs"])
                assert all(row["state"] == "needs_review" for row in response["jobs"])
                seen.extend(row["job_id"] for row in response["jobs"])
                cursor = response["next_job_id"]
                writer.close()
                await writer.wait_closed()
                if cursor is None:
                    break
        assert len(seen) == 200 and len(set(seen)) == 200

    asyncio.run(scenario())
    monitor.inbox.close()


def test_job_active_cli_preserves_page_arguments():
    args = bridge.build_parser().parse_args(
        ["--account", "work", "job-active", "--limit", "2", "--after-job-id", "job-" + "a" * 32]
    )
    assert bridge._job_cli_request(args) == {
        "operation": "job_active",
        "limit": 2,
        "after_job_id": "job-" + "a" * 32,
    }


def test_execution_pause_rpc_uses_separate_revision_after_new_intake(tmp_path):
    config, monitor = _runtime(tmp_path)
    status = monitor.inbox.status()
    revision = status["execution_revision"]
    monitor.record(_message(100))
    request = {"operation": "job_execution_set", "paused": True, "expected_revision": revision}
    response = asyncio.run(bridge._handle_operation(object(), config, request, inbound_monitor=monitor))
    assert response["status"]["execution_paused"] is True
    assert response["status"]["execution_revision"] == revision + 1
    assert monitor.generation == monitor.inbox.generation
    with pytest.raises(InboxError, match="inbox_execution_revision_conflict"):
        asyncio.run(bridge._handle_operation(object(), config, request | {"paused": False}, inbound_monitor=monitor))
    with pytest.raises(bridge.BridgeError, match="job_execution_paused_invalid"):
        asyncio.run(bridge._handle_operation(object(), config, request | {"paused": 1}, inbound_monitor=monitor))
    assert monitor.inbox.status()["execution_paused"] is True
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


@pytest.mark.parametrize(
    "state", ["claimed", "running", "needs_review", "unknown_effect", "wrong_lease", "stale_revision"]
)
def test_retired_owner_control_is_read_only_and_keeps_claim_guards(tmp_path, monkeypatch, state):
    config, monitor = _runtime(tmp_path)
    source = _message(100, text="/automations")
    monitor.record(source)
    job = _job(monitor, running=state in {"running", "unknown_effect"})
    if state == "needs_review":
        job = monitor.inbox.needs_review(
            job_id=job["job_id"], lease=job["lease"], expected_revision=job["revision"], reason_code="synthetic_hold"
        )
    if state == "unknown_effect":
        key = monitor.inbox.reserve_effect(
            job_id=job["job_id"],
            lease=job["lease"],
            expected_revision=job["revision"],
            kind="result",
            text_sha256="a" * 64,
            peer_id=42,
            reply_to_message_id=100,
        )
        monitor.inbox.mark_effect_intent(key)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("retired owner ingress must not read identity, messages or call an adapter")

    monitor.automation_coordinator = SimpleNamespace(adapter=SimpleNamespace(handle=forbidden))
    monkeypatch.setattr(bridge, "_owner_peer_id", forbidden)
    client = SimpleNamespace(get_messages=forbidden, send_message=forbidden)
    request = {
        "operation": "job_owner_control",
        "job_id": job["job_id"],
        "lease": job["lease"],
        "expected_revision": job["revision"],
    }
    if state == "wrong_lease":
        request["lease"] += "stale"
    if state == "stale_revision":
        request["expected_revision"] += 1
    before = monitor.inbox.read_job(job["job_id"])
    if state == "claimed":
        result = asyncio.run(bridge._handle_job_operation(client, config, request, monitor))
        assert result == {"ok": True, "handled": False, "job": before}
    else:
        with pytest.raises(bridge.BridgeError, match="job_owner_control_state_invalid"):
            asyncio.run(bridge._handle_job_operation(client, config, request, monitor))
    assert monitor.inbox.read_job(job["job_id"]) == before
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
    monitor.transport_ready = True
    asyncio.run(bridge._capture_incoming_event(monitor, _message(100)))
    assert monitor.failure_event.is_set() is True
    assert monitor.transport_ready is False
    with pytest.raises(bridge.BridgeError, match="inbound_storage_unavailable"):
        asyncio.run(bridge._await_intake_failure(monitor))
    monitor.inbox.close()


def test_real_sqlite_wrapped_storage_failure_is_fatal_and_accepts_nothing(tmp_path, capsys):
    _config, monitor = _runtime(tmp_path)
    monitor.transport_ready = True
    monitor.inbox._db.execute("PRAGMA query_only=ON")
    asyncio.run(bridge._capture_incoming_event(monitor, _message(100)))
    assert monitor.failure_event.is_set() is True
    assert monitor.failure_code == "inbound_storage_unavailable"
    assert monitor.transport_ready is False
    assert monitor.status()["open_events"] == 0
    assert monitor.inbox.events() == []
    assert "work_telegram_inbox_error=inbound_storage_unavailable" in capsys.readouterr().err
    with pytest.raises(bridge.BridgeError, match="inbound_storage_unavailable"):
        asyncio.run(bridge._await_intake_failure(monitor))
    monitor.inbox.close()


def test_capacity_gap_sends_one_incident_then_recovers_after_room_is_freed(tmp_path, monkeypatch):
    config, monitor = _runtime(tmp_path, max_events=1)
    monitor.transport_ready = True
    source, missing = _message(100), _message(101)
    client = Client([source, missing])
    monkeypatch.setattr(bridge, "_owner_peer_id", lambda *_args, **_kwargs: 84)

    async def owner(_client, _config):
        return 84, {"id": 84, "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_owner_target", owner)

    async def scenario():
        await bridge._capture_incoming_event(monitor, source)
        await bridge._capture_incoming_event(monitor, missing)
        assert monitor.failure_event.is_set() is False and monitor.transport_ready is True
        assert monitor.status()["open_events"] == 1 and monitor.status()["gap_count"] == 1
        assert monitor.inbox.health()["intake"]["error_code"] == "inbound_monitor_full"
        assert monitor.reconcile_requested.is_set() is True
        assert len(monitor.inbox.pending_notifications()) == 1
        monitor.reconcile_requested.clear()
        await bridge._reconcile_incoming_history(client, monitor)
        await bridge._deliver_incident_notifications(client, config, monitor)
        assert monitor.reconcile_requested.is_set() is False
        assert monitor.inbox.health()["intake"]["error_code"] == "inbound_monitor_full"
        assert client.sent == 1 and monitor.inbox.pending_notifications() == []
        incident = client.messages[102]
        assert incident.chat_id == 84 and "требуется проверка (intake_inbound_monitor_full)" in incident.message
        assert "Входящие сохраняются" not in incident.message
        await bridge._capture_incoming_event(monitor, missing)
        await bridge._deliver_incident_notifications(client, config, monitor)
        assert monitor.reconcile_requested.is_set() is False and client.sent == 1
        job = _job(monitor)
        result = await bridge._handle_job_operation(
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
        assert result["job"]["state"] == "completed" and monitor.reconcile_requested.is_set() is True
        monitor.reconcile_requested.clear()
        await bridge._reconcile_incoming_history(client, monitor)
        await bridge._deliver_incident_notifications(client, config, monitor)
        assert monitor.inbox.health()["intake"]["state"] == "ready"
        assert monitor.status()["gap_count"] == 0 and monitor.status()["open_events"] == 1
        assert monitor.inbox.checkpoint(42) == 101 and client.sent == 2
        recovery = client.messages[103]
        assert recovery.chat_id == 84 and "сбой устранён (intake_inbound_monitor_full)" in recovery.message
        assert "соединение восстановлено" not in recovery.message
        ledger = bridge._load_idempotency(config.state_dir / "idempotency.json")
        assert len(ledger) == 2 and all(entry["readback_verified"] for entry in ledger.values())
        assert monitor.inbox.pending_notifications() == []
        await bridge._reconcile_incoming_history(client, monitor)
        await bridge._deliver_incident_notifications(client, config, monitor)
        assert client.sent == 2 and monitor.reconcile_requested.is_set() is False

    monkeypatch.setattr(bridge, "_MUTATION_LOCK", asyncio.Lock())
    asyncio.run(scenario())
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
