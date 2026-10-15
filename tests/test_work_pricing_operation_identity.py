from __future__ import annotations

from copy import deepcopy
from itertools import permutations

import pytest

from autostop_manager import work_pricing as pricing, work_pricing_research as research

ROUTES = ["manual", "ready", "combined"]
RACK = "замена рулевой рейки"
OIL = "замена масла"


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, tmp_path):
    def denied(*_args, **_kwargs):
        raise AssertionError("implicit private or network read forbidden")

    monkeypatch.setattr(pricing, "_load_labor_experience", denied)
    monkeypatch.setattr(research, "urlopen", denied)
    monkeypatch.setattr(research, "_ddg_search", denied)
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "manager.sqlite3"))
    monkeypatch.setenv("AUTOSTOP_AUTOMOTIVE_OFFLINE_RUNTIME", str(tmp_path / "unavailable-runtime"))
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "j1"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "vin"))


def quote(operation, source, price=4500, **changes):
    return {
        "source": source,
        "operation_name": operation,
        "city": "Красноярск",
        "price_rub": price,
        "includes_parts": False,
        "labor_only": True,
        "confidence": "high",
        "evidence_source": {"locator": f"https://{source}/prices"},
        **changes,
    }


def time_row(operation, source, hours=2, **changes):
    return {
        "source": source,
        "operation_name": operation,
        "hours": hours,
        "public_source": True,
        "confidence": "high",
        **changes,
    }


def estimate(requested, *, route="manual", quotes=(), times=(), **context):
    def split(rows):
        rows = list(rows)
        if route == "manual":
            return rows, []
        if route == "ready":
            return [], rows
        return rows[:1], rows[1:]

    manual_quotes, ready_quotes = split(quotes)
    manual_times, ready_times = split(times)
    inputs = {
        "vehicle": "Synthetic sedan",
        "work_items": [requested],
        "quotes_json": {"quotes": manual_quotes, "labor_time_sample": manual_times},
        "price_evidence": (
            {"work_items": [requested], "observations": ready_quotes, "labor": ready_times}
            if route != "manual"
            else None
        ),
        "auto_research": False,
        "use_internal_experience": False,
        **context,
    }
    before = deepcopy(inputs)
    actual = pricing.estimate_repair_work_cost(**inputs)
    assert inputs == before
    assert actual["ok"] is True
    return actual


def evidence(operation):
    return {
        "quotes": [quote(operation, f"price-{index}.example", price) for index, price in enumerate((4000, 4500, 5000))],
        "times": [time_row(operation, "time-a.example", 2), time_row(operation, "time-b.example", 3)],
    }


def assert_unmatched(actual):
    operation = actual["operation_estimates"][0]
    assert (operation["sample"]["raw_matched_count"], operation["labor_time_analysis"]["valid_count"]) == (0, 0)
    assert operation["pricing_method"] is None
    assert operation["autostop_price_rub"] is None
    assert actual["total_works_rub"] is None
    assert actual["recommended_total_works_rub"] is None


