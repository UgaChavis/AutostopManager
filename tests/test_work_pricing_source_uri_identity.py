"""Source URI identity survives equal-value deduplication and scope conflicts."""

from __future__ import annotations

from copy import deepcopy
import os

import pytest

from autostop_manager import work_pricing as pricing, work_pricing_research as research
from autostop_manager.work_operation_matching import OperationMatcher

OPERATION = "замена масла"
ROUTES = ["manual", "ready", "combined"]
URI_PAIRS = [
    ("https://a.example/Labor", "https://a.example/labor"),
    ("https://a.example/Labor?A=1", "https://a.example/Labor?a=1"),
    ("https://a.example/Labor#A", "https://a.example/Labor#a"),
    ("HTTPS://A.EXAMPLE/Labor", "https://a.example/Labor"),
    ("custom+v1://service/Labor", "custom+v1://service/labor"),
]
URI_IDS = ["path", "query", "fragment", "scheme-host-case", "custom-scheme"]


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, tmp_path):
    def denied(*_args, **_kwargs):
        raise AssertionError("source identity tests forbid private experience or acquisition")

    monkeypatch.setattr(pricing, "_load_labor_experience", denied)
    monkeypatch.setattr(research, "urlopen", denied)
    monkeypatch.setattr(research, "_ddg_search", denied)
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", os.devnull)
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "manager.sqlite3"))
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "j1"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "vin"))


def time_row(source, **changes):
    return {
        "source": source,
        "operation_name": OPERATION,
        "hours": 2,
        "public_source": True,
        "official": False,
        "confidence": "high",
        "captured_at": "2026-10-01",
        **changes,
    }


def quote(source, price=1000, **changes):
    return {
        "source": source,
        "operation_name": OPERATION,
        "city": "Красноярск",
        "price_rub": price,
        "includes_parts": False,
        "labor_only": True,
        "confidence": "high",
        "captured_at": "2026-10-01",
        **changes,
    }


def estimate(*, times=(), quotes=(), route="manual"):
    times, quotes = list(times), list(quotes)
    before = deepcopy((times, quotes))

    def split(rows):
        return (rows, []) if route == "manual" else ([], rows) if route == "ready" else (rows[:1], rows[1:])

    manual_times, ready_times = split(times)
    manual_quotes, ready_quotes = split(quotes)
    result = pricing.estimate_repair_work_cost(
        make="DEMO",
        work_items=[OPERATION],
        quotes_json={"quotes": manual_quotes, "labor_time_sample": manual_times},
        price_evidence=(
            {"work_items": [OPERATION], "observations": ready_quotes, "labor": ready_times}
            if route != "manual"
            else None
        ),
        auto_research=False,
        use_internal_experience=False,
    )
    assert result["ok"] is True
    assert (times, quotes) == before
    return result


@pytest.mark.parametrize("sources", URI_PAIRS, ids=URI_IDS)
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_equal_values_keep_both_uri_observations_in_all_four_dedup_paths(sources, reverse):
    times = [time_row(source) for source in sources]
    quotes = [quote(source) for source in sources]
    if reverse:
        times.reverse()
        quotes.reverse()
    before = deepcopy((times, quotes))
    assert pricing._source_count(times) == 2
    samples = [
        pricing._normalize_labor_time_sample(times),
        research._dedupe_rows(times, ("source", "operation_name", "hours", "range_hours")),
        pricing._normalize_quote_sample(quotes, OperationMatcher()),
        research._dedupe_quote_rows(quotes),
    ]
    for sample in samples:
        assert len(sample) == 2
        assert [row["source"] for row in sample] == [row["source"] for row in times]
    assert (times, quotes) == before


@pytest.mark.parametrize("sources", URI_PAIRS[:2], ids=URI_IDS[:2])
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_equal_hours_from_distinct_uri_sources_retain_high_confidence(sources, route, reverse):
    rows = [time_row(source) for source in sources]
    if reverse:
        rows.reverse()
    result = estimate(times=rows, route=route)
    analysis = result["operation_estimates"][0]["labor_time_analysis"]
    assert (analysis["valid_count"], analysis["source_count"], analysis["confidence"]) == (2, 2, "high")
    assert analysis["average_hours"] == 2
    assert {row["source"] for row in analysis["sample"]} == set(sources)


@pytest.mark.parametrize("sources", URI_PAIRS[:2], ids=URI_IDS[:2])
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_equal_uri_quotes_do_not_remove_a_supported_market_price(sources, route, reverse):
    rows = [quote(sources[0]), quote(sources[1]), quote("independent.example", 1300)]
    if reverse:
        rows.reverse()
    result = estimate(quotes=rows, route=route)
    operation = result["operation_estimates"][0]
    assert (operation["sample"]["valid_count"], operation["sample"]["source_count"]) == (3, 3)
    assert operation["market_average_rub"] == 1100
    assert operation["pricing_method"] == "krasnoyarsk_market_mean"
    assert result["autostop_price_rub"] == 1600


