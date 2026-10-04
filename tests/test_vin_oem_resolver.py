from __future__ import annotations

import json

import pytest

from autostop_manager.catalog_clients import extract_partsapi_article_candidates
from autostop_manager.vin_oem_resolver import (
    _assess_partsapi_identity_agreement,
    _rank_article_candidate,
    resolve_vin_oem_parts,
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
        "conflicts": [],
        "warnings": [],
    }


def _fake_lookup(
    calls: list[dict],
    *,
    profiles: list[dict] | None = None,
    rows: list[dict] | None = None,
    articles: list[dict] | None = None,
    failures: set[str] | None = None,
    vin_outcome: str | None = None,
):
    profiles = (
        profiles
        if profiles is not None
        else [
            {
                "make": "HONDA",
                "model": "Accord",
                "tecdoc_car_id": "9877",
                "vehicle_type": "PC",
                "identifier_matches_request": True,
            }
        ]
    )
    profiles = [
        {
            **profile,
            "identifier_matches_request": profile.get("identifier_matches_request"),
            "requires_exact_identifier_confirmation": profile.get(
                "requires_exact_identifier_confirmation", profile.get("identifier_matches_request") is not True
            ),
        }
        for profile in profiles
    ]
    vin_outcome = vin_outcome or (
        "identifier_mismatch"
        if any(profile["identifier_matches_request"] is False for profile in profiles)
        else "identifier_unverified"
        if any(profile["identifier_matches_request"] is None for profile in profiles)
        else "success"
    )
    rows = rows if rows is not None else [{"NODE_3_TEXT": "Колодки тормозные", "NODE_3_STR_ID": 100470}]
    articles = (
        articles
        if articles is not None
        else [
            {
                "article_id": "123",
                "part_number": "ABC123",
                "brand": "Example",
                "product_name": "Front brake pads",
                "fitment_evidence": {"fitment_confirmed": False},
            }
        ]
    )
    failures = failures or set()

    def lookup(**kwargs):
        calls.append(kwargs)
        operation = kwargs["operation"]
        dry_run = kwargs.get("dry_run", False)
        failed = operation in failures and not dry_run
        mismatch = operation == "vin_decode" and not dry_run and vin_outcome == "identifier_mismatch"
        return {
            "ok": not failed and not mismatch,
            "provider": "partsapi_ru",
            "operation": operation,
            "partsapi_method": {"vin_decode": "VINdecode", "search_tree": "getSearchTree", "articles": "getArticles"}[
                operation
            ],
            "dry_run": dry_run,
            "outcome": "upstream_failure"
            if failed
            else "configured_unverified"
            if dry_run
            else vin_outcome
            if operation == "vin_decode"
            else "success",
            "failure_class": "upstream_failure" if failed else "provider_identifier_mismatch" if mismatch else None,
            "retryable": failed,
            "requires_fallback": failed or mismatch,
            "attempt_count": 0 if dry_run else 1,
            "request_plan": {"configured": True, "params": {}, "redacted_url": "https://api.partsapi.ru?key=***"},
            "vehicle_profiles": profiles if operation == "vin_decode" and not dry_run and not failed else [],
            "search_tree_rows": rows if operation == "search_tree" and not dry_run and not failed else [],
            "article_candidates": articles if operation == "articles" and not dry_run and not failed else [],
            "oem_candidates": [],
        }

    return lookup


def _install_fakes(monkeypatch, **kwargs) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(
        "autostop_manager.vin_oem_resolver.decode_vehicle_identity", lambda *_a, **_k: _medium_identity()
    )
    monkeypatch.setattr("autostop_manager.vin_oem_resolver.partsapi_catalog_lookup", _fake_lookup(calls, **kwargs))
    return calls


