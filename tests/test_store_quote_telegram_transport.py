from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autostop_manager import store_quote_conductor, telegram_bridge
from autostop_manager.store_quote_conductor import StoreQuoteTelegramDelivery, StoreQuoteTelegramInboundReply
from autostop_manager.store_quote_telegram_transport import (
    create_work_store_quote_transport,
)
from autostop_manager.telegram_bridge import BridgeError, TelegramConfig


_SNAPSHOT = "a" * 64
_CONTEXT = "b" * 64


def _work_config(tmp_path: Path, *, account: str = "work") -> TelegramConfig:
    state_dir = tmp_path / "state"
    return TelegramConfig(
        api_id=12345678,
        api_hash="0123456789abcdef0123456789abcdef",
        credentials_path=tmp_path / "credentials",
        session_path=state_dir / "account",
        state_dir=state_dir,
        socket_path=tmp_path / "bridge.sock",
        account=account,
    )


def _delivery() -> StoreQuoteTelegramDelivery:
    return StoreQuoteTelegramDelivery(
        quote_request_id="quote-000001",
        estimate_revision="revision-0001",
        published_snapshot_hash=_SNAPSHOT,
        context_hash=_CONTEXT,
        kind="offer",
        text="Кость, глянул. Его оформляем?",
    )


def _delivery_request(delivery: StoreQuoteTelegramDelivery, *, text: bool = False) -> dict[str, str]:
    request = {
        "quote_ref_sha256": telegram_bridge._store_quote_sha256(
            f"store-quote-conductor-v1\0{delivery.quote_request_id}"
        ),
        "revision_sha256": telegram_bridge._store_quote_sha256(delivery.estimate_revision),
        "published_snapshot_hash": delivery.published_snapshot_hash,
        "context_hash": delivery.context_hash,
        "delivery_binding_sha256": delivery.binding_sha256,
        "route_binding_sha256": delivery.route_binding_sha256,
        "message_sha256": delivery.text_sha256,
        "message_kind": delivery.kind,
    }
    if text:
        request["text"] = delivery.text
    return request


def _identity_delivery(delivery: StoreQuoteTelegramDelivery) -> StoreQuoteTelegramDelivery:
    return StoreQuoteTelegramDelivery(
        quote_request_id=delivery.quote_request_id,
        estimate_revision=delivery.estimate_revision,
        published_snapshot_hash=delivery.published_snapshot_hash,
        context_hash=delivery.context_hash,
        kind="identity_prompt",
        text=telegram_bridge.STORE_QUOTE_IDENTITY_PROMPT_TEXT,
    )


class _WorkClient:
    peer_id = 77

    def __init__(self) -> None:
        self.entity = object()
        self.messages: dict[int, SimpleNamespace] = {}
        self.last_id = 0

    async def send_message(self, entity, text: str):
        assert entity is self.entity
        self.last_id += 1
        message = SimpleNamespace(
            id=self.last_id,
            message=text,
            out=True,
            reply_to_msg_id=0,
            sender_id=1,
        )
        self.messages[message.id] = message
        return message

    async def get_messages(self, entity, *, ids=None, limit=None):
        assert entity is self.entity
        if ids is not None:
            return self.messages.get(ids)
        if limit is not None:
            return sorted(self.messages.values(), key=lambda item: item.id, reverse=True)[:limit]
        raise AssertionError("unexpected get_messages request")

    def add_direct_reply(self, *, reply_to: int, text: str) -> SimpleNamespace:
        self.last_id += 1
        message = SimpleNamespace(
            id=self.last_id,
            message=text,
            out=False,
            reply_to_msg_id=reply_to,
            sender_id=self.peer_id,
        )
        self.messages[message.id] = message
        return message


def _install_private_peer(monkeypatch, client: _WorkClient) -> None:
    async def resolve(_client, raw_peer: str):
        assert _client is client
        assert raw_peer == str(client.peer_id)
        return client.entity, {"id": client.peer_id, "kind": "private", "title": "private"}

    monkeypatch.setattr(telegram_bridge, "_resolve_peer", resolve)


