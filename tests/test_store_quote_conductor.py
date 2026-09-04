from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pytest

from autostop_manager.storage import ManagerMemoryStore
from autostop_manager.store_quote_conductor import (
    StoreQuoteConductor,
    StoreQuoteOwnerApi,
    StoreQuoteTelegramDelivery,
    StoreQuoteTelegramInboundReply,
    assess_quote_evidence,
)


_SNAPSHOT_HASH = "b" * 64
_CONSENT_HASH = "c" * 64
_TELEGRAM_CONTEXT_HASH = "d" * 64
_ENTRIES = [{"type": "position", "name": "part-one", "cost": "1300.00", "quantity": 1}]
_COVERAGE = [{"requestItemId": "item-1", "state": "OFFERED"}]
_EVIDENCE = {
    "fitment_confirmed": True,
    "catalog_number_confirmed": True,
    "availability_confirmed": True,
    "delivery_confirmed": True,
    "customer_price_confirmed": True,
    "warranty_confirmed": True,
    "offer_count": 2,
    "recommendation_basis": "quality",
    "handoff_reasons": [],
}


class _QuoteGateway:
    def __init__(self, *, manual: bool = False, has_offers: bool = False) -> None:
        self.revision = "revision-1"
        self.status = "WAITING_FOR_QUOTE"
        self.converted_order_id: str | None = None
        self.has_offers = has_offers
        self.estimate: dict | None = None
        if manual:
            self.estimate = {
                "entries": deepcopy(_ENTRIES),
                "coverage": deepcopy(_COVERAGE),
                "provenance": "MANUAL",
                "publishedSnapshotHash": None,
            }
        self.calls: list[tuple[str, dict]] = []

    def _result(self) -> dict:
        return {
            "ok": True,
            "data": {
                "quoteRequestId": "quote-1",
                "revision": self.revision,
                "status": self.status,
                "convertedOrderId": self.converted_order_id,
                "hasQuoteOffers": self.has_offers,
                "estimate": deepcopy(self.estimate),
            },
        }

    def get_estimate_draft(self, *, quote_request_id: str) -> dict:
        self.calls.append(("get", {"quote_request_id": quote_request_id}))
        return self._result()

    def replace_estimate_draft(self, **kwargs) -> dict:
        self.calls.append(("replace", deepcopy(kwargs)))
        if kwargs["mode"] == "apply":
            self.revision = "revision-2"
            self.status = "WAITING_FOR_QUOTE"
            self.estimate = {
                "entries": deepcopy(kwargs["entries"]),
                "coverage": deepcopy(kwargs["coverage"]),
                "provenance": "AUTOSTOP_MANAGER",
                "publishedSnapshotHash": None,
            }
        return {"ok": True, "summary": {"dry_run_proof": "a" * 64}}

    def submit_estimate(self, **kwargs) -> dict:
        self.calls.append(("submit", deepcopy(kwargs)))
        if kwargs["mode"] == "apply":
            self.revision = "revision-3"
            self.status = "WAITING_FOR_APPROVAL"
            assert self.estimate is not None
            self.estimate["publishedSnapshotHash"] = _SNAPSHOT_HASH
        return {"ok": True, "summary": {"dry_run_proof": "d" * 64}}

    def reopen_estimate(self, **kwargs) -> dict:
        self.calls.append(("reopen", deepcopy(kwargs)))
        if kwargs["mode"] == "apply":
            self.revision = "revision-4"
            self.status = "WAITING_FOR_QUOTE"
            assert self.estimate is not None
            self.estimate["publishedSnapshotHash"] = None
        return {"ok": True, "summary": {"dry_run_proof": "e" * 64}}

    def confirm_estimate_order_from_telegram(self, **kwargs) -> dict:
        self.calls.append(("confirm", deepcopy(kwargs)))
        if kwargs["mode"] == "apply":
            self.revision = "revision-4"
            self.status = "APPROVED"
            self.converted_order_id = "order-1"
        return {
            "ok": True,
            "summary": {"dry_run_proof": "f" * 64},
            "data": {"orderStatus": "WAITING_FOR_PAYMENT"},
        }


