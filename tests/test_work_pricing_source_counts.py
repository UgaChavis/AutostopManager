from __future__ import annotations

from copy import deepcopy

import pytest

from autostop_manager import automotive_labor as labor, work_pricing as pricing, work_pricing_research as research

OPERATION = "замена масла"
OTHER_OPERATION = "замена свечей зажигания"
ROUTES = ["manual", "ready", "combined"]
ALIASES = [
    ("Garage Alpha", "GARAGE ALPHA"),
    ("Сервис А", "СЕРВИС\u00a0А"),
    ("Straße", "STRASSE"),
    ("Garage Alpha", "Garage\t  Alpha"),
]


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


def time_row(source, hours, **changes):
    return {
        "source": source,
        "operation_name": OPERATION,
        "hours": hours,
        "public_source": True,
        "confidence": "high",
        **changes,
    }


def quote(source, price, **changes):
    return {
        "source": source,
        "operation_name": OPERATION,
        "city": "Красноярск",
        "price_rub": price,
        "includes_parts": False,
        "labor_only": True,
        "confidence": "high",
        **changes,
    }


def source_form(rows, form):
    rows = deepcopy(rows)
    for index, row in enumerate(rows):
        source = row["source"]
        if form == "source_name":
            row["source_name"] = row.pop("source")
        elif form in {"lineage", "provider"}:
            row["source"] = {
                "provider": f"adapter-{index}" if form == "lineage" else source,
                "locator": f"https://evidence.example/{index}",
                "fetched_at": "2026-10-01T00:00:00Z",
                **({"primary_lineage": source} if form == "lineage" else {}),
            }
    return rows


def estimate(*, times=(), quotes=(), route="manual", operations=(OPERATION,)):
    def split(rows):
        rows = list(rows)
        return (rows, []) if route == "manual" else ([], rows) if route == "ready" else (rows[:1], rows[1:])

    manual_times, ready_times = split(times)
    manual_quotes, ready_quotes = split(quotes)
    return pricing.estimate_repair_work_cost(
        make="DEMO",
        work_items=list(operations),
        quotes_json={"quotes": manual_quotes, "labor_time_sample": manual_times},
        price_evidence=(
            {"work_items": list(operations), "observations": ready_quotes, "labor": ready_times}
            if route != "manual"
            else None
        ),
        auto_research=False,
        use_internal_experience=False,
    )


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("form", ["source", "source_name", "lineage", "provider"])
@pytest.mark.parametrize("sources", ALIASES)
def test_aliases_cannot_make_one_labor_source_independent(route, form, sources):
    rows = source_form([time_row(sources[0], 1), time_row(sources[1], 3)], form)
    before = deepcopy(rows)
    actual = estimate(times=rows, route=route)
    assert rows == before
    analysis = actual["operation_estimates"][0]["labor_time_analysis"]
    assert (analysis["valid_count"], analysis["source_count"], analysis["confidence"]) == (2, 1, "low")
    assert analysis["average_hours"] == 2
    assert analysis["range_hours"] == [1, 3]
    assert actual["recommended_total_works_rub"] == 8000
    assert actual["autostop_price_rub"] is None
    expected_sources = [str(row.get("source_name") or row.get("source")) for row in rows]
    if form in {"lineage", "provider"}:
        assert [row["evidence_source"] for row in analysis["sample"]] == [row["source"] for row in rows]
        expected_sources = list(sources)
    assert [row["source"] for row in analysis["sample"]] == expected_sources


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("form", ["source", "source_name", "lineage", "provider"])
@pytest.mark.parametrize("sources", ALIASES)
def test_aliases_cannot_admit_one_station_as_a_regional_market(route, form, sources):
    rows = source_form([quote(sources[index % 2], price) for index, price in enumerate([1000, 1100, 1200])], form)
    before = deepcopy(rows)
    actual = estimate(quotes=rows, route=route)
    assert rows == before
    operation = actual["operation_estimates"][0]
    assert operation["sample"]["matched_valid_count"] == 3
    assert operation["weak_average_rub"] == 1100
    assert operation["market_region"] is None
    assert operation["sample"]["source_count"] == actual["market_sample"]["source_count"] == 0
    assert actual["autostop_price_rub"] is None


