from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from autostop_manager import j1_fetch, j1_research


QUERY = "Punch P1765 secondary pulley pressure"
ENDPOINT = "http://127.0.0.1:8890"


def _search_http(monkeypatch, responses):
    queries = []

    class Response:
        def __init__(self, status, body):
            self.status = status
            self.body = body

        def read(self, count):
            return self.body[:count]

        def close(self):
            pass

    class Connection:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, _method, target, **_kwargs):
            query = parse_qs(urlsplit(target).query)["q"][0]
            queries.append(query)
            self.response = responses[query]

        def getresponse(self):
            if isinstance(self.response, Exception):
                raise self.response
            status, body = self.response
            return Response(status, body if isinstance(body, bytes) else json.dumps(body).encode())

        def close(self):
            pass

    monkeypatch.setattr(j1_fetch.http.client, "HTTPConnection", Connection)
    return queries


def _duckduckgo_http(monkeypatch, status, body):
    http_calls = []
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (True, 1.0))
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)

    def request(url, **_kwargs):
        http_calls.append(url)
        return status, {"content-type": "text/html"}, body, url

    monkeypatch.setattr(j1_fetch, "_request_public", request)
    return http_calls


def test_weak_explicit_results_merge_with_default_and_keep_upstream_diagnostics(monkeypatch):
    forum = "https://www.drive2.ru/l/101"
    bulletin = "https://static.nhtsa.gov/odi/tsbs/2021/punch.pdf"
    forum_row = {"url": forum, "title": "Punch P1765 secondary pulley pressure forum discussion"}
    queries = _search_http(
        monkeypatch,
        {
            f"!bing {QUERY}": (200, {"results": [forum_row]}),
            f"!yahoo {QUERY}": (
                200,
                {
                    "results": [],
                    "unresponsive_engines": [
                        ["yahoo", "HTTP error from https://example.invalid/?token=private-token-value"],
                        ["private-contact@example.invalid", "CAPTCHA for private source"],
                    ],
                },
            ),
            QUERY: (
                200,
                {
                    "results": [
                        {"url": bulletin, "title": "Punch P1765 secondary pulley pressure service bulletin"},
                        {**forum_row, "content": "Public forum describes another transmission family."},
                        {"url": "http://127.0.0.1/private", "title": QUERY},
                    ]
                },
            ),
        },
    )
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: pytest.fail("unexpected public fallback"))

    with j1_fetch.capture_search_attempts() as attempts:
        rows, source = j1_fetch.search_public(QUERY, searxng_url=ENDPOINT)

    assert source == "searxng"
    assert queries == [f"!bing {QUERY}", f"!yahoo {QUERY}", QUERY]
    assert [(row["url"], row["engines"], row["search_rank"]) for row in rows] == [
        (bulletin, ["searxng_default"], 1),
        (forum, ["bing", "searxng_default"], 2),
    ]
    assert [row["outcome"] for row in attempts] == ["results", "empty", "results"]
    assert attempts[1] == {
        "provider": "searxng",
        "engine": "yahoo",
        "outcome": "empty",
        "http_status": 200,
        "received": 0,
        "candidates": 0,
        "upstream_failures": [
            {"engine": "yahoo", "reason": "http_error"},
            {"engine": "other", "reason": "access_challenge"},
        ],
    }
    assert (attempts[2]["received"], attempts[2]["candidates"]) == (3, 2)
    diagnostics = json.dumps(attempts)
    assert not any(value in diagnostics for value in (QUERY, bulletin, forum, "private", "token", "https://"))


def test_search_attempts_tell_clean_empty_results_from_failures(monkeypatch):
    _search_http(
        monkeypatch,
        {
            f"!bing {QUERY}": (200, {"results": []}),
            f"!yahoo {QUERY}": (200, {"results": [{"url": "https://example.org/unrelated", "title": "Blue whales"}]}),
            QUERY: (200, {"results": []}),
        },
    )
    _duckduckgo_http(monkeypatch, 200, b'<div class="result result--no-result">No results.</div>')
    with j1_fetch.capture_search_attempts() as attempts:
        assert j1_fetch.search_public(QUERY, searxng_url=ENDPOINT) == ([], "search_unavailable")
    assert [attempt["outcome"] for attempt in attempts] == ["empty", "no_relevant_results", "empty", "empty"]
    assert all(attempt["http_status"] == 200 and attempt["candidates"] == 0 for attempt in attempts)
    assert all("upstream_failures" not in attempt for attempt in attempts)


