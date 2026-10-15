from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from html import escape
from io import BytesIO
import socket

import pytest

from autostop_manager import automotive_labor as labor, work_pricing as pricing, work_pricing_research as research
from autostop_manager.work_operation_matching import OperationMatcher


OIL = "замена масла"
CONTEXT = {"make": "Toyota", "model": "Camry", "year": 2020}
MEASUREMENTS = [("labor_prices", "5000 руб. только работа"), ("labor_times", "2 часа")]
MONEY_SYMBOLS = ["$", "€", "£", "¥", "₸", "₴", "₩", "₽"]


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, tmp_path):
    def denied(*_args, **_kwargs):
        raise AssertionError("implicit private or network read forbidden")

    monkeypatch.setattr(pricing, "_load_labor_experience", denied)
    monkeypatch.setattr(research, "urlopen", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "manager.sqlite3"))
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "j1"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "vin"))


def observed_fields(label, measurement, style):
    if style == "caption":
        return label, "Красноярск. " + measurement
    if style == "title":
        return label + ": " + measurement, ""
    return "Прайс СТО", "Красноярск. " + label + ": " + measurement


def parse(label, measurement, kind, style, *, operations=None, context=None):
    title, snippet = observed_fields(label, measurement, style)
    row = {"title": title, "snippet": snippet, "source": "scope.example", "url": "https://scope.example/prices"}
    return research.parse_public_work_evidence(
        row,
        "synthetic query",
        operations or [{"normalized_name": OIL, "input": OIL}],
        kind,
        operation_matcher=OperationMatcher(context),
    )[0]


def install_html(monkeypatch, title, snippet):
    document = "".join(
        f'<div class="result"><a class="result__a" href="https://source{i}.example/prices">'
        f'{escape(title)}</a><div class="result__snippet">{escape(snippet)}</div></div>'
        for i in range(3)
    ).encode()
    calls = []

    def fake_gateway(request, *, timeout):
        assert request.full_url.startswith("https://html.duckduckgo.com/html/?q=")
        assert timeout > 0
        calls.append(request.full_url)
        return closing(BytesIO(document))

    monkeypatch.setattr(research, "urlopen", fake_gateway)
    return calls


def collect(route, requested, context=CONTEXT):
    if route == "new":
        return labor.collect_work_price_evidence([requested], context, max_queries=4)
    return research.collect_public_work_pricing_research(
        vehicle_context=context,
        operations=[{"normalized_name": requested, "input": requested}],
        city="Красноярск",
        auto_research=True,
    )


def evidence_rows(route, acquired):
    if route == "new":
        return acquired["data"]["observations"], acquired["data"]["labor"]
    return acquired["quotes"], acquired["labor_time_sample"]


def estimate(route, requested, acquired, context=CONTEXT):
    kwargs = {**context, "work_items": [requested], "auto_research": False, "use_internal_experience": False}
    if route == "new":
        return pricing.estimate_repair_work_cost(**kwargs, price_evidence=acquired)
    return pricing.estimate_repair_work_cost(**kwargs, quotes_json=acquired)


def pure_price(requested, observations):
    return labor.calculate_work_price(
        [{"operation_name": requested}],
        {"basis": "public_observations", "version": "observed-scope-test-v1"},
        observations=observations,
    )["data"]["total"]


@pytest.mark.parametrize(
    "observed",
    [
        "замена масла и масляного фильтра",
        "замена масла + фильтра",
        "замена масла, фильтра",
        "замена масла / фильтра",
        "замена масла с очисткой поддона",
        "замена масла и проверка свечей",
        "не замена масла",
        "замена масла не требуется",
        "замена масла без",
        "замена масла и",
        "замена масла +",
        "замена масла /",
        "замена масла без запчастей и",
        "замена масла без запчастей и свечей",
        "замена масла 123",
        "замена масла 2005",
        "замена масла 冷却器",
        "замена масла φίλτρο",
        "фильтр и замена масла",
    ],
)
@pytest.mark.parametrize("kind,measurement", MEASUREMENTS)
@pytest.mark.parametrize("style", ["inline", "title", "caption"])
def test_unexplained_observed_scope_is_discovery_only(observed, kind, measurement, style):
    rows = parse(observed, measurement, kind, style, context=CONTEXT)
    assert rows
    assert all(row["operation_name"] == "" for row in rows)
    assert all(row["public_source"] is True for row in rows)
    assert rows[0]["source"]["locator"] == "https://scope.example/prices"


