from __future__ import annotations

import json
import subprocess
import sys
from urllib.error import URLError

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
    monkeypatch.setenv("AUTOSTOP_AUTOMOTIVE_OFFLINE_RUNTIME", str(tmp_path / "unavailable-runtime"))
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "j1"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "vin"))


def quote(source, price):
    return {
        "source": source,
        "url": "https://" + source + "/prices",
        "city": "Красноярск",
        "operation_name": OPERATION,
        "price_rub": price,
        "includes_parts": False,
        "labor_only": True,
        "captured_at": "2026-10-13",
        "confidence": "medium",
    }


def estimate(quotes, **kwargs):
    return pricing.estimate_repair_work_cost(
        vehicle="Synthetic sedan",
        make="DEMO",
        model="Demo",
        work_items=[OPERATION],
        quotes_json=quotes,
        auto_research=False,
        use_internal_experience=False,
        **kwargs,
    )


def public_rows(*, operation=OPERATION, sources=("a.example", "b.example", "c.example")):
    return [
        {
            "source": source,
            "url": "https://" + source + "/prices",
            "title": "Прайс СТО",
            "snippet": "Красноярск. " + operation + ": только работа 5000 руб.",
        }
        for source in sources
    ]


class HtmlResponse:
    def __init__(self, body):
        self.body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return self.body


def search_html(rows):
    return "".join(
        '<div class="result results_links"><a rel="nofollow" class="result__a" href="'
        + row["url"]
        + '">'
        + row["title"]
        + '</a><a class="result__snippet">'
        + row["snippet"]
        + "</a></div></div>"
        for row in rows
    )


def test_valid_positive_price_preserves_explicit_regional_formula():
    result = estimate([quote("a.example", "5 000 руб."), quote("b.example", 5000), quote("c.example", 5000)])
    assert result["autostop_price_rub"] == 7300
    assert result["formula"]["minimum_confident_quotes"] == 3
    assert result["formula"]["krasnoyarsk_market"]["markup_multiplier"] == 1.45


def test_negative_price_string_cannot_become_positive_quote():
    row = pricing._normalize_quote(quote("a.example", "-3000 руб."))
    assert not row["valid_input"]
    assert row["price_rub"] is None


@pytest.mark.parametrize("value", [10**400, "9" * 400 + " руб."], ids=["huge-json-int", "huge-currency-string"])
def test_valid_json_oversized_price_does_not_raise_or_create_price(value):
    rows = [quote("a.example", value)]
    json.dumps(rows, allow_nan=False)
    result = estimate(rows)
    assert result["autostop_price_rub"] is None


def test_negative_labor_hours_string_cannot_become_positive_time():
    row = pricing._normalize_labor_time_row(
        {"source": "time.example", "operation_name": OPERATION, "hours": "-2 часа", "public_source": True}
    )
    assert not row["valid_input"]
    assert row["hours"] is None


def test_valid_json_oversized_labor_time_does_not_raise_or_create_price():
    times = [{"source": "time.example", "operation_name": OPERATION, "hours": 10**400, "public_source": True}]
    json.dumps(times, allow_nan=False)
    result = estimate({"labor_time_sample": times})
    assert result["autostop_price_rub"] is None


def test_replayed_quote_does_not_admit_two_observation_market():
    original = [quote("a.example", 5000), quote("b.example", 5000)]
    assert estimate(original)["autostop_price_rub"] is None
    replayed = [original[0], original[0], original[1]]
    result = estimate(replayed)
    assert result["autostop_price_rub"] is None


def test_collected_ready_evidence_replay_does_not_admit_two_stations(monkeypatch):
    rows = public_rows(sources=("a.example", "b.example"))
    monkeypatch.setattr(research, "urlopen", lambda *_args, **_kwargs: HtmlResponse(search_html(rows)))
    ready = labor.collect_work_price_evidence([OPERATION], {"make": "DEMO"}, max_queries=2)
    assert ready["ok"]
    assert ready["execution"]["network_calls"] == 2
    result = pricing.estimate_repair_work_cost(
        make="DEMO", work_items=[OPERATION], price_evidence=ready, use_internal_experience=False
    )
    assert result["research"]["access_mode"] == "provided_evidence"
    assert result["autostop_price_rub"] is None