def _run(client: _WorkClient, config: TelegramConfig, request: dict) -> dict:
    return asyncio.run(telegram_bridge._handle_operation(client, config, request))


def _promote_identity(
    client: _WorkClient,
    config: TelegramConfig,
    delivery: StoreQuoteTelegramDelivery,
    *,
    reply: str = "да",
) -> StoreQuoteTelegramDelivery:
    """Exercise the only route from candidate peer to confirmed client."""

    identity = _identity_delivery(delivery)
    candidate = _run(
        client,
        config,
        {
            "operation": "store_quote_bind_identity_candidate",
            **_delivery_request(identity, text=True),
            "peer": str(client.peer_id),
            # A malicious/stale caller claim must not promote this route.
            "recipient_confirmed": True,
        },
    )
    assert candidate["summary"]["recipient_confirmed"] is False
    assert candidate["summary"]["identity_pending"] is True
    dry = _run(
        client,
        config,
        {
            "operation": "store_quote_send",
            **_delivery_request(identity, text=True),
            "mode": "dry_run",
            "idempotency_key": "identity-delivery-0001.dry",
            "correlation_id": "identity-correlation-0001",
        },
    )
    applied = _run(
        client,
        config,
        {
            "operation": "store_quote_send",
            **_delivery_request(identity, text=True),
            "mode": "apply",
            "idempotency_key": "identity-delivery-0001",
            "correlation_id": "identity-correlation-0001",
            "dry_run_proof": dry["summary"]["dry_run_proof"],
        },
    )
    identity_delivery_id = client.last_id
    client.add_direct_reply(reply_to=identity_delivery_id, text=reply)
    minted = _run(
        client,
        config,
        {
            "operation": "store_quote_mint_identity_receipt",
            **_delivery_request(identity),
            "delivery_ref_sha256": applied["summary"]["delivery_ref_sha256"],
            "correlation_id": "identity-correlation-0001",
        },
    )
    confirmed = _run(
        client,
        config,
        {
            "operation": "store_quote_identity_readback",
            **_delivery_request(identity),
            "delivery_ref_sha256": applied["summary"]["delivery_ref_sha256"],
            "receipt": minted["receipt"],
            "correlation_id": "identity-correlation-0001",
        },
    )
    assert confirmed["summary"]["identity_classification"] == (
        "confirmed" if reply == "да" else "declined" if reply == "нет" else "ambiguous"
    )
    return identity


def _bind_confirmed_offer(client: _WorkClient, config: TelegramConfig, delivery: StoreQuoteTelegramDelivery) -> dict:
    return _run(
        client,
        config,
        {
            "operation": "store_quote_bind_recipient",
            **_delivery_request(delivery),
            "peer": str(client.peer_id),
        },
    )


def test_identity_candidate_is_literal_pending_and_cannot_unlock_offer(monkeypatch, tmp_path) -> None:
    client = _WorkClient()
    config = _work_config(tmp_path)
    delivery = _delivery()
    identity = _identity_delivery(delivery)
    _install_private_peer(monkeypatch, client)

    non_neutral = StoreQuoteTelegramDelivery(
        quote_request_id=delivery.quote_request_id,
        estimate_revision=delivery.estimate_revision,
        published_snapshot_hash=delivery.published_snapshot_hash,
        context_hash=delivery.context_hash,
        kind="identity_prompt",
        text="Это вы оставляли заявку на запчасти?",
    )
    with pytest.raises(BridgeError, match="store_quote_identity_prompt_required"):
        _run(
            client,
            config,
            {
                "operation": "store_quote_bind_identity_candidate",
                **_delivery_request(non_neutral, text=True),
                "peer": str(client.peer_id),
            },
        )

    pending = _run(
        client,
        config,
        {
            "operation": "store_quote_bind_identity_candidate",
            **_delivery_request(identity, text=True),
            "peer": str(client.peer_id),
            # This legacy/caller claim is ignored, not an authority grant.
            "recipient_confirmed": True,
        },
    )
    assert pending["summary"]["recipient_confirmed"] is False
    assert pending["summary"]["identity_pending"] is True
    with pytest.raises(BridgeError, match="store_quote_recipient_not_confirmed"):
        _bind_confirmed_offer(client, config, delivery)


