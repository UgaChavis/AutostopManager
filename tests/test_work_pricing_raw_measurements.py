from __future__ import annotations

import socket
from copy import deepcopy

import pytest

from autostop_manager import automotive_labor as labor, work_pricing as pricing, work_pricing_research as research

OPERATION = "замена масла"
OPERATIONS = [{"normalized_name": OPERATION, "aliases": ["смена масла"]}]
KINDS = [("labor_prices", "price_rub"), ("labor_times", "hours")]
PLACEMENTS = ["snippet", "title", "caption", "caption-explicit"]


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, tmp_path):
    def denied(*_args, **_kwargs):
        raise AssertionError("implicit private or network access forbidden")

    monkeypatch.setattr(pricing, "_load_labor_experience", denied)
    monkeypatch.setattr(research, "urlopen", denied)
    monkeypatch.setattr(research, "_ddg_search", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "manager.sqlite3"))
    monkeypatch.setenv("AUTOSTOP_AUTOMOTIVE_OFFLINE_RUNTIME", str(tmp_path / "unavailable-runtime"))
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "j1"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "vin"))


def public_row(measurements, placement="snippet", *, label=OPERATION, source="synthetic.example", reverse=False):
    record = f"{measurements} — {label}" if reverse else f"{label}: {measurements}"
    title, snippet = "Прайс СТО", "Красноярск. " + record
    if placement == "title":
        title, snippet = record, "Красноярск"
    elif placement == "caption":
        title, snippet = label, "Красноярск. " + measurements
    elif placement == "caption-explicit":
        title = label
    return {"source": source, "url": f"https://{source}/prices", "title": title, "snippet": snippet}


def parse(row, kind, *, operations=OPERATIONS):
    before = deepcopy((row, operations))
    rows, source = research.parse_public_work_evidence(row, "unrelated synthetic query", operations, kind)
    assert (row, operations) == before
    assert source["primary_lineage"] == row["source"]
    assert source["locator"] == row["url"]
    assert source["provider"] == "public_web"
    assert source["method"] == "public_search"
    assert source["scope"] == ("public_price_snippet" if kind == "labor_prices" else "public_labor_time_snippet")
    for observation in rows:
        assert observation["source"] == source
        assert observation["captured_at"] == source["fetched_at"]
        assert observation["city"] == "Красноярск"
        assert observation["public_source"] is True
        assert observation["official"] is False
        assert observation["capture_method"] == "public_search_snippet"
        assert observation["evidence"]
    return rows


AMBIGUOUS = [
    pytest.param("labor_prices", "price_rub", "5000 руб., 5000 руб.", [5000], id="price-repeat"),
    pytest.param(
        "labor_prices", "price_rub", "5000 руб., 5000 руб., 5000 руб., 9000 руб.", [5000], id="price-cap-before-dedupe"
    ),
    pytest.param("labor_prices", "price_rub", "-1000 руб., 5000 руб.", [5000], id="price-negative-first"),
    pytest.param("labor_prices", "price_rub", "5000 руб., 200 руб.", [5000], id="price-below-minimum-last"),
    pytest.param("labor_prices", "price_rub", "300000 руб., 5000 руб.", [5000], id="price-above-maximum"),
    pytest.param("labor_prices", "price_rub", "5000 руб., 9000 руб.", [5000, 9000], id="price-two-distinct"),
    pytest.param("labor_times", "hours", "3 часа, 3 часа", [3.0], id="hours-repeat"),
    pytest.param("labor_times", "hours", "3 часа, 3 часа, 3 часа, 6 часов", [3.0], id="hours-cap-before-dedupe"),
    pytest.param("labor_times", "hours", "-1 час, 3 часа", [3.0], id="hours-negative-first"),
    pytest.param("labor_times", "hours", "3 часа, 90 часов", [3.0], id="hours-above-maximum-last"),
    pytest.param("labor_times", "hours", "1,5 часа, 1,5 часа", [1.5], id="hours-decimal-repeat"),
    pytest.param("labor_times", "hours", "2–3 часа, 2–3 часа", [2.5], id="hours-range-repeat"),
    pytest.param("labor_times", "hours", "3 часа, 6 часов", [3.0, 6.0], id="hours-two-distinct"),
]


