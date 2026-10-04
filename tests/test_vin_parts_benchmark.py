from __future__ import annotations

import json

import pytest

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
        lambda items, **_k: {
            "ok": True,
            "count": len(items),
            "high_confidence_count": 0,
            "medium_confidence_count": len(items),
            "low_confidence_count": 0,
            "identity_coverage": {},
            "vpic_batch": {},
            "results": [identity or _medium_identity() for _ in items],
        },
    )


def _fake_lookup(calls: list[dict], *, profiles: list[dict] | None = None, vin_outcome: str | None = None):
    profiles = [
        {
            **profile,
            "identifier_matches_request": profile.get("identifier_matches_request"),
            "requires_exact_identifier_confirmation": profile.get(
                "requires_exact_identifier_confirmation", profile.get("identifier_matches_request") is not True
            ),
        }
        for profile in profiles or []
    ]
    vin_outcome = vin_outcome or (
        "identifier_mismatch"
        if any(profile["identifier_matches_request"] is False for profile in profiles)
        else "identifier_unverified"
        if any(profile["identifier_matches_request"] is None for profile in profiles)
        else "success"
    )

    def lookup(**kwargs):
        calls.append(kwargs)
        operation = kwargs["operation"]
        dry_run = kwargs.get("dry_run", False)
        mismatch = operation == "vin_decode" and not dry_run and vin_outcome == "identifier_mismatch"
        return {
            "ok": not mismatch,
            "provider": "partsapi_ru",
            "operation": operation,
            "partsapi_method": {"vin_decode": "VINdecode", "search_tree": "getSearchTree"}[operation],
            "dry_run": dry_run,
            "outcome": "configured_unverified" if dry_run else vin_outcome if operation == "vin_decode" else "success",
            "failure_class": "provider_identifier_mismatch" if mismatch else None,
            "retryable": False,
            "requires_fallback": mismatch,
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


@pytest.mark.parametrize(
    ("matches", "outcome", "confirmation", "allowed"),
    [
        (True, "success", False, True),
        (None, "identifier_unverified", True, True),
        (None, "success", True, False),
        (None, "identifier_unverified", False, False),
        (False, "identifier_mismatch", True, False),
        (True, "identifier_mismatch", True, False),
    ],
)
def test_benchmark_preserves_vin_evidence_and_candidate_gate(monkeypatch, matches, outcome, confirmation, allowed):
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
                    "identifier_matches_request": matches,
                    "requires_exact_identifier_confirmation": confirmation,
                }
            ],
            vin_outcome=outcome,
        ),
    )
    result = benchmark_vin_parts_lookup(
        [{"identifier": SYNTHETIC_VIN}],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
        live_partsapi_identity=True,
    )
    assert [call["operation"] for call in calls] == (["vin_decode", "search_tree"] if allowed else ["vin_decode"])
    item = result["items"][0]
    agreement = item["identity"]["cross_source_agreement"]
    digest = item["prepared_calls"]["partsapi"][0]
    expected_matches = False if outcome == "identifier_mismatch" else matches
    expected_confirmation = confirmation or outcome in {"identifier_mismatch", "identifier_unverified"}
    for evidence in (agreement, digest):
        assert evidence["identifier_matches_request"] is expected_matches
        assert evidence["requires_exact_identifier_confirmation"] is expected_confirmation
        assert evidence["provider_outcome"] == outcome
    assert digest["outcome"] == outcome
    assert digest["failure_class"] == ("provider_identifier_mismatch" if outcome == "identifier_mismatch" else None)
    assert digest["retryable"] is False
    assert digest["requires_fallback"] is (outcome == "identifier_mismatch")
    assert item["identity"]["ready_for_tecdoc_candidate_lookup"] is allowed
    assert item["identity"]["ready_for_crm_writeback"] is False
    assert result["summary"]["oem_candidate_count"] == 0
    assert SYNTHETIC_VIN not in json.dumps(result)