def test_identity_confirmation_cannot_cross_changed_published_snapshot_or_revision(monkeypatch, tmp_path) -> None:
    client = _WorkClient()
    config = _work_config(tmp_path)
    delivery = _delivery()
    _install_private_peer(monkeypatch, client)
    _promote_identity(client, config, delivery)

    changed_snapshot = StoreQuoteTelegramDelivery(
        quote_request_id=delivery.quote_request_id,
        estimate_revision=delivery.estimate_revision,
        published_snapshot_hash="c" * 64,
        context_hash=delivery.context_hash,
        kind="offer",
        text=delivery.text,
    )
    changed_revision = StoreQuoteTelegramDelivery(
        quote_request_id=delivery.quote_request_id,
        estimate_revision="revision-0002",
        published_snapshot_hash=delivery.published_snapshot_hash,
        context_hash=delivery.context_hash,
        kind="offer",
        text=delivery.text,
    )
    assert changed_snapshot.route_binding_sha256 != delivery.route_binding_sha256
    assert changed_revision.route_binding_sha256 != delivery.route_binding_sha256
    for changed in (changed_snapshot, changed_revision):
        with pytest.raises(BridgeError, match="store_quote_route_binding_missing"):
            _bind_confirmed_offer(client, config, changed)


def test_identity_receipt_requires_direct_reply_and_is_one_time(monkeypatch, tmp_path) -> None:
    client = _WorkClient()
    config = _work_config(tmp_path)
    delivery = _delivery()
    identity = _identity_delivery(delivery)
    _install_private_peer(monkeypatch, client)
    _run(
        client,
        config,
        {
            "operation": "store_quote_bind_identity_candidate",
            **_delivery_request(identity, text=True),
            "peer": str(client.peer_id),
        },
    )
    dry = _run(
        client,
        config,
        {
            "operation": "store_quote_send",
            **_delivery_request(identity, text=True),
            "mode": "dry_run",
            "idempotency_key": "identity-direct-0001.dry",
            "correlation_id": "identity-direct-correlation-0001",
        },
    )
    applied = _run(
        client,
        config,
        {
            "operation": "store_quote_send",
            **_delivery_request(identity, text=True),
            "mode": "apply",
            "idempotency_key": "identity-direct-0001",
            "correlation_id": "identity-direct-correlation-0001",
            "dry_run_proof": dry["summary"]["dry_run_proof"],
        },
    )
    identity_message_id = client.last_id
    mint_request = {
        "operation": "store_quote_mint_identity_receipt",
        **_delivery_request(identity),
        "delivery_ref_sha256": applied["summary"]["delivery_ref_sha256"],
        "correlation_id": "identity-direct-correlation-0001",
    }
    client.add_direct_reply(reply_to=999, text="да")
    with pytest.raises(BridgeError, match="store_quote_identity_reply_not_unique"):
        _run(client, config, mint_request)

    client.add_direct_reply(reply_to=identity_message_id, text="да")
    minted = _run(client, config, mint_request)
    assert "peer" not in json.dumps(minted) and "да" not in json.dumps(minted)
    readback_request = {
        "operation": "store_quote_identity_readback",
        **_delivery_request(identity),
        "delivery_ref_sha256": applied["summary"]["delivery_ref_sha256"],
        "receipt": minted["receipt"],
        "correlation_id": "identity-direct-correlation-0001",
    }
    confirmed = _run(client, config, readback_request)
    assert confirmed["summary"]["identity_confirmed"] is True
    assert confirmed["summary"]["recipient_confirmed"] is True
    with pytest.raises(BridgeError, match="store_quote_identity_reply_consumed"):
        _run(client, config, readback_request)