COMPOUNDS = [
    pytest.param("замена масла и фильтра", OIL, id="and"),
    pytest.param("замена масла плюс фильтра", OIL, id="plus-word"),
    pytest.param("замена масла + фильтра", OIL, id="plus-sign"),
    pytest.param("замена масла, фильтра", OIL, id="comma"),
    pytest.param("замена масла / фильтра", OIL, id="slash"),
    pytest.param("замена масла с фильтром", OIL, id="with"),
    pytest.param("замена масла и замена фильтра", OIL, id="repeated-verb"),
    pytest.param("замена рулевой рейки и насоса ГУР", RACK, id="canonical-rack-and-pump"),
    pytest.param("замена рулевой рейки и масла", RACK, id="canonical-rack-and-oil"),
    pytest.param("замена опорных подшипников и амортизаторов", "замена опорных подшипников", id="bearing-and-strut"),
]


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("reverse", [False, True], ids=["component-request", "compound-request"])
@pytest.mark.parametrize("compound,component", COMPOUNDS)
def test_compound_and_component_evidence_are_not_interchangeable(route, reverse, compound, component):
    requested, observed = (compound, component) if reverse else (component, compound)
    assert_unmatched(estimate(requested, route=route, **evidence(observed)))


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "other",
    [
        "ремонт рулевой рейки",
        "проверка рулевой рейки",
        "диагностика рулевой рейки",
        "установка рулевой рейки",
        "снятие рулевой рейки",
        "не замена рулевой рейки",
        "ремонт рулевой рейки без замены",
    ],
    ids=["repair", "check", "diagnose", "install", "remove", "negated", "repair-without-replace"],
)
def test_same_part_with_another_action_cannot_price_replacement(route, reverse, other):
    requested, observed = (other, RACK) if reverse else (RACK, other)
    assert_unmatched(estimate(requested, route=route, **evidence(observed)))


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "first,second",
    [
        ("замена пыльника переднего ШРУС", "замена пыльника заднего ШРУС"),
        ("замена передних тормозных колодок", "замена задних тормозных колодок"),
        ("замена левого пыльника ШРУС", "замена правого пыльника ШРУС"),
        (
            "замена передних колодок и задних дисков",
            "замена задних колодок и передних дисков",
        ),
        (
            "замена колодок передних и дисков задних",
            "замена колодок задних и дисков передних",
        ),
        ("замена масла Toyota Camry", "замена масла Toyota Corolla"),
        ("замена масла АКПП", "замена масла двигателя"),
        ("замена масляного фильтра", "замена воздушного фильтра"),
        ("замена наружного пыльника ШРУС", "замена внутреннего пыльника ШРУС"),
    ],
    ids=[
        "cv-front-rear",
        "brakes-front-rear",
        "cv-left-right",
        "compound-prefix-positions",
        "compound-suffix-positions",
        "vehicle-model",
        "oil-system",
        "filter-kind",
        "cv-inner-outer",
    ],
)
def test_conflicting_qualifiers_and_their_component_positions_survive_normalization(route, reverse, first, second):
    requested, observed = (second, first) if reverse else (first, second)
    assert_unmatched(estimate(requested, route=route, **evidence(observed)))


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("context_field", ["engine", "transmission"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("part", ["масла", "фильтра"])
def test_powertrain_context_cannot_erase_the_operations_system_qualifier(route, context_field, reverse, part):
    generic, specific = f"замена {part}", f"замена {part} АКПП"
    requested, observed = (specific, generic) if reverse else (generic, specific)
    assert_unmatched(estimate(requested, route=route, **evidence(observed), **{context_field: "АКПП"}))


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "first,second",
    [
        (RACK, "поменять рулевую рейку"),
        (RACK, "заменить рулевую рейку"),
        ("замена опорных подшипников стоек", "замена опорных подшипников передних стоек"),
        ("замена мехатроника DSG", "замена мехатроника 0AM"),
        ("замена тормозных колодок", "замена передних тормозных колодок"),
    ],
    ids=["rack-change", "rack-replace", "strut-bearing", "dsg-0am", "generic-front-brakes"],
)
def test_supported_action_noun_aliases_and_simple_position_compatibility_remain_usable(route, reverse, first, second):
    requested, observed = (second, first) if reverse else (first, second)
    actual = estimate(requested, route=route, **evidence(observed))
    operation = actual["operation_estimates"][0]
    assert operation["sample"]["valid_count"] == 3
    assert operation["market_average_rub"] == 4500
    assert operation["autostop_price_rub"] == 6500
    assert operation["labor_time_analysis"]["valid_count"] == 2
    assert operation["labor_time_analysis"]["average_hours"] == 2.5


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "operation",
    [
        "снятие и установка двигателя",
        "снятие, ремонт и установка рулевой рейки",
        "замена масла и фильтра с промывкой",
        "замена передних колодок и задних дисков",
        "замена рулевой рейки и масла",
    ],
)
def test_exact_complete_multi_action_operations_keep_prices_and_hours(route, operation):
    actual = estimate(operation, route=route, **evidence(operation))
    selected = actual["operation_estimates"][0]
    assert selected["sample"]["valid_count"] == 3
    assert selected["market_average_rub"] == 4500
    assert selected["labor_time_analysis"]["valid_count"] == 2
    assert selected["labor_time_analysis"]["average_hours"] == 2.5


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("context", [{"vehicle": "Toyota Camry"}, {"make": "Toyota", "model": "Camry"}])
def test_verified_vehicle_words_are_compatible_with_an_otherwise_identical_operation(route, reverse, context):
    names = (OIL, "замена масла Toyota Camry")
    requested, observed = names[::-1] if reverse else names
    actual = estimate(requested, route=route, **evidence(observed), **context)
    selected = actual["operation_estimates"][0]
    assert selected["sample"]["valid_count"] == 3
    assert selected["market_average_rub"] == 4500
    assert selected["labor_time_analysis"]["valid_count"] == 2