@pytest.mark.parametrize("reverse", [False, True])
def test_benchmark_prioritizes_foreign_row_over_modification_ambiguity(reverse):
    profiles = [{"identifier_matches_request": True}, {"identifier_matches_request": False}]
    agreement = _assess_partsapi_identity_agreement(
        _medium_identity(),
        {"ok": True, "outcome": "success", "vehicle_profiles": profiles[::-1] if reverse else profiles},
    )
    assert agreement["status"] == "identifier_mismatch"
    assert agreement["identifier_matches_request"] is False
    assert agreement["requires_exact_identifier_confirmation"] is True


@pytest.mark.parametrize(
    "guard",
    [
        "model_conflict",
        "make_only",
        "low_confidence",
        "high_conflict",
        "medium_conflict",
        "invalid_car_id",
        "unknown_car_type",
        "car_type_conflict",
    ],
)
def test_benchmark_does_not_prepare_tree_without_candidate_readiness(monkeypatch, guard):
    identity = _medium_identity()
    profile = {"make": "HONDA", "model": "Accord", "tecdoc_car_id": "9877", "vehicle_type": "PC"}
    vehicle_type = None
    if guard == "model_conflict":
        profile["model"] = "Civic"
    elif guard == "make_only":
        profile.pop("model")
    elif guard == "low_confidence":
        identity["confidence_label"] = "low"
    elif guard in {"high_conflict", "medium_conflict"}:
        identity["conflicts"] = [
            {"field": "model_year", "severity": "medium" if guard == "medium_conflict" else "high"}
        ]
    elif guard == "invalid_car_id":
        profile["tecdoc_car_id"] = "0"
    elif guard == "unknown_car_type":
        profile.pop("vehicle_type")
    else:
        profile["vehicle_type"] = "CV"
        vehicle_type = "PC"
    _install_batch(monkeypatch, identity)
    calls = []
    monkeypatch.setattr(
        "autostop_manager.vin_parts_benchmark.partsapi_catalog_lookup", _fake_lookup(calls, profiles=[profile])
    )
    result = benchmark_vin_parts_lookup(
        [{"identifier": SYNTHETIC_VIN, "vehicle_type": vehicle_type}],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
        live_partsapi_identity=True,
    )
    assert [call["operation"] for call in calls] == ["vin_decode"]
    item = result["items"][0]
    assert item["identity"]["ready_for_tecdoc_candidate_lookup"] is False
    assert item["prepared_calls"]["partsapi"][0]["outcome"] == "identifier_unverified"


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
        "autostop_manager.vin_parts_benchmark._resolve_vin_oem_parts",
        fake_resolver,
    )
    result = benchmark_vin_parts_lookup(
        [
            {
                "identifier": SYNTHETIC_VIN,
                "vehicle_type": "CV",
                "crm_context": {
                    "make_display": "HONDA",
                    "model_display": "Accord",
                    "production_year": 2003,
                    "engine_model": "ENGINE-1",
                    "gearbox_model": "6AT",
                },
            }
        ],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
        resolve_oem=True,
    )
    assert result["summary"]["oem_candidate_count"] == 0
    assert result["summary"]["tecdoc_article_candidate_count"] == 2
    assert result["summary"]["ready_for_tecdoc_candidate_lookup_count"] == 1
    assert forwarded[0]["vehicle_type"] == "CV"
    assert {key: forwarded[0][key] for key in ("make", "model", "model_year", "engine", "transmission")} == {
        "make": "HONDA",
        "model": "Accord",
        "model_year": None,
        "engine": "ENGINE-1",
        "transmission": "6AT",
    }


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


