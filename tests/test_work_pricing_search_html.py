from __future__ import annotations

from contextlib import closing
from io import BytesIO
import subprocess
import sys
from urllib.parse import quote

import pytest

from autostop_manager import work_pricing_research as research


def _search(monkeypatch, markup: str) -> list[dict[str, str]]:
    monkeypatch.setattr(research, "urlopen", lambda *_args, **_kwargs: closing(BytesIO(markup.encode())))
    return research._ddg_search("synthetic labor search")["results"]


def _title(number: int = 1, *, attrs: str = "") -> str:
    return f'<a rel="nofollow" class="result__a" href="https://source{number}.test/" {attrs}>Work {number}</a>'


def _snippet(number: int = 1) -> str:
    return f'<a class="result__snippet">Only labor {number}000 rub.</a>'


@pytest.mark.parametrize(
    "anchor",
    [
        '<a rel="nofollow" class="result__a" href="https://source1.test/">Work 1</a>',
        '<a href="https://source1.test/" class="result__a">Work 1</a>',
        "<a class='result__a' href='https://source1.test/'>Work 1</a>",
        '<a class="other result__a selected" href="https://source1.test/">Work 1</a>',
        '<A CLASS="result__a" HREF="https://source1.test/">Work 1</A>',
        '<a class="result&#95;_a" href="https://source1.test/">Work 1</a>',
        '<a class="result__a" href="https://source1.test/" data-note="1 > 0">Work 1</a>',
    ],
)
def test_equivalent_link_attributes_preserve_result(monkeypatch, anchor):
    assert _search(monkeypatch, f'<div class="result">{anchor}{_snippet()}</div></div>') == [
        {
            "source": "source1.test",
            "title": "Work 1",
            "url": "https://source1.test/",
            "snippet": "Only labor 1000 rub.",
        }
    ]


@pytest.mark.parametrize("snippet_tag", ["a", "div", "span"])
def test_nested_card_keeps_its_own_snippet(monkeypatch, snippet_tag):
    markup = (
        f'<div class="result other">{_title()}<div><div>metadata</div></div>'
        f'<{snippet_tag} class="other result__snippet">Only <b>labor</b> 1000 rub.</{snippet_tag}></div>'
        f'<div class="result">{_title(2)}{_snippet(2)}</div>'
    )
    rows = _search(monkeypatch, markup)
    assert [(row["url"], row["snippet"]) for row in rows] == [
        ("https://source1.test/", "Only labor 1000 rub."),
        ("https://source2.test/", "Only labor 2000 rub."),
    ]


@pytest.mark.parametrize("container", [False, True])
def test_missing_snippet_never_borrows_the_next_results_price(monkeypatch, container):
    first = _title()
    second = _title(2) + _snippet(2)
    markup = f'<div class="result">{first}</div><div class="result">{second}</div>' if container else first + second
    rows = _search(monkeypatch, markup + "</div>")
    assert [(row["url"], row["snippet"]) for row in rows] == [
        ("https://source1.test/", "Work 1"),
        ("https://source2.test/", "Only labor 2000 rub."),
    ]


def test_evidence_price_retains_the_source_that_published_it(monkeypatch):
    markup = (
        '<a rel="nofollow" class="result__a" href="https://one.test/">Замена масла</a>'
        '<a rel="nofollow" class="result__a" href="https://two.test/">Замена масла</a>'
        '<a class="result__snippet">Только работа 2000 руб.</a></div>'
    )
    evidence = []
    for row in _search(monkeypatch, markup):
        parsed, _ = research.parse_public_work_evidence(
            row, "замена масла", [{"normalized_name": "замена масла"}], "labor_prices"
        )
        evidence.extend(parsed)
    assert [
        (row["source"]["primary_lineage"], row["source"]["locator"], row["price_rub"], row["labor_only"])
        for row in evidence
    ] == [("two.test", "https://two.test/", 2000, True)]


