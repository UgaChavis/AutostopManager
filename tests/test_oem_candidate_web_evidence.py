from __future__ import annotations

import json
import autostop_manager.oem_candidate_web_evidence as evidence
import autostop_manager.web_research_gateway as web_gateway


_VIN_LIKE = "WBA00000000000000"
_VIN_LIKE_WITH_SEPARATORS = "WBA/000000/00000000"
_VIN_LIKE_WITH_MIXED_SEPARATORS = "WBA 000000/00000000"


def _candidate(**overrides):
    return {
        "part_number": "4H0 615 301",
        "brand": "AUDI",
        "name": "Brake pad set",
        "fitment_scope": "vin_specific",
        "position_match": "matched",
        "confidence_label": "high",
        "manual_review_required": True,
        **overrides,
    }


def test_default_mode_builds_safe_profile_query_without_network(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("default mode must not search the public web")

    monkeypatch.setattr(evidence, "research_part_public_evidence", forbidden)

    result = evidence.verify_oem_candidates_web(
        candidates=[_candidate()],
        requested_part=f"передние колодки {_VIN_LIKE}",
        make="Audi",
        model=f"A8 {_VIN_LIKE}",
        model_year=2016,
        engine="3.0 TDI",
        axle="front",
        side="left",
        position="inner",
    )

    rendered = json.dumps(result, ensure_ascii=False)
    item = result["candidate_evidence"][0]
    assert calls == []
    assert result["ok"] is True
    assert result["status"] == "prepared_no_network"
    assert item["status"] == "кандидат"
    assert item["evidence_status"] == "not_checked"
    assert item["oem_number"] == "4H0 615 301"
    assert item["sources"] == []
    assert item["applicability_conditions"] == {
        "axle": "FRONT",
        "side": "LEFT",
        "position": "INNER",
        "catalog_fitment_scope": "vin_specific",
        "catalog_position_match": "matched",
        "catalog_applicability_status": None,
        "catalog_provenance_available": False,
    }
    assert {"2016", "FRONT", "LEFT", "INNER"} <= set(item["query"].split())
    assert "3.0 TDI" in item["query"]
    assert _VIN_LIKE not in rendered
    assert result["privacy"]["full_vin_in_search_queries"] is False
    assert all(route["requires_license"] is False for route in result["source_routes"])
    assert all(
        not any(
            token in route["access"].casefold() for token in ("login", "registration", "subscription", "paid", "mixed")
        )
        for route in result["source_routes"]
    )
    assert not any(route["source_id"] == "audi_erwin_na" for route in result["source_routes"])


def test_live_search_uses_gateway_with_vin_free_query_and_returns_only_weak_public_candidate_reference(monkeypatch):
    captured = []

    def fake_gateway(*, query, **kwargs):
        captured.append((query, kwargs))
        return {
            "ok": True,
            "schema": "WebResearchGatewayV1",
            "capability": "research_part_public_evidence",
            "adapter": "e8_capability_adapter",
            "provider_order": ["searxng", "duckduckgo"],
            "fallback_used": True,
            "results": [
                {
                    "source": "NHTSA",
                    "url": "//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.nhtsa.gov%2Frecalls%2F4H0615301",
                    "title": "AUDI 4H0 615 301",
                    "snippet": "Reference for brake pad set.",
                }
            ],
        }

    monkeypatch.setattr(evidence, "research_part_public_evidence", fake_gateway)

    result = evidence.verify_oem_candidates_web(
        candidates=[_candidate()],
        requested_part=f"передние колодки {_VIN_LIKE}",
        make="Audi",
        model=f"A8 {_VIN_LIKE}",
        model_year=2016,
        engine="3.0 TDI",
        axle="front",
        side="left",
        position="inner",
        live_search=True,
    )

    item = result["candidate_evidence"][0]
    assert captured and _VIN_LIKE not in captured[0][0]
    assert {"2016", "FRONT", "LEFT", "INNER"} <= set(captured[0][0].split())
    assert "3.0 TDI" in captured[0][0]
    assert captured[0][1]["max_pages"] == 1
    assert item["research_gateway"] == {
        "schema": "WebResearchGatewayV1",
        "capability": "research_part_public_evidence",
        "adapter": "e8_capability_adapter",
        "provider_order": ["searxng", "duckduckgo"],
        "fallback_used": True,
    }
    assert item["status"] == "кандидат"
    assert item["evidence_status"] == "public_reference_found"
    assert item["fitment_confirmed"] is False
    assert item["sources"] == [
        {
            "source": "NHTSA Datasets and APIs",
            "source_id": "nhtsa_datasets_apis",
            "source_type": "open_government_data",
            "url": "https://www.nhtsa.gov/recalls/4H0615301",
        }
    ]
    assert item["evidence"][0]["part_number_reference_found"] is True
    assert item["next_manual_step"]["code"] == "confirm_in_vin_specific_epc"
    assert result["status"] == "public_reference_found_needs_epc_confirmation"
    assert any(action["code"] == "confirm_in_vin_specific_epc" for action in result["manual_actions"])


def test_unallowlisted_search_result_is_not_used_as_evidence(monkeypatch):
    monkeypatch.setattr(
        evidence,
        "research_part_public_evidence",
        lambda **_kwargs: {
            "ok": True,
            "results": [
                {
                    "source": "unknown catalog",
                    "url": "https://unknown-catalog.example/4H0615301",
                    "title": "4H0 615 301",
                    "snippet": "Unverified result",
                }
            ],
        },
    )

    result = evidence.verify_oem_candidates_web(candidates=[_candidate()], live_search=True)

    item = result["candidate_evidence"][0]
    assert item["status"] == "не подтверждён"
    assert item["evidence_status"] == "public_reference_not_found"
    assert item["sources"] == []
    assert item["evidence"] == []


def test_claimed_catalog_state_without_link_stays_candidate(monkeypatch):
    monkeypatch.setattr(
        evidence,
        "research_part_public_evidence",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("default mode must not search")),
    )

    result = evidence.verify_oem_candidates_web(
        candidates=[_candidate(applicability_status="catalog_evidence_found")],
        make="Audi",
    )

    item = result["candidate_evidence"][0]
    assert item["status"] == "кандидат"
    assert item["fitment_confirmed"] is False
    assert item["evidence_status"] == "not_checked"
    assert "доверенной EPC-проверки" in item["status_reason"]
    assert item["next_manual_step"]["code"] == "run_optional_public_search_or_epc"