class _QuoteTelegramSender:
    def __init__(
        self,
        *,
        confirmed: bool = True,
        mutate_readback: bool = False,
        mutate_reply_readback: bool = False,
    ) -> None:
        self.confirmed = confirmed
        self.mutate_readback = mutate_readback
        self.mutate_reply_readback = mutate_reply_readback
        self.calls: list[tuple[str, dict]] = []

    @staticmethod
    def _projection(delivery) -> dict:
        return {
            "quote_ref_sha256": hashlib.sha256(
                f"store-quote-conductor-v1\0{delivery.quote_request_id}".encode()
            ).hexdigest(),
            "revision_sha256": hashlib.sha256(delivery.estimate_revision.encode()).hexdigest(),
            "message_sha256": delivery.text_sha256,
            "context_hash": delivery.context_hash,
            "published_snapshot_hash": delivery.published_snapshot_hash,
            "delivery_binding_sha256": delivery.binding_sha256,
            "route_binding_sha256": delivery.route_binding_sha256,
        }

    def send_work_quote_message(self, **kwargs) -> dict:
        self.calls.append(("send", dict(kwargs)))
        proof = "e" * 64 if kwargs["mode"] == "dry_run" else "f" * 64
        return {"ok": True, "summary": {"dry_run_proof": proof, **self._projection(kwargs["delivery"])}}

    def readback_work_quote_message(self, **kwargs) -> dict:
        self.calls.append(("readback", dict(kwargs)))
        delivery = kwargs["delivery"]
        projection = self._projection(delivery)
        if self.mutate_readback:
            projection["delivery_binding_sha256"] = "a" * 64
        return {
            "ok": True,
            "summary": {
                "recipient_confirmed": self.confirmed,
                "private_target_confirmed": self.confirmed,
                "unique_target_confirmed": self.confirmed,
                "work_account_confirmed": self.confirmed,
                "delivery_confirmed": self.confirmed,
                **projection,
                "delivery_ref_sha256": "f" * 64,
            },
        }

    def readback_work_quote_reply(self, **kwargs) -> dict:
        self.calls.append(("reply_readback", dict(kwargs)))
        inbound = kwargs["inbound"]
        reply_text_sha256 = "1" * 64
        incoming_ref_sha256 = "2" * 64
        binding_payload = {
            "quote_ref_sha256": hashlib.sha256(
                f"store-quote-conductor-v1\0{inbound.quote_request_id}".encode()
            ).hexdigest(),
            "revision_sha256": inbound.revision_sha256,
            "published_snapshot_hash": inbound.published_snapshot_hash,
            "telegram_context_hash": inbound.context_hash,
            "delivery_binding_sha256": inbound.delivery_binding_sha256,
            "delivery_ref_sha256": inbound.delivery_ref_sha256,
            "reply_classification": inbound.classification,
            "reply_text_sha256": reply_text_sha256,
            "incoming_ref_sha256": incoming_ref_sha256,
        }
        inbound_binding_sha256 = hashlib.sha256(
            json.dumps(binding_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if self.mutate_reply_readback:
            inbound_binding_sha256 = "3" * 64
        return {
            "ok": True,
            "summary": {
                "recipient_confirmed": self.confirmed,
                "private_target_confirmed": self.confirmed,
                "unique_target_confirmed": self.confirmed,
                "work_account_confirmed": self.confirmed,
                "delivery_confirmed": self.confirmed,
                "reply_confirmed": self.confirmed,
                "reply_sender_matches_delivery": self.confirmed,
                "quote_ref_sha256": binding_payload["quote_ref_sha256"],
                "revision_sha256": inbound.revision_sha256,
                "published_snapshot_hash": inbound.published_snapshot_hash,
                "context_hash": inbound.context_hash,
                "delivery_binding_sha256": inbound.delivery_binding_sha256,
                "delivery_ref_sha256": inbound.delivery_ref_sha256,
                "reply_classification": inbound.classification,
                "reply_text_sha256": reply_text_sha256,
                "incoming_ref_sha256": incoming_ref_sha256,
                "inbound_binding_sha256": inbound_binding_sha256,
            },
        }


def _conductor(
    tmp_path,
    gateway: _QuoteGateway | None = None,
    telegram_sender: _QuoteTelegramSender | None = None,
) -> tuple[StoreQuoteConductor, _QuoteGateway, ManagerMemoryStore]:
    fake = gateway or _QuoteGateway()
    store = ManagerMemoryStore(tmp_path / "manager.sqlite3")
    return (
        StoreQuoteConductor(
            store=store,
            gateway=fake,
            telegram_sender=telegram_sender or _QuoteTelegramSender(),
        ),
        fake,
        store,
    )


def _start(conductor: StoreQuoteConductor) -> dict:
    result = conductor.execute(
        operation="start",
        quote_request_id="quote-1",
        idempotency_key="quote-start-001",
        correlation_id="quote-correlation-001",
    )
    assert result["ok"] is True
    return result


def _evidence_ready(conductor: StoreQuoteConductor) -> dict:
    started = _start(conductor)
    result = conductor.execute(
        operation="evidence",
        quote_request_id="quote-1",
        run_id=started["run_id"],
        expected_state_version=started["summary"]["state_version"],
        evidence=deepcopy(_EVIDENCE),
    )
    assert result["ok"] is True
    assert result["summary"]["phase"] == "evidence_ready"
    return result


def _draft_saved(conductor: StoreQuoteConductor) -> dict:
    ready = _evidence_ready(conductor)
    result = conductor.execute(
        operation="draft",
        quote_request_id="quote-1",
        run_id=ready["run_id"],
        expected_state_version=ready["summary"]["state_version"],
        expected_revision="revision-1",
        idempotency_key="quote-draft-001",
        correlation_id="quote-correlation-001",
        entries=deepcopy(_ENTRIES),
        coverage=deepcopy(_COVERAGE),
    )
    assert result["ok"] is True
    assert result["summary"]["phase"] == "draft_saved"
    return result


def _published(conductor: StoreQuoteConductor) -> dict:
    drafted = _draft_saved(conductor)
    result = conductor.execute(
        operation="publish",
        quote_request_id="quote-1",
        run_id=drafted["run_id"],
        expected_state_version=drafted["summary"]["state_version"],
        expected_revision="revision-2",
        idempotency_key="quote-publish-001",
        correlation_id="quote-correlation-001",
        customer_response="client-visible response",
    )
    assert result["ok"] is True
    assert result["summary"]["phase"] == "published"
    return result


def test_conductor_runs_verified_estimate_to_one_waiting_payment_order_without_ledger_pii(tmp_path):
    conductor, gateway, store = _conductor(tmp_path)
    published = _published(conductor)

    waiting = conductor.execute(
        operation="wait",
        quote_request_id="quote-1",
        run_id=published["run_id"],
        expected_state_version=published["summary"]["state_version"],
        expected_revision="revision-3",
        published_snapshot_hash=_SNAPSHOT_HASH,
        telegram_context_hash=_TELEGRAM_CONTEXT_HASH,
        telegram_message="Глянул: нормальный вариант. Его оформляем?",
        telegram_message_kind="offer",
    )
    assert waiting["ok"] is True
    assert waiting["status"] == "external_wait"

    consent = conductor.execute(
        operation="reply",
        quote_request_id="quote-1",
        run_id=waiting["run_id"],
        expected_state_version=waiting["summary"]["state_version"],
        step_id=waiting["summary"]["step_id"],
        reply_classification="consent",
        telegram_inbound_receipt="telegram-receipt-0001",
    )
    assert consent["ok"] is True
    assert consent["summary"]["phase"] == "published"
    assert consent["summary"]["consent_context_hash"] != _CONSENT_HASH

    ordered = conductor.execute(
        operation="order",
        quote_request_id="quote-1",
        run_id=consent["run_id"],
        expected_state_version=consent["summary"]["state_version"],
        expected_revision="revision-3",
        idempotency_key="quote-order-001",
        correlation_id="quote-correlation-001",
        consent_context_hash=consent["summary"]["consent_context_hash"],
        published_snapshot_hash=_SNAPSHOT_HASH,
    )
    assert ordered["ok"] is True
    assert ordered["summary"]["phase"] == "waiting_payment"
    assert ordered["summary"]["technical_review"] == {
        "route_version": "store_quote_conductor_v1",
        "outcome": "waiting_payment",
        "failure_class": "none",
        "offers": 2,
        "entries": 1,
        "coverage": 1,
    }
    assert [name for name, _ in gateway.calls].count("confirm") == 2
    assert gateway.status == "APPROVED"

    persisted = store.get_manager_run(ordered["run_id"], include_events=True, include_external_steps=True)
    serialized = json.dumps(persisted, ensure_ascii=False)
    assert "part-one" not in serialized
    assert "1300.00" not in serialized
    assert "client-visible response" not in serialized
    assert "quote-1" not in serialized
    assert "telegram-receipt-0001" not in serialized


def test_conductor_deduplicates_one_active_quote_run_and_rejects_stale_state(tmp_path):
    conductor, _, _ = _conductor(tmp_path)
    started = _start(conductor)
    duplicate = conductor.execute(
        operation="start",
        quote_request_id="quote-1",
        idempotency_key="quote-start-002",
        correlation_id="quote-correlation-002",
    )
    assert duplicate["ok"] is True
    assert duplicate["run_id"] == started["run_id"]
    assert duplicate["summary"]["active_target_deduplicated"] is True

    stale = conductor.execute(
        operation="evidence",
        quote_request_id="quote-1",
        run_id=started["run_id"],
        expected_state_version=started["summary"]["state_version"] - 1,
        evidence=deepcopy(_EVIDENCE),
    )
    assert stale["ok"] is False
    assert "workflow_state_conflict" in stale["warnings"]


def test_conductor_handoffs_manual_or_unsafe_evidence_without_store_write(tmp_path):
    conductor, gateway, _ = _conductor(tmp_path, _QuoteGateway(manual=True))
    manual = _start(conductor)
    assert manual["status"] == "handoff"
    assert manual["summary"]["technical_review"]["outcome"] == "handoff"
    assert manual["summary"]["technical_review"]["failure_class"] == "manual_estimate"
    assert [name for name, _ in gateway.calls] == ["get"]

    conductor, gateway, _ = _conductor(tmp_path / "unsafe")
    started = _start(conductor)
    unsafe = conductor.execute(
        operation="evidence",
        quote_request_id="quote-1",
        run_id=started["run_id"],
        expected_state_version=started["summary"]["state_version"],
        evidence={**_EVIDENCE, "offer_count": 4},
    )
    assert unsafe["ok"] is True
    assert unsafe["status"] == "handoff"
    assert [name for name, _ in gateway.calls] == ["get"]


def test_conductor_rejects_caller_asserted_reply_bindings(tmp_path):
    conductor, _, _ = _conductor(tmp_path)
    published = _published(conductor)
    waiting = conductor.execute(
        operation="wait",
        quote_request_id="quote-1",
        run_id=published["run_id"],
        expected_state_version=published["summary"]["state_version"],
        expected_revision="revision-3",
        published_snapshot_hash=_SNAPSHOT_HASH,
        telegram_context_hash=_TELEGRAM_CONTEXT_HASH,
        telegram_message="Глянул: нормальный вариант. Его оформляем?",
        telegram_message_kind="offer",
    )

    stale = conductor.execute(
        operation="reply",
        quote_request_id="quote-1",
        run_id=waiting["run_id"],
        expected_state_version=waiting["summary"]["state_version"],
        step_id=waiting["summary"]["step_id"],
        reply_classification="consent",
        consent_context_hash=_CONSENT_HASH,
        published_snapshot_hash="a" * 64,
        telegram_context_hash=_TELEGRAM_CONTEXT_HASH,
    )
    assert stale["ok"] is False
    assert "store_quote_conductor_reply_binding_must_be_transport_verified" in stale["warnings"]

    stale_context = conductor.execute(
        operation="reply",
        quote_request_id="quote-1",
        run_id=waiting["run_id"],
        expected_state_version=waiting["summary"]["state_version"],
        step_id=waiting["summary"]["step_id"],
        reply_classification="consent",
        telegram_inbound_receipt="telegram-receipt-0002",
    )
    assert stale_context["ok"] is True


def test_conductor_routes_conversation_addition_to_safe_revision_without_message_text(tmp_path):
    conductor, _, store = _conductor(tmp_path)
    published = _published(conductor)
    waiting = conductor.execute(
        operation="wait",
        quote_request_id="quote-1",
        run_id=published["run_id"],
        expected_state_version=published["summary"]["state_version"],
        expected_revision="revision-3",
        published_snapshot_hash=_SNAPSHOT_HASH,
        telegram_context_hash=_TELEGRAM_CONTEXT_HASH,
        telegram_message="Глянул: нормальный вариант. Его оформляем?",
        telegram_message_kind="offer",
    )
    revised = conductor.execute(
        operation="reply",
        quote_request_id="quote-1",
        run_id=waiting["run_id"],
        expected_state_version=waiting["summary"]["state_version"],
        step_id=waiting["summary"]["step_id"],
        reply_classification="addition",
        telegram_inbound_receipt="telegram-receipt-0003",
    )

    assert revised["ok"] is True
    assert revised["summary"]["phase"] == "revision_needed"
    persisted = json.dumps(store.get_manager_run(revised["run_id"], include_events=True), ensure_ascii=False)
    # The adapter supplies only a typed classification here; no customer text
    # or catalog payload is retained while the new item is clarified.
    assert "добавьте" not in persisted
    assert "part-one" not in persisted


def test_conductor_retries_delivery_readback_without_republishing_store_estimate(tmp_path):
    sender = _QuoteTelegramSender(confirmed=False)
    conductor, gateway, store = _conductor(tmp_path, telegram_sender=sender)
    published = _published(conductor)
    wait_args = {
        "operation": "wait",
        "quote_request_id": "quote-1",
        "run_id": published["run_id"],
        "expected_state_version": published["summary"]["state_version"],
        "expected_revision": "revision-3",
        "published_snapshot_hash": _SNAPSHOT_HASH,
        "telegram_context_hash": _TELEGRAM_CONTEXT_HASH,
        "telegram_message": "Глянул: нормальный вариант. Его оформляем?",
        "telegram_message_kind": "offer",
    }
    failed = conductor.execute(**wait_args)
    assert failed["ok"] is False
    assert failed["status"] == "retryable"
    assert failed["summary"]["phase"] == "published"
    assert [name for name, _ in gateway.calls].count("submit") == 2
    assert store.get_manager_run(published["run_id"], include_external_steps=True)["item"]["external_steps"] == []

    sender.confirmed = True
    retried = conductor.execute(**wait_args)
    assert retried["ok"] is True
    assert retried["status"] == "external_wait"
    assert [name for name, _ in gateway.calls].count("submit") == 2
    assert [name for name, _ in sender.calls].count("send") == 4
    assert [name for name, _ in sender.calls].count("readback") == 2


def test_conductor_rejects_telegram_delivery_when_apply_readback_binding_changes(tmp_path):
    sender = _QuoteTelegramSender(mutate_readback=True)
    conductor, gateway, _ = _conductor(tmp_path, telegram_sender=sender)
    published = _published(conductor)

    rejected = conductor.execute(
        operation="wait",
        quote_request_id="quote-1",
        run_id=published["run_id"],
        expected_state_version=published["summary"]["state_version"],
        expected_revision="revision-3",
        published_snapshot_hash=_SNAPSHOT_HASH,
        telegram_context_hash=_TELEGRAM_CONTEXT_HASH,
        telegram_message="Глянул: нормальный вариант. Его оформляем?",
        telegram_message_kind="offer",
    )
    assert rejected["ok"] is False
    assert "store_quote_conductor_telegram_readback_mismatch" in rejected["warnings"]
    assert [name for name, _ in gateway.calls].count("submit") == 2


def test_conductor_requires_transport_verified_inbound_reply_before_consent(tmp_path):
    sender = _QuoteTelegramSender()
    conductor, _, store = _conductor(tmp_path, telegram_sender=sender)
    published = _published(conductor)
    waiting = conductor.execute(
        operation="wait",
        quote_request_id="quote-1",
        run_id=published["run_id"],
        expected_state_version=published["summary"]["state_version"],
        expected_revision="revision-3",
        published_snapshot_hash=_SNAPSHOT_HASH,
        telegram_context_hash=_TELEGRAM_CONTEXT_HASH,
        telegram_message="Глянул: нормальный вариант. Его оформляем?",
        telegram_message_kind="offer",
    )
    reply_args = {
        "operation": "reply",
        "quote_request_id": "quote-1",
        "run_id": waiting["run_id"],
        "expected_state_version": waiting["summary"]["state_version"],
        "step_id": waiting["summary"]["step_id"],
        "reply_classification": "consent",
    }

    missing_receipt = conductor.execute(**reply_args)
    assert missing_receipt["ok"] is False
    assert "store_quote_conductor_telegram_inbound_receipt_invalid" in missing_receipt["warnings"]

    sender.mutate_reply_readback = True
    rejected = conductor.execute(**reply_args, telegram_inbound_receipt="telegram-receipt-0004")
    assert rejected["ok"] is False
    assert "store_quote_conductor_telegram_reply_readback_mismatch" in rejected["warnings"]

    conductor.telegram_sender = None
    unavailable = conductor.execute(**reply_args, telegram_inbound_receipt="telegram-receipt-0005")
    assert unavailable["ok"] is False
    assert "store_quote_conductor_telegram_reply_sender_unavailable" in unavailable["warnings"]
    persisted = json.dumps(store.get_manager_run(waiting["run_id"], include_events=True), ensure_ascii=False)
    assert "telegram-receipt-0004" not in persisted


def test_conductor_reconciles_cabinet_order_while_telegram_reply_is_pending(tmp_path):
    conductor, gateway, store = _conductor(tmp_path)
    published = _published(conductor)
    waiting = conductor.execute(
        operation="wait",
        quote_request_id="quote-1",
        run_id=published["run_id"],
        expected_state_version=published["summary"]["state_version"],
        expected_revision="revision-3",
        published_snapshot_hash=_SNAPSHOT_HASH,
        telegram_context_hash=_TELEGRAM_CONTEXT_HASH,
        telegram_message="Глянул: нормальный вариант. Его оформляем?",
        telegram_message_kind="offer",
    )
    assert waiting["status"] == "external_wait"
    gateway.revision = "revision-4"
    gateway.status = "APPROVED"
    gateway.converted_order_id = "order-created-in-cabinet"

    reconciled = conductor.execute(operation="status", quote_request_id="quote-1", run_id=waiting["run_id"])
    assert reconciled["ok"] is True
    assert reconciled["status"] == "waiting_payment"
    assert reconciled["summary"]["cabinet_order_reconciled"] is True
    assert reconciled["summary"]["technical_review"]["outcome"] == "waiting_payment"
    assert [name for name, _ in gateway.calls].count("confirm") == 0
    run = store.get_manager_run(waiting["run_id"], include_external_steps=True)["item"]
    assert run["status"] == "executing"
    assert run["external_steps"][0]["status"] == "completed"


def test_assess_quote_evidence_limits_offers_and_handoffs_on_risky_request():
    assert assess_quote_evidence(_EVIDENCE)["ok"] is True
    assert assess_quote_evidence({**_EVIDENCE, "offer_count": 4})["ok"] is False
    assert assess_quote_evidence({**_EVIDENCE, "handoff_reasons": ["discount_requested"]})["ok"] is False


def test_conductor_rejects_identity_prompt_from_published_quote_delivery(tmp_path):
    sender = _QuoteTelegramSender()
    conductor, _, store = _conductor(tmp_path, telegram_sender=sender)
    published = _published(conductor)

    result = conductor.execute(
        operation="wait",
        quote_request_id="quote-1",
        run_id=published["run_id"],
        expected_state_version=published["summary"]["state_version"],
        expected_revision="revision-3",
        published_snapshot_hash=_SNAPSHOT_HASH,
        telegram_context_hash=_TELEGRAM_CONTEXT_HASH,
        telegram_message="Привет! Вы оставляли заявку на запчасти в AutoStop?",
        telegram_message_kind="identity_prompt",
    )

    assert result["ok"] is False
    assert "store_quote_conductor_identity_prompt_requires_explicit_binding" in result["warnings"]
    assert sender.calls == []
    assert store.get_manager_run(published["run_id"], include_external_steps=True)["item"]["external_steps"] == []


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("quote_request_id", "", "quote_id_invalid"),
        ("estimate_revision", "", "expected_revision_required"),
        ("published_snapshot_hash", "not-a-hash", "published_snapshot_stale"),
        ("context_hash", "not-a-hash", "telegram_context_hash_invalid"),
        ("kind", "free-form-message", "telegram_message_kind_invalid"),
        ("text", "No question mark.", "telegram_message_invalid"),
    ],
)
def test_quote_telegram_delivery_rejects_unbound_or_non_neutral_input(field, value, error):
    payload = {
        "quote_request_id": "quote-1",
        "estimate_revision": "revision-1",
        "published_snapshot_hash": _SNAPSHOT_HASH,
        "context_hash": _TELEGRAM_CONTEXT_HASH,
        "kind": "offer",
        "text": "Глянул: нормальный вариант. Его оформляем?",
    }
    payload[field] = value

    with pytest.raises(ValueError, match=error):
        StoreQuoteTelegramDelivery(**payload)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("quote_request_id", "", "quote_id_invalid"),
        ("revision_sha256", "not-a-hash", "telegram_reply_binding_invalid"),
        ("classification", "unverified", "reply_classification_invalid"),
        ("receipt", "too-short", "telegram_inbound_receipt_invalid"),
    ],
)
def test_quote_telegram_inbound_receipt_rejects_unverified_binding_input(field, value, error):
    payload = {
        "quote_request_id": "quote-1",
        "revision_sha256": "a" * 64,
        "published_snapshot_hash": _SNAPSHOT_HASH,
        "context_hash": _TELEGRAM_CONTEXT_HASH,
        "delivery_binding_sha256": "b" * 64,
        "delivery_ref_sha256": "c" * 64,
        "classification": "consent",
        "receipt": "receipt-token-1234567890123456",
    }
    payload[field] = value

    with pytest.raises(ValueError, match=error):
        StoreQuoteTelegramInboundReply(**payload)