@pytest.mark.parametrize("boundary", ['<a class="result__a">Missing URL</a>', '<div class="result"></div>'])
def test_invalid_next_result_or_empty_card_resets_snippet_target(monkeypatch, boundary):
    rows = _search(monkeypatch, _title() + boundary + _snippet(2) + "</div>")
    assert [(row["url"], row["snippet"]) for row in rows] == [("https://source1.test/", "Work 1")]


def test_mixed_card_and_bare_result_are_both_retained(monkeypatch):
    rows = _search(monkeypatch, f'<div class="result">{_title()}{_snippet()}</div></div>{_title(2)}{_snippet(2)}')
    assert [row["url"] for row in rows] == ["https://source1.test/", "https://source2.test/"]


@pytest.mark.parametrize(
    "wrapper",
    ["<!--{}-->", "<script>{}</script>", "<style>{}</style>", "<template>{}</template>", "<textarea>{}</textarea>"],
)
def test_inactive_markup_is_not_a_source(monkeypatch, wrapper):
    hidden = f'<div class="result">{_title()}{_snippet()}</div></div>'
    visible = f'<div class="result">{_title(2)}{_snippet(2)}</div></div>'
    assert [row["url"] for row in _search(monkeypatch, wrapper.format(hidden) + visible)] == ["https://source2.test/"]


@pytest.mark.parametrize(
    "anchor",
    [
        '<a data-class="result__a" data-href="https://wrong.test/">Wrong</a>',
        '<a class="not-result__a" href="https://wrong.test/">Wrong</a>',
        '<a class="result__a" href="" href="https://wrong.test/">Wrong</a>',
    ],
)
def test_similar_attributes_and_classes_are_not_result_links(monkeypatch, anchor):
    assert _search(monkeypatch, f'<div class="result">{anchor}{_snippet()}</div></div>') == []


def test_entities_are_decoded_once_before_whitespace_normalization(monkeypatch):
    markup = (
        '<div class="result"><a class="result__a" href="https://source1.test/?a=1&amp;b=2">'
        "Work&nbsp;&nbsp;1 &#10; &lt;part&gt; &amp;lt;tag&amp;gt;</a>"
        '<a class="result__snippet">Only&nbsp; labor &#10; &lt;b&gt;1000&lt;/b&gt;</a></div></div>'
    )
    row = _search(monkeypatch, markup)[0]
    assert row["url"] == "https://source1.test/?a=1&b=2"
    assert row["title"] == "Work 1 <part> &lt;tag&gt;"
    assert row["snippet"] == "Only labor <b>1000</b>"


@pytest.mark.parametrize("ending", ["", '<a class="result__snippet">Partial price 999', '<a class="result__snippet">'])
def test_truncated_prefix_keeps_complete_title_without_partial_snippet(monkeypatch, ending):
    rows = _search(monkeypatch, f'<div class="result">{_title()}{ending}')
    assert [(row["title"], row["snippet"]) for row in rows] == [("Work 1", "Work 1")]


def test_unclosed_title_cannot_capture_another_links_closing_tag(monkeypatch):
    markup = '<div class="result"><a class="result__a" href="https://wrong.test/">Partial'
    rows = _search(monkeypatch, markup + '<a href="https://other.test/">Other</a></div>' + _title(2) + _snippet(2))
    assert [row["url"] for row in rows] == ["https://source2.test/"]


@pytest.mark.parametrize("snippet_tag", ["a", "div", "span"])
def test_nested_link_cannot_complete_an_anchor_snippet(monkeypatch, snippet_tag):
    markup = f'<div class="result">{_title()}<{snippet_tag} class="result__snippet">'
    markup += f'<a href="https://other.test/">Только работа 9000 руб.</a></{snippet_tag}></div>'
    rows = _search(monkeypatch, markup)
    expected = "Work 1" if snippet_tag == "a" else "Только работа 9000 руб."
    assert rows[0]["snippet"] == expected
    parsed, _ = research.parse_public_work_evidence(rows[0], "synthetic", [], "labor_prices")
    assert [row["price_rub"] for row in parsed] == ([] if snippet_tag == "a" else [9000])


