from __future__ import annotations

import json

from autostop_manager import avito_listings
from autostop_manager.avito_market_assessment import assess_avito_price_sample


class _Response:
    status = 200

    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, limit):
        return self.body[:limit]


def _search_row(listing_id, price):
    return {
        "ad_id": listing_id,
        "url": f"https://www.avito.ru/krasnoyarsk/zapchasti_i_aksessuary/bamper_toyota_camry_{listing_id}",
        "title": "Бампер Toyota Camry",
        "description_snippet": "Артикул 1712024, новый бампер",
        "price": price,
        "location": {"name": "Красноярск"},
        "condition": "Новое",
        "delivery_available": False,
    }


def test_provider_search_detail_and_price_sample_preserve_useful_rows(monkeypatch):
    """The actual search/read/assessment chain retains the reviewed provider fields."""
    monkeypatch.setenv("REEFAPI_API_KEY", "synthetic-regression-key")
    rows = [_search_row(str(8386499447 + index), price) for index, price in enumerate((5_000, 6_000, 7_000))]
    detail = {
        **rows[1],
        "condition": None,
        "description": "Артикул 1712024, новый бампер, фиксированная цена",
        "price": 7_500,
        "params": [{"name": "Состояние", "value": "Новое"}],
        "delivery_available": True,
        "delivery_text": "Доставка доступна",
        "published_or_raised_text": "Сегодня",
    }
    calls = []

    def respond(request, *, timeout):
        calls.append((request.full_url, json.loads(request.data)))
        if request.full_url.endswith("/search"):
            return _Response({"ok": True, "data": {"listings": [{"ad_id": "invalid"}, rows[0], rows[0], *rows[1:]]}})
        assert request.full_url.endswith("/listing")
        assert calls[-1][1] == {"ad_id": rows[1]["ad_id"]}
        return _Response({"ok": True, "data": detail})

    monkeypatch.setattr(avito_listings, "_open_request", respond)

    search = avito_listings.avito_search_listings("1712024 бампер", limit=3)
    assert search["ok"] is True
    assert search["count"] == 3
    assert all(row["description"] == "Артикул 1712024, новый бампер" for row in search["listings"])
    assert all(row["delivery"]["available"] is False for row in search["listings"])

    read = avito_listings.avito_read_listing(rows[1]["url"])
    assert read["ok"] is True
    assert read["listing"]["condition"] == "Новое"
    assert read["listing"]["delivery"] == {"available": True, "label": "Доставка доступна"}
    assert read["listing"]["published_at"] is None
    assert read["listing"]["published_or_raised_text"] == "Сегодня"

    sample = assess_avito_price_sample(part_number="1712024", listings=[*search["listings"], read["listing"]])
    assert sample["ok"] is True
    assert sample["accepted_count"] == 3
    assert sample["segments"][0]["sample_median_price_rub"] == 7_000
    assert sample["excluded_listings"] == [{"listing_index": 1, "code": "duplicate_listing_id"}]
    assert sample["fitment_confirmed"] is False
    assert sample["availability_confirmed"] is False
    assert len(calls) == 2