@pytest.mark.parametrize("already_blocked", [False, True])
@pytest.mark.parametrize(
    ("kind", "status", "reasons"),
    [
        ("failed", "provider_failed", ["partsapi_identity_provider_failed"]),
        ("empty", "no_profile", ["partsapi_identity_no_profile", "partsapi_identity_missing_tecdoc_car_id"]),
        ("ambiguous", "ambiguous_vehicle_modification", ["partsapi_identity_ambiguous_vehicle_modification"]),
        (
            "uncompared",
            "profile_present_uncompared",
            ["partsapi_identity_insufficient_agreement", "partsapi_identity_profile_present_uncompared"],
        ),
        ("partial", "partial_match", ["partsapi_identity_insufficient_agreement"]),
    ],
)
def test_benchmark_incomplete_vin_decode_blocks_tree_without_duplicate_reasons(
    monkeypatch, kind, status, reasons, already_blocked
):
    profile = {"tecdoc_car_id": "9877", "vehicle_type": "PC"}
    profiles = [] if kind in {"failed", "empty"} else [profile]
    if kind == "partial":
        profile["make"] = "HONDA"
    elif kind == "ambiguous":
        profile.update(make="HONDA", model="Accord")
        profiles.append({**profile, "tecdoc_car_id": "9878"})
    identity = _medium_identity()
    if already_blocked:
        identity["parts_lookup_readiness"]["blocking_reasons"].extend(reasons)
    _install_batch(monkeypatch, identity)
    calls = []
    original = _fake_lookup(calls, profiles=profiles, vin_outcome="empty_result" if kind == "empty" else None)

    def lookup(**kwargs):
        result = original(**kwargs)
        if kind == "failed":
            result.update(ok=False, outcome="upstream_failure", failure_class="upstream_failure")
        return result

    monkeypatch.setattr("autostop_manager.vin_parts_benchmark.partsapi_catalog_lookup", lookup)
    result = benchmark_vin_parts_lookup(
        [{"identifier": SYNTHETIC_VIN}],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
        live_partsapi_identity=True,
    )
    assert [call["operation"] for call in calls] == ["vin_decode"]
    digest = result["items"][0]["identity"]
    assert digest["cross_source_agreement"]["status"] == status
    assert all(digest["blocking_reasons"].count(reason) == 1 for reason in reasons)
    assert digest["ready_for_tecdoc_candidate_lookup"] is False
    assert digest["ready_for_crm_writeback"] is False
    assert result["summary"]["tecdoc_article_candidate_count"] == 0


def test_benchmark_aggregates_provider_and_resolver_requirements(monkeypatch):
    _install_batch(monkeypatch)
    monkeypatch.setattr(
        "autostop_manager.vin_parts_benchmark.build_oem_parts_provider_plan",
        lambda **_k: {
            "blockers": [
                {"stage": "credentials", "missing_env_names": ["KEY_A", ""], "reason": "not_configured"},
                {"stage": "credentials", "missing_env": " KEY_B "},
                {"missing_env_names": ("KEY_C",)},
            ]
        },
    )
    identity = _medium_identity()
    monkeypatch.setattr(
        "autostop_manager.vin_parts_benchmark._resolve_vin_oem_parts",
        lambda **_k: {
            "identity": {**identity, **identity["parts_lookup_readiness"]},
            "candidate_count": 0,
            "article_candidate_count": 0,
            "calls": [{"operation": "articles", "missing_env_names": ["PARTSAPI_ARTICLES_KEY"]}],
        },
    )
    monkeypatch.setattr(
        "autostop_manager.vin_parts_benchmark.partsapi_catalog_lookup",
        lambda **_k: pytest.fail("A disabled preparation must not call PartsAPI"),
    )
    result = benchmark_vin_parts_lookup(
        [{"identifier": SYNTHETIC_VIN}],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
        resolve_oem=True,
        include_partsapi_dry_run=False,
    )
    assert result["summary"]["missing_env_names"] == ["KEY_A", "KEY_B", "KEY_C", "PARTSAPI_ARTICLES_KEY"]
    assert result["blockers_by_stage"] == {
        "credentials": {"count": 2, "missing_env_names": ["KEY_A", "KEY_B"], "reasons": ["not_configured"]},
        "unknown": {"count": 1, "missing_env_names": ["KEY_C"], "reasons": []},
    }
    assert result["summary"]["ready_for_crm_writeback_count"] == 0
    assert result["summary"]["oem_candidate_count"] == 0


