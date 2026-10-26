"""Mapped IPv6 and its IPv4 literal share one market evidence identity."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import json
import socket
import sqlite3
import subprocess

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import mcp_telemetry, mcp_tools
from autostop_manager import part_market_assessment as market


IPV4 = "8.8.8.8"
MAPPED = ("::ffff:8.8.8.8", "::ffff:808:808", "0:0:0:0:0:ffff:808:808")


def offer(host, price, *, day=26, condition="new", region="Красноярск"):
    state = "новый" if condition == "new" else "б/у"
    return {
        "article": "DEMO123",
        "brand": "Demo",
        "price_rub": price,
        "source": f"Synthetic {price}",
        "source_excerpt": f"Demo DEMO123 {state}, цена {price} ₽",
        "url": f"https://{host}:8443/offer?tracking=synthetic#section",
        "observed_at": f"2026-10-{day:02d}",
        "published_at": "2026-10-24",
        "offer_kind": "original",
        "condition": condition,
        "region": region,
    }


def segment(result, *, condition="new", region="krasnoyarsk"):
    return next(
        row
        for row in result["segments"]
        if (row["offer_kind"], row["condition"], row["region_scope"]) == ("original", condition, region)
    )


@pytest.fixture(autouse=True)
def isolated_market(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is UTC
            return cls(2026, 10, 26, 12, tzinfo=UTC)

    def denied(*args, **kwargs):
        raise AssertionError("mapped source identity must not acquire network, process or DB data")

    monkeypatch.setattr(market, "datetime", FixedDatetime)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(subprocess, "run", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(sqlite3, "connect", denied)


@pytest.fixture(scope="module")
def native_tool():
    class Inert:
        def __init__(self, *args, **kwargs):
            pass

    with pytest.MonkeyPatch.context() as patch:
        for name in (
            "StoreIntegration",
            "StoreApiClient",
            "StoreOwnerApiClient",
            "StoreQuoteConductor",
            "StoreQuoteOwnerApi",
        ):
            patch.setattr(mcp_tools, name, Inert)
        for name in (
            "get_store_api_url",
            "get_store_read_token",
            "get_store_manage_token",
            "get_store_quote_token",
            "get_store_owner_token",
        ):
            patch.setattr(mcp_tools, name, lambda: "")
        patch.setattr(mcp_telemetry.LOGGER, "disabled", True)
        server = FastMCP("offline-market-mapped-ip")
        mcp_tools.register_manager_tools(
            server, store=Inert(), store_client=Inert(), include_tools={"assess_part_market"}
        )
        yield server._tool_manager.get_tool("assess_part_market")


@pytest.fixture(params=["public", "native", "native-json"])
def assess(request, native_tool):
    def call(rows):
        original = deepcopy(rows)
        if request.param == "public":
            result = market.assess_part_market(article="DEMO123", brand="Demo", observations=rows)
        else:
            arguments = {
                "article": "DEMO123",
                "brand": "Demo",
                "observations": json.dumps(rows) if request.param == "native-json" else rows,
            }
            content, result = asyncio.run(native_tool.run(arguments, convert_result=True))
            assert json.loads(content[0].text) == result
        assert rows == original
        assert result["schema"] == "PartMarketAssessmentV1"
        assert result["read_only"] is True
        return result

    return call


@pytest.mark.parametrize("mapped", MAPPED)
def test_mapped_alias_cannot_supply_the_third_independent_offer(assess, mapped):
    result = assess([offer(IPV4, 1000), offer(f"[{mapped}]", 9000), offer("independent.example", 5000)])
    selected = segment(result)

    assert result["status"] == "insufficient_independent_offers"
    assert result["accepted_offer_count"] == selected["independent_offer_count"] == 2
    assert selected["median_input_offer_count"] == 2
    assert selected["median_price_rub"] is None
    assert result["rejected_observations"] == [{"observation_index": 1, "code": "duplicate_source_in_segment"}]


@pytest.mark.parametrize("latest_mapped", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_latest_cross_family_observation_keeps_price_url_and_input_index(assess, latest_mapped, reverse):
    mapped = f"[{MAPPED[0]}]"
    older = offer(IPV4 if latest_mapped else mapped, 1000, day=25)
    latest = offer(mapped if latest_mapped else IPV4, 9000)
    pair = [latest, older] if reverse else [older, latest]
    result = assess([*pair, offer("independent-a.example", 5000), offer("independent-b.example", 7000)])
    selected = segment(result)

    assert result["status"] == "assessed"
    assert result["accepted_offer_count"] == selected["median_input_offer_count"] == 3
    assert selected["median_price_rub"] == 7000
    retained = next(row for row in selected["offers"] if row["source_key"] == IPV4)
    assert retained["price_rub"] == 9000
    assert retained["observation_index"] == pair.index(latest)
    assert retained["url"] == latest["url"].split("?", 1)[0]
    assert result["rejected_observations"] == [
        {"observation_index": pair.index(older), "code": "duplicate_source_in_segment"}
    ]


@pytest.mark.parametrize("first_host", [IPV4, f"[{MAPPED[0]}]"])
def test_equal_dates_keep_first_cross_family_observation(assess, first_host):
    second_host = f"[{MAPPED[0]}]" if first_host == IPV4 else IPV4
    result = assess([offer(first_host, 1000), offer(second_host, 9000)])
    selected = segment(result)

    assert result["accepted_offer_count"] == 1
    assert selected["offers"][0]["source_key"] == IPV4
    assert selected["offers"][0]["observation_index"] == 0
    assert selected["offers"][0]["price_rub"] == 1000
    assert selected["median_price_rub"] is None


@pytest.mark.parametrize("mapped", MAPPED)
def test_retained_mapped_url_preserves_ipv6_brackets_port_and_literal_spelling(assess, mapped):
    result = assess([offer(f"[{mapped}]", 5000)])
    retained = segment(result)["offers"][0]

    assert retained["source_key"] == IPV4
    assert retained["url"] == f"https://[{mapped}]:8443/offer"


@pytest.mark.parametrize("embedded", ["127.0.0.1", "10.0.0.1", "169.254.1.2", "0.0.0.0", "192.0.2.1", "240.0.0.1"])
def test_non_public_mapped_source_remains_rejected(assess, embedded):
    result = assess([offer(f"[::ffff:{embedded}]", 5000)])

    assert result["ok"] is False
    assert result["accepted_offer_count"] == 0
    assert result["status"] == "no_valid_public_evidence"
    assert result["rejected_observations"] == [{"observation_index": 0, "code": "observation_fields_invalid"}]


@pytest.mark.parametrize(
    ("hosts", "keys"),
    [
        ([IPV4, "1.1.1.1", "9.9.9.9"], [IPV4, "1.1.1.1", "9.9.9.9"]),
        (
            ["[2606:4700:4700::abcd]", "[2606:4700:4700::abce]", "[2606:4700:4700::abcf]"],
            ["2606:4700:4700::abcd", "2606:4700:4700::abce", "2606:4700:4700::abcf"],
        ),
        (["[::ffff:8.8.8.8]", "[::ffff:1.1.1.1]", "[::ffff:9.9.9.9]"], [IPV4, "1.1.1.1", "9.9.9.9"]),
    ],
)
def test_distinct_numeric_sources_still_provide_three_independent_offers(assess, hosts, keys):
    result = assess([offer(host, price) for host, price in zip(hosts, (1000, 5000, 9000), strict=True)])
    selected = segment(result)

    assert result["status"] == "assessed"
    assert result["accepted_offer_count"] == selected["median_input_offer_count"] == 3
    assert selected["median_price_rub"] == 5000
    assert [row["source_key"] for row in selected["offers"]] == keys
    assert result["rejected_observations"] == []


def test_cross_family_key_does_not_deduplicate_different_segments(assess):
    result = assess(
        [
            offer(IPV4, 1000),
            offer(f"[{MAPPED[0]}]", 2000, condition="used"),
            offer(f"[{MAPPED[1]}]", 3000, region="РФ: Москва"),
        ]
    )

    assert result["accepted_offer_count"] == 3
    assert result["rejected_observations"] == []
    for condition, region in [("new", "krasnoyarsk"), ("used", "krasnoyarsk"), ("new", "rf")]:
        selected = segment(result, condition=condition, region=region)
        assert selected["independent_offer_count"] == 1
        assert selected["offers"][0]["source_key"] == IPV4
        assert selected["median_price_rub"] is None