def test_inert_content_cannot_close_a_visible_title_or_container(monkeypatch):
    markup = '<div class="result"><a class="result__a" href="https://source1.test/">Work '
    markup += '<template></a></div><a class="result__a" href="https://wrong.test/">Wrong</a></template>'
    markup += "1</a>" + _snippet() + "</div>"
    rows = _search(monkeypatch, markup)
    assert [(row["url"], row["title"], row["snippet"]) for row in rows] == [
        ("https://source1.test/", "Work 1", "Only labor 1000 rub.")
    ]


def test_hidden_and_void_content_cannot_supply_a_snippet(monkeypatch):
    markup = f'<div class="result">{_title()}<img class="result__snippet">Not a snippet'
    markup += '<div hidden><a class="result__snippet">Hidden 9000 rub.</a></div></div>'
    rows = _search(monkeypatch, markup + _title(2) + _snippet(2))
    assert [row["snippet"] for row in rows] == ["Work 1", "Only labor 2000 rub."]


def test_five_valid_results_not_five_arbitrary_blocks(monkeypatch):
    empty = '<div class="result"><div></div></div>' * 6
    cards = "".join(f'<div class="result">{_title(i)}{_snippet(i)}</div></div>' for i in range(1, 8))
    rows = _search(monkeypatch, empty + cards)
    assert [row["url"] for row in rows] == [f"https://source{i}.test/" for i in range(1, 6)]
    assert rows[-1]["snippet"] == "Only labor 5000 rub."


def test_text_lengths_and_empty_output_contract(monkeypatch):
    markup = f'<div class="result"><a class="result__a" href="https://source1.test/">{"T" * 150}</a>'
    markup += f'<a class="result__snippet">{"S" * 250}</a></div></div>'
    row = _search(monkeypatch, markup)[0]
    assert row["title"] == "T" * 140
    assert row["snippet"] == "S" * 240
    assert _search(monkeypatch, "<div>No results</div>") == []


@pytest.mark.parametrize(
    "url, source",
    [
        ("https://www.source.test/path", "source.test"),
        ("/relative", "public_web_search"),
        ("https://[bad", "public_web_search"),
    ],
)
def test_one_unparseable_source_does_not_lose_other_results(monkeypatch, url, source):
    rows = _search(
        monkeypatch,
        f'<div class="result"><a class="result__a" href="{url}">One</a>{_snippet()}</div></div>'
        + _title(2)
        + _snippet(2),
    )
    assert [row["source"] for row in rows] == [source, "source2.test"]


def test_redirect_source_keeps_raw_locator_and_handles_excessive_wrappers(monkeypatch):
    url = "https://www.source.test/path?a=1&b=2"
    for _ in range(3):
        url = "https://duckduckgo.com/l/?uddg=" + quote(url, safe="")
    row = _search(
        monkeypatch, f'<div class="result"><a class="result__a" href="{url}">One</a>{_snippet()}</div></div>'
    )[0]
    assert row["url"] == url
    assert row["source"] == "source.test"
    assert research._safe_source_from_url("?uddg=" * 1100 + "https://source.test/") == "public_web_search"


@pytest.mark.parametrize(
    "fragment", ['<a rel="nofollow" class="result__a">x</a>', '<div class="result">', "<script>", "<"]
)
def test_response_limit_worth_of_malformed_html_finishes_in_a_bounded_process(fragment):
    # The former regex fallback took >5s for just 30KB; the transport permits 250KB.
    # A separate process makes a parser regression fail without hanging pytest.
    code = """
from contextlib import closing
from io import BytesIO
import sys
from unittest.mock import patch
from autostop_manager import work_pricing_research as research
fragment = sys.argv[1]
raw = (fragment * (research.MAX_PUBLIC_RESPONSE_BYTES // len(fragment))).encode()
with patch.object(research, 'urlopen', lambda *a, **kw: closing(BytesIO(raw))):
    assert research._ddg_search('synthetic')['results'] == []
"""
    subprocess.run([sys.executable, "-c", code, fragment], check=True, timeout=5, capture_output=True)
