"""Public regressions for numeric source identity in supplied market observations.

Twelve distinct scenarios, twelve pytest runs, and no interface repeats. Offers
and URLs are synthetic: no address is contacted, resolved, or attributed to a
merchant. Tests use real URL validation, grouping and assessment; only UTC now
is frozen. Source identity must not rewrite the retained safe URL or merge
separate segments, and invalid source URLs must remain rejected.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import ipaddress
from urllib.parse import urlsplit

import pytest

from autostop_manager import part_market_assessment as market


EQUIVALENT_IPS = (
    "2606:4700:4700::abcd",
    "2606:4700:4700:0:0:0:0:abcd",
    "2606:4700:4700:0000:0000:0000:0000:ABCD",
)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is UTC
            return cls(2026, 10, 16, 12, tzinfo=UTC)

    monkeypatch.setattr(market, "datetime", FixedDatetime)


def _url(address):
    return f"https://[{address}]/offer/1712024"


def _offer(url, price, *, day=16, region="Красноярск", condition="new"):
    condition_text = "новый" if condition == "new" else "б/у"
    return {
        "article": "1712024",
        "brand": "Ford",
        "price_rub": price,
        "source": f"Synthetic observation {price}",
        "source_excerpt": f"Ford 1712024: {condition_text}, цена {price} ₽",
        "url": url,
        "observed_at": f"2026-10-{day:02d}",
        "published_at": "2026-10-14",
        "offer_kind": "original",
        "condition": condition,
        "region": region,
    }


def _assess(rows):
    before = deepcopy(rows)
    result = market.assess_part_market(article="1712024", brand="Ford", observations=rows)
    assert rows == before
    assert result["ok"] is True and result["read_only"] is True
    assert not any(row["code"] != "duplicate_source_in_segment" for row in result["rejected_observations"])
    return result


def _segment(result, *, scope="krasnoyarsk", condition="new"):
    return next(
        row
        for row in result["segments"]
        if (row["offer_kind"], row["condition"], row["region_scope"]) == ("original", condition, scope)
    )


def test_equivalent_public_ipv6_representations_have_one_source_key():
    addresses = [ipaddress.ip_address(value) for value in EQUIVALENT_IPS]
    assert all(address.is_global for address in addresses)
    assert len(set(addresses)) == 1  # Numeric identity, independent of any DNS claim.
    parsed = [
        market._public_source_url(f"https://[{value}]:8443/offer/1712024?tracking=synthetic#section")
        for value in EQUIVALENT_IPS
    ]
    expected_safe_urls = (
        "https://[2606:4700:4700::abcd]:8443/offer/1712024",
        "https://[2606:4700:4700:0:0:0:0:abcd]:8443/offer/1712024",
        "https://[2606:4700:4700:0000:0000:0000:0000:abcd]:8443/offer/1712024",
    )
    assert all(row is not None for row in parsed)
    for (safe_url, _source_key), expected_url in zip(parsed, expected_safe_urls, strict=True):
        assert safe_url == expected_url
        observed = urlsplit(safe_url)
        assert observed.scheme == "https" and not observed.query and not observed.fragment
        assert observed.port == 8443 and observed.path == "/offer/1712024"
        assert ipaddress.ip_address(observed.hostname) == addresses[0]
    assert len({row[1] for row in parsed}) == 1


def test_three_representations_do_not_fabricate_a_three_source_median():
    rows = [
        _offer(_url(address), price, day=day)
        for address, price, day in zip(EQUIVALENT_IPS, (1000, 2000, 9000), (14, 15, 16), strict=True)
    ]
    result = _assess(rows)
    segment = _segment(result)
    assert result["status"] == "insufficient_independent_offers"
    assert result["accepted_offer_count"] == segment["independent_offer_count"] == 1
    assert segment["median_input_offer_count"] == 1
    assert segment["median_price_rub"] is None and segment["median_available"] is False
    assert segment["offers"][0]["observation_index"] == 2
    assert segment["offers"][0]["price_rub"] == 9000
    assert result["rejected_observations"] == [
        {"observation_index": 0, "code": "duplicate_source_in_segment"},
        {"observation_index": 1, "code": "duplicate_source_in_segment"},
    ]


def test_latest_equivalent_address_row_is_the_only_median_input_for_its_source():
    result = _assess(
        [
            _offer(_url(EQUIVALENT_IPS[0]), 1000, day=14),
            _offer(_url(EQUIVALENT_IPS[1]), 9000, day=16),
            _offer("https://independent-a.example/offer", 5000),
            _offer("https://independent-b.example/offer", 7000),
        ]
    )
    segment = _segment(result)
    assert result["status"] == "assessed"
    assert result["accepted_offer_count"] == segment["independent_offer_count"] == 3
    assert segment["median_input_offer_count"] == 3
    assert segment["median_price_rub"] == 7000
    assert [row["observation_index"] for row in segment["offers"]] == [1, 2, 3]
    assert result["rejected_observations"] == [{"observation_index": 0, "code": "duplicate_source_in_segment"}]


def test_distinct_public_ipv6_addresses_remain_three_sources():
    addresses = ("2606:4700:4700::abcd", "2606:4700:4700::abce", "2606:4700:4700::abcf")
    assert len({ipaddress.ip_address(value) for value in addresses}) == 3
    result = _assess(
        [_offer(_url(address), price) for address, price in zip(addresses, (1000, 5000, 9000), strict=True)]
    )
    segment = _segment(result)
    assert result["status"] == "assessed"
    assert result["accepted_offer_count"] == segment["independent_offer_count"] == 3
    assert segment["median_price_rub"] == 5000 and segment["median_available"] is True
    assert result["rejected_observations"] == []


def test_existing_dns_www_case_trailing_dot_and_port_deduplication_is_preserved():
    urls = (
        "https://www.ONE.example/old?tracking=synthetic#section",
        "https://one.example.:443/middle",
        "https://one.example:8443/latest",
    )
    result = _assess(
        [_offer(url, price, day=day) for url, price, day in zip(urls, (1000, 2000, 9000), (14, 15, 16), strict=True)]
    )
    segment = _segment(result)
    assert result["accepted_offer_count"] == segment["independent_offer_count"] == 1
    assert segment["offers"][0]["observation_index"] == 2
    assert segment["offers"][0]["source_key"] == "one.example"
    assert segment["offers"][0]["url"] == "https://one.example:8443/latest"
    assert segment["median_price_rub"] is None
    assert result["rejected_observation_count"] == 2


def test_same_numeric_source_is_kept_separately_in_distinct_segments():
    result = _assess(
        [
            _offer(_url(EQUIVALENT_IPS[0]), 1000),
            _offer(_url(EQUIVALENT_IPS[1]), 2000, region="РФ: Москва"),
            _offer(_url(EQUIVALENT_IPS[2]), 3000, condition="used"),
        ]
    )
    assert result["accepted_offer_count"] == 3 and result["rejected_observations"] == []
    assert _segment(result)["independent_offer_count"] == 1
    assert _segment(result, scope="rf")["independent_offer_count"] == 1
    assert _segment(result, condition="used")["independent_offer_count"] == 1
    assert result["status"] == "insufficient_independent_offers"
    assert all(segment["median_price_rub"] is None for segment in result["segments"])


def test_equal_dates_keep_first_equivalent_ipv6_observation():
    result = _assess(
        [
            _offer(_url(EQUIVALENT_IPS[0]), 1000),
            _offer(_url(EQUIVALENT_IPS[1]), 9000),
        ]
    )
    segment = _segment(result)
    assert result["status"] == "insufficient_independent_offers"
    assert result["accepted_offer_count"] == segment["independent_offer_count"] == 1
    assert segment["median_input_offer_count"] == 1
    assert segment["median_price_rub"] is None and segment["median_available"] is False
    assert segment["offers"][0]["observation_index"] == 0
    assert segment["offers"][0]["price_rub"] == 1000
    assert result["rejected_observations"] == [{"observation_index": 1, "code": "duplicate_source_in_segment"}]


def test_ordinary_public_ipv4_keeps_exact_safe_url_and_source_key():
    assert market._public_source_url("https://8.8.8.8:8443/offer/1712024?tracking=synthetic#section") == (
        "https://8.8.8.8:8443/offer/1712024",
        "8.8.8.8",
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://[::1]/offer/1712024",
        "https://synthetic-user:synthetic-secret@one.example/offer/1712024",
        "https://[2606:4700:4700::abcd/offer/1712024",
        "https://one.example:65536/offer/1712024",
    ],
    ids=["https-loopback", "userinfo", "broken-ipv6-bracket", "port-out-of-range"],
)
def test_invalid_source_url_cannot_enter_the_assessment(url):
    assert market._public_source_url(url) is None
    rows = [_offer(url, 1000)]
    before = deepcopy(rows)
    result = market.assess_part_market(article="1712024", brand="Ford", observations=rows)
    assert rows == before
    assert result["ok"] is False and result["read_only"] is True
    assert result["status"] == "no_valid_public_evidence"
    assert result["accepted_offer_count"] == 0
    assert result["rejected_observation_count"] == 1
    assert result["rejected_observations"] == [{"observation_index": 0, "code": "observation_fields_invalid"}]
    assert all(segment["independent_offer_count"] == 0 for segment in result["segments"])
    assert all(segment["median_price_rub"] is None for segment in result["segments"])
