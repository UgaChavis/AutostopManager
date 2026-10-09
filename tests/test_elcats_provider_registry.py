from __future__ import annotations

import pytest

from autostop_manager.catalog_adapters import PROVIDERS, _apply_elcats_provider_status
from autostop_manager.oem_candidate_web_evidence import _gateway_authorized_source


def _status(*, enabled: bool, readiness: str, parts_verified: bool) -> dict:
    return {
        "enabled": enabled,
        "registry_version": "fixture-v1",
        "entries": [
            {
                "id": "elcats_fixture",
                "brand": "DEMO",
                "provider": "elcats_catalog",
                "readiness": readiness,
                "reason": readiness,
                "capabilities": ["models", "groups", "diagrams", "parts"],
                "verification": {
                    "public_navigation_verified": True,
                    "parts_verified": parts_verified,
                    "number_extraction_verified": parts_verified,
                },
            }
        ],
    }


@pytest.mark.parametrize("enabled", [False, True])
def test_robots_denied_matrix_never_becomes_callable_by_enabling_flag(enabled):
    row = {"source_id": "elcats_catalog", "capabilities": ["resolve_vehicle", "list_parts"]}
    _apply_elcats_provider_status(row, _status(enabled=enabled, readiness="robots_disallowed", parts_verified=True))
    assert row["configured"] is enabled
    assert row["live_callable_now"] is False
    assert row["live_operations"] == []
    assert row["blocked_reasons"] == ["robots_disallowed"]
    assert row["catalog_entries"][0]["readiness"] == "robots_disallowed"


@pytest.mark.parametrize("enabled,verified", [(False, True), (True, False)])
def test_source_status_requires_flag_and_verified_parts_for_ready_route(enabled, verified):
    row = {"source_id": "elcats_catalog", "capabilities": ["list_parts"]}
    _apply_elcats_provider_status(row, _status(enabled=enabled, readiness="working", parts_verified=verified))
    assert row["live_callable_now"] is False
    assert row["readiness_basis"] == "local_registry_and_feature_flag; no network probe"


def test_callable_is_not_a_live_health_probe_or_fitment_assertion():
    row = {"source_id": "elcats_catalog", "capabilities": ["list_parts"]}
    _apply_elcats_provider_status(row, _status(enabled=True, readiness="working", parts_verified=True))
    assert row["live_callable_now"] is True
    assert row["activation_status"] == "unverified"
    assert row["live_operations"] == ["list_parts"]
    assert "fitment_confirmed" not in row


def test_rendered_numbers_preserve_partial_parts_and_available_navigation():
    row = {
        "source_id": "elcats_catalog",
        "capabilities": ["resolve_vehicle", "list_groups", "list_diagrams", "list_parts", "lookup_candidates"],
    }
    status = _status(enabled=True, readiness="partial_rendered_numbers", parts_verified=True)
    status["entries"][0]["verification"]["number_extraction_verified"] = False
    _apply_elcats_provider_status(row, status)
    assert row["live_callable_now"] is True
    assert row["oem_number_extraction_available"] is False
    assert row["partial_operations"] == ["list_parts", "lookup_candidates"]
    assert row["activation_status"] == "unverified"


def test_working_ocr_extraction_is_separate_from_oem_and_fitment_verification():
    row = {"source_id": "elcats_catalog", "capabilities": ["list_parts", "lookup_candidates"]}
    status = _status(enabled=True, readiness="working", parts_verified=True)
    status["entries"][0]["ocr_required"] = True
    status["entries"][0]["verification"].update(
        {"identifier_verified": False, "oem_verified": False, "fitment_verified": False}
    )
    _apply_elcats_provider_status(row, status)
    assert row["live_callable_now"] and row["candidate_number_extraction_available"]
    assert row["candidate_evidence_only"] and row["ocr_required"]
    assert row["identifier_verified_route_count"] == 0
    assert row["oem_verified_route_count"] == 0
    assert row["fitment_verified_route_count"] == 0
    assert row["partial_operations"] == []


def test_public_catalog_providers_have_distinct_canonical_ids_and_default_flag():
    selected = [
        provider
        for provider in PROVIDERS
        if provider.source_id in {"elcats_catalog", "japancats_catalog", "exist_ssangyong_catalog"}
    ]
    assert len(selected) == 3
    assert all(provider.env_names == ("AUTOSTOP_ELCATS_ENABLED",) for provider in selected)
    assert len({provider.docs_url for provider in selected}) == 3
    assert all(provider.access_mode == "public_site_read_only" and not provider.manual_allowed for provider in selected)


@pytest.mark.parametrize(
    "source_id,domain",
    [
        ("elcats_catalog", "elcats.ru"),
        ("japancats_catalog", "japancats.ru"),
        ("exist_ssangyong_catalog", "ssangyong.exist.ru"),
    ],
)
def test_e8_source_lineage_agrees_with_domain_and_rejects_relabeling(source_id, domain):
    url = f"https://{domain}/catalog/DEMO"
    row = {"source_id": source_id, "source_type": "oem_catalog", "source_authorized": True, "domain": domain}
    assert _gateway_authorized_source(row, url) is not None
    row["domain"] = "untrusted.example"
    assert _gateway_authorized_source(row, url) is None