@pytest.mark.parametrize("placement", PLACEMENTS)
@pytest.mark.parametrize("kind,value_field,measurements,expected", AMBIGUOUS)
def test_raw_mentions_remain_ambiguous_before_validation_cap_and_value_deduplication(
    placement, kind, value_field, measurements, expected
):
    rows = parse(public_row("только работа " + measurements, placement), kind)
    assert [row[value_field] for row in rows] == expected
    assert [row["operation_name"] for row in rows] == [""] * len(expected)
    if kind == "labor_prices":
        assert {(row["includes_parts"], row["labor_only"]) for row in rows} == {(False, True)}


@pytest.mark.parametrize("placement", PLACEMENTS)
@pytest.mark.parametrize("kind,value_field", KINDS)
@pytest.mark.parametrize(
    "measurements,price,hours",
    [
        pytest.param("5000 руб., 3 часа, 3 часа", 5000, 3.0, id="one-price-two-equal-hours"),
        pytest.param("5000 руб., 5000 руб., 3 часа", 5000, 3.0, id="two-equal-prices-one-hour"),
        pytest.param("5000 руб., -1 час, 3 часа", 5000, 3.0, id="invalid-hour-still-counts"),
        pytest.param("200 руб., 5000 руб., 3 часа", 5000, 3.0, id="invalid-price-still-counts"),
    ],
)
def test_multiple_mentions_of_either_kind_block_both_bindings(placement, kind, value_field, measurements, price, hours):
    rows = parse(public_row("только работа " + measurements, placement), kind)
    assert [row[value_field] for row in rows] == [price if kind == "labor_prices" else hours]
    assert rows[0]["operation_name"] == ""


@pytest.mark.parametrize("explicit_snippet", [False, True], ids=["inheritance-denied", "independent-label-allowed"])
@pytest.mark.parametrize(
    "kind,value_field,invalid_title,measurement,expected",
    [
        pytest.param("labor_prices", "price_rub", "-1000 руб.", "5000 руб.", 5000, id="negative-price"),
        pytest.param("labor_prices", "price_rub", "200 руб.", "5000 руб.", 5000, id="small-price"),
        pytest.param("labor_times", "hours", "-1 час", "3 часа", 3.0, id="negative-hours"),
        pytest.param("labor_times", "hours", "90 часов", "3 часа", 3.0, id="large-hours"),
    ],
)
def test_rejected_title_amount_cannot_make_an_unlabelled_caption_look_like_a_single_observation(
    explicit_snippet, kind, value_field, invalid_title, measurement, expected
):
    row = public_row("только работа " + measurement, "caption-explicit" if explicit_snippet else "caption")
    row["title"] = f"{OPERATION}: {invalid_title}"
    rows = parse(row, kind)
    assert [item[value_field] for item in rows] == [expected]
    assert rows[0]["operation_name"] == (OPERATION if explicit_snippet else "")


@pytest.mark.parametrize("placement", ["snippet", "caption"])
@pytest.mark.parametrize(
    "scope,expected_flags", [("с запчастями", (True, False)), ("без учета запчастей и материалов", (False, True))]
)
def test_unbound_discovery_keeps_observed_parts_scope_and_provenance(placement, scope, expected_flags):
    row = public_row(f"{scope} 5000 руб., 5000 руб.", placement)
    rows = parse(row, "labor_prices")
    assert len(rows) == 1 and rows[0]["price_rub"] == 5000
    assert rows[0]["operation_name"] == ""
    assert (rows[0]["includes_parts"], rows[0]["labor_only"]) == expected_flags


@pytest.mark.parametrize(
    "kind,value_field,measurement,expected",
    [
        pytest.param("labor_prices", "price_rub", "5000 руб.", 5000, id="one-price"),
        pytest.param("labor_times", "hours", "3 часа", 3.0, id="one-hour"),
        pytest.param("labor_times", "hours", "1,5 часа", 1.5, id="decimal-comma"),
        pytest.param("labor_times", "hours", "2,5–3,5 часа", 3.0, id="one-logical-range"),
        pytest.param("labor_times", "hours", "норма времени 2,5–3,5 часа", 3.0, id="overlapping-range-prefix-suffix"),
    ],
)
@pytest.mark.parametrize("field", ["title", "snippet"])
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_single_logical_measurement_survives_forward_and_reverse_labels(
    kind, value_field, measurement, expected, field, reverse
):
    rows = parse(public_row("только работа " + measurement, field, reverse=reverse), kind)
    assert [(row["operation_name"], row[value_field]) for row in rows] == [(OPERATION, expected)]
    if "–" in measurement:
        assert rows[0]["range_hours"] == [2.5, 3.5]


