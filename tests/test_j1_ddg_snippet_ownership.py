"""A discarded DDG result cannot replace a retained result's snippet."""

from copy import deepcopy
from html import escape

import pytest

from autostop_manager import j1_fetch


QUERY = "service manual"


def row(number, *, url=None, snippet=None):
    return {
        "url": url or f"https://source{number:02d}.example.org/manual?edition=2",
        "title": f"Service manual title-{number:02d}",
        "snippet": f"Service manual snippet-{number:02d}." if snippet is None else snippet,
    }


def card(item, *, include_snippet=True, missing_href=False, snippet_markup=None, snippet_tag="a"):
    href = "" if missing_href else f' href="{escape(item["url"], quote=True)}"'
    title = f'<a class="result__a"{href}>{escape(item["title"])}</a>'
    if not include_snippet:
        return '<div class="result">' + title + "</div>"
    markup = escape(item["snippet"]) if snippet_markup is None else snippet_markup
    snippet_href = f' href="{escape(item["url"], quote=True)}"' if snippet_tag == "a" else ""
    snippet = f'<{snippet_tag} class="result__snippet"{snippet_href}>{markup}</{snippet_tag}>'
    return '<div class="result">' + title + snippet + "</div>"


def parse(markup):
    parser = j1_fetch._DDGLinks()
    parser.feed(markup)
    return parser.rows


def expected_raw(items):
    return [{**item, "source": "duckduckgo"} for item in items]


@pytest.fixture
def search(monkeypatch):
    def invoke(markup):
        events = []

        def robots(url):
            assert url == "https://html.duckduckgo.com/html/?q=service+manual"
            events.append("robots")
            return True, 0.0

        def rate(url, delay):
            assert url == "https://html.duckduckgo.com" and delay == 0.0
            events.append("rate")

        def request(url, *, max_bytes):
            assert url == "https://html.duckduckgo.com/html/?q=service+manual"
            assert max_bytes == j1_fetch.MAX_HTML_BYTES
            events.append("request")
            return 200, {"content-type": "text/html; charset=utf-8"}, markup.encode("utf-8"), url

        monkeypatch.setattr(j1_fetch, "_robots_policy", robots)
        monkeypatch.setattr(j1_fetch, "_rate_limit", rate)
        monkeypatch.setattr(j1_fetch, "_request_public", request)

        found, provider = j1_fetch.search_public(QUERY, searxng_url="")

        assert provider == "duckduckgo"
        assert events == ["robots", "rate", "request"]
        return found

    return invoke


def assert_public_rows(found, expected):
    by_url = {item["url"]: item for item in found}
    assert len(found) == len(expected) and set(by_url) == {item["url"] for item in expected}
    for item in expected:
        actual = by_url[item["url"]]
        assert actual["title"] == item["title"]
        assert actual["snippet"] == item["snippet"]
        assert actual["source"] == "duckduckgo" and actual["engines"] == ["duckduckgo"]


def test_cap_owner_stays_fixed_after_21st_and_later_discarded_cards():
    retained = [row(number) for number in range(20)]
    parser = j1_fetch._DDGLinks()
    parser.feed("<main>" + "".join(card(item) for item in retained))
    original = deepcopy(parser.rows)
    assert original == expected_raw(retained)

    for number in range(20, 24):
        parser.feed(card(row(number), include_snippet=number != 21))
        assert parser.rows == original

    parser.feed("</main>")
    assert parser.rows == original


@pytest.mark.parametrize("count", [20, 21, 24], ids=["at-cap", "first-discarded", "later-discarded"])
def test_public_search_keeps_first20_owned_metadata(search, count):
    items = [row(number) for number in range(count)]
    body = "<html><body>" + "".join(card(item) for item in items) + "</body></html>"

    found = search(body)

    assert_public_rows(found, items[:20])


@pytest.mark.parametrize("tail", ["missing", "empty"])
def test_discarded_missing_or_empty_snippet_cannot_erase_retained_text(search, tail):
    retained = [row(number) for number in range(20)]
    extra = row(20, snippet="")
    body = "".join(card(item) for item in retained) + card(extra, include_snippet=tail != "missing")

    found = search(body)

    assert_public_rows(found, retained)


def test_retained_missing_snippet_and_next_valid_card_keep_separate_owners():
    first, middle, last = row(0), row(1, snippet=""), row(2)
    body = card(first) + card(middle, include_snippet=False) + card(last)

    assert parse(body) == expected_raw([first, middle, last])


@pytest.mark.parametrize("snippet_tag", ["a", "div"])
def test_same_owner_nested_inline_markup_is_preserved(snippet_tag):
    first = row(0)
    second = row(1, snippet="Service manual nested inline detail.")
    body = card(first) + card(
        second,
        snippet_tag=snippet_tag,
        snippet_markup="Service manual <b>nested <span>inline</span></b> detail.",
    )

    assert parse(body) == expected_raw([first, second])


@pytest.mark.parametrize("discarded", ["missing-href", "private-url"])
def test_unretained_link_resets_owner_before_next_valid_card(discarded):
    first, last = row(0), row(2)
    rejected = row(1, url="http://127.0.0.1/manual" if discarded == "private-url" else None)
    body = card(first) + card(rejected, missing_href=discarded == "missing-href") + card(last)

    assert parse(body) == expected_raw([first, last])


def test_owned_snippet_keeps_existing_600_character_cap(search):
    supplied = row(0, snippet="Service manual " + "technical detail " * 100)

    found = search(card(supplied))

    assert len(found) == 1 and found[0]["url"] == supplied["url"]
    assert len(found[0]["snippet"]) == 600
    assert found[0]["snippet"].startswith("Service manual technical detail")


def test_retained_duplicate_url_cards_keep_their_own_snippets_before_ranking():
    first = row(0)
    second = row(1, url=first["url"])

    assert parse(card(first) + card(second)) == expected_raw([first, second])
