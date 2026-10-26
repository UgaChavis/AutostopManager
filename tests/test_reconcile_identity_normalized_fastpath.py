"""Keep raw all-pairs semantics while avoiding repeated equal-value normalization."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from itertools import permutations
import socket
import sqlite3

import pytest

from autostop_manager import automotive_identity as identity
from autostop_manager import vehicle_identity as comparison
from autostop_manager.automotive_contracts import binding, result


@pytest.fixture(autouse=True)
def pure_reconciliation_only(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Pure reconciliation cannot access network or SQLite")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)


def _ready_rows(profiles, *, identifier="A" * 17, identifier_type="vin", level="candidate", variants=False):
    rows = []
    for index, profile in enumerate(profiles):
        origin = {
            "provider": "synthetic_supplied_catalog",
            "primary_lineage": "synthetic_lineage_" + str(index % 2),
            "method": "supplied_result",
            "fetched_at": "2026-01-01T00:00:00Z",
            "locator": "synthetic:provided-result",
            "scope": "vehicle",
            "strength": level,
            "independent": False,
        }
        origins = [{**origin, "field": field, "value": value} for field, value in profile.items()]
        data = {
            "input_binding": binding(identifier, identifier_type),
            "vehicle_profile": dict(profile),
            "field_statuses": {field: {"status": level} for field in profile},
            "field_evidence": origins,
        }
        if variants:
            data["variants"] = [{"profile": dict(profile), "scope": "family", "field_evidence": origins}]
            data["model_year_candidates"] = [2020]
        rows.append(result("synthetic_supplied_result", "success", data, evidence=[origin]))
    return rows


def _same_response(left, right):
    # Keep failure logs free of identifier digests and full reusable profiles.
    same = left == right
    assert same, "Fast path changed the reconciliation envelope"


def _without_fastpath(monkeypatch, identifier, rows, **options):
    # Empty local precomputed keys force every pair through the original raw
    # comparator. Its own vehicle_identity normalizer remains untouched.
    with monkeypatch.context() as patch:
        patch.setattr(identity, "_normalized_identity_value", lambda *_args: "", raising=False)
        return identity.reconcile_vehicle_identity(identifier, rows, **options)


def test_reconciliation_normalizes_each_observation_once_before_equal_pair_checks(monkeypatch):
    profiles = [
        {
            "make": "Demo Motors",
            "model": "Demo-Model 7" if index % 2 else "demo model-7",
            "engine": "AB12-C" if index % 2 else "ab12.c",
            "transmission": "6AT",
            "market": "Europe",
            "production_date": "2020-01-02",
        }
        for index in range(12)
    ]
    rows = _ready_rows(profiles)
    normalizations, comparisons = Counter(), Counter()
    normalize = comparison._normalized_identity_value
    agree = identity.identity_values_agree

    def normalized(field, value):
        normalizations[field] += 1
        return normalize(field, value)

    def compared(field, left, right):
        comparisons[field] += 1
        return agree(field, left, right)

    monkeypatch.setattr(identity, "_normalized_identity_value", normalized, raising=False)
    monkeypatch.setattr(identity, "identity_values_agree", compared)

    response = identity.reconcile_vehicle_identity("A" * 17, rows, identifier_type="vin")

    assert response["outcome"] == "success" and not response["conflicts"]
    assert comparisons == Counter()
    assert normalizations == Counter(dict.fromkeys(profiles[0], 12))
    assert len(response["data"]["variants"]) == 12
    assert sum(map(len, response["data"]["provenance"].values())) == 72
    assert set(response["data"]["field_statuses"].values()) == {"candidate"}


@pytest.mark.parametrize("models", list(permutations(("DemoModel 7", "Demo Model7", "Demo Model7 Extra"))))
def test_reconciliation_keeps_nontransitive_raw_model_pairs_in_every_order(models):
    first, same_key, refinement = "DemoModel 7", "Demo Model7", "Demo Model7 Extra"
    assert comparison.identity_values_agree("model", first, same_key)
    assert comparison.identity_values_agree("model", same_key, refinement)
    assert not comparison.identity_values_agree("model", first, refinement)

    response = identity.reconcile_vehicle_identity("A" * 17, _ready_rows([{"model": value} for value in models]))

    assert response["data"]["field_statuses"]["model"] == "disputed"
    assert "model" not in response["data"]["vehicle_profile"]
    assert any(
        item["field"] == "model" and item["code"] == "conflicting_vehicle_fields" for item in response["conflicts"]
    )
    assert len(response["data"]["provenance"]["model"]) == 3


@pytest.mark.parametrize("value", ["!!!", "東京"])
def test_reconciliation_equal_empty_normalized_values_stay_disputed(value):
    assert comparison._normalized_identity_value("model", value) == ""

    response = identity.reconcile_vehicle_identity("A" * 17, _ready_rows([{"model": value}, {"model": value}]))

    assert response["data"]["field_statuses"]["model"] == "disputed"
    assert "model" not in response["data"]["vehicle_profile"]


@pytest.mark.parametrize(
    "field,left,right",
    [
        ("engine", "AB12-C 1.6", "ab12c"),
        ("transmission", "6-speed automatic", "6AT"),
        ("market", "Europe", "EU"),
    ],
)
def test_reconciliation_unequal_keys_use_the_original_raw_comparator(monkeypatch, field, left, right):
    calls = []
    agree = identity.identity_values_agree

    def observed(name, raw_left, raw_right):
        calls.append((name, raw_left, raw_right))
        return agree(name, raw_left, raw_right)

    monkeypatch.setattr(identity, "identity_values_agree", observed)

    response = identity.reconcile_vehicle_identity("A" * 17, _ready_rows([{field: left}, {field: right}]))

    assert calls == [(field, left, right)]
    assert response["data"]["field_statuses"][field] == "candidate"
    assert response["data"]["vehicle_profile"][field] == left


_SEMANTIC_PROFILES = [
    pytest.param([{"model": "Demo-Model 7"}, {"model": "demo model-7"}], id="case-punctuation"),
    pytest.param([{"model": "Straße"}, {"model": "STRASSE"}], id="unicode-casefold"),
    pytest.param([{"model": "Ёлка-7"}, {"model": "ёлка 7"}], id="cyrillic"),
    pytest.param([{"model": "!!!"}, {"model": "!!!"}], id="same-empty-key"),
    pytest.param([{"model": "!!!"}, {"model": "---"}], id="different-empty-keys"),
    pytest.param([{"model": "東京"}, {"model": "東京"}], id="unicode-empty-keys"),
    pytest.param([{"model": "!!!"}], id="single-empty-key"),
    pytest.param([{"model": "Demo Model 7"}, {"model": "Demo Model 8"}], id="model-conflict"),
    pytest.param([{"engine": "AB12-C 1.6"}, {"engine": "ab12c"}], id="raw-engine-fallback"),
    pytest.param([{"transmission": "6-speed automatic"}, {"transmission": "6AT"}], id="raw-gearbox-fallback"),
    pytest.param([{"market": "Europe"}, {"market": "EU"}], id="raw-market-fallback"),
    pytest.param([{"options": ["AB"]}, {"options": ["A", "B"]}], id="existing-options-comparison"),
]


@pytest.mark.parametrize("profiles", _SEMANTIC_PROFILES)
@pytest.mark.parametrize("detail", ["full", "summary"])
def test_reconciliation_matches_raw_all_pairs_counterfactual(monkeypatch, profiles, detail):
    rows = _ready_rows(profiles, variants=True)
    before = deepcopy(rows)

    expected = _without_fastpath(monkeypatch, "A" * 17, rows, detail=detail)
    actual = identity.reconcile_vehicle_identity("A" * 17, rows, detail=detail)

    _same_response(actual, expected)
    assert rows == before
    assert actual["execution"]["network_calls"] == 0


@pytest.mark.parametrize("level", ["candidate", "observed", "supported"])
@pytest.mark.parametrize("identifier_type", ["vin", "frame_number"])
def test_reconciliation_full_summary_and_reuse_preserve_levels_and_origins(monkeypatch, level, identifier_type):
    identifier = "A" * 17 if identifier_type == "vin" else "DEMO-000001"
    profiles = [{"make": "Demo Motors", "model": value} for value in ("Demo-Model 7", "demo model-7")]
    rows = _ready_rows(profiles, identifier=identifier, identifier_type=identifier_type, level=level, variants=True)
    options = {"identifier_type": identifier_type}
    before = deepcopy(rows)

    full = identity.reconcile_vehicle_identity(identifier, rows, **options)
    summary = identity.reconcile_vehicle_identity(identifier, rows, detail="summary", **options)
    reused = identity.reconcile_vehicle_identity(identifier, [summary], **options)
    expected_full = _without_fastpath(monkeypatch, identifier, rows, **options)
    expected_summary = _without_fastpath(monkeypatch, identifier, rows, detail="summary", **options)
    expected_reused = _without_fastpath(monkeypatch, identifier, [expected_summary], **options)

    _same_response(full, expected_full)
    _same_response(summary, expected_summary)
    _same_response(reused, expected_reused)
    assert set(full["data"]["field_statuses"].values()) == {level}
    assert summary["data"]["provenance"] == full["data"]["provenance"]
    assert summary["data"]["model_year_candidates"] == [2020]
    assert len(full["data"]["variants"]) == len(summary["data"]["variants"]) == 4
    assert not full["data"]["parts_lookup_readiness"]["exact_applicability_confirmed"]
    assert rows == before
