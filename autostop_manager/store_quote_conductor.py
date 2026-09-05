from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from .action_contract import prepare_action_contract
from .agent_gateway import agent_envelope
from .storage import (
    ACTIVE_WORKFLOW_STATES,
    STORE_QUOTE_CONDUCTOR_LEDGER_INTENT,
    STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION,
    STORE_QUOTE_CONDUCTOR_LEDGER_WORKFLOW_ID,
    ManagerMemoryStore,
)
from .store_owner_api import StoreOwnerApiClient


STORE_QUOTE_CONDUCTOR_FORMAT = "store_quote_conductor_v1"
_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,159}$")
_WRITE_OPERATIONS = frozenset({"draft", "publish", "reopen", "order"})
_OPERATIONS = frozenset(
    {
        "start",
        "status",
        "evidence",
        "draft",
        "publish",
        "reopen",
        "order",
        "handoff",
        "decline",
    }
)
_EVIDENCE_FLAGS = frozenset(
    {
        "fitment_confirmed",
        "catalog_number_confirmed",
        "availability_confirmed",
        "delivery_confirmed",
        "customer_price_confirmed",
        "warranty_confirmed",
    }
)
_EVIDENCE_LABEL = re.compile(r"^[a-z][a-z0-9_.:-]{0,79}$")
_PHASES = frozenset(
    {
        "new",
        "evidence_ready",
        "draft_saved",
        "published",
        "waiting_payment",
        "revision_needed",
        "handoff",
        "declined",
        "compensating",
    }
)


@dataclass(frozen=True)
class QuoteEstimateSnapshot:
    """A redacted Store projection used for concurrency and exact readback only."""

    updated_at: str
    status: str
    provenance: str | None
    entries_hash: str
    entries_count: int
    coverage_hash: str
    coverage_count: int
    published_snapshot_hash: str | None
    has_quote_offers: bool
    converted_order_ref_sha256: str | None

    def compact(self) -> dict[str, Any]:
        return {
            "updated_at": self.updated_at,
            "status": self.status,
            "provenance": self.provenance,
            "entries_hash": self.entries_hash or None,
            "entries_count": self.entries_count,
            "coverage_hash": self.coverage_hash or None,
            "coverage_count": self.coverage_count,
            "published_snapshot_hash": self.published_snapshot_hash,
            "has_quote_offers": self.has_quote_offers,
            "converted_order_present": self.converted_order_ref_sha256 is not None,
        }