def test_unrelated_snippets_cannot_price_requested_work_end_to_end(monkeypatch):
    rows = public_rows(operation="Замена свечей")
    monkeypatch.setattr(research, "urlopen", lambda *_args, **_kwargs: HtmlResponse(search_html(rows)))
    result = pricing.estimate_repair_work_cost(
        make="DEMO", work_items=[OPERATION], auto_research=True, use_internal_experience=False
    )
    assert len(result["sources_checked"]) == 4
    assert result["autostop_price_rub"] is None


def test_price_list_keeps_amount_bound_to_observed_operation():
    row = public_rows()[0]
    row["snippet"] = "Красноярск. Замена масла: только работа 1000 руб. Замена свечей: только работа 9000 руб."
    parsed, _ = research.parse_public_work_evidence(
        row, OPERATION + " цена", [{"normalized_name": OPERATION}], "labor_prices"
    )
    assert not any(item["operation_name"] == OPERATION and item["price_rub"] == 9000 for item in parsed)


def test_matching_snippets_preserve_regional_estimate_end_to_end(monkeypatch):
    monkeypatch.setattr(research, "urlopen", lambda *_args, **_kwargs: HtmlResponse(search_html(public_rows())))
    result = pricing.estimate_repair_work_cost(
        make="DEMO", work_items=[OPERATION], auto_research=True, use_internal_experience=False
    )
    assert result["autostop_price_rub"] == 7300
    assert result["operation_estimates"][0]["sample"]["valid_count"] == 3


class ResetResponse(HtmlResponse):
    def read(self, *_args):
        raise ConnectionResetError("synthetic response body reset")


def test_legacy_body_reset_is_source_failure_not_unhandled_error(monkeypatch):
    monkeypatch.setattr(research, "urlopen", lambda *_args, **_kwargs: ResetResponse(""))
    result = research.collect_public_work_pricing_research(
        vehicle_context={"make": "DEMO"},
        operations=[{"normalized_name": OPERATION}],
        city="Красноярск",
        auto_research=True,
    )
    assert result["quotes"] == []
    assert all(row["status"] == "error" for row in result["sources_checked"])