def test_identity_receipt_expiry_keeps_tombstone_and_does_not_promote(monkeypatch, tmp_path) -> None:
    client = _WorkClient()
    config = _work_config(tmp_path)
    delivery = _delivery()
    identity = _identity_delivery(delivery)
    _install_private_peer(monkeypatch, client)
    _run(
        client,
        config,
        {
            "operation": "store_quote_bind_identity_candidate",
            **_delivery_request(identity, text=True),
            "peer": str(client.peer_id),
        },
    )
    dry = _run(
        client,
        config,
        {
            "operation": "store_quote_send",
            **_delivery_request(identity, text=True),
            "mode": "dry_run",
            "idempotency_key": "identity-expiry-0001.dry",
            "correlation_id": "identity-expiry-correlation-0001",
        },
    )
    applied = _run(
        client,
        config,
        {
            "operation": "store_quote_send",
            **_delivery_request(identity, text=True),
            "mode": "apply",
            "idempotency_key": "identity-expiry-0001",
            "correlation_id": "identity-expiry-correlation-0001",
            "dry_run_proof": dry["summary"]["dry_run_proof"],
        },
    )
    client.add_direct_reply(reply_to=client.last_id, text="да")
    mint_request = {
        "operation": "store_quote_mint_identity_receipt",
        **_delivery_request(identity),
        "delivery_ref_sha256": applied["summary"]["delivery_ref_sha256"],
        "correlation_id": "identity-expiry-correlation-0001",
    }
    minted = _run(client, config, mint_request)
    state_path = config.state_dir / telegram_bridge.STORE_QUOTE_TRANSPORT_STATE_FILE
    issued_at = json.loads(state_path.read_text(encoding="utf-8"))["receipts"][minted["receipt"]]["issued_at"]
    monkeypatch.setattr(
        telegram_bridge.time,
        "time",
        lambda: issued_at + telegram_bridge.STORE_QUOTE_RECEIPT_TTL_SECONDS + 1,
    )
    with pytest.raises(BridgeError, match="store_quote_identity_reply_expired"):
        _run(
            client,
            config,
            {
                "operation": "store_quote_identity_readback",
                **_delivery_request(identity),
                "delivery_ref_sha256": applied["summary"]["delivery_ref_sha256"],
                "receipt": minted["receipt"],
                "correlation_id": "identity-expiry-correlation-0001",
            },
        )
    with pytest.raises(BridgeError, match="store_quote_identity_reply_expired"):
        _run(client, config, mint_request)
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["routes"][identity.route_binding_sha256]["recipient_confirmed"] is False
    assert minted["receipt"] in saved["receipts"]


