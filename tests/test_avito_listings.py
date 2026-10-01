from __future__ import annotations

import json
from urllib.error import HTTPError

import pytest

from autostop_manager import avito_listings


class _Response:
    def __init__(self, payload: object):
        if isinstance(payload, bytes):
            self.body = payload
        else:
            self.body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False

    def read(self, limit: int = -1):
        return self.body if limit < 0 else self.body[:limit]


def _listing(**overrides):
    listing = {
        "ad_id": "8386499447",
        "url": "https://www.avito.ru/krasnoyarsk/zapchasti_i_aksessuary/bamper_8386499447",
        "title": "Бампер Toyota Camry",
        "description": "Б/у, хорошее состояние. Телефон +7 999 123-45-67; email parts@example.org",
        "price": 35_900,
        "price_text": "35 900 ₽",
        "price_is_from": False,
        "price_not_published": False,
        "is_free": False,
        "location": {"name": "Красноярск", "address": "улица Ленина, 1"},
        "parameters": [{"name": "Состояние", "value": "Б/у"}],
        "seller": {
            "id": "seller-private-id",
            "name": "Иван Иванов",
            "type": "Частное лицо",
            "rating": 4.9,
            "reviews_count": 13,
            "reviews": "13 отзывов",
            "phone": "+7 999 123-45-67",
            "summary": "Иван, звоните по телефону +7 999 123-45-67",
        },
        "delivery": {"available": True, "label": "Авито Доставка", "phone": "+7 999 123-45-67"},
        "published_at": "2026-09-16T19:51:58Z",
        "coordinates": {"lat": 56.0106, "lng": 92.8526},
    }
    listing.update(overrides)
    return listing


def _install_response(monkeypatch, payload, *, base_url="http://127.0.0.1:8765"):
    monkeypatch.setenv("REEFAPI_API_KEY", "reef-secret-test-key")
    monkeypatch.setattr(avito_listings, "_API_BASE_URL", base_url)
    calls = []

    def fake_open_request(request, timeout):
        calls.append((request, timeout))
        return _Response(payload)

    monkeypatch.setattr(avito_listings, "_open_request", fake_open_request)
    return calls


def test_search_sends_bounded_request_and_returns_normalized_non_pii_listing(monkeypatch):
    calls = _install_response(monkeypatch, {"ok": True, "data": {"listings": [_listing()]}})

    result = avito_listings.avito_search_listings(
        "Toyota 52159-0E901",
        location="krasnoyarsk",
        category="zapchasti_i_aksessuary",
        price_min=100,
        price_max=50_000,
        delivery_only=True,
    )

    assert result["ok"] is True
    assert result["verification"] == "provider_response_received"
    assert result["count"] == 1
    assert result["listings"][0] == {
        "source": "avito",
        "listing_id": "8386499447",
        "url": "https://www.avito.ru/krasnoyarsk/zapchasti_i_aksessuary/bamper_8386499447",
        "title": "Бампер Toyota Camry",
        "description": "Б/у, хорошее состояние. Телефон [redacted]; email [redacted]",
        "price_rub": 35_900,
        "price_text": "35 900 ₽",
        "price_qualifier": "fixed",
        "city": "Красноярск",
        "condition": "Б/у",
        "seller": {
            "name": "Иван Иванов",
            "type": "Частное лицо",
            "rating": 4.9,
            "reviews_count": 13,
            "reviews": "13 отзывов",
        },
        "delivery": {"available": True, "label": "Авито Доставка"},
        "availability": None,
        "published_at": "2026-09-16T19:51:58Z",
        "observed_at": result["listings"][0]["observed_at"],
        "status": "lead",
        "fitment_confirmed": False,
        "availability_confirmed": False,
    }
    assert result["listings"][0]["seller"]["name"] == "Иван Иванов"
    assert "seller-private-id" not in repr(result)
    assert "+7 999 123-45-67" not in repr(result)
    assert "reef-secret-test-key" not in repr(result)

    request, timeout = calls[0]
    assert request.full_url == "http://127.0.0.1:8765/avito/v1/search"
    assert request.get_method() == "POST"
    assert request.get_header("X-api-key") == "reef-secret-test-key"
    assert timeout == 30.0
    payload = json.loads(request.data)
    assert payload == {
        "query": "Toyota 52159-0E901",
        "location": "krasnoyarsk",
        "category": "zapchasti_i_aksessuary",
        "page": 1,
        "delivery_only": True,
        "price_min": 100,
        "price_max": 50_000,
    }
    assert "limit" not in payload