@pytest.mark.parametrize(
    ("count", "ready", "status"),
    [(0, False, "no_items"), (1, True, "identity_ready_but_blocked_by_live_catalog_or_supplier_credentials")],
)
def test_benchmark_distinguishes_empty_batch_from_identity_ready(monkeypatch, count, ready, status):
    identity = _medium_identity()
    identity["parts_lookup_readiness"]["ready_for_oem_candidate_lookup"] = ready
    _install_batch(monkeypatch, identity)
    monkeypatch.setattr("autostop_manager.vin_parts_benchmark.partsapi_catalog_lookup", _fake_lookup([]))
    result = benchmark_vin_parts_lookup(
        [{"identifier": SYNTHETIC_VIN} for _ in range(count)],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
    )
    assert result["summary"]["count"] == count
    assert result["summary"]["benchmark_status"] == status
    assert result["summary"]["ready_for_oem_candidate_lookup_count"] == (count if ready else 0)
    assert result["summary"]["ready_for_crm_writeback_count"] == 0
    assert result["summary"]["oem_candidate_count"] == 0


@pytest.mark.parametrize("verified", [False, True])
@pytest.mark.parametrize(
    ("fields", "conflict", "field_sources"),
    [
        ({"engine_code": "ENGINE-B"}, "engine", None),
        ({"market": "US"}, "market", None),
        ({"model_year_from": 202001, "model_year_to": 202412}, "model_year", None),
        ({"engine": "engine.a", "market": "eu", "model_year_from": "2010", "model_year_to": "2010"}, None, None),
        ({"model_year_from": "unparsed", "model_year_to": None}, None, None),
        ({}, None, None),
        ({"engine_code": "ENGINE-B"}, "engine", ("engine", ["rule", ("NHTSA vPIC", "ENGINE-A")])),
        ({"engine_code": "ENGINE-A"}, None, ("engine", ["rule", ("NHTSA vPIC", "ENGINE-A")])),
        ({}, "engine", ("engine", ["CRM context", ("NHTSA vPIC", "ENGINE-B")])),
        ({}, None, ("engine", ["CRM context", ("NHTSA vPIC", "ENGINE-A")])),
        *[
            (
                {field: value if provider_present else None},
                None if agreed else field,
                (field, [("CRM context", value), ("NHTSA vPIC", value if agreed else other)]),
            )
            for field, value, other in [
                ("make", "HONDA", "TOYOTA"),
                ("model", "Accord", "Civic"),
                ("transmission", "6AT", "6MT"),
                ("model_year", 2010, 2015),
            ]
            for provider_present in [False, True]
            for agreed in [False, True]
        ],
        (
            {"model_year_from": 2010, "model_year_to": 2010},
            "model_year",
            ("model_year", [("rule", 2010), ("NHTSA vPIC", 2015)]),
        ),
        (
            {"model_year_from": 2015, "model_year_to": 2015},
            None,
            ("model_year", [("rule", 2010), ("NHTSA vPIC", 2015)]),
        ),
        ({"model": None, "model_family": "Accord"}, None, None),
    ],
)
def test_benchmark_known_characteristics_guard_candidate_readiness(
    monkeypatch, verified, fields, conflict, field_sources
):
    identity = _medium_identity()
    identity["vehicle_profile"].update(engine="ENGINE-A", market="EU", model_year=2010, transmission="6AT")
    if "model_family" in fields:
        identity["vehicle_profile"].update(model=None, model_family="Accord")
    if field_sources:
        field, sources = field_sources
        known_value = identity["vehicle_profile"][field]
        hint = "Europe/global" if field == "market" else "ENGINE-HINT likely"
        sources = [
            (source, known_value if source == "CRM context" else hint) if isinstance(source, str) else source
            for source in sources
        ]
        identity["field_evidence"] = [{"field": field, "source": source, "value": value} for source, value in sources]
        identity["vehicle_profile"][field] = identity["field_evidence"][0]["value"]
    _install_batch(monkeypatch, identity)
    profiles = [{"make": "HONDA", "model": "Accord", "tecdoc_car_id": "9877", "vehicle_type": "PC", **fields}]
    profiles[0]["identifier_matches_request"] = True if verified else None
    calls = []
    monkeypatch.setattr(
        "autostop_manager.vin_parts_benchmark.partsapi_catalog_lookup", _fake_lookup(calls, profiles=profiles)
    )
    result = benchmark_vin_parts_lookup(
        [{"identifier": SYNTHETIC_VIN}],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
        live_partsapi_identity=True,
    )
    digest = result["items"][0]["identity"]
    agreement = digest["cross_source_agreement"]
    can_read = not conflict and bool(
        profiles[0].get("make") and (profiles[0].get("model") or profiles[0].get("model_family"))
    )
    assert agreement["status"] == ("conflict" if conflict else "matched" if can_read else "partial_match")
    assert [item["field"] for item in agreement["conflicting_fields"]] == ([conflict] if conflict else [])
    if not fields and field_sources:
        assert "engine" not in agreement["matched_fields"]
    assert [call["operation"] for call in calls] == (["vin_decode", "search_tree"] if can_read else ["vin_decode"])
    assert digest["ready_for_tecdoc_candidate_lookup"] is can_read
    assert digest["ready_for_crm_writeback"] is False
    assert result["summary"]["oem_candidate_count"] == result["summary"]["tecdoc_article_candidate_count"] == 0


