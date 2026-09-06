from __future__ import annotations

import json

from autostop_manager.vin_oem_resolver import resolve_vin_oem_parts


def _medium_identity() -> dict:
    return {
        "confidence": 0.7,
        "confidence_label": "medium",
        "parts_lookup_readiness": {
            "ready_for_oem_lookup": False,
            "ready_for_oem_candidate_lookup": False,
            "ready_for_crm_writeback": False,
            "blocking_reasons": ["identity_confidence_below_high"],
        },
        "vehicle_profile": {"make": "HONDA", "model": "Accord", "model_year": 2003},
        "conflicts": [],
        "warnings": [],
    }


def test_resolver_allows_candidate_lookup_after_partsapi_oe_agreement(monkeypatch):
    monkeypatch.setattr(
        "autostop_manager.vin_oem_resolver.decode_vehicle_identity", lambda *args, **kwargs: _medium_identity()
    )
    calls = []

    def fake_partsapi_catalog_lookup(**kwargs):
        calls.append(kwargs)
        operation = kwargs["operation"]
        base = {
            "ok": True,
            "provider": "partsapi_ru",
            "operation": operation,
            "partsapi_method": operation,
            "dry_run": kwargs.get("dry_run", False),
            "quota_cost_estimate": 1,
            "request_plan": {"configured": True, "params": {}, "redacted_url": "https://api.partsapi.ru?key=***"},
            "attempt_count": 1,
            "max_attempts": 1,
            "attempts": [{"attempt": 1, "ok": True}],
        }
        if operation == "vin_decode_oe":
            return {**base, "vehicle_profiles": [{"make": "HONDA", "model": "Accord"}]}
        if operation == "parts_by_vin":
            return {
                **base,
                "oem_candidates": [
                    {
                        "provider": "partsapi_ru",
                        "part_number": "45022SDAA00",
                        "brand": "HONDA",
                        "name": "Pad set, front",
                        "source_operation": "parts_by_vin",
                        "fitment_evidence": {"is_fit_for_this_vin": True, "group": "Brake"},
                        "confidence": 0.95,
                    }
                ],
            }
        return {**base, "article_candidates": [], "cross_candidates": [], "oem_candidates": []}

    monkeypatch.setattr("autostop_manager.vin_oem_resolver.partsapi_catalog_lookup", fake_partsapi_catalog_lookup)

    result = resolve_vin_oem_parts(
        identifier="1HGCM82633A004352",
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_identity=True,
        live_partsapi_oem=True,
        max_live_calls=2,
        max_candidates=1,
    )

    assert result["readiness"]["ready_for_oem_candidate_lookup"] is True
    assert result["readiness"]["ready_for_crm_writeback"] is False
    assert result["candidate_count"] == 1
    assert result["oem_candidates"][0]["manual_review_required"] is True
    assert result["status"] == "oem_candidates_found_needs_manual_confirmation"
    assert [call["operation"] for call in calls[:2]] == ["vin_decode_oe", "parts_by_vin"]
    assert result["live_call_count"] == 2
    rendered = json.dumps(result, ensure_ascii=False)
    assert "1HGCM82633A004352" not in rendered
    assert "key=***" in rendered


def test_resolver_uses_third_live_call_for_oem_applicability(monkeypatch):
    monkeypatch.setattr(
        "autostop_manager.vin_oem_resolver.decode_vehicle_identity", lambda *args, **kwargs: _medium_identity()
    )
    calls = []

    def fake_partsapi_catalog_lookup(**kwargs):
        calls.append(kwargs)
        operation = kwargs["operation"]
        base = {
            "ok": True,
            "provider": "partsapi_ru",
            "operation": operation,
            "dry_run": kwargs.get("dry_run", False),
            "attempt_count": 0 if kwargs.get("dry_run", False) else 1,
            "request_plan": {"configured": True, "params": {}, "redacted_url": "https://api.partsapi.ru?key=***"},
        }
        if operation == "vin_decode_oe":
            return {**base, "vehicle_profiles": [{"make": "HONDA", "model": "Accord"}]}
        if operation == "parts_by_vin":
            return {
                **base,
                "oem_candidates": [
                    {
                        "provider": "partsapi_ru",
                        "part_number": "45022SDAA00",
                        "brand": "HONDA",
                        "name": "Pad set, front",
                        "source_operation": "parts_by_vin",
                        "fitment_evidence": {"is_fit_for_this_vin": True},
                        "confidence": 0.95,
                    }
                ],
            }
        return {**base, "vehicle_profiles": [], "oem_candidates": [], "article_candidates": [], "cross_candidates": []}

    monkeypatch.setattr("autostop_manager.vin_oem_resolver.partsapi_catalog_lookup", fake_partsapi_catalog_lookup)

    result = resolve_vin_oem_parts(
        identifier="1HGCM82633A004352",
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_identity=True,
        live_partsapi_oem=True,
        max_live_calls=3,
        max_candidates=1,
    )

    assert [call["operation"] for call in calls[:3]] == ["vin_decode_oe", "parts_by_vin", "oe_applicability"]
    assert result["live_call_count"] == 3
    assert result["oem_candidates"][0]["applicability_status"] == "not_found"
    assert "applicability_not_confirmed" in result["oem_candidates"][0]["blocking_reasons"]


