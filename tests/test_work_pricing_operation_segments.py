from __future__ import annotations

import pytest

from autostop_manager import automotive_labor as labor, work_pricing as pricing, work_pricing_research as research

OIL = "замена масла"
CANDLES = "замена свечей"
OPERATIONS = [{"normalized_name": name} for name in (OIL, CANDLES)]


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


def parse(text, *, kind="labor_prices", operations=OPERATIONS, field="snippet", title="Прайс СТО"):
    row = {"source": "synthetic.example", "url": "https://synthetic.example/prices", "title": title, "snippet": ""}
    row[field] = text
    return research.parse_public_work_evidence(row, "посторонний запрос", operations, kind)[0]


def values(rows, field):
    return [(row["operation_name"], row[field]) for row in rows]


@pytest.mark.parametrize("separator", [", ", "; ", "\n", " | ", " • ", " / ", "\u2028"])
@pytest.mark.parametrize("field", ["title", "snippet"])
@pytest.mark.parametrize("operations", [OPERATIONS, OPERATIONS[::-1], OPERATIONS[:1]])
@pytest.mark.parametrize(
    "kind, first, second, value_field, expected",
    [
        ("labor_prices", "1000 руб. с запчастями", "5000 руб. только работа", "price_rub", [1000, 5000]),
        ("labor_times", "1,5–2,0 часа", "3 часа", "hours", [1.75, 3.0]),
    ],
)
def test_reverse_rows_bind_each_measurement_to_its_own_operation(
    separator, field, operations, kind, first, second, value_field, expected
):
    text = f"{first} — {OIL}{separator}{second} — {CANDLES}"
    rows = parse(text, kind=kind, operations=operations, field=field)
    second_name = CANDLES if len(operations) == 2 else ""
    assert values(rows, value_field) == [(OIL, expected[0]), (second_name, expected[1])]
    if kind == "labor_prices":
        assert [(row["includes_parts"], row["labor_only"]) for row in rows] == [(True, False), (False, True)]


@pytest.mark.parametrize(
    "text, expected",
    [
        (f"{OIL}: 1000 руб., 5000 руб. — {CANDLES}", [(OIL, 1000), (CANDLES, 5000)]),
        (f"1000 руб. — {OIL}, {CANDLES}: 5000 руб.", [(OIL, 1000), (CANDLES, 5000)]),
        (f"{OIL}: 1000 руб. только работа {CANDLES}: 5000 руб.", [(OIL, 1000), (CANDLES, 5000)]),
        (f"{OIL}: 1000 руб., {CANDLES}: 5000 руб.", [(OIL, 1000), (CANDLES, 5000)]),
    ],
)
def test_forward_and_mixed_records_keep_clear_bindings(text, expected):
    assert values(parse(text), "price_rub") == expected


@pytest.mark.parametrize(
    "text",
    [
        f"{OIL} 1000 руб., 1,5 часа, {CANDLES} 5000 руб., 3 часа",
        f"1000 руб., 1,5 часа — {OIL}, 5000 руб., 3 часа — {CANDLES}",
    ],
)
def test_price_and_time_pairs_survive_record_boundaries(text):
    assert values(parse(text), "price_rub") == [(OIL, 1000), (CANDLES, 5000)]
    assert values(parse(text, kind="labor_times"), "hours") == [(OIL, 1.5), (CANDLES, 3.0)]


@pytest.mark.parametrize("text", [f"1000 руб. — {OIL}", f"{OIL}, только работа, 1000 руб."])
def test_a_single_operation_keeps_prefixes_and_scope_commas(text):
    assert values(parse(text), "price_rub") == [(OIL, 1000)]


