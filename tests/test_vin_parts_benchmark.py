from __future__ import annotations

import json

from autostop_manager.vin_parts_benchmark import (
    _assess_partsapi_identity_agreement,
    benchmark_vin_parts_lookup,
)


SYNTHETIC_VIN = "A" * 17


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
        "vehicle_profile": {"make": "HONDA", "model": "Accord"},
        "diagnostics": {},
        "warnings": [],
        "conflicts": [],
        "required_next_sources": [],
        "evidence_sources": [],
    }


def _install_batch(monkeypatch, identity: dict | None = None):
    monkeypatch.setattr(
        "autostop_manager.vin_parts_benchmark.decode_vehicle_identities",
        lambda *_a, **_k: {
            "ok": True,
            "count": 1,
            "high_confidence_count": 0,
            "medium_confidence_count": 1,
            "low_confidence_count": 0,
            "identity_coverage": {},
            "vpic_batch": {},
            "results": [identity or _medium_identity()],
        },
    )


def _fake_lookup(calls: list[dict], *, profiles: list[dict] | None = None):
    def lookup(**kwargs):
        calls.append(kwargs)
        operation = kwargs["operation"]
        return {
            "ok": True,
            "provider": "partsapi_ru",
            "operation": operation,
            "partsapi_method": {"vin_decode": "VINdecode", "search_tree": "getSearchTree"}[operation],
            "dry_run": kwargs.get("dry_run", False),
            "attempt_count": 0 if kwargs.get("dry_run") else 1,
            "request_plan": {
                "configured": True,
                "params": {"vin": "AAA***AAA"} if operation == "vin_decode" else {},
                "redacted_url": "https://api.partsapi.ru?key=***",
            },
            "vehicle_profiles": profiles or [] if operation == "vin_decode" and not kwargs.get("dry_run") else [],
        }

    return lookup


def test_benchmark_prepares_only_available_vin_decode_until_car_id_is_known(monkeypatch):
    _install_batch(monkeypatch)
    calls = []
    monkeypatch.setattr("autostop_manager.vin_parts_benchmark.partsapi_catalog_lookup", _fake_lookup(calls))
    result = benchmark_vin_parts_lookup(
        [{"identifier": SYNTHETIC_VIN, "requested_part": "передние колодки"}],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
    )
    assert [call["operation"] for call in calls] == ["vin_decode"]
    assert calls[0]["dry_run"] is True
    assert result["items"][0]["prepared_calls"]["partsapi"][0]["request_param_names"] == ["vin"]
    assert result["summary"]["tecdoc_article_candidate_count"] == 0
    assert SYNTHETIC_VIN not in json.dumps(result, ensure_ascii=False)


def test_benchmark_uses_vin_decode_agreement_for_tecdoc_read_only_gate(monkeypatch):
    _install_batch(monkeypatch)
    calls = []
    monkeypatch.setattr(
        "autostop_manager.vin_parts_benchmark.partsapi_catalog_lookup",
        _fake_lookup(
            calls,
            profiles=[
                {
                    "make": "HONDA",
                    "model": "Accord",
                    "tecdoc_car_id": "9877",
                    "vehicle_type": "PC",
                    "identifier_matches_request": True,
                }
            ],
        ),
    )
    result = benchmark_vin_parts_lookup(
        [{"identifier": SYNTHETIC_VIN, "requested_part": "передние колодки"}],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
        live_partsapi_identity=True,
    )
    assert [call["operation"] for call in calls] == ["vin_decode", "search_tree"]
    assert calls[1]["dry_run"] is True
    assert calls[1]["type_id"] == "9877"
    identity = result["items"][0]["identity"]
    assert identity["ready_for_tecdoc_candidate_lookup"] is True
    assert identity["ready_for_oem_candidate_lookup"] is False
    assert identity["ready_for_crm_writeback"] is False
    assert result["summary"]["ready_for_tecdoc_candidate_lookup_count"] == 1
    assert SYNTHETIC_VIN not in json.dumps(result, ensure_ascii=False)


def test_benchmark_blocks_vin_decode_identifier_mismatch(monkeypatch):
    _install_batch(monkeypatch)
    calls = []
    monkeypatch.setattr(
        "autostop_manager.vin_parts_benchmark.partsapi_catalog_lookup",
        _fake_lookup(calls, profiles=[{"make": "HONDA", "tecdoc_car_id": "9877", "identifier_matches_request": False}]),
    )
    result = benchmark_vin_parts_lookup(
        [{"identifier": SYNTHETIC_VIN}],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
        live_partsapi_identity=True,
    )
    assert [call["operation"] for call in calls] == ["vin_decode"]
    identity = result["items"][0]["identity"]
    assert identity["ready_for_tecdoc_candidate_lookup"] is False
    assert identity["cross_source_agreement"]["status"] == "identifier_mismatch"


