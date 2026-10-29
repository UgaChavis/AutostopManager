"""Keep client context and concurrently verified receipts bound to their own peers."""

from __future__ import annotations

import asyncio
import hashlib
import hmac

import pytest

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_inbox import InboxError
from test_telegram_bridge_durable import MediaClient, _job, _media_request, _message, _readback, _runtime


@pytest.fixture(autouse=True)
def synthetic_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "synthetic.sqlite3"))
    monkeypatch.setattr(bridge, "_load_reconciliation_state_request", object)


class PeerScopedClient(MediaClient):
    """Model the SDK's entity filter for IDs and independent peer histories."""

    def __init__(self, messages):
        super().__init__(messages)
        self.reads = []
        self.after_watermark = None
        self.watermark_seen = False

    async def get_messages(self, entity, *, ids=None, limit=None):
        self.reads.append((entity, ids, limit))
        if self.after_watermark is not None and limit == 1:
            self.watermark_seen = True
        elif self.after_watermark is not None and self.watermark_seen and isinstance(ids, list):
            callback, self.after_watermark = self.after_watermark, None
            await callback()
        if ids is not None:
            rows = await super().get_messages(entity, ids=ids, limit=limit)

            def scoped(row):
                return row if row is not None and row.chat_id == entity else None

            return [scoped(row) for row in rows] if isinstance(rows, list) else scoped(rows)
        rows = [row for row in self.messages.values() if row.chat_id == entity]
        return sorted(rows, key=lambda row: row.id, reverse=True)[:limit]


def targets(monkeypatch, *, owner_id=84):
    async def resolve(_client, peer):
        return int(peer), {"id": int(peer), "kind": "private"}

    async def owner(_client, _config):
        return owner_id, {"id": owner_id, "kind": "private", "role": "owner"}

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)
    monkeypatch.setattr(bridge, "_resolve_owner_target", owner)
    monkeypatch.setattr(bridge, "_owner_peer_id", lambda *_args, **_kwargs: owner_id)


def handoff_request(job):
    return {
        "operation": "job_handoff",
        "job_id": job["job_id"],
        "lease": job["lease"],
        "expected_revision": job["revision"],
        "reply_to_message_id": 100,
        "text": "Synthetic verified handoff",
        "mode": "dry_run",
    }


def applied(request, draft):
    return request | {
        "mode": "apply",
        "contract_token": draft["contract_token"],
        "idempotency_key": draft["idempotency_key"],
    }


def test_handoff_uses_client_context_and_keeps_owner_send_readback_and_replay(tmp_path, monkeypatch):
    targets(monkeypatch)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        source = _message(100)
        monitor.record(source)
        job = _job(monitor)
        client = PeerScopedClient([source, _message(50, peer=84, text="Owner's earlier conversation")])
        request = handoff_request(job)
        try:
            draft = await bridge._handle_operation(client, config, request, inbound_monitor=monitor)
            sent = await bridge._handle_operation(client, config, applied(request, draft), inbound_monitor=monitor)
            replayed = await bridge._handle_operation(client, config, applied(request, draft), inbound_monitor=monitor)
            proof = await _readback(client, config, monitor, job, draft["idempotency_key"])
            assert sent["verified"] and replayed["replayed"] and proof["verified"]
            assert client.sent == 1 and client.messages[sent["message_id"]].chat_id == 84
            effect = monitor.inbox.read_effect(draft["idempotency_key"])
            assert effect["peer_id"] == 42 and effect["reply_to_message_id"] == 100 and effect["verified"]
            assert any(entity == 42 and isinstance(ids, list) for entity, ids, _ in client.reads)
            assert not any(entity == 84 and isinstance(ids, list) for entity, ids, _ in client.reads)
            assert source.out is False and monitor.status()["open_events"] == 1
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["pause", "client_context"])
def test_handoff_still_refuses_paused_or_changed_client_context(tmp_path, monkeypatch, change):
    targets(monkeypatch)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        rows = [_message(100), _message(101)]
        for row in rows:
            monitor.record(row)
        job = _job(monitor)
        client = PeerScopedClient([*rows, _message(50, peer=84)])
        request = handoff_request(job)
        try:
            draft = await bridge._handle_operation(client, config, request, inbound_monitor=monitor)
            if change == "pause":
                monitor.inbox.set_client_control(42, action="pause", expected_revision=0)
            else:
                rows[1].message = "The customer corrected the request"
            expected_error = "inbox_client_paused" if change == "pause" else "inbox_context_stale"
            with pytest.raises(InboxError, match=expected_error):
                await bridge._handle_operation(client, config, applied(request, draft), inbound_monitor=monitor)
            assert client.sent == 0 and not monitor.inbox.read_effect(draft["idempotency_key"])["attempted"]
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())