@pytest.mark.parametrize(
    "observed", ["Без замена масла запчастей", "запчасти не замена масла включены", "только замена масла работа"]
)
@pytest.mark.parametrize("kind,measurement", MEASUREMENTS)
@pytest.mark.parametrize("style", ["inline", "caption"])
def test_removing_operation_cannot_create_a_new_metadata_phrase(observed, kind, measurement, style):
    rows = parse(observed, measurement, kind, style)
    assert rows and all(row["operation_name"] == "" for row in rows)


@pytest.mark.parametrize(
    "label",
    [
        "замена масла без снятия защиты",
        "замена масла и материалов",
        "снятие и установка двигателя",
        "обслуживание ТО 2",
        "замена рулевой рейки и замена масла",
    ],
)
@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize("style", ["inline", "caption"])
def test_full_declared_operation_protects_its_internal_words(label, alias, style):
    target = "обслуживание двигателя" if alias else label
    operations = [{"normalized_name": target, "aliases": [label] if alias else []}]
    for kind in ("labor_prices", "labor_times"):
        rows = parse(label, "5000 руб. только работа, 1–2 часа", kind, style, operations=operations)
        assert len(rows) == 1 and rows[0]["operation_name"] == target
        if kind == "labor_prices":
            assert (rows[0]["price_rub"], rows[0]["includes_parts"], rows[0]["labor_only"]) == (5000, False, True)
        else:
            assert rows[0]["range_hours"] == [1, 2]


@pytest.mark.parametrize(
    "scope", ["без учета запчастей и материалов", "запчасти не включены", "с запчастями и материалами"]
)
@pytest.mark.parametrize("style", ["inline", "caption"])
def test_complete_scope_phrase_remains_metadata(scope, style):
    rows = parse(OIL + " " + scope, "5000 руб. только работа", "labor_prices", style)
    assert len(rows) == 1 and rows[0]["operation_name"] == OIL
    expected = (True, False) if scope.startswith("с ") else (False, True)
    assert (rows[0]["includes_parts"], rows[0]["labor_only"]) == expected


@pytest.mark.parametrize("route", ["legacy", "new"])
@pytest.mark.parametrize("kind,measurement", MEASUREMENTS)
@pytest.mark.parametrize("style", ["inline", "caption"])
@pytest.mark.parametrize(
    "observed",
    [
        "замена масла и масляного фильтра",
        "не замена масла",
        "замена масла без запчастей и",
        "замена масла 2005",
        "замена масла 冷却器",
    ],
)
def test_unknown_scope_from_html_never_prices_single_requested_work(
    monkeypatch, route, kind, measurement, style, observed
):
    calls = install_html(monkeypatch, *observed_fields(observed, measurement, style))
    acquired = collect(route, OIL)
    snapshot = deepcopy(acquired)
    before = len(calls)
    result = estimate(route, OIL, acquired)
    observations, hours = evidence_rows(route, acquired)
    rows = observations if kind == "labor_prices" else hours
    assert rows and all(row["operation_name"] == "" for row in rows)
    assert result["recommended_total_works_rub"] is None
    assert pure_price(OIL, observations) is None
    assert acquired == snapshot and len(calls) == before == 4


@pytest.mark.parametrize("route", ["legacy", "new"])
@pytest.mark.parametrize(
    "requested,observed,conflict,qualifier",
    [
        (
            "замена тормозных колодок",
            "замена тормозных колодок передних",
            "замена задних тормозных колодок",
            "передних",
        ),
        (
            "замена тормозных колодок",
            "замена передних тормозных колодок",
            "замена задних тормозных колодок",
            "передних",
        ),
        ("замена мехатроника DSG", "замена мехатроника DSG 0AM", "замена мехатроника DSG 0CW", "0AM"),
    ],
)
def test_acquisition_preserves_actual_qualifier_when_evidence_is_reused(
    monkeypatch, route, requested, observed, conflict, qualifier
):
    calls = install_html(monkeypatch, *observed_fields(observed, "5000 руб. только работа, 2 часа", "inline"))
    acquired = collect(route, requested)
    observations, hours = evidence_rows(route, acquired)
    assert observations and hours
    assert all(qualifier.casefold() in row["operation_name"].casefold() for row in observations + hours)
    assert estimate(route, requested, acquired)["recommended_total_works_rub"] == 7300
    assert pure_price(requested, observations) == 5000
    assert pure_price(conflict, observations) is None
    # Change only the request envelope; captured evidence itself is reused intact.
    replay = deepcopy(acquired)
    if route == "new":
        replay["data"]["work_items"] = [conflict]
    snapshot = deepcopy(replay)
    result = estimate(route, conflict, replay)
    assert result["recommended_total_works_rub"] is None
    assert result["operation_estimates"][0]["sample"]["valid_count"] == 0
    assert result["operation_estimates"][0]["labor_time_analysis"]["valid_count"] == 0
    assert replay == snapshot and len(calls) == 4