def test_resolver_reranks_candidates_using_applicability_evidence(monkeypatch):
    identity = _medium_identity()
    identity["confidence_label"] = "high"
    identity["parts_lookup_readiness"]["ready_for_oem_candidate_lookup"] = True
    monkeypatch.setattr("autostop_manager.vin_oem_resolver.decode_vehicle_identity", lambda *args, **kwargs: identity)

    calls = []

    def fake_partsapi_catalog_lookup(**kwargs):
        calls.append(kwargs)
        operation = kwargs["operation"]
        base = {
            "ok": True,
            "provider": "partsapi_ru",
            "operation": operation,
            "dry_run": kwargs.get("dry_run", False),
            "attempt_count": 0 if kwargs.get("dry_run", False) else 1,
            "request_plan": {"configured": True, "params": {}, "redacted_url": "https://api.partsapi.ru?key=***"},
        }
        if operation == "parts_by_vin":
            return {
                **base,
                "oem_candidates": [
                    {
                        "provider": "partsapi_ru",
                        "part_number": part_number,
                        "brand": "HONDA",
                        "source_operation": "parts_by_vin",
                        "fitment_evidence": {"is_fit_for_this_vin": True},
                        "confidence": 0.95,
                    }
                    for part_number in ("45022SDAA00", "45022SDAA01")
                ],
            }
        if operation == "oe_applicability":
            part_number = kwargs["part_number"]
            return {
                **base,
                "outcome": "success" if part_number.endswith("1") else "empty_result",
                "oem_candidates": (
                    [{"part_number": part_number, "fitment_evidence": {"applicability": "Accord"}}]
                    if part_number.endswith("1")
                    else []
                ),
            }
        return {**base, "vehicle_profiles": [], "oem_candidates": [], "article_candidates": [], "cross_candidates": []}

    monkeypatch.setattr("autostop_manager.vin_oem_resolver.partsapi_catalog_lookup", fake_partsapi_catalog_lookup)

    result = resolve_vin_oem_parts(
        identifier="1HGCM82633A004352",
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_oem=True,
        max_live_calls=3,
        max_candidates=2,
    )

    assert [item["part_number"] for item in result["oem_candidates"]] == ["45022SDAA01", "45022SDAA00"]
    assert result["oem_candidates"][0]["applicability_status"] == "catalog_evidence_found"
    assert result["oem_candidates"][1]["applicability_status"] == "not_found"
    assert [call["operation"] for call in calls if not call.get("dry_run")][:3] == [
        "parts_by_vin",
        "oe_applicability",
        "oe_applicability",
    ]


def test_resolver_does_not_spend_live_budget_on_missing_credentials(monkeypatch):
    identity = _medium_identity()
    identity["confidence_label"] = "high"
    identity["parts_lookup_readiness"]["ready_for_oem_candidate_lookup"] = True
    monkeypatch.setattr("autostop_manager.vin_oem_resolver.decode_vehicle_identity", lambda *args, **kwargs: identity)
    calls = []

    def fake_partsapi_catalog_lookup(**kwargs):
        calls.append(kwargs)
        operation = kwargs["operation"]
        if operation == "vin_decode_oe":
            return {
                "ok": False,
                "provider": "partsapi_ru",
                "operation": operation,
                "dry_run": False,
                "attempt_count": 0,
                "outcome": "credentials_missing",
                "request_plan": {"configured": False, "params": {}, "redacted_url": None},
            }
        if operation == "parts_by_vin":
            return {
                "ok": True,
                "provider": "partsapi_ru",
                "operation": operation,
                "dry_run": kwargs.get("dry_run", False),
                "attempt_count": 0 if kwargs.get("dry_run", False) else 1,
                "request_plan": {"configured": True, "params": {}, "redacted_url": "https://api.partsapi.ru?key=***"},
                "oem_candidates": [],
            }
        return {
            "ok": True,
            "provider": "partsapi_ru",
            "operation": operation,
            "dry_run": True,
            "attempt_count": 0,
            "request_plan": {"configured": True, "params": {}, "redacted_url": "https://api.partsapi.ru?key=***"},
        }

    monkeypatch.setattr("autostop_manager.vin_oem_resolver.partsapi_catalog_lookup", fake_partsapi_catalog_lookup)

    result = resolve_vin_oem_parts(
        identifier="1HGCM82633A004352",
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_identity=True,
        live_partsapi_oem=True,
        max_live_calls=1,
    )

    by_vin_call = next(call for call in calls if call["operation"] == "parts_by_vin")
    assert by_vin_call["dry_run"] is False
    assert result["live_call_count"] == 1


