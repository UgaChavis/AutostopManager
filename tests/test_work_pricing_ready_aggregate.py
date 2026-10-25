from __future__ import annotations

from copy import deepcopy

import pytest

from autostop_manager import work_pricing as pricing

OPERATION = "замена тестовой детали"
ERROR = {"ok": False, "error": "invalid_ready_price_evidence", "execution": {"network_calls": 0}}


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("ready evidence must not acquire public or private data")

    monkeypatch.setattr(pricing, "get_db_path", denied)
    monkeypatch.setattr(pricing, "_load_labor_experience", denied)
    monkeypatch.setattr(pricing, "collect_public_work_pricing_research", denied)


def baseline(**values):
    return {"operation_name": OPERATION, "sample_count": 3, **values}


def estimate(aggregate, *, envelope=False, observations=(), labor=(), **kwargs):
    ready = {
        "work_items": [OPERATION],
        "vehicle_context": {"make": "DEMO"},
        "observations": list(observations),
        "labor": list(labor),
        "provided_aggregate": aggregate,
    }
    if envelope:
        ready = {"ok": True, "outcome": "success", "data": ready}
    before = deepcopy(ready)
    try:
        # Keep real acquisition defaults: bound ready input must suppress both.
        return pricing.estimate_repair_work_cost(make="DEMO", work_items=[OPERATION], price_evidence=ready, **kwargs)
    finally:
        assert ready == before


def public_quotes():
    return [
        {
            "source": f"demo-{index}.example",
            "operation_name": OPERATION,
            "city": "Красноярск",
            "price_rub": 1000,
            "labor_only": True,
            "includes_parts": False,
        }
        for index in range(3)
    ]


@pytest.mark.parametrize("envelope", [False, True], ids=["direct", "envelope"])
@pytest.mark.parametrize("count", ["invalid-demo-count", [1], {"demo": 1}, float("inf")])
def test_unusable_ready_count_returns_existing_error_and_next_call_recovers(count, envelope):
    assert estimate({"labor_baselines": [baseline(sample_count=count)]}, envelope=envelope) == ERROR
    healthy = estimate({"labor_baselines": [baseline(sample_count=1)]})
    assert healthy["ok"] is True
    assert healthy["operation_estimates"][0]["recommendation_basis"] == "insufficient_evidence"


@pytest.mark.parametrize("value", [1, 1.5, True])
def test_noniterable_baselines_return_existing_error(value):
    assert estimate({"labor_baselines": value}) == ERROR


@pytest.mark.parametrize(
    "field,value",
    [
        ("recommended_anchor_rub", "invalid-demo-anchor"),
        ("recommended_anchor_rub", []),
        ("recommended_anchor_rub", {"demo": 1}),
        ("recommended_anchor_rub", 10**400),
        ("p25_rub", "invalid-demo-range"),
        ("p75_rub", {"demo": 1}),
    ],
    ids=["anchor-text", "anchor-list", "anchor-object", "anchor-overflow", "lower-text", "upper-object"],
)
def test_later_consumed_anchor_and_range_failures_are_structured(field, value):
    row = baseline(recommended_anchor_rub=1000, p25_rub=900, p75_rub=1100)
    row[field] = value
    assert estimate({"labor_baselines": [row]}) == ERROR


def test_weighted_market_branch_also_handles_invalid_internal_range():
    row = baseline(recommended_anchor_rub=1000, p25_rub=[], p75_rub=1100)
    assert estimate({"labor_baselines": [row]}, observations=public_quotes()) == ERROR


@pytest.mark.parametrize("count", [None, False, True, 0, -1, "3", 3.9, [], {}])
def test_successful_count_coercion_and_raw_values_are_preserved(count):
    row = baseline(sample_count=count, recommended_anchor_rub="1000", p25_rub="900", p75_rub="1100")
    actual = estimate({"labor_baselines": [row]})
    operation = actual["operation_estimates"][0]
    assert actual["ok"] is True
    assert operation["internal_experience"]["selected"] == {**row, "match_score": 1.0}
    if int(count or 0) >= 3:
        assert operation["recommendation_basis"] == "internal_experience_provisional"
        assert operation["recommended_price_rub"] == 1000
        assert operation["recommended_range_rub"] == [900, 1100]
    else:
        assert operation["recommendation_basis"] == "insufficient_evidence"
        assert operation["recommended_price_rub"] is None


