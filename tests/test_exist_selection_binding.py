from __future__ import annotations

from copy import deepcopy
from html import escape
import json
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from autostop_manager import catalog_clients


def _item(*, pid="PID-A", article="DEMO-A", brand="BRAND-A"):
    return {"ProductIdEnc": pid, "PartNumber": article, "CatalogName": brand, "PartName": "Synthetic part"}


def _candidate(*, pid="PID-A", article="DEMO-A", brand="BRAND-A"):
    return {"pid": pid, "part_number": article, "brand": brand}


def _catalog_html(candidates):
    return "".join(
        f'<a href="/Price/?{urlencode({"pid": candidate["pid"]})}">'
        f"<b>{escape(candidate['brand'] or '')}</b> {escape(candidate['part_number'] or '')}"
        "<dd>Synthetic part</dd></a>"
        for candidate in candidates
    )


def _price_html(items, *, hidden_pid="FORM-CONTEXT", hidden_hash="SYNTHETIC-HASH"):
    return (
        f'<input id="hdnPid" value="{escape(hidden_pid)}">'
        f'<input id="hfPidHash" value="{escape(hidden_hash)}">'
        '<input id="hfSrcId" value="RawPartNumber">'
        f"<script>var _data = {json.dumps(items)};</script>"
    )


class _Response:
    def __init__(self, text):
        self.payload = text.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False

    def read(self, limit=-1):
        return self.payload if limit < 0 else self.payload[:limit]


def _responses(monkeypatch, items, *, candidates=None, hidden_pid="FORM-CONTEXT", hidden_hash="SYNTHETIC-HASH"):
    calls = []
    price_html = _price_html(items, hidden_pid=hidden_pid, hidden_hash=hidden_hash)

    def fake_urlopen(request, timeout=20.0):
        url = urlsplit(request.full_url)
        query = parse_qs(url.query)
        body = json.loads(request.data) if request.data else None
        calls.append({"method": request.get_method(), "path": url.path, "query": query, "body": body})
        assert timeout == 20.0
        if request.get_method() == "POST":
            assert url.path == "/Price/Default.aspx/GetQuery"
            return _Response(json.dumps({"d": "[]"}))
        if url.path == "/Api/Parts/Search":
            return _Response("[]")
        if "pcode" in query:
            return _Response(_catalog_html(candidates) if candidates is not None else price_html)
        if "pid" in query:
            return _Response(price_html)
        raise AssertionError("Unexpected synthetic Exist request")

    monkeypatch.setattr(catalog_clients, "urlopen", fake_urlopen)
    return calls


def _assert_reference(result):
    assert result["ok"] is True
    assert result["benchmark_kind"] == "public_retail_reference"
    assert result["requires_confirmation"] is True
    assert result["privacy"]["returns_raw_html"] is False


@pytest.mark.parametrize("foreign_first", [True, False])
def test_catalog_selection_follows_identity_not_price_order(monkeypatch, foreign_first):
    target = _item()
    foreign = _item(pid="PID-B", article="DEMO-B", brand="BRAND-B")
    items = [foreign, target] if foreign_first else [target, foreign]
    _responses(monkeypatch, items, candidates=[_candidate()])

    result = catalog_clients.exist_price_lookup(part_number="DEMO-A", brand="BRAND-A")

    _assert_reference(result)
    assert result["needs_disambiguation"] is False
    assert len(result["items"]) == 2
    selected = result["selected_item"]
    assert selected["pid"] == "PID-A"
    assert selected["catalog_candidate"]["pid"] == "PID-A"
    other = next(item for item in result["items"] if item["pid"] == "PID-B")
    assert other["part_number"] == "DEMO-B"
    assert "catalog_candidate" not in other


@pytest.mark.parametrize(
    "item",
    [
        _item(pid="PID-B", article="DEMO-B", brand="BRAND-B"),
        _item(pid="PID-B"),
        _item(article="DEMO-B"),
        _item(brand="BRAND-B"),
        {},
        _item(pid=None, article=None),
        _item(pid=None, brand=None),
    ],
    ids=["foreign", "pid-conflict", "article-conflict", "brand-conflict", "unknown", "brand-only", "article-only"],
)
def test_catalog_selection_never_attaches_candidate_to_unbound_row(monkeypatch, item):
    _responses(monkeypatch, [item], candidates=[_candidate()])

    result = catalog_clients.exist_price_lookup(part_number="DEMO-A", brand="BRAND-A")

    _assert_reference(result)
    assert result["needs_disambiguation"] is True
    assert result["selected_item"] is None
    assert len(result["items"]) == 1
    assert "catalog_candidate" not in result["items"][0]