@pytest.mark.parametrize("route", ["legacy", "new"])
@pytest.mark.parametrize("style", ["inline", "caption"])
def test_known_vehicle_context_is_shared_by_collectors_and_consumers(monkeypatch, route, style):
    calls = install_html(
        monkeypatch, *observed_fields(OIL + " Toyota Camry 2020", "5000 руб. только работа, 1–2 часа", style)
    )
    acquired = collect(route, OIL)
    observations, hours = evidence_rows(route, acquired)
    assert observations and hours
    assert all(row["operation_name"] == OIL for row in observations + hours)
    assert estimate(route, OIL, acquired)["recommended_total_works_rub"] == 7300
    assert pure_price(OIL, observations) == 5000
    assert len(calls) == 4


@pytest.mark.parametrize("route", ["legacy", "new"])
def test_short_title_parts_veto_survives_known_vehicle_suffix(monkeypatch, route):
    requested = "замена рулевой рейки Toyota Camry"
    install_html(monkeypatch, "замена рулевой рейки с запчастями", f"Красноярск. {requested}: 5000 руб. только работа")
    acquired = collect(route, requested)
    observations, _ = evidence_rows(route, acquired)
    assert observations and all(row["operation_name"] == requested for row in observations)
    assert all((row["includes_parts"], row["labor_only"]) == (True, False) for row in observations)
    assert estimate(route, requested, acquired)["recommended_total_works_rub"] is None
    assert pure_price(requested, observations) is None


@pytest.mark.parametrize("route", ["legacy", "new"])
@pytest.mark.parametrize("requested", ["замена масла без снятия защиты", "замена рулевой рейки и замена масла"])
def test_full_complex_html_evidence_replays_without_network_or_mutation(monkeypatch, route, requested):
    calls = install_html(monkeypatch, *observed_fields(requested, "5000 руб. только работа, 1–2 часа", "caption"))
    acquired = collect(route, requested)
    snapshot = deepcopy(acquired)
    observations, hours = evidence_rows(route, acquired)
    assert observations and hours
    assert all(row["operation_name"] == requested for row in observations + hours)
    assert all(row["range_hours"] == [1, 2] for row in hours)
    for row in observations:
        source = row.get("evidence_source") or row["source"]
        assert source["provider"] == "public_web"
        assert source["locator"].endswith(".example/prices")
        assert source["primary_lineage"].endswith(".example")
        assert source["method"] == "public_search" and source["fetched_at"]
        assert row["captured_at"] and row["public_source"] is True
        assert row["confidence"] == "low"
        assert (row["includes_parts"], row["labor_only"]) == (False, True)
    for _ in range(2):
        assert estimate(route, requested, acquired)["recommended_total_works_rub"] == 7300
        assert pure_price(requested, observations) == 5000
    assert acquired == snapshot and len(calls) == 4


@pytest.mark.parametrize(
    "kind,measurement,bound",
    [
        ("labor_prices", "1000 руб. и 1000 руб. только работа", False),
        ("labor_times", "2 часа и 2 часа", False),
        ("labor_times", "норма времени 0, 2 часа", False),
        ("labor_times", "норма времени 1, только работа", True),
        ("labor_prices", "5000 руб. только работа, 1–2 часа", True),
        ("labor_times", "5000 руб. только работа, 1–2 часа", True),
    ],
)
def test_scope_resolution_keeps_raw_measurement_cardinality(kind, measurement, bound):
    rows = parse(OIL, measurement, kind, "inline")
    assert rows
    assert all(row["operation_name"] == (OIL if bound else "") for row in rows)