def test_search_limits_normalized_rows_without_hidden_page_loop(monkeypatch):
    calls = _install_response(
        monkeypatch,
        {"ok": True, "data": {"listings": [_listing(), _listing(ad_id="8386499450")]}},
    )

    result = avito_listings.avito_search_listings("brake pad", limit=1)

    assert result["ok"] is True
    assert result["count"] == 1
    assert len(result["listings"]) == 1
    assert len(calls) == 1


def test_search_ignores_overflowing_seller_rating_without_internal_error(monkeypatch):
    listing = _listing()
    listing["seller"]["rating"] = 10**400
    calls = _install_response(monkeypatch, {"ok": True, "data": {"listings": [listing]}})

    result = avito_listings.avito_search_listings("synthetic part")

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["listings"][0]["seller"]["rating"] is None
    assert len(calls) == 1


@pytest.mark.parametrize(
    "body",
    [b'{"ok":true,"data":{"price":' + b"9" * 5_000 + b"}}", b"[" * 20_000 + b"0" + b"]" * 20_000],
    ids=["integer-limit", "nesting-limit"],
)
def test_search_returns_malformed_response_for_excessive_json_numbers_or_nesting(monkeypatch, body):
    calls = _install_response(monkeypatch, body)

    result = avito_listings.avito_search_listings("synthetic part")

    assert result == {"ok": False, "source": "avito", "error": "malformed_response"}
    assert len(calls) == 1


def test_search_deduplicates_by_listing_id_or_source_url(monkeypatch):
    first = _listing()
    duplicate_id = _listing(
        url="https://www.avito.ru/krasnoyarsk/other/bamper_8386499447",
        title="Duplicate ID",
    )
    duplicate_url = _listing(ad_id="8386499450", title="Duplicate URL")
    _install_response(
        monkeypatch,
        {"ok": True, "data": {"listings": [first, duplicate_id, duplicate_url]}},
    )

    result = avito_listings.avito_search_listings("bumper")

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["listings"][0]["listing_id"] == "8386499447"


@pytest.mark.parametrize(
    "override",
    [
        {"url": None},
        {"url": "https://example.com/krasnoyarsk/part_8386499447"},
    ],
)
def test_search_skips_rows_without_original_avito_source_url(monkeypatch, override):
    _install_response(monkeypatch, {"ok": True, "data": {"listings": [_listing(**override)]}})

    result = avito_listings.avito_search_listings("part")

    assert result == {"ok": False, "source": "avito", "error": "malformed_response"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": "1HGCM82633A004352"},
        {"query": "engine part person@example.org"},
        {"query": "+7 999 123-45-67"},
        {"query": "Toyota alternator", "location": "password=secret-value"},
    ],
)
def test_search_rejects_sensitive_input_before_network(monkeypatch, kwargs):
    monkeypatch.delenv("REEFAPI_API_KEY", raising=False)
    monkeypatch.setattr(avito_listings, "_open_request", lambda *_args, **_kwargs: pytest.fail("network call"))

    result = avito_listings.avito_search_listings(**kwargs)

    assert result["ok"] is False
    assert "sensitive" in result["error"] or result["error"] == "invalid_location"


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        ("page", 0, "invalid_page"),
        ("page", 31, "invalid_page"),
        ("limit", 0, "invalid_limit"),
        ("limit", 51, "invalid_limit"),
        ("limit", True, "invalid_limit"),
        ("price_min", -1, "invalid_price_min"),
        ("price_max", 1.5, "invalid_price_max"),
        ("delivery_only", 1, "invalid_delivery_filter"),
    ],
)
def test_search_rejects_out_of_bounds_arguments(monkeypatch, field, value, expected_error):
    monkeypatch.setattr(avito_listings, "_open_request", lambda *_args, **_kwargs: pytest.fail("network call"))

    result = avito_listings.avito_search_listings("brake pad", **{field: value})

    assert result["ok"] is False
    assert result["error"] == expected_error