@pytest.mark.parametrize(
    "item",
    [_item(article=None, brand=None), _item(pid=None)],
    ids=["pid-anchor", "article-brand-anchor"],
)
def test_catalog_selection_accepts_proven_identity_with_optional_fields_missing(monkeypatch, item):
    _responses(monkeypatch, [item], candidates=[_candidate()])

    result = catalog_clients.exist_price_lookup(part_number="DEMO-A", brand="BRAND-A")

    _assert_reference(result)
    assert result["needs_disambiguation"] is False
    selected = result["selected_item"]
    assert selected["catalog_candidate"]["pid"] == "PID-A"
    assert selected["pid"] == item["ProductIdEnc"]
    assert selected["part_number"] == item["PartNumber"]
    assert selected["brand"] == item["CatalogName"]


def test_selected_catalog_analog_owns_its_article_not_the_initial_query(monkeypatch):
    calls = _responses(monkeypatch, [_item()], candidates=[_candidate()])

    result = catalog_clients.exist_price_lookup(part_number="SOURCE-QUERY", brand="BRAND-A")

    _assert_reference(result)
    assert result["needs_disambiguation"] is False
    assert result["selected_item"]["part_number"] == "DEMO-A"
    assert result["selected_item"]["catalog_candidate"]["part_number"] == "DEMO-A"
    assert calls[1]["query"] == {"pcode": ["SOURCE-QUERY"]}


def test_catalog_binding_preserves_article_formatting_and_brand_key_controls(monkeypatch):
    _responses(monkeypatch, [_item(article="de mo-a", brand="Brand A")], candidates=[_candidate()])

    result = catalog_clients.exist_price_lookup(part_number="DEMO-A", brand="BRAND-A")

    _assert_reference(result)
    assert result["needs_disambiguation"] is False
    assert result["selected_item"]["part_number"] == "de mo-a"
    assert result["selected_item"]["brand"] == "Brand A"


def test_known_foreign_unicode_brand_is_not_erased_by_identity_normalization(monkeypatch):
    _responses(monkeypatch, [_item(brand="商标A")], candidates=[_candidate(brand="品牌A")])

    result = catalog_clients.exist_price_lookup(part_number="DEMO-A", brand="品牌A")

    _assert_reference(result)
    assert result["selected_item"] is None
    assert result["needs_disambiguation"] is True
    assert result["items"][0]["brand"] == "商标A"
    assert "catalog_candidate" not in result["items"][0]


def test_partial_catalog_rows_can_bind_to_the_same_selected_identity_without_field_merging(monkeypatch):
    _responses(
        monkeypatch,
        [_item(article=None, brand=None), _item(pid=None)],
        candidates=[_candidate()],
    )

    result = catalog_clients.exist_price_lookup(part_number="DEMO-A", brand="BRAND-A")

    _assert_reference(result)
    assert result["needs_disambiguation"] is False
    assert result["selected_item"]["pid"] == "PID-A"
    assert result["selected_item"]["brand"] is None
    assert result["selected_item"]["part_number"] is None
    assert result["items"][1]["pid"] is None


def test_direct_pid_route_has_priority_over_initial_article_and_brand_context(monkeypatch):
    calls = _responses(monkeypatch, [_item(pid="PID-B"), _item()])

    result = catalog_clients.exist_price_lookup(part_number="SOURCE-QUERY", brand="SOURCE-BRAND", pid="PID-A")

    _assert_reference(result)
    assert result["needs_disambiguation"] is False
    assert result["selected_item"]["pid"] == "PID-A"
    assert result["selected_item"]["part_number"] == "DEMO-A"
    assert "catalog_candidate" not in result["selected_item"]
    assert len(calls) == 1
    assert calls[0]["query"] == {"pid": ["PID-A"]}