@pytest.mark.parametrize("route", ROUTES)
def test_context_removal_does_not_make_a_different_vehicle_model_compatible(route):
    assert_unmatched(estimate(OIL, route=route, vehicle="Toyota Camry", **evidence("замена масла Toyota Corolla")))


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "context", [{"vehicle": "Toyota Camry", "year": 2025}, {"make": "Toyota", "model": "Camry", "year": 2025}]
)
def test_car_and_year_qualifier_remains_compatible(route, reverse, context):
    names = (OIL, "замена масла Toyota Camry 2025")
    requested, observed = names[::-1] if reverse else names
    selected = estimate(requested, route=route, **evidence(observed), **context)["operation_estimates"][0]
    assert selected["sample"]["valid_count"] == 3
    assert selected["labor_time_analysis"]["valid_count"] == 2


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "part,position",
    [
        ("колодок", "передних"),
        ("дисков", "передних"),
        ("стойки", "передней"),
        ("подшипника", "переднего"),
        ("насоса", "переднего"),
        ("ШРУС", "переднего"),
    ],
)
def test_simple_known_component_keeps_optional_position(route, reverse, part, position):
    names = (f"замена {part}", f"замена {position} {part}")
    requested, observed = names[::-1] if reverse else names
    selected = estimate(requested, route=route, **evidence(observed))["operation_estimates"][0]
    assert selected["sample"]["valid_count"] == 3
    assert selected["labor_time_analysis"]["valid_count"] == 2


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("requested,observed", [(RACK + " и масла", RACK), (RACK, RACK + " и масла")])
def test_hours_use_the_callers_full_request_before_lossy_canonicalization(route, requested, observed):
    actual = estimate(requested, route=route, times=[time_row(observed, "a.example"), time_row(observed, "b.example")])
    assert_unmatched(actual)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("other", [RACK + " и насоса ГУР", "ремонт рулевой рейки", "не замена рулевой рейки"])
def test_same_source_value_and_locator_cannot_merge_different_scopes_or_transfer_parts_flags(route, reverse, other):
    rows = evidence(RACK)["quotes"]
    conflicting = quote(other, "price-0.example", 4000, includes_parts=True, labor_only=False)
    rows = [conflicting, *rows] if reverse else [*rows, conflicting]
    before = deepcopy(rows)
    actual = estimate(RACK, route=route, quotes=rows)
    assert rows == before
    selected = actual["operation_estimates"][0]
    assert selected["sample"]["valid_count"] == 3
    assert selected["sample"]["raw_matched_count"] == 3
    assert selected["market_average_rub"] == 4500
    assert actual["market_sample"]["invalid_count"] == 1
    assert selected["autostop_price_rub"] == 6500


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("reverse", [False, True])
def test_equivalent_action_aliases_cannot_turn_two_quotes_into_three(route, reverse):
    rows = [
        quote(RACK, "a.example", 4000),
        quote("поменять рулевую рейку", "a.example", 4000),
        quote(RACK, "b.example", 5000),
    ]
    if reverse:
        rows.reverse()
    actual = estimate(RACK, route=route, quotes=rows)
    selected = actual["operation_estimates"][0]
    assert selected["sample"]["raw_matched_count"] == 2
    assert selected["sample"]["regional_counts"]["krasnoyarsk"] == 2
    assert selected["market_average_rub"] is None
    assert actual["total_works_rub"] is None


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("order", list(permutations(range(3))))
@pytest.mark.parametrize(
    "names",
    [
        ["замена тормозных колодок", "замена передних тормозных колодок", "замена задних тормозных колодок"],
        ["замена пыльника ШРУС", "замена пыльника переднего ШРУС", "замена пыльника заднего ШРУС"],
    ],
    ids=["brakes", "cv-boot"],
)
def test_quote_deduplication_uses_strict_identity_not_nontransitive_position_compatibility(route, order, names):
    rows = [quote(names[index], "a.example", 4500) for index in order]
    rows += [quote(names[0], "b.example", 4500), quote(names[0], "c.example", 4500)]
    actual = estimate(names[0], route=route, quotes=rows)
    selected = actual["operation_estimates"][0]
    assert selected["sample"]["raw_matched_count"] == 5
    assert selected["sample"]["valid_count"] == 5
    assert selected["sample"]["source_count"] == 3
    assert sorted(row["operation_name"] for row in selected["sample"]["valid_quotes"]) == sorted(
        row["operation_name"] for row in rows
    )