def test_conductor_rejects_unknown_and_non_apply_ledger_operations(tmp_path):
    conductor, _, _ = _conductor(tmp_path)

    unknown = conductor.execute(operation="archive", quote_request_id="quote-1")
    assert unknown["ok"] is False
    assert "store_quote_conductor_operation_invalid" in unknown["warnings"]

    dry_ledger_operation = conductor.execute(operation="evidence", mode="dry_run")
    assert dry_ledger_operation["ok"] is False
    assert "store_quote_conductor_ledger_operations_require_apply" in dry_ledger_operation["warnings"]


def test_assess_quote_evidence_rejects_unknown_or_malformed_review_claims():
    unexpected = assess_quote_evidence({**_EVIDENCE, "unexpected_field": True})
    assert unexpected == {"ok": False, "error_code": "store_quote_conductor_evidence_fields_invalid"}

    malformed = assess_quote_evidence(
        {
            **_EVIDENCE,
            "recommendation_basis": "unverified",
            "handoff_reasons": "discount_requested",
        }
    )
    assert malformed["ok"] is False
    assert malformed["error_code"] == "store_quote_conductor_evidence_incomplete"
    assert malformed["blockers"] == ["handoff_reasons", "recommendation_basis"]


class _OwnerApiClient:
    def __init__(self, *, prepared: dict) -> None:
        self.prepared = prepared
        self.prepare_calls: list[dict] = []
        self.invoke_calls: list[dict] = []

    def prepare_invocation(self, **kwargs) -> dict:
        self.prepare_calls.append(kwargs)
        return self.prepared

    def invoke(self, **kwargs) -> dict:
        self.invoke_calls.append(kwargs)
        return {"ok": True, "summary": {"operation": kwargs["operation_id"]}}