@pytest.mark.parametrize("item", [_item(pid="PID-B"), _item(pid=None)], ids=["foreign-pid", "missing-pid"])
def test_direct_pid_route_does_not_resolve_a_different_or_unknown_pid(monkeypatch, item):
    _responses(monkeypatch, [item])

    result = catalog_clients.exist_price_lookup(part_number="DEMO-A", pid="PID-A")

    _assert_reference(result)
    assert result["selected_item"] is None
    assert result["needs_disambiguation"] is True
    assert "catalog_candidate" not in result["items"][0]


@pytest.mark.parametrize("brand", ["BRAND-A", None])
def test_pcode_fallback_selects_matching_article_and_optional_brand(monkeypatch, brand):
    calls = _responses(monkeypatch, [_item(pid="PID-B", article="DEMO-B"), _item()])

    result = catalog_clients.exist_price_lookup(part_number="DEMO-A", brand=brand)

    _assert_reference(result)
    assert result["selected_item"]["pid"] == "PID-A"
    assert result["needs_disambiguation"] is False
    assert result["candidates"] == []
    assert "catalog_candidate" not in result["selected_item"]
    assert len(result["items"]) == 2
    assert len(calls) == 2


@pytest.mark.parametrize(
    "items",
    [
        [_item(), _item(pid="PID-B", brand="BRAND-B")],
        [_item(), _item(pid="PID-B")],
        [_item(pid=None), _item(pid=None, brand=None)],
        [_item(pid=None, brand=None), _item(pid=None)],
        [_item(brand=None), _item(pid=None)],
        [_item(), _item(pid=None, brand=None)],
        [_item(pid=None, brand=None), _item()],
        [{}],
        [_item(article="DEMO-B")],
    ],
    ids=[
        "different-brands",
        "different-pids",
        "unknown-brand-not-wildcard",
        "unknown-brand-first-not-wildcard",
        "separate-unlinked-anchors",
        "complete-row-does-not-bind-anchorless-row",
        "anchorless-row-not-bound-by-later-complete-row",
        "unknown",
        "foreign-article",
    ],
)
def test_pcode_fallback_keeps_distinct_or_unproven_identities_unresolved(monkeypatch, items):
    _responses(monkeypatch, items)

    result = catalog_clients.exist_price_lookup(part_number="DEMO-A")

    _assert_reference(result)
    assert result["selected_item"] is None
    assert result["needs_disambiguation"] is True
    assert len(result["items"]) == len(items)
    assert all("catalog_candidate" not in item for item in result["items"])


@pytest.mark.parametrize("route", ["catalog", "pid", "pcode"])
def test_duplicate_rows_for_one_known_identity_are_not_new_ambiguity(monkeypatch, route):
    target = _item()
    _responses(monkeypatch, [target, deepcopy(target)], candidates=[_candidate()] if route == "catalog" else None)

    result = catalog_clients.exist_price_lookup(
        part_number="DEMO-A", brand="BRAND-A", pid="PID-A" if route == "pid" else None
    )

    _assert_reference(result)
    assert result["needs_disambiguation"] is False
    assert result["selected_item"] is result["items"][0]
    assert len(result["items"]) == 2
    assert result["selected_item"]["offers"] == []
    assert result["items"][1]["offers"] == []


def test_pcode_partial_duplicates_share_pid_without_inventing_missing_brand(monkeypatch):
    _responses(monkeypatch, [_item(brand=None), _item()])

    result = catalog_clients.exist_price_lookup(part_number="DEMO-A")

    _assert_reference(result)
    assert result["needs_disambiguation"] is False
    assert result["selected_item"]["pid"] == "PID-A"
    assert result["selected_item"]["brand"] is None
    assert result["items"][1]["brand"] == "BRAND-A"


def test_pcode_partial_duplicates_share_article_and_known_brand_without_inventing_pid(monkeypatch):
    _responses(monkeypatch, [_item(pid=None), _item()])

    result = catalog_clients.exist_price_lookup(part_number="DEMO-A")

    _assert_reference(result)
    assert result["needs_disambiguation"] is False
    assert result["selected_item"]["pid"] is None
    assert result["items"][1]["pid"] == "PID-A"