def test_benchmark_does_not_send_frame_to_vin_decode(monkeypatch):
    _install_batch(monkeypatch)
    calls = []
    monkeypatch.setattr("autostop_manager.vin_parts_benchmark.partsapi_catalog_lookup", _fake_lookup(calls))
    result = benchmark_vin_parts_lookup(
        [{"identifier": "ABC-123456"}],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
        live_partsapi_identity=True,
    )
    assert calls == []
    assert result["items"][0]["prepared_calls"]["partsapi"] == []


def test_benchmark_limits_live_identity_calls_across_batch(monkeypatch):
    second_vin = "B" * 17
    monkeypatch.setattr(
        "autostop_manager.vin_parts_benchmark.decode_vehicle_identities",
        lambda *_a, **_k: {
            "ok": True,
            "count": 2,
            "identity_coverage": {},
            "vpic_batch": {},
            "results": [_medium_identity(), _medium_identity()],
        },
    )
    calls = []
    monkeypatch.setattr(
        "autostop_manager.vin_parts_benchmark.partsapi_catalog_lookup",
        _fake_lookup(
            calls, profiles=[{"make": "HONDA", "model": "Accord", "tecdoc_car_id": "9877", "vehicle_type": "PC"}]
        ),
    )
    result = benchmark_vin_parts_lookup(
        [{"identifier": SYNTHETIC_VIN}, {"identifier": second_vin}],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
        live_partsapi_identity=True,
        max_live_calls=1,
    )
    live_vin_calls = [call for call in calls if call["operation"] == "vin_decode"]
    assert [call["dry_run"] for call in live_vin_calls] == [False, True]
    assert result["summary"]["partsapi_live_call_count"] == 1
    assert result["items"][1]["identity"]["ready_for_tecdoc_candidate_lookup"] is False
    assert SYNTHETIC_VIN not in json.dumps(result, ensure_ascii=False)
    assert second_vin not in json.dumps(result, ensure_ascii=False)


def test_benchmark_attaches_article_resolution_without_claiming_oem(monkeypatch):
    _install_batch(monkeypatch)
    monkeypatch.setattr("autostop_manager.vin_parts_benchmark.partsapi_catalog_lookup", _fake_lookup([]))
    forwarded = []

    def fake_resolver(**kwargs):
        forwarded.append(kwargs)
        return {
            "schema": "VinOemResolution",
            "status": "tecdoc_articles_found_needs_manual_fitment",
            "identity": {
                "confidence_label": "medium",
                "ready_for_oem_lookup": False,
                "ready_for_oem_candidate_lookup": False,
                "ready_for_tecdoc_candidate_lookup": True,
                "ready_for_crm_writeback": False,
                "vehicle_profile": {"make": "HONDA"},
            },
            "candidate_count": 0,
            "article_candidate_count": 2,
            "oem_candidates": [],
            "calls": [],
            "manual_actions": [],
            "live_call_count": 0,
        }

    monkeypatch.setattr(
        "autostop_manager.vin_parts_benchmark.resolve_vin_oem_parts",
        fake_resolver,
    )
    result = benchmark_vin_parts_lookup(
        [{"identifier": SYNTHETIC_VIN, "vehicle_type": "CV"}],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
        resolve_oem=True,
    )
    assert result["summary"]["oem_candidate_count"] == 0
    assert result["summary"]["tecdoc_article_candidate_count"] == 2
    assert result["summary"]["ready_for_tecdoc_candidate_lookup_count"] == 1
    assert forwarded[0]["vehicle_type"] == "CV"


def test_benchmark_redacts_identifier_from_public_search(monkeypatch):
    _install_batch(monkeypatch)
    monkeypatch.setattr("autostop_manager.vin_parts_benchmark.partsapi_catalog_lookup", _fake_lookup([]))
    monkeypatch.setattr(
        "autostop_manager.vin_parts_benchmark.build_oem_parts_provider_plan",
        lambda **kwargs: {
            "live_capability": {},
            "manual_public_search_queries": [
                {
                    "source_id": "example",
                    "role": "manual",
                    "query": f"parts {kwargs['identifier']}",
                    "url": f"https://example.test/search?q={kwargs['identifier']}",
                    "needs": "fitment",
                }
            ],
            "blockers": [],
        },
    )
    result = benchmark_vin_parts_lookup(
        [{"identifier": SYNTHETIC_VIN}],
        requested_part=f"передние колодки {SYNTHETIC_VIN}",
        live_vpic=False,
        use_vpic_batch=False,
    )
    assert result["summary"]["manual_public_queries_with_raw_identifier_count"] == 1
    assert SYNTHETIC_VIN not in json.dumps(result, ensure_ascii=False)
    assert "PARTSAPI_KEY" not in result["next_requirements"][1]["env_names"]


def test_benchmark_identity_agreement_rejects_different_model():
    agreement = _assess_partsapi_identity_agreement(
        {"vehicle_profile": {"make": "VW", "model": "3"}},
        {"ok": True, "vehicle_profiles": [{"make": "VOLKSWAGEN", "model": "320"}]},
    )
    assert agreement["status"] == "conflict"
    assert agreement["matched_fields"] == ["make"]
    assert agreement["conflicting_fields"] == [{"field": "model", "identity_value": "3", "partsapi_value": "320"}]
