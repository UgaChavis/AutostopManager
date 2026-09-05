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