def test_typed_work_quote_delivery_and_inbound_receipt_are_bound_and_redacted(monkeypatch, tmp_path) -> None:
    client = _WorkClient()
    config = _work_config(tmp_path)
    delivery = _delivery()
    _install_private_peer(monkeypatch, client)

    _promote_identity(client, config, delivery)
    bind = _bind_confirmed_offer(client, config, delivery)
    assert bind["summary"]["recipient_bound"] is True
    assert "peer" not in json.dumps(bind)

    dry = _run(
        client,
        config,
        {
            "operation": "store_quote_send",
            **_delivery_request(delivery, text=True),
            "mode": "dry_run",
            "idempotency_key": "quote-delivery-0001",
            "correlation_id": "quote-correlation-0001",
        },
    )
    proof = dry["summary"]["dry_run_proof"]
    assert proof != delivery.text and len(proof) == 64
    assert delivery.text not in json.dumps(dry)

    applied = _run(
        client,
        config,
        {
            "operation": "store_quote_send",
            **_delivery_request(delivery, text=True),
            "mode": "apply",
            "idempotency_key": "quote-delivery-0001",
            "correlation_id": "quote-correlation-0001",
            "dry_run_proof": proof,
        },
    )
    delivery_ref = applied["summary"]["delivery_ref_sha256"]
    offer_delivery_id = client.last_id
    assert applied["summary"]["delivery_confirmed"] is True
    assert delivery.text not in json.dumps(applied)

    # A resumed workflow first repeats dry-run, then uses the original apply
    # key.  It must reconcile the single delivery rather than reject or resend.
    retry_dry = _run(
        client,
        config,
        {
            "operation": "store_quote_send",
            **_delivery_request(delivery, text=True),
            "mode": "dry_run",
            "idempotency_key": "quote-delivery-0001.dry",
            "correlation_id": "quote-correlation-0001",
        },
    )
    retry_apply = _run(
        client,
        config,
        {
            "operation": "store_quote_send",
            **_delivery_request(delivery, text=True),
            "mode": "apply",
            "idempotency_key": "quote-delivery-0001",
            "correlation_id": "quote-correlation-0001",
            "dry_run_proof": retry_dry["summary"]["dry_run_proof"],
        },
    )
    assert retry_apply["replayed"] is True
    assert client.last_id == offer_delivery_id

    readback = _run(
        client,
        config,
        {
            "operation": "store_quote_readback",
            **_delivery_request(delivery),
            "correlation_id": "quote-correlation-0001",
        },
    )
    assert all(
        readback["summary"][key] is True
        for key in (
            "recipient_confirmed",
            "private_target_confirmed",
            "unique_target_confirmed",
            "work_account_confirmed",
            "delivery_confirmed",
        )
    )
    client.add_direct_reply(reply_to=offer_delivery_id, text="Да, оформляем")

    minted = _run(
        client,
        config,
        {
            "operation": "store_quote_mint_inbound_receipt",
            **_delivery_request(delivery),
            "delivery_ref_sha256": delivery_ref,
            "correlation_id": "quote-correlation-0001",
        },
    )
    receipt = minted["receipt"]
    assert minted["summary"]["reply_classification"] == "consent"
    assert "оформляем" not in json.dumps(minted).casefold()

    reply_readback = _run(
        client,
        config,
        {
            "operation": "store_quote_reply_readback",
            "quote_ref_sha256": _delivery_request(delivery)["quote_ref_sha256"],
            "revision_sha256": _delivery_request(delivery)["revision_sha256"],
            "published_snapshot_hash": _SNAPSHOT,
            "context_hash": _CONTEXT,
            "delivery_binding_sha256": delivery.binding_sha256,
            "route_binding_sha256": delivery.route_binding_sha256,
            "delivery_ref_sha256": delivery_ref,
            "receipt": receipt,
            "reply_classification": "consent",
            "correlation_id": "quote-correlation-0001",
        },
    )
    summary = reply_readback["summary"]
    assert summary["reply_confirmed"] is True
    assert summary["reply_sender_matches_delivery"] is True
    assert len(summary["inbound_binding_sha256"]) == 64
    assert "оформляем" not in json.dumps(reply_readback).casefold()

    state = (config.state_dir / telegram_bridge.STORE_QUOTE_TRANSPORT_STATE_FILE).read_text(encoding="utf-8")
    assert delivery.text not in state
    assert delivery.quote_request_id not in state
    assert "Да, оформляем" not in state

    with pytest.raises(BridgeError, match="store_quote_inbound_receipt_consumed"):
        _run(
            client,
            config,
            {
                "operation": "store_quote_reply_readback",
                "quote_ref_sha256": _delivery_request(delivery)["quote_ref_sha256"],
                "revision_sha256": _delivery_request(delivery)["revision_sha256"],
                "published_snapshot_hash": _SNAPSHOT,
                "context_hash": _CONTEXT,
                "delivery_binding_sha256": delivery.binding_sha256,
                "route_binding_sha256": delivery.route_binding_sha256,
                "delivery_ref_sha256": delivery_ref,
                "receipt": receipt,
                "reply_classification": "consent",
                "correlation_id": "quote-correlation-0001",
            },
        )