def test_linked_vin_specific_catalog_evidence_stays_candidate_until_trusted_epc_check():
    result = evidence.verify_oem_candidates_web(
        candidates=[
            _candidate(
                applicability_status="catalog_evidence_found",
                catalog_evidence=[
                    {
                        "source": "OEM EPC",
                        "url": "https://epc.example.test/record/123",
                        "evidence_scope": "vin_specific_catalog",
                    }
                ],
            )
        ],
        make="Audi",
    )

    item = result["candidate_evidence"][0]
    assert item["status"] == "кандидат"
    assert item["fitment_confirmed"] is False
    assert item["sources"] == [
        {
            "source": "OEM EPC",
            "url": "https://epc.example.test/record/123",
            "evidence_scope": "vin_specific_catalog",
        }
    ]
    assert item["next_manual_step"]["code"] == "run_optional_public_search_or_epc"


def test_vin_like_part_number_is_rejected_before_search(monkeypatch):
    monkeypatch.setattr(
        evidence,
        "research_part_public_evidence",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("VIN must never be searched")),
    )

    result = evidence.verify_oem_candidates_web(candidates=[_candidate(part_number=_VIN_LIKE)], live_search=True)

    assert result["ok"] is False
    assert result["status"] == "needs_valid_oem_candidate"
    assert result["conflicts"] == [{"candidate_index": 1, "code": "vin_like_part_number_rejected"}]
    assert _VIN_LIKE not in json.dumps(result, ensure_ascii=False)


def test_separator_formatted_vin_is_removed_before_query():
    result = evidence.verify_oem_candidates_web(
        candidates=[_candidate()],
        requested_part=f"brake pad {_VIN_LIKE_WITH_SEPARATORS}",
        model=f"A8 {_VIN_LIKE_WITH_SEPARATORS}",
    )

    rendered = json.dumps(result, ensure_ascii=False)
    assert _VIN_LIKE_WITH_SEPARATORS not in rendered
    assert _VIN_LIKE not in rendered


def test_mixed_separator_vin_is_removed_from_e7_query_and_search_url():
    result = evidence.verify_oem_candidates_web(
        candidates=[_candidate()],
        requested_part=f"brake pad {_VIN_LIKE_WITH_MIXED_SEPARATORS}",
        make=_VIN_LIKE_WITH_MIXED_SEPARATORS,
    )

    rendered = json.dumps(result, ensure_ascii=False)
    assert _VIN_LIKE_WITH_MIXED_SEPARATORS not in rendered
    assert _VIN_LIKE not in rendered
    assert result["privacy"]["full_vin_in_search_queries"] is False