def test_resolver_uses_current_three_method_chain_and_keeps_fitment_manual(monkeypatch):
    calls = _install_fakes(monkeypatch)
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_identity=True,
        live_partsapi_oem=True,
    )

    assert [call["operation"] for call in calls] == ["vin_decode", "search_tree", "articles"]
    assert [call["dry_run"] for call in calls] == [False, False, False]
    assert calls[1]["type_id"] == "9877"
    assert calls[2]["category"] == "100470"
    assert result["status"] == "tecdoc_articles_found_needs_manual_fitment"
    assert result["live_call_count"] == 3
    assert result["category_resolution"]["category_mode"] == "exact_tree_name"
    assert result["article_candidate_count"] == 1
    assert result["article_candidates"][0]["candidate_kind"] == "tecdoc_aftermarket_article"
    assert result["article_candidates"][0]["vin_fitment_confirmed"] is False
    assert result["article_candidates"][0]["oem_number_confirmed"] is False
    assert result["article_candidates"][0]["manual_review_required"] is True
    assert result["oem_candidates"] == []
    assert result["candidate_count"] == 0
    assert result["readiness"]["ready_for_crm_writeback"] is False
    assert result["crm_writeback_gate"]["can_prepare_manual_writeback"] is False
    assert SYNTHETIC_VIN not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize(
    ("field", "title", "expected_match"),
    [
        ("ART_PRODUCT_NAME", "Rear brake pads", "conflict"),
        ("ART_PRODUCT_NAME", "Front brake pads", "matched"),
        ("PRODUCT_GROUP", "Rear brake pads", "conflict"),
        ("PRODUCT_GROUP", "Front brake pads", "matched"),
    ],
)
def test_resolver_compares_position_from_normalized_article_fields(monkeypatch, field, title, expected_match):
    articles = extract_partsapi_article_candidates(
        payload=[{"ART_ID": "42", "ART_ARTICLE_NR": "SYNTHETIC123", "ART_SUP_BRAND": "Example", field: title}],
        operation="articles",
    )
    _install_fakes(monkeypatch, articles=articles)
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_oem=True,
    )
    candidate = result["article_candidates"][0]
    assert candidate["requested_position_coordinates"] == {"axle": "front"}
    assert candidate["position_match"] == expected_match
    assert ("candidate_position_conflict" in candidate["blocking_reasons"]) is (expected_match == "conflict")
    assert candidate["vin_fitment_confirmed"] is False
    assert candidate["manual_review_required"] is True
    assert result["readiness"]["ready_for_crm_writeback"] is False


@pytest.mark.parametrize(
    "profiles",
    [
        [{"make": "HONDA", "tecdoc_car_id": "9877"}, {"make": "HONDA", "tecdoc_car_id": "9878"}],
        [{"make": "HONDA", "tecdoc_car_id": "9877", "identifier_matches_request": False}],
        [{"make": "HONDA"}],
    ],
)
def test_resolver_requires_unambiguous_matching_vehicle_modification(monkeypatch, profiles):
    calls = _install_fakes(monkeypatch, profiles=profiles)
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_identity=True,
        live_partsapi_oem=True,
    )
    assert [call["operation"] for call in calls] == ["vin_decode"]
    assert result["article_candidates"] == []
    assert result["readiness"]["has_tecdoc_car_id"] is False
    assert result["status"] in {"needs_identity_confirmation", "needs_vehicle_modification"}


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
def test_resolver_keeps_vin_verification_separate_from_candidate_lookup(
    monkeypatch, matches, outcome, confirmation, allowed
):
    calls = _install_fakes(
        monkeypatch,
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
    )
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_oem=True,
    )
    assert [call["operation"] for call in calls] == (
        ["vin_decode", "search_tree", "articles"] if allowed else ["vin_decode"]
    )
    agreement = result["identity"]["cross_source_agreement"]
    expected_matches = False if outcome == "identifier_mismatch" else matches
    expected_confirmation = confirmation or outcome in {"identifier_mismatch", "identifier_unverified"}
    for evidence in (agreement, result["tecdoc_vehicle"], result["calls"][0]):
        assert evidence["identifier_matches_request"] is expected_matches
        assert evidence["requires_exact_identifier_confirmation"] is expected_confirmation
        assert evidence["provider_outcome"] == outcome
    assert agreement["status"] == ("identifier_mismatch" if expected_matches is False else "matched")
    assert result["article_candidate_count"] == int(allowed)
    assert result["readiness"]["ready_for_crm_writeback"] is False
    if allowed:
        candidate = result["article_candidates"][0]
        assert candidate["vin_fitment_confirmed"] is False
        assert candidate["manual_review_required"] is True
    assert SYNTHETIC_VIN not in json.dumps(result)


@pytest.mark.parametrize("reverse", [False, True])
def test_resolver_prioritizes_foreign_row_over_modification_ambiguity(reverse):
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
        "boolean_car_id",
        "unknown_car_type",
        "car_type_conflict",
    ],
)
def test_resolver_unverified_candidates_require_identity_guards(monkeypatch, guard):
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
    elif guard in {"invalid_car_id", "boolean_car_id"}:
        profile["tecdoc_car_id"] = True if guard == "boolean_car_id" else "0"
    elif guard == "unknown_car_type":
        profile.pop("vehicle_type")
    else:
        profile["vehicle_type"] = "CV"
        vehicle_type = "PC"
    calls = _install_fakes(monkeypatch, profiles=[profile])
    monkeypatch.setattr("autostop_manager.vin_oem_resolver.decode_vehicle_identity", lambda *_a, **_k: identity)
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_oem=True,
        vehicle_type=vehicle_type,
    )
    assert [call["operation"] for call in calls] == ["vin_decode"]
    assert result["calls"][0]["outcome"] == "identifier_unverified"
    assert result["readiness"]["ready_for_tecdoc_candidate_lookup"] is False
    assert result["identity"]["ready_for_tecdoc_candidate_lookup"] is False
    assert result["article_candidates"] == []


