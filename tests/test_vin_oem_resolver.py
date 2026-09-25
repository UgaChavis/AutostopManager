from __future__ import annotations

import json

import pytest

from autostop_manager.vin_oem_resolver import (
    _assess_partsapi_identity_agreement,
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
        return {
            "ok": not failed,
            "provider": "partsapi_ru",
            "operation": operation,
            "partsapi_method": {"vin_decode": "VINdecode", "search_tree": "getSearchTree", "articles": "getArticles"}[
                operation
            ],
            "dry_run": dry_run,
            "outcome": "upstream_failure" if failed else "ready" if dry_run else "success",
            "failure_class": "upstream_failure" if failed else None,
            "retryable": failed,
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


def test_resolver_reports_provider_failure_without_claiming_no_parts(monkeypatch):
    calls = _install_fakes(monkeypatch, failures={"search_tree"})
    result = resolve_vin_oem_parts(
        identifier=SYNTHETIC_VIN,
        requested_part="передние колодки",
        live_vpic=False,
        live_partsapi_oem=True,
    )
    assert [call["operation"] for call in calls] == ["vin_decode", "search_tree"]
    assert result["status"] == "tecdoc_lookup_provider_failed"
    assert result["tecdoc_lookup_outcome"]["outcome"] == "not_attempted"
    assert any(item["stage"] == "partsapi_tecdoc_lookup" for item in result["blockers"])


@pytest.mark.parametrize("broken_operation", ["search_tree", "articles"])
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