def test_typed_reply_category_is_verified_not_caller_asserted(monkeypatch, tmp_path) -> None:
    client = _WorkClient()
    config = _work_config(tmp_path)
    delivery = _delivery()
    _install_private_peer(monkeypatch, client)
    _promote_identity(client, config, delivery)
    _bind_confirmed_offer(client, config, delivery)
    dry = _run(
        client,
        config,
        {
            "operation": "store_quote_send",
            **_delivery_request(delivery, text=True),
            "mode": "dry_run",
            "idempotency_key": "quote-delivery-0002",
            "correlation_id": "quote-correlation-0002",
        },
    )
    applied = _run(
        client,
        config,
        {
            "operation": "store_quote_send",
            **_delivery_request(delivery, text=True),
            "mode": "apply",
            "idempotency_key": "quote-delivery-0002",
            "correlation_id": "quote-correlation-0002",
            "dry_run_proof": dry["summary"]["dry_run_proof"],
        },
    )
    client.add_direct_reply(reply_to=client.last_id, text="Подскажи срок?")
    minted = _run(
        client,
        config,
        {
            "operation": "store_quote_mint_inbound_receipt",
            **_delivery_request(delivery),
            "delivery_ref_sha256": applied["summary"]["delivery_ref_sha256"],
            "correlation_id": "quote-correlation-0002",
        },
    )
    assert minted["summary"]["reply_classification"] == "clarification"
    bad = {
        "operation": "store_quote_reply_readback",
        "quote_ref_sha256": _delivery_request(delivery)["quote_ref_sha256"],
        "revision_sha256": _delivery_request(delivery)["revision_sha256"],
        "published_snapshot_hash": _SNAPSHOT,
        "context_hash": _CONTEXT,
        "delivery_binding_sha256": delivery.binding_sha256,
        "route_binding_sha256": delivery.route_binding_sha256,
        "delivery_ref_sha256": applied["summary"]["delivery_ref_sha256"],
        "receipt": minted["receipt"],
        "reply_classification": "consent",
        "correlation_id": "quote-correlation-0002",
    }
    with pytest.raises(BridgeError, match="store_quote_inbound_binding_invalid"):
        _run(client, config, bad)
    good = _run(client, config, bad | {"reply_classification": "clarification"})
    assert good["summary"]["reply_classification"] == "clarification"


def test_expired_inbound_receipt_is_tombstoned_and_cannot_be_reminted(monkeypatch, tmp_path) -> None:
    client = _WorkClient()
    config = _work_config(tmp_path)
    delivery = _delivery()
    _install_private_peer(monkeypatch, client)
    _promote_identity(client, config, delivery)
    _bind_confirmed_offer(client, config, delivery)
    dry = _run(
        client,
        config,
        {
            "operation": "store_quote_send",
            **_delivery_request(delivery, text=True),
            "mode": "dry_run",
            "idempotency_key": "quote-delivery-0007.dry",
            "correlation_id": "quote-correlation-0007",
        },
    )
    applied = _run(
        client,
        config,
        {
            "operation": "store_quote_send",
            **_delivery_request(delivery, text=True),
            "mode": "apply",
            "idempotency_key": "quote-delivery-0007",
            "correlation_id": "quote-correlation-0007",
            "dry_run_proof": dry["summary"]["dry_run_proof"],
        },
    )
    client.add_direct_reply(reply_to=client.last_id, text="Да, оформляем")
    mint_request = {
        "operation": "store_quote_mint_inbound_receipt",
        **_delivery_request(delivery),
        "delivery_ref_sha256": applied["summary"]["delivery_ref_sha256"],
        "correlation_id": "quote-correlation-0007",
    }
    minted = _run(client, config, mint_request)
    state_path = config.state_dir / telegram_bridge.STORE_QUOTE_TRANSPORT_STATE_FILE
    issued_at = json.loads(state_path.read_text(encoding="utf-8"))["receipts"][minted["receipt"]]["issued_at"]
    monkeypatch.setattr(
        telegram_bridge.time,
        "time",
        lambda: issued_at + telegram_bridge.STORE_QUOTE_RECEIPT_TTL_SECONDS + 1,
    )

    with pytest.raises(BridgeError, match="store_quote_inbound_reply_expired"):
        _run(client, config, mint_request)

    # The capability expires, but its hash-only tombstone remains until the
    # delivery binding expires and prevents scanning the old customer reply
    # into a new receipt.
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert minted["receipt"] in saved["receipts"]


def test_typed_store_quote_rpc_is_work_only(monkeypatch, tmp_path) -> None:
    client = _WorkClient()
    _install_private_peer(monkeypatch, client)
    with pytest.raises(BridgeError, match="store_quote_work_account_required"):
        _run(
            client,
            _work_config(tmp_path, account="personal"),
            {
                "operation": "store_quote_bind_identity_candidate",
                **_delivery_request(_identity_delivery(_delivery()), text=True),
                "peer": str(client.peer_id),
            },
        )


