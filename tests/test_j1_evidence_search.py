"""Focused evidence-discovery tests for the J1 public-web boundary."""

from __future__ import annotations

from typing import Any

import pytest

from autostop_manager import j1_fetch
from autostop_manager.j1_sources import classify_source, discovery_domain


@pytest.mark.parametrize(
    ("url", "source_class", "tier", "basis"),
    (
        (
            "https://static.nhtsa.gov/odi/tsbs/2021/MC-10190467-9999.pdf",
            "official_registry",
            "A",
            "registry:official:nhtsa.gov:",
        ),
        (
            "https://techinfo.toyota.com/service/8ar.pdf",
            "official_registry",
            "A",
            "registry:official:techinfo.toyota.com:",
        ),
        ("https://www.denso-am.eu/technical/injection", "technical", "B", "registry:technical:denso-am.eu"),
        ("https://partsouq.com/en/catalog/genuine/unit", "supplier_catalog", "C", "registry:supplier:partsouq.com"),
        (
            "https://club-lexus.ru/forum/viewtopic.php?t=1",
            "owner_community",
            "D",
            "registry:owner_community:club-lexus.ru",
        ),
        ("https://oemdtc.com/1234", "technical_reference", "D", "registry:technical_reference:oemdtc.com"),
    ),
)
def test_source_registry_assigns_auditable_tiers(url: str, source_class: str, tier: str, basis: str) -> None:
    classified = classify_source(url)
    assert (classified.source_class, classified.source_tier) == (source_class, tier)
    assert classified.source_basis.startswith(basis)


def test_source_registry_keeps_heuristics_below_official_and_groups_subdomains() -> None:
    community = classify_source("https://forum.example.org/repair/topic")
    unknown = classify_source("https://example.org/technical-note")
    assert (community.source_class, community.source_tier, community.source_basis) == (
        "owner_community",
        "D",
        "heuristic:community_url",
    )
    assert (unknown.source_class, unknown.source_tier) == ("unknown", "unclassified")
    assert discovery_domain("https://static.nhtsa.gov/odi/tsbs/test.pdf") == "nhtsa.gov"


def test_search_merges_all_engines_preserves_snippets_ranks_tiers_and_caps_domains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def engine_results(query: str, _base_url: str) -> list[dict[str, str]]:
        calls.append(query)
        engine = query.split(" ", 1)[0]
        if engine == "!brave":
            return [
                {
                    "url": "https://static.nhtsa.gov/odi/tsbs/2021/8ar.pdf?utm_source=brave",
                    "title": "8AR FTS injector bulletin",
                    "content": "First public technical snippet.",
                    "source": "searxng",
                },
                *[
                    {
                        "url": f"https://example.org/8ar-injector-{number}",
                        "title": "8AR FTS injector notes",
                        "content": f"Duplicate-domain evidence {number}",
                        "source": "searxng",
                    }
                    for number in range(4)
                ],
            ]
        if engine == "!google":
            return [
                {
                    "url": "https://static.nhtsa.gov/odi/tsbs/2021/8ar.pdf",
                    "title": "8AR FTS injector",
                    "content": "Second public technical snippet.",
                    "source": "searxng",
                }
            ]
        if engine == "!qwant":
            return [
                {
                    "url": "https://forum.example.org/8ar-fts-injector",
                    "title": "8AR FTS injector owner report",
                    "content": "Owner observation only.",
                    "source": "searxng",
                }
            ]
        return []

    monkeypatch.setattr(j1_fetch, "_search_searxng", engine_results)
    rows, provider = j1_fetch.search_public("8AR FTS injector technical guide", searxng_url="http://127.0.0.1:8890")

    assert provider == "searxng"
    assert calls == [
        "!brave 8AR FTS injector technical guide",
        "!google 8AR FTS injector technical guide",
        "!qwant 8AR FTS injector technical guide",
        "!yep 8AR FTS injector technical guide",
    ]
    first = rows[0]
    assert (first["source_class"], first["source_tier"], first["search_rank"]) == ("official_registry", "A", 1)
    assert first["engines"] == ["brave", "google"]
    assert "First public technical snippet." in first["snippet"]
    assert "Second public technical snippet." in first["snippet"]
    assert (
        len([row for row in rows if row["url"].startswith("https://example.org/")])
        == j1_fetch.MAX_SEARCH_RESULTS_PER_DOMAIN
    )
    assert any(row["source_tier"] == "D" for row in rows)


def test_search_result_snippets_are_redacted_before_return(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        j1_fetch,
        "_search_searxng",
        lambda *_args: [
            {
                "url": "https://example.org/8ar-injector",
                "title": "8AR injector guide",
                "content": "Contact owner@example.org for 8AR injector details.",
                "source": "searxng",
            }
        ],
    )
    rows, _ = j1_fetch.search_public("8AR injector guide", searxng_url="http://127.0.0.1:8890")
    assert rows[0]["snippet"] == "Contact [redacted] for 8AR injector details."


def test_ddg_parser_preserves_result_snippet() -> None:
    parser = j1_fetch._DDGLinks()
    parser.feed(
        '<a class="result__a" href="https://example.org/8ar">8AR injector guide</a>'
        '<div class="result__snippet">Technical <b>injector</b> context</div>'
    )
    assert parser.rows == [
        {
            "url": "https://example.org/8ar",
            "title": "8AR injector guide",
            "snippet": "Technical injector context",
            "source": "duckduckgo",
        }
    ]


def test_fetch_retries_one_transient_failure_only(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def once(_url: str, *, allow_browser: bool) -> dict[str, Any]:
        calls.append("fetch")
        return {"ok": False, "error": "fetch_failed"} if len(calls) == 1 else {"ok": True, "text": "public"}

    monkeypatch.setattr(j1_fetch, "_fetch_document_once", once)
    result = j1_fetch.fetch_document("https://example.org/article")
    assert calls == ["fetch", "fetch"]
    assert result == {"ok": True, "text": "public"}


@pytest.mark.parametrize(
    "error", ("robots_disallowed", "requires_human", "access_restricted", "unsafe_url", "browser_isolation_unverified")
)
def test_fetch_never_retries_nontransient_or_safety_errors(monkeypatch: pytest.MonkeyPatch, error: str) -> None:
    calls: list[str] = []

    def blocked(_url: str, *, allow_browser: bool) -> dict[str, Any]:
        calls.append("fetch")
        return {"ok": False, "error": error}

    monkeypatch.setattr(j1_fetch, "_fetch_document_once", blocked)
    assert j1_fetch.fetch_document("https://example.org/article") == {"ok": False, "error": error}
    assert calls == ["fetch"]


def test_fetch_honors_a_bounded_single_429_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    waits: list[float] = []

    def limited(_url: str, *, allow_browser: bool) -> dict[str, Any]:
        calls.append("fetch")
        return (
            {"ok": False, "error": "rate_limited", "retry_after_seconds": 999.0}
            if len(calls) == 1
            else {"ok": False, "error": "access_restricted"}
        )

    monkeypatch.setattr(j1_fetch, "_fetch_document_once", limited)
    monkeypatch.setattr(j1_fetch.time, "sleep", waits.append)
    result = j1_fetch.fetch_document("https://example.org/article")
    assert calls == ["fetch", "fetch"]
    assert waits == [3.0]
    assert result == {"ok": False, "error": "access_restricted"}