class StoreQuoteGateway(Protocol):
    """Narrow Store capability surface; callers never select raw owner operations."""

    def get_estimate_draft(self, *, quote_request_id: str) -> dict[str, Any]: ...

    def replace_estimate_draft(
        self,
        *,
        quote_request_id: str,
        entries: list[dict[str, Any]],
        coverage: list[dict[str, Any]],
        expected_revision: str,
        idempotency_key: str,
        correlation_id: str,
        mode: str,
        dry_run_proof: str | None = None,
    ) -> dict[str, Any]: ...

    def submit_estimate(
        self,
        *,
        quote_request_id: str,
        customer_response: str,
        expected_revision: str,
        idempotency_key: str,
        correlation_id: str,
        mode: str,
        dry_run_proof: str | None = None,
    ) -> dict[str, Any]: ...

    def reopen_estimate(
        self,
        *,
        quote_request_id: str,
        expected_revision: str,
        idempotency_key: str,
        correlation_id: str,
        mode: str,
        dry_run_proof: str | None = None,
    ) -> dict[str, Any]: ...

    def confirm_estimate_order_from_telegram(
        self,
        *,
        quote_request_id: str,
        published_snapshot_hash: str,
        consent_context_hash: str,
        expected_revision: str,
        idempotency_key: str,
        correlation_id: str,
        mode: str,
        dry_run_proof: str | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class _OwnerOperation:
    operation_id: str
    method: str
    owner_intent: str


class StoreQuoteOwnerApi:
    """Typed facade over the owner transport for the five quote-estimate operations only."""

    _operations: ClassVar[dict[str, _OwnerOperation]] = {
        "get": _OwnerOperation(
            operation_id="get_estimate_draft",
            method="GET",
            owner_intent="store_quote_conductor_read_estimate",
        ),
        "draft": _OwnerOperation(
            operation_id="replace_estimate_draft",
            method="POST",
            owner_intent="store_quote_conductor_replace_estimate",
        ),
        "publish": _OwnerOperation(
            operation_id="submit_estimate",
            method="POST",
            owner_intent="store_quote_conductor_submit_estimate",
        ),
        "reopen": _OwnerOperation(
            operation_id="reopen_estimate",
            method="POST",
            owner_intent="store_quote_conductor_reopen_estimate",
        ),
        "order": _OwnerOperation(
            operation_id="confirm_estimate_order_from_telegram",
            method="POST",
            owner_intent="store_quote_conductor_confirm_telegram_order",
        ),
    }

    def __init__(self, client: StoreOwnerApiClient) -> None:
        self._client = client

    def get_estimate_draft(self, *, quote_request_id: str) -> dict[str, Any]:
        return self._invoke("get", quote_request_id=quote_request_id, body=None, mode="read")

    def replace_estimate_draft(
        self,
        *,
        quote_request_id: str,
        entries: list[dict[str, Any]],
        coverage: list[dict[str, Any]],
        expected_revision: str,
        idempotency_key: str,
        correlation_id: str,
        mode: str,
        dry_run_proof: str | None = None,
    ) -> dict[str, Any]:
        return self._invoke(
            "draft",
            quote_request_id=quote_request_id,
            body={"entries": entries, "coverage": coverage},
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            mode=mode,
            dry_run_proof=dry_run_proof,
        )

    def submit_estimate(
        self,
        *,
        quote_request_id: str,
        customer_response: str,
        expected_revision: str,
        idempotency_key: str,
        correlation_id: str,
        mode: str,
        dry_run_proof: str | None = None,
    ) -> dict[str, Any]:
        return self._invoke(
            "publish",
            quote_request_id=quote_request_id,
            body={"customerResponse": customer_response},
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            mode=mode,
            dry_run_proof=dry_run_proof,
        )

    def reopen_estimate(
        self,
        *,
        quote_request_id: str,
        expected_revision: str,
        idempotency_key: str,
        correlation_id: str,
        mode: str,
        dry_run_proof: str | None = None,
    ) -> dict[str, Any]:
        return self._invoke(
            "reopen",
            quote_request_id=quote_request_id,
            body={},
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            mode=mode,
            dry_run_proof=dry_run_proof,
        )

    def confirm_estimate_order_from_telegram(
        self,
        *,
        quote_request_id: str,
        published_snapshot_hash: str,
        consent_context_hash: str,
        expected_revision: str,
        idempotency_key: str,
        correlation_id: str,
        mode: str,
        dry_run_proof: str | None = None,
    ) -> dict[str, Any]:
        return self._invoke(
            "order",
            quote_request_id=quote_request_id,
            body={
                "publishedSnapshotHash": published_snapshot_hash,
                "consentContextHash": consent_context_hash,
            },
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            mode=mode,
            dry_run_proof=dry_run_proof,
        )

    def _invoke(
        self,
        name: str,
        *,
        quote_request_id: str,
        body: dict[str, Any] | None,
        mode: str,
        expected_revision: str | None = None,
        idempotency_key: str = "",
        correlation_id: str = "",
        dry_run_proof: str | None = None,
    ) -> dict[str, Any]:
        operation = self._operations[name]
        prepared = self._client.prepare_invocation(
            operation_id=operation.operation_id,
            path_parameters={"quote_request_id": quote_request_id},
            body=body,
            expected_revision=expected_revision,
        )
        if not prepared.get("ok"):
            return prepared
        summary = _mapping(prepared.get("summary"))
        if not self._matches_expected_operation(summary, operation):
            return _owner_error("store_quote_owner_operation_contract_changed")
        return self._client.invoke(
            operation_id=operation.operation_id,
            mode=mode,
            path_parameters={"quote_request_id": quote_request_id},
            body=body,
            owner_intent=operation.owner_intent,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            expected_revision=expected_revision,
            dry_run_proof=dry_run_proof,
            expected_plan_hash=str(summary.get("plan_hash") or "") or None,
        )

    @staticmethod
    def _matches_expected_operation(summary: dict[str, Any], expected: _OwnerOperation) -> bool:
        # The owner client resolves the OpenAPI path from the operation id at
        # invocation time.  Keep this facade narrow by checking that stable id,
        # verb and discovered schema/plan rather than freezing a path template
        # that Store may legitimately revise.
        return (
            str(summary.get("operation_id") or "") == expected.operation_id
            and str(summary.get("method") or "").upper() == expected.method
            and _HASH.fullmatch(str(summary.get("schema_hash") or "")) is not None
            and _HASH.fullmatch(str(summary.get("plan_hash") or "")) is not None
        )


class StoreQuoteConductor:
    """Runs a quote request as one refs-only, CAS-protected Store workflow."""

    def __init__(
        self,
        *,
        store: ManagerMemoryStore,
        gateway: StoreQuoteGateway,
    ) -> None:
        self.store = store
        self.gateway = gateway

    def execute(
        self,
        *,
        operation: str,
        quote_request_id: str = "",
        run_id: int | None = None,
        expected_state_version: int | None = None,
        expected_revision: str = "",
        idempotency_key: str = "",
        correlation_id: str = "",
        entries: list[dict[str, Any]] | None = None,
        coverage: list[dict[str, Any]] | None = None,
        customer_response: str = "",
        evidence: dict[str, Any] | None = None,
        consent_context_hash: str = "",
        published_snapshot_hash: str = "",
        mode: str = "apply",
    ) -> dict[str, Any]:
        normalized = str(operation or "").strip().casefold().replace("-", "_")
        if normalized not in _OPERATIONS:
            return _error("store_quote_conductor_operation_invalid")
        if normalized == "start":
            return self._start(
                quote_request_id=quote_request_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
            )
        if normalized == "status":
            return self._status(run_id=run_id, quote_request_id=quote_request_id)
        if normalized in _WRITE_OPERATIONS:
            return self._write(
                operation=normalized,
                run_id=run_id,
                quote_request_id=quote_request_id,
                expected_state_version=expected_state_version,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                entries=entries,
                coverage=coverage,
                customer_response=customer_response,
                consent_context_hash=consent_context_hash,
                published_snapshot_hash=published_snapshot_hash,
                mode=mode,
            )
        if str(mode or "apply").strip().casefold() != "apply":
            return _error("store_quote_conductor_ledger_operations_require_apply")
        if normalized == "evidence":
            return self._record_evidence(
                run_id=run_id,
                quote_request_id=quote_request_id,
                expected_state_version=expected_state_version,
                evidence=evidence,
            )
        if normalized == "handoff":
            return self._handoff(
                run_id=run_id,
                quote_request_id=quote_request_id,
                expected_state_version=expected_state_version,
                reason="store_quote_conductor_handoff",
            )
        return self._decline(
            run_id=run_id,
            quote_request_id=quote_request_id,
            expected_state_version=expected_state_version,
        )

    def _start(
        self,
        *,
        quote_request_id: str,
        expected_revision: str,
        idempotency_key: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        invalid = _run_identifiers_invalid(quote_request_id, idempotency_key, correlation_id)
        if invalid:
            return _error(invalid)
        snapshot_result = self._read_snapshot(quote_request_id)
        if not snapshot_result["ok"]:
            return _error(str(snapshot_result["error_code"]))
        snapshot: QuoteEstimateSnapshot = snapshot_result["snapshot"]
        if expected_revision and expected_revision != snapshot.updated_at:
            return _error("store_quote_conductor_expected_revision_stale", status="conflict")
        effective_revision = snapshot.updated_at
        target_hash = _quote_ref_sha256(quote_request_id)
        scope = {
            "operation": STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION,
            "workflow_id": STORE_QUOTE_CONDUCTOR_LEDGER_WORKFLOW_ID,
            "domain": "store",
            "source": "store_quote_conductor",
            "correlation_id": correlation_id,
            "target_entity": "store_quote_request",
            "target_ref_sha256": target_hash,
            "expected_revision_sha256": _sha256(effective_revision),
        }
        started = self.store.start_store_quote_conductor_run(
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            scope=scope,
            active_target_ref_sha256=target_hash,
        )
        if not started.get("ok"):
            return _error(str(started.get("error") or "store_quote_conductor_start_failed"))
        run = self._run_from_started(started)
        if run is None:
            return _error("store_quote_conductor_run_invalid")
        if started.get("active_target_deduplicated"):
            return _envelope(
                ok=True,
                status=str(run.get("status") or "planned"),
                run=run,
                technical={"active_target_deduplicated": True},
            )
        checkpoint = self._checkpoint(
            run,
            phase="new",
            expected_revision=snapshot.updated_at,
            technical={"snapshot_at": _sha256(snapshot.updated_at)},
        )
        if not checkpoint.get("ok"):
            return _error(str(checkpoint.get("error") or "store_quote_conductor_checkpoint_failed"), run=run)
        refreshed = self._get_run(int(run["id"]), quote_request_id)
        if conflict_reason := _snapshot_handoff_reason(snapshot):
            return self._handoff_from_run(refreshed if refreshed.get("ok") else run, reason=conflict_reason)
        return _envelope(
            ok=True,
            status="planned",
            run=refreshed if refreshed.get("ok") else run,
            technical={"snapshot": snapshot.compact(), "deduplicated": bool(started.get("deduplicated"))},
        )

    def _status(self, *, run_id: int | None, quote_request_id: str) -> dict[str, Any]:
        run = self._get_run(run_id, quote_request_id)
        if not run.get("ok"):
            return _error(str(run.get("error") or "store_quote_conductor_run_not_found"))
        return _envelope(ok=True, status=str(run["status"]), run=run)

    def _record_evidence(
        self,
        *,
        run_id: int | None,
        quote_request_id: str,
        expected_state_version: int | None,
        evidence: dict[str, Any] | None,
    ) -> dict[str, Any]:
        run = self._require_active_run(run_id, quote_request_id, expected_state_version)
        if not run.get("ok"):
            return _error(str(run.get("error")))
        executing = self._ensure_executing(run)
        if not executing.get("ok"):
            return _error(str(executing.get("error")), run=run)
        run = executing["run"]
        if _phase(run) not in {"new", "revision_needed"}:
            return _error("store_quote_conductor_evidence_phase_invalid", run=run, status="conflict")
        assessment = assess_quote_evidence(evidence)
        if not assessment["ok"]:
            return self._handoff_from_run(run, reason="store_quote_conductor_evidence_handoff")
        checkpoint = self._checkpoint(
            run,
            phase="evidence_ready",
            technical={
                "evidence_hash": str(assessment["evidence_hash"]),
                "counts": {"offers": int(assessment["offer_count"])},
            },
        )
        if not checkpoint.get("ok"):
            return _error(str(checkpoint.get("error") or "store_quote_conductor_checkpoint_failed"), run=run)
        current = self._get_run(int(run["id"]), quote_request_id)
        return _envelope(
            ok=True,
            status="executing",
            run=current if current.get("ok") else run,
            technical={"evidence_hash": assessment["evidence_hash"], "offer_count": assessment["offer_count"]},
        )

    def _write(
        self,
        *,
        operation: str,
        run_id: int | None,
        quote_request_id: str,
        expected_state_version: int | None,
        expected_revision: str,
        idempotency_key: str,
        correlation_id: str,
        entries: list[dict[str, Any]] | None,
        coverage: list[dict[str, Any]] | None,
        customer_response: str,
        consent_context_hash: str,
        published_snapshot_hash: str,
        mode: str,
    ) -> dict[str, Any]:
        if str(mode or "").strip().casefold() not in {"dry_run", "apply"}:
            return _error("store_quote_conductor_write_mode_invalid")
        invalid = _run_identifiers_invalid(quote_request_id, idempotency_key, correlation_id)
        if invalid or not str(expected_revision or "").strip():
            return _error(invalid or "store_quote_conductor_expected_revision_required")
        run = self._require_active_run(run_id, quote_request_id, expected_state_version)
        if not run.get("ok"):
            return _error(str(run.get("error")))
        executing = self._ensure_executing(run)
        if not executing.get("ok"):
            return _error(str(executing.get("error")), run=run)
        run = executing["run"]
        if _sha256(expected_revision) != _checkpoint_value(run, "expected_revision_sha256"):
            return _error("store_quote_conductor_expected_revision_stale", run=run, status="conflict")
        phase_error = _write_phase_error(operation, _phase(run))
        if phase_error:
            return _error(phase_error, run=run, status="conflict")
        request = _write_request(
            operation=operation,
            entries=entries,
            coverage=coverage,
            customer_response=customer_response,
            consent_context_hash=consent_context_hash,
            published_snapshot_hash=published_snapshot_hash,
            checkpoint=run.get("checkpoint"),
        )
        if not request["ok"]:
            return _error(str(request["error_code"]), run=run)
        snapshot_result = self._read_snapshot(quote_request_id)
        if not snapshot_result["ok"]:
            return _error(str(snapshot_result["error_code"]), run=run)
        before: QuoteEstimateSnapshot = snapshot_result["snapshot"]
        if before.updated_at != expected_revision:
            return _error("store_quote_conductor_expected_revision_stale", run=run, status="conflict")
        conflict_reason = _snapshot_handoff_reason(before)
        if conflict_reason:
            return self._handoff_from_run(run, reason=conflict_reason)
        action_name = _contract_action(operation)
        dry_contract = self._contract(
            action=action_name,
            quote_request_id=quote_request_id,
            expected_revision=expected_revision,
            idempotency_key=_dry_run_key(idempotency_key),
            correlation_id=correlation_id,
            changes=request["contract_changes"],
            dry_run=True,
        )
        if not dry_contract.get("ok"):
            return _error("store_quote_conductor_action_contract_blocked", run=run)
        dry_result = self._invoke_write(
            operation=operation,
            quote_request_id=quote_request_id,
            expected_revision=expected_revision,
            idempotency_key=_dry_run_key(idempotency_key),
            correlation_id=correlation_id,
            request=request,
            mode="dry_run",
        )
        proof = _dry_run_proof(dry_result)
        if proof is None:
            return _error(_result_error_code(dry_result, "store_quote_conductor_dry_run_failed"), run=run)
        if str(mode).strip().casefold() == "dry_run":
            return _envelope(
                ok=True,
                status="planned",
                run=run,
                technical={
                    "contract_id": dry_contract.get("contract_id"),
                    "operation": operation,
                    "dry_run_receipt_sha256": _sha256(proof),
                },
            )
        apply_contract = self._contract(
            action=action_name,
            quote_request_id=quote_request_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            changes=request["contract_changes"],
            dry_run=False,
        )
        if not apply_contract.get("ok"):
            return _error("store_quote_conductor_action_contract_blocked", run=run)
        applied = self._invoke_write(
            operation=operation,
            quote_request_id=quote_request_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            request=request,
            mode="apply",
            dry_run_proof=proof,
        )
        readback = self._read_snapshot(quote_request_id)
        if not readback["ok"]:
            return self._compensating(run, operation=operation, error_code="store_quote_conductor_readback_failed")
        after: QuoteEstimateSnapshot = readback["snapshot"]
        if not _write_readback_matches(operation, after, request):
            return self._compensating(run, operation=operation, error_code="store_quote_conductor_readback_mismatch")
        if not applied.get("ok") and not _apply_outcome_uncertain(applied):
            return _error(_result_error_code(applied, "store_quote_conductor_apply_failed"), run=run)
        phase, technical = _post_write_state(operation, after, request)
        technical.update(
            {
                "contract_id": apply_contract.get("contract_id"),
                "request_fingerprint": _sha256(_canonical_json(request["contract_changes"])),
            }
        )
        if phase == "waiting_payment":
            technical["verification"] = _technical_review(run, outcome="waiting_payment")
        checkpoint = self._checkpoint(run, phase=phase, expected_revision=after.updated_at, technical=technical)
        if not checkpoint.get("ok"):
            return self._compensating(run, operation=operation, error_code="store_quote_conductor_checkpoint_failed")
        current = self._get_run(int(run["id"]), quote_request_id)
        return _envelope(
            ok=True,
            status="executing",
            run=current if current.get("ok") else run,
            technical={"operation": operation, "readback_verified": True},
        )

    def _invoke_write(
        self,
        *,
        operation: str,
        quote_request_id: str,
        expected_revision: str,
        idempotency_key: str,
        correlation_id: str,
        request: dict[str, Any],
        mode: str,
        dry_run_proof: str | None = None,
    ) -> dict[str, Any]:
        if operation == "draft":
            return self.gateway.replace_estimate_draft(
                quote_request_id=quote_request_id,
                entries=request["entries"],
                coverage=request["coverage"],
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                mode=mode,
                dry_run_proof=dry_run_proof,
            )
        if operation == "publish":
            return self.gateway.submit_estimate(
                quote_request_id=quote_request_id,
                customer_response=request["customer_response"],
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                mode=mode,
                dry_run_proof=dry_run_proof,
            )
        if operation == "reopen":
            return self.gateway.reopen_estimate(
                quote_request_id=quote_request_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                mode=mode,
                dry_run_proof=dry_run_proof,
            )
        return self.gateway.confirm_estimate_order_from_telegram(
            quote_request_id=quote_request_id,
            published_snapshot_hash=request["published_snapshot_hash"],
            consent_context_hash=request["consent_context_hash"],
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            mode=mode,
            dry_run_proof=dry_run_proof,
        )

    def _contract(
        self,
        *,
        action: str,
        quote_request_id: str,
        expected_revision: str,
        idempotency_key: str,
        correlation_id: str,
        changes: dict[str, Any],
        dry_run: bool,
    ) -> dict[str, Any]:
        return prepare_action_contract(
            domain="store_quote_conductor",
            action=action,
            target_id=quote_request_id,
            planned_changes=changes,
            owner_intent=f"store_quote_conductor_{action}",
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            dry_run=dry_run,
        )

    def _handoff(
        self,
        *,
        run_id: int | None,
        quote_request_id: str,
        expected_state_version: int | None,
        reason: str,
    ) -> dict[str, Any]:
        run = self._require_active_run(run_id, quote_request_id, expected_state_version)
        if not run.get("ok"):
            return _error(str(run.get("error")))
        return self._handoff_from_run(run, reason=reason)

    def _handoff_from_run(self, run: dict[str, Any], *, reason: str) -> dict[str, Any]:
        checkpoint = self._checkpoint(
            run,
            phase="handoff",
            technical={
                "error_code": reason,
                "verification": _technical_review(run, outcome="handoff", failure_class=reason),
            },
        )
        if not checkpoint.get("ok"):
            return _error(str(checkpoint.get("error") or "store_quote_conductor_handoff_failed"), run=run)
        current = self._get_run(int(run["id"]), None)
        if not current.get("ok"):
            return _error(str(current.get("error")))
        terminal = self._complete_from_run(current, phase="handoff", return_raw=True)
        if not terminal.get("ok"):
            return _error(str(terminal.get("error") or "store_quote_conductor_handoff_failed"), run=current)
        final_run = self._get_run(int(current["id"]), None)
        return _envelope(
            ok=True,
            status="handoff",
            run=final_run if final_run.get("ok") else current,
            technical={"error_code": reason},
        )

    def _decline(
        self,
        *,
        run_id: int | None,
        quote_request_id: str,
        expected_state_version: int | None,
    ) -> dict[str, Any]:
        run = self._require_active_run(run_id, quote_request_id, expected_state_version)
        if not run.get("ok"):
            return _error(str(run.get("error")))
        return self._complete_from_run(run, phase="declined")

    def _complete_from_run(
        self,
        run: dict[str, Any],
        *,
        phase: str,
        return_raw: bool = False,
    ) -> dict[str, Any]:
        current = run
        if str(current.get("status") or "") == "planned":
            executing = self._ensure_executing(current)
            if not executing.get("ok"):
                return executing
            current = executing["run"]
        existing_review = _mapping(_mapping(current.get("checkpoint")).get("verification"))
        review = (
            existing_review
            if phase == "handoff" and existing_review.get("route_version") == STORE_QUOTE_CONDUCTOR_FORMAT
            else _technical_review(current, outcome=phase)
        )
        checkpoint = self._checkpoint(current, phase=phase, technical={"verification": review})
        if not checkpoint.get("ok"):
            return checkpoint
        current_result = self._get_run(int(current["id"]), None)
        if not current_result.get("ok"):
            return current_result
        current = current_result
        if str(current["status"]) == "executing":
            verifying = self.store.transition_store_quote_conductor_run(
                int(current["id"]),
                status="verifying",
                message="verify store_quote_conductor",
                expected_state_version=int(current["state_version"]),
            )
            if not verifying.get("ok"):
                return verifying
            current_result = self._get_run(int(current["id"]), None)
            if not current_result.get("ok"):
                return current_result
            current = current_result
        completed = self.store.transition_store_quote_conductor_run(
            int(current["id"]),
            status="completed",
            message="completed store_quote_conductor",
            summary=STORE_QUOTE_CONDUCTOR_LEDGER_WORKFLOW_ID,
            verification={"workflow_completed": True},
            expected_state_version=int(current["state_version"]),
        )
        if return_raw:
            return completed
        final_run = self._get_run(int(current["id"]), None)
        return _envelope(
            ok=bool(completed.get("ok")),
            status=phase,
            run=final_run if final_run.get("ok") else current,
        )

    def _compensating(self, run: dict[str, Any], *, operation: str, error_code: str) -> dict[str, Any]:
        checkpoint = self._checkpoint(
            run,
            phase="compensating",
            technical={
                "error_code": error_code,
                "verification": _technical_review(run, outcome="compensating", failure_class=error_code),
            },
        )
        current = self._get_run(int(run["id"]), None)
        if current.get("ok") and str(current.get("status")) == "executing":
            self.store.transition_store_quote_conductor_run(
                int(current["id"]),
                status="compensating",
                message="verification failed after executor applied store_quote_conductor",
                verification={"executor_ok": True, "passed": False},
                expected_state_version=int(current["state_version"]),
            )
            current = self._get_run(int(run["id"]), None)
        return _envelope(
            ok=False,
            status="compensating",
            run=current if current.get("ok") else run,
            technical={
                "operation": operation,
                "error_code": error_code,
                "checkpoint_recorded": bool(checkpoint.get("ok")),
            },
            warnings=[error_code],
        )

    def _read_snapshot(self, quote_request_id: str) -> dict[str, Any]:
        try:
            result = self.gateway.get_estimate_draft(quote_request_id=quote_request_id)
        except (OSError, TypeError, ValueError):
            return {"ok": False, "error_code": "store_quote_conductor_estimate_read_failed"}
        if not result.get("ok"):
            return {"ok": False, "error_code": _result_error_code(result, "store_quote_conductor_estimate_read_failed")}
        try:
            snapshot = _snapshot_from_data(result.get("data"), quote_request_id=quote_request_id)
        except (TypeError, ValueError):
            return {"ok": False, "error_code": "store_quote_conductor_estimate_snapshot_invalid"}
        return {"ok": True, "snapshot": snapshot}

    def _require_active_run(
        self,
        run_id: int | None,
        quote_request_id: str,
        expected_state_version: int | None,
    ) -> dict[str, Any]:
        run = self._get_run(run_id, quote_request_id)
        if not run.get("ok"):
            return run
        if str(run.get("status") or "") not in ACTIVE_WORKFLOW_STATES:
            return {"ok": False, "error": "store_quote_conductor_workflow_terminal"}
        if expected_state_version is None or int(expected_state_version) != int(run["state_version"]):
            return {
                "ok": False,
                "error": "workflow_state_conflict",
                "current_state_version": int(run["state_version"]),
            }
        return run

    def _get_run(self, run_id: int | None, quote_request_id: str | None) -> dict[str, Any]:
        if type(run_id) is not int or run_id <= 0:
            return {"ok": False, "error": "store_quote_conductor_run_id_required"}
        result = self.store.get_manager_run(run_id, include_events=False, include_external_steps=True)
        if not result.get("ok"):
            return {"ok": False, "error": "store_quote_conductor_run_not_found"}
        item = _mapping(result.get("item"))
        if (
            str(item.get("workflow_id") or "") != STORE_QUOTE_CONDUCTOR_LEDGER_WORKFLOW_ID
            or str(item.get("intent") or "") != STORE_QUOTE_CONDUCTOR_LEDGER_INTENT
            or str(_mapping(item.get("scope")).get("operation") or "") != STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION
        ):
            return {"ok": False, "error": "store_quote_conductor_run_binding_invalid"}
        if quote_request_id is not None and (
            not _safe_quote_identifier(quote_request_id)
            or _quote_ref_sha256(quote_request_id) != str(_mapping(item.get("scope")).get("target_ref_sha256") or "")
        ):
            return {"ok": False, "error": "store_quote_conductor_target_mismatch"}
        return {"ok": True, **item}

    def _run_from_started(self, started: dict[str, Any]) -> dict[str, Any] | None:
        run_id = started.get("id")
        if type(run_id) is not int:
            return None
        result = self._get_run(run_id, None)
        return result if result.get("ok") else None

    def _ensure_executing(self, run: dict[str, Any]) -> dict[str, Any]:
        status = str(run.get("status") or "")
        if status == "executing":
            return {"ok": True, "run": run}
        if status == "planned":
            transitioned = self.store.transition_store_quote_conductor_run(
                int(run["id"]),
                status="executing",
                message="execute store_quote_conductor",
                expected_state_version=int(run["state_version"]),
            )
        else:
            return {"ok": False, "error": "store_quote_conductor_not_resumable"}
        if not transitioned.get("ok"):
            return {"ok": False, "error": str(transitioned.get("error") or "store_quote_conductor_resume_failed")}
        current = self._get_run(int(run["id"]), None)
        return {"ok": bool(current.get("ok")), "run": current} if current.get("ok") else current

    def _checkpoint(
        self,
        run: dict[str, Any],
        *,
        phase: str,
        expected_revision: str | None = None,
        technical: dict[str, Any],
    ) -> dict[str, Any]:
        revision_sha256 = (
            _sha256(expected_revision)
            if expected_revision is not None
            else _checkpoint_value(run, "expected_revision_sha256")
        )
        if phase not in _PHASES or _HASH.fullmatch(revision_sha256) is None:
            return {"ok": False, "error": "store_quote_conductor_checkpoint_invalid"}
        checkpoint = {
            key: value
            for key, value in _mapping(run.get("checkpoint")).items()
            if key
            in {
                "counts",
                "entries_hash",
                "evidence_hash",
                "published_snapshot_hash",
                "quote_snapshot_hash",
                "request_fingerprint",
                "snapshot_at",
                "verification",
            }
        }
        checkpoint.update(
            {
                "operation": STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION,
                "phase": phase,
                "expected_revision_sha256": revision_sha256,
                "target_ref_sha256": _mapping(run.get("scope")).get("target_ref_sha256"),
            }
        )
        for key, value in technical.items():
            # Empty identifiers are not safe ledger values.  They represent a
            # deliberate removal (for example published snapshot on reopen),
            # not a customer-visible value.
            if value is None or value == "":
                checkpoint.pop(key, None)
            elif key == "counts" and isinstance(value, dict):
                # Counts describe independent aggregate dimensions.  A saved
                # quote replaces entries/coverage but must not erase the
                # already-verified number of alternatives used for the safe
                # recommendation review.
                checkpoint[key] = {**_mapping(checkpoint.get(key)), **value}
            else:
                checkpoint[key] = value
        return self.store.checkpoint_store_quote_conductor_run(
            int(run["id"]),
            checkpoint=checkpoint,
            message="verify store_quote_conductor",
            expected_state_version=int(run["state_version"]),
        )


def assess_quote_evidence(evidence: dict[str, Any] | None) -> dict[str, Any]:
    """Validate a compact factual review without prescribing the recommendation."""

    value = _mapping(evidence)
    allowed = set(_EVIDENCE_FLAGS) | {"offer_count", "handoff_reasons", "recommendation_basis"}
    if set(value).difference(allowed):
        return {"ok": False, "error_code": "store_quote_conductor_evidence_fields_invalid"}
    missing = sorted(flag for flag in _EVIDENCE_FLAGS if value.get(flag) is not True)
    offer_count = value.get("offer_count")
    if type(offer_count) is not int or not 1 <= offer_count <= 50:
        missing.append("offer_count")
    reasons = value.get("handoff_reasons", [])
    if not isinstance(reasons, list) or any(_EVIDENCE_LABEL.fullmatch(str(reason)) is None for reason in reasons):
        missing.append("handoff_reasons")
    elif reasons:
        missing.extend(f"handoff:{reason}" for reason in reasons)
    if missing:
        return {
            "ok": False,
            "error_code": "store_quote_conductor_evidence_incomplete",
            "blockers": sorted(set(missing)),
        }
    canonical = {key: value.get(key) for key in sorted(allowed)}
    return {"ok": True, "evidence_hash": _sha256(_canonical_json(canonical)), "offer_count": offer_count}


def _technical_review(
    run: dict[str, Any],
    *,
    outcome: str,
    failure_class: str = "",
) -> dict[str, Any]:
    """Return an aggregate-only workflow review; it never changes rules itself."""

    counts = _mapping(_mapping(run.get("checkpoint")).get("counts"))
    return {
        "route_version": STORE_QUOTE_CONDUCTOR_FORMAT,
        "outcome": outcome,
        "failure_class": failure_class or "none",
        "offers": int(counts.get("offers") or 0),
        "entries": int(counts.get("entries") or 0),
        "coverage": int(counts.get("coverage") or 0),
    }


def _write_request(
    *,
    operation: str,
    entries: list[dict[str, Any]] | None,
    coverage: list[dict[str, Any]] | None,
    customer_response: str,
    consent_context_hash: str,
    published_snapshot_hash: str,
    checkpoint: Any,
) -> dict[str, Any]:
    saved = _mapping(checkpoint)
    if operation == "draft":
        if entries is None or coverage is None or not _valid_entries(entries) or not _valid_coverage(coverage):
            return {"ok": False, "error_code": "store_quote_conductor_estimate_draft_invalid"}
        evidence_hash = str(saved.get("evidence_hash") or "")
        if _HASH.fullmatch(evidence_hash) is None:
            return {"ok": False, "error_code": "store_quote_conductor_evidence_required"}
        entries_hash = _sha256(_canonical_json(entries))
        coverage_hash = _sha256(_canonical_json(coverage))
        return {
            "ok": True,
            "entries": entries,
            "coverage": coverage,
            "contract_changes": {
                "entries_count": len(entries),
                "entries_sha256": entries_hash,
                "coverage_count": len(coverage),
                "coverage_sha256": coverage_hash,
                "evidence_sha256": evidence_hash,
                "provenance": "AUTOSTOP_MANAGER",
            },
        }
    if operation == "publish":
        entries_hash = str(saved.get("entries_hash") or "")
        if _HASH.fullmatch(entries_hash) is None or not _valid_customer_response(customer_response):
            return {"ok": False, "error_code": "store_quote_conductor_publish_input_invalid"}
        return {
            "ok": True,
            "customer_response": customer_response,
            "contract_changes": {
                "entries_count": int(_mapping(saved.get("counts")).get("entries") or 1),
                "entries_sha256": entries_hash,
                "customer_response_sha256": _sha256(customer_response),
                "provenance": "AUTOSTOP_MANAGER",
            },
        }
    if operation == "reopen":
        snapshot_hash = str(saved.get("published_snapshot_hash") or "")
        if _HASH.fullmatch(snapshot_hash) is None:
            return {"ok": False, "error_code": "store_quote_conductor_published_snapshot_required"}
        return {"ok": True, "contract_changes": {"published_snapshot_sha256": snapshot_hash}}
    snapshot_hash = str(published_snapshot_hash or "")
    saved_snapshot = str(saved.get("published_snapshot_hash") or "")
    if not _valid_hash_match(snapshot_hash, saved_snapshot) or _HASH.fullmatch(str(consent_context_hash or "")) is None:
        return {"ok": False, "error_code": "store_quote_conductor_consent_binding_invalid"}
    return {
        "ok": True,
        "published_snapshot_hash": snapshot_hash,
        "consent_context_hash": consent_context_hash,
        "contract_changes": {
            "published_snapshot_sha256": snapshot_hash,
            "consent_context_sha256": consent_context_hash,
        },
    }


def _snapshot_from_data(value: Any, *, quote_request_id: str) -> QuoteEstimateSnapshot:
    payload = _mapping(value)
    if str(payload.get("quoteRequestId") or "") != quote_request_id:
        raise ValueError("quote binding")
    # Store's owner estimate contract exposes an opaque `revision` token.  An
    # earlier implementation named the same field `updatedAt`; accepting it
    # as a read-only compatibility fallback avoids guessing a stale timestamp
    # while deployments converge on the new OpenAPI schema.
    updated_at = str(payload.get("revision") or payload.get("updatedAt") or "").strip()
    status = str(payload.get("status") or "").strip().upper()
    if not updated_at or not status or type(payload.get("hasQuoteOffers")) is not bool:
        raise ValueError("required snapshot fields")
    estimate = payload.get("estimate")
    if estimate is None:
        return QuoteEstimateSnapshot(
            updated_at=updated_at,
            status=status,
            provenance=None,
            entries_hash="",
            entries_count=0,
            coverage_hash="",
            coverage_count=0,
            published_snapshot_hash=None,
            has_quote_offers=bool(payload["hasQuoteOffers"]),
            converted_order_ref_sha256=_optional_ref_hash(payload.get("convertedOrderId")),
        )
    estimate_data = _mapping(estimate)
    entries = estimate_data.get("entries")
    coverage = estimate_data.get("coverage")
    provenance = str(estimate_data.get("provenance") or "").strip() or None
    if (
        provenance not in {"MANUAL", "AUTOSTOP_MANAGER"}
        or not isinstance(entries, list)
        or not isinstance(coverage, list)
    ):
        raise ValueError("estimate fields")
    snapshot_hash = str(estimate_data.get("publishedSnapshotHash") or "").strip() or None
    if snapshot_hash is not None and _HASH.fullmatch(snapshot_hash) is None:
        raise ValueError("published snapshot hash")
    return QuoteEstimateSnapshot(
        updated_at=updated_at,
        status=status,
        provenance=provenance,
        entries_hash=_sha256(_canonical_json(entries)),
        entries_count=len(entries),
        coverage_hash=_sha256(_canonical_json(coverage)),
        coverage_count=len(coverage),
        published_snapshot_hash=snapshot_hash,
        has_quote_offers=bool(payload["hasQuoteOffers"]),
        converted_order_ref_sha256=_optional_ref_hash(payload.get("convertedOrderId")),
    )


def _write_readback_matches(operation: str, snapshot: QuoteEstimateSnapshot, request: dict[str, Any]) -> bool:
    if operation == "draft":
        changes = _mapping(request.get("contract_changes"))
        return (
            snapshot.provenance == "AUTOSTOP_MANAGER"
            and not snapshot.has_quote_offers
            and snapshot.entries_hash == changes.get("entries_sha256")
            and snapshot.coverage_hash == changes.get("coverage_sha256")
        )
    if operation == "publish":
        return (
            snapshot.provenance == "AUTOSTOP_MANAGER"
            and snapshot.entries_hash == _mapping(request.get("contract_changes")).get("entries_sha256")
            and _HASH.fullmatch(str(snapshot.published_snapshot_hash or "")) is not None
            and snapshot.status == "WAITING_FOR_APPROVAL"
        )
    if operation == "reopen":
        return snapshot.published_snapshot_hash is None and snapshot.status == "WAITING_FOR_QUOTE"
    return (
        snapshot.converted_order_ref_sha256 is not None
        and snapshot.published_snapshot_hash == request.get("published_snapshot_hash")
        and snapshot.status in {"APPROVED", "ORDERED", "WAITING_FOR_PAYMENT"}
    )


def _post_write_state(
    operation: str,
    snapshot: QuoteEstimateSnapshot,
    request: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    if operation == "draft":
        changes = _mapping(request.get("contract_changes"))
        return (
            "draft_saved",
            {
                "entries_hash": changes["entries_sha256"],
                "counts": {"entries": changes["entries_count"], "coverage": changes["coverage_count"]},
            },
        )
    if operation == "publish":
        return "published", {"published_snapshot_hash": str(snapshot.published_snapshot_hash)}
    if operation == "reopen":
        return "revision_needed", {"published_snapshot_hash": ""}
    return "waiting_payment", {"quote_snapshot_hash": str(snapshot.published_snapshot_hash)}


def _write_phase_error(operation: str, phase: str) -> str | None:
    allowed = {
        "draft": {"evidence_ready", "revision_needed", "draft_saved"},
        "publish": {"draft_saved"},
        "reopen": {"published", "revision_needed"},
        "order": {"published"},
    }
    return None if phase in allowed[operation] else f"store_quote_conductor_{operation}_phase_invalid"


def _contract_action(operation: str) -> str:
    return {
        "draft": "replace_estimate_draft",
        "publish": "submit_estimate",
        "reopen": "reopen_estimate",
        "order": "confirm_estimate_order_from_telegram",
    }[operation]


def _snapshot_handoff_reason(snapshot: QuoteEstimateSnapshot) -> str | None:
    if snapshot.has_quote_offers:
        return "quote_offer_conflict"
    if snapshot.provenance == "MANUAL":
        return "manual_estimate"
    return None


def _run_identifiers_invalid(quote_request_id: str, idempotency_key: str, correlation_id: str) -> str | None:
    if not _safe_quote_identifier(quote_request_id):
        return "store_quote_conductor_quote_id_invalid"
    if _IDENTIFIER.fullmatch(str(idempotency_key or "").strip()) is None:
        return "store_quote_conductor_idempotency_key_invalid"
    if _IDENTIFIER.fullmatch(str(correlation_id or "").strip()) is None:
        return "store_quote_conductor_correlation_id_invalid"
    return None


def _safe_quote_identifier(value: str) -> bool:
    normalized = str(value or "").strip()
    return (
        bool(normalized)
        and len(normalized) <= 160
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", normalized) is not None
    )


def _valid_entries(value: Any) -> bool:
    return isinstance(value, list) and 1 <= len(value) <= 50 and all(isinstance(item, dict) for item in value)


def _valid_coverage(value: Any) -> bool:
    return isinstance(value, list) and 1 <= len(value) <= 50 and all(isinstance(item, dict) for item in value)


def _valid_customer_response(value: str) -> bool:
    normalized = str(value or "").strip()
    return 1 <= len(normalized) <= 2_000


def _phase(run: dict[str, Any]) -> str:
    value = str(_mapping(run.get("checkpoint")).get("phase") or "new")
    return value if value in _PHASES else "new"


def _checkpoint_value(run: dict[str, Any], key: str) -> str:
    return str(_mapping(run.get("checkpoint")).get(key) or "")


def _valid_hash_match(value: str, expected: str) -> bool:
    return _HASH.fullmatch(str(value or "")) is not None and str(value) == str(expected)


def _dry_run_key(value: str) -> str:
    normalized = str(value or "").strip()
    candidate = f"{normalized}.dry"
    return candidate if _IDENTIFIER.fullmatch(candidate) is not None else f"quote-dry-{_sha256(normalized)[:32]}"


def _dry_run_proof(result: dict[str, Any]) -> str | None:
    proof = str(_mapping(result.get("summary")).get("dry_run_proof") or "")
    return proof if _HASH.fullmatch(proof) is not None else None


def _apply_outcome_uncertain(result: dict[str, Any]) -> bool:
    return _mapping(result.get("meta")).get("outcome_uncertain") is True


def _result_error_code(result: dict[str, Any], fallback: str) -> str:
    error = _mapping(result.get("error"))
    code = str(
        result.get("error_code") or error.get("code") or _mapping(result.get("summary")).get("error_code") or fallback
    )
    return code if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,119}", code) is not None else fallback


def _quote_ref_sha256(quote_request_id: str) -> str:
    return _sha256(f"store-quote-conductor-v1\0{quote_request_id}")


def _optional_ref_hash(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return _sha256(f"store-ref-v1\0{normalized}") if normalized else None


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _owner_error(code: str) -> dict[str, Any]:
    return {
        "ok": False,
        "format": "autostop_store_owner_api_v1",
        "status": "blocked",
        "error": {"code": code},
        "summary": {"error_code": code},
        "data_included": False,
    }


def _error(
    code: str,
    *,
    run: dict[str, Any] | None = None,
    status: str = "blocked",
) -> dict[str, Any]:
    return _envelope(
        ok=False,
        status=status,
        run=run,
        technical={"error_code": code},
        warnings=[code],
    )


def _envelope(
    *,
    ok: bool,
    status: str,
    run: dict[str, Any] | None,
    technical: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    item = _mapping(run)
    checkpoint = _mapping(item.get("checkpoint"))
    summary = {
        "workflow": STORE_QUOTE_CONDUCTOR_LEDGER_WORKFLOW_ID,
        "phase": _phase(item) if item else None,
        "state_version": item.get("state_version"),
        "workflow_status": item.get("status"),
        **(technical or {}),
    }
    review = _mapping(checkpoint.get("verification"))
    if review.get("route_version") == STORE_QUOTE_CONDUCTOR_FORMAT:
        summary["technical_review"] = review
    return agent_envelope(
        ok=ok,
        status=status,
        run_id=int(item["id"]) if type(item.get("id")) is int else None,
        summary=summary,
        warnings=warnings or [],
        next_actions=_next_actions(checkpoint, status),
        meta={"conductor_format": STORE_QUOTE_CONDUCTOR_FORMAT},
    )


def _next_actions(checkpoint: dict[str, Any], status: str) -> list[str]:
    phase = str(checkpoint.get("phase") or "")
    if status == "handoff" or phase == "handoff":
        return ["human_review_required"]
    if phase == "waiting_payment":
        return ["human_payment_confirmation_required"]
    if phase == "revision_needed":
        return ["refresh_evidence_and_replace_estimate"]
    if phase == "evidence_ready":
        return ["save_estimate_draft"]
    if phase == "draft_saved":
        return ["publish_estimate"]
    if phase == "published":
        return ["continue_customer_dialogue_or_create_order_after_choice"]
    return []


__all__ = [
    "STORE_QUOTE_CONDUCTOR_FORMAT",
    "QuoteEstimateSnapshot",
    "StoreQuoteConductor",
    "StoreQuoteGateway",
    "StoreQuoteOwnerApi",
    "assess_quote_evidence",
]
