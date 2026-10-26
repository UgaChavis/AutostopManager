from __future__ import annotations

from html import escape
import json
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from autostop_manager import catalog_clients


def _candidate(brand, pid="PID-A"):
    return {"brand": brand, "pid": pid, "part_number": f"DEMO-{pid}"}


class _Response:
    def __init__(self, text):
        self.payload = text.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False

    def read(self, limit=-1):
        return self.payload if limit < 0 else self.payload[:limit]


def _responses(monkeypatch, candidates):
    catalog_html = "".join(
        f'<a href="/Price/?{urlencode({"pid": candidate["pid"]})}">'
        f"<b>{escape(candidate['brand'] or '')}</b> {candidate['part_number']}"
        "<dd>Synthetic part</dd></a>"
        for candidate in candidates
    )
    by_pid = {candidate["pid"]: candidate for candidate in candidates}
    calls = []

    def fake_urlopen(request, timeout=20.0):
        url = urlsplit(request.full_url)
        query = parse_qs(url.query)
        calls.append(query)
        assert timeout == 20.0
        assert request.get_method() == "GET"
        if url.path == "/Api/Parts/Search":
            return _Response("[]")
        if "pcode" in query:
            return _Response(catalog_html)
        if "pid" in query:
            # Every route returns its own product, so choosing the wrong catalog
            # candidate cannot be hidden by an unrelated target-only price page.
            candidate = by_pid[query["pid"][0]]
            data = [
                {
                    "ProductIdEnc": candidate["pid"],
                    "PartNumber": candidate["part_number"],
                    "CatalogName": candidate["brand"],
                    "PartName": "Synthetic part",
                }
            ]
            return _Response(f"<script>var _data = {json.dumps(data)};</script>")
        raise AssertionError("Unexpected synthetic Exist request")

    monkeypatch.setattr(catalog_clients, "urlopen", fake_urlopen)
    return calls


def _assert_selected(result, calls, candidate):
    assert result["ok"] is True
    assert result["needs_disambiguation"] is False
    assert result["benchmark_kind"] == "public_retail_reference"
    assert result["requires_confirmation"] is True
    selected = result["selected_item"]
    assert selected["pid"] == candidate["pid"]
    assert selected["part_number"] == candidate["part_number"]
    assert selected["brand"] == candidate["brand"]
    assert selected["catalog_candidate"]["pid"] == candidate["pid"]
    assert calls[-1] == {"pid": [candidate["pid"]]}
    assert len(calls) == 3


def _assert_unresolved(result, calls):
    assert result["ok"] is True
    assert result["needs_disambiguation"] is True
    assert result["selected_item"] is None
    assert result["benchmark_kind"] == "public_retail_reference"
    assert result["requires_confirmation"] is True
    assert len(calls) == 2
    assert all("pid" not in call for call in calls)


@pytest.mark.parametrize(("target_brand", "foreign_brand"), [("品牌A", "商标A"), ("品牌", "商标")])
@pytest.mark.parametrize("target_first", [True, False])
def test_unicode_request_selects_the_exact_candidate_in_either_order(
    monkeypatch, target_brand, foreign_brand, target_first
):
    target = _candidate(target_brand)
    foreign = _candidate(foreign_brand, "PID-B")
    candidates = [target, foreign] if target_first else [foreign, target]
    calls = _responses(monkeypatch, candidates)

    result = catalog_clients.exist_price_lookup(part_number="SOURCE-QUERY", brand=target_brand)

    _assert_selected(result, calls, target)
    assert len(result["candidates"]) == 2
    assert result["selected_item"]["brand"] != foreign_brand


@pytest.mark.parametrize("unknown_brand", [None, "", "---"])
@pytest.mark.parametrize("target_first", [True, False])
def test_unicode_request_never_selects_an_unknown_brand(monkeypatch, unknown_brand, target_first):
    target = _candidate("品牌")
    unknown = _candidate(unknown_brand, "PID-B")
    candidates = [target, unknown] if target_first else [unknown, target]
    calls = _responses(monkeypatch, candidates)

    result = catalog_clients.exist_price_lookup(part_number="SOURCE-QUERY", brand="品牌")

    _assert_selected(result, calls, target)


@pytest.mark.parametrize("unknown_first", [True, False])
def test_missing_unicode_target_keeps_foreign_and_unknown_candidates_unresolved(monkeypatch, unknown_first):
    foreign = _candidate("商标A", "PID-B")
    unknown = _candidate(None, "PID-C")
    candidates = [unknown, foreign] if unknown_first else [foreign, unknown]
    calls = _responses(monkeypatch, candidates)

    result = catalog_clients.exist_price_lookup(part_number="SOURCE-QUERY", brand="品牌A")

    _assert_unresolved(result, calls)
    assert len(result["candidates"]) == 2


@pytest.mark.parametrize("brand", ["---", "🙂"])
@pytest.mark.parametrize("candidate_brand", [None, "SYNTH-A"])
def test_nonblank_empty_normalized_brand_does_not_select_unknown_or_known_brand(monkeypatch, brand, candidate_brand):
    calls = _responses(monkeypatch, [_candidate(candidate_brand)])

    result = catalog_clients.exist_price_lookup(part_number="SOURCE-QUERY", brand=brand)

    _assert_unresolved(result, calls)


@pytest.mark.parametrize(
    ("requested", "candidate_brand"),
    [
        (" SYNTH-A ", "synth a"),
        ("ТЕСТ-БРЕНД", "тест бренд"),
        ("SYNTH", "SYNTH GROUP"),
        ("SYNTH GROUP", "SYNTH"),
    ],
    ids=[
        "latin-punctuation",
        "cyrillic-case-punctuation",
        "existing-forward-containment",
        "existing-reverse-containment",
    ],
)
def test_existing_formatting_and_nonempty_containment_behavior_is_preserved(monkeypatch, requested, candidate_brand):
    target = _candidate(candidate_brand)
    calls = _responses(monkeypatch, [_candidate("OTHER", "PID-B"), target])

    result = catalog_clients.exist_price_lookup(part_number="SOURCE-QUERY", brand=requested)

    _assert_selected(result, calls, target)


def test_exact_brand_match_still_precedes_an_earlier_containment_candidate(monkeypatch):
    target = _candidate("SYNTH")
    calls = _responses(monkeypatch, [_candidate("SYNTH GROUP", "PID-B"), target])

    result = catalog_clients.exist_price_lookup(part_number="SOURCE-QUERY", brand="SYNTH")

    _assert_selected(result, calls, target)


@pytest.mark.parametrize(
    ("candidates", "selected"),
    [
        ([_candidate("SYNTH-A")], _candidate("SYNTH-A")),
        ([_candidate(None)], _candidate(None)),
        ([_candidate("SYNTH-A"), _candidate("SYNTH-B", "PID-B")], None),
    ],
    ids=["single-known", "single-unknown", "multiple-unresolved"],
)
@pytest.mark.parametrize("brand", [None, "", " \t "])
def test_blank_optional_brand_keeps_single_and_multiple_candidate_behavior(monkeypatch, candidates, selected, brand):
    calls = _responses(monkeypatch, candidates)

    result = catalog_clients.exist_price_lookup(part_number="SOURCE-QUERY", brand=brand)

    assert result["request_plan"]["params"]["brand"] is None
    if selected is None:
        _assert_unresolved(result, calls)
    else:
        _assert_selected(result, calls, selected)