@pytest.mark.parametrize("route", ROUTES)
def test_different_values_from_the_same_source_remain_distinct_observations(route):
    rows = [
        quote(RACK, "a.example", 4000),
        quote("поменять рулевую рейку", "a.example", 4500),
        quote(RACK, "b.example", 5000),
    ]
    actual = estimate(RACK, route=route, quotes=rows)
    selected = actual["operation_estimates"][0]
    assert selected["sample"]["valid_count"] == 3
    assert selected["sample"]["source_count"] == 2
    assert selected["market_average_rub"] == 4500


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("reverse", [False, True])
def test_same_scope_alias_duplicates_still_merge_conflicting_parts_flags_conservatively(route, reverse):
    rows = evidence(RACK)["quotes"]
    conflicting = quote("поменять рулевую рейку", "price-0.example", 4000, includes_parts=True, labor_only=False)
    rows = [conflicting, *rows] if reverse else [*rows, conflicting]
    actual = estimate(RACK, route=route, quotes=rows)
    selected = actual["operation_estimates"][0]
    assert selected["sample"]["raw_matched_count"] == 2
    assert actual["market_sample"]["invalid_count"] == 1
    assert actual["total_works_rub"] is None


def collect_synthetic(monkeypatch, requested, observed):
    monkeypatch.setattr(
        research,
        "build_public_research_queries",
        lambda **_kwargs: {"labor_prices": ["synthetic-prices"], "labor_times": ["synthetic-times"]},
    )
    calls = []

    def fake_ddg(query, **_kwargs):
        calls.append(query)
        prices = query == "synthetic-prices"
        return {
            "results": [
                {
                    "source": f"public-{index}.example",
                    "url": f"https://public-{index}.example/prices",
                    "title": "Прайс СТО",
                    "snippet": f"Красноярск. {observed}: только работа {4000 + index * 500} руб."
                    if prices
                    else f"{observed}: {2 + index} часа",
                }
                for index in range(3 if prices else 2)
            ]
        }

    monkeypatch.setattr(research, "_ddg_search", fake_ddg)
    actual = pricing.estimate_repair_work_cost(
        vehicle="Synthetic sedan",
        work_items=[requested] if requested == observed else [requested, observed],
        auto_research=True,
        use_internal_experience=False,
    )
    assert calls == ["synthetic-prices", "synthetic-times"]
    assert all(item["status"] == "ok" for item in actual["sources_checked"])
    assert [item["rows_found"] for item in actual["sources_checked"]] == [3, 2]
    assert {row["operation_name"].casefold() for row in actual["market_sample"]["quotes"]} == {observed.casefold()}
    assert {row["operation_name"].casefold() for row in actual["labor_time_sample"]["rows"]} == {observed.casefold()}
    return actual


@pytest.mark.parametrize("observed", ["замена масла и фильтра", "замена масла АКПП", "замена масла с промывкой"])
def test_real_public_collector_does_not_price_a_preserved_full_compound_as_its_component(monkeypatch, observed):
    # Listing the complete label keeps it in parser output; single-label parser loss is outside this regression.
    assert_unmatched(collect_synthetic(monkeypatch, OIL, observed))