def test_resolver_requires_exact_tree_node_or_explicit_id_from_same_tree(monkeypatch):
    ambiguous_rows = [
        {"NODE_3_TEXT": "Колодки тормозные", "NODE_3_STR_ID": 100470},
        {"NODE_3_TEXT": "Колодки тормозные", "NODE_3_STR_ID": 100471},
    ]
    calls = _install_fakes(monkeypatch, rows=ambiguous_rows)
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_oem=True,
    )
    assert [call["operation"] for call in calls] == ["vin_decode", "search_tree"]
    assert result["status"] == "needs_tecdoc_tree_node"
    assert result["category_resolution"]["candidate_node_ids"] == ["100470", "100471"]

    calls.clear()
    selected = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        tecdoc_tree_node_id="100471",
        live_vpic=False,
        live_partsapi_oem=True,
    )
    assert [call["operation"] for call in calls] == ["vin_decode", "search_tree", "articles"]
    assert calls[2]["category"] == "100471"
    assert selected["article_candidate_count"] == 1

    calls.clear()
    unknown = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        tecdoc_tree_node_id="999999",
        live_vpic=False,
        live_partsapi_oem=True,
    )
    assert [call["operation"] for call in calls] == ["vin_decode", "search_tree"]
    assert unknown["status"] == "needs_tecdoc_tree_node"


def test_resolver_auto_selects_specific_node_but_accepts_explicit_ancestor(monkeypatch):
    calls = _install_fakes(
        monkeypatch,
        rows=[
            {
                "ROOT_NODE_TEXT": "Тормозная система",
                "ROOT_NODE_STR_ID": 100,
                "NODE_1_TEXT": "Колодки тормозные",
                "NODE_1_STR_ID": 200,
            }
        ],
    )
    auto = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_oem=True,
    )
    assert calls[2]["category"] == "200"
    assert auto["category_resolution"]["category_mode"] == "exact_tree_name"

    calls.clear()
    explicit = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        tecdoc_tree_node_id="100",
        live_vpic=False,
        live_partsapi_oem=True,
    )
    assert calls[2]["category"] == "100"
    assert explicit["category_resolution"]["category_mode"] == "explicit_tree_node"


def test_resolver_dry_run_is_one_redacted_vin_decode_plan(monkeypatch):
    calls = _install_fakes(monkeypatch)
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_oem=True,
        dry_run=True,
    )
    assert [call["operation"] for call in calls] == ["vin_decode"]
    assert calls[0]["dry_run"] is True
    assert result["live_call_count"] == 0
    assert result["status"] == "ready_for_live_tecdoc_lookup"
    assert SYNTHETIC_VIN not in json.dumps(result, ensure_ascii=False)


def test_resolver_stops_at_live_budget_before_articles(monkeypatch):
    calls = _install_fakes(monkeypatch)
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_oem=True,
        max_live_calls=2,
    )
    assert [call["dry_run"] for call in calls] == [False, False, True]
    assert result["live_call_count"] == 2
    assert result["article_candidate_count"] == 0
    assert result["status"] == "ready_for_live_tecdoc_lookup"


def test_resolver_counts_retry_attempts_against_total_live_budget(monkeypatch):
    calls = _install_fakes(monkeypatch)
    original = _fake_lookup([])

    def retried_lookup(**kwargs):
        result = original(**kwargs)
        if kwargs["operation"] == "vin_decode":
            result["attempt_count"] = 2
        calls.append(kwargs)
        return result

    monkeypatch.setattr("autostop_manager.vin_oem_resolver.partsapi_catalog_lookup", retried_lookup)
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_oem=True,
        max_live_calls=3,
        max_attempts=3,
    )
    assert [call["operation"] for call in calls] == ["vin_decode", "search_tree", "articles"]
    assert [call["max_attempts"] for call in calls] == [3, 1, 1]
    assert [call["dry_run"] for call in calls] == [False, False, True]
    assert result["live_call_count"] == 3


@pytest.mark.parametrize("operation", ["vin_decode", "search_tree", "articles"])
def test_resolver_reports_provider_failure_without_claiming_no_parts(monkeypatch, operation):
    calls = _install_fakes(monkeypatch, failures={operation})
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_oem=True,
    )
    chain = ["vin_decode", "search_tree", "articles"]
    assert [call["operation"] for call in calls] == chain[: chain.index(operation) + 1]
    assert result["status"] == "tecdoc_lookup_provider_failed"
    assert result["tecdoc_lookup_outcome"]["outcome"] == (
        "provider_failed" if operation == "articles" else "not_attempted"
    )
    assert result["article_candidates"] == []
    assert any(item["stage"] == "partsapi_tecdoc_lookup" for item in result["blockers"])