@pytest.mark.parametrize(
    ("body", "outcome"),
    [
        (b"<html><h1>Please sign in</h1></html>", "invalid_response"),
        (b'<html><a class="result__a">Unfinished result</a></html>', "invalid_response"),
        (b'<a class="result__a" href="http://127.0.0.1/private">Filtered result</a>', "no_relevant_results"),
    ],
)
def test_duckduckgo_200_without_public_links_requires_recognized_search_markup(monkeypatch, body, outcome):
    _duckduckgo_http(monkeypatch, 200, body)
    with j1_fetch.capture_search_attempts() as attempts:
        assert j1_fetch.search_public(QUERY) == ([], "search_unavailable")
    assert [(item["outcome"], item["http_status"], item["candidates"]) for item in attempts] == [(outcome, 200, 0)]


@pytest.mark.parametrize(
    ("body", "outcome", "collection"),
    [
        (b"<html><h1>Please sign in</h1></html>", "invalid_response", "search_failed"),
        (b'<div class="result result--no-result">No results.</div>', "empty", "no_sources"),
    ],
)
def test_duckduckgo_200_outcome_reaches_public_worker_report(monkeypatch, tmp_path, body, outcome, collection):
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "vin"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "0")
    monkeypatch.delenv("AUTOSTOP_J1_SEARXNG_URL", raising=False)
    monkeypatch.setattr(j1_research, "_STOP", False)
    monkeypatch.setattr(j1_research.signal, "signal", lambda *_args: None)
    _duckduckgo_http(monkeypatch, 200, body)
    job_id = j1_research.start_research("Public technical information", [QUERY], max_pages=1)["job_id"]
    j1_research.run_worker(once=True)

    status = j1_research.research_status(job_id)
    report = j1_research.research_report(job_id)
    assert status["collection_outcome"]["state"] == collection
    assert report["collection_outcome"] == status["collection_outcome"]
    assert status["search_attempts"] == report["report"]["search_attempts"]
    assert [attempt["outcome"] for attempt in status["search_attempts"]] == [outcome]


@pytest.mark.parametrize("status", [200, 202])
def test_captured_duckduckgo_challenge_is_an_access_denial(monkeypatch, status):
    calls = _duckduckgo_http(
        monkeypatch,
        status,
        b'<form id="challenge-form"></form><a class="result__a" href="https://example.org/unsafe">Unused</a>',
    )
    with j1_fetch.capture_search_attempts() as attempts:
        assert j1_fetch.search_public(QUERY) == ([], "search_unavailable")
    assert len(calls) == 1
    assert attempts == [
        {"provider": "duckduckgo", "engine": "duckduckgo", "outcome": "access_challenge", "http_status": status}
    ]


def test_transport_schema_http_and_robots_failures_have_distinct_outcomes(monkeypatch):
    _search_http(
        monkeypatch,
        {
            f"!bing {QUERY}": TimeoutError("sensitive transport detail"),
            f"!yahoo {QUERY}": (500, b"No results"),
            QUERY: (200, {"results": "malformed"}),
        },
    )
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (False, 1.0))
    monkeypatch.setattr(j1_fetch, "_request_public", lambda *_args, **_kwargs: pytest.fail("robots denied"))
    with j1_fetch.capture_search_attempts() as attempts:
        assert j1_fetch.search_public(QUERY, searxng_url=ENDPOINT) == ([], "search_unavailable")
    assert [(item["engine"], item["outcome"], item.get("http_status")) for item in attempts] == [
        ("bing", "timeout", None),
        ("yahoo", "http_server_error", 500),
        ("searxng_default", "invalid_response", 200),
        ("duckduckgo", "robots_denied", None),
    ]
    assert "sensitive transport detail" not in json.dumps(attempts)


def test_attempt_capture_is_bounded_and_does_not_escape_context(monkeypatch):
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (False, 1.0))
    with j1_fetch.capture_search_attempts() as outer:
        assert j1_fetch.search_public("https://example.org/document")[1] == "direct_url"
        assert j1_fetch.search_public("contact private@example.invalid")[1] == "sensitive_query"
        with j1_fetch.capture_search_attempts() as inner:
            for _ in range(j1_fetch.MAX_SEARCH_ATTEMPTS + 2):
                assert j1_fetch.search_public(QUERY) == ([], "search_unavailable")
        assert len(inner) == j1_fetch.MAX_SEARCH_ATTEMPTS
        assert outer == []
    assert j1_fetch.search_public(QUERY) == ([], "search_unavailable")
    assert outer == []