@pytest.mark.parametrize(
    "kind, first, second, field, expected",
    [
        ("labor_prices", "200 руб.", "5000 руб.", "price_rub", 5000),
        ("labor_prices", "-1000 руб.", "5000 руб.", "price_rub", 5000),
        ("labor_times", "-1,5 часа", "3,5 часа", "hours", 3.5),
        ("labor_times", "90 часов", "3,5 часа", "hours", 3.5),
        ("labor_times", "н/ч -2", "3,5 часа", "hours", 3.5),
        ("labor_times", "норма времени -2", "3,5 часа", "hours", 3.5),
    ],
)
def test_rejected_prefix_values_do_not_shift_next_operation(kind, first, second, field, expected):
    rows = parse(f"{first} — {OIL}, {second} — {CANDLES}", kind=kind)
    assert values(rows, field) == [(CANDLES, expected)]


@pytest.mark.parametrize("operations", [OPERATIONS, OPERATIONS[:1]])
def test_equal_prices_do_not_collapse_across_operations_or_borrow_labor_scope(operations):
    rows = parse(f"{OIL}: с запчастями 1000 руб., 1000 руб. только работа — {CANDLES}", operations=operations)
    assert values(rows, "price_rub") == [(OIL, 1000), (CANDLES if len(operations) == 2 else "", 1000)]
    assert [(row["includes_parts"], row["labor_only"]) for row in rows] == [(True, False), (False, True)]


@pytest.mark.parametrize("operation", ["снятие и установка двигателя", "снятие, ремонт и установка рейки"])
@pytest.mark.parametrize("reverse", [False, True])
def test_full_multi_action_labels_remain_one_record(operation, reverse):
    text = f"1000 руб. — {operation}" if reverse else f"{operation}: 1000 руб."
    assert values(parse(text, operations=[{"normalized_name": operation}]), "price_rub") == [(operation, 1000)]


def test_reverse_alias_and_long_operation_keep_full_names():
    operations = [{"normalized_name": "капитальный ремонт двигателя", "aliases": ["капремонт двигателя"]}]
    assert values(parse("5000 руб. — капремонт двигателя", operations=operations), "price_rub") == [
        ("капитальный ремонт двигателя", 5000)
    ]


@pytest.mark.parametrize("operations", [OPERATIONS, OPERATIONS[:1]])
@pytest.mark.parametrize(
    "kind, measurement, field, expected",
    [
        ("labor_prices", "1000 руб.", "price_rub", 1000),
        ("labor_times", "1,5 часа", "hours", 1.5),
    ],
)
def test_ambiguous_measurement_between_labels_is_preserved_unbound(operations, kind, measurement, field, expected):
    rows = parse(f"{OIL} — {measurement} — {CANDLES}", kind=kind, operations=operations, title=OIL)
    assert values(rows, field) == [("", expected)]


def test_repeated_ambiguous_measurement_cannot_gain_name_after_deduplication():
    rows = parse(f"{OIL} — 1000 руб. и 1000 руб. только работа — {CANDLES}", operations=OPERATIONS[:1], title=OIL)
    assert rows and all(row["operation_name"] == "" for row in rows)


def test_ambiguous_time_between_two_priced_operations_is_not_guessed():
    text = f"{OIL} 1000 руб., 1,5 часа, {CANDLES} 5000 руб."
    assert values(parse(text, kind="labor_times"), "hours") == [("", 1.5)]


@pytest.mark.parametrize("operations", [OPERATIONS, OPERATIONS[:1]])
@pytest.mark.parametrize("evidence_field", ["title", "snippet"])
@pytest.mark.parametrize(
    "text, kind, field, expected",
    [
        (f"{OIL}: только работа 1000 руб., {CANDLES}", "labor_prices", "price_rub", 1000),
        (f"{OIL}: только работа 1000 руб. {CANDLES}:", "labor_prices", "price_rub", 1000),
        (f"{OIL}: 1000 руб., 1 час {CANDLES}:", "labor_times", "hours", 1.0),
        (f"Норма времени {OIL}: 1 час {CANDLES}:", "labor_times", "hours", 1.0),
        (f"Норма времени {OIL}: 1 {CANDLES}:", "labor_times", "hours", 1.0),
    ],
)
def test_empty_trailing_heading_cannot_steal_a_forward_records_value(
    text, kind, field, expected, operations, evidence_field
):
    assert values(parse(text, kind=kind, operations=operations, field=evidence_field), field) == [(OIL, expected)]