def test_search_dry_run_marks_configuration_only_and_does_not_need_key(monkeypatch):
    monkeypatch.delenv("REEFAPI_API_KEY", raising=False)
    monkeypatch.setattr(avito_listings, "_open_request", lambda *_args, **_kwargs: pytest.fail("network call"))

    result = avito_listings.avito_search_listings("mirror", dry_run=True)

    assert result == {
        "ok": True,
        "source": "avito",
        "dry_run": True,
        "verification": "configuration_only",
        "request": {
            "query": "mirror",
            "location": "krasnoyarsk",
            "category": "zapchasti_i_aksessuary",
            "page": 1,
            "delivery_only": False,
            "limit": 50,
        },
        "count": 0,
        "listings": [],
    }


@pytest.mark.parametrize(
    ("override", "expected_price", "expected_text", "expected_qualifier"),
    [
        ({"is_free": True, "price": 0, "price_text": None}, 0, "Бесплатно", "free"),
        ({"price_not_published": True, "price": 100, "price_text": None}, None, "Цена не указана", "unpublished"),
        ({"price_is_from": True, "price": 500, "price_text": None}, 500, "от 500 ₽", "from"),
        ({"price_min": 100, "price_max": 250, "price": 100, "price_text": None}, None, "100–250 ₽", "range"),
        ({"price": 500, "price_text": "от 500 ₽"}, 500, "от 500 ₽", "from"),
        ({"price": 0, "price_text": "Бесплатно"}, 0, "Бесплатно", "free"),
        ({"price": 100, "price_text": "Цена не указана"}, None, "Цена не указана", "unpublished"),
    ],
)
def test_search_preserves_price_qualifier(monkeypatch, override, expected_price, expected_text, expected_qualifier):
    _install_response(monkeypatch, {"ok": True, "data": {"listings": [_listing(**override)]}})

    result = avito_listings.avito_search_listings("part")

    listing = result["listings"][0]
    assert listing["price_rub"] == expected_price
    assert listing["price_text"] == expected_text
    assert listing["price_qualifier"] == expected_qualifier


def test_search_empty_results_are_a_valid_live_response(monkeypatch):
    _install_response(monkeypatch, {"ok": True, "data": {"listings": []}})

    result = avito_listings.avito_search_listings("nonexistent part")

    assert result == {
        "ok": True,
        "source": "avito",
        "verification": "provider_response_received",
        "count": 0,
        "listings": [],
    }


def test_search_uses_avito_delivery_when_delivery_is_missing(monkeypatch):
    _install_response(
        monkeypatch,
        {
            "ok": True,
            "data": {
                "listings": [
                    _listing(
                        delivery=None,
                        avito_delivery={"available": True, "label": "Авито Доставка"},
                    )
                ]
            },
        },
    )

    result = avito_listings.avito_search_listings("part")

    assert result["listings"][0]["delivery"] == {
        "available": True,
        "label": "Авито Доставка",
    }


def test_search_prefers_explicit_false_delivery_flag(monkeypatch):
    _install_response(
        monkeypatch,
        {
            "ok": True,
            "data": {
                "listings": [
                    _listing(
                        delivery=False,
                        avito_delivery={"available": True, "label": "Авито Доставка"},
                    )
                ]
            },
        },
    )

    result = avito_listings.avito_search_listings("part")

    assert result["listings"][0]["delivery"] == {"available": False, "label": None}


def test_search_malformed_response_is_sanitized(monkeypatch):
    _install_response(monkeypatch, {"ok": True, "data": {"items": [_listing()]}})

    result = avito_listings.avito_search_listings("part")

    assert result == {"ok": False, "source": "avito", "error": "malformed_response"}


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, "authentication_failed"),
        (403, "provider_forbidden"),
        (429, "rate_limited"),
        (503, "provider_unavailable"),
    ],
)
def test_search_maps_http_errors_without_echoing_response_or_key(monkeypatch, status, error):
    monkeypatch.setenv("REEFAPI_API_KEY", "reef-secret-test-key")
    monkeypatch.setattr(avito_listings, "_API_BASE_URL", "http://127.0.0.1:8765")

    def fail_request(_request, timeout):
        raise HTTPError("https://api.reefapi.com/avito/v1/search", status, "token=reef-secret-test-key", {}, None)

    monkeypatch.setattr(avito_listings, "_open_request", fail_request)
    result = avito_listings.avito_search_listings("part")

    assert result == {"ok": False, "source": "avito", "error": error}
    assert "reef-secret-test-key" not in repr(result)


