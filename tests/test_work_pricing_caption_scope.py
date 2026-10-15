from __future__ import annotations

import pytest

from autostop_manager import automotive_labor as labor, work_pricing as pricing, work_pricing_research as research

OPERATION = "замена масла"
OPERATIONS = [{"normalized_name": OPERATION, "aliases": ["смена масла"]}]
MEASUREMENTS = [("labor_prices", "Только работа 5000 руб."), ("labor_times", "Норма времени 2 часа.")]


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


def parse(title, snippet, kind="labor_prices", *, operations=OPERATIONS, source="a.example"):
    row = {"title": title, "snippet": snippet, "source": source, "url": f"https://{source}/prices"}
    return research.parse_public_work_evidence(row, "synthetic query", operations, kind)[0]


@pytest.mark.parametrize(
    "title",
    [
        "Замена масла и запчастей",
        "Замена масла и материалов",
        "Замена масла плюс запчастей",
        "Замена масла + материалов",
        "Замена масла, материалы",
        "Запчасти и замена масла",
        "Материалы: замена масла",
        "Работа замена масла и запчастей",
        "Без замена масла материалов",
        "Замена масла включая материалы",
        "Замена масла только работающий",
    ],
)
@pytest.mark.parametrize("kind,snippet", MEASUREMENTS)
def test_caption_does_not_discard_a_separate_parts_subject(title, kind, snippet):
    rows = parse(title, snippet, kind)
    assert len(rows) == 1
    assert rows[0]["operation_name"] == ""
    assert rows[0]["source"]["primary_lineage"] == "a.example"
    assert rows[0]["source"]["locator"] == "https://a.example/prices"
    if kind == "labor_prices":
        assert (rows[0]["price_rub"], rows[0]["includes_parts"], rows[0]["labor_only"]) == (5000, False, True)
    else:
        assert (rows[0]["hours"], rows[0]["range_hours"]) == (2.0, [2.0, 2.0])


@pytest.mark.parametrize("noun", ["Запчасти", "Материалы", "ЗАПЧАСТИ И МАТЕРИАЛЫ"])
@pytest.mark.parametrize("kind,measurement", MEASUREMENTS)
def test_caption_does_not_inherit_a_title_for_another_snippet_subject(noun, kind, measurement):
    rows = parse("Замена масла", f"{noun}: {measurement}", kind)
    assert len(rows) == 1 and rows[0]["operation_name"] == ""


@pytest.mark.parametrize(
    "scope",
    [
        "с запчастями и материалами",
        "с материалами плюс запчастями",
        "включая запчасти, материалы",
        "работа и запчасти + материалы",
        "работа плюс запчасти / материалы",
        "работа + запчасти и материалы",
        "работа с запчастями и материалами",
    ],
)
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("style", ["ordinary", "uppercase-nbsp"])
def test_known_included_scope_keeps_caption_binding_and_parts_veto(scope, explicit, style):
    if style == "uppercase-nbsp":
        scope = scope.upper().replace(" ", "\u00a0")
    rows = parse("Смена масла " + scope, (OPERATION + ": " if explicit else "") + "Только работа 5000 руб.")
    assert len(rows) == 1
    assert (rows[0]["operation_name"], rows[0]["price_rub"]) == (OPERATION, 5000)
    assert (rows[0]["includes_parts"], rows[0]["labor_only"]) == (True, False)


@pytest.mark.parametrize(
    "scope",
    [
        "без запчастей и материалов",
        "без учета запчастей, материалов",
        "работа без материалов плюс запчастей",
        "запчасти не включены",
        "материалы не включены",
        "запчасти и материалы не включены",
        "материалы + запчасти не включены",
    ],
)
@pytest.mark.parametrize("kind,measurement", MEASUREMENTS)
def test_known_excluded_scope_preserves_caption_binding(scope, kind, measurement):
    rows = parse("Смена масла " + scope, "Красноярск. " + measurement, kind)
    assert len(rows) == 1 and rows[0]["operation_name"] == OPERATION
    assert rows[0]["city"] == "Красноярск"


@pytest.mark.parametrize("scope", ["без учета запчастей и материалов", "материалы не включены"])
def test_title_does_not_supply_positive_labor_flags(scope):
    row = parse("Замена масла " + scope, "Цена 5000 руб.")[0]
    assert row["operation_name"] == OPERATION
    assert (row["includes_parts"], row["labor_only"]) == (None, False)


@pytest.mark.parametrize("label", ["Замена масла и материалов", "Работа замена масла и запчастей"])
@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize("kind,measurement", MEASUREMENTS)
def test_full_explicit_operation_name_or_alias_remains_observed(label, alias, kind, measurement):
    name = "обслуживание двигателя" if alias else label.casefold()
    operation = {"normalized_name": name, "aliases": [label] if alias else []}
    rows = parse(label, measurement, kind, operations=[*OPERATIONS, operation])
    assert len(rows) == 1 and rows[0]["operation_name"] == name


@pytest.mark.parametrize("bad_caption", [False, True])
@pytest.mark.parametrize("kind,measurement", MEASUREMENTS)
def test_caption_scope_reaches_actual_price_and_time_consumers(bad_caption, kind, measurement):
    title = "Замена масла" + (" и материалов" if bad_caption else " без учета запчастей и материалов")
    observations = [
        row
        for source in ("a.example", "b.example", "c.example")
        for row in parse(title, "Красноярск. " + measurement, kind, source=source)
    ]
    evidence = [research.legacy_work_evidence_row(row) for row in observations]
    legacy = pricing.estimate_repair_work_cost(
        make="DEMO",
        work_items=[OPERATION],
        quotes_json=evidence if kind == "labor_prices" else {"labor_time_sample": evidence},
        auto_research=False,
        use_internal_experience=False,
    )
    expected_legacy = 7300 if not bad_caption and kind == "labor_prices" else None
    assert legacy["autostop_price_rub"] == expected_legacy
    if kind == "labor_prices":
        pure = labor.calculate_work_price(
            [{"operation_name": OPERATION}],
            {"basis": "public_observations", "version": "test-v1"},
            observations=observations,
        )
        assert pure["data"]["total"] == (None if bad_caption else 5000)
    else:
        assert legacy["recommended_total_works_rub"] == (None if bad_caption else 8000)
        assert legacy["operation_estimates"][0]["labor_time_analysis"]["valid_count"] == (0 if bad_caption else 3)
