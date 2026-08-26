from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from autostop_manager.evidence_bundle import (
    Claim,
    EvidenceBundle,
    EvidenceRecord,
    ResolutionStatus,
    RiskLevel,
    SourceKind,
    safe_display_value,
)


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def test_high_risk_oem_evidence_beats_multiple_forum_hypotheses():
    claim = Claim(
        claim_id="oem_part",
        subject_ref="vehicle:card123",
        predicate="oem_part_number",
        risk=RiskLevel.HIGH,
        required_source_kinds=(SourceKind.OEM,),
    )
    bundle = EvidenceBundle(
        (
            EvidenceRecord(
                evidence_id="epc_1",
                claim_id="oem_part",
                source_id="brand_epc",
                source_kind=SourceKind.OEM,
                value="A1234567890",
                observed_at=NOW,
                applicability=1.0,
            ),
            EvidenceRecord(
                evidence_id="forum_1",
                claim_id="oem_part",
                source_id="forum_one",
                source_kind=SourceKind.FORUM,
                value="B111",
                observed_at=NOW,
            ),
            EvidenceRecord(
                evidence_id="forum_2",
                claim_id="oem_part",
                source_id="forum_two",
                source_kind=SourceKind.FORUM,
                value="B111",
                observed_at=NOW,
            ),
        )
    )

    result = bundle.reconcile((claim,), now=NOW).claim("oem_part")

    assert result.status is ResolutionStatus.RESOLVED
    assert result.value == "A1234567890"
    assert result.source_ids == ("brand_epc",)


def test_independent_current_price_sources_produce_a_conflict_not_a_blended_price():
    claim = Claim(
        claim_id="part_price",
        subject_ref="part:abc123",
        predicate="procurement_price",
        risk=RiskLevel.FINANCIAL,
        required_source_kinds=(SourceKind.STORE, SourceKind.SUPPLIER),
    )
    bundle = EvidenceBundle(
        (
            EvidenceRecord(
                evidence_id="store_1",
                claim_id="part_price",
                source_id="autostop_store",
                source_kind=SourceKind.STORE,
                value=1000,
                observed_at=NOW,
            ),
            EvidenceRecord(
                evidence_id="supplier_1",
                claim_id="part_price",
                source_id="rossko",
                source_kind=SourceKind.SUPPLIER,
                value=1100,
                observed_at=NOW,
            ),
        )
    )

    result = bundle.reconcile((claim,), now=NOW).claim("part_price")

    assert result.status is ResolutionStatus.CONFLICT
    assert result.value is None
    assert set(result.conflicting_evidence_ids) == {"supplier_1"}


def test_old_store_value_is_stale_instead_of_a_current_answer():
    claim = Claim(
        claim_id="store_price",
        subject_ref="part:abc123",
        predicate="retail_price",
        risk=RiskLevel.NORMAL,
    )
    bundle = EvidenceBundle(
        (
            EvidenceRecord(
                evidence_id="store_old",
                claim_id="store_price",
                source_id="autostop_store",
                source_kind=SourceKind.STORE,
                value=1200,
                observed_at=NOW - timedelta(days=2),
            ),
        )
    )

    result = bundle.reconcile((claim,), now=NOW).claim("store_price")

    assert result.status is ResolutionStatus.STALE
    assert result.value is None
    assert "below" in result.issues[0]


def test_same_correlation_group_does_not_double_count_copied_web_evidence():
    claim = Claim("repair_hint", "vehicle:card123", "repair_hint", risk=RiskLevel.NORMAL)
    bundle = EvidenceBundle(
        (
            EvidenceRecord(
                evidence_id="web_1",
                claim_id="repair_hint",
                source_id="search_one",
                source_kind=SourceKind.PUBLIC_WEB,
                correlation_key="mirror:one",
                value="replace_belt",
                observed_at=NOW,
            ),
            EvidenceRecord(
                evidence_id="web_2",
                claim_id="repair_hint",
                source_id="search_two",
                source_kind=SourceKind.PUBLIC_WEB,
                correlation_key="mirror:one",
                value="replace_belt",
                observed_at=NOW,
            ),
        )
    )

    result = bundle.reconcile((claim,), now=NOW).claim("repair_hint")

    assert result.confidence < 0.5


def test_dependent_claim_is_not_returned_when_identity_evidence_is_unavailable():
    identity = Claim(
        "identity",
        "card:abc123",
        "vehicle_identity",
        required_source_kinds=(SourceKind.CRM,),
    )
    oem_part = Claim(
        "oem_part",
        "card:abc123",
        "oem_part_number",
        risk=RiskLevel.HIGH,
        required_source_kinds=(SourceKind.OEM,),
        depends_on=("identity",),
    )
    bundle = EvidenceBundle(
        (
            EvidenceRecord(
                evidence_id="crm_down",
                claim_id="identity",
                source_id="crm_context",
                source_kind=SourceKind.CRM,
                value="unavailable",
                observed_at=NOW,
                disposition="unavailable",
            ),
            EvidenceRecord(
                evidence_id="epc_part",
                claim_id="oem_part",
                source_id="brand_epc",
                source_kind=SourceKind.OEM,
                value="A1678350400",
                observed_at=NOW,
            ),
        )
    )

    result = bundle.reconcile((identity, oem_part), now=NOW).claim("oem_part")

    assert result.status is ResolutionStatus.INSUFFICIENT
    assert result.value is None
    assert any("blocked by unresolved dependency" in issue for issue in result.issues)


def test_safe_display_redacts_common_personal_identifiers_but_not_part_numbers():
    rendered = safe_display_value("A1234567890 WDB12345678901234 test@example.com +7 999 123-45-67")

    assert "A1234567890" in rendered
    assert "[VIN hidden]" in rendered
    assert "[email hidden]" in rendered
    assert "[phone hidden]" in rendered


def test_safe_display_redacts_person_name_contact_and_registration_plate():
    rendered = safe_display_value("Иван Петров А123ВС124 +7 999 123-45-67")

    assert "Иван Петров" not in rendered
    assert "А123ВС124" not in rendered
    assert "+7 999 123-45-67" not in rendered
    assert "[name hidden]" in rendered
    assert "[plate hidden]" in rendered
    assert "[phone hidden]" in rendered


def test_claim_rejects_a_raw_vin_inside_an_opaque_subject_reference():
    with pytest.raises(ValueError, match="non-sensitive"):
        Claim("identity", "vehicle:WDB12345678901234", "vehicle_identity")
