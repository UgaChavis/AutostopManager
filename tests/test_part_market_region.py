"""Region evidence cannot manufacture a local or Russian public-market median."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import socket

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import mcp_telemetry, mcp_tools, part_market_assessment as market
from autostop_manager import work_pricing as pricing, work_pricing_research as research
from autostop_manager.storage import StoreState


INVALID_REGIONS = [
    pytest.param(None, id="null"),
    pytest.param(False, id="false"),
    pytest.param(True, id="true"),
    pytest.param(0, id="integer"),
    pytest.param(1.5, id="float"),
    pytest.param([], id="empty-list"),
    pytest.param(["Красноярск"], id="city-list"),
    pytest.param({}, id="empty-object"),
    pytest.param({"city": "Красноярск"}, id="city-object"),
    pytest.param({"country": "Россия"}, id="country-object"),
    pytest.param("", id="empty-string"),
    pytest.param("\u00a0\t\n", id="whitespace"),
    pytest.param("Некрасноярск", id="city-prefix"),
    pytest.param("Красноярский край", id="territory"),
    pytest.param("Красноярск2", id="city-number-suffix"),
    pytest.param("Красноярск市", id="city-unicode-suffix"),
    pytest.param("Беларуссия", id="different-country"),
    pytest.param("Belarussia", id="country-substring"),
    pytest.param("RussiaTown", id="country-suffix"),
    pytest.param("Россиях", id="russian-country-suffix"),
    pytest.param("РФО", id="abbreviation-suffix"),
    pytest.param("РФ123", id="abbreviation-number"),
    pytest.param("xРФ", id="abbreviation-prefix"),
    pytest.param("冷Россия", id="country-unicode-prefix"),
    pytest.param("Россия市", id="country-unicode-suffix"),
    pytest.param("РФ_Москва", id="country-underscore"),
    pytest.param("доставка в Красноярск", id="delivery"),
    pytest.param("магазин Красноярск", id="extra-subject"),
    pytest.param("Красноярск / Москва", id="two-cities"),
    pytest.param("Красноярск Норильск", id="second-city"),
    pytest.param("гКрасноярск", id="unsupported-city-prefix"),
    pytest.param("(Красноярск]", id="unbalanced-brackets"),
    pytest.param("[[Красноярск]]", id="nested-brackets"),
]
NEGATIONS = [
    "не",
    "ни",
    "нет",
    "без",
    "кроме",
    "исключая",
    "исключением",
    "not",
    "no",
    "never",
    "except",
    "excluding",
    "without",
]
LOCAL_REGIONS = [
    "Красноярск",
    "КРАСНОЯРСК",
    " \tКрасноярск\u00a0 ",
    "г.Красноярск",
    "г. Красноярск",
    "город Красноярск",
    "Красноярск, Россия",
    "Россия,Красноярск",
    "Красноярск РФ",
    "Российская\u00a0Федерация Красноярск",
    "Красноярск: Russia",
    "РФ; Красноярск",
    "Красноярск / Россия",
    "Russia-Красноярск",
    "Красноярск – РФ",
    "Россия — Красноярск",
    "Красноярск(Россия)",
    "Красноярск[РФ]",
    "Россия(Красноярск)",
    "РФ[г.Красноярск]",
    "(Красноярск, Россия)",
    "[Российская Федерация, город Красноярск]",
]
RF_REGIONS = [
    "Россия",
    "Российская Федерация",
    "РФ",
    "Russia",
    "rUsSiA",
    "РФ: Москва",
    "Норильск, Россия",
    "Российская\u00a0Федерация: Москва",
    "доставка Красноярск,Россия",
    "Красноярск/Москва,РФ",
    "Красноярский край, Россия",
    "Russia: Krasnoyarsk",
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


def observation(region, index=0, price=5000, **overrides):
    today = datetime.now(UTC).date().isoformat()
    return {
        "article": "1712024",
        "brand": "Ford",
        "price_rub": price,
        "source": f"Synthetic shop {index}",
        "source_excerpt": f"Ford 1712024: новый, цена {price} ₽",
        "url": f"https://region-{index}.example/offer/1712024",
        "observed_at": today,
        "published_at": today,
        "offer_kind": "original",
        "condition": "new",
        "region": region,
        **overrides,
    }


def offers(region):
    return [observation(region, index, price) for index, price in enumerate((5000, 6000, 7000))]


def assess(rows, **options):
    snapshot = deepcopy((rows, options))
    response = market.assess_part_market(article="1712024", brand="Ford", observations=rows, **options)
    assert (rows, options) == snapshot
    assert response["schema"] == "PartMarketAssessmentV1" and response["read_only"] is True
    return response


def segment(response, region="krasnoyarsk", kind="original", condition="new"):
    return next(
        row
        for row in response["segments"]
        if (row["region_scope"], row["offer_kind"], row["condition"]) == (region, kind, condition)
    )


def assert_rejected_region(response):
    assert response["ok"] is False and response["status"] == "no_valid_public_evidence"
    assert response["accepted_offer_count"] == 0 and response["rejected_observation_count"] == 3
    assert response["rejected_observations"] == [
        {"observation_index": index, "code": "observation_fields_invalid"} for index in range(3)
    ]
    assert all(
        row["median_price_rub"] is None and row["median_confidence"] == "unavailable" for row in response["segments"]
    )


def assert_medium_median(response, scope):
    assert response["ok"] is True and response["status"] == "assessed"
    assert response["accepted_offer_count"] == 3 and response["rejected_observation_count"] == 0
    result = segment(response, scope)
    assert result["independent_offer_count"] == result["median_input_offer_count"] == 3
    assert result["median_price_rub"] == 6000 and result["median_confidence"] == "medium"
    other = "rf" if scope == "krasnoyarsk" else "krasnoyarsk"
    assert segment(response, other)["independent_offer_count"] == 0


@pytest.mark.parametrize("region", INVALID_REGIONS)
def test_unproved_region_cannot_create_a_three_domain_median(region):
    assert_rejected_region(assess(offers(region)))


@pytest.mark.parametrize("marker", NEGATIONS)
def test_negative_or_exclusion_token_rejects_entire_country_and_city_field(marker):
    assert_rejected_region(assess(offers(f"Россия, {marker} Красноярск")))


@pytest.mark.parametrize("region", ["Россия (не) Красноярск", "Россия—кроме—Красноярск"])
def test_punctuation_does_not_hide_explicit_region_exclusions(region):
    assert_rejected_region(assess(offers(region)))


@pytest.mark.parametrize("region", LOCAL_REGIONS)
def test_complete_supported_city_field_keeps_local_medium_median(region):
    assert_medium_median(assess(offers(region)), "krasnoyarsk")


@pytest.mark.parametrize("region", RF_REGIONS)
def test_standalone_country_marker_keeps_rf_scope_without_inventing_locality(region):
    assert_medium_median(assess(offers(region)), "rf")


@pytest.mark.parametrize(
    "bad",
    [
        ["Красноярск"],
        {"country": "Россия"},
        "Россия, не Красноярск",
        "кроме Красноярск",
        "Некрасноярск",
        "Красноярский край",
        "Belarussia",
        "РФ_Москва",
        "Россия " + "x" * 94,
    ],
)
def test_two_valid_sources_plus_invalid_region_remain_below_median_threshold(bad):
    response = assess(
        [observation("Красноярск", 0, 5000), observation("Россия, Красноярск", 1, 6000), observation(bad, 2, 7000)]
    )
    assert response["ok"] is True and response["status"] == "insufficient_independent_offers"
    assert response["accepted_offer_count"] == 2 and response["rejected_observation_count"] == 1
    assert response["rejected_observations"] == [{"observation_index": 2, "code": "observation_fields_invalid"}]
    local = segment(response)
    assert local["independent_offer_count"] == local["median_input_offer_count"] == 2
    assert local["median_price_rub"] is None and local["median_confidence"] == "unavailable"


@pytest.mark.parametrize(
    "region,scope",
    [
        ("Россия " + "x" * 93, "rf"),
        ("Россия " + "x" * 94, None),
        ("Россия " + "x" * 93 + " не Красноярск", None),
        ("\u00a0" * 150 + "Россия" + "\t" * 150, "rf"),
        ("Россия\u00a0\t\n" + "x" * 93, "rf"),
        ("Красноярск " + "x" * 100, None),
    ],
)
def test_region_limit_applies_to_complete_whitespace_normalized_field(region, scope):
    response = assess(offers(region))
    if scope is None:
        assert_rejected_region(response)
    else:
        assert_medium_median(response, scope)


def test_missing_region_is_rejected_without_using_the_target_as_evidence():
    rows = offers("Красноярск")
    for row in rows:
        row.pop("region")
    assert_rejected_region(assess(rows))


def test_local_and_rf_sources_cannot_be_pooled_to_reach_three():
    response = assess(
        [
            observation("Красноярск", 0, 5000),
            observation("Россия, Красноярск", 1, 6000),
            observation("РФ: Москва", 2, 7000),
        ]
    )
    assert response["accepted_offer_count"] == 3 and response["status"] == "insufficient_independent_offers"
    assert segment(response)["independent_offer_count"] == 2
    assert segment(response, "rf")["independent_offer_count"] == 1
    assert all(row["median_price_rub"] is None for row in response["segments"])


@pytest.mark.parametrize(
    "scope,variants",
    [
        ("krasnoyarsk", ["Красноярск", "Россия, г.Красноярск", "город Красноярск"]),
        ("rf", ["Россия", "РФ: Москва", "Норильск, Russia"]),
    ],
)
def test_same_domain_replays_remain_one_independent_source_after_region_normalization(scope, variants):
    rows = [
        observation(
            region,
            index,
            5000 + index * 1000,
            url=f"https://{'www.' if index == 1 else ''}one-source.example/offer/{index}",
        )
        for index, region in enumerate(variants)
    ]
    response = assess(rows)
    assert response["accepted_offer_count"] == 1 and response["rejected_observation_count"] == 2
    result = segment(response, scope)
    assert result["independent_offer_count"] == 1 and result["median_price_rub"] is None
    assert result["offers"][0]["price_rub"] == 5000
    assert all(row["code"] == "duplicate_source_in_segment" for row in response["rejected_observations"])


@pytest.mark.parametrize("variant", ["used", "unknown-condition", "stale-page", "analog"])
def test_region_validation_preserves_existing_condition_freshness_and_sku_segments(variant):
    rows = offers("РФ: Москва")
    kind, condition = "original", "new"
    for row in rows:
        if variant == "used":
            row["condition"] = condition = "used"
            row["source_excerpt"] = row["source_excerpt"].replace("новый", "б/у")
        elif variant == "unknown-condition":
            row["condition"] = condition = "unknown"
            row["source_excerpt"] = row["source_excerpt"].replace("новый", "состояние не указано")
        elif variant == "stale-page":
            row["published_at"] = (datetime.now(UTC).date() - timedelta(days=31)).isoformat()
        else:
            row.update(article="P111", brand="Brembo", offer_kind="analog")
            row["source_excerpt"] = f"Brembo P111 для Ford 1712024: новый, цена {row['price_rub']} ₽"
            kind = "analog"
    response = assess(rows)
    assert response["accepted_offer_count"] == 3
    result = segment(response, "rf", kind, condition)
    assert result["median_price_rub"] == (None if variant in {"unknown-condition", "stale-page"} else 6000)
    assert segment(response)["independent_offer_count"] == 0


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
    server = FastMCP("isolated-part-market-region")
    mcp_tools.register_manager_tools(
        server, StoreState(tmp_path / "native.sqlite3"), include_tools={"assess_part_market"}
    )
    assert set(server._tool_manager._tools) == {"assess_part_market"}
    tool = server._tool_manager.get_tool("assess_part_market")
    assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False
    return tool


@pytest.mark.parametrize(
    "region,scope",
    [
        ("Красноярск", "krasnoyarsk"),
        ("Россия, г.Красноярск", "krasnoyarsk"),
        ("РФ[Красноярск]", "krasnoyarsk"),
        ("Россия", "rf"),
        ("Норильск, Russia", "rf"),
        ("доставка Красноярск,Россия", "rf"),
        (["Красноярск"], None),
        ({"country": "Россия"}, None),
        ("Россия, не Красноярск", None),
        ("Россия кроме Красноярска", None),
        ("Belarussia", None),
        ("Россия " + "x" * 94, None),
    ],
)
@pytest.mark.parametrize("encoded", [False, True])
def test_actual_native_assessor_keeps_region_contract_and_result_channels(native_tool, region, scope, encoded):
    arguments = {"article": "1712024", "brand": "Ford", "observations": offers(region)}
    snapshot = deepcopy(arguments)
    wire = {**arguments, "observations": json.dumps(arguments["observations"])} if encoded else arguments
    content, response = asyncio.run(native_tool.run(wire, convert_result=True))
    assert json.loads(content[0].text) == response
    direct = assess(arguments["observations"])
    assert {key: value for key, value in response.items() if key != "tool_execution"} == direct
    if scope is None:
        assert_rejected_region(response)
    else:
        assert_medium_median(response, scope)
    assert arguments == snapshot


@pytest.mark.parametrize("bad", [["Красноярск"], {"city": "Красноярск"}, "Россия, не Красноярск", "РФО"])
@pytest.mark.parametrize("encoded", [False, True])
def test_native_invalid_region_does_not_complete_a_two_source_sample(native_tool, bad, encoded):
    rows = [observation("Красноярск", 0, 5000), observation("Россия, Красноярск", 1, 6000), observation(bad, 2, 7000)]
    arguments = {"article": "1712024", "brand": "Ford", "observations": rows}
    snapshot = deepcopy(arguments)
    wire = {**arguments, "observations": json.dumps(rows)} if encoded else arguments
    content, response = asyncio.run(native_tool.run(wire, convert_result=True))
    assert json.loads(content[0].text) == response
    assert response["accepted_offer_count"] == 2 and response["status"] == "insufficient_independent_offers"
    assert segment(response)["median_price_rub"] is None
    assert response["rejected_observations"] == [{"observation_index": 2, "code": "observation_fields_invalid"}]
    assert arguments == snapshot