@pytest.mark.parametrize("broken_operation", ["vin_decode", "search_tree", "articles"])
def test_resolver_reports_unparsed_nonempty_payload(monkeypatch, broken_operation):
    calls = _install_fakes(monkeypatch)
    original = _fake_lookup([])

    def unparsed_lookup(**kwargs):
        result = original(**kwargs)
        calls.append(kwargs)
        if kwargs["operation"] == broken_operation:
            result.update(
                ok=False,
                outcome="unparsed_response",
                failure_class="adapter_unparsed_response",
                empty_payload=False,
                response_shape={"kind": "dict"},
                vehicle_profiles=[],
                search_tree_rows=[],
                article_candidates=[],
            )
        return result

    monkeypatch.setattr("autostop_manager.vin_oem_resolver.partsapi_catalog_lookup", unparsed_lookup)
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_oem=True,
    )
    assert result["status"] == "tecdoc_parser_gap"
    assert result["article_candidate_count"] == 0
    assert any(action["code"] == "inspect_provider_response" for action in result["manual_actions"])
    assert not any(
        action["code"] in {"manual_catalog_fallback", "select_tecdoc_tree_node"} for action in result["manual_actions"]
    )


def test_resolver_distinguishes_truly_empty_articles(monkeypatch):
    calls = _install_fakes(monkeypatch)
    original = _fake_lookup([])

    def empty_articles(**kwargs):
        result = original(**kwargs)
        calls.append(kwargs)
        if kwargs["operation"] == "articles":
            result.update(outcome="empty_result", empty_payload=True, article_candidates=[])
        return result

    monkeypatch.setattr("autostop_manager.vin_oem_resolver.partsapi_catalog_lookup", empty_articles)
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_oem=True,
    )
    assert result["status"] == "no_tecdoc_articles_found_needs_manual_lookup"
    assert any(action["code"] == "manual_catalog_fallback" for action in result["manual_actions"])


def test_resolver_does_not_request_articles_when_tree_is_truly_empty(monkeypatch):
    calls = _install_fakes(monkeypatch)
    original = _fake_lookup([])

    def empty_tree(**kwargs):
        result = original(**kwargs)
        calls.append(kwargs)
        if kwargs["operation"] == "search_tree":
            result.update(outcome="empty_result", empty_payload=True, search_tree_rows=[])
        return result

    monkeypatch.setattr("autostop_manager.vin_oem_resolver.partsapi_catalog_lookup", empty_tree)
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_oem=True,
    )
    assert [call["operation"] for call in calls] == ["vin_decode", "search_tree"]
    assert result["status"] == "no_tecdoc_tree_nodes_needs_manual_lookup"
    assert any(action["code"] == "manual_catalog_fallback" for action in result["manual_actions"])
    assert not any(action["code"] == "select_tecdoc_tree_node" for action in result["manual_actions"])


def test_resolver_rejects_frame_for_vin_only_method(monkeypatch):
    calls = _install_fakes(monkeypatch)
    result = resolve_vin_oem_parts(
        identifier="ABC-123456",
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_oem=True,
    )
    assert calls == []
    assert result["status"] == "unsupported_identifier_for_vin_decode"


def test_resolver_redacts_compact_and_hyphenated_frame_aliases(monkeypatch):
    _install_fakes(monkeypatch)
    result = resolve_vin_oem_parts(
        identifier="MR41S123456",
        requested_part="передние колодки MR41S123456 MR41S-123456",
        live_vpic=False,
        dry_run=True,
    )
    rendered = json.dumps(result, ensure_ascii=False)
    assert "MR41S123456" not in rendered
    assert "MR41S-123456" not in rendered


def test_resolver_uses_provider_vehicle_type_and_blocks_conflict(monkeypatch):
    calls = _install_fakes(
        monkeypatch, profiles=[{"make": "HONDA", "model": "Accord", "tecdoc_car_id": "9877", "vehicle_type": "CV"}]
    )
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_oem=True,
    )
    assert calls[1]["vehicle_type"] == "CV"
    assert result["tecdoc_vehicle"]["vehicle_type_source"] == "vin_decode"

    calls.clear()
    lower_case = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        vehicle_type="cv",
        live_vpic=False,
        live_partsapi_oem=True,
    )
    assert [call["operation"] for call in calls] == ["vin_decode", "search_tree", "articles"]
    assert calls[1]["vehicle_type"] == "CV"
    assert lower_case["tecdoc_vehicle"]["vehicle_type"] == "CV"

    calls.clear()
    conflict = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        vehicle_type="PC",
        live_vpic=False,
        live_partsapi_oem=True,
    )
    assert [call["operation"] for call in calls] == ["vin_decode"]
    assert conflict["article_candidate_count"] == 0
    assert any(item["stage"] == "partsapi_vehicle_type" for item in conflict["blockers"])