def test_automotive_search_uses_default_before_explicit_engines(monkeypatch):
    bulletin = "https://www.nhtsa.gov/punch-technical-bulletin.pdf"
    response = (200, {"results": [{"url": bulletin, "title": QUERY}]})
    calls = _search_http(
        monkeypatch,
        {QUERY: response, f"!bing {QUERY}": response, f"!yahoo {QUERY}": (200, {"results": []})},
    )
    with j1_fetch.automotive_search_routing({}):
        rows, provider = j1_fetch.search_public(QUERY, searxng_url=ENDPOINT)
        assert [(row["url"], row["engines"]) for row in rows] == [(bulletin, ["searxng_default"])]
        assert provider == "searxng"
        assert calls == [QUERY]

    # Legacy/general direct callers still start with the explicit engines.
    rows, provider = j1_fetch.search_public(QUERY, searxng_url=ENDPOINT)
    assert [(row["url"], row["engines"]) for row in rows] == [(bulletin, ["bing"])]
    assert provider == "searxng"
    assert calls == [QUERY, f"!bing {QUERY}", f"!yahoo {QUERY}"]


def test_automotive_search_merges_weak_default_results_with_explicit_engines(monkeypatch):
    forum = "https://www.drive2.ru/l/101"
    bulletin = "https://www.nhtsa.gov/punch-technical-bulletin.pdf"
    calls = _search_http(
        monkeypatch,
        {
            QUERY: (200, {"results": [{"url": forum, "title": QUERY}]}),
            f"!bing {QUERY}": (200, {"results": [{"url": bulletin, "title": QUERY}]}),
            f"!yahoo {QUERY}": (200, {"results": [{"url": forum, "title": QUERY}]}),
        },
    )
    with j1_fetch.automotive_search_routing({}), j1_fetch.capture_search_attempts() as attempts:
        rows, provider = j1_fetch.search_public(QUERY, searxng_url=ENDPOINT)
    assert provider == "searxng"
    assert calls == [QUERY, f"!bing {QUERY}", f"!yahoo {QUERY}"]
    assert [(row["url"], row["engines"]) for row in rows] == [
        (bulletin, ["bing"]),
        (forum, ["searxng_default", "yahoo"]),
    ]
    assert [attempt["outcome"] for attempt in attempts] == ["results"] * 3


@pytest.mark.parametrize(
    ("url", "title"),
    [
        ("https://partsouq.com/synthetic/sensor", "Punch secondary pulley pressure sensor catalogue"),
        ("https://www.nhtsa.gov/synthetic/technical-note", "Punch secondary pulley pressure technical note"),
        ("https://www.nhtsa.gov/synthetic/other-bulletin", "Punch P17650 secondary pulley pressure technical note"),
    ],
)
def test_automotive_search_does_not_stop_when_default_misses_identifier(monkeypatch, url, title):
    bulletin = "https://www.nhtsa.gov/synthetic/p1765-diagnostic.pdf"
    calls = _search_http(
        monkeypatch,
        {
            QUERY: (200, {"results": [{"url": url, "title": title}]}),
            f"!bing {QUERY}": (200, {"results": [{"url": bulletin, "title": QUERY}]}),
            f"!yahoo {QUERY}": (200, {"results": []}),
        },
    )
    with j1_fetch.automotive_search_routing({}):
        rows, provider = j1_fetch.search_public(QUERY, searxng_url=ENDPOINT)
    assert provider == "searxng"
    assert calls == [QUERY, f"!bing {QUERY}", f"!yahoo {QUERY}"]
    assert rows[0]["url"] == bulletin


def test_automotive_search_can_stop_with_exact_catalog_identifier(monkeypatch):
    query = "Fiat Ducato, 6000631976, hydraulic actuator"
    catalog = "https://partsouq.com/synthetic/6000631976"
    calls = _search_http(monkeypatch, {query: (200, {"results": [{"url": catalog, "title": query}]})})
    with j1_fetch.automotive_search_routing({}):
        rows, provider = j1_fetch.search_public(query, searxng_url=ENDPOINT)
    assert (provider, [row["url"] for row in rows], calls) == ("searxng", [catalog], [query])


def test_generic_search_keeps_prior_catalog_early_stop(monkeypatch):
    catalog = "https://partsouq.com/synthetic/pressure-sensor"
    calls = _search_http(
        monkeypatch,
        {
            f"!bing {QUERY}": (200, {"results": [{"url": catalog, "title": "Punch secondary pulley pressure"}]}),
            f"!yahoo {QUERY}": (200, {"results": []}),
        },
    )
    rows, provider = j1_fetch.search_public(QUERY, searxng_url=ENDPOINT)
    assert (provider, [row["url"] for row in rows], calls) == (
        "searxng",
        [catalog],
        [f"!bing {QUERY}", f"!yahoo {QUERY}"],
    )


