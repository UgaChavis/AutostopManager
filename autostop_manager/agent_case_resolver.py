"""Safe DTO boundary for the read-only adaptive Case Resolver.

The core resolver is deliberately connector-neutral.  This module gives the
Manager MCP layer a small, bounded interface without allowing raw prompts,
connector payloads, or personal identifiers to enter workflow state.  Results
are transient and a reconciled value is returned only through the redacting
display representation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .case_resolver import CaseRequest, CaseResolver, ReadPlan
from .evidence_bundle import Claim, EvidenceRecord, SourceDescriptor, validate_public_technical_evidence_value


_MAX_CLAIMS = 24
_MAX_SOURCES = 48
_MAX_EVIDENCE = 96
_FORBIDDEN_DTO_KEYS = frozenset(
    {
        "body",
        "content",
        "context",
        "email",
        "gmail",
        "message",
        "payload",
        "phone",
        "prompt",
        "raw",
        "request",
        "response",
        "text",
        "thread",
        "vin",
    }
)
_CLAIM_KEYS = frozenset(
    {
        "claim_id",
        "subject_ref",
        "predicate",
        "risk",
        "required_source_kinds",
        "depends_on",
        "freshness_seconds",
        "preferred_source_ids",
    }
)
_SOURCE_KEYS = frozenset({"source_id", "kind", "authority", "read_only", "available", "priority", "tags"})
_EVIDENCE_KEYS = frozenset(
    {
        "evidence_id",
        "claim_id",
        "source_id",
        "source_kind",
        "value",
        "observed_at",
        "applicability",
        "authority",
        "disposition",
        "expires_at",
        "correlation_key",
    }
)


def _invalid(error: str, **details: Any) -> dict[str, Any]:
    return {"ok": False, "schema": "AgentCaseResolverV1", "error": error, **details}


def _bounded_rows(value: Any, *, field: str, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty list")
    if len(value) > limit:
        raise ValueError(f"{field} exceeds its safe limit")
    if not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{field} must contain objects")
    return [dict(item) for item in value]


def _optional_rows(value: Any, *, field: str, limit: int) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    if len(value) > limit:
        raise ValueError(f"{field} exceeds its safe limit")
    if not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{field} must contain objects")
    return [dict(item) for item in value]


def _checked_row(row: dict[str, Any], *, allowed: frozenset[str], field: str) -> dict[str, Any]:
    normalized = {str(key).strip(): value for key, value in row.items()}
    lower_keys = {key.casefold() for key in normalized}
    forbidden = sorted(lower_keys.intersection(_FORBIDDEN_DTO_KEYS))
    unknown = sorted(set(normalized).difference(allowed))
    if forbidden or unknown:
        rejected = sorted(set(forbidden + unknown))
        raise ValueError(f"{field} contains unsupported fields: {', '.join(rejected)}")
    return normalized


def _safe_scalar(value: Any) -> str | int | float | bool | None:
    try:
        return validate_public_technical_evidence_value(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"evidence value violates public technical policy: {exc}") from exc


def _moment(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if moment.tzinfo is None:
        raise ValueError(f"{field} must include timezone")
    return moment


def _optional_moment(value: Any, *, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    return _moment(value, field=field)


def _optional_seconds(value: Any) -> timedelta | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 31_536_000:
        raise ValueError("freshness_seconds must be an integer from 1 to 31536000")
    return timedelta(seconds=value)


def _claim_from_row(row: dict[str, Any]) -> Claim:
    item = _checked_row(row, allowed=_CLAIM_KEYS, field="claim")
    return Claim(
        claim_id=str(item.get("claim_id") or ""),
        subject_ref=str(item.get("subject_ref") or ""),
        predicate=str(item.get("predicate") or ""),
        risk=str(item.get("risk") or "normal"),
        required_source_kinds=tuple(item.get("required_source_kinds") or ()),
        depends_on=tuple(item.get("depends_on") or ()),
        freshness_window=_optional_seconds(item.get("freshness_seconds")),
        preferred_source_ids=tuple(item.get("preferred_source_ids") or ()),
    )


def _source_from_row(row: dict[str, Any]) -> SourceDescriptor:
    item = _checked_row(row, allowed=_SOURCE_KEYS, field="source")
    return SourceDescriptor(
        source_id=str(item.get("source_id") or ""),
        kind=str(item.get("kind") or ""),
        authority=item.get("authority"),
        read_only=bool(item.get("read_only", True)),
        available=bool(item.get("available", True)),
        priority=int(item.get("priority") or 0),
        tags=tuple(item.get("tags") or ()),
    )


def _evidence_from_row(row: dict[str, Any]) -> EvidenceRecord:
    item = _checked_row(row, allowed=_EVIDENCE_KEYS, field="evidence")
    return EvidenceRecord(
        evidence_id=str(item.get("evidence_id") or ""),
        claim_id=str(item.get("claim_id") or ""),
        source_id=str(item.get("source_id") or ""),
        source_kind=str(item.get("source_kind") or ""),
        value=_safe_scalar(item.get("value")),
        observed_at=_moment(item.get("observed_at"), field="observed_at"),
        applicability=float(item.get("applicability", 1.0)),
        authority=item.get("authority"),
        disposition=str(item.get("disposition") or "supports"),
        expires_at=_optional_moment(item.get("expires_at"), field="expires_at"),
        correlation_key=str(item["correlation_key"]) if item.get("correlation_key") not in (None, "") else None,
    )


def _plan_payload(plan: ReadPlan) -> dict[str, Any]:
    batches = plan.topological_batches()
    return {
        "case_id": plan.case_id,
        "summary": plan.summary(),
        "warnings": list(plan.warnings),
        "source_catalog_warnings": list(plan.source_catalog_warnings),
        "batches": [
            [
                {
                    "step_id": step.step_id,
                    "claim_id": step.claim_id,
                    "source_id": step.source_id,
                    "source_kind": step.source_kind.value,
                    "phase": step.phase,
                    "depends_on": list(step.depends_on),
                    "expected_score": step.expected_score,
                    "reason": step.reason,
                    "read_only": step.read_only,
                }
                for step in batch
            ]
            for batch in batches
        ],
    }


def _resolution_payload(resolution: Any) -> dict[str, Any]:
    return {
        "generated_at": resolution.generated_at.isoformat(),
        "is_complete": resolution.is_complete,
        "summary": resolution.summary(),
        "claims": [
            {
                "claim_id": claim.claim_id,
                "status": claim.status.value,
                "display_value": claim.display_value,
                "confidence": claim.confidence,
                "source_ids": list(claim.source_ids),
                "supporting_evidence_ids": list(claim.supporting_evidence_ids),
                "conflicting_evidence_ids": list(claim.conflicting_evidence_ids),
                "issues": list(claim.issues),
            }
            for claim in resolution.claims
        ],
    }


def agent_case_resolver(
    operation: str,
    case_id: str,
    claims: list[dict[str, Any]],
    sources: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    brand: str | None = None,
    data_type: str | None = None,
    include_licensed: bool = True,
    include_forums: bool = False,
    max_sources_per_claim: int = 3,
) -> dict[str, Any]:
    """Plan or reconcile a focused multi-source case without connector calls.

    ``plan`` returns a read-only execution DAG. ``reconcile`` additionally
    needs scalar evidence records and returns redacted display values only.
    It never writes CRM, Store, Gmail, memory, a workflow ledger, or files.
    """

    normalized_operation = str(operation or "").strip().casefold()
    if normalized_operation not in {"plan", "reconcile"}:
        return _invalid("invalid_agent_case_resolver_operation", supported_operations=["plan", "reconcile"])
    try:
        claim_rows = _bounded_rows(claims, field="claims", limit=_MAX_CLAIMS)
        source_rows = _optional_rows(sources, field="sources", limit=_MAX_SOURCES)
        evidence_rows = _optional_rows(evidence, field="evidence", limit=_MAX_EVIDENCE)
        if normalized_operation == "reconcile" and not evidence_rows:
            raise ValueError("evidence is required for reconcile")
        request = CaseRequest(
            case_id=str(case_id or ""),
            claims=tuple(_claim_from_row(row) for row in claim_rows),
            brand=str(brand).strip() if brand is not None else None,
            data_type=str(data_type).strip() if data_type is not None else None,
            available_sources=tuple(_source_from_row(row) for row in source_rows),
            include_licensed=bool(include_licensed),
            include_forums=bool(include_forums),
            max_sources_per_claim=int(max_sources_per_claim),
        )
        resolver = CaseResolver()
        plan = resolver.plan(request)
        result: dict[str, Any] = {
            "ok": True,
            "schema": "AgentCaseResolverV1",
            "operation": normalized_operation,
            "plan": _plan_payload(plan),
            "writes": [],
        }
        if normalized_operation == "reconcile":
            resolution = resolver.reconcile(request, tuple(_evidence_from_row(row) for row in evidence_rows))
            result["resolution"] = _resolution_payload(resolution)
        return result
    except (TypeError, ValueError) as exc:
        # Exceptions contain field/policy names only; never echo the rejected
        # DTO, a prompt, or a connector response back to the caller.
        return _invalid("invalid_agent_case_resolver_input", reason=str(exc)[:240])