@pytest.mark.parametrize(
    "aggregate",
    [{}, {"count": 3}, {"labor_baselines": None}, {"labor_baselines": 0}, {"labor_baselines": "opaque"}],
)
def test_opaque_or_unused_aggregate_data_remains_compatible(aggregate):
    actual = estimate(aggregate)
    assert actual["ok"] is True
    assert actual["operation_estimates"][0]["internal_experience"]["matches"] == []


def test_matching_rank_uses_original_integer_coercion_without_rewriting_rows():
    rows = [baseline(sample_count="3"), baseline(sample_count=5.9), baseline(sample_count=False)]
    actual = estimate({"labor_baselines": [None, "opaque", *rows]})
    matches = actual["operation_estimates"][0]["internal_experience"]["matches"]
    assert matches == [{**rows[index], "match_score": 1.0} for index in (1, 0, 2)]


def test_unmatched_bad_count_and_unused_bad_anchor_are_not_eagerly_rejected():
    rows = [
        {"operation_name": "иная несвязанная операция", "sample_count": "invalid-demo-count"},
        baseline(sample_count=1, recommended_anchor_rub="unused-demo-anchor", p25_rub="unused-demo-range"),
    ]
    actual = estimate({"labor_baselines": rows})
    assert actual["ok"] is True
    assert actual["operation_estimates"][0]["recommendation_basis"] == "insufficient_evidence"


def test_falsy_internal_range_still_falls_back_to_anchor():
    actual = estimate({"labor_baselines": [baseline(recommended_anchor_rub=1000, p25_rub=[], p75_rub={})]})
    assert actual["operation_estimates"][0]["recommended_range_rub"] == [1000, 1000]


def test_valid_public_market_and_internal_evidence_keep_weighted_recommendation():
    actual = estimate(
        {"labor_baselines": [baseline(recommended_anchor_rub="1000", p25_rub="900", p75_rub="1100")]},
        observations=public_quotes(),
    )
    operation = actual["operation_estimates"][0]
    assert actual["ok"] is True
    assert operation["recommendation_basis"] == "weighted_public_market_and_internal_experience"
    assert operation["recommended_price_rub"] == 1300
    assert operation["recommended_range_rub"] == [900, 1500]


def test_hourly_fallback_does_not_consume_internal_price_or_range():
    actual = estimate(
        {"labor_baselines": [baseline(recommended_anchor_rub="unused-demo-anchor", p25_rub="unused-demo-range")]},
        labor=[{"source": "demo-time.example", "operation_name": OPERATION, "hours": 1, "public_source": True}],
    )
    operation = actual["operation_estimates"][0]
    assert actual["ok"] is True
    assert operation["recommendation_basis"] == "public_labor_hours_exclusive"
    assert operation["recommended_price_rub"] == 4000
    assert operation["internal_experience"]["applied_to_price"] is False


def test_explicit_experience_still_takes_precedence_over_ready_aggregate():
    actual = estimate(
        {"labor_baselines": [baseline(sample_count="invalid-demo-count")]},
        internal_experience_json={"labor_baselines": [baseline(recommended_anchor_rub=1000)]},
    )
    assert actual["ok"] is True
    assert actual["operation_estimates"][0]["recommended_price_rub"] == 1000


def test_empty_explicit_experience_still_overrides_invalid_ready_aggregate():
    actual = estimate({"labor_baselines": [baseline(sample_count="invalid-demo-count")]}, internal_experience_json={})
    assert actual["ok"] is True
    assert actual["operation_estimates"][0]["internal_experience"]["matches"] == []


def test_disabling_private_experience_still_consumes_explicit_ready_aggregate():
    assert (
        estimate(
            {"labor_baselines": [baseline(sample_count="invalid-demo-count")]},
            auto_research=False,
            use_internal_experience=False,
        )
        == ERROR
    )


def test_other_experience_source_errors_are_not_reclassified(monkeypatch):
    def broken(**_kwargs):
        raise ValueError("synthetic non-aggregate failure")

    monkeypatch.setattr(pricing, "_attach_internal_experience", broken)
    with pytest.raises(ValueError, match="synthetic non-aggregate failure"):
        estimate(None)


def test_unrelated_internal_failure_is_not_disguised_as_invalid_ready_input(monkeypatch):
    def broken(**_kwargs):
        raise RuntimeError("synthetic implementation failure")

    monkeypatch.setattr(pricing, "_attach_internal_experience", broken)
    with pytest.raises(RuntimeError, match="synthetic implementation failure"):
        estimate({"labor_baselines": [baseline()]})