def test_resolver_requires_car_type_when_provider_does_not_supply_it(monkeypatch):
    calls = _install_fakes(monkeypatch, profiles=[{"make": "HONDA", "model": "Accord", "tecdoc_car_id": "9877"}])
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_oem=True,
    )
    assert [call["operation"] for call in calls] == ["vin_decode"]
    assert result["status"] == "needs_vehicle_type"
    assert result["identity"]["ready_for_tecdoc_candidate_lookup"] is False
    assert result["tecdoc_vehicle"]["vehicle_type_source"] == "unknown"

    calls.clear()
    explicit = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        vehicle_type="PC",
        live_vpic=False,
        live_partsapi_oem=True,
    )
    assert [call["operation"] for call in calls] == ["vin_decode", "search_tree", "articles"]
    assert explicit["tecdoc_vehicle"]["vehicle_type_source"] == "caller"


def test_resolver_does_not_spend_tree_quota_on_make_only_match(monkeypatch):
    calls = _install_fakes(monkeypatch, profiles=[{"make": "HONDA", "tecdoc_car_id": "9877", "vehicle_type": "PC"}])
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_oem=True,
    )
    assert [call["operation"] for call in calls] == ["vin_decode"]
    assert result["status"] == "needs_identity_confirmation"
    assert result["identity"]["cross_source_agreement"]["status"] == "partial_match"


def test_resolver_redacts_vins_embedded_in_part_text(monkeypatch):
    _install_fakes(monkeypatch)
    another_vin = "B" * 17
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part=f"передние колодки VIN {SYNTHETIC_VIN} и {another_vin}",
        live_vpic=False,
        dry_run=True,
    )
    rendered = json.dumps(result, ensure_ascii=False)
    assert SYNTHETIC_VIN not in rendered
    assert another_vin not in rendered
    assert "[REDACTED" in rendered


@pytest.mark.parametrize(("vehicle_type", "expected"), [("pc", "PC"), ("motorcycle", "Motorcycle")])
def test_resolver_normalizes_caller_vehicle_type(monkeypatch, vehicle_type, expected):
    calls = _install_fakes(monkeypatch, profiles=[{"make": "HONDA", "model": "Accord", "tecdoc_car_id": "9877"}])
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        vehicle_type=vehicle_type,
        live_vpic=False,
        live_partsapi_oem=True,
    )
    assert calls[1]["vehicle_type"] == expected
    assert result["tecdoc_vehicle"]["vehicle_type"] == expected


@pytest.mark.parametrize(
    ("field", "left", "right", "status"),
    [
        ("model", "A8", "A80", "conflict"),
        ("model", "Corolla", "Corolla Cross", "conflict"),
        ("transmission", "6AT", "6MT", "conflict"),
        ("transmission", "6AT", "6 speed automatic", "partial_match"),
        ("make", "VW", "Volkswagen", "partial_match"),
    ],
)
def test_vin_decode_profile_uses_shared_identity_comparison(field, left, right, status):
    result = _assess_partsapi_identity_agreement(
        {"vehicle_profile": {field: left}},
        {"ok": True, "vehicle_profiles": [{field: right}]},
    )
    assert result["status"] == status


@pytest.mark.parametrize(
    ("identifier", "part", "status", "action"),
    [
        ("", "передние колодки", "needs_vin_or_frame", "request_identifier"),
        ("123", "передние колодки", "unsupported_identifier_for_vin_decode", "confirm_identifier"),
        (SYNTHETIC_VIN, "неизвестная деталь", "needs_part_clarification", "clarify_part"),
        (SYNTHETIC_VIN, "колодки", "needs_part_clarification", "clarify_part_position"),
    ],
)
def test_resolver_incomplete_input_preserves_manual_next_step(monkeypatch, identifier, part, status, action):
    calls = _install_fakes(monkeypatch)
    result = resolve_vin_oem_parts(identifier=identifier, requested_part=part, live_vpic=False, live_partsapi_oem=True)
    assert result["status"] == status
    assert action in {item["code"] for item in result["manual_actions"]}
    assert [call["operation"] for call in calls] == (["vin_decode"] if identifier == SYNTHETIC_VIN else [])
    assert result["readiness"]["ready_for_tecdoc_candidate_lookup"] is False
    assert result["identity"]["ready_for_crm_writeback"] is False
    assert result["article_candidates"] == []


@pytest.mark.parametrize(
    ("profile_update", "reason"),
    [
        ({"model": "Civic"}, "partsapi_identity_conflict"),
        ({"model": ""}, "partsapi_identity_insufficient_agreement"),
        ({"tecdoc_car_id": ""}, "partsapi_identity_missing_tecdoc_car_id"),
    ],
)
def test_resolver_preserves_existing_identity_blocker(monkeypatch, profile_update, reason):
    profile = {"make": "HONDA", "model": "Accord", "tecdoc_car_id": "9877", "vehicle_type": "PC"}
    profile.update(profile_update)
    calls = _install_fakes(monkeypatch, profiles=[profile])
    identity = _medium_identity()
    identity["parts_lookup_readiness"]["blocking_reasons"].append(reason)
    monkeypatch.setattr("autostop_manager.vin_oem_resolver.decode_vehicle_identity", lambda *_a, **_k: identity)
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN, requested_part="передние колодки", live_vpic=False, live_partsapi_oem=True
    )
    assert [call["operation"] for call in calls] == ["vin_decode"]
    assert result["readiness"]["blocking_reasons"].count(reason) == 1
    assert result["readiness"]["ready_for_tecdoc_candidate_lookup"] is False
    assert result["article_candidates"] == []