@pytest.mark.parametrize("observed", ["замена масла АКПП", "замена масла 0CW", "замена масла 2019"])
def test_free_form_vehicle_fields_do_not_erase_technical_operation_qualifiers(observed):
    context = {**CONTEXT, "engine": "АКПП", "transmission": "0CW"}
    rows = parse(observed, "5000 руб. только работа", "labor_prices", "inline", context=context)
    assert rows and all(row["operation_name"] == "" for row in rows)


@pytest.mark.parametrize(
    "requested,observed,expected",
    [
        ("замена тормозных колодок", "замена передних тормозных колодок", 5000),
        ("замена передних тормозных колодок", "замена тормозных колодок", 5000),
        ("замена передних тормозных колодок", "замена передних левых тормозных колодок", 5000),
        ("замена мехатроника DSG", "замена мехатроника DSG 7", 5000),
        ("замена мехатроника DSG", "замена мехатроника DSG 0AM", 5000),
        ("замена передних тормозных колодок", "замена задних тормозных колодок", None),
        ("замена мехатроника DSG 6", "замена мехатроника DSG 7", None),
        ("замена мехатроника DSG 7", "замена мехатроника DSG 0AM", None),
        (OIL, "замена масла и фильтра", None),
        ("проверка свечей", "проверка передних свечей", None),
        (OIL, "ЗАМЕНА МАСЛА", None),
        (OIL, "смена масла", None),
        ("замена передних тормозных колодок", "смена передних тормозных колодок", None),
        (OIL, OIL, 5000),
    ],
)
def test_pure_price_consumer_accepts_only_known_compatible_added_qualifiers(requested, observed, expected):
    observations = [{"operation_name": observed, "price_rub": 5000, "labor_only": True, "includes_parts": False}]
    snapshot = deepcopy(observations)
    assert pure_price(requested, observations) == expected
    assert observations == snapshot


@pytest.mark.parametrize("currency", ["$", "€"])
@pytest.mark.parametrize("kind,measurement", MEASUREMENTS)
@pytest.mark.parametrize("style", ["inline", "title", "caption"])
def test_foreign_amount_is_not_positive_operation_metadata(currency, kind, measurement, style):
    rows = parse(f"{OIL} 1000 {currency}", measurement, kind, style)
    assert len(rows) == 1 and rows[0]["operation_name"] == ""
    assert rows[0]["public_source"] is True
    assert rows[0]["source"]["locator"] == "https://scope.example/prices"
    if kind == "labor_prices":
        assert rows[0]["price_rub"] == 5000
    else:
        assert rows[0]["range_hours"] == [2, 2]


@pytest.mark.parametrize("currency", ["$", "€"])
@pytest.mark.parametrize("residual", ["0CW", "и фильтра", "и проверка свечей"])
@pytest.mark.parametrize("kind,measurement", MEASUREMENTS)
def test_unknown_scope_after_foreign_title_amount_cannot_supply_caption_operation(
    currency, residual, kind, measurement
):
    rows = parse(f"{OIL} 1000 {currency} {residual}", measurement, kind, "caption")
    assert rows and all(row["operation_name"] == "" for row in rows)
    assert all(row["source"]["primary_lineage"] == "scope.example" for row in rows)


@pytest.mark.parametrize("currency", ["$", "€"])
@pytest.mark.parametrize("residual", ["0CW", "и фильтра", "и проверка свечей"])
def test_unrelated_foreign_title_scope_does_not_veto_independently_named_caption(currency, residual):
    row = {
        "title": f"{OIL} 1000 {currency} {residual} с запчастями",
        "snippet": f"Красноярск. {OIL}: 5000 руб. только работа",
        "source": "scope.example",
        "url": "https://scope.example/prices",
    }
    snapshot = deepcopy(row)
    rows, source = research.parse_public_work_evidence(
        row, "synthetic query", [{"normalized_name": OIL, "input": OIL}], "labor_prices"
    )
    assert len(rows) == 1
    assert (rows[0]["operation_name"], rows[0]["price_rub"]) == (OIL, 5000)
    assert (rows[0]["includes_parts"], rows[0]["labor_only"]) == (False, True)
    assert rows[0]["source"] == source and source["locator"] == row["url"]
    assert row == snapshot