@pytest.mark.parametrize("relation", ["different-URI", "same-URI", "label-alias"])
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_parts_veto_stays_with_its_own_source_identity(relation, route, reverse):
    first, second = URI_PAIRS[0]
    if relation == "same-URI":
        second = first
    elif relation == "label-alias":
        first, second = "Garage Alpha", "GARAGE ALPHA"
    rows = [
        quote(first),
        quote(second, includes_parts=True, labor_only=False),
        quote("independent.example", 1100),
        quote("independent.example", 1200),
    ]
    if reverse:
        rows.reverse()
    result = estimate(quotes=rows, route=route)
    operation = result["operation_estimates"][0]
    sample = operation["sample"]
    distinct = relation == "different-URI"
    assert sample["raw_matched_count"] == (3 if distinct else 2)
    assert sample["valid_count"] == (3 if distinct else 0)
    assert len(sample["invalid_quotes"]) == 1
    invalid = sample["invalid_quotes"][0]
    assert "includes_parts" in invalid["reasons"]
    if distinct:
        assert invalid["source"] == second
        assert operation["market_average_rub"] == 1100
        assert result["autostop_price_rub"] == 1600
    else:
        assert result["autostop_price_rub"] is None


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_equal_structured_lineages_preserve_raw_provenance_and_two_sources(route, reverse):
    sources = [
        {
            "provider": "synthetic",
            "primary_lineage": source,
            "locator": "https://evidence.example/same",
            "fetched_at": "2026-10-01T00:00:00Z",
        }
        for source in URI_PAIRS[0]
    ]
    rows = [time_row(source) for source in sources]
    if reverse:
        rows.reverse()
        sources.reverse()
    result = estimate(times=rows, route=route)
    analysis = result["operation_estimates"][0]["labor_time_analysis"]
    assert (analysis["valid_count"], analysis["source_count"], analysis["confidence"]) == (2, 2, "high")
    assert [row["evidence_source"] for row in analysis["sample"]] == sources


@pytest.mark.parametrize(
    "alias,normalized_quote_count",
    [(" GARAGE ALPHA ", 1), ("Garage\t  Alpha", 2), ("Garage\u00a0Alpha", 2)],
    ids=["outer-space", "internal-spaces", "unicode-space"],
)
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_ordinary_label_whitespace_keeps_each_existing_context_policy(alias, normalized_quote_count, reverse):
    sources = ["Garage Alpha", alias]
    if reverse:
        sources.reverse()
    times, quotes = [time_row(source) for source in sources], [quote(source) for source in sources]
    assert pricing._source_count(times) == 1
    assert len(pricing._normalize_labor_time_sample(times)) == 1
    assert len(research._dedupe_rows(times, ("source", "operation_name", "hours"))) == 2
    assert len(research._dedupe_quote_rows(quotes)) == 2
    assert len(pricing._normalize_quote_sample(quotes, OperationMatcher())) == normalized_quote_count


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_exact_uri_replay_does_not_inflate_either_sample(route, reverse):
    first = time_row(URI_PAIRS[0][0])
    times = [first, {**first, "captured_at": "2026-10-02"}]
    first_quote = quote(URI_PAIRS[0][0])
    quotes = [
        first_quote,
        {**first_quote, "captured_at": "2026-10-02"},
        quote("b.example", 1100),
        quote("c.example", 1200),
    ]
    if reverse:
        times.reverse()
        quotes.reverse()
    result = estimate(times=times, quotes=quotes, route=route)
    operation = result["operation_estimates"][0]
    assert (operation["labor_time_analysis"]["valid_count"], operation["labor_time_analysis"]["source_count"]) == (1, 1)
    assert (operation["sample"]["valid_count"], operation["sample"]["source_count"]) == (3, 3)


@pytest.mark.parametrize("change", ["value", "locator", "city"])
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_other_observation_identity_fields_still_keep_separate_rows(change, reverse):
    first_time = time_row(URI_PAIRS[0][0], city="Красноярск", url="https://evidence.example/Page")
    first_quote = quote(URI_PAIRS[0][0], url="https://evidence.example/Page")
    other_time, other_quote = dict(first_time), dict(first_quote)
    if change == "value":
        other_time["hours"], other_quote["price_rub"] = 3, 1100
    elif change == "locator":
        other_time["url"] = other_quote["url"] = "https://evidence.example/page"
    else:
        other_time["city"] = other_quote["city"] = "Санкт-Петербург"
    times, quotes = [first_time, other_time], [first_quote, other_quote]
    if reverse:
        times.reverse()
        quotes.reverse()
    assert pricing._source_count(times) == 1
    assert len(pricing._normalize_labor_time_sample(times)) == 2
    assert len(research._dedupe_rows(times, ("source", "operation_name", "hours"))) == 2
    assert len(pricing._normalize_quote_sample(quotes, OperationMatcher())) == 2
    assert len(research._dedupe_quote_rows(quotes)) == 2