@pytest.mark.parametrize(
    ("part", "candidate", "coordinates", "match"),
    [
        ({"intent_id": "rear_brake_pads"}, {"product_name": "Rear brake pads"}, {"axle": "rear"}, "matched"),
        (
            {"intent_id": "inner_cv_joint", "explicit_position_context": {"axle": "front", "side": "left"}},
            {"product_name": "Front left inner CV joint"},
            {"axle": "front", "side": "left", "inner_outer": "inner"},
            "matched",
        ),
        (
            {"intent_id": "outer_cv_joint", "raw": "передний правый ШРУС"},
            {"product_name": "Front right outer CV joint"},
            {"axle": "front", "side": "right", "inner_outer": "outer"},
            "matched",
        ),
        ({"raw": "термостат"}, {"product_name": "Thermostat"}, {}, "not_required"),
        (
            {"intent_id": "front_brake_pads"},
            {"product_name": "Front or rear brake pads", "fitment_evidence": []},
            {"axle": "front"},
            "ambiguous",
        ),
    ],
)
def test_article_ranking_checks_all_requested_coordinates(part, candidate, coordinates, match):
    ranked = _rank_article_candidate(candidate, part_profile=part, category="100470", index=1)
    assert ranked["requested_position_coordinates"] == coordinates
    assert ranked["position_match"] == match
    assert ("candidate_position_ambiguous" in ranked["blocking_reasons"]) is (match == "ambiguous")
    assert ranked["vin_fitment_confirmed"] is False
    assert ranked["oem_number_confirmed"] is False
    assert ranked["manual_review_required"] is True


def test_resolver_live_identity_keeps_catalog_plan_dry(monkeypatch):
    calls = _install_fakes(monkeypatch)
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_identity=True,
    )
    assert [(call["operation"], call["dry_run"]) for call in calls] == [("vin_decode", False), ("search_tree", True)]
    assert result["status"] == "ready_for_live_tecdoc_lookup"
    assert result["live_call_count"] == 1
    assert "run_live_tecdoc_search_tree" in {item["code"] for item in result["manual_actions"]}
    assert result["article_candidates"] == []


