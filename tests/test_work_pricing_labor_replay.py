from __future__ import annotations

from copy import deepcopy

import pytest

from autostop_manager import automotive_labor as labor, work_pricing as pricing, work_pricing_research as research

OPERATION = "замена масла"


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, tmp_path):
    def denied(*_args, **_kwargs):
        raise AssertionError("implicit private or network read forbidden")

    monkeypatch.setattr(pricing, "_load_labor_experience", denied)
    monkeypatch.setattr(research, "urlopen", denied)
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "manager.sqlite3"))
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "j1"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "vin"))


def observation(source="a.example", hours=1, **kwargs):
    return {
        "source": source,
        "operation_name": OPERATION,
        "hours": hours,
        "public_source": True,
        "official": False,
        "confidence": "high",
        "city": "Красноярск",
        "url": f"https://{source}/Labor",
        **kwargs,
    }


def estimate(manual=(), ready=None, *, quotes=()):
    return pricing.estimate_repair_work_cost(
        make="DEMO",
        work_items=[OPERATION],
        quotes_json={"labor_time_sample": list(manual), "quotes": list(quotes)},
        price_evidence=(
            {"work_items": [OPERATION], "vehicle_context": {"make": "DEMO"}, "observations": [], "labor": ready}
            if ready is not None
            else None
        ),
        auto_research=False,
        use_internal_experience=False,
    )


def signature(result):
    analysis = result["operation_estimates"][0]["labor_time_analysis"]
    return (
        analysis["valid_count"],
        analysis["source_count"],
        analysis["average_hours"],
        analysis["range_hours"],
        analysis["confidence"],
        result["recommended_total_works_rub"],
    )


@pytest.mark.parametrize("route", ["manual", "ready", "combined"])
@pytest.mark.parametrize("reverse", [False, True])
def test_labor_replay_keeps_the_same_count_mean_confidence_and_price(route, reverse):
    first, second = observation(), observation("b.example", 5)
    original = estimate([first, second])
    repeated = [deepcopy(first) for _ in range(9)] + [second]
    if reverse:
        repeated.reverse()
    actual = (
        estimate(repeated)
        if route == "manual"
        else estimate(ready=repeated)
        if route == "ready"
        else estimate(repeated, ready=[first, second])
    )
    assert signature(original) == (2, 2, 3.0, [1.0, 5.0], "high", 12000)
    assert signature(actual) == signature(original)
    assert actual["labor_time_sample"]["valid_count"] == 2


@pytest.mark.parametrize("field,value", [("hours", "1.0 часа"), ("norm_hours", "1"), ("range_hours", [1, 1])])
@pytest.mark.parametrize("structured", [False, True])
def test_numeric_aliases_and_refresh_metadata_do_not_create_another_measurement(field, value, structured):
    first = observation()
    if structured:
        first["source"] = {
            "provider": "public_web",
            "primary_lineage": "a.example",
            "locator": first.pop("url"),
            "fetched_at": "2026-10-01T00:00:00Z",
        }
    repeated = {**deepcopy(first), "captured_at": "2026-10-15T00:00:00Z", "evidence": "refreshed snippet"}
    repeated.pop("hours", None)
    repeated[field] = value
    if structured:
        repeated["source"]["fetched_at"] = "2026-10-15T00:00:00Z"
    actual = estimate([first, repeated, observation("b.example", 5)])
    assert signature(actual) == (2, 2, 3.0, [1.0, 5.0], "high", 12000)
    if structured:
        assert actual["labor_time_sample"]["rows"][0]["evidence_source"] == first["source"]


@pytest.mark.parametrize(
    "change",
    [
        {"source": " A.EXAMPLE "},
        {"operation_name": " ЗАМЕНА  МАСЛА "},
        {"official": "no", "public_source": "yes"},
        {"official": 0, "public_source": 1},
        {"capture_method": " public_source "},
    ],
)
def test_existing_text_and_boolean_aliases_keep_a_single_measurement(change):
    first = observation()
    actual = estimate([first, {**first, **change}, observation("b.example", 5)])
    assert signature(actual) == (2, 2, 3.0, [1.0, 5.0], "high", 12000)


def test_field_aliases_use_the_normalizers_existing_source_operation_and_city_priority():
    first = observation()
    alias = {**first, "source_name": first["source"], "operation": OPERATION, "region": "Красноярск"}
    for key in ("source", "operation_name", "city"):
        alias.pop(key)
    actual = estimate([first, alias, observation("b.example", 5)])
    assert signature(actual) == (2, 2, 3.0, [1.0, 5.0], "high", 12000)


@pytest.mark.parametrize(
    "change",
    [
        {"source": "c.example"},
        {"url": "https://a.example/another"},
        {"url": "https://a.example/labor"},
        {"url": "", "locator": "https://a.example/other"},
        {"city": "Санкт-Петербург"},
        {"hours": 2},
        {"range_hours": [0.5, 1.5]},
    ],
)
def test_distinct_source_page_city_value_or_range_remains_an_independent_row(change):
    first = observation()
    actual = estimate([first, {**first, **change}, observation("b.example", 5)])
    assert actual["labor_time_sample"]["valid_count"] == 3