@pytest.mark.parametrize("link_first", [True, False])
def test_pcode_partial_duplicate_anchors_can_be_linked_by_a_complete_identity_row(monkeypatch, link_first):
    partial = [_item(brand=None), _item(pid=None)]
    items = [_item(), *partial] if link_first else [*partial, _item()]
    _responses(monkeypatch, items)

    result = catalog_clients.exist_price_lookup(part_number="DEMO-A")

    _assert_reference(result)
    assert result["needs_disambiguation"] is False
    assert result["selected_item"] is result["items"][0]
    assert len(result["items"]) == 3
    assert [item["pid"] for item in result["items"]] == [item["ProductIdEnc"] for item in items]
    assert [item["brand"] for item in result["items"]] == [item["CatalogName"] for item in items]


@pytest.mark.parametrize("route", ["catalog", "pid", "pcode"])
def test_empty_items_preserve_parse_success_without_false_multiple_choices(monkeypatch, route):
    _responses(monkeypatch, [], candidates=[_candidate()] if route == "catalog" else None)

    result = catalog_clients.exist_price_lookup(
        part_number="DEMO-A", brand="BRAND-A", pid="PID-A" if route == "pid" else None
    )

    _assert_reference(result)
    assert result["selected_item"] is None
    assert result["needs_disambiguation"] is False
    assert result["items"] == []


def test_multiple_catalog_candidates_without_brand_still_stop_before_price_read(monkeypatch):
    calls = _responses(monkeypatch, [_item()], candidates=[_candidate(), _candidate(pid="PID-B", brand="BRAND-B")])

    result = catalog_clients.exist_price_lookup(part_number="DEMO-A")

    _assert_reference(result)
    assert result["selected_item"] is None
    assert result["needs_disambiguation"] is True
    assert len(result["candidates"]) == 2
    assert len(calls) == 2


def test_more_offers_use_selected_row_and_preserve_distinct_form_context_pid(monkeypatch):
    calls = _responses(
        monkeypatch,
        [_item(pid="PID-B", article="DEMO-B", brand="BRAND-B"), _item()],
        candidates=[_candidate()],
        hidden_pid="FORM-CONTEXT-B",
    )

    result = catalog_clients.exist_price_lookup(part_number="DEMO-A", brand="BRAND-A", include_more_offers=True)

    _assert_reference(result)
    assert result["selected_item"]["pid"] == "PID-A"
    assert result["selected_item"]["more_offers_loaded"] is True
    assert result["selected_item"]["more_offers_count"] == 0
    assert "more_offers_loaded" not in result["items"][0]
    post = [call for call in calls if call["method"] == "POST"]
    assert len(post) == 1
    assert post[0]["body"] == {
        "ProductID": "FORM-CONTEXT-B",
        "OriginalProductID": "PID-A",
        "textValue": "SYNTHETIC-HASH",
        "srcId": "RawPartNumber",
    }


def test_unresolved_items_do_not_request_more_offers(monkeypatch):
    calls = _responses(monkeypatch, [_item(pid="PID-B")], candidates=[_candidate()])

    result = catalog_clients.exist_price_lookup(part_number="DEMO-A", brand="BRAND-A", include_more_offers=True)

    _assert_reference(result)
    assert result["selected_item"] is None
    assert result["needs_disambiguation"] is True
    assert all(call["method"] == "GET" for call in calls)


def test_pcode_fallback_does_not_introduce_an_additional_more_offers_request(monkeypatch):
    calls = _responses(monkeypatch, [_item()])

    result = catalog_clients.exist_price_lookup(part_number="DEMO-A", include_more_offers=True)

    _assert_reference(result)
    assert result["selected_item"]["pid"] == "PID-A"
    assert "more_offers_loaded" not in result["selected_item"]
    assert len(calls) == 2
    assert all(call["method"] == "GET" for call in calls)


def test_missing_more_offers_context_does_not_erase_selected_identity(monkeypatch):
    calls = _responses(monkeypatch, [_item()], candidates=[_candidate()], hidden_hash="")

    result = catalog_clients.exist_price_lookup(part_number="DEMO-A", brand="BRAND-A", include_more_offers=True)

    _assert_reference(result)
    assert result["needs_disambiguation"] is False
    assert result["selected_item"]["pid"] == "PID-A"
    assert result["selected_item"]["more_offers_loaded"] is False
    assert result["selected_item"]["more_offers_error"] == "Exist more-offers hidden fields are missing."
    assert all(call["method"] == "GET" for call in calls)