@pytest.mark.parametrize("evidence_field", ["title", "snippet"])
@pytest.mark.parametrize("all_operations", [False, True])
@pytest.mark.parametrize(
    "text, kind, field, expected",
    [
        (f"{OIL}: нет данных, {CANDLES}: только работа 5000 руб.", "labor_prices", "price_rub", [(CANDLES, 5000)]),
        (f"{OIL}: цена по запросу, {CANDLES}: 2 часа", "labor_times", "hours", [(CANDLES, 2.0)]),
        (
            f"{OIL}: 1000 руб., {CANDLES}: нет данных, замена колодок: 5000 руб.",
            "labor_prices",
            "price_rub",
            [(OIL, 1000), ("замена колодок", 5000)],
        ),
        (f"{OIL}: 1000 руб., {CANDLES}:, замена колодок:", "labor_prices", "price_rub", [(OIL, 1000)]),
    ],
)
def test_explicit_empty_first_and_middle_records_preserve_other_measurements(
    text, kind, field, expected, all_operations, evidence_field
):
    names = [OIL, CANDLES, "замена колодок"] if all_operations else [name for name, _ in expected]
    operations = [{"normalized_name": name} for name in names]
    assert values(parse(text, kind=kind, operations=operations, field=evidence_field), field) == expected


@pytest.mark.parametrize("prefix", ["н/ч", "норма времени", "время выполнения", "трудоемкость"])
@pytest.mark.parametrize("measurement, expected", [("2 часа", 2.0), ("2–3 часа", 2.5)])
@pytest.mark.parametrize("field", ["title", "snippet"])
@pytest.mark.parametrize("operations", [OPERATIONS, OPERATIONS[1:]])
def test_time_prefix_of_an_empty_record_cannot_capture_the_next_records_unit(
    prefix, measurement, expected, field, operations
):
    text = f"{OIL}: {prefix}, {CANDLES}: {measurement}"
    rows = parse(text, kind="labor_times", field=field, operations=operations)
    assert values(rows, "hours") == [(CANDLES, expected)]


@pytest.mark.parametrize("text", [f"{OIL} 1000 руб., {CANDLES} 2 часа", f"1000 руб. — {OIL}, 2 часа — {CANDLES}"])
def test_separate_price_and_hours_records_do_not_become_an_ambiguous_single_pair(text):
    assert values(parse(text), "price_rub") == [(OIL, 1000)]
    assert values(parse(text, kind="labor_times"), "hours") == [(CANDLES, 2.0)]


def test_empty_middle_exception_needs_its_own_forward_cue():
    text = f"{OIL}: 1000 руб., {CANDLES}, 3000 руб. — замена колодок"
    operations = [*OPERATIONS, {"normalized_name": "замена колодок"}]
    assert values(parse(text, operations=operations), "price_rub") == [("", 1000), ("", 3000)]


def test_a_forward_heading_cannot_take_a_preceding_hours_value():
    text = f"{OIL}: 1000 руб., 1 час {CANDLES}: 5000 руб."
    assert values(parse(text, kind="labor_times"), "hours") == [(OIL, 1.0)]


@pytest.mark.parametrize(
    "text, kind, field, expected",
    [
        (f"{OIL} — 1000 руб. — {CANDLES}", "labor_prices", "price_rub", 1000),
        (f"{OIL}: 1000 руб., 1 час {CANDLES}", "labor_times", "hours", 1.0),
    ],
)
def test_empty_heading_exception_does_not_resolve_other_ambiguous_records(text, kind, field, expected):
    assert values(parse(text, kind=kind), field) == [("", expected)]


