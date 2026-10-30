"""URL identity must retain escaped octets without merging different documents."""

from __future__ import annotations

from copy import deepcopy
import socket

import pytest

from autostop_manager import j1_fetch, j1_research, j1_vin_store


@pytest.fixture(autouse=True)
def isolated_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "private"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "0")
    monkeypatch.delenv("AUTOSTOP_J1_SEARXNG_URL", raising=False)
    monkeypatch.setattr(j1_research, "_STOP", False)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("this synthetic URL regression must not contact external services")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(j1_fetch, "_request_public", forbidden)
    monkeypatch.setattr(j1_fetch, "_search_searxng", forbidden)


@pytest.fixture(params=[j1_fetch._discovery_url_key, j1_research._canonical_url], ids=["discovery", "corpus"])
def key(request):
    return request.param


@pytest.mark.parametrize("query", ["part=%C0", "part=%C1", "%FF=value", "part=%ED%A0%80", "part=%EF%BF%BD"])
def test_escaped_octets_survive_key_normalization(key, query):
    url = "https://example.org/catalog?" + query

    assert j1_fetch.public_url(url) == url
    assert key(url) == url
    assert key(key(url)) == url


def test_distinct_non_utf8_values_do_not_merge_discovery_metadata():
    urls = ["https://example.org/catalog?part=%C0", "https://example.org/catalog?part=%C1"]
    rows = [
        {"url": url, "title": f"Part catalog {i}", "snippet": f"Independent page {i}", "engine": f"engine-{i}"}
        for i, url in enumerate(urls)
    ]
    original = deepcopy(rows)

    result = j1_fetch._rank_discovered_results("part catalog", rows)

    assert len(result) == 2
    for found in result:
        supplied = next(row for row in original if row["url"] == found["url"])
        assert found["title"] == supplied["title"]
        assert found["snippet"] == supplied["snippet"]
        assert found["engines"] == [supplied["engine"]]
    assert rows == original


def test_collector_fetches_both_distinct_byte_queries(monkeypatch):
    urls = ["https://example.org/catalog?part=%C0", "https://example.org/catalog?part=%C1"]
    monkeypatch.setattr(j1_research, "_vin_api", lambda _job_id: None)
    monkeypatch.setattr(j1_vin_store, "prune", lambda: None)
    monkeypatch.setattr(
        j1_research, "search_public", lambda *_args, **_kwargs: ([{"url": url} for url in urls], "synthetic")
    )
    fetched = []

    def fetch(url, **_kwargs):
        fetched.append(url)
        return {"ok": False, "error": "synthetic_no_content"}

    monkeypatch.setattr(j1_research, "fetch_document", fetch)
    created = j1_research.start_research("Public part catalog", ["part catalog"], max_pages=2)
    assert created["ok"]
    with j1_research._db() as conn:
        assert j1_research._claim_job(conn) == created["job_id"]

    j1_research._work_job(created["job_id"])

    assert set(fetched) == set(urls) and len(fetched) == 2
    with j1_research._db(readonly=True) as conn:
        stored = conn.execute("SELECT url,canonical_url FROM documents WHERE job_id=?", (created["job_id"],)).fetchall()
    assert {(row["url"], row["canonical_url"]) for row in stored} == {(url, url) for url in urls}


@pytest.mark.parametrize("surrogate", ["\ud800", "\udc00", "\udc80", "\udfff"])
@pytest.mark.parametrize(
    "template",
    ["https://ex{value}ample.org/catalog", "https://example.org/{value}", "https://example.org/?part={value}"],
)
def test_literal_surrogates_are_rejected_without_encoding_errors(key, surrogate, template):
    url = template.format(value=surrogate)

    assert j1_fetch.public_url(url) == ""
    assert key(url) == ""


@pytest.mark.parametrize(
    "query,expected",
    [
        ("part=%c0&part=%C1&empty=&utm_source=one", "part=%C0&part=%C1&empty="),
        ("part=%D0%90&name=service%20manual&utm_campaign=one", "part=%D0%90&name=service+manual"),
        ("part=А&name=service+manual&utm_source=two", "part=%D0%90&name=service+manual"),
    ],
)
def test_tracking_and_standard_query_aliases_still_deduplicate(key, query, expected):
    url = "https://EXAMPLE.org:443/catalog?" + query

    assert key(url) == "https://example.org/catalog?" + expected