def test_real_public_collector_keeps_an_exact_full_operation_price_and_hours(monkeypatch):
    operation = "замена масла и фильтра с промывкой"
    actual = collect_synthetic(monkeypatch, operation, operation)
    selected = actual["operation_estimates"][0]
    assert selected["sample"]["valid_count"] == 3
    assert selected["market_average_rub"] == 4500
    assert selected["labor_time_analysis"]["average_hours"] == 2.5


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("variant", ["DSG6", "DSG7", "S tronic", "0AM", "0CW", "02E", "0D9", "0GC"])
def test_dsg_family_keeps_compatibility_with_explicit_variants(route, reverse, variant):
    names = ("замена мехатроника DSG", f"замена мехатроника {variant}")
    requested, observed = names[::-1] if reverse else names
    selected = estimate(requested, route=route, **evidence(observed))["operation_estimates"][0]
    assert selected["sample"]["valid_count"] == 3
    assert selected["market_average_rub"] == 4500
    assert selected["labor_time_analysis"]["valid_count"] == 2


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("first,second", [("0AM", "02E"), ("DSG6", "DSG7"), ("DSG6 0AM", "DSG7 0AM")])
def test_explicit_transmission_identifiers_are_never_erased_by_the_family_alias(route, reverse, first, second):
    names = (f"замена мехатроника {first}", f"замена мехатроника {second}")
    requested, observed = names[::-1] if reverse else names
    assert_unmatched(estimate(requested, route=route, **evidence(observed)))


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("order", list(permutations(range(3))))
@pytest.mark.parametrize("variants", [("DSG", "DSG6", "DSG7"), ("DSG", "0AM", "02E")])
def test_transmission_family_and_distinct_variants_keep_separate_quote_identities(route, order, variants):
    names = [f"замена мехатроника {variant}" for variant in variants]
    rows = [quote(names[index], "a.example", 4500) for index in order]
    rows += [quote(names[0], "b.example", 4500), quote(names[0], "c.example", 4500)]
    actual = estimate(names[0], route=route, quotes=rows)
    selected = actual["operation_estimates"][0]
    assert selected["sample"]["raw_matched_count"] == 5
    assert selected["sample"]["valid_count"] == 5
    assert selected["sample"]["source_count"] == 3
    assert sorted(row["operation_name"] for row in selected["sample"]["valid_quotes"]) == sorted(
        row["operation_name"] for row in rows
    )


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "first,second",
    [
        ("замена мехатроника DSG", "замена мехатроника S tronic"),
        ("замена мехатроника 0AM", "Замена мехатроника 0am"),
        ("замена мехатроника DSG7", "поменять мехатроник dsg7"),
    ],
)
def test_transmission_spelling_and_action_alias_replay_cannot_create_a_third_quote(route, reverse, first, second):
    rows = [quote(first, "a.example", 4000), quote(second, "a.example", 4000), quote(first, "b.example", 5000)]
    if reverse:
        rows.reverse()
    selected = estimate(first, route=route, quotes=rows)["operation_estimates"][0]
    assert selected["sample"]["raw_matched_count"] == 2
    assert selected["sample"]["regional_counts"]["krasnoyarsk"] == 2
    assert selected["market_average_rub"] is None
    assert selected["autostop_price_rub"] is None


def estimate_public_rows(monkeypatch, requested, prices, times=(), **context):
    monkeypatch.setattr(
        research,
        "build_public_research_queries",
        lambda **_kwargs: {"labor_prices": ["synthetic-prices"], "labor_times": ["synthetic-times"]},
    )
    calls = []
    before = deepcopy((prices, times))

    def fake_ddg(query, **_kwargs):
        calls.append(query)
        return {"results": prices if query == "synthetic-prices" else list(times)}

    collected = []
    real_collect = research.collect_public_work_pricing_research

    def capture_actual_collector(**kwargs):
        result = real_collect(**kwargs)
        collected.append(result)
        return result

    monkeypatch.setattr(research, "_ddg_search", fake_ddg)
    monkeypatch.setattr(pricing, "collect_public_work_pricing_research", capture_actual_collector)
    actual = pricing.estimate_repair_work_cost(
        vehicle="Synthetic sedan",
        work_items=[requested],
        auto_research=True,
        use_internal_experience=False,
        **context,
    )
    assert (prices, times) == before
    assert calls == ["synthetic-prices", "synthetic-times"]
    assert len(collected) == 1
    assert all(item["status"] == "ok" for item in actual["sources_checked"])
    return actual, collected[0]