@pytest.mark.parametrize("route", ROUTES)
def test_false_target_city_independence_does_not_suppress_a_valid_alternative(route):
    local = [
        quote(source, price)
        for source, price in zip(["a.example", "A.EXAMPLE", "A.example"], [1000, 1100, 1200], strict=True)
    ]
    alternate = [
        quote(f"b{index}.example", price, city="Санкт-Петербург") for index, price in enumerate([3000, 3100, 3200])
    ]
    actual = estimate(quotes=local + alternate, route=route)
    operation = actual["operation_estimates"][0]
    assert operation["market_region"] == "saint_petersburg"
    assert operation["sample"]["source_count"] == actual["market_sample"]["source_count"] == 3
    assert operation["sample"]["regional_counts"] == {"krasnoyarsk": 3, "saint_petersburg": 3}
    assert actual["market_average_rub"] == 3100
    assert actual["autostop_price_rub"] == 3600


@pytest.mark.parametrize("route", ROUTES)
def test_one_station_aliases_do_not_replace_the_hourly_fallback(route):
    quotes = [
        quote(source, price)
        for source, price in zip(["a.example", "A.EXAMPLE", "A.example"], [1000, 1100, 1200], strict=True)
    ]
    actual = estimate(quotes=quotes, times=[time_row("time.example", 2)], route=route)
    assert actual["operation_estimates"][0]["pricing_method"] == "public_labor_hours"
    assert actual["recommended_total_works_rub"] == 8000
    assert actual["autostop_price_rub"] is None


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("three_sources", [False, True])
def test_high_market_confidence_requires_three_distinct_source_keys(route, three_sources):
    sources = [
        "Garage Alpha",
        "GARAGE ALPHA",
        "Garage Beta",
        "GARAGE BETA",
        "Garage Gamma" if three_sources else "Garage Beta",
    ]
    actual = estimate(
        quotes=[quote(source, price) for source, price in zip(sources, [1000, 1100, 1200, 1300, 1400], strict=True)],
        times=[time_row("time-a.example", 2), time_row("time-b.example", 2)],
        route=route,
    )
    operation = actual["operation_estimates"][0]
    assert operation["sample"]["source_count"] == actual["market_sample"]["source_count"] == (3 if three_sources else 2)
    assert operation["confidence"] == ("high" if three_sources else "medium")
    assert operation["sample"]["valid_count"] == 5
    assert actual["market_average_rub"] == 1200
    assert actual["autostop_price_rub"] == 1700


@pytest.mark.parametrize("route", ROUTES)
def test_top_level_count_unifies_aliases_across_operations(route):
    rows = [
        quote(source, price, operation_name=operation)
        for operation, sources in [(OPERATION, ["a", "b", "b"]), (OTHER_OPERATION, ["A", "B", "B"])]
        for source, price in zip(sources, [1000, 1100, 1200], strict=True)
    ]
    actual = estimate(quotes=rows, operations=(OPERATION, OTHER_OPERATION), route=route)
    assert [operation["sample"]["source_count"] for operation in actual["operation_estimates"]] == [2, 2]
    assert actual["market_sample"]["valid_count"] == 6
    assert actual["market_sample"]["source_count"] == 2


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "sources",
    [
        ("a-b.example", "ab.example"),
        ("a.example", "a-example"),
        ("Garage A", "Garage B"),
        ("https://a.example/Labor", "https://a.example/labor"),
        ("https://a.example/Labor?A=1", "https://a.example/Labor?a=1"),
        ("https://a.example/Labor#A", "https://a.example/Labor#a"),
        ("HTTPS://A.EXAMPLE/Labor", "https://a.example/Labor"),
        ("custom+v1://service/Labor", "custom+v1://service/labor"),
    ],
)
def test_distinct_labels_and_explicit_uris_keep_their_existing_independence(route, sources):
    actual = estimate(
        times=[time_row(sources[0], 1), time_row(sources[1], 3)],
        quotes=[quote(sources[index % 2], price) for index, price in enumerate([1000, 1100, 1200])],
        route=route,
    )
    operation = actual["operation_estimates"][0]
    assert operation["sample"]["source_count"] == actual["market_sample"]["source_count"] == 2
    assert operation["labor_time_analysis"]["source_count"] == 2
    assert operation["labor_time_analysis"]["confidence"] == "high"
    assert operation["market_average_rub"] == 1100