@pytest.mark.parametrize("route", ["legacy", "new"])
@pytest.mark.parametrize("currency", ["$", "€"])
@pytest.mark.parametrize("kind,measurement", MEASUREMENTS)
def test_foreign_title_from_html_never_supplies_positive_price_or_hours(
    monkeypatch, route, currency, kind, measurement
):
    calls = install_html(monkeypatch, *observed_fields(f"{OIL} 1000 {currency}", measurement, "caption"))
    acquired = collect(route, OIL)
    snapshot = deepcopy(acquired)
    observations, hours = evidence_rows(route, acquired)
    rows = observations if kind == "labor_prices" else hours
    assert rows and all(row["operation_name"] == "" for row in rows)
    result = estimate(route, OIL, acquired)
    assert result["recommended_total_works_rub"] is None
    assert pure_price(OIL, observations) is None
    assert acquired == snapshot and len(calls) == 4


@pytest.mark.parametrize("scope", ["без {amount} запчастей", "только {amount} работа"])
@pytest.mark.parametrize("kind,amount", [("labor_prices", "5000 руб."), ("labor_times", "2 часа")])
@pytest.mark.parametrize("position", ["before", "after"])
def test_measurement_cannot_join_fragments_into_operation_metadata(scope, kind, amount, position):
    fragment = scope.format(amount=amount)
    text = f"{fragment} {OIL}" if position == "before" else f"{OIL} {fragment}"
    rows = parse(text, "", kind, "title")
    assert len(rows) == 1 and rows[0]["operation_name"] == ""
    assert rows[0]["source"]["locator"] == "https://scope.example/prices"
    if kind == "labor_prices":
        assert rows[0]["price_rub"] == 5000
    else:
        assert rows[0]["range_hours"] == [2, 2]


@pytest.mark.parametrize("scope", ["без {amount} запчастей", "только {amount} работа"])
@pytest.mark.parametrize(
    "title_amount,kind,caption",
    [("5000 руб.", "labor_times", "2 часа"), ("2 часа", "labor_prices", "5000 руб. только работа")],
)
def test_title_measurement_cannot_create_metadata_for_other_measurement_kind(scope, title_amount, kind, caption):
    rows = parse(f"{OIL} {scope.format(amount=title_amount)}", caption, kind, "caption")
    assert len(rows) == 1 and rows[0]["operation_name"] == ""
    assert rows[0]["public_source"] is True


@pytest.mark.parametrize("currency", ["$", "€"])
@pytest.mark.parametrize("scope", ["без {amount} запчастей", "только {amount} работа"])
def test_foreign_annotation_cannot_create_metadata_for_negative_title_veto(currency, scope):
    row = {
        "title": f"{OIL} {scope.format(amount='1000 ' + currency)} с запчастями",
        "snippet": f"Красноярск. {OIL}: 5000 руб. только работа",
        "source": "scope.example",
        "url": "https://scope.example/prices",
    }
    snapshot = deepcopy(row)
    rows, source = research.parse_public_work_evidence(
        row, "synthetic query", [{"normalized_name": OIL, "input": OIL}], "labor_prices"
    )
    assert len(rows) == 1
    assert (rows[0]["operation_name"], rows[0]["price_rub"]) == (OIL, 5000)
    assert (rows[0]["includes_parts"], rows[0]["labor_only"]) == (False, True)
    assert rows[0]["source"] == source and row == snapshot


@pytest.mark.parametrize(
    "observed",
    [
        "замена мехатроника DSG 0AM1000USD",
        "замена мехатроника DSG1000$",
        "замена мехатроника DSG$1000",
        "замена мехатроника DSG 0AM$1000",
        "замена мехатроника $1000DSG",
        "замена мехатроника DSG-$1000",
        "замена мехатроника DSG$-1000",
        "замена мехатроника DSG $1000-0AM",
        "замена мехатроника DSG 0AM-$1000",
        "замена мехатроника DSG $1000+0AM",
    ],
)
def test_foreign_annotation_does_not_remove_part_of_technical_identifier(observed):
    requested = "замена мехатроника DSG"
    row = {
        "title": observed + " с запчастями",
        "snippet": f"Красноярск. {requested}: 5000 руб. только работа",
        "source": "scope.example",
        "url": "https://scope.example/prices",
    }
    rows, source = research.parse_public_work_evidence(
        row, "synthetic query", [{"normalized_name": requested, "input": requested}], "labor_prices"
    )
    assert len(rows) == 1 and rows[0]["operation_name"] == requested
    assert (rows[0]["includes_parts"], rows[0]["labor_only"]) == (False, True)
    assert rows[0]["source"] == source