def test_existing_fitment_conflict_is_retained_as_not_confirmed(monkeypatch):
    monkeypatch.setattr(
        evidence,
        "research_part_public_evidence",
        lambda **_kwargs: {"ok": False, "error": {"code": "web_search_provider_failed", "retryable": True}},
    )

    result = evidence.verify_oem_candidates_web(
        candidates=[_candidate(position_match="conflict", fitment_scope="not_vin_specific", confidence_label="low")],
        live_search=True,
    )

    item = result["candidate_evidence"][0]
    assert item["status"] == "не подтверждён"
    assert item["evidence_status"] == "search_failed"
    candidate_codes = {conflict["code"] for conflict in item["contradictions"]}
    assert {"candidate_position_conflict", "fitment_not_vin_specific", "low_catalog_confidence"} <= candidate_codes
    codes = {conflict["code"] for conflict in result["conflicts"]}
    assert candidate_codes <= codes
    assert item["next_manual_step"]["code"] == "resolve_candidate_conflicts"
    assert any(action["code"] == "retry_or_use_registry_route" for action in result["manual_actions"])
    assert any(action["code"] == "resolve_candidate_conflicts" for action in result["manual_actions"])


def test_e8_authorized_part_source_is_accepted_after_gateway_domain_policy():
    def invoke(_name, _arguments):
        return {
            "ok": True,
            "results": [
                {
                    "title": "Ford 1712024 brake pad set",
                    "url": "https://partsouq.com/en/catalog/genuine/parts?number=1712024",
                    "snippet": "1712024 front brake pad set",
                    "domain": "partsouq.com",
                    "provider": "searxng",
                    "source_id": "partsouq_catalog",
                    "source_type": "oem_catalog",
                    "source_authorized": True,
                }
            ],
        }

    web_gateway.install_web_research_gateway(web_gateway.CapabilityWebResearchGatewayAdapter(invoke))
    try:
        result = evidence.verify_oem_candidates_web(
            candidates=[_candidate(part_number="1712024", brand="FORD")],
            requested_part="передние колодки",
            live_search=True,
        )
    finally:
        web_gateway.install_web_research_gateway(None)

    item = result["candidate_evidence"][0]
    assert item["evidence_status"] == "public_reference_found"
    assert item["sources"] == [
        {
            "source": "partsouq.com",
            "source_id": "partsouq_catalog",
            "source_type": "oem_catalog",
            "url": "https://partsouq.com/en/catalog/genuine/parts?number=1712024",
        }
    ]


def test_e8_authorized_source_rejects_unregistered_declared_domain(monkeypatch):
    monkeypatch.setattr(
        evidence,
        "research_part_public_evidence",
        lambda **_kwargs: {
            "ok": True,
            "results": [
                {
                    "title": "Ford 1712024 brake pad set",
                    "url": "https://unregistered.invalid/catalog/1712024",
                    "snippet": "1712024 front brake pad set",
                    "domain": "unregistered.invalid",
                    "source_id": "partsouq_catalog",
                    "source_type": "oem_catalog",
                    "source_authorized": True,
                }
            ],
        },
    )

    result = evidence.verify_oem_candidates_web(
        candidates=[_candidate(part_number="1712024", brand="FORD")],
        requested_part="передние колодки",
        live_search=True,
    )

    item = result["candidate_evidence"][0]
    assert item["evidence_status"] == "public_reference_not_found"
    assert item["sources"] == []


def test_installed_gateway_exception_becomes_e7_search_failed_without_leak():
    class FailingGateway:
        def research_part_public_evidence(self, **_kwargs):
            raise KeyError("private upstream details")

    web_gateway.install_web_research_gateway(FailingGateway())
    try:
        result = evidence.verify_oem_candidates_web(
            candidates=[_candidate(part_number="1712024", brand="FORD")],
            requested_part="передние колодки",
            live_search=True,
        )
    finally:
        web_gateway.install_web_research_gateway(None)

    item = result["candidate_evidence"][0]
    assert item["evidence_status"] == "search_failed"
    assert result["status"] == "public_search_provider_failed"
    assert "private upstream details" not in json.dumps(result, ensure_ascii=False)