@pytest.mark.parametrize("items", [[{"identifier": SYNTHETIC_VIN}] * 501, {"items": []}])
def test_benchmark_rejects_invalid_outer_batch_without_decoding_or_catalogs(monkeypatch, items):
    monkeypatch.setattr(
        "autostop_manager.vin_parts_benchmark.decode_vehicle_identities",
        lambda *_a, **_k: pytest.fail("Rejected batch reached E4"),
    )
    monkeypatch.setattr(
        "autostop_manager.vin_parts_benchmark.partsapi_catalog_lookup",
        lambda **_k: pytest.fail("Rejected batch reached PartsAPI"),
    )
    result = benchmark_vin_parts_lookup(items, requested_part="передние колодки")
    assert result["ok"] is False
    assert result["status"] == "invalid_input"
    assert result["errors"][0]["field"] == "items"
    assert result["summary"]["count"] == result["summary"]["partsapi_live_call_count"] == 0
    assert result["items"] == []
    assert SYNTHETIC_VIN not in json.dumps(result)


def test_benchmark_preserves_invalid_and_missing_row_positions_without_catalog_calls(monkeypatch):
    from autostop_manager.vehicle_identity import invalid_identity_result

    invalid = invalid_identity_result(
        [{"code": "expected_string", "field": "model", "stage": "input_validation"}], item_index=1
    )
    monkeypatch.setattr(
        "autostop_manager.vin_parts_benchmark.decode_vehicle_identities",
        lambda items, **_k: {"ok": True, "count": len(items), "results": [_medium_identity(), invalid]},
    )
    plan_calls = []

    def fake_plan(**kwargs):
        plan_calls.append(kwargs)
        return {}

    monkeypatch.setattr("autostop_manager.vin_parts_benchmark.build_oem_parts_provider_plan", fake_plan)
    monkeypatch.setattr(
        "autostop_manager.vin_parts_benchmark.partsapi_catalog_lookup",
        lambda **_k: pytest.fail("Disabled or invalid row reached PartsAPI"),
    )
    result = benchmark_vin_parts_lookup(
        [{"identifier": SYNTHETIC_VIN}, {"identifier": SYNTHETIC_VIN, "model": {}}, None],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
        include_partsapi_dry_run=False,
    )
    assert result["summary"]["count"] == 3
    assert result["summary"]["error_count"] == 2
    assert [item["index"] for item in result["items"]] == [1, 2, 3]
    assert len(plan_calls) == 1
    for offset in (1, 2):
        row = result["items"][offset]
        assert row["identity"]["ok"] is False
        assert row["identity"]["item_index"] == offset
        assert row["prepared_calls"]["partsapi"] == []
        assert row["oem_resolution"] is None
        assert row["manual_public_search"]["count"] == 0
        assert row["identity"]["ready_for_crm_writeback"] is False