def test_manager_transport_is_explicit_and_redacts_injected_bridge_response() -> None:
    delivery = _delivery()
    calls: list[dict] = []

    def rpc(request: dict) -> dict:
        calls.append(request)
        return {
            "ok": True,
            "mode": "dry_run",
            "target": {"id": 77, "title": "secret"},
            "message_id": 100,
            "text": delivery.text,
            "summary": {
                **_delivery_request(delivery),
                "dry_run_proof": "c" * 64,
                "recipient_confirmed": True,
            },
        }

    with pytest.raises(ValueError, match="socket_required"):
        create_work_store_quote_transport()
    transport = create_work_store_quote_transport(rpc=rpc)
    result = transport.send_work_quote_message(
        delivery=delivery,
        idempotency_key="quote-delivery-0003",
        correlation_id="quote-correlation-0003",
        mode="dry_run",
    )
    assert calls[0]["operation"] == "store_quote_send"
    assert calls[0]["quote_ref_sha256"] != delivery.quote_request_id
    assert calls[0]["text"] == delivery.text
    assert result["summary"]["dry_run_proof"] == "c" * 64
    assert "target" not in result and "text" not in result and "message_id" not in result


def test_manager_transport_reply_readback_uses_only_receipt_and_hashes() -> None:
    delivery = _delivery()
    inbound = StoreQuoteTelegramInboundReply(
        quote_request_id=delivery.quote_request_id,
        revision_sha256=telegram_bridge._store_quote_sha256(delivery.estimate_revision),
        published_snapshot_hash=delivery.published_snapshot_hash,
        context_hash=delivery.context_hash,
        delivery_binding_sha256=delivery.binding_sha256,
        delivery_ref_sha256="d" * 64,
        classification="consent",
        receipt="receipt-token-1234567890123456",
    )
    seen: dict = {}

    def rpc(request: dict) -> dict:
        seen.update(request)
        return {
            "ok": True,
            "summary": {
                "quote_ref_sha256": request["quote_ref_sha256"],
                "revision_sha256": request["revision_sha256"],
                "published_snapshot_hash": request["published_snapshot_hash"],
                "context_hash": request["context_hash"],
                "delivery_binding_sha256": request["delivery_binding_sha256"],
                "delivery_ref_sha256": request["delivery_ref_sha256"],
                "reply_classification": "consent",
                "reply_text_sha256": "e" * 64,
                "incoming_ref_sha256": "f" * 64,
                "inbound_binding_sha256": "1" * 64,
                "recipient_confirmed": True,
                "private_target_confirmed": True,
                "unique_target_confirmed": True,
                "work_account_confirmed": True,
                "delivery_confirmed": True,
                "reply_confirmed": True,
                "reply_sender_matches_delivery": True,
            },
        }

    result = create_work_store_quote_transport(rpc=rpc).readback_work_quote_reply(
        inbound=inbound,
        correlation_id="quote-correlation-0004",
    )
    assert seen["operation"] == "store_quote_reply_readback"
    assert "quote_request_id" not in seen and "text" not in seen and "peer" not in seen
    assert result["summary"]["reply_classification"] == "consent"


def test_manager_transport_preserves_only_safe_uncertain_send_outcome() -> None:
    delivery = _delivery()

    def rpc(_request: dict) -> dict:
        return {
            "ok": False,
            "target": {"id": 77},
            "text": delivery.text,
            "summary": {"error_code": "store_quote_send_readback_failed"},
            "meta": {"outcome_uncertain": True, "peer_id": 77},
        }

    result = create_work_store_quote_transport(rpc=rpc).send_work_quote_message(
        delivery=delivery,
        idempotency_key="quote-delivery-0006",
        correlation_id="quote-correlation-0006",
        mode="apply",
        dry_run_proof="d" * 64,
    )
    assert result == {
        "ok": False,
        "summary": {"error_code": "store_quote_send_readback_failed"},
        "meta": {"outcome_uncertain": True},
    }


