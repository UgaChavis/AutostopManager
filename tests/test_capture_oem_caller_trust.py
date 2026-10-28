"""Pure capture can classify supplied evidence, but cannot authenticate it."""

from __future__ import annotations

from copy import deepcopy

import pytest

from autostop_manager.automotive_contracts import binding
from autostop_manager.automotive_parts import capture_oem_evidence


_IDENTIFIER = "WBA" + "0" * 14
_MODIFICATION = {
    "provider": "partsapi_ru",
    "namespace": "tecdoc",
    "entity_kind": "modification",
    "id": "42",
    "carType": "PC",
}


def _source(scope: str) -> tuple[dict, dict | None]:
    bound = binding(_IDENTIFIER) if scope == "exact_identifier" else _MODIFICATION if scope == "modification" else None
    source = {
        "provider": "DEMO",
        "primary_lineage": "DEMO manufacturer",
        "method": "epc",
        "locator": "https://example.invalid/oem",
        "fetched_at": "2026-01-01T00:00:00Z",
        "document_kind": "official_epc",
        "declares_oem": True,
        "part_number": "DEMO-1",
        "brand": "DEMO",
        "scope": scope,
        "source_verified": True,
        "independent_source_verified": True,
        "identifier_verified": True,
        "ready_for_crm_writeback": True,
    }
    if scope == "exact_identifier":
        source["identifier_binding"] = bound
    elif scope == "modification":
        source["catalog_ref"] = bound
    return source, bound


@pytest.mark.parametrize("scope", ["family", "modification", "exact_identifier"])
def test_caller_document_metadata_can_only_establish_conditional_oem_candidate(scope):
    source, bound = _source(scope)
    original = deepcopy(source)
    result = capture_oem_evidence("DEMO-1", source, scope, bound, "DEMO")
    candidate = result["data"]["candidate"]
    assert result["outcome"] == "success"  # The structural assessment completed.
    assert candidate["kind"] == "oem_candidate"
    assert candidate["scope"] == scope
    assert candidate["vehicle_binding"] == bound
    assert candidate["caller_evidence_sufficient"] is True
    assert candidate["oem_confirmed"] is False
    assert candidate["fitment_confirmed"] is False
    assert candidate["exact_applicability_confirmed"] is False
    assert candidate["independent_source_verified"] is False
    assert candidate["conditional_on_caller_evidence"] is True
    assert candidate["ready_for_crm_writeback"] is False
    assert result["evidence"][0]["independent_source_verified"] is False
    assert result["evidence"][0]["source_verified"] is False
    assert result["evidence"][0]["identifier_verified"] is False
    assert result["evidence"][0]["reported_identifier_verified"] is True
    assert result["evidence"][0]["ready_for_crm_writeback"] is False
    assert result["warnings"]
    assert result["execution"]["network_calls"] == 0
    assert source == original


@pytest.mark.parametrize("change", [{"part_number": "OTHER"}, {"brand": "OTHER"}, {"document_kind": "article"}])
def test_missing_structural_evidence_remains_unknown(change):
    source, bound = _source("exact_identifier")
    result = capture_oem_evidence("DEMO-1", {**source, **change}, "exact_identifier", bound, "DEMO")
    candidate = result["data"]["candidate"]
    assert result["outcome"] == "partial"
    assert candidate["kind"] == "unknown"
    assert candidate["caller_evidence_sufficient"] is False
    assert candidate["oem_confirmed"] is False
    assert candidate["fitment_confirmed"] is False
    assert candidate["independent_source_verified"] is False
    assert result["missing_fields"]


def test_wrong_binding_remains_structurally_unverified():
    source, bound = _source("exact_identifier")
    source["identifier_binding"] = binding("WBA" + "1" * 14)
    result = capture_oem_evidence("DEMO-1", source, "exact_identifier", bound, "DEMO")
    assert result["data"]["candidate"]["caller_evidence_sufficient"] is False
    assert result["data"]["candidate"]["oem_confirmed"] is False
    assert "exact_vehicle_binding" in result["missing_fields"]