@pytest.mark.parametrize("scope", ["без запчастей", "только работа"])
@pytest.mark.parametrize("kind,measurement", MEASUREMENTS)
@pytest.mark.parametrize("style", ["inline", "caption"])
def test_original_complete_metadata_phrase_keeps_operation_binding(scope, kind, measurement, style):
    rows = parse(f"{OIL} {scope}", measurement, kind, style)
    assert len(rows) == 1 and rows[0]["operation_name"] == OIL


@pytest.mark.parametrize("label", ["замена масла без запчастей", "замена масла только работа"])
@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize("kind,measurement", MEASUREMENTS)
@pytest.mark.parametrize("style", ["inline", "caption"])
def test_declared_full_operation_preserves_internal_metadata_phrase(label, alias, kind, measurement, style):
    target = "обслуживание двигателя" if alias else label
    operations = [{"normalized_name": target, "aliases": [label] if alias else []}]
    rows = parse(label, measurement, kind, style, operations=operations)
    assert len(rows) == 1 and rows[0]["operation_name"] == target


@pytest.mark.parametrize("currency", MONEY_SYMBOLS)
@pytest.mark.parametrize("position", ["prefix", "suffix"])
def test_complete_currency_annotation_only_restores_negative_title_scope(currency, position):
    annotation = currency + "1000" if position == "prefix" else "1000" + currency
    row = {
        "title": f"{OIL} с запчастями {annotation}",
        "snippet": f"Красноярск. {OIL}: 5000 руб. только работа",
        "source": "scope.example",
        "url": "https://scope.example/prices",
    }
    snapshot = deepcopy(row)
    rows, source = research.parse_public_work_evidence(
        row, "synthetic query", [{"normalized_name": OIL, "input": OIL}], "labor_prices"
    )
    quote = next(item for item in rows if item["price_rub"] == 5000)
    assert quote["operation_name"] == OIL
    # A suffix RUB amount is already an independent accepted title price.
    expected = (False, True) if annotation == "1000₽" else (True, False)
    assert (quote["includes_parts"], quote["labor_only"]) == expected
    assert quote["source"] == source and source["locator"] == row["url"]
    assert row == snapshot


@pytest.mark.parametrize("route", ["legacy", "new"])
@pytest.mark.parametrize("currency", MONEY_SYMBOLS)
def test_prefix_currency_title_veto_reaches_both_collectors_and_consumers(monkeypatch, route, currency):
    calls = install_html(
        monkeypatch, f"{OIL} с запчастями {currency}1000", f"Красноярск. {OIL}: 5000 руб. только работа"
    )
    acquired = collect(route, OIL)
    snapshot = deepcopy(acquired)
    observations, _ = evidence_rows(route, acquired)
    assert observations and all(row["operation_name"] == OIL for row in observations)
    assert all((row["includes_parts"], row["labor_only"]) == (True, False) for row in observations)
    assert all(row["price_rub"] == 5000 and row["public_source"] is True for row in observations)
    assert estimate(route, OIL, acquired)["recommended_total_works_rub"] is None
    assert pure_price(OIL, observations) is None
    assert acquired == snapshot and len(calls) == 4


@pytest.mark.parametrize("currency", MONEY_SYMBOLS)
@pytest.mark.parametrize("kind,measurement", MEASUREMENTS)
@pytest.mark.parametrize("style", ["inline", "caption"])
def test_prefix_currency_is_not_positive_binding_metadata(currency, kind, measurement, style):
    rows = parse(f"{OIL} {currency}1000", measurement, kind, style)
    assert len(rows) == 1 and rows[0]["operation_name"] == ""
    assert rows[0]["public_source"] is True
    if kind == "labor_prices":
        assert rows[0]["price_rub"] == 5000
    else:
        assert rows[0]["range_hours"] == [2, 2]