def test_manager_transport_translates_a_local_bridge_error_to_safe_failure() -> None:
    def rpc(_request: dict) -> dict:
        raise BridgeError("bridge_unavailable")

    result = create_work_store_quote_transport(rpc=rpc).readback_work_quote_message(
        delivery=_delivery(),
        correlation_id="quote-correlation-0008",
    )
    assert result == {
        "ok": False,
        "summary": {"error_code": "store_quote_telegram_bridge_unavailable"},
    }


def test_manager_transport_projection_matches_the_conductor_contract(monkeypatch, tmp_path) -> None:
    client = _WorkClient()
    config = _work_config(tmp_path)
    delivery = _delivery()
    _install_private_peer(monkeypatch, client)

    def rpc(request: dict) -> dict:
        return _run(client, config, request)

    transport = create_work_store_quote_transport(rpc=rpc)
    identity = _identity_delivery(delivery)
    pending = transport.bind_work_quote_identity_candidate(
        delivery=identity,
        peer=str(client.peer_id),
    )
    assert pending["summary"]["recipient_confirmed"] is False
    identity_dry = transport.send_work_quote_message(
        delivery=identity,
        idempotency_key="identity-delivery-0005.dry",
        correlation_id="identity-correlation-0005",
        mode="dry_run",
    )
    identity_applied = transport.send_work_quote_message(
        delivery=identity,
        idempotency_key="identity-delivery-0005",
        correlation_id="identity-correlation-0005",
        mode="apply",
        dry_run_proof=identity_dry["summary"]["dry_run_proof"],
    )
    client.add_direct_reply(reply_to=client.last_id, text="да")
    identity_receipt = transport.mint_work_quote_identity_receipt(
        delivery=identity,
        delivery_ref_sha256=identity_applied["summary"]["delivery_ref_sha256"],
        correlation_id="identity-correlation-0005",
    )
    identity_confirmed = transport.readback_work_quote_identity_reply(
        delivery=identity,
        delivery_ref_sha256=identity_applied["summary"]["delivery_ref_sha256"],
        receipt=identity_receipt["receipt"],
        correlation_id="identity-correlation-0005",
    )
    assert identity_confirmed["summary"]["identity_confirmed"] is True
    bound = transport.bind_work_quote_recipient(
        delivery=delivery,
        peer=str(client.peer_id),
    )
    assert bound["summary"]["recipient_bound"] is True
    dry = transport.send_work_quote_message(
        delivery=delivery,
        idempotency_key="quote-delivery-0005.dry",
        correlation_id="quote-correlation-0005",
        mode="dry_run",
    )
    assert store_quote_conductor._telegram_delivery_projection_matches(dry, delivery, require_delivery=False)
    applied = transport.send_work_quote_message(
        delivery=delivery,
        idempotency_key="quote-delivery-0005",
        correlation_id="quote-correlation-0005",
        mode="apply",
        dry_run_proof=dry["summary"]["dry_run_proof"],
    )
    readback = transport.readback_work_quote_message(
        delivery=delivery,
        correlation_id="quote-correlation-0005",
    )
    assert applied["ok"] is True
    assert store_quote_conductor._telegram_delivery_readback_matches(readback, delivery)
    client.add_direct_reply(reply_to=client.last_id, text="Да, оформляем")
    minted = transport.mint_work_quote_inbound_receipt(
        delivery=delivery,
        delivery_ref_sha256=readback["summary"]["delivery_ref_sha256"],
        correlation_id="quote-correlation-0005",
    )
    inbound = StoreQuoteTelegramInboundReply(
        quote_request_id=delivery.quote_request_id,
        revision_sha256=telegram_bridge._store_quote_sha256(delivery.estimate_revision),
        published_snapshot_hash=delivery.published_snapshot_hash,
        context_hash=delivery.context_hash,
        delivery_binding_sha256=delivery.binding_sha256,
        delivery_ref_sha256=readback["summary"]["delivery_ref_sha256"],
        classification=minted["summary"]["reply_classification"],
        receipt=minted["receipt"],
    )
    reply = transport.readback_work_quote_reply(
        inbound=inbound,
        correlation_id="quote-correlation-0005",
    )
    assert store_quote_conductor._telegram_reply_readback_refs(reply, inbound) is not None
