"""Explicit foreign SKU references cannot supply the requested part's price."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import json
import socket

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import mcp_telemetry, mcp_tools, part_market_assessment as market
from autostop_manager import work_pricing as pricing, work_pricing_research as research
from autostop_manager.storage import StoreState


SHORT_SKUS = ["B12", "AB12", "XY9", "ABC", "1999", "2020"]
FOREIGN_REFERENCES = [
    "SKU B12",
    "sku AB12",
    "ARTICLE XY9",
    "артикул ABC",
    "SKU 1999",
    "артикул 2020",
    "SKU:B12",
    "article=AB12",
    "артикул#XY9",
    "SKU№ABC",
    "SKU : B12",
    "ARTICLE = XY9",
    "АрТиКуЛ # AB12",
    "sku № 2020",
    "SKU\u00a0B12",
    "article\tB12",
    "(SKU:B12)",
    "Артикул:АБ12",
]


@pytest.fixture(autouse=True)
def isolated_public_assessment(monkeypatch, tmp_path):
    def denied(*_args, **_kwargs):
        raise AssertionError("supplied market observations attempted network or private acquisition")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(pricing, "_load_labor_experience", denied)
    monkeypatch.setattr(research, "urlopen", denied)
    monkeypatch.setattr(mcp_tools, "get_store_api_url", lambda: "")
    for getter in ("get_store_read_token", "get_store_manage_token", "get_store_quote_token", "get_store_owner_token"):
        monkeypatch.setattr(mcp_tools, getter, lambda: "")
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "manager.sqlite3"))
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "j1"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "vin"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def observation(index, price=5000, *, kind="original", article=None, between="", excerpt=None):
    article = article or ("1712024" if kind == "original" else "P111")
    brand = "Ford" if kind == "original" else "Brembo"
    reference = " для Ford 1712024" if kind == "analog" else ""
    today = datetime.now(UTC).date().isoformat()
    return {
        "article": article,
        "brand": brand,
        "price_rub": price,
        "source": f"Synthetic shop {index}",
        "source_excerpt": excerpt or f"{brand} {article}{reference}: новый; {between}; цена {price} ₽",
        "url": f"https://binding-{index}.example/offer/{article}",
        "observed_at": today,
        "published_at": today,
        "offer_kind": kind,
        "condition": "new",
        "region": "Красноярск, Россия",
    }


def offers(**options):
    return [observation(index, price, **options) for index, price in enumerate((5000, 6000, 7000))]


def assess(rows, article="1712024"):
    snapshot = deepcopy(rows)
    response = market.assess_part_market(article=article, brand="Ford", observations=rows)
    assert rows == snapshot
    assert response["schema"] == "PartMarketAssessmentV1" and response["read_only"] is True
    return response


def segment(response, kind="original"):
    return next(
        row
        for row in response["segments"]
        if (row["offer_kind"], row["condition"], row["region_scope"]) == (kind, "new", "krasnoyarsk")
    )


def assert_foreign_rejected(response, kind="original"):
    assert response["ok"] is False and response["status"] == "no_valid_public_evidence"
    assert response["accepted_offer_count"] == 0 and response["rejected_observation_count"] == 3
    assert response["rejected_observations"] == [
        {"observation_index": index, "code": "price_not_tied_to_article_in_source_excerpt"} for index in range(3)
    ]
    result = segment(response, kind)
    assert result["median_price_rub"] is None and result["median_confidence"] == "unavailable"


def assert_medium_median(response, kind="original"):
    assert response["ok"] is True and response["status"] == "assessed"
    assert response["accepted_offer_count"] == 3 and response["rejected_observation_count"] == 0
    result = segment(response, kind)
    assert result["independent_offer_count"] == result["median_input_offer_count"] == 3
    assert result["median_price_rub"] == 6000 and result["median_confidence"] == "medium"


@pytest.mark.parametrize("reference", FOREIGN_REFERENCES)
def test_explicit_foreign_short_sku_cannot_create_original_median(reference):
    assert_foreign_rejected(assess(offers(between=reference)))


@pytest.mark.parametrize("foreign", SHORT_SKUS)
def test_explicit_foreign_short_sku_cannot_create_analog_median(foreign):
    assert_foreign_rejected(assess(offers(kind="analog", between="артикул: " + foreign)), "analog")


@pytest.mark.parametrize("kind", ["original", "analog"])
@pytest.mark.parametrize("foreign", SHORT_SKUS)
def test_two_valid_sources_and_one_foreign_label_cannot_reach_median_threshold(kind, foreign):
    rows = [
        observation(0, 5000, kind=kind),
        observation(1, 6000, kind=kind),
        observation(2, 7000, kind=kind, between="SKU=" + foreign),
    ]
    response = assess(rows)
    assert response["ok"] is True and response["status"] == "insufficient_independent_offers"
    assert response["accepted_offer_count"] == 2 and response["rejected_observation_count"] == 1
    assert response["rejected_observations"] == [
        {"observation_index": 2, "code": "price_not_tied_to_article_in_source_excerpt"}
    ]
    result = segment(response, kind)
    assert result["independent_offer_count"] == result["median_input_offer_count"] == 2
    assert result["median_price_rub"] is None and result["median_confidence"] == "unavailable"


@pytest.mark.parametrize("kind", ["original", "analog"])
@pytest.mark.parametrize("article", SHORT_SKUS)
def test_short_and_numeric_articles_remain_valid_when_they_identify_the_offer(kind, article):
    rows = offers(kind=kind, article=article, between="SKU: " + article)
    assert_medium_median(assess(rows, article if kind == "original" else "1712024"), kind)


@pytest.mark.parametrize(
    "kind,article,target,reference",
    [
        ("original", "AB-12", "AB12", "SKU ab/12"),
        ("original", "AB12", "AB-12", "артикул: AB-12"),
        ("analog", "AB-12", "1712024", "article AB/12"),
        ("analog", "AB12", "1712024", "SKU 1712024"),
    ],
)
def test_exact_normalization_and_allowed_analog_cross_reference_preserve_binding(kind, article, target, reference):
    assert_medium_median(assess(offers(kind=kind, article=article, between=reference), target), kind)


@pytest.mark.parametrize(
    "unrecognized",
    [
        "preSKU B12",
        "SKU2 B12",
        "SKU_ B12",
        "冷SKU B12",
        "SKU市 B12",
        "артикулы B12",
        "articles B12",
        "SKU-B12",
        "SKU::B12",
        "артикулB12",
    ],
)
def test_standalone_label_grammar_does_not_expand_to_embedded_words_or_extra_separators(unrecognized):
    assert_medium_median(assess(offers(between=unrecognized)))


@pytest.mark.parametrize(
    "description", ["модель E46", "модель CX5", "год 1999", "год 2020", "количество 2", "количество 12"]
)
def test_unlabelled_model_year_and_quantity_do_not_become_foreign_skus(description):
    assert_medium_median(assess(offers(between=description)))


@pytest.mark.parametrize(
    "template,bound",
    [
        ("SKU B12; Ford 1712024: новый; цена {price} ₽", True),
        ("Ford 1712024: новый; цена {price} ₽; SKU B12", True),
        ("Ford 1712024; SKU B12; Ford 1712024: новый; цена {price} ₽", True),
        ("Ford 1712024; SKU B12; Ford 1712024: новый; article XY9; цена {price} ₽", False),
    ],
)
def test_foreign_label_only_affects_interval_after_last_offer_article_before_price(template, bound):
    rows = [
        observation(index, price, excerpt=template.format(price=price))
        for index, price in enumerate((5000, 6000, 7000))
    ]
    response = assess(rows)
    if bound:
        assert_medium_median(response)
    else:
        assert_foreign_rejected(response)


def test_existing_unlabelled_long_foreign_sku_heuristic_is_preserved():
    assert_foreign_rejected(assess(offers(between="AB123")))


@pytest.mark.parametrize("has_target", [False, True])
def test_source_metadata_cannot_replace_or_override_excerpt_article_evidence(has_target):
    rows = offers(between="SKU B12")
    for row in rows:
        row["source"] = "Ford 1712024; SKU 1712024"
        if not has_target:
            row["source_excerpt"] = row["source_excerpt"].replace("1712024", "")
    response = assess(rows)
    assert response["accepted_offer_count"] == 0
    expected = "price_not_tied_to_article_in_source_excerpt" if has_target else "article_not_in_source_excerpt"
    assert {row["code"] for row in response["rejected_observations"]} == {expected}


@pytest.mark.parametrize(
    "money,code",
    [
        ("5000 ₽ и 6000 ₽", "price_ambiguous_in_source_excerpt"),
        ("5000 ₽ и 1,50 ₽", "price_ambiguous_in_source_excerpt"),
        ("5000,50 ₽", "price_not_in_source_excerpt"),
    ],
)
def test_explicit_sku_rule_preserves_existing_money_ambiguity_and_fraction_errors(money, code):
    row = observation(0, excerpt=f"Ford 1712024: новый; SKU B12; цена {money}")
    response = assess([row])
    assert response["accepted_offer_count"] == 0
    assert response["rejected_observations"] == [{"observation_index": 0, "code": code}]


@pytest.fixture
def native_tool(monkeypatch, tmp_path):
    class InertAdapter:
        def __init__(self, *_args, **_kwargs):
            pass

    for name in (
        "StoreIntegration",
        "StoreApiClient",
        "StoreOwnerApiClient",
        "StoreQuoteConductor",
        "StoreQuoteOwnerApi",
    ):
        monkeypatch.setattr(mcp_tools, name, InertAdapter)
    monkeypatch.setattr(mcp_telemetry.LOGGER, "disabled", True)
    server = FastMCP("isolated-part-market-price-binding")
    mcp_tools.register_manager_tools(
        server, StoreState(tmp_path / "native.sqlite3"), include_tools={"assess_part_market"}
    )
    assert set(server._tool_manager._tools) == {"assess_part_market"}
    tool = server._tool_manager.get_tool("assess_part_market")
    assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False
    return tool


def native_assess(tool, rows, *, encoded, article="1712024"):
    arguments = {"article": article, "brand": "Ford", "observations": rows}
    snapshot = deepcopy(arguments)
    wire = {**arguments, "observations": json.dumps(rows)} if encoded else arguments
    content, response = asyncio.run(tool.run(wire, convert_result=True))
    assert json.loads(content[0].text) == response
    assert {key: value for key, value in response.items() if key != "tool_execution"} == assess(rows, article)
    assert arguments == snapshot
    return response


@pytest.mark.parametrize(
    "kind,reference",
    [
        ("original", "SKU B12"),
        ("original", "article=AB12"),
        ("original", "артикул № 1999"),
        ("analog", "SKU XY9"),
        ("analog", "ARTICLE ABC"),
        ("analog", "артикул: 2020"),
    ],
)
@pytest.mark.parametrize("encoded", [False, True])
def test_actual_registered_native_tool_rejects_foreign_short_reference(native_tool, kind, reference, encoded):
    response = native_assess(native_tool, offers(kind=kind, between=reference), encoded=encoded)
    assert_foreign_rejected(response, kind)


@pytest.mark.parametrize(
    "kind,article,target,reference",
    [
        ("original", "AB-12", "AB12", "SKU AB/12"),
        ("original", "1712024", "1712024", "модель E46"),
        ("analog", "XY9", "1712024", "артикул 1712024"),
    ],
)
@pytest.mark.parametrize("encoded", [False, True])
def test_actual_registered_native_tool_preserves_allowed_and_unlabelled_evidence(
    native_tool, kind, article, target, reference, encoded
):
    response = native_assess(
        native_tool, offers(kind=kind, article=article, between=reference), encoded=encoded, article=target
    )
    assert_medium_median(response, kind)


@pytest.mark.parametrize("kind", ["original", "analog"])
@pytest.mark.parametrize("encoded", [False, True])
def test_native_foreign_third_source_cannot_complete_median(native_tool, kind, encoded):
    rows = [
        observation(0, 5000, kind=kind),
        observation(1, 6000, kind=kind),
        observation(2, 7000, kind=kind, between="SKU: B12"),
    ]
    response = native_assess(native_tool, rows, encoded=encoded)
    assert response["accepted_offer_count"] == 2 and response["status"] == "insufficient_independent_offers"
    assert segment(response, kind)["median_price_rub"] is None
    assert response["rejected_observations"] == [
        {"observation_index": 2, "code": "price_not_tied_to_article_in_source_excerpt"}
    ]


@pytest.mark.parametrize("kind", ["original", "analog"])
@pytest.mark.parametrize("article", ["AA/SKU", "AA.ARTICLE", "AA-АРТИКУЛ"])
def test_label_inside_complete_known_offer_article_is_not_a_foreign_reference(kind, article):
    rows = offers(kind=kind, article=article)
    assert_medium_median(assess(rows, article if kind == "original" else "1712024"), kind)


@pytest.mark.parametrize("kind", ["original", "analog"])
@pytest.mark.parametrize("encoded", [False, True])
def test_native_label_inside_known_offer_article_preserves_binding(native_tool, kind, encoded):
    article = "AA/SKU"
    response = native_assess(
        native_tool,
        offers(kind=kind, article=article),
        encoded=encoded,
        article=article if kind == "original" else "1712024",
    )
    assert_medium_median(response, kind)


@pytest.mark.parametrize("target", ["AA/SKU", "AA.ARTICLE", "AA-АРТИКУЛ", "SKU", "ARTICLE", "АРТИКУЛ"])
def test_label_inside_allowed_analog_target_is_preserved_as_complete_identity(target):
    rows = offers(kind="analog")
    for row in rows:
        row["source_excerpt"] = f"Brembo P111 для Ford {target}: новый; цена {row['price_rub']} ₽"
    assert_medium_median(assess(rows, target), "analog")


@pytest.mark.parametrize("target", ["AA/SKU", "ARTICLE"])
@pytest.mark.parametrize("encoded", [False, True])
def test_native_allowed_analog_target_is_not_reparsed_as_a_label(native_tool, target, encoded):
    rows = offers(kind="analog")
    for row in rows:
        row["source_excerpt"] = f"Brembo P111 для Ford {target}: новый; цена {row['price_rub']} ₽"
    assert_medium_median(native_assess(native_tool, rows, encoded=encoded, article=target), "analog")
