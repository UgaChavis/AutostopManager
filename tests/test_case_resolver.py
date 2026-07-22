from __future__ import annotations

from datetime import UTC, datetime

import pytest

from autostop_manager.case_resolver import CaseRequest, CaseResolver, build_read_only_plan
from autostop_manager.evidence_bundle import Claim, EvidenceRecord, RiskLevel, SourceDescriptor, SourceKind


def test_read_plan_orders_vin_specific_part_lookup_after_identity_and_keeps_steps_read_only():
    request = CaseRequest(
        case_id="case_001",
        claims=(
            Claim(
                claim_id="identity",
                subject_ref="card:abc123",
                predicate="vehicle_identity",
                required_source_kinds=(SourceKind.CRM,),
            ),
            Claim(
                claim_id="oem_part",
                subject_ref="card:abc123",
                predicate="oem_part_number",
                risk=RiskLevel.HIGH,
                required_source_kinds=(SourceKind.OEM,),
                depends_on=("identity",),
            ),
        ),
        available_sources=(
            SourceDescriptor("crm_context", SourceKind.CRM),
            SourceDescriptor("parts_catalogs_api", SourceKind.OEM),
        ),
        max_sources_per_claim=2,
    )

    plan = build_read_only_plan(request)
    identity_primary = next(step for step in plan.steps if step.step_id.endswith("identity:crm_context:p0"))
    oem_primary = next(step for step in plan.steps if step.step_id.endswith("oem_part:parts_catalogs_api:p0"))
    batches = plan.topological_batches()

    assert identity_primary in batches[0]
    assert identity_primary.step_id in oem_primary.depends_on
    assert all(step.read_only for step in plan.steps)
    assert all(step.source_id in {"crm_context", "parts_catalogs_api"} for step in plan.steps)


def test_catalog_routes_are_used_when_vehicle_brand_and_technical_data_type_are_known():
    request = CaseRequest(
        case_id="case_toyota_manual",
        claims=(
            Claim(
                claim_id="procedure",
                subject_ref="vehicle:known",
                predicate="repair_procedure",
                risk=RiskLevel.HIGH,
                required_source_kinds=(SourceKind.OEM,),
            ),
        ),
        brand="Toyota",
        data_type="repair_manuals",
    )

    plan = build_read_only_plan(request)

    assert any(step.source_id == "toyota_tis_na" for step in plan.steps)
    assert all(step.read_only for step in plan.steps)


def test_claim_dependency_cycle_fails_before_any_source_is_planned():
    request = CaseRequest(
        case_id="case_cycle",
        claims=(
            Claim("first", "card:abc123", "first_fact", depends_on=("second",)),
            Claim("second", "card:abc123", "second_fact", depends_on=("first",)),
        ),
    )

    with pytest.raises(ValueError, match="acyclic"):
        build_read_only_plan(request)


def test_claim_is_blocked_when_its_identity_dependency_has_no_read_route():
    request = CaseRequest(
        case_id="case_blocked",
        claims=(
            Claim(
                "identity",
                "card:abc123",
                "vehicle_identity",
                required_source_kinds=(SourceKind.CRM,),
            ),
            Claim(
                "oem_part",
                "card:abc123",
                "oem_part_number",
                required_source_kinds=(SourceKind.OEM,),
                depends_on=("identity",),
            ),
        ),
        available_sources=(SourceDescriptor("parts_catalogs_api", SourceKind.OEM),),
    )

    plan = build_read_only_plan(request)

    assert plan.steps == ()
    assert any("blocked" in warning for warning in plan.warnings)


def test_resolver_reconciles_without_storing_a_raw_prompt_or_connector_payload():
    moment = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    request = CaseRequest(
        case_id="case_reconcile",
        claims=(
            Claim(
                claim_id="part",
                subject_ref="card:abc123",
                predicate="oem_part_number",
                required_source_kinds=(SourceKind.OEM,),
            ),
        ),
        available_sources=(SourceDescriptor("parts_catalogs_api", SourceKind.OEM),),
    )

    resolution = CaseResolver().resolve(
        request,
        (
            EvidenceRecord(
                evidence_id="epc_result",
                claim_id="part",
                source_id="parts_catalogs_api",
                source_kind=SourceKind.OEM,
                value="A1678350400",
                observed_at=moment,
            ),
        ),
        now=moment,
    )

    assert resolution.evidence.claim("part").value == "A1678350400"
    assert not hasattr(request, "prompt")
    assert "resolved: 1" in resolution.summary()