def test_store_quote_owner_api_uses_only_approved_operations_and_a_prepared_plan():
    client = _OwnerApiClient(
        prepared={
            "ok": True,
            "summary": {
                "operation_id": "get_estimate_draft",
                "method": "GET",
                "schema_hash": "a" * 64,
                "plan_hash": "b" * 64,
            },
        }
    )
    owner_api = StoreQuoteOwnerApi(client)

    assert owner_api.get_estimate_draft(quote_request_id="quote-1")["ok"] is True

    for operation_id, method, call in (
        (
            "replace_estimate_draft",
            "POST",
            lambda: owner_api.replace_estimate_draft(
                quote_request_id="quote-1",
                entries=_ENTRIES,
                coverage=_COVERAGE,
                expected_revision="revision-1",
                idempotency_key="owner-draft-0001",
                correlation_id="owner-correlation-0001",
                mode="dry_run",
            ),
        ),
        (
            "submit_estimate",
            "POST",
            lambda: owner_api.submit_estimate(
                quote_request_id="quote-1",
                customer_response="Проверьте опубликованную смету.",
                expected_revision="revision-1",
                idempotency_key="owner-publish-0001",
                correlation_id="owner-correlation-0001",
                mode="apply",
                dry_run_proof="c" * 64,
            ),
        ),
        (
            "reopen_estimate",
            "POST",
            lambda: owner_api.reopen_estimate(
                quote_request_id="quote-1",
                expected_revision="revision-1",
                idempotency_key="owner-reopen-0001",
                correlation_id="owner-correlation-0001",
                mode="apply",
                dry_run_proof="d" * 64,
            ),
        ),
        (
            "confirm_estimate_order_from_telegram",
            "POST",
            lambda: owner_api.confirm_estimate_order_from_telegram(
                quote_request_id="quote-1",
                published_snapshot_hash=_SNAPSHOT_HASH,
                consent_context_hash=_CONSENT_HASH,
                expected_revision="revision-1",
                idempotency_key="owner-order-0001",
                correlation_id="owner-correlation-0001",
                mode="apply",
                dry_run_proof="e" * 64,
            ),
        ),
    ):
        client.prepared["summary"] = {
            "operation_id": operation_id,
            "method": method,
            "schema_hash": "a" * 64,
            "plan_hash": "b" * 64,
        }
        assert call()["ok"] is True

    assert [call["operation_id"] for call in client.prepare_calls] == [
        "get_estimate_draft",
        "replace_estimate_draft",
        "submit_estimate",
        "reopen_estimate",
        "confirm_estimate_order_from_telegram",
    ]
    assert all(call["path_parameters"] == {"quote_request_id": "quote-1"} for call in client.prepare_calls)
    assert client.invoke_calls[0]["mode"] == "read"
    assert client.invoke_calls[3]["owner_intent"] == "store_quote_conductor_reopen_estimate"
    assert all(call["expected_plan_hash"] == "b" * 64 for call in client.invoke_calls)


def test_store_quote_owner_api_fails_closed_before_invoke_when_the_operation_contract_changes():
    unprepared_client = _OwnerApiClient(prepared={"ok": False, "summary": {"error_code": "owner_unavailable"}})
    assert StoreQuoteOwnerApi(unprepared_client).get_estimate_draft(quote_request_id="quote-1")["ok"] is False
    assert unprepared_client.invoke_calls == []

    changed_client = _OwnerApiClient(
        prepared={
            "ok": True,
            "summary": {
                "operation_id": "submit_estimate",
                "method": "DELETE",
                "schema_hash": "a" * 64,
                "plan_hash": "b" * 64,
            },
        }
    )
    blocked = StoreQuoteOwnerApi(changed_client).submit_estimate(
        quote_request_id="quote-1",
        customer_response="Проверьте опубликованную смету.",
        expected_revision="revision-1",
        idempotency_key="owner-publish-contract-0001",
        correlation_id="owner-correlation-contract-0001",
        mode="apply",
    )

    assert blocked["ok"] is False
    assert blocked["summary"]["error_code"] == "store_quote_owner_operation_contract_changed"
    assert changed_client.invoke_calls == []