@pytest.mark.parametrize("resolve_oem", [False, True])
def test_benchmark_input_error_never_calls_provider_plan_or_resolver(monkeypatch, resolve_oem):
    from autostop_manager.vehicle_identity import invalid_identity_result

    _install_batch(
        monkeypatch,
        invalid_identity_result(
            [{"code": "expected_integer_year", "field": "model_year", "stage": "input_validation"}]
        ),
    )
    for name in ("partsapi_catalog_lookup", "build_oem_parts_provider_plan", "_resolve_vin_oem_parts"):
        monkeypatch.setattr(
            f"autostop_manager.vin_parts_benchmark.{name}",
            lambda **_k: pytest.fail("Input error reached a catalog consumer"),
        )
    result = benchmark_vin_parts_lookup(
        [{"identifier": SYNTHETIC_VIN}],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
        live_partsapi_identity=True,
        resolve_oem=resolve_oem,
    )
    assert result["summary"]["error_count"] == 1
    assert result["summary"]["partsapi_live_call_count"] == 0


def test_benchmark_keeps_batch_provenance_and_disputes_through_oem_resolution(monkeypatch):
    from autostop_manager import vin_oem_resolver

    identity = _medium_identity()
    identity["vehicle_profile"].update(production_year=2009, production_date="2009-12", modification="variant-a")
    identity["parts_lookup_readiness"].update(ready_for_family_lookup=True, ready_for_vehicle_lookup=False)
    identity["conflicts"] = [{"field": "engine", "severity": "high", "blocking_scopes": ["vehicle"]}]
    identity["field_statuses"] = {
        "engine": {"status": "disputed", "evidence_ids": ["a", "b"], "alternatives": ["A", "B"]}
    }
    identity["field_evidence"] = [{"field": "engine", "value": "A", "source": "source-a", "evidence_id": "a"}]
    identity["provenance"] = {"engine": {"evidence_ids": ["a", "b"]}}
    identity["missing_fields"] = ["options"]
    identity["family_candidates"] = [
        {"make": "HONDA", "model": "Accord", "source": "source-a", "evidence_ids": ["family-a"]}
    ]
    _install_batch(monkeypatch, identity)
    monkeypatch.setattr(
        vin_oem_resolver, "decode_vehicle_identity", lambda *_a, **_k: pytest.fail("Benchmark repeated E4 decoding")
    )
    calls = []
    monkeypatch.setattr(
        vin_oem_resolver,
        "partsapi_catalog_lookup",
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
    monkeypatch.setattr(
        "autostop_manager.vin_parts_benchmark.partsapi_catalog_lookup",
        lambda **_k: pytest.fail("A disabled preparation called PartsAPI"),
    )
    result = benchmark_vin_parts_lookup(
        [{"identifier": SYNTHETIC_VIN}],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
        resolve_oem=True,
        live_partsapi_oem=True,
        include_partsapi_dry_run=False,
    )
    digest = result["items"][0]["identity"]
    assert [call["operation"] for call in calls] == ["vin_decode"]
    assert digest["ready_for_family_lookup"] is True
    assert digest["ready_for_vehicle_lookup"] is False
    assert digest["ready_for_tecdoc_candidate_lookup"] is False
    assert digest["vehicle_profile"]["production_year"] == 2009
    assert digest["vehicle_profile"]["production_date"] == "2009-12"
    for key in ("field_statuses", "field_evidence", "provenance", "missing_fields", "family_candidates", "conflicts"):
        assert digest[key] == identity[key]
    assert result["summary"]["ready_for_family_lookup_count"] == 1
    assert result["summary"]["ready_for_vehicle_lookup_count"] == 0


def test_benchmark_missing_writeback_permission_stays_false(monkeypatch):
    identity = _medium_identity()
    identity["confidence_label"] = "high"
    identity["parts_lookup_readiness"] = {"ready_for_oem_lookup": True}
    _install_batch(monkeypatch, identity)
    result = benchmark_vin_parts_lookup(
        [{"identifier": SYNTHETIC_VIN}],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
        include_partsapi_dry_run=False,
    )
    assert result["items"][0]["identity"]["ready_for_oem_candidate_lookup"] is True
    assert result["items"][0]["identity"]["ready_for_crm_writeback"] is False
    assert result["summary"]["ready_for_crm_writeback_count"] == 0


def test_benchmark_provider_plan_honors_resolver_identifier_mismatch(monkeypatch):
    from autostop_manager import vin_oem_resolver

    identity = _medium_identity()
    identity["confidence_label"] = "high"
    identity["parts_lookup_readiness"].update(
        ready_for_family_lookup=True,
        ready_for_vehicle_lookup=True,
        ready_for_oem_lookup=True,
        ready_for_oem_candidate_lookup=True,
    )
    _install_batch(monkeypatch, identity)
    monkeypatch.setattr(
        vin_oem_resolver, "decode_vehicle_identity", lambda *_a, **_k: pytest.fail("Benchmark repeated E4 decoding")
    )
    calls = []
    monkeypatch.setattr(
        vin_oem_resolver,
        "partsapi_catalog_lookup",
        _fake_lookup(
            calls,
            profiles=[
                {
                    "make": "HONDA",
                    "model": "Accord",
                    "tecdoc_car_id": "9877",
                    "vehicle_type": "PC",
                    "identifier_matches_request": False,
                }
            ],
        ),
    )
    result = benchmark_vin_parts_lookup(
        [{"identifier": SYNTHETIC_VIN}],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
        resolve_oem=True,
        live_partsapi_oem=True,
        include_partsapi_dry_run=False,
    )
    row = result["items"][0]
    assert [call["operation"] for call in calls] == ["vin_decode"]
    assert row["identity"]["cross_source_agreement"]["status"] == "identifier_mismatch"
    assert row["identity"]["ready_for_vehicle_lookup"] is False
    assert row["live_capability"]["identity_ready_for_vehicle_lookup"] is False
    assert row["live_capability"]["identity_ready_for_family_lookup"] is True
    assert row["live_capability"]["identity_ready_for_crm_writeback"] is False


def test_benchmark_family_queries_omit_supported_engine_after_catalog_disagreement(monkeypatch):
    from autostop_manager import vin_oem_resolver

    identity = _medium_identity()
    identity["vehicle_profile"]["engine"] = "ENGINE-A"
    identity["field_statuses"] = {"engine": {"status": "supported", "evidence_ids": ["a"]}}
    identity["field_evidence"] = [{"field": "engine", "source": "CRM context", "value": "ENGINE-A"}]
    identity["parts_lookup_readiness"].update(ready_for_family_lookup=True, ready_for_vehicle_lookup=True)
    _install_batch(monkeypatch, identity)
    calls = []
    monkeypatch.setattr(
        vin_oem_resolver,
        "partsapi_catalog_lookup",
        _fake_lookup(
            calls,
            profiles=[
                {
                    "make": "HONDA",
                    "model": "Accord",
                    "engine": "ENGINE-B",
                    "tecdoc_car_id": "9877",
                    "vehicle_type": "PC",
                    "identifier_matches_request": True,
                }
            ],
        ),
    )
    result = benchmark_vin_parts_lookup(
        [{"identifier": SYNTHETIC_VIN}],
        requested_part="передние колодки",
        live_vpic=False,
        use_vpic_batch=False,
        resolve_oem=True,
        live_partsapi_oem=True,
        include_partsapi_dry_run=False,
    )
    row = result["items"][0]
    assert [call["operation"] for call in calls] == ["vin_decode"]
    assert [conflict["field"] for conflict in row["identity"]["cross_source_agreement"]["conflicting_fields"]] == [
        "engine"
    ]
    assert row["identity"]["ready_for_vehicle_lookup"] is False
    assert row["live_capability"]["identity_ready_for_vehicle_lookup"] is False
    assert row["live_capability"]["identity_ready_for_family_lookup"] is True
    assert row["manual_public_search"]["count"] > 0
    assert all(
        "ENGINE-A" not in query["query"] and "ENGINE-B" not in query["query"]
        for query in row["manual_public_search"]["queries"]
    )