@pytest.mark.parametrize(
    "annotation,veto",
    [
        ("-$1000", True),
        ("$-1000", True),
        ("+$1000", True),
        ("$+1000", True),
        ("−$1000", True),
        ("$−1000", True),
        ("$1000.50", True),
        ("$1000,50", True),
        ("1000.50$", True),
        ("-1000USD", True),
        ("$1 000.50", True),
        ("£1\u00a0000", True),
        ("USD1000", True),
        ("EUR1000", True),
        ("GBP1000", True),
        ("$1.2.3", False),
        ("$1,,5", False),
        ("--$1000", False),
        ("$--1000", False),
        ("+-$1000", False),
        ("$1000-", False),
        ("+$+1000", False),
        ("$1000+", False),
    ],
)
def test_title_currency_annotation_requires_complete_number_and_sign_boundaries(annotation, veto):
    row = {
        "title": f"{OIL} с запчастями {annotation}",
        "snippet": f"Красноярск. {OIL}: 5000 руб. только работа",
        "source": "scope.example",
        "url": "https://scope.example/prices",
    }
    rows, source = research.parse_public_work_evidence(
        row, "synthetic query", [{"normalized_name": OIL, "input": OIL}], "labor_prices"
    )
    assert len(rows) == 1 and rows[0]["operation_name"] == OIL
    assert (rows[0]["includes_parts"], rows[0]["labor_only"]) == (veto, not veto)
    assert rows[0]["source"] == source


@pytest.mark.parametrize("label", ["обслуживание ТО №2", "CUSTOM operation #42"])
@pytest.mark.parametrize("role", ["normalized_name", "input", "alias"])
@pytest.mark.parametrize("kind,measurement", MEASUREMENTS)
@pytest.mark.parametrize("style", ["inline", "caption"])
def test_declared_full_label_protects_its_number_symbols(label, role, kind, measurement, style):
    if role == "normalized_name":
        operation = {"normalized_name": label}
    elif role == "input":
        operation = {"normalized_name": "обслуживание двигателя", "input": label}
    else:
        operation = {"normalized_name": "обслуживание двигателя", "aliases": [label]}
    expected = "обслуживание двигателя" if role == "alias" else label
    rows = parse(label, measurement, kind, style, operations=[operation])
    assert len(rows) == 1 and rows[0]["operation_name"] == expected
    assert rows[0]["source"]["locator"] == "https://scope.example/prices"
    if kind == "labor_prices":
        assert (rows[0]["price_rub"], rows[0]["includes_parts"], rows[0]["labor_only"]) == (5000, False, True)
    else:
        assert rows[0]["range_hours"] == [2, 2]


@pytest.mark.parametrize(
    "requested,observed",
    [
        (OIL, OIL + " #42"),
        (OIL, OIL + " №2"),
        ("обслуживание ТО", "обслуживание ТО №2"),
        ("CUSTOM operation", "CUSTOM operation #42"),
    ],
)
@pytest.mark.parametrize("kind,measurement", MEASUREMENTS)
@pytest.mark.parametrize("style", ["inline", "caption"])
def test_undeclared_number_symbols_are_not_neutral_operation_metadata(requested, observed, kind, measurement, style):
    operations = [{"normalized_name": requested, "input": requested}]
    rows = parse(observed, measurement, kind, style, operations=operations)
    assert rows and all(row["operation_name"] == "" for row in rows)


@pytest.mark.parametrize("label", ["обслуживание ТО №2", "CUSTOM operation #42"])
@pytest.mark.parametrize("residual", ["и фильтра", "冷却器", "0CW"])
@pytest.mark.parametrize("kind,measurement", MEASUREMENTS)
@pytest.mark.parametrize("style", ["inline", "caption"])
def test_protected_number_symbol_does_not_protect_an_undeclared_external_scope(
    label, residual, kind, measurement, style
):
    rows = parse(
        f"{label} {residual}", measurement, kind, style, operations=[{"normalized_name": label, "input": label}]
    )
    assert rows and all(row["operation_name"] == "" for row in rows)


@pytest.mark.parametrize("requested", ["обслуживание ТО №2", "CUSTOM operation #42"])
@pytest.mark.parametrize("route", ["legacy", "new"])
@pytest.mark.parametrize("style", ["inline", "caption"])
def test_full_numbered_label_from_html_reaches_collectors_and_consumers(monkeypatch, requested, route, style):
    calls = install_html(monkeypatch, *observed_fields(requested, "5000 руб. только работа, 2 часа", style))
    acquired = collect(route, requested)
    snapshot = deepcopy(acquired)
    observations, hours = evidence_rows(route, acquired)
    assert observations and hours
    assert all(row["operation_name"] == requested for row in observations + hours)
    assert estimate(route, requested, acquired)["recommended_total_works_rub"] == 7300
    assert pure_price(requested, observations) == 5000
    assert acquired == snapshot and len(calls) == 4
