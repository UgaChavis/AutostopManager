"""Keep each part-market assessment coherent across UTC midnight.

Only the module clock is replaced; validation, evidence binding, region parsing
and segmentation remain real. Four synthetic domains establish independent offers.
Crossing cases require a result consistent with one UTC day during the call.
Frozen-day and threshold cases preserve the existing 7/30-day policy.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from autostop_manager import part_market_assessment as market


_BEFORE = datetime(2026, 10, 16, 23, 59, 59, 999999, tzinfo=UTC)
_AFTER = _BEFORE + timedelta(microseconds=1)
_PRICES = (1000, 2000, 3000, 10000)


class _Clock:
    def __init__(self, first: datetime, *, after_calls: int | None = None):
        self.first = first
        self.after_calls = after_calls
        self.calls = 0

    def now(self, tz=None):
        self.calls += 1
        moment = _AFTER if self.after_calls is not None and self.calls > self.after_calls else self.first
        return moment.replace(tzinfo=None) if tz is None else moment.astimezone(tz)


def _offers(*, observed_age: int = 0, published_age: int = 30):
    observed = (_BEFORE.date() - timedelta(days=observed_age)).isoformat()
    published = (_BEFORE.date() - timedelta(days=published_age)).isoformat()
    return [
        {
            "article": "1712024",
            "brand": "Ford",
            "price_rub": price,
            "source": f"Synthetic market {index}",
            "source_excerpt": f"Ford 1712024: новый, цена {price} ₽",
            "url": f"https://coherence-{index}.example/offer/1712024",
            "observed_at": observed,
            "published_at": published,
            "offer_kind": "original",
            "condition": "new",
            "region": "Красноярск, Россия",
        }
        for index, price in enumerate(_PRICES)
    ]


def _assess(monkeypatch, offers, clock):
    original = deepcopy(offers)
    with monkeypatch.context() as patch:
        patch.setattr(market, "datetime", clock)
        result = market.assess_part_market(article="1712024", brand="Ford", observations=offers)
    assert offers == original
    assert result["ok"] is True
    assert result["accepted_offer_count"] == 4
    assert result["rejected_observations"] == []
    return result


def _segment(result):
    return next(
        segment
        for segment in result["segments"]
        if (
            segment["offer_kind"],
            segment["condition"],
            segment["region_scope"],
        )
        == ("original", "new", "krasnoyarsk")
    )


@pytest.mark.parametrize("day_offset", [0, 1], ids=["day-D", "day-D-plus-1"])
def test_frozen_day_produces_coherent_public_assessment(monkeypatch, day_offset):
    now = _BEFORE if day_offset == 0 else _AFTER
    result = _assess(monkeypatch, _offers(), _Clock(now))
    segment = _segment(result)
    assert {offer["observed_age_days"] for offer in segment["offers"]} == {day_offset}
    assert {offer["page_age_days"] for offer in segment["offers"]} == {30 + day_offset}
    assert {offer["observation_freshness"] for offer in segment["offers"]} == {"recent"}
    assert segment["independent_offer_count"] == 4
    assert segment["median_input_offer_count"] == (4 if day_offset == 0 else 0)
    assert segment["median_price_rub"] == (2500 if day_offset == 0 else None)
    assert segment["median_available"] is (day_offset == 0)
    assert segment["median_confidence"] == ("medium" if day_offset == 0 else "unavailable")
    assert segment["median_exclusion_reasons"] == ({} if day_offset == 0 else {"stale_published_page": 4})
    assert result["status"] == ("assessed" if day_offset == 0 else "insufficient_independent_offers")


@pytest.mark.parametrize(
    ("observed_age", "published_age", "observation_freshness", "price_freshness", "confidence"),
    [
        (7, 30, "recent", "fresh", "medium"),
        (8, 30, "stale", "fresh", "low"),
        (7, 31, "recent", "stale", "unavailable"),
    ],
    ids=["inclusive-7-and-30", "observation-8-keeps-median", "publication-31-excludes-median"],
)
def test_documented_age_thresholds(
    monkeypatch, observed_age, published_age, observation_freshness, price_freshness, confidence
):
    result = _assess(
        monkeypatch,
        _offers(observed_age=observed_age, published_age=published_age),
        _Clock(_BEFORE),
    )
    segment = _segment(result)
    assert {offer["observed_age_days"] for offer in segment["offers"]} == {observed_age}
    assert {offer["page_age_days"] for offer in segment["offers"]} == {published_age}
    assert {offer["observation_freshness"] for offer in segment["offers"]} == {observation_freshness}
    assert {offer["price_freshness"] for offer in segment["offers"]} == {price_freshness}
    assert segment["median_confidence"] == confidence
    assert segment["median_price_rub"] == (None if published_age > 30 else 2500)
    assert segment["median_input_offer_count"] == (0 if published_age > 30 else 4)


@pytest.mark.parametrize("after_calls", [12, 3], ids=["between-rows-three-and-four", "within-first-row"])
def test_assessment_matches_a_single_utc_day(monkeypatch, after_calls):
    offers = _offers()
    before = _assess(monkeypatch, offers, _Clock(_BEFORE))
    after = _assess(monkeypatch, offers, _Clock(_AFTER))
    assert _segment(before)["median_price_rub"] == 2500
    assert _segment(after)["median_price_rub"] is None

    # Baseline uses four clock reads per dated row. After 12 old readings the
    # fourth row alone crosses the 30-day cutoff. After 3, the first row itself
    # mixes days; this schedule also catches a weaker once-per-row clock change.
    # A single clock snapshot may use fewer calls and legitimately stay on D.
    result = _assess(monkeypatch, offers, _Clock(_BEFORE, after_calls=after_calls))
    segment = _segment(result)
    evidence = {
        "after_clock_calls": after_calls,
        "median": segment["median_price_rub"],
        "median_inputs": segment["median_input_offer_count"],
        "observed_ages": [offer["observed_age_days"] for offer in segment["offers"]],
        "published_ages": [offer["page_age_days"] for offer in segment["offers"]],
    }
    assert result == before or result == after, evidence