def test_search_timeout_is_bounded_and_does_not_retry_a_chargeable_request(monkeypatch):
    monkeypatch.setenv("REEFAPI_API_KEY", "reef-secret-test-key")
    calls = []

    def time_out(_request, timeout):
        calls.append(timeout)
        raise TimeoutError("key=reef-secret-test-key")

    monkeypatch.setattr(avito_listings, "_open_request", time_out)

    result = avito_listings.avito_search_listings("part")

    assert result == {"ok": False, "source": "avito", "error": "provider_unavailable"}
    assert calls == [30.0]
    assert "reef-secret-test-key" not in repr(result)


def test_search_provider_error_maps_rate_limit_and_hides_provider_message(monkeypatch):
    _install_response(
        monkeypatch,
        {
            "ok": False,
            "data": None,
            "error": {"code": "RATE_LIMITED", "message": "key=reef-secret-test-key"},
        },
    )

    result = avito_listings.avito_search_listings("part")

    assert result == {"ok": False, "source": "avito", "error": "rate_limited"}
    assert "reef-secret-test-key" not in repr(result)


def test_search_requires_key_for_live_request(monkeypatch):
    monkeypatch.delenv("REEFAPI_API_KEY", raising=False)
    monkeypatch.setattr(avito_listings, "_open_request", lambda *_args, **_kwargs: pytest.fail("network call"))

    result = avito_listings.avito_search_listings("part")

    assert result == {"ok": False, "source": "avito", "error": "api_key_missing"}


def test_injected_base_url_accepts_only_reefapi_or_loopback(monkeypatch):
    monkeypatch.setenv("REEFAPI_API_KEY", "reef-secret-test-key")
    monkeypatch.setattr(avito_listings, "_open_request", lambda *_args, **_kwargs: pytest.fail("network call"))

    bad = avito_listings._request_json("search", {"query": "part"}, base_url="https://api.reefapi.com.evil.test")
    good = avito_listings._safe_base_url("http://127.0.0.1:8765")

    assert bad == {"ok": False, "error": "invalid_api_base_url"}
    assert good == "http://127.0.0.1:8765"


def test_request_uses_no_redirect_handler_and_opens_only_configured_origin(monkeypatch):
    monkeypatch.setenv("REEFAPI_API_KEY", "reef-secret-test-key")
    monkeypatch.setattr(avito_listings, "_API_BASE_URL", "http://127.0.0.1:8765")
    captured_handlers = []
    opened = []

    class _Opener:
        def open(self, request, timeout):
            opened.append((request.full_url, timeout))
            return _Response({"ok": True, "data": {"listings": []}})

    def fake_build_opener(*handlers):
        captured_handlers.extend(handlers)
        return _Opener()

    monkeypatch.setattr(avito_listings, "build_opener", fake_build_opener)

    result = avito_listings.avito_search_listings("part")

    assert result["ok"] is True
    assert len(captured_handlers) == 1
    assert isinstance(captured_handlers[0], avito_listings._NoRedirectHandler)
    assert opened == [("http://127.0.0.1:8765/avito/v1/search", avito_listings._REQUEST_TIMEOUT_SECONDS)]


def test_no_redirect_handler_refuses_redirect_to_another_host():
    from urllib.request import Request

    handler = avito_listings._NoRedirectHandler()
    request = Request("https://api.reefapi.com/avito/v1/search")

    redirected = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {"Location": "https://attacker.example/collect"},
        "https://attacker.example/collect",
    )

    assert redirected is None


def test_read_listing_accepts_numeric_id_and_returns_normalized_card(monkeypatch):
    calls = _install_response(monkeypatch, {"ok": True, "data": _listing()})

    result = avito_listings.avito_read_listing("8386499447")

    assert result["ok"] is True
    assert result["verification"] == "provider_response_received"
    assert result["listing"]["listing_id"] == "8386499447"
    assert result["listing"]["availability_confirmed"] is False
    request, _timeout = calls[0]
    assert request.full_url.endswith("/avito/v1/listing")
    assert json.loads(request.data) == {"ad_id": "8386499447"}
    assert result["listing"]["seller"]["name"] == "Иван Иванов"


def test_read_listing_rejects_a_different_returned_listing(monkeypatch):
    _install_response(
        monkeypatch,
        {
            "ok": True,
            "data": _listing(
                ad_id="8386499450",
                url="https://www.avito.ru/krasnoyarsk/part_8386499450",
            ),
        },
    )

    result = avito_listings.avito_read_listing("8386499447")

    assert result == {"ok": False, "source": "avito", "error": "malformed_response"}