@pytest.mark.parametrize("route", ROUTES)
def test_pages_and_untrusted_metadata_do_not_create_new_sources(route):
    rows = [
        time_row("Garage Alpha", 1, url="https://a.example/Labor", evidence_source={"primary_lineage": "b"}),
        time_row("GARAGE ALPHA", 3, url="https://a.example/labor", evidence_source={"primary_lineage": "c"}),
    ]
    actual = estimate(times=rows, route=route)
    analysis = actual["operation_estimates"][0]["labor_time_analysis"]
    assert (analysis["valid_count"], analysis["source_count"], analysis["confidence"]) == (2, 1, "low")
    assert [row["evidence_source"] for row in analysis["sample"]] == [row["evidence_source"] for row in rows]


@pytest.mark.parametrize("route", ROUTES)
def test_rejected_unmatched_and_outlier_rows_do_not_increase_source_count(route):
    rows = [quote("a.example", price) for price in [1000, 1100, 1200, 1300, 1400]]
    rows += [
        quote("b.example", 100000),
        quote("c.example", 1200, includes_parts=True),
        quote("d.example", -1200),
        quote("e.example", 1200, operation_name=OTHER_OPERATION),
        quote("f.example", 1200, city="Москва"),
    ]
    actual = estimate(quotes=rows, route=route)
    operation = actual["operation_estimates"][0]
    assert operation["market_region"] is None
    assert operation["sample"]["regional_counts"]["krasnoyarsk"] == 5
    assert len(operation["sample"]["excluded_outliers"]) == 1
    assert actual["autostop_price_rub"] is None
    hours = estimate(
        times=[
            time_row("a", 1, confidence="low"),
            time_row("b", 3),
            time_row("c", 2, public_source=False),
            time_row("d", -1),
        ],
        route=route,
    )["operation_estimates"][0]["labor_time_analysis"]
    assert (hours["valid_count"], hours["invalid_count"], hours["source_count"], hours["confidence"]) == (
        2,
        2,
        2,
        "low",
    )


@pytest.mark.parametrize("collector", ["legacy", "ready"])
def test_public_collectors_keep_source_identity_when_values_differ(monkeypatch, collector):
    monkeypatch.setattr(
        research,
        "build_public_research_queries",
        lambda **_kwargs: {"labor_prices": ["prices"], "labor_times": ["times"]},
    )

    def search(query, **_kwargs):
        prices = query == "prices"
        values = [1000, 1100, 1200] if prices else [1, 3]
        return {
            "results": [
                {
                    "source": ["a.example", "A.EXAMPLE", "A.example"][index],
                    "url": f"https://a.example/{index}",
                    "title": "Прайс",
                    "snippet": f"Красноярск. {OPERATION}: только работа {value} руб."
                    if prices
                    else f"{OPERATION}: {value} часа",
                }
                for index, value in enumerate(values)
            ]
        }

    monkeypatch.setattr(research, "_ddg_search", search)
    kwargs = {"make": "DEMO", "work_items": [OPERATION], "use_internal_experience": False}
    if collector == "ready":
        kwargs["price_evidence"] = labor.collect_work_price_evidence([OPERATION], {"make": "DEMO"}, max_queries=2)
    actual = pricing.estimate_repair_work_cost(**kwargs)
    operation = actual["operation_estimates"][0]
    assert operation["sample"]["matched_valid_count"] == 3
    assert operation["market_region"] is None
    assert operation["labor_time_analysis"]["source_count"] == 1
    assert operation["labor_time_analysis"]["valid_count"] == 2
    assert actual["recommended_total_works_rub"] == 8000
    assert actual["autostop_price_rub"] is None
