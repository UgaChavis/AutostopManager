"""Adaptive, read-only case planning built on focused evidence primitives.

The resolver does not call connectors. It turns a caller-provided set of
claims and available sources into a deterministic DAG of read steps, then can
reconcile the evidence returned by an executor. Keeping planning separate from
execution makes the layer testable and safe to attach to CRM, Store, Gmail, or
public research later.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from .evidence_bundle import (
    BundleResolution,
    Claim,
    EvidenceBundle,
    EvidenceRecord,
    RiskLevel,
    SourceDescriptor,
    SourceKind,
    claims_in_dependency_order,
    compact_identifier,
    default_authority,
)
from .source_catalog import recommend_automotive_sources

__all__ = [
    "CaseRequest",
    "CaseResolution",
    "CaseResolver",
    "ReadPlan",
    "ReadStep",
    "build_read_only_plan",
    "catalog_source_descriptors",
]


@dataclass(frozen=True)
class CaseRequest:
    """A privacy-safe planning input; it intentionally has no raw prompt field."""

    case_id: str
    claims: tuple[Claim, ...]
    brand: str | None = None
    data_type: str | None = None
    available_sources: tuple[SourceDescriptor, ...] = ()
    include_licensed: bool = True
    include_forums: bool = False
    max_sources_per_claim: int = 3

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", compact_identifier(self.case_id, "case_id"))
        if not self.claims:
            raise ValueError("CaseRequest needs at least one claim")
        if not 1 <= self.max_sources_per_claim <= 6:
            raise ValueError("max_sources_per_claim must be between 1 and 6")
        source_ids = [source.source_id for source in self.available_sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("available source_id values must be unique")


@dataclass(frozen=True)
class ReadStep:
    """One connector-neutral, always read-only action in an execution DAG."""

    step_id: str
    claim_id: str
    source_id: str
    source_kind: SourceKind
    phase: int
    depends_on: tuple[str, ...]
    expected_score: float
    reason: str
    read_only: bool = True


@dataclass(frozen=True)
class ReadPlan:
    case_id: str
    steps: tuple[ReadStep, ...]
    warnings: tuple[str, ...]
    source_catalog_warnings: tuple[str, ...] = ()

    def topological_batches(self) -> tuple[tuple[ReadStep, ...], ...]:
        """Return independent read groups; a malformed plan fails closed."""
        remaining = {step.step_id: step for step in self.steps}
        done: set[str] = set()
        batches: list[tuple[ReadStep, ...]] = []
        while remaining:
            ready = tuple(
                sorted(
                    (step for step in remaining.values() if set(step.depends_on).issubset(done)),
                    key=lambda step: (step.phase, step.step_id),
                )
            )
            if not ready:
                raise ValueError("read plan has a dependency cycle or an unknown dependency")
            batches.append(ready)
            for step in ready:
                done.add(step.step_id)
                remaining.pop(step.step_id)
        return tuple(batches)

    def summary(self) -> str:
        primary = sum(1 for step in self.steps if step.phase == 0)
        fallback = len(self.steps) - primary
        return f"Read-only plan: {primary} primary reads, {fallback} fallback reads, {len(self.warnings)} warnings."


@dataclass(frozen=True)
class CaseResolution:
    plan: ReadPlan
    evidence: BundleResolution

    def summary(self) -> str:
        return f"{self.plan.summary()} {self.evidence.summary()}"


def _catalog_source_kind(row: dict[str, object]) -> SourceKind:
    category = str(row.get("category") or "").strip().lower()
    source_id = str(row.get("source_id") or "").strip().lower()
    if "forum" in category or "forum" in source_id:
        return SourceKind.FORUM
    if category in {"oem_service_portal", "oem_parts_catalog", "oem_owner_manuals"}:
        return SourceKind.OEM
    if category in {"open_government_data", "government_data", "official_recall_database"}:
        return SourceKind.OFFICIAL
    if bool(row.get("requires_license")) or str(row.get("access") or "").startswith("paid"):
        return SourceKind.LICENSED
    return SourceKind.PUBLIC_WEB


def catalog_source_descriptors(
    *,
    brand: str | None = None,
    data_type: str | None = None,
    include_licensed: bool = True,
    include_forums: bool = False,
    limit: int = 12,
) -> tuple[tuple[SourceDescriptor, ...], tuple[str, ...]]:
    """Translate the canonical source catalog into connector-neutral read routes."""
    recommendation = recommend_automotive_sources(
        brand=brand,
        data_type=data_type,
        include_licensed=include_licensed,
        limit=limit,
    )
    descriptors: list[SourceDescriptor] = []
    for row in recommendation.get("sources", []):
        if not isinstance(row, dict):
            continue
        kind = _catalog_source_kind(row)
        if kind is SourceKind.FORUM and not include_forums:
            continue
        source_id = str(row.get("source_id") or "").strip()
        if not source_id:
            continue
        priority = int(row.get("priority_score_1_5") or 0)
        descriptors.append(
            SourceDescriptor(
                source_id=source_id,
                kind=kind,
                authority=min(1.0, default_authority(kind) + priority * 0.01),
                priority=priority,
                tags=tuple(
                    tag
                    for tag, present in (
                        ("brand_match", bool(row.get("brand_match"))),
                        ("data_type_match", bool(row.get("data_type_match"))),
                    )
                    if present
                ),
            )
        )
    return tuple(descriptors), tuple(str(warning) for warning in recommendation.get("warnings", []) if warning)


def _source_selection_score(source: SourceDescriptor, claim: Claim) -> float:
    if not source.available or not source.read_only:
        return 0.0
    source_kind = source.source_kind
    risk_fit = 1.0
    if claim.required_kinds:
        risk_fit = 1.0 if source_kind in claim.required_kinds else 0.08
    elif claim.risk_level is RiskLevel.FINANCIAL:
        risk_fit = 1.0 if source_kind in {SourceKind.STORE, SourceKind.SUPPLIER, SourceKind.CRM} else 0.25
    elif claim.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
        risk_fit = 1.0 if source_kind in {SourceKind.OEM, SourceKind.LICENSED, SourceKind.OFFICIAL} else 0.45
    preferred = 0.08 if source.source_id in claim.preferred_source_ids else 0.0
    tag_bonus = 0.03 * len({"brand_match", "data_type_match"}.intersection(source.tags))
    # Leave headroom for exact catalog matches. Otherwise multiple high-quality
    # OEM sources all cap at 1.0 and a different brand can win alphabetically.
    return min(1.0, source.effective_authority * risk_fit * 0.90 + preferred + tag_bonus + source.priority * 0.005)


def _merged_sources(request: CaseRequest) -> tuple[tuple[SourceDescriptor, ...], tuple[str, ...]]:
    if request.brand or request.data_type:
        catalog_sources, catalog_warnings = catalog_source_descriptors(
            brand=request.brand,
            data_type=request.data_type,
            include_licensed=request.include_licensed,
            include_forums=request.include_forums,
        )
    else:
        catalog_sources, catalog_warnings = (), ()
    sources_by_id: dict[str, SourceDescriptor] = {source.source_id: source for source in catalog_sources}
    # An actually connected source supplied by the runtime overrides a catalog
    # route with the same id, while retaining catalog suggestions as fallbacks.
    sources_by_id.update({source.source_id: source for source in request.available_sources})
    return tuple(sources_by_id[source_id] for source_id in sorted(sources_by_id)), catalog_warnings


def build_read_only_plan(request: CaseRequest) -> ReadPlan:
    """Build a deterministic DAG of focused, read-only source calls.

    Primary sources for independent claims can run in parallel. Secondary
    sources wait for the claim's primary source, which lets an executor avoid
    unnecessary public/API work while keeping a defined fallback path.
    """
    sources, catalog_warnings = _merged_sources(request)
    steps: list[ReadStep] = []
    primary_by_claim: dict[str, str] = {}
    selected_by_claim: dict[str, tuple[SourceDescriptor, ...]] = {}
    warnings: list[str] = []

    for claim in claims_in_dependency_order(request.claims):
        unavailable_dependencies = [dependency for dependency in claim.depends_on if dependency not in primary_by_claim]
        if unavailable_dependencies:
            selected_by_claim[claim.claim_id] = ()
            warnings.append(
                f"Claim {claim.claim_id} is blocked because dependency routes are unavailable: "
                f"{', '.join(unavailable_dependencies)}."
            )
            continue
        ranked = sorted(
            ((source, _source_selection_score(source, claim)) for source in sources),
            key=lambda row: (-row[1], row[0].source_id),
        )
        selected = tuple(
            source
            for source, score in ranked
            if score > 0 and (not claim.required_kinds or source.source_kind in claim.required_kinds)
        )[: request.max_sources_per_claim]
        selected_by_claim[claim.claim_id] = selected
        if not selected:
            warnings.append(f"No read-only source is available for claim {claim.claim_id}.")
            continue
        primary_by_claim[claim.claim_id] = f"{request.case_id}:{claim.claim_id}:{selected[0].source_id}:p0"

        if claim.required_kinds and not any(source.source_kind in claim.required_kinds for source in selected):
            required = ", ".join(kind.value for kind in claim.required_kinds)
            warnings.append(f"Claim {claim.claim_id} has no selected required source kind: {required}.")

    for claim in request.claims:
        selected = selected_by_claim[claim.claim_id]
        for phase, source in enumerate(selected):
            step_id = f"{request.case_id}:{claim.claim_id}:{source.source_id}:p{phase}"
            dependencies = [primary_by_claim[dependency] for dependency in claim.depends_on]
            if phase > 0 and claim.claim_id in primary_by_claim:
                dependencies.append(primary_by_claim[claim.claim_id])
            reason_parts = [f"{source.source_kind.value} source ranked for {claim.risk_level.value} claim"]
            if source.source_id in claim.preferred_source_ids:
                reason_parts.append("caller preference")
            if source.tags:
                reason_parts.append("catalog match")
            steps.append(
                ReadStep(
                    step_id=step_id,
                    claim_id=claim.claim_id,
                    source_id=source.source_id,
                    source_kind=source.source_kind,
                    phase=phase,
                    depends_on=tuple(sorted(set(dependencies))),
                    expected_score=round(_source_selection_score(source, claim), 4),
                    reason="; ".join(reason_parts),
                )
            )

    plan = ReadPlan(
        case_id=request.case_id,
        steps=tuple(steps),
        warnings=tuple(sorted(set(warnings))),
        source_catalog_warnings=catalog_warnings,
    )
    # Validate the generated graph eagerly, before a caller dispatches a tool.
    plan.topological_batches()
    return plan


class CaseResolver:
    """Thin orchestration facade for planners and connector-neutral evidence."""

    def plan(self, request: CaseRequest) -> ReadPlan:
        return build_read_only_plan(request)

    def reconcile(
        self,
        request: CaseRequest,
        evidence: Iterable[EvidenceRecord] | EvidenceBundle,
        *,
        now: datetime | None = None,
    ) -> BundleResolution:
        bundle = evidence if isinstance(evidence, EvidenceBundle) else EvidenceBundle(evidence)
        return bundle.reconcile(request.claims, now=now)

    def resolve(
        self,
        request: CaseRequest,
        evidence: Iterable[EvidenceRecord] | EvidenceBundle,
        *,
        now: datetime | None = None,
    ) -> CaseResolution:
        return CaseResolution(plan=self.plan(request), evidence=self.reconcile(request, evidence, now=now))