@pytest.mark.parametrize("reverse", [False, True])
def test_equal_midpoints_keep_different_ranges_and_quoted_operation_names(reverse):
    rows = [observation(range_hours=[1, 3]), observation(hours=2)]
    if reverse:
        rows.reverse()
    actual = estimate(rows)
    analysis = actual["operation_estimates"][0]["labor_time_analysis"]
    assert analysis["valid_count"] == 2
    assert analysis["average_hours"] == 2.0 and analysis["range_hours"] == [1.0, 3.0]
    different_name = {**rows[0], "operation_name": "замена масла и фильтра"}
    assert estimate([rows[0], different_name])["labor_time_sample"]["valid_count"] == 2


@pytest.mark.parametrize(
    "change",
    [
        {"confidence": "low"},
        {"official": True},
        {"official": None},
        {"capture_method": "manual_transcription"},
        {"capture_method": "PUBLIC_SOURCE"},
    ],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_admission_metadata_is_preserved_without_first_wins(change, reverse):
    first = observation()
    rows = [first, {**first, **change}, observation("b.example", 5)]
    if reverse:
        rows.reverse()
    actual = estimate(rows)
    analysis = actual["operation_estimates"][0]["labor_time_analysis"]
    assert analysis["valid_count"] == 3
    if change.get("confidence") == "low":
        assert analysis["confidence"] == "low"
        assert actual["autostop_price_rub"] is None


@pytest.mark.parametrize("reverse", [False, True])
def test_invalid_rows_remain_visible_and_do_not_become_valid_on_dedupe(reverse):
    rows = [observation(), observation("b.example", 5), observation(public_source=False), observation(hours=0)]
    if reverse:
        rows.reverse()
    actual = estimate(rows)
    assert actual["labor_time_sample"]["valid_count"] == 2
    assert actual["labor_time_sample"]["invalid_count"] == 2
    assert signature(actual) == (2, 2, 3.0, [1.0, 5.0], "high", 12000)


def test_distinct_and_repeated_invalid_values_retain_their_diagnostics():
    actual = estimate([observation(hours=0), observation(hours=0), observation(hours=100)])
    assert actual["labor_time_sample"]["valid_count"] == 0
    assert actual["labor_time_sample"]["invalid_count"] == 3
    assert actual["recommended_total_works_rub"] is None


def test_replaying_one_source_cannot_create_confident_labor_time():
    actual = estimate([observation()] * 12)
    assert signature(actual) == (1, 1, 1.0, [1.0, 1.0], "low", 4000)
    assert actual["autostop_price_rub"] is None
    assert estimate()["recommended_total_works_rub"] is None


def test_replayed_time_does_not_replace_a_qualifying_regional_price():
    quotes = [
        {
            "source": source,
            "city": "Красноярск",
            "operation_name": OPERATION,
            "price_rub": 5000,
            "includes_parts": False,
            "labor_only": True,
        }
        for source in ("a.example", "b.example", "c.example")
    ]
    actual = estimate([observation()] * 9 + [observation("b.example", 5)], quotes=quotes)
    assert actual["autostop_price_rub"] == 7300
    assert actual["operation_estimates"][0]["pricing_method"] == "krasnoyarsk_market_mean"


def public_row(source, time):
    return {"source": source, "url": f"https://{source}/Labor", "title": "Прайс", "snippet": f"{OPERATION}: {time}"}


def stub_queries(monkeypatch, responses):
    monkeypatch.setattr(
        research, "build_public_research_queries", lambda **_kwargs: {"labor_prices": [], "labor_times": ["one", "two"]}
    )
    answers = iter(responses)
    monkeypatch.setattr(research, "_ddg_search", lambda *_args, **_kwargs: {"results": next(answers)})


def test_new_public_collector_replays_are_removed_at_the_common_estimate_input(monkeypatch):
    first, second = public_row("a.example", "1 час"), public_row("b.example", "5 часов")
    stub_queries(monkeypatch, [[first, second], [first]])
    ready = labor.collect_work_price_evidence([OPERATION], {"make": "DEMO"}, max_queries=2)
    assert len(ready["data"]["labor"]) == 3
    actual = pricing.estimate_repair_work_cost(
        make="DEMO", work_items=[OPERATION], price_evidence=ready, use_internal_experience=False
    )
    assert signature(actual) == (2, 2, 3.0, [1.0, 5.0], "low", 12000)


@pytest.mark.parametrize("reverse", [False, True])
def test_legacy_collector_preserves_different_ranges_at_one_midpoint(monkeypatch, reverse):
    rows = [public_row("a.example", "1–3 часа"), public_row("a.example", "2 часа")]
    if reverse:
        rows.reverse()
    stub_queries(monkeypatch, [rows, rows])
    result = research.collect_public_work_pricing_research(
        vehicle_context={"make": "DEMO"},
        operations=[{"normalized_name": OPERATION}],
        city="Красноярск",
        auto_research=True,
    )
    samples = result["labor_time_sample"]
    assert len(samples) == 2
    assert {tuple(row["range_hours"]) for row in samples} == {(1.0, 3.0), (2.0, 2.0)}