@pytest.mark.parametrize("verified", [False, True])
@pytest.mark.parametrize(
    ("fields", "conflict", "field_sources"),
    [
        ({"engine": "ENGINE-B"}, "engine", None),
        ({"engine": "ENGINE-A", "engine_code": "ENGINE-B"}, "engine", None),
        ({"market": "US"}, "market", None),
        ({"model_year_from": "2020", "model_year_to": "2024"}, "model_year", None),
        ({"model_year_from": 202001, "model_year_to": 202412}, "model_year", None),
        ({"model_year_from": "2020-01"}, "model_year", None),
        ({"model_year_to": "2009/12"}, "model_year", None),
        ({"engine": "engine.a", "market": "eu"}, None, None),
        ({"engine_code": "ENGINE-A", "engine": "marketing description"}, None, None),
        ({"model_year_from": "2010", "model_year_to": "2010"}, None, None),
        ({"model_year_from": "201001", "model_year_to": "2010/12"}, None, None),
        ({"model_year_from": None, "model_year_to": "2010"}, None, None),
        ({"model_year_from": "2010-01", "model_year_to": ""}, None, None),
        ({"model_year_from": "unparsed", "model_year_to": True}, None, None),
        ({}, None, None),
        ({"market": "US"}, "market", ("market", ["CRM context", "local WMI hint"])),
        ({"market": "Europe"}, None, ("market", ["local WMI hint"])),
        ({"engine_code": "ENGINE-A"}, None, ("engine", ["synthetic platform rule"])),
        ({"engine_code": "ENGINE-B"}, "engine", ("engine", ["CRM context", "synthetic platform rule"])),
        ({"engine_code": "ENGINE-B"}, "engine", ("engine", ["rule", ("NHTSA vPIC", "ENGINE-A")])),
        ({"engine_code": "ENGINE-A"}, None, ("engine", ["rule", ("NHTSA vPIC", "ENGINE-A")])),
        ({"engine_code": "ENGINE-A"}, "engine", ("engine", ["CRM context", ("NHTSA vPIC", "ENGINE-B")])),
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
def test_resolver_known_characteristics_guard_catalog(monkeypatch, verified, fields, conflict, field_sources):
    profile = {"make": "HONDA", "model": "Accord", "tecdoc_car_id": "9877", "vehicle_type": "PC", **fields}
    profile["identifier_matches_request"] = True if verified else None
    calls = _install_fakes(monkeypatch, profiles=[profile])
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
    monkeypatch.setattr("autostop_manager.vin_oem_resolver.decode_vehicle_identity", lambda *_a, **_k: identity)
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN, requested_part="передние колодки", live_vpic=False, live_partsapi_oem=True
    )
    agreement = result["identity"]["cross_source_agreement"]
    can_read = not conflict and bool(profile.get("make") and (profile.get("model") or profile.get("model_family")))
    assert agreement["identifier_matches_request"] is (True if verified else None)
    assert agreement["status"] == ("conflict" if conflict else "matched" if can_read else "partial_match")
    assert [item["field"] for item in agreement["conflicting_fields"]] == ([conflict] if conflict else [])
    if not fields and field_sources:
        assert "engine" not in agreement["matched_fields"]
        if conflict:
            assert agreement["conflicting_fields"][0] == {
                "field": "engine",
                "identity": ["ENGINE-A", "ENGINE-B"],
                "partsapi_value": None,
            }
    assert [call["operation"] for call in calls] == (
        ["vin_decode", "search_tree", "articles"] if can_read else ["vin_decode"]
    )
    assert result["article_candidate_count"] == int(can_read)
    assert result["readiness"]["ready_for_crm_writeback"] is False
    assert all(
        not item["vin_fitment_confirmed"] and not item["oem_number_confirmed"] and item["manual_review_required"]
        for item in result["article_candidates"]
    )


@pytest.mark.parametrize("field", ["engine", "transmission", "modification"])
def test_resolver_preserves_family_research_without_promoting_disputed_vehicle(monkeypatch, field):
    calls = _install_fakes(monkeypatch)
    identity = _medium_identity()
    identity["parts_lookup_readiness"].update(ready_for_family_lookup=True, ready_for_vehicle_lookup=False)
    identity["conflicts"] = [{"field": field, "severity": "high", "blocking_scopes": ["vehicle"]}]
    identity["field_statuses"] = {
        field: {"status": "disputed", "evidence_ids": ["source-a", "source-b"], "alternatives": ["A", "B"]}
    }
    identity["field_evidence"] = [{"field": field, "source": "source-a", "value": "A", "evidence_id": "source-a"}]
    identity["provenance"] = {field: {"evidence_ids": ["source-a", "source-b"]}}
    identity["missing_fields"] = [{"field": "production_date", "reason": "not_observed"}]
    identity["family_candidates"] = [{"make": "HONDA", "model": "Accord", "evidence_ids": ["family-a"]}]
    monkeypatch.setattr("autostop_manager.vin_oem_resolver.decode_vehicle_identity", lambda *_a, **_k: identity)
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN, requested_part="передние колодки", live_vpic=False, live_partsapi_oem=True
    )
    assert [call["operation"] for call in calls] == ["vin_decode"]
    assert result["readiness"]["ready_for_family_lookup"] is True
    assert result["readiness"]["ready_for_vehicle_lookup"] is False
    assert result["readiness"]["ready_for_tecdoc_candidate_lookup"] is False
    assert result["readiness"]["ready_for_crm_writeback"] is False
    assert "research_vehicle_family" in {action["code"] for action in result["manual_actions"]}
    for key in ["field_statuses", "field_evidence", "provenance", "missing_fields", "family_candidates", "conflicts"]:
        assert result["identity"][key] == identity[key]


def test_resolver_soft_scoped_disagreement_keeps_independent_medium_upgrade(monkeypatch):
    calls = _install_fakes(monkeypatch)
    identity = _medium_identity()
    identity["conflicts"] = [{"field": "production_year", "severity": "medium", "blocking_scopes": []}]
    identity["vehicle_profile"].update(model_year=2010, production_year=2009)
    monkeypatch.setattr("autostop_manager.vin_oem_resolver.decode_vehicle_identity", lambda *_a, **_k: identity)
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN, requested_part="передние колодки", live_vpic=False, live_partsapi_oem=True
    )
    assert [call["operation"] for call in calls] == ["vin_decode", "search_tree", "articles"]
    assert result["identity"]["cross_source_agreement"]["status"] == "matched"
    assert result["identity"]["vehicle_profile"]["model_year"] == 2010
    assert result["identity"]["vehicle_profile"]["production_year"] == 2009
    assert result["readiness"]["ready_for_crm_writeback"] is False


