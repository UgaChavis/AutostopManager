"""Legacy PDF page boundaries never guess the page or OCR origin of a hit."""

import pytest

from test_offline_catalog_crlf import cache as cache, install, search as search


@pytest.mark.parametrize("ending", [b"\n", b"\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize(
    "leading,trailing,page",
    [("", "\f", 1), ("", "\f\f", 1), ("\f", "", 2), ("\f\f", "", 3), ("\f", "\f", 2)],
    ids=["trailing", "two-trailing", "leading", "two-leading", "both"],
)
def test_locator_uses_only_page_breaks_before_the_matched_text(cache, search, ending, leading, trailing, page):
    root, _loop, _processes = cache
    install(
        root,
        ["Prior context", leading + "ABC123 first hit" + trailing, "ABC123 next hit"],
        (ending,),
        ocr_pages=[page],
    )

    response = search()

    assert response["warnings"] == [] and response["search_incomplete"] is False
    assert len(response["results"]) == 2
    first, second = response["results"]
    assert first["locator"] == {"page": page}
    assert first["extraction_method"] == "ocr" and first["ocr_unverified"] is True
    assert ("Prior context" in first["excerpt"]) is (not leading)
    end_page = 1 + len(leading) + len(trailing)
    assert second["locator"] == {"page": end_page}
    assert second["ocr_unverified"] is (end_page == page)
    if trailing:
        assert "first hit" not in second["excerpt"]


@pytest.mark.parametrize("ending", [b"\n", b"\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize(
    "ambiguous,query,expected_page",
    [
        ("ABC123 first page\fLast page context", "ABC123", 2),
        ("First page context\fABC123 last page", "ABC123", 2),
        ("ABC123 first page\fABC123 last page", "ABC123", 2),
        ("ABC123 first page\f\fLast page context", "ABC123", 3),
        ("\u0131TEM-123 first page\fITEM-123 last page", "ITEM-123", 2),
        ("Straße first page\fSTRASSE last page", "STRASSE", 2),
    ],
    ids=["before", "after", "both", "empty-page", "unicode-i", "unicode-fold"],
)
def test_ambiguous_line_is_reported_without_losing_other_hits(cache, search, ending, ambiguous, query, expected_page):
    root, _loop, _processes = cache
    install(root, [query + " earlier known hit", ambiguous, query + " later known hit"], (ending,), ocr_pages=[1])

    response = search(query)

    assert response["ok"] is True and response["search_incomplete"] is True
    assert response["warnings"] == ["catalog_locator_unavailable"]
    assert response["scanned_catalogs"] == 1 and response["skipped_catalog_ids"] == []
    assert len(response["results"]) == 2
    first, last = response["results"]
    assert first["locator"] == {"page": 1} and first["ocr_unverified"] is True
    assert last["locator"] == {"page": expected_page} and last["ocr_unverified"] is False
    assert last["extraction_method"] == "text"
    assert "earlier known hit" in first["excerpt"] and "later known hit" in last["excerpt"]
    assert "first page" not in last["excerpt"]


def test_ambiguous_only_match_is_incomplete_instead_of_a_wrong_page(cache, search):
    root, _loop, _processes = cache
    install(root, ["ABC123 first page\fOther page"], (b"\n",), ocr_pages=[2])

    response = search()

    assert response["ok"] is True and response["results"] == []
    assert response["search_incomplete"] is True
    assert response["warnings"] == ["catalog_locator_unavailable"]


def test_last_page_context_remains_bounded_and_redacted(cache, search):
    root, _loop, _processes = cache
    vin = "A" * 17
    install(root, ["Old page context\fCurrent page " + vin, "ABC123 current hit"], (b"\r\n",))

    response = search()

    assert response["warnings"] == [] and len(response["results"]) == 1
    hit = response["results"][0]
    assert hit["locator"] == {"page": 2}
    assert hit["excerpt"] == "Current page [VIN REDACTED] | ABC123 current hit"
    assert vin not in hit["excerpt"] and len(hit["excerpt"]) <= 360


def test_standalone_page_breaks_and_modern_page_markers_keep_their_meaning(cache, search):
    root, _loop, _processes = cache
    install(root, ["Heading", "\f", " \f ", "ABC123 legacy page"], (b"\n",))
    legacy = search()
    assert legacy["warnings"] == [] and legacy["results"][0]["locator"] == {"page": 3}
    assert legacy["results"][0]["excerpt"] == "ABC123 legacy page"

    install(root, ["\f[PAGE 7]", "ABC123 modern page", "\f[PAGE 9]", "ABC123 next page"], (b"\r\n",), ocr_pages=[9])
    modern = search()
    assert modern["warnings"] == []
    assert [hit["locator"] for hit in modern["results"]] == [{"page": 7}, {"page": 9}]
    assert [hit["ocr_unverified"] for hit in modern["results"]] == [False, True]
