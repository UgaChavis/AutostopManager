"""Distinct public IPv6 endpoints retain their authority and discovery metadata."""

from __future__ import annotations

from copy import deepcopy
from urllib.parse import parse_qsl, urlsplit

import pytest

from autostop_manager import j1_fetch, j1_research


HOST = "2606:4700:4700::1111"


@pytest.fixture(params=["canonical", "discovery"])
def key(request):
    return {
        "canonical": j1_research._canonical_url,
        "discovery": j1_fetch._discovery_url_key,
    }[request.param]


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            f"https://[{HOST}]/manual?edition=2&ref=&utm_source=x#section",
            f"https://[{HOST}]/manual?edition=2&ref=",
        ),
        (
            f"https://[{HOST}]:443/manual?edition=2&ref=&utm_source=x#section",
            f"https://[{HOST}]/manual?edition=2&ref=",
        ),
        (
            "http://[2001:4860:4860::ABCD]:80/manual?edition=2&ref=&utm_source=x#section",
            "http://[2001:4860:4860::abcd]/manual?edition=2&ref=",
        ),
        (
            f"https://[{HOST}]:80/manual?edition=2&ref=&utm_source=x#section",
            f"https://[{HOST}]:80/manual?edition=2&ref=",
        ),
        (
            f"http://[{HOST}]:443/manual?edition=2&ref=&utm_source=x#section",
            f"http://[{HOST}]:443/manual?edition=2&ref=",
        ),
    ],
    ids=["implicit-https", "default-https443", "default-http80-uppercase", "https80", "http443"],
)
def test_public_ipv6_keys_preserve_authority_and_roundtrip(key, url, expected):
    assert j1_fetch.public_url(url)

    actual = key(url)

    assert actual == expected
    assert j1_fetch.public_url(actual) == actual
    assert key(actual) == actual
    assert urlsplit(actual).hostname == urlsplit(expected).hostname
    assert urlsplit(actual).port == urlsplit(expected).port


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "HTTPS://Manual.Example.Org:443/manual?edition=2&ref=&utm_source=x#section",
            "https://manual.example.org/manual?edition=2&ref=",
        ),
        (
            "https://8.8.4.4:80/manual?edition=2&ref=&utm_source=x#section",
            "https://8.8.4.4:80/manual?edition=2&ref=",
        ),
    ],
    ids=["DNS-default-port-casefold", "IPv4-nondefault-port"],
)
def test_dns_and_ipv4_keys_keep_exact_existing_normalization(key, url, expected):
    assert j1_fetch.public_url(url)
    assert key(url) == expected
    assert j1_fetch.public_url(expected) == expected
    assert key(expected) == expected


def row(url, marker):
    return {
        "url": url,
        "title": "Service manual " + marker,
        "snippet": "Service manual document " + marker,
        "source": "synthetic",
        "engine": "engine-" + marker,
    }


@pytest.mark.parametrize("scheme,port", [("https", 80), ("http", 443)])
def test_distinct_ipv6_host_and_port_do_not_merge_discovery_metadata(scheme, port):
    first = f"{scheme}://[{HOST}]:{port}/manual?edition=2"
    second = f"{scheme}://[{HOST}:{port}]/manual?edition=2"
    rows = [row(first, "A"), row(second, "longer-B")]
    original = deepcopy(rows)
    assert j1_fetch.public_url(first) and j1_fetch.public_url(second)

    ranked = j1_fetch._rank_discovered_results("service manual", rows)

    assert len(ranked) == 2
    by_url = {item["url"]: item for item in ranked}
    assert set(by_url) == {first, second}
    for supplied in original:
        actual = by_url[supplied["url"]]
        assert actual["title"] == supplied["title"]
        assert actual["snippet"] == supplied["snippet"]
        assert actual["source"] == supplied["source"]
        assert actual["engines"] == [supplied["engine"]]
    for helper in (j1_research._canonical_url, j1_fetch._discovery_url_key):
        assert helper(first) != helper(second)
    assert rows == original


def test_same_ipv6_endpoint_tracking_aliases_still_merge_their_metadata():
    first = f"https://[{HOST}]:443/manual?edition=2&ref=&utm_source=one"
    second = f"https://[{HOST}]/manual?edition=2&ref=&utm_campaign=two"
    rows = [row(first, "A"), row(second, "longer-B")]
    original = deepcopy(rows)

    ranked = j1_fetch._rank_discovered_results("service manual", rows)

    assert len(ranked) == 1
    assert ranked[0]["url"] == first
    assert ranked[0]["title"] == original[1]["title"]
    assert ranked[0]["snippet"] == original[0]["snippet"] + " | " + original[1]["snippet"]
    assert ranked[0]["engines"] == ["engine-A", "engine-longer-B"]
    for helper in (j1_research._canonical_url, j1_fetch._discovery_url_key):
        assert helper(first) == helper(second)
    assert rows == original


def test_meaningful_query_versions_and_blank_ref_remain_separate():
    urls = [f"https://[{HOST}]/manual?edition={edition}&ref=&utm_source=x" for edition in (2, 3)]
    rows = [row(urls[0], "edition-2"), row(urls[1], "edition-3")]

    ranked = j1_fetch._rank_discovered_results("service manual", rows)

    assert {item["url"] for item in ranked} == set(urls)
    for helper in (j1_research._canonical_url, j1_fetch._discovery_url_key):
        assert helper(urls[0]) != helper(urls[1])
        for edition, url in zip((2, 3), urls, strict=True):
            assert parse_qsl(urlsplit(helper(url)).query, keep_blank_values=True) == [
                ("edition", str(edition)),
                ("ref", ""),
            ]


@pytest.mark.parametrize("host", ["::1", "fd00::1"], ids=["loopback", "private"])
def test_private_ipv6_is_still_rejected_before_discovery(host):
    assert j1_fetch.public_url(f"https://[{host}]/manual?edition=2") == ""
