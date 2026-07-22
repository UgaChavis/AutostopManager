"""Pure evidence reconciliation primitives for adaptive read-only work.

This module deliberately has no persistence, connector, or workflow dependency.
Callers may pass focused live facts to it, but it never writes them to disk and
the human-facing explanation helpers redact common personal identifiers.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from math import isfinite, prod
import re
from typing import TypeAlias


Scalar: TypeAlias = str | int | float | bool | None

__all__ = [
    "BundleResolution",
    "CandidateResolution",
    "Claim",
    "EvidenceBundle",
    "EvidenceDisposition",
    "EvidenceRecord",
    "EvidenceScore",
    "ResolutionStatus",
    "ResolvedClaim",
    "RiskLevel",
    "SourceDescriptor",
    "SourceKind",
    "compact_identifier",
    "default_authority",
    "default_freshness_window",
    "safe_display_value",
    "score_evidence",
    "validate_public_technical_evidence_value",
    "value_fingerprint",
]


class SourceKind(StrEnum):
    """A source role, rather than a concrete connector implementation."""

    CRM = "crm"
    STORE = "store"
    GMAIL = "gmail"
    OEM = "oem"
    LICENSED = "licensed"
    OFFICIAL = "official"
    SUPPLIER = "supplier"
    PUBLIC_WEB = "public_web"
    FORUM = "forum"
    MANAGER_MEMORY = "manager_memory"
    INTERNAL_RULE = "internal_rule"


class RiskLevel(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"
    FINANCIAL = "financial"


class EvidenceDisposition(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    UNAVAILABLE = "unavailable"


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    CONFLICT = "conflict"
    INSUFFICIENT = "insufficient"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
_VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b", flags=re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+7|[78])[\s()\-]*\d(?:[\s()\-]*\d){9}(?!\w)")
_INTERNATIONAL_PHONE_RE = re.compile(r"(?<![\w+])\+\d(?:[\s().\-]*\d){6,14}(?!\w)")
_PLATE_LETTERS = "ABEKMHOPCTYXАВЕКМНОРСТУХ"
_PLATE_RE = re.compile(
    rf"(?<![A-Za-zА-Яа-яЁё0-9])[{_PLATE_LETTERS}]\d{{3}}[{_PLATE_LETTERS}]{{2}}\d{{2,3}}(?![A-Za-zА-Яа-яЁё0-9])",
    flags=re.IGNORECASE,
)
_PERSON_NAME_FULL_RE = re.compile(
    r"^(?:[A-Z][a-z]{1,30}(?:[ -][A-Z][a-z]{1,30}){1,2}|[А-ЯЁ][а-яё]{1,30}(?:[ -][А-ЯЁ][а-яё]{1,30}){1,2})$"
)
_PERSON_NAME_IN_TEXT_RE = re.compile(
    r"(?<![A-Za-zА-Яа-яЁё])(?:[A-Z][a-z]{1,30}(?:[ -][A-Z][a-z]{1,30}){1,2}|[А-ЯЁ][а-яё]{1,30}(?:[ -][А-ЯЁ][а-яё]{1,30}){1,2})(?![A-Za-zА-Яа-яЁё])"
)
_PUBLIC_TECHNICAL_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:/+\-]*(?: [A-Za-z0-9][A-Za-z0-9.:/+\-]*){0,3}$")
_PUBLIC_TECHNICAL_ENUM_RE = re.compile(r"^[a-zа-яё][a-zа-яё0-9]*(?:_[a-zа-яё0-9]+){0,5}$", flags=re.IGNORECASE)
_PUBLIC_TECHNICAL_STATES = frozenset(
    {
        "available",
        "confirmed",
        "contradicts",
        "not_applicable",
        "not_confirmed",
        "requires_verification",
        "supports",
        "unavailable",
        "unknown",
    }
)
_PUBLIC_TECHNICAL_TOKENS = frozenset(
    {
        "adjust",
        "article",
        "belt",
        "brake",
        "capacity",
        "chain",
        "code",
        "compatible",
        "diagnostic",
        "dtc",
        "engine",
        "filter",
        "fitment",
        "fluid",
        "gearbox",
        "incompatible",
        "inspect",
        "install",
        "interval",
        "oil",
        "oem",
        "part",
        "pressure",
        "program",
        "repair",
        "replace",
        "service",
        "temperature",
        "torque",
        "transmission",
        "voltage",
        "цепь",
        "диагностика",
        "двигатель",
        "давление",
        "замена",
        "коробка",
        "масло",
        "момент",
        "объем",
        "ошибка",
        "ремень",
        "тормоз",
        "трансмиссия",
        "фильтр",
        "температура",
    }
)
_MAX_PUBLIC_TECHNICAL_VALUE_LENGTH = 96

_DEFAULT_AUTHORITY: dict[SourceKind, float] = {
    SourceKind.CRM: 0.90,
    SourceKind.STORE: 0.90,
    SourceKind.GMAIL: 0.75,
    SourceKind.OEM: 0.98,
    SourceKind.LICENSED: 0.92,
    SourceKind.OFFICIAL: 0.88,
    SourceKind.SUPPLIER: 0.76,
    SourceKind.PUBLIC_WEB: 0.45,
    SourceKind.FORUM: 0.25,
    SourceKind.MANAGER_MEMORY: 0.45,
    SourceKind.INTERNAL_RULE: 0.65,
}

_DEFAULT_FRESHNESS: dict[SourceKind, timedelta] = {
    SourceKind.CRM: timedelta(hours=12),
    SourceKind.STORE: timedelta(hours=4),
    SourceKind.GMAIL: timedelta(hours=24),
    SourceKind.OEM: timedelta(days=365),
    SourceKind.LICENSED: timedelta(days=180),
    SourceKind.OFFICIAL: timedelta(days=180),
    SourceKind.SUPPLIER: timedelta(hours=2),
    SourceKind.PUBLIC_WEB: timedelta(days=30),
    SourceKind.FORUM: timedelta(days=90),
    SourceKind.MANAGER_MEMORY: timedelta(days=30),
    SourceKind.INTERNAL_RULE: timedelta(days=90),
}

_MIN_CONFIDENCE: dict[RiskLevel, float] = {
    RiskLevel.LOW: 0.35,
    RiskLevel.NORMAL: 0.55,
    RiskLevel.HIGH: 0.68,
    RiskLevel.CRITICAL: 0.80,
    RiskLevel.FINANCIAL: 0.70,
}


def compact_identifier(value: str, field: str = "identifier") -> str:
    """Validate an opaque reference used in a plan without accepting raw text."""
    normalized = str(value or "").strip()
    if (
        not _SAFE_ID_RE.fullmatch(normalized)
        or _VIN_RE.search(normalized)
        or _EMAIL_RE.search(normalized)
        or _PHONE_RE.search(normalized)
    ):
        raise ValueError(f"{field} must be a compact non-sensitive identifier")
    return normalized


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _ensure_scalar(value: Scalar) -> Scalar:
    if not isinstance(value, (str, int, float, bool, type(None))):
        raise TypeError("evidence values must be scalar; keep raw connector payloads outside the bundle")
    if isinstance(value, float) and not isfinite(value):
        raise ValueError("evidence numeric values must be finite")
    return value


def _contains_personal_identifier(value: str) -> bool:
    """Detect identifiers that must never cross the public evidence DTO boundary."""
    normalized = value.strip()
    return bool(
        _VIN_RE.search(normalized)
        or _EMAIL_RE.search(normalized)
        or _PHONE_RE.search(normalized)
        or _INTERNATIONAL_PHONE_RE.search(normalized)
        or _PLATE_RE.search(normalized)
        or _PERSON_NAME_FULL_RE.fullmatch(normalized)
        or _PERSON_NAME_IN_TEXT_RE.search(normalized)
    )


def _is_public_technical_enum(value: str) -> bool:
    if not _PUBLIC_TECHNICAL_ENUM_RE.fullmatch(value) or value != value.casefold():
        return False
    parts = tuple(value.split("_"))
    return value in _PUBLIC_TECHNICAL_STATES or bool(set(parts).intersection(_PUBLIC_TECHNICAL_TOKENS))


def validate_public_technical_evidence_value(value: Scalar) -> Scalar:
    """Allow only a compact technical scalar through the public resolver DTO.

    The core :class:`EvidenceRecord` remains connector-neutral because internal
    callers may reconcile transient data before applying their own boundary.
    The Manager MCP endpoint is stricter: a value must be numeric/boolean/null,
    a compact technical code, or a deliberately technical enum.  This keeps
    names, contacts, registration plates, VINs, and free-form source text out
    of an output-bearing public interface.
    """
    checked = _ensure_scalar(value)
    if not isinstance(checked, str):
        return checked
    normalized = checked.strip()
    if not normalized or normalized != checked or len(normalized) > _MAX_PUBLIC_TECHNICAL_VALUE_LENGTH:
        raise ValueError("evidence value must be a compact technical scalar")
    if _contains_personal_identifier(normalized):
        raise ValueError("evidence value contains a personal identifier")
    if _PUBLIC_TECHNICAL_CODE_RE.fullmatch(normalized) and any(char.isdigit() or char in ".:/+" for char in normalized):
        return normalized
    if _is_public_technical_enum(normalized):
        return normalized
    raise ValueError("evidence value must be a compact technical code or approved technical enum")


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_value(value: Scalar) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return format(value, ".12g")
    if isinstance(value, int):
        return str(value)
    return " ".join(value.strip().casefold().split())


def value_fingerprint(value: Scalar) -> str:
    """Return an opaque, deterministic fingerprint without exposing a value."""
    return sha256(_canonical_value(value).encode("utf-8")).hexdigest()[:16]


def safe_display_value(value: Scalar, *, limit: int = 96) -> str:
    """Render a compact value while hiding common personal identifiers."""
    rendered = str(value)
    rendered = _EMAIL_RE.sub("[email hidden]", rendered)
    rendered = _VIN_RE.sub("[VIN hidden]", rendered)
    rendered = _PHONE_RE.sub("[phone hidden]", rendered)
    rendered = _INTERNATIONAL_PHONE_RE.sub("[phone hidden]", rendered)
    rendered = _PLATE_RE.sub("[plate hidden]", rendered)
    rendered = _PERSON_NAME_IN_TEXT_RE.sub("[name hidden]", rendered)
    if len(rendered) > limit:
        return f"{rendered[: max(1, limit - 1)]}…"
    return rendered


def default_authority(source_kind: SourceKind | str) -> float:
    return _DEFAULT_AUTHORITY[SourceKind(source_kind)]


def default_freshness_window(source_kind: SourceKind | str) -> timedelta:
    return _DEFAULT_FRESHNESS[SourceKind(source_kind)]


@dataclass(frozen=True)
class SourceDescriptor:
    """An available read path known to a caller, without credentials or payloads."""

    source_id: str
    kind: SourceKind | str
    authority: float | None = None
    read_only: bool = True
    available: bool = True
    priority: int = 0
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", compact_identifier(self.source_id, "source_id"))
        object.__setattr__(self, "kind", SourceKind(self.kind))
        if self.authority is not None:
            object.__setattr__(self, "authority", _clamp(self.authority))
        object.__setattr__(self, "tags", tuple(sorted({str(tag).strip() for tag in self.tags if str(tag).strip()})))

    @property
    def effective_authority(self) -> float:
        return default_authority(self.kind) if self.authority is None else self.authority

    @property
    def source_kind(self) -> SourceKind:
        return SourceKind(self.kind)


@dataclass(frozen=True)
class Claim:
    """A focused fact the resolver needs to establish.

    ``subject_ref`` must be a local opaque reference such as ``card:abc123``;
    raw VINs, phone numbers, emails, and full natural-language prompts do not
    belong in a plan or evidence bundle.
    """

    claim_id: str
    subject_ref: str
    predicate: str
    risk: RiskLevel | str = RiskLevel.NORMAL
    required_source_kinds: tuple[SourceKind | str, ...] = ()
    depends_on: tuple[str, ...] = ()
    freshness_window: timedelta | None = None
    preferred_source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim_id", compact_identifier(self.claim_id, "claim_id"))
        object.__setattr__(self, "subject_ref", compact_identifier(self.subject_ref, "subject_ref"))
        object.__setattr__(self, "predicate", compact_identifier(self.predicate, "predicate"))
        object.__setattr__(self, "risk", RiskLevel(self.risk))
        object.__setattr__(
            self,
            "required_source_kinds",
            tuple(SourceKind(kind) for kind in self.required_source_kinds),
        )
        object.__setattr__(
            self,
            "depends_on",
            tuple(compact_identifier(claim_id, "dependency claim_id") for claim_id in self.depends_on),
        )
        object.__setattr__(
            self,
            "preferred_source_ids",
            tuple(compact_identifier(source_id, "preferred_source_id") for source_id in self.preferred_source_ids),
        )
        if self.freshness_window is not None and self.freshness_window <= timedelta(0):
            raise ValueError("freshness_window must be positive")

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel(self.risk)

    @property
    def required_kinds(self) -> tuple[SourceKind, ...]:
        return tuple(SourceKind(kind) for kind in self.required_source_kinds)


@dataclass(frozen=True)
class EvidenceRecord:
    """One focused read result. It is intentionally transient and scalar-only."""

    evidence_id: str
    claim_id: str
    source_id: str
    source_kind: SourceKind | str
    value: Scalar
    observed_at: datetime
    applicability: float = 1.0
    authority: float | None = None
    disposition: EvidenceDisposition | str = EvidenceDisposition.SUPPORTS
    expires_at: datetime | None = None
    correlation_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_id", compact_identifier(self.evidence_id, "evidence_id"))
        object.__setattr__(self, "claim_id", compact_identifier(self.claim_id, "claim_id"))
        object.__setattr__(self, "source_id", compact_identifier(self.source_id, "source_id"))
        object.__setattr__(self, "source_kind", SourceKind(self.source_kind))
        object.__setattr__(self, "value", _ensure_scalar(self.value))
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))
        object.__setattr__(self, "applicability", _clamp(self.applicability))
        object.__setattr__(self, "disposition", EvidenceDisposition(self.disposition))
        if self.authority is not None:
            object.__setattr__(self, "authority", _clamp(self.authority))
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", _aware(self.expires_at, "expires_at"))
        if self.correlation_key is not None:
            object.__setattr__(self, "correlation_key", compact_identifier(self.correlation_key, "correlation_key"))

    @property
    def effective_authority(self) -> float:
        return default_authority(self.source_kind) if self.authority is None else self.authority

    @property
    def source_kind_enum(self) -> SourceKind:
        return SourceKind(self.source_kind)

    @property
    def value_key(self) -> str:
        return value_fingerprint(self.value)

    @property
    def effective_correlation_key(self) -> str:
        return self.correlation_key or self.source_id


@dataclass(frozen=True)
class EvidenceScore:
    evidence_id: str
    authority: float
    applicability: float
    freshness: float
    risk_suitability: float
    total: float
    is_stale: bool


@dataclass(frozen=True)
class CandidateResolution:
    value: Scalar
    value_fingerprint: str
    confidence: float
    freshness: float
    risk_suitability: float
    supporting_evidence_ids: tuple[str, ...]
    contradicting_evidence_ids: tuple[str, ...]
    source_ids: tuple[str, ...]
    source_kinds: tuple[SourceKind, ...]


@dataclass(frozen=True)
class ResolvedClaim:
    claim_id: str
    status: ResolutionStatus
    value: Scalar | None
    display_value: str | None
    confidence: float
    source_ids: tuple[str, ...]
    supporting_evidence_ids: tuple[str, ...]
    conflicting_evidence_ids: tuple[str, ...]
    issues: tuple[str, ...]


@dataclass(frozen=True)
class BundleResolution:
    claims: tuple[ResolvedClaim, ...]
    generated_at: datetime

    @property
    def is_complete(self) -> bool:
        return all(claim.status is ResolutionStatus.RESOLVED for claim in self.claims)

    def claim(self, claim_id: str) -> ResolvedClaim:
        normalized = compact_identifier(claim_id, "claim_id")
        for claim in self.claims:
            if claim.claim_id == normalized:
                return claim
        raise KeyError(normalized)

    def summary(self) -> str:
        counts: dict[ResolutionStatus, int] = defaultdict(int)
        for claim in self.claims:
            counts[claim.status] += 1
        parts = [f"{status.value}: {counts[status]}" for status in ResolutionStatus if counts[status]]
        return "Evidence bundle — " + (", ".join(parts) if parts else "no claims") + "."


def _risk_suitability(claim: Claim, source_kind: SourceKind) -> float:
    if claim.required_kinds:
        return 1.0 if source_kind in claim.required_kinds else 0.08
    if claim.risk_level is RiskLevel.FINANCIAL:
        return {
            SourceKind.STORE: 1.0,
            SourceKind.SUPPLIER: 1.0,
            SourceKind.CRM: 0.88,
            SourceKind.OEM: 0.45,
            SourceKind.LICENSED: 0.45,
            SourceKind.OFFICIAL: 0.40,
            SourceKind.GMAIL: 0.50,
            SourceKind.PUBLIC_WEB: 0.20,
            SourceKind.FORUM: 0.10,
            SourceKind.MANAGER_MEMORY: 0.20,
            SourceKind.INTERNAL_RULE: 0.25,
        }[source_kind]
    if claim.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
        return {
            SourceKind.OEM: 1.0,
            SourceKind.LICENSED: 0.94,
            SourceKind.OFFICIAL: 0.88,
            SourceKind.CRM: 0.75,
            SourceKind.STORE: 0.70,
            SourceKind.SUPPLIER: 0.60,
            SourceKind.GMAIL: 0.45,
            SourceKind.PUBLIC_WEB: 0.30,
            SourceKind.FORUM: 0.10,
            SourceKind.MANAGER_MEMORY: 0.25,
            SourceKind.INTERNAL_RULE: 0.40,
        }[source_kind]
    return {
        SourceKind.CRM: 1.0,
        SourceKind.STORE: 1.0,
        SourceKind.GMAIL: 0.85,
        SourceKind.OEM: 1.0,
        SourceKind.LICENSED: 0.95,
        SourceKind.OFFICIAL: 0.92,
        SourceKind.SUPPLIER: 0.80,
        SourceKind.PUBLIC_WEB: 0.62,
        SourceKind.FORUM: 0.38,
        SourceKind.MANAGER_MEMORY: 0.50,
        SourceKind.INTERNAL_RULE: 0.60,
    }[source_kind]


def _freshness_score(evidence: EvidenceRecord, claim: Claim, now: datetime) -> tuple[float, bool]:
    if evidence.expires_at is not None and evidence.expires_at <= now:
        return 0.05, True
    window = claim.freshness_window or default_freshness_window(evidence.source_kind_enum)
    age = max(timedelta(0), now - evidence.observed_at)
    ratio = age.total_seconds() / window.total_seconds()
    if ratio <= 1:
        return _clamp(1.0 - ratio * 0.15), False
    return max(0.05, 1.0 / (1.0 + ratio)), True


def score_evidence(evidence: EvidenceRecord, claim: Claim, *, now: datetime | None = None) -> EvidenceScore:
    """Score one item with transparent source, fitment, freshness, and risk inputs."""
    moment = _aware(now, "now") if now is not None else datetime.now(UTC)
    if evidence.disposition is EvidenceDisposition.UNAVAILABLE:
        return EvidenceScore(evidence.evidence_id, 0.0, 0.0, 0.0, 0.0, 0.0, True)
    freshness, is_stale = _freshness_score(evidence, claim, moment)
    authority = evidence.effective_authority
    suitability = _risk_suitability(claim, evidence.source_kind_enum)
    total = _clamp(authority * evidence.applicability * freshness * suitability)
    return EvidenceScore(
        evidence_id=evidence.evidence_id,
        authority=authority,
        applicability=evidence.applicability,
        freshness=freshness,
        risk_suitability=suitability,
        total=total,
        is_stale=is_stale,
    )


def _independent_confidence(scores: Iterable[float]) -> float:
    bounded = [_clamp(score) for score in scores if score > 0]
    return _clamp(1.0 - prod(1.0 - score for score in bounded)) if bounded else 0.0


class EvidenceBundle:
    """Immutable collection of focused evidence records and deterministic reconciliation."""

    def __init__(self, records: Iterable[EvidenceRecord] = ()) -> None:
        rows = tuple(records)
        ids = [row.evidence_id for row in rows]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence_id values must be unique")
        self._records = rows

    @property
    def records(self) -> tuple[EvidenceRecord, ...]:
        return self._records

    def add(self, *records: EvidenceRecord) -> EvidenceBundle:
        return EvidenceBundle((*self._records, *records))

    def for_claim(self, claim_id: str) -> tuple[EvidenceRecord, ...]:
        normalized = compact_identifier(claim_id, "claim_id")
        return tuple(record for record in self._records if record.claim_id == normalized)

    def reconcile(self, claims: Iterable[Claim], *, now: datetime | None = None) -> BundleResolution:
        moment = _aware(now, "now") if now is not None else datetime.now(UTC)
        claim_rows = tuple(claims)
        _validate_claims(claim_rows)
        resolved_by_id: dict[str, ResolvedClaim] = {}
        for claim in _claims_in_dependency_order(claim_rows):
            resolved = _reconcile_claim(claim, self.for_claim(claim.claim_id), moment)
            blocked_by = tuple(
                dependency
                for dependency in claim.depends_on
                if resolved_by_id[dependency].status is not ResolutionStatus.RESOLVED
            )
            if blocked_by:
                resolved = ResolvedClaim(
                    claim_id=resolved.claim_id,
                    status=ResolutionStatus.INSUFFICIENT,
                    value=None,
                    display_value=None,
                    confidence=resolved.confidence,
                    source_ids=resolved.source_ids,
                    supporting_evidence_ids=resolved.supporting_evidence_ids,
                    conflicting_evidence_ids=resolved.conflicting_evidence_ids,
                    issues=(*resolved.issues, f"blocked by unresolved dependency: {', '.join(blocked_by)}"),
                )
            resolved_by_id[claim.claim_id] = resolved
        return BundleResolution(
            claims=tuple(resolved_by_id[claim.claim_id] for claim in claim_rows),
            generated_at=moment,
        )


def _validate_claims(claims: tuple[Claim, ...]) -> None:
    claim_ids = [claim.claim_id for claim in claims]
    if len(claim_ids) != len(set(claim_ids)):
        raise ValueError("claim_id values must be unique")
    known_claim_ids = set(claim_ids)
    unknown_dependencies = {
        dependency for claim in claims for dependency in claim.depends_on if dependency not in known_claim_ids
    }
    if unknown_dependencies:
        raise ValueError(f"unknown claim dependencies: {', '.join(sorted(unknown_dependencies))}")


def _claims_in_dependency_order(claims: tuple[Claim, ...]) -> tuple[Claim, ...]:
    by_id = {claim.claim_id: claim for claim in claims}
    ordered: list[Claim] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(claim_id: str) -> None:
        if claim_id in visited:
            return
        if claim_id in visiting:
            raise ValueError("claim dependencies must be acyclic")
        visiting.add(claim_id)
        for dependency in by_id[claim_id].depends_on:
            visit(dependency)
        visiting.remove(claim_id)
        visited.add(claim_id)
        ordered.append(by_id[claim_id])

    for claim in claims:
        visit(claim.claim_id)
    return tuple(ordered)


def _reconcile_claim(claim: Claim, records: tuple[EvidenceRecord, ...], now: datetime) -> ResolvedClaim:
    unavailable = tuple(
        record.evidence_id for record in records if record.disposition is EvidenceDisposition.UNAVAILABLE
    )
    scored: list[tuple[EvidenceRecord, EvidenceScore]] = [
        (record, score_evidence(record, claim, now=now))
        for record in records
        if record.disposition is not EvidenceDisposition.UNAVAILABLE
    ]
    if not scored:
        status = ResolutionStatus.UNAVAILABLE if unavailable else ResolutionStatus.INSUFFICIENT
        return ResolvedClaim(
            claim_id=claim.claim_id,
            status=status,
            value=None,
            display_value=None,
            confidence=0.0,
            source_ids=(),
            supporting_evidence_ids=(),
            conflicting_evidence_ids=unavailable,
            issues=("no usable evidence",),
        )

    grouped: dict[str, list[tuple[EvidenceRecord, EvidenceScore]]] = defaultdict(list)
    for record, score in scored:
        grouped[record.value_key].append((record, score))

    candidates = [_candidate_resolution(rows) for rows in grouped.values()]
    candidates.sort(key=lambda candidate: (-candidate.confidence, candidate.value_fingerprint))
    selected = candidates[0]
    threshold = _MIN_CONFIDENCE[claim.risk_level]
    runner_up = candidates[1] if len(candidates) > 1 else None
    issues: list[str] = []

    if selected.confidence < threshold:
        status = ResolutionStatus.STALE if selected.freshness < 0.5 else ResolutionStatus.INSUFFICIENT
        issues.append(f"confidence {selected.confidence:.2f} below {threshold:.2f} for {claim.risk_level.value} claim")
    elif claim.required_kinds and not set(selected.source_kinds).intersection(claim.required_kinds):
        status = ResolutionStatus.INSUFFICIENT
        issues.append("required source kind is absent")
    elif runner_up is not None and runner_up.confidence >= max(0.35, selected.confidence * 0.75):
        status = ResolutionStatus.CONFLICT
        issues.append("independent sources support competing values")
    elif selected.freshness < 0.5:
        status = ResolutionStatus.STALE
        issues.append("best evidence is stale")
    else:
        status = ResolutionStatus.RESOLVED

    conflicting_ids = list(unavailable)
    if runner_up is not None and status is ResolutionStatus.CONFLICT:
        conflicting_ids.extend(runner_up.supporting_evidence_ids)
        conflicting_ids.extend(runner_up.contradicting_evidence_ids)
    return ResolvedClaim(
        claim_id=claim.claim_id,
        status=status,
        value=selected.value if status is ResolutionStatus.RESOLVED else None,
        display_value=safe_display_value(selected.value) if status is ResolutionStatus.RESOLVED else None,
        confidence=selected.confidence,
        source_ids=selected.source_ids,
        supporting_evidence_ids=selected.supporting_evidence_ids,
        conflicting_evidence_ids=tuple(sorted(set(conflicting_ids))),
        issues=tuple(issues),
    )


def _candidate_resolution(rows: list[tuple[EvidenceRecord, EvidenceScore]]) -> CandidateResolution:
    first_record = rows[0][0]
    support_by_group: dict[str, tuple[EvidenceRecord, EvidenceScore]] = {}
    contradiction_by_group: dict[str, tuple[EvidenceRecord, EvidenceScore]] = {}
    for record, score in rows:
        target = support_by_group if record.disposition is EvidenceDisposition.SUPPORTS else contradiction_by_group
        current = target.get(record.effective_correlation_key)
        if current is None or score.total > current[1].total:
            target[record.effective_correlation_key] = (record, score)

    support = _independent_confidence(score.total for _, score in support_by_group.values())
    contradiction = _independent_confidence(score.total for _, score in contradiction_by_group.values())
    confidence = _clamp(support * (1.0 - contradiction * 0.75))
    selected_rows = tuple(support_by_group.values())
    all_support = tuple(record for record, _ in selected_rows)
    all_scores = tuple(score for _, score in selected_rows)
    return CandidateResolution(
        value=first_record.value,
        value_fingerprint=first_record.value_key,
        confidence=confidence,
        freshness=max((score.freshness for score in all_scores), default=0.0),
        risk_suitability=max((score.risk_suitability for score in all_scores), default=0.0),
        supporting_evidence_ids=tuple(sorted(record.evidence_id for record in all_support)),
        contradicting_evidence_ids=tuple(sorted(record.evidence_id for record, _ in contradiction_by_group.values())),
        source_ids=tuple(sorted({record.source_id for record in all_support})),
        source_kinds=tuple(sorted({record.source_kind_enum for record in all_support}, key=lambda kind: kind.value)),
    )