@pytest.mark.parametrize("placement", PLACEMENTS)
@pytest.mark.parametrize("kind,value_field", KINDS)
def test_one_price_and_one_hour_range_are_one_valid_record(placement, kind, value_field):
    rows = parse(public_row("без учета запчастей и материалов 5000 руб., 2,5–3,5 часа", placement), kind)
    expected = 5000 if kind == "labor_prices" else 3.0
    assert [(row["operation_name"], row[value_field]) for row in rows] == [(OPERATION, expected)]
    if kind == "labor_prices":
        assert (rows[0]["includes_parts"], rows[0]["labor_only"]) == (False, True)
    else:
        assert rows[0]["range_hours"] == [2.5, 3.5]


@pytest.mark.parametrize("kind,value_field", KINDS)
@pytest.mark.parametrize(
    "label,normalized,aliases",
    [
        pytest.param("снятие, ремонт и установка рейки", "снятие, ремонт и установка рейки", [], id="full-compound"),
        pytest.param("смена масла", OPERATION, ["смена масла"], id="declared-alias"),
    ],
)
def test_full_compounds_and_declared_aliases_keep_one_price_hour_pair(kind, value_field, label, normalized, aliases):
    operations = [{"normalized_name": normalized, "aliases": aliases}]
    rows = parse(public_row("только работа 5000 руб., 3 часа", label=label), kind, operations=operations)
    assert [(row["operation_name"], row[value_field]) for row in rows] == [
        (normalized, 5000 if kind == "labor_prices" else 3.0)
    ]


@pytest.mark.parametrize(
    "kind,value_field,measurements,expected",
    [
        pytest.param(
            "labor_prices",
            "price_rub",
            "1000 руб., 2000 руб., 3000 руб., 4000 руб.",
            [1000, 2000, 3000],
            id="price-cap",
        ),
        pytest.param("labor_times", "hours", "1 час, 2 часа, 3 часа, 4 часа", [1.0, 2.0, 3.0], id="hours-cap"),
    ],
)
def test_unbound_discovery_keeps_existing_per_record_output_caps(kind, value_field, measurements, expected):
    rows = parse(public_row(measurements), kind)
    assert [row[value_field] for row in rows] == expected
    assert all(row["operation_name"] == "" for row in rows)


@pytest.mark.parametrize("separator", [", ", "; ", "\n"])
@pytest.mark.parametrize("kind,value_field", KINDS)
def test_repeated_values_in_separate_explicit_records_are_not_single_record_ambiguity(separator, kind, value_field):
    first = f"{OPERATION}: только работа 5000 руб., 3 часа"
    second = f"{OPERATION}: только работа 5000 руб., 3 часа"
    row = public_row("")
    row["snippet"] = "Красноярск. " + first + separator + second
    rows = parse(row, kind)
    expected = 5000 if kind == "labor_prices" else 3.0
    assert [(item["operation_name"], item[value_field]) for item in rows] == [
        (OPERATION, expected),
        (OPERATION, expected),
    ]