@pytest.mark.parametrize("operation", ["read", "search"])
def test_listing_url_must_reference_the_same_returned_id(monkeypatch, operation):
    row = _listing(url="https://www.avito.ru/krasnoyarsk/part_8386499450")
    data = row if operation == "read" else {"listings": [row]}
    _install_response(monkeypatch, {"ok": True, "data": data})

    result = (
        avito_listings.avito_read_listing("8386499447")
        if operation == "read"
        else avito_listings.avito_search_listings("synthetic part")
    )

    assert result == {"ok": False, "source": "avito", "error": "malformed_response"}


def test_read_listing_preserves_known_target_fallback_for_missing_fields(monkeypatch):
    _install_response(monkeypatch, {"ok": True, "data": _listing(ad_id=None, url=None)})

    result = avito_listings.avito_read_listing("https://www.avito.ru/item/8386499447")

    assert result["ok"] is True
    assert result["listing"]["listing_id"] == "8386499447"
    assert result["listing"]["url"] == "https://www.avito.ru/item/8386499447"


def test_read_listing_accepts_avito_url_but_sends_only_id(monkeypatch):
    calls = _install_response(monkeypatch, {"ok": True, "data": _listing()})

    result = avito_listings.avito_read_listing(
        "https://www.avito.ru/krasnoyarsk/zapchasti_i_aksessuary/bamper_8386499447?utm_source=internal"
    )

    assert result["ok"] is True
    assert result["listing"]["url"] == "https://www.avito.ru/krasnoyarsk/zapchasti_i_aksessuary/bamper_8386499447"
    assert json.loads(calls[0][0].data) == {"ad_id": "8386499447"}


def test_read_listing_accepts_avito_url_with_id_in_final_path_segment(monkeypatch):
    calls = _install_response(monkeypatch, {"ok": True, "data": _listing()})

    result = avito_listings.avito_read_listing("https://www.avito.ru/item/8386499447")

    assert result["ok"] is True
    assert json.loads(calls[0][0].data) == {"ad_id": "8386499447"}


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/krasnoyarsk/part_8386499447",
        "https://avito.ru.evil.test/krasnoyarsk/part_8386499447",
        "https://user:pass@www.avito.ru/krasnoyarsk/part_8386499447",
        "https://www.avito.ru/krasnoyarsk/part_1HGCM82633A004352",
    ],
)
def test_read_listing_rejects_non_avito_or_sensitive_urls_before_network(monkeypatch, value):
    monkeypatch.setattr(avito_listings, "_open_request", lambda *_args, **_kwargs: pytest.fail("network call"))

    result = avito_listings.avito_read_listing(value)

    assert result["ok"] is False


def test_read_listing_dry_run_is_configuration_only(monkeypatch):
    monkeypatch.setattr(avito_listings, "_open_request", lambda *_args, **_kwargs: pytest.fail("network call"))

    result = avito_listings.avito_read_listing("8386499447", dry_run=True)

    assert result == {
        "ok": True,
        "source": "avito",
        "dry_run": True,
        "verification": "configuration_only",
        "request": {"listing_id": "8386499447"},
    }


def test_read_listing_maps_non_json_and_missing_title(monkeypatch):
    _install_response(monkeypatch, b"not-json")
    malformed = avito_listings.avito_read_listing("8386499447")
    assert malformed["error"] == "malformed_response"

    _install_response(monkeypatch, {"ok": True, "data": {"ad_id": "8386499447"}})
    missing_title = avito_listings.avito_read_listing("8386499447")
    assert missing_title["error"] == "malformed_response"


def test_read_listing_requires_provider_source_url_when_input_was_only_numeric_id(monkeypatch):
    _install_response(
        monkeypatch,
        {"ok": True, "data": {"ad_id": "8386499447", "title": "Bumper"}},
    )

    result = avito_listings.avito_read_listing("8386499447")

    assert result == {"ok": False, "source": "avito", "error": "malformed_response"}


def test_api_key_with_header_control_characters_is_rejected(monkeypatch):
    monkeypatch.setenv("REEFAPI_API_KEY", "reef-key\r\nX-Evil: yes")
    monkeypatch.setattr(avito_listings, "_open_request", lambda *_args, **_kwargs: pytest.fail("network call"))

    result = avito_listings.avito_search_listings("part")

    assert result == {"ok": False, "source": "avito", "error": "api_key_invalid"}