@pytest.mark.parametrize(
    ("profiles", "status"),
    [
        (
            [
                {
                    "make": "HONDA",
                    "model": "Accord",
                    "tecdoc_car_id": "1",
                    "vehicle_type": "PC",
                    "identifier_matches_request": False,
                }
            ],
            "identifier_mismatch",
        ),
        (
            [
                {
                    "make": "HONDA",
                    "model": "Accord",
                    "tecdoc_car_id": str(car),
                    "vehicle_type": "PC",
                    "identifier_matches_request": True,
                }
                for car in (1, 2)
            ],
            "ambiguous_vehicle_modification",
        ),
        (
            [
                {
                    "make": "HONDA",
                    "model": "Civic",
                    "tecdoc_car_id": "1",
                    "vehicle_type": "PC",
                    "identifier_matches_request": True,
                }
            ],
            "conflict",
        ),
    ],
)
def test_resolver_catalog_disagreement_revokes_all_exact_readiness(monkeypatch, profiles, status):
    calls = _install_fakes(monkeypatch, profiles=profiles)
    identity = _medium_identity()
    identity["confidence_label"] = "high"
    identity["parts_lookup_readiness"].update(
        ready_for_family_lookup=True,
        ready_for_vehicle_lookup=True,
        ready_for_oem_lookup=True,
        ready_for_oem_candidate_lookup=True,
    )
    monkeypatch.setattr("autostop_manager.vin_oem_resolver.decode_vehicle_identity", lambda *_a, **_k: identity)
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN, requested_part="передние колодки", live_vpic=False, live_partsapi_oem=True
    )
    assert [call["operation"] for call in calls] == ["vin_decode"]
    assert result["identity"]["cross_source_agreement"]["status"] == status
    assert result["readiness"]["ready_for_family_lookup"] is True
    assert result["readiness"]["ready_for_vehicle_lookup"] is False
    for flag in [
        "ready_for_vehicle_lookup",
        "ready_for_oem_lookup",
        "ready_for_oem_candidate_lookup",
        "ready_for_tecdoc_candidate_lookup",
        "ready_for_crm_writeback",
    ]:
        assert result["identity"][flag] is False
    assert result["tecdoc_vehicle"]["car_id"] is None


@pytest.mark.parametrize("input_error", [False, True])
def test_resolver_invalid_identity_does_not_reach_partsapi(monkeypatch, input_error):
    identity = _medium_identity()
    if input_error:
        identity.update(
            ok=False,
            status="invalid_input",
            errors=[{"code": "expected_string", "field": "model", "stage": "input_validation"}],
        )
    else:
        identity["identifier_validation"] = {"ok": False, "errors": [{"code": "forbidden_vin_characters"}]}
    monkeypatch.setattr("autostop_manager.vin_oem_resolver.decode_vehicle_identity", lambda *_a, **_k: identity)
    monkeypatch.setattr(
        "autostop_manager.vin_oem_resolver.partsapi_catalog_lookup",
        lambda **_k: pytest.fail("Invalid identity reached PartsAPI"),
    )
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN, requested_part="передние колодки", live_vpic=False, live_partsapi_oem=True
    )
    assert result["call_count"] == result["live_call_count"] == 0
    assert result["readiness"]["ready_for_vehicle_lookup"] is False
    assert result["readiness"]["ready_for_tecdoc_candidate_lookup"] is False
    assert result["readiness"]["ready_for_crm_writeback"] is False


@pytest.mark.parametrize("mismatch", ["identifier", "identifier_type", "model_year", "missing_binding"])
def test_private_resolver_rejects_unbound_predecoded_identity_without_catalog_calls(monkeypatch, mismatch):
    from autostop_manager.vin_oem_resolver import _resolve_vin_oem_parts
    from autostop_manager.vehicle_identity_inputs import validate_identity_input

    vin_a = "WAU" + "ZZZ4H" + "A" * 9
    vin_b = "WAU" + "ZZZ4H" + "B" * 9
    identity = _medium_identity()
    binding = validate_identity_input(vin_a, {"make": "HONDA", "model": "Accord", "model_year": 2010})
    kwargs = {"identifier": vin_a, "identifier_type": "vin", "model_year": 2010}
    if mismatch == "identifier":
        kwargs["identifier"] = vin_b
    elif mismatch == "identifier_type":
        kwargs["identifier_type"] = "frame_number"
    elif mismatch == "model_year":
        kwargs["model_year"] = 2020
    else:
        binding = None
    monkeypatch.setattr(
        "autostop_manager.vin_oem_resolver.decode_vehicle_identity",
        lambda *_a, **_k: pytest.fail("Unbound identity was redecoded"),
    )
    monkeypatch.setattr(
        "autostop_manager.vin_oem_resolver.partsapi_catalog_lookup",
        lambda **_k: pytest.fail("Unbound identity reached PartsAPI"),
    )
    result = _resolve_vin_oem_parts(
        requested_part="передние колодки",
        _decoded_identity=identity,
        _decoded_identity_input=binding,
        live_partsapi_oem=True,
        **kwargs,
    )
    assert result["identity"]["ok"] is False
    assert result["identity"]["errors"][0]["code"] == "predecoded_identity_input_mismatch"
    assert result["call_count"] == result["live_call_count"] == 0
    assert result["identity"]["ready_for_tecdoc_candidate_lookup"] is False
    assert result["readiness"]["ready_for_crm_writeback"] is False
    assert vin_a not in json.dumps(result) and vin_b not in json.dumps(result)
