from __future__ import annotations

from copy import deepcopy
import json

from autostop_manager.storage import ManagerMemoryStore
from autostop_manager.store_quote_conductor import (
    StoreQuoteConductor,
    StoreQuoteOwnerApi,
    assess_quote_evidence,
)


_SNAPSHOT_HASH = "b" * 64
_CONSENT_HASH = "c" * 64
_ENTRIES = [
    {
        "type": "position",
        "name": "part-one",
        "catalogNumber": "TEST-001",
        "manufacturer": "TEST",
        "quantity": 1,
        "delivery": "2 days",
        "cost": "1300.00",
        "requestItemId": "item-1",
        "source": "original_request",
    }
]
_COVERAGE = [{"requestItemId": "item-1", "state": "OFFERED"}]
_EVIDENCE = {
    "fitment_confirmed": True,
    "catalog_number_confirmed": True,
    "availability_confirmed": True,
    "delivery_confirmed": True,
    "customer_price_confirmed": True,
    "warranty_confirmed": True,
    "offer_count": 2,
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
                # Mirror the owner API's canonical readback: browser-style
                # estimate fields are serialized as text and an offered
                # coverage row exposes an explicit null reason.
                "entries": [
                    {
                        **deepcopy(entry),
                        "quantity": str(entry.get("quantity") or ""),
                        "warranty": str(entry.get("warranty") or ""),
                    }
                    if entry.get("type") == "position"
                    else deepcopy(entry)
                    for entry in kwargs["entries"]
                ],
                "coverage": [
                    {
                        **deepcopy(item),
                        "clientReason": (
                            str(item.get("clientReason") or "") if item.get("state") == "DECLINED" else None
                        ),
                    }
                    for item in kwargs["coverage"]
                ],
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
            for entry in self.estimate["entries"]:
                if entry.get("type") == "position":
                    entry["warranty"] = "Configured Store warranty"
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


def _conductor(
    tmp_path,
    gateway: _QuoteGateway | None = None,
) -> tuple[StoreQuoteConductor, _QuoteGateway, ManagerMemoryStore]:
    fake = gateway or _QuoteGateway()
    store = ManagerMemoryStore(tmp_path / "manager.sqlite3")
    return (
        StoreQuoteConductor(
            store=store,
            gateway=fake,
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

    ordered = conductor.execute(
        operation="order",
        quote_request_id="quote-1",
        run_id=published["run_id"],
        expected_state_version=published["summary"]["state_version"],
        expected_revision="revision-3",
        idempotency_key="quote-order-001",
        correlation_id="quote-correlation-001",
        consent_context_hash=_CONSENT_HASH,
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


def test_conductor_accepts_owner_canonical_draft_and_store_owned_warranty(tmp_path):
    conductor, gateway, store = _conductor(tmp_path)

    published = _published(conductor)

    assert published["ok"] is True
    assert published["summary"]["phase"] == "published"
    assert gateway.estimate is not None
    assert gateway.estimate["entries"][0]["quantity"] == "1"
    assert gateway.estimate["entries"][0]["warranty"] == "Configured Store warranty"
    assert gateway.estimate["coverage"][0]["clientReason"] is None

    persisted = store.get_manager_run(published["run_id"], include_events=False, include_external_steps=True)
    assert persisted["item"]["status"] == "executing"
    assert persisted["item"]["checkpoint"]["phase"] == "published"


def test_conductor_reopen_requires_full_guard_and_verified_readback(tmp_path):
    conductor, gateway, _ = _conductor(tmp_path)
    published = _published(conductor)

    missing_state_guard = conductor.execute(
        operation="reopen",
        quote_request_id="quote-1",
        run_id=published["run_id"],
        expected_revision="revision-3",
        idempotency_key="quote-reopen-001",
        correlation_id="quote-correlation-001",
    )
    assert missing_state_guard["ok"] is False
    assert "workflow_state_conflict" in missing_state_guard["warnings"]
    assert [name for name, _ in gateway.calls].count("reopen") == 0

    reopened = conductor.execute(
        operation="reopen",
        quote_request_id="quote-1",
        run_id=published["run_id"],
        expected_state_version=published["summary"]["state_version"],
        expected_revision="revision-3",
        idempotency_key="quote-reopen-001",
        correlation_id="quote-correlation-001",
    )

    assert reopened["ok"] is True
    assert reopened["summary"]["phase"] == "revision_needed"
    assert reopened["summary"]["readback_verified"] is True
    reopen_calls = [payload for name, payload in gateway.calls if name == "reopen"]
    assert len(reopen_calls) == 2
    assert [call["mode"] for call in reopen_calls] == ["dry_run", "apply"]
    assert all(call["expected_revision"] == "revision-3" for call in reopen_calls)
    assert all(call["correlation_id"] == "quote-correlation-001" for call in reopen_calls)
    assert reopen_calls[0]["idempotency_key"] != "quote-reopen-001"
    assert reopen_calls[1]["idempotency_key"] == "quote-reopen-001"
    assert reopen_calls[1]["dry_run_proof"] == "e" * 64


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
        evidence={**_EVIDENCE, "offer_count": 4, "handoff_reasons": ["fitment_unverified"]},
    )
    assert unsafe["ok"] is True
    assert unsafe["status"] == "handoff"
    assert [name for name, _ in gateway.calls] == ["get"]


def test_assess_quote_evidence_allows_useful_option_count_and_handoffs_on_risk():
    assert assess_quote_evidence(_EVIDENCE)["ok"] is True
    assert assess_quote_evidence({**_EVIDENCE, "offer_count": 4})["ok"] is True
    assert assess_quote_evidence({**_EVIDENCE, "recommendation_basis": "quality"})["ok"] is True
    assert assess_quote_evidence({**_EVIDENCE, "handoff_reasons": ["discount_requested"]})["ok"] is False


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
            "handoff_reasons": "discount_requested",
        }
    )
    assert malformed["ok"] is False
    assert malformed["error_code"] == "store_quote_conductor_evidence_incomplete"
    assert malformed["blockers"] == ["handoff_reasons"]


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


def test_store_quote_owner_api_reopen_uses_no_request_body():
    client = _OwnerApiClient(
        prepared={
            "ok": True,
            "summary": {
                "operation_id": "reopen_estimate",
                "method": "POST",
                "schema_hash": "a" * 64,
                "plan_hash": "b" * 64,
            },
        }
    )

    result = StoreQuoteOwnerApi(client).reopen_estimate(
        quote_request_id="quote-1",
        expected_revision="revision-1",
        idempotency_key="owner-reopen-body-0001",
        correlation_id="owner-correlation-body-0001",
        mode="dry_run",
    )

    assert result["ok"] is True
    assert client.prepare_calls == [
        {
            "operation_id": "reopen_estimate",
            "path_parameters": {"quote_request_id": "quote-1"},
            "body": None,
            "expected_revision": "revision-1",
        }
    ]
    assert client.invoke_calls[0]["body"] is None


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