def collect_and_estimate(monkeypatch, route, responses, queries):
    before = deepcopy((responses, queries))
    monkeypatch.setattr(research, "build_public_research_queries", lambda **_kwargs: queries)
    calls = []

    def fake_ddg(query, *, timeout_seconds):
        assert 0 < timeout_seconds <= 4
        calls.append(query)
        return {"results": responses[query]}

    monkeypatch.setattr(research, "_ddg_search", fake_ddg)
    if route == "ready":
        collected = labor.collect_work_price_evidence([OPERATION], {"make": "DEMO"}, max_queries=4)
        rows = {"prices": collected["data"]["observations"], "times": collected["data"]["labor"]}
        original = deepcopy(collected)
        actual = pricing.estimate_repair_work_cost(
            make="DEMO", work_items=[OPERATION], price_evidence=collected, use_internal_experience=False
        )
        assert collected == original
        assert actual["research"]["access_mode"] == "provided_evidence"
    else:
        captured = []
        real_collect = research.collect_public_work_pricing_research

        def capture_actual_collector(**kwargs):
            result = real_collect(**kwargs)
            captured.append(result)
            return result

        monkeypatch.setattr(pricing, "collect_public_work_pricing_research", capture_actual_collector)
        actual = pricing.estimate_repair_work_cost(
            make="DEMO", work_items=[OPERATION], auto_research=True, use_internal_experience=False
        )
        assert len(captured) == 1
        rows = {"prices": captured[0]["quotes"], "times": captured[0]["labor_time_sample"]}
    assert calls == [query for values in queries.values() for query in values]
    assert (responses, queries) == before
    for observations in rows.values():
        for row in observations:
            source = row.get("evidence_source", row["source"])
            assert source["primary_lineage"].endswith(".example")
            assert source["locator"] == f"https://{source['primary_lineage']}/prices"
            assert source["fetched_at"] == row["captured_at"]
            assert row["city"] == "Красноярск"
    return actual, rows


@pytest.mark.parametrize("route", ["legacy", "ready"])
@pytest.mark.parametrize("placement", ["snippet", "caption"])
@pytest.mark.parametrize(
    "kind,value_field,measurements,expected",
    [
        pytest.param("labor_prices", "price_rub", "5000 руб., 5000 руб.", 5000, id="price-repeat"),
        pytest.param("labor_prices", "price_rub", "5000 руб., 5000 руб., 5000 руб., 9000 руб.", 5000, id="price-cap"),
        pytest.param("labor_prices", "price_rub", "200 руб., 5000 руб.", 5000, id="price-invalid"),
        pytest.param("labor_times", "hours", "3 часа, 3 часа", 3.0, id="hours-repeat"),
        pytest.param("labor_times", "hours", "3 часа, 3 часа, 3 часа, 6 часов", 3.0, id="hours-cap"),
        pytest.param("labor_times", "hours", "-1 час, 3 часа", 3.0, id="hours-invalid"),
    ],
)
def test_both_public_collectors_preserve_ambiguous_values_without_creating_a_price(
    monkeypatch, route, placement, kind, value_field, measurements, expected
):
    public_rows = [
        public_row("только работа " + measurements, placement, source=f"sto-{index}.example") for index in range(3)
    ]
    queries = {
        "labor_prices": ["prices"] if kind == "labor_prices" else [],
        "labor_times": ["times"] if kind == "labor_times" else [],
    }
    key = "prices" if kind == "labor_prices" else "times"
    actual, collected = collect_and_estimate(monkeypatch, route, {key: public_rows}, queries)
    assert [row[value_field] for row in collected[key]] == [expected] * 3
    assert all(row["operation_name"] == "" for row in collected[key])
    selected = actual["operation_estimates"][0]
    assert selected["sample"]["raw_matched_count"] == 0
    assert selected["labor_time_analysis"]["valid_count"] == 0
    assert selected["pricing_method"] is None
    assert actual["autostop_price_rub"] is None
    assert actual["recommended_total_works_rub"] is None


@pytest.mark.parametrize("route", ["legacy", "ready"])
@pytest.mark.parametrize("parts_conflict", [False, True], ids=["same-evidence-replay", "conservative-parts-conflict"])
def test_query_replays_keep_source_counts_and_conservative_price_deduplication(monkeypatch, route, parts_conflict):
    first = [public_row("только работа 5000 руб.", source=f"sto-{index}.example") for index in range(3)]
    second = deepcopy(first)
    if parts_conflict:
        for row in second:
            row["snippet"] = row["snippet"].replace("только работа", "с запчастями")
    actual, collected = collect_and_estimate(
        monkeypatch, route, {"one": first, "two": second}, {"labor_prices": ["one", "two"], "labor_times": []}
    )
    assert len(collected["prices"]) == (3 if route == "legacy" else 6)
    selected = actual["operation_estimates"][0]
    assert selected["sample"]["valid_count"] == (0 if parts_conflict else 3)
    assert selected["sample"]["source_count"] == (0 if parts_conflict else 3)
    assert actual["market_sample"]["invalid_count"] == (3 if parts_conflict else 0)
    assert actual["autostop_price_rub"] == (None if parts_conflict else 7300)
    if not parts_conflict:
        assert selected["market_region"] == "krasnoyarsk"