@pytest.mark.parametrize("status", [401, 403, 429])
def test_automotive_search_holds_all_searxng_selectors_after_endpoint_denial(monkeypatch, status):
    calls = _search_http(monkeypatch, {QUERY: (status, b"Unavailable")})
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (False, 1.0))
    holdoffs: dict[str, str] = {}
    with j1_fetch.automotive_search_routing(holdoffs), j1_fetch.capture_search_attempts() as attempts:
        assert j1_fetch.search_public(QUERY, searxng_url=ENDPOINT) == ([], "search_unavailable")
    reason = "rate_limited" if status == 429 else "access_restricted"
    assert calls == [QUERY]
    assert holdoffs == {"searxng": reason}
    assert [(item["engine"], item.get("holdoff_reason")) for item in attempts] == [
        ("searxng_default", None),
        ("bing", reason),
        ("yahoo", reason),
        ("duckduckgo", None),
    ]


def test_automotive_search_does_not_repeat_challenge_within_job(monkeypatch):
    calls = _duckduckgo_http(monkeypatch, 202, b'<form id="challenge-form"></form>')
    holdoffs: dict[str, str] = {}
    with j1_fetch.automotive_search_routing(holdoffs):
        assert j1_fetch.search_public(QUERY) == ([], "search_unavailable")
    with j1_fetch.automotive_search_routing(holdoffs), j1_fetch.capture_search_attempts() as attempts:
        assert j1_fetch.search_public("Punch P1765 VT3S") == ([], "search_unavailable")
    assert len(calls) == 1
    assert attempts == [
        {
            "provider": "duckduckgo",
            "engine": "duckduckgo",
            "outcome": "held_off",
            "holdoff_reason": "access_challenge",
        }
    ]
    with j1_fetch.automotive_search_routing({}):
        assert j1_fetch.search_public("Punch VT3S service manual") == ([], "search_unavailable")
    assert len(calls) == 2


def test_duckduckgo_robots_denial_does_not_hold_off_allowed_queries(monkeypatch):
    calls = []

    def request(url, **_kwargs):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return 200, {}, b"User-agent: *\nDisallow: /html/?q=restricted\n", url
        return 200, {}, b'<a class="result__a" href="https://example.org/guide">Public brake guide</a>', url

    monkeypatch.setattr(j1_fetch, "_ROBOTS", {})
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)
    monkeypatch.setattr(j1_fetch, "_request_public", request)
    holdoffs: dict[str, str] = {}
    with j1_fetch.automotive_search_routing(holdoffs), j1_fetch.capture_search_attempts() as attempts:
        assert j1_fetch.search_public("restricted") == ([], "search_unavailable")
        rows, provider = j1_fetch.search_public("public brake guide")
    assert provider == "duckduckgo"
    assert [row["url"] for row in rows] == ["https://example.org/guide"]
    assert [item["outcome"] for item in attempts] == ["robots_denied", "results"]
    assert calls == ["https://html.duckduckgo.com/robots.txt", "https://html.duckduckgo.com/html/?q=public+brake+guide"]
    assert holdoffs == {}


def test_automotive_search_keeps_provider_eligible_after_upstream_and_server_errors(monkeypatch):
    second_query = "Punch P1765 VT3S"
    empty = (200, {"results": []})
    upstream_errors = (200, {"results": [], "unresponsive_engines": [["yahoo", "HTTP error"], ["bing", "Suspended"]]})
    calls = _search_http(
        monkeypatch,
        {
            QUERY: upstream_errors,
            f"!bing {QUERY}": TimeoutError(),
            f"!yahoo {QUERY}": (500, b"Server error"),
            second_query: empty,
            f"!bing {second_query}": empty,
            f"!yahoo {second_query}": empty,
        },
    )
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (False, 1.0))
    holdoffs: dict[str, str] = {}
    with j1_fetch.automotive_search_routing(holdoffs):
        j1_fetch.search_public(QUERY, searxng_url=ENDPOINT)
        j1_fetch.search_public(second_query, searxng_url=ENDPOINT)
    assert calls == [
        QUERY,
        f"!bing {QUERY}",
        f"!yahoo {QUERY}",
        second_query,
        f"!bing {second_query}",
        f"!yahoo {second_query}",
    ]
    assert holdoffs == {}