@pytest.mark.parametrize("append_record", [False, True])
@pytest.mark.parametrize(
    "kind, first, second, field, expected",
    [
        ("labor_prices", "1000 руб.", "5000 руб.", "price_rub", [1000, 5000]),
        ("labor_times", "1 час", "3 часа", "hours", [1.0, 3.0]),
    ],
)
def test_forward_header_rejects_prefix_value_independently_of_later_records(
    append_record, kind, first, second, field, expected
):
    text = f"{OIL} — {first} — {CANDLES}:"
    if append_record:
        text += f", замена колодок: {second}"
    operations = [*OPERATIONS, {"normalized_name": "замена колодок"}]
    expected_rows = [(OIL, expected[0])]
    if append_record:
        expected_rows.append(("замена колодок", expected[1]))
    assert values(parse(text, kind=kind, operations=operations), field) == expected_rows


def test_excessive_boundary_alternatives_preserve_unbound_observations():
    text = f"{OIL}: 1000 руб." + ", примечание" * 70 + f", {CANDLES}: 5000 руб."
    assert values(parse(text), "price_rub") == [("", 1000), ("", 5000)]


@pytest.mark.parametrize("parts_first", [False, True])
def test_reverse_operation_prices_reach_both_real_calculators(parts_first):
    observations = []
    first_scope = "с запчастями" if parts_first else "только работа"
    second_scope = "только работа" if parts_first else "с запчастями"
    for host in ("a.example", "b.example", "c.example"):
        row = {
            "source": host,
            "url": f"https://{host}/prices",
            "title": "Прайс СТО",
            "snippet": f"Красноярск. 1000 руб. {first_scope} — {OIL}, 5000 руб. {second_scope} — {CANDLES}",
        }
        observations.extend(research.parse_public_work_evidence(row, "цена", OPERATIONS[:1], "labor_prices")[0])
    legacy = pricing.estimate_repair_work_cost(
        make="DEMO",
        work_items=[OIL],
        quotes_json=[research.legacy_work_evidence_row(row) for row in observations],
        auto_research=False,
        use_internal_experience=False,
    )
    pure = labor.calculate_work_price(
        [{"operation_name": OIL}], {"basis": "public_observations", "version": "test"}, observations=observations
    )
    assert (legacy["autostop_price_rub"], pure["data"]["total"]) == ((None, None) if parts_first else (1500, 1000))


def test_reverse_hours_do_not_inflate_the_real_fallback_estimate():
    rows = parse(f"1 час — {OIL}, 3 часа — {CANDLES}", kind="labor_times", operations=OPERATIONS[:1])
    result = pricing.estimate_repair_work_cost(
        make="DEMO",
        work_items=[OIL],
        quotes_json={"labor_time_sample": [research.legacy_work_evidence_row(row) for row in rows]},
        auto_research=False,
        use_internal_experience=False,
    )
    assert result["operation_estimates"][0]["labor_time_analysis"]["average_hours"] == 1.0
    assert result["recommended_total_works_rub"] == 4000


def test_empty_first_record_does_not_erase_the_real_calculators_price():
    observations = []
    for host in ("a.example", "b.example", "c.example"):
        row = {
            "source": host,
            "url": f"https://{host}/prices",
            "title": "Прайс СТО",
            "snippet": f"Красноярск. {OIL}: нет данных, {CANDLES}: только работа 5000 руб.",
        }
        observations.extend(research.parse_public_work_evidence(row, "цена", OPERATIONS, "labor_prices")[0])
    legacy = pricing.estimate_repair_work_cost(
        make="DEMO",
        work_items=[CANDLES],
        quotes_json=[research.legacy_work_evidence_row(row) for row in observations],
        auto_research=False,
        use_internal_experience=False,
    )
    pure = labor.calculate_work_price(
        [{"operation_name": CANDLES}], {"basis": "public_observations", "version": "test"}, observations=observations
    )
    assert (legacy["autostop_price_rub"], pure["data"]["total"]) == (7300, 5000)