def test_resolver_blocks_generic_part_before_live_catalog_search(monkeypatch):
    identity = _medium_identity()
    identity["confidence_label"] = "high"
    identity["parts_lookup_readiness"]["ready_for_oem_candidate_lookup"] = True
    monkeypatch.setattr("autostop_manager.vin_oem_resolver.decode_vehicle_identity", lambda *args, **kwargs: identity)
    operations = []

    def fake_partsapi_catalog_lookup(**kwargs):
        operations.append(kwargs["operation"])
        return {
            "ok": True,
            "provider": "partsapi_ru",
            "operation": kwargs["operation"],
            "dry_run": kwargs.get("dry_run", False),
            "request_plan": {"configured": True, "params": {}, "redacted_url": "https://api.partsapi.ru?key=***"},
            "vehicle_profiles": [{"make": "HONDA"}] if kwargs["operation"] == "vin_decode_oe" else [],
        }

    monkeypatch.setattr("autostop_manager.vin_oem_resolver.partsapi_catalog_lookup", fake_partsapi_catalog_lookup)

    result = resolve_vin_oem_parts(
        identifier="1HGCM82633A004352",
        requested_part="тормозные колодки",
        live_vpic=False,
        live_partsapi_identity=False,
        live_partsapi_oem=True,
    )

    assert result["status"] == "needs_part_clarification"
    assert result["readiness"]["ready_for_category_lookup"] is False
    assert "parts_by_vin" not in operations
    clarification = next(action for action in result["manual_actions"] if action["code"] == "clarify_part_position")
    assert clarification["fields"] == ["axle"]
    assert "message" not in clarification


def test_resolver_blocks_live_oem_when_category_is_unresolved(monkeypatch):
    identity = _medium_identity()
    identity["confidence_label"] = "high"
    identity["parts_lookup_readiness"]["ready_for_oem_candidate_lookup"] = True
    monkeypatch.setattr("autostop_manager.vin_oem_resolver.decode_vehicle_identity", lambda *args, **kwargs: identity)
    calls = []

    def fake_partsapi_catalog_lookup(**kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "provider": "partsapi_ru",
            "operation": kwargs["operation"],
            "dry_run": kwargs.get("dry_run", False),
            "request_plan": {"configured": True, "params": {}, "redacted_url": "https://api.partsapi.ru?key=***"},
            "vehicle_profiles": [{"make": "HONDA"}] if kwargs["operation"] == "vin_decode_oe" else [],
        }

    monkeypatch.setattr("autostop_manager.vin_oem_resolver.partsapi_catalog_lookup", fake_partsapi_catalog_lookup)

    result = resolve_vin_oem_parts(
        identifier="1HGCM82633A004352",
        requested_part="масляный фильтр",
        live_vpic=False,
        live_partsapi_oem=True,
    )

    assert result["status"] == "needs_partsapi_category_mapping"
    assert result["readiness"]["needs_partsapi_category_mapping"] is True
    assert all(call["operation"] != "parts_by_vin" for call in calls)


def test_resolver_blocks_high_identity_conflict(monkeypatch):
    identity = _medium_identity()
    identity["confidence_label"] = "high"
    identity["parts_lookup_readiness"]["ready_for_oem_candidate_lookup"] = True
    identity["conflicts"] = [{"field": "make", "severity": "high"}]
    monkeypatch.setattr("autostop_manager.vin_oem_resolver.decode_vehicle_identity", lambda *args, **kwargs: identity)
    monkeypatch.setattr(
        "autostop_manager.vin_oem_resolver.partsapi_catalog_lookup",
        lambda **kwargs: {
            "ok": True,
            "provider": "partsapi_ru",
            "operation": kwargs["operation"],
            "dry_run": True,
            "request_plan": {"configured": True, "params": {}, "redacted_url": "https://api.partsapi.ru?key=***"},
        },
    )

    result = resolve_vin_oem_parts(
        identifier="1HGCM82633A004352",
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_oem=True,
    )

    assert result["status"] == "needs_identity_confirmation"
    assert result["readiness"]["ready_for_identity_crosscheck"] is False