def public_rows_with_label(observed, field):
    groups = []
    for kind, values in (("prices", (4000, 4500, 5000)), ("times", (2, 3))):
        rows = []
        for index, value in enumerate(values):
            measurement = f"только работа {value} руб." if kind == "prices" else f"{value} часа"
            rows.append(
                {
                    "source": f"public-{index}.example",
                    "url": f"https://public-{index}.example/prices",
                    "title": observed if field == "title" else "Прайс СТО",
                    "snippet": f"Красноярск. {measurement}"
                    if field == "title"
                    else f"Красноярск. {observed}: {measurement}",
                }
            )
        groups.append(rows)
    return groups


@pytest.mark.parametrize("field", ["title", "snippet"])
@pytest.mark.parametrize(
    "operation",
    ["Замена рулевой рейки и масла", "Ремонт рулевой рейки", "Замена пыльника заднего ШРУС", "Замена масла"],
)
def test_public_collector_preserves_exact_raw_request_labels_without_canonical_scope_loss(
    monkeypatch, field, operation
):
    prices, times = public_rows_with_label(operation, field)
    actual, collected = estimate_public_rows(monkeypatch, operation, prices, times)
    assert [len(collected[key]) for key in ("quotes", "labor_time_sample")] == [3, 2]
    assert {row["operation_name"].casefold() for key in ("quotes", "labor_time_sample") for row in collected[key]} == {
        operation.casefold()
    }
    selected = actual["operation_estimates"][0]
    assert selected["sample"]["valid_count"] == 3
    assert selected["market_average_rub"] == 4500
    assert selected["labor_time_analysis"]["valid_count"] == 2
    assert selected["labor_time_analysis"]["average_hours"] == 2.5


@pytest.mark.parametrize("field", ["title", "snippet"])
@pytest.mark.parametrize(
    "requested,observed",
    [
        ("Замена рулевой рейки и масла", RACK),
        ("Ремонт рулевой рейки", RACK),
        ("Замена пыльника заднего ШРУС", "замена пыльника переднего ШРУС"),
    ],
)
def test_public_collector_cannot_relabel_a_canonical_short_source_as_the_full_request(
    monkeypatch, field, requested, observed
):
    prices, times = public_rows_with_label(observed, field)
    actual, collected = estimate_public_rows(monkeypatch, requested, prices, times)
    assert [len(collected[key]) for key in ("quotes", "labor_time_sample")] == [3, 2]
    assert {row["operation_name"].casefold() for key in ("quotes", "labor_time_sample") for row in collected[key]} == {
        observed.casefold()
    }
    assert_unmatched(actual)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("parts_in_title", [False, True])
@pytest.mark.parametrize(
    "raw,canonical,context",
    [
        ("Поменять рулевую рейку", RACK, {}),
        (
            "Замена опорных подшипников",
            "замена опорных подшипников передних стоек",
            {},
        ),
        (
            "Замена опорных подшипников Toyota Camry",
            "замена опорных подшипников передних стоек",
            {"make": "Toyota", "model": "Camry"},
        ),
    ],
    ids=["action-alias", "bearing-position", "bearing-vehicle-context"],
)
def test_public_caption_aliases_keep_parts_veto_and_labor_only_controls(
    monkeypatch, reverse, parts_in_title, raw, canonical, context
):
    title_name, snippet_name = (canonical, raw) if reverse else (raw, canonical)
    rows = [
        {
            "source": f"public-{index}.example",
            "url": f"https://public-{index}.example/prices",
            "title": title_name + (" с запчастями" if parts_in_title else ""),
            "snippet": f"Красноярск. {snippet_name}: только работа {4000 + index * 500} руб.",
        }
        for index in range(3)
    ]
    actual, collected = estimate_public_rows(monkeypatch, raw, rows, **context)
    assert len(collected["quotes"]) == 3
    assert {(row["includes_parts"], row["labor_only"]) for row in collected["quotes"]} == {
        (parts_in_title, not parts_in_title)
    }
    selected = actual["operation_estimates"][0]
    assert selected["sample"]["valid_count"] == (0 if parts_in_title else 3)
    assert selected["market_average_rub"] == (None if parts_in_title else 4500)
    assert actual["market_sample"]["invalid_count"] == (3 if parts_in_title else 0)