def test_legacy_urlerror_remains_recorded_source_failure(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise URLError("synthetic unavailable")

    monkeypatch.setattr(research, "urlopen", unavailable)
    result = research.collect_public_work_pricing_research(
        vehicle_context={"make": "DEMO"},
        operations=[{"normalized_name": OPERATION}],
        city="Красноярск",
        auto_research=True,
    )
    assert result["quotes"] == []
    assert len(result["sources_checked"]) == 4
    assert all(row["status"] == "error" for row in result["sources_checked"])


def evidence_estimate(*, internal_price, hours, public_price=5000):
    historical = {
        "schema_version": pricing.LABOR_EXPERIENCE_SCHEMA,
        "labor_baselines": [
            {
                "operation_name": OPERATION,
                "operation_key": "замена_масла",
                "sample_count": 10,
                "p25_rub": internal_price - 300,
                "p75_rub": internal_price + 300,
                "recommended_anchor_rub": internal_price,
            }
        ],
    }
    times = [
        {
            "source": source,
            "operation_name": OPERATION,
            "hours": hours,
            "public_source": True,
            "confidence": "medium",
            "captured_at": "2026-10-13",
        }
        for source in ("time-a.example", "time-b.example")
    ]
    return estimate(
        {
            "quotes": [quote(source, public_price) for source in ("a.example", "b.example", "c.example")],
            "labor_time_sample": times,
        },
        internal_experience_json=historical,
    )


def test_explicit_time_conflict_prevents_high_decision_confidence():
    result = evidence_estimate(internal_price=50000, hours=0.25)
    operation = result["operation_estimates"][0]
    assert operation["labor_time_analysis"]["cross_check"] == "too_high"
    assert operation["recommended_range_rub"][0] == 7300
    assert operation["recommended_range_rub"][1] >= 50000
    assert result["decision_confidence"] != "high"


def test_consistent_three_family_evidence_preserves_high_confidence():
    result = evidence_estimate(internal_price=7300, hours=2)
    operation = result["operation_estimates"][0]
    assert operation["labor_time_analysis"]["cross_check"] == "ok"
    assert result["decision_confidence"] == "high"


@pytest.mark.parametrize("value", ["−2 часа", "-2-3 часа", "-2–3 часа", "−2–3 часа", "- 2 часа"])
def test_signed_labor_time_does_not_salvage_positive_endpoint(value):
    assert pricing._coerce_hours(value) == (None, None)


@pytest.mark.parametrize("value", ["−3000 руб.", "- 3000 руб.", float("inf"), float("nan")])
def test_invalid_numeric_price_remains_unavailable(value):
    assert pricing._coerce_price(value) is None


@pytest.mark.parametrize("parser", ["price", "hours", "public_price", "public_hours"])
def test_invalid_long_whitespace_is_bounded_without_provider_work(parser):
    script = """
import os
import sys

def audit(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.bind', 'subprocess.Popen'}:
        raise AssertionError('unexpected external operation')
    if event == 'open':
        for value in args:
            if isinstance(value, (str, bytes)) and os.fsdecode(value).startswith((
                '/run/autostop-j1-vin', '/opt/AutostopManager/data', '/opt/autostop-automotive',
                '/var/lib/autostop-manager/roles',
            )):
                raise AssertionError('unexpected default private data read')

sys.addaudithook(audit)
from autostop_manager import work_pricing as pricing, work_pricing_research as research
parsers = {
    'price': (pricing._coerce_price, None),
    'hours': (pricing._coerce_hours, (None, None)),
    'public_price': (research._prices_from_text, []),
    'public_hours': (research._hours_from_text, []),
}
parse, expected = parsers[sys.argv[1]]
assert parse('prefix ' + ' ' * 100000 + 'suffix') == expected
"""
    # A subprocess bounds a stalled parser without leaving a Python worker alive.
    process = subprocess.run(
        [sys.executable, "-c", script, parser], capture_output=True, text=True, timeout=5, check=False
    )
    assert process.returncode == 0, process.stderr


def test_computed_finite_market_overflow_returns_unknown_price():
    result = estimate([quote(source, 1.4e308) for source in ("a.example", "b.example", "c.example")])
    assert result["autostop_price_rub"] is None
    assert result["recommended_total_works_rub"] is None
    json.dumps(result, allow_nan=False)


def test_computed_hourly_rate_overflow_does_not_leak_nonfinite_number():
    times = [{"source": "time.example", "operation_name": OPERATION, "hours": 0.01, "public_source": True}]
    result = estimate(
        {
            "quotes": [quote(source, 1e307) for source in ("a.example", "b.example", "c.example")],
            "labor_time_sample": times,
        }
    )
    assert result["operation_estimates"][0]["labor_time_analysis"]["cross_check"] == "too_high"
    assert result["decision_confidence"] != "high"
    json.dumps(result, allow_nan=False)


def test_numeric_rate_overflow_cannot_promote_three_family_conflict():
    result = evidence_estimate(internal_price=7300, hours=0.01, public_price=1e307)
    operation = result["operation_estimates"][0]
    analysis = operation["labor_time_analysis"]
    assert len(operation["evidence_source_families"]) == 3
    assert operation["autostop_price_rub"] > analysis["average_hours"] * 12000
    assert analysis["autostop_effective_rate_rub_per_hour"] is None
    assert result["decision_confidence"] != "high"
    assert analysis["cross_check"] == "too_high"
    json.dumps(result, allow_nan=False)


def test_known_zero_rounded_price_retains_zero_rate_and_existing_low_conflict():
    result = evidence_estimate(internal_price=7300, hours=2, public_price=1)
    operation = result["operation_estimates"][0]
    analysis = operation["labor_time_analysis"]
    assert operation["autostop_price_rub"] == 0
    assert len(operation["evidence_source_families"]) == 3
    assert analysis["autostop_effective_rate_rub_per_hour"] == 0
    assert analysis["cross_check"] == "too_low"
    assert result["decision_confidence"] != "high"


@pytest.mark.parametrize("included", [True, "true", "да", 1])
@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_parts_conflict_cannot_create_three_valid_quotes(included, reverse):
    pure = quote("a.example", 5000)
    bundle = {**pure, "includes_parts": included, "labor_only": False}
    duplicated = [bundle, pure] if reverse else [pure, bundle]
    result = estimate([*duplicated, quote("b.example", 5000), quote("c.example", 5000)])
    assert result["autostop_price_rub"] is None
    assert any(
        "includes_parts" in row["reasons"] for row in result["operation_estimates"][0]["sample"]["invalid_quotes"]
    )


def test_duplicate_structured_sources_ignore_fetch_timestamp():
    base = quote("a.example", 5000)
    first = {
        **base,
        "source": {
            "provider": "public_web",
            "primary_lineage": "a.example",
            "locator": base["url"],
            "fetched_at": "2026-10-13T10:00:00+00:00",
        },
    }
    replay = {**first, "source": {**first["source"], "fetched_at": "2026-10-13T10:00:01+00:00"}}
    result = estimate([first, replay, quote("b.example", 5000)])
    assert result["autostop_price_rub"] is None


def test_duplicate_numeric_aliases_do_not_create_sample_size():
    base = quote("a.example", 5000)
    replay = {**base, "price_rub": "5 000 руб.", "includes_parts": "false", "labor_only": "true"}
    result = estimate([base, replay, quote("b.example", 5000)])
    assert result["autostop_price_rub"] is None


def test_separate_locator_keeps_separate_quote():
    base = quote("a.example", 5000)
    other_page = {**base, "url": "https://a.example/other-price-page"}
    result = estimate([base, other_page, quote("b.example", 5000)])
    assert result["autostop_price_rub"] == 7300
    assert result["operation_estimates"][0]["sample"]["valid_count"] == 3


def test_separate_city_does_not_transfer_parts_conflict():
    base = quote("a.example", 5000)
    other_city = {**base, "city": "Санкт-Петербург", "includes_parts": True, "labor_only": False}
    result = estimate([base, other_city, quote("b.example", 5000), quote("c.example", 5000)])
    assert result["autostop_price_rub"] == 7300


def test_separate_amount_keeps_separate_quote():
    result = estimate([quote("a.example", 5000), quote("a.example", 6000), quote("b.example", 5500)])
    assert result["market_average_rub"] == 5500
    assert result["autostop_price_rub"] == 8000


def test_title_cannot_bind_another_operations_snippet_amount():
    row = public_rows(operation="Замена свечей")[0]
    row["title"] = "Замена масла"
    parsed, source = research.parse_public_work_evidence(
        row, OPERATION + " цена", [{"normalized_name": OPERATION}], "labor_prices"
    )
    assert parsed and parsed[0]["price_rub"] == 5000
    assert parsed[0]["operation_name"] != OPERATION
    assert "Замена свечей" in parsed[0]["evidence"]
    assert parsed[0]["source"] == source
    assert source["locator"] == row["url"]


def test_mixed_operation_price_does_not_inherit_another_labor_flag():
    row = public_rows()[0]
    row["snippet"] = "Красноярск. Замена масла 3000 руб.; Замена свечей: только работа 9000 руб."
    parsed, _ = research.parse_public_work_evidence(
        row, OPERATION + " цена", [{"normalized_name": OPERATION}], "labor_prices"
    )
    assert not any(item["operation_name"] == OPERATION and item["price_rub"] == 9000 for item in parsed)
    oil = next(item for item in parsed if item["operation_name"] == OPERATION)
    assert oil["labor_only"] is False and oil["includes_parts"] is None


def test_query_cannot_bind_other_operations_time_or_activate_fallback(monkeypatch):
    rows = public_rows(operation="Замена свечей")
    for row in rows:
        row["title"] = "Замена масла"
        row["snippet"] = "Красноярск. Замена свечей: 2 часа."
    monkeypatch.setattr(research, "urlopen", lambda *_args, **_kwargs: HtmlResponse(search_html(rows)))
    result = pricing.estimate_repair_work_cost(
        make="DEMO", work_items=[OPERATION], auto_research=True, use_internal_experience=False
    )
    assert result["recommended_total_works_rub"] is None
    assert result["operation_estimates"][0]["labor_time_analysis"]["valid_count"] == 0


def test_matching_time_evidence_preserves_about_hourly_fallback(monkeypatch):
    rows = public_rows()
    for row in rows:
        row["snippet"] = "Красноярск. Замена масла: 2 часа."
    monkeypatch.setattr(research, "urlopen", lambda *_args, **_kwargs: HtmlResponse(search_html(rows)))
    result = pricing.estimate_repair_work_cost(
        make="DEMO", work_items=[OPERATION], auto_research=True, use_internal_experience=False
    )
    operation = result["operation_estimates"][0]
    assert operation["autostop_price_rub"] == 8000
    assert operation["pricing_method"] == "public_labor_hours"
    assert operation["price_qualifier"] == "about"


def test_title_only_operation_preserves_regional_price_end_to_end(monkeypatch):
    rows = public_rows()
    for row in rows:
        row["title"] = "Замена масла"
        row["snippet"] = "Красноярск. Только работа 5000 руб."
    monkeypatch.setattr(research, "urlopen", lambda *_args, **_kwargs: HtmlResponse(search_html(rows)))
    result = pricing.estimate_repair_work_cost(
        make="DEMO", work_items=[OPERATION], auto_research=True, use_internal_experience=False
    )
    assert result["autostop_price_rub"] == 7300
    assert result["confidence"] == "medium"
    assert result["operation_estimates"][0]["sample"]["valid_count"] == 3


def test_title_only_operation_preserves_about_hourly_fallback(monkeypatch):
    rows = public_rows()
    for row in rows:
        row["title"] = "Замена масла"
        row["snippet"] = "Красноярск. Норма времени 2 часа."
    monkeypatch.setattr(research, "urlopen", lambda *_args, **_kwargs: HtmlResponse(search_html(rows)))
    result = pricing.estimate_repair_work_cost(
        make="DEMO", work_items=[OPERATION], auto_research=True, use_internal_experience=False
    )
    operation = result["operation_estimates"][0]
    assert operation["autostop_price_rub"] == 8000
    assert operation["pricing_method"] == "public_labor_hours"
    assert operation["price_qualifier"] == "about"
    assert operation["labor_time_analysis"]["cross_check"] == "selected_hourly_rate_basis"


@pytest.mark.parametrize(
    ("kind", "snippet"),
    [
        ("labor_prices", "Красноярск. Свечи: только работа 5000 руб."),
        ("labor_prices", "Красноярск. Замена свечей: только работа 5000 руб."),
        ("labor_prices", "Красноярск. Только работа 5000 руб.; свечи зажигания."),
        ("labor_prices", "Красноярск. Только работа 3000 руб.; пакет 9000 руб."),
        ("labor_prices", "Красноярск. Только работа 5000 руб.; только работа 5000 руб."),
        ("labor_prices", "Красноярск. Только работа 5000 руб.; замена свечей без запчастей."),
        ("labor_times", "Красноярск. Свечи: 2 часа."),
        ("labor_times", "Красноярск. Замена свечей: 2 часа."),
        ("labor_times", "Красноярск. Норма времени 2 часа; 3 часа."),
        ("labor_times", "Красноярск. Норма времени 2 часа; норма времени 2 часа."),
        ("labor_times", "Красноярск. Норма времени 2 часа; снятие свечей."),
    ],
)
def test_title_only_operation_cannot_bind_competing_or_ambiguous_snippet(kind, snippet):
    row = {**public_rows()[0], "title": "Замена масла", "snippet": snippet}
    parsed, _ = research.parse_public_work_evidence(row, OPERATION + " цена", [{"normalized_name": OPERATION}], kind)
    assert parsed
    assert all(item["operation_name"] != OPERATION for item in parsed)


def test_title_only_operation_cannot_bind_another_explicit_alias():
    row = {**public_rows()[0], "title": "Замена масла", "snippet": "Красноярск. Свечи: только работа 5000 руб."}
    operations = [{"normalized_name": OPERATION}, {"normalized_name": "замена свечей", "aliases": ["свечи"]}]
    parsed, _ = research.parse_public_work_evidence(row, OPERATION + " цена", operations, "labor_prices")
    assert parsed[0]["operation_name"] == "замена свечей"


def test_title_only_parts_included_price_retains_parts_veto():
    row = {**public_rows()[0], "title": "Замена масла", "snippet": "Красноярск. Работа с запчастями 5000 руб."}
    parsed, _ = research.parse_public_work_evidence(
        row, OPERATION + " цена", [{"normalized_name": OPERATION}], "labor_prices"
    )
    assert parsed[0]["operation_name"] == OPERATION
    assert parsed[0]["includes_parts"] is True
    assert parsed[0]["labor_only"] is False


@pytest.mark.parametrize(
    ("snippet", "city"),
    [
        ("Красноярск. Стоимость работы 5000 рублей без учета запчастей.", "Красноярск"),
        ("Санкт-Петербург. Только работа 5000 р.", "Санкт-Петербург"),
        ("Только работа 5000 ₽", ""),
        ("Красноярск. Работа 5000 руб. Запчасти не включены.", "Красноярск"),
    ],
)
def test_title_only_price_keeps_currency_scope_and_city_annotations(snippet, city):
    row = {**public_rows()[0], "title": "Замена масла", "snippet": snippet}
    parsed, _ = research.parse_public_work_evidence(
        row, OPERATION + " цена", [{"normalized_name": OPERATION}], "labor_prices"
    )
    assert len(parsed) == 1
    assert parsed[0]["operation_name"] == OPERATION
    assert parsed[0]["price_rub"] == 5000
    assert parsed[0]["city"] == city
    assert parsed[0]["includes_parts"] is False
    assert parsed[0]["labor_only"] is True


@pytest.mark.parametrize(
    ("snippet", "hours"),
    [
        ("Красноярск. Трудоемкость 2,5-3,0 н/ч", [2.5, 3.0]),
        ("Санкт-Петербург. Время выполнения 2,5 часа", [2.5, 2.5]),
        ("Норма времени 2 ч.", [2.0, 2.0]),
    ],
)
def test_title_only_time_keeps_normal_units_ranges_and_scope(snippet, hours):
    row = {**public_rows()[0], "title": "Замена масла", "snippet": snippet}
    parsed, _ = research.parse_public_work_evidence(
        row, OPERATION + " норма", [{"normalized_name": OPERATION}], "labor_times"
    )
    assert len(parsed) == 1
    assert parsed[0]["operation_name"] == OPERATION
    assert parsed[0]["range_hours"] == hours


def test_title_only_paired_price_and_normative_time_preserve_operation_binding():
    row = {
        **public_rows()[0],
        "title": "Замена масла",
        "snippet": "Красноярск, только работа 1000 руб. Норматив времени 1.5 часа.",
    }
    operations = [{"normalized_name": OPERATION}]
    prices, _ = research.parse_public_work_evidence(row, OPERATION + " цена", operations, "labor_prices")
    times, _ = research.parse_public_work_evidence(row, OPERATION + " нормо часы", operations, "labor_times")
    assert prices[0]["operation_name"] == OPERATION
    assert prices[0]["price_rub"] == 1000
    assert prices[0]["includes_parts"] is False
    assert prices[0]["labor_only"] is True
    assert times[0]["operation_name"] == OPERATION
    assert times[0]["range_hours"] == [1.5, 1.5]


def test_title_only_scope_accepts_an_explicit_operation_alias():
    name = "замена рулевой рейки"
    row = {**public_rows()[0], "title": "Рулевая рейка", "snippet": "Красноярск. Только работа 5000 руб."}
    parsed, _ = research.parse_public_work_evidence(
        row, "произвольный запрос", [{"normalized_name": name, "aliases": ["рулевая рейка"]}], "labor_prices"
    )
    assert parsed[0]["operation_name"] == name


def test_title_only_scope_does_not_inherit_a_combined_operation_heading():
    row = {**public_rows()[0], "title": "Замена масла и свечей", "snippet": "Красноярск. Только работа 5000 руб."}
    parsed, _ = research.parse_public_work_evidence(
        row, OPERATION + " цена", [{"normalized_name": OPERATION}], "labor_prices"
    )
    assert parsed[0]["operation_name"] != OPERATION


def test_selected_hourly_rate_confidence_preserves_existing_nonconflict_policy():
    result = estimate(
        {
            "labor_time_sample": [
                {"source": "time.example", "operation_name": OPERATION, "hours": 2, "public_source": True}
            ]
        }
    )
    operation = result["operation_estimates"][0]
    assert operation["pricing_method"] == "public_labor_hours"
    assert operation["labor_time_analysis"]["cross_check"] == "selected_hourly_rate_basis"
    assert result["recommended_total_works_rub"] == 8000
    assert result["decision_confidence"] == "low"


@pytest.mark.parametrize(
    "snippet",
    ["Капитальный ремонт двигателя: только работа 5000 руб.", "5000 руб. только работа — капитальный ремонт двигателя"],
)
def test_matching_long_operation_is_not_split_inside_its_label(snippet):
    operation = "капитальный ремонт двигателя"
    row = public_rows()[0]
    row["snippet"] = snippet
    parsed, _ = research.parse_public_work_evidence(
        row, operation + " цена", [{"normalized_name": operation}], "labor_prices"
    )
    assert any(item["operation_name"] == operation and item["price_rub"] == 5000 for item in parsed)


@pytest.mark.parametrize("text", ["−3000 руб.", "- 3000 руб."])
def test_public_parser_does_not_salvage_negative_currency(text):
    assert research._prices_from_text(text) == []


@pytest.mark.parametrize(
    "text", ["−2 часа", "-2-3 часа", "-2–3 часа", "норма времени −2 часа", "норма времени - 2 часа"]
)
def test_public_parser_does_not_salvage_negative_hours(text):
    assert research._hours_from_text(text) == []