def test_handoff_unknown_send_keeps_original_key_without_second_dispatch(tmp_path, monkeypatch):
    targets(monkeypatch)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        source = _message(100)
        monitor.record(source)
        job = _job(monitor)
        client = PeerScopedClient([source, _message(50, peer=84)])
        request = handoff_request(job)
        try:
            draft = await bridge._handle_operation(client, config, request, inbound_monitor=monitor)
            client.fail_send = True
            with pytest.raises(OSError, match="network lost"):
                await bridge._handle_operation(client, config, applied(request, draft), inbound_monitor=monitor)
            client.fail_send = False
            before = (config.state_dir / "idempotency.json").read_bytes()
            for retry in (request, applied(request, draft)):
                with pytest.raises(bridge.BridgeError, match="owner_notification_outcome_uncertain"):
                    await bridge._handle_operation(client, config, retry, inbound_monitor=monitor)
            assert client.sent == 1 and (config.state_dir / "idempotency.json").read_bytes() == before
            effect = monitor.inbox.read_effect(draft["idempotency_key"])
            assert effect["attempted"] and not effect["verified"]
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["other_peer", "other_kind", "unavailable"])
def test_handoff_context_resolution_refuses_another_peer_or_unavailable_target(tmp_path, monkeypatch, failure):
    targets(monkeypatch)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        source = _message(100)
        monitor.record(source)
        job = _job(monitor)
        client = PeerScopedClient([source, _message(50, peer=84)])
        request = handoff_request(job)
        try:
            draft = await bridge._handle_operation(client, config, request, inbound_monitor=monitor)
            calls = []

            async def resolve(_client, peer):
                calls.append(peer)
                if len(calls) == 1:
                    return 42, {"id": 42, "kind": "private"}
                if failure == "unavailable":
                    raise bridge.BridgeError("peer_not_found")
                return 42, {
                    "id": 43 if failure == "other_peer" else 42,
                    "kind": "group" if failure == "other_kind" else "private",
                }

            monkeypatch.setattr(bridge, "_resolve_peer", resolve)
            error = "peer_not_found" if failure == "unavailable" else "inbound_target_mismatch"
            with pytest.raises(bridge.BridgeError, match=error):
                await bridge._handle_operation(client, config, applied(request, draft), inbound_monitor=monitor)
            assert calls == ["42", "42"] and client.sent == 0
            assert not monitor.inbox.read_effect(draft["idempotency_key"])["attempted"]
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["send", "send_photo", "send_document"])
def test_final_context_reread_preserves_concurrent_verified_receipt_and_new_entry(tmp_path, monkeypatch, operation):
    targets(monkeypatch)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        source = _message(100)
        monitor.record(source)
        job = _job(monitor)
        client = PeerScopedClient([source])
        owner_message = _message(900, peer=84, out=True, text="Synthetic owner notification")
        owner_client = PeerScopedClient([owner_message])
        digest = hashlib.sha256(owner_message.message.encode()).hexdigest()
        owner_key = "synthetic-owner-readback"
        ledger_path = config.state_dir / "idempotency.json"
        request = (
            {"operation": "send", "peer": "42", "text": "Synthetic client response", "mode": "dry_run"}
            if operation == "send"
            else _media_request(config, operation)
        ) | {
            "job_id": job["job_id"],
            "lease": job["lease"],
            "revision": job["revision"],
            "kind": "result",
            "reply_to_message_id": 100,
        }
        try:
            draft = await bridge._handle_operation(client, config, request, inbound_monitor=monitor)
            contract_key = bridge._ensure_private_key(config.state_dir / "contract.key")
            binding = hmac.new(contract_key, b"owner\x0084", hashlib.sha256).digest()
            bridge._save_idempotency(
                ledger_path,
                {
                    owner_key: {
                        "operation": "send_owner_notification",
                        "target_role": "owner",
                        "target_fingerprint": f"{(int.from_bytes(binding[:8], 'big') or 1):016x}",
                        "text_sha256": digest,
                        "message_id": 900,
                        "readback_verified": False,
                    }
                },
            )
            later_entry = {"operation": "send_text", "peer_id": 85, "message_id": 901, "readback_verified": True}
            readbacks = []

            async def interleave():
                readbacks.append(
                    await bridge._handle_owner_notification_idempotency_readback(
                        owner_client, config, {"idempotency_key": owner_key, "expected_text_sha256": digest}
                    )
                )
                current = bridge._load_idempotency(ledger_path)
                current["synthetic-new-entry"] = later_entry
                bridge._save_idempotency(ledger_path, current)

            client.after_watermark = interleave
            sent = await bridge._handle_operation(client, config, applied(request, draft), inbound_monitor=monitor)
            assert sent["verified"] and client.sent == 1 and readbacks[0]["verified"]
            ledger = bridge._load_idempotency(ledger_path)
            assert ledger[owner_key]["readback_verified"] is True
            assert ledger["synthetic-new-entry"] == later_entry
            assert ledger[draft["idempotency_key"]]["readback_verified"] is True
            assert bridge._native_outgoing_origins(config, 84)[1] is False
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["send", "send_photo", "send_document"])
def test_exact_key_appearing_during_final_reread_is_never_overwritten_or_sent(tmp_path, monkeypatch, operation):
    targets(monkeypatch)

    async def scenario():
        config, monitor = _runtime(tmp_path)
        source = _message(100)
        monitor.record(source)
        job = _job(monitor)
        client = PeerScopedClient([source])
        request = (
            {"operation": "send", "peer": "42", "text": "Synthetic client response", "mode": "dry_run"}
            if operation == "send"
            else _media_request(config, operation)
        ) | {
            "job_id": job["job_id"],
            "lease": job["lease"],
            "revision": job["revision"],
            "kind": "result",
            "reply_to_message_id": 100,
        }
        try:
            draft = await bridge._handle_operation(client, config, request, inbound_monitor=monitor)
            ledger_path = config.state_dir / "idempotency.json"
            appeared = {"operation": "send_text", "peer_id": 42, "message_id": 0, "readback_verified": False}

            async def interleave():
                bridge._save_idempotency(ledger_path, {draft["idempotency_key"]: appeared})

            client.after_watermark = interleave
            with pytest.raises(bridge.BridgeError, match="idempotency_key_conflict"):
                await bridge._handle_operation(client, config, applied(request, draft), inbound_monitor=monitor)
            assert client.sent == 0 and bridge._load_idempotency(ledger_path)[draft["idempotency_key"]] == appeared
            assert not monitor.inbox.read_effect(draft["idempotency_key"])["attempted"]
        finally:
            monitor.inbox.close()

    asyncio.run(scenario())