@pytest.mark.parametrize("route", ["legacy", "ready"])
def test_replayed_valid_hour_ranges_keep_two_sources_and_one_range_per_source(monkeypatch, route):
    public_rows = [public_row("2,5–3,5 часа", "caption", source=f"sto-{index}.example") for index in range(2)]
    actual, collected = collect_and_estimate(
        monkeypatch,
        route,
        {"one": public_rows, "two": public_rows},
        {"labor_prices": [], "labor_times": ["one", "two"]},
    )
    assert len(collected["times"]) == (2 if route == "legacy" else 4)
    analysis = actual["operation_estimates"][0]["labor_time_analysis"]
    assert (analysis["valid_count"], analysis["source_count"], analysis["average_hours"]) == (2, 2, 3.0)
    assert analysis["range_hours"] == [2.5, 3.5]
    assert actual["operation_estimates"][0]["autostop_price_rub"] == 12000


@pytest.mark.parametrize("placement", PLACEMENTS)
@pytest.mark.parametrize("kind,value_field", KINDS)
@pytest.mark.parametrize(
    "measurements,expected_hours",
    [
        ("трудоемкость 0, трудоемкость 3", [3.0]),
        ("трудоемкость -0, трудоемкость 3", [3.0]),
        ("норма времени 90, трудоемкость 3", [3.0]),
        ("трудоемкость 1,5, трудоемкость 3", [1.5, 3.0]),
        ("трудоемкость 1.5, трудоемкость 3", [1.5, 3.0]),
    ],
)
def test_punctuation_after_prefix_hours_cannot_hide_another_measurement(
    placement, kind, value_field, measurements, expected_hours
):
    rows = parse(public_row("только работа 5000 руб., " + measurements, placement), kind)
    assert [row[value_field] for row in rows] == ([5000] if kind == "labor_prices" else expected_hours)
    assert all(row["operation_name"] == "" for row in rows)


@pytest.mark.parametrize("placement", PLACEMENTS)
@pytest.mark.parametrize(
    "measurement,expected",
    [
        ("трудоемкость 1,5, без запчастей", 1.5),
        ("трудоемкость 1.5. Только работа", 1.5),
        ("норма времени + 3, только работа", 3.0),
        ("трудоемкость 0,5, только работа", 0.5),
    ],
)
def test_punctuation_ends_a_complete_prefix_decimal(placement, measurement, expected):
    rows = parse(public_row(measurement, placement), "labor_times")
    assert [(row["operation_name"], row["hours"]) for row in rows] == [(OPERATION, expected)]


@pytest.mark.parametrize("malformed", ["1,5,6", "1.5.6", "1,,5", "1..5", "1,.5", "1.,5"])
def test_malformed_prefix_decimal_never_becomes_a_partial_accepted_number(malformed):
    assert parse(public_row("трудоемкость " + malformed), "labor_times") == []


@pytest.mark.parametrize("route", ["legacy", "ready"])
@pytest.mark.parametrize("kind,value_field", KINDS)
def test_both_collectors_reject_a_price_when_zero_prefix_hours_disappeared_before_a_comma(
    monkeypatch, route, kind, value_field
):
    public_rows = [
        public_row("только работа 5000 руб., трудоемкость 0, трудоемкость 3", source=f"sto-{index}.example")
        for index in range(3)
    ]
    key = "prices" if kind == "labor_prices" else "times"
    queries = {
        "labor_prices": ["prices"] if kind == "labor_prices" else [],
        "labor_times": ["times"] if kind == "labor_times" else [],
    }
    actual, collected = collect_and_estimate(monkeypatch, route, {key: public_rows}, queries)
    assert [row[value_field] for row in collected[key]] == [5000 if kind == "labor_prices" else 3.0] * 3
    assert all(row["operation_name"] == "" for row in collected[key])
    assert actual["recommended_total_works_rub"] is None
