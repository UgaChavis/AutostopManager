from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from urllib.error import HTTPError, URLError

import pytest

from autostop_manager import store_analytics
from autostop_manager.store_analytics import get_store_analytics_report


def _payload() -> dict:
    return {
        "ok": True,
        "schema": "store_analytics_report_v1",
        "timezone": "Asia/Krasnoyarsk",
        "periodPreset": "today",
        "period": {"start": "2026-07-16T00:00:00+07:00", "end": "2026-07-16T23:00:00+07:00"},
        "previousPeriod": {"start": "2026-07-15T01:00:00+07:00", "end": "2026-07-16T00:00:00+07:00"},
        "summary": {
            "visitors": 12,
            "sessions": 14,
            "pageViews": 40,
            "engagedSessions": 10,
            "averageEngagedSeconds": 75.5,
            "medianEngagedSeconds": 60.0,
            "searches": 8,
            "zeroResultSearches": 2,
            "zeroResultRate": 25.0,
            "cartAdditions": 4,
            "quoteSubmissions": 1,
            "orders": 2,
            "meaningfulClicks": 9,
        },
        "topPages": [{"path": "/search", "views": 20}],
        "topProducts": [{"productId": "part-1", "name": "Фильтр", "sku": "OF-1", "views": 7, "cartAdditions": 2}],
        "clicks": [{"event": "cart_open", "label": "Открытие корзины", "count": 5}],
        "funnel": {
            "productViewSessions": 10,
            "cartSessions": 4,
            "quoteOrOrderSessions": 3,
            "orderSessions": 2,
            "viewToCartRate": 40.0,
            "cartToQuoteOrOrderRate": 75.0,
            "cartToOrderRate": 50.0,
            "viewToOrderRate": 20.0,
        },
        "previous": {"summary": {"visitors": 8}},
        "comparison": {
            "visitors": {"current": 12, "previous": 8, "delta": 4, "percentChange": 50.0},
            "viewToOrderRate": {"current": 20.0, "previous": 10.0, "deltaPercentagePoints": 10.0},
        },
        "meta": {
            "aggregatedOnly": True,
            "rawEventsIncluded": False,
            "retentionDays": 60,
            "engagedTimeUnit": "seconds_per_session",
        },
    }


class _Response:
    def __init__(self, payload: dict | bytes):
        self.raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        return self.raw[:limit]


def test_report_posts_bounded_custom_period_and_returns_only_aggregates(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured.update(
            {
                "url": request.full_url,
                "authorization": request.headers["Authorization"],
                "body": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        return _Response(_payload())

    monkeypatch.setattr(store_analytics, "urlopen", fake_urlopen)
    result = get_store_analytics_report(
        api_url="http://autostop-app:8000/internal/agent/v1",
        read_token="runtime-secret",
        query="какая конверсия в корзину и заказ",
        period="custom",
        date_from="2026-07-01",
        date_to="2026-07-07",
        top_limit=50,
    )

    assert result["ok"] is True
    assert result["format"] == "store_analytics_report_v1"
    assert result["summary"]["visitors"] == 12
    assert result["topProducts"][0]["name"] == "Фильтр"
    assert "40.00%" in result["answer"]
    assert "20.00%" in result["answer"]
    assert result["meta"]["aggregatedOnly"] is True
    assert result["meta"]["rawEventsIncluded"] is False
    assert "previous" not in result
    assert "runtime-secret" not in str(result)
    assert "visitorId" not in str(result)
    assert "sessionId" not in str(result)
    assert captured["url"].endswith("/internal/agent/v1/analytics/report")
    assert captured["authorization"] == "Bearer runtime-secret"
    assert captured["body"] == {
        "period": "custom",
        "comparePrevious": True,
        "topLimit": 20,
        "startAt": "2026-07-01T00:00:00+07:00",
        "endAt": "2026-07-08T00:00:00+07:00",
    }


@pytest.mark.parametrize(
    ("query", "expected_period", "answer_fragment"),
    [
        ("сколько посетителей сегодня", "today", "посетителей 12"),
        ("какие товары смотрели за неделю", "last_7_days", "Фильтр: 7"),
        ("куда чаще нажимают", "today", "Открытие корзины: 5"),
        ("сколько времени проводят на сайте", "today", "75.5 с"),
        ("какая конверсия в корзину и заказ", "today", "20.00%"),
    ],
)
def test_natural_queries_select_period_and_compose_russian_answer(
    monkeypatch,
    query,
    expected_period,
    answer_fragment,
):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _Response(_payload())

    monkeypatch.setattr(store_analytics, "urlopen", fake_urlopen)
    result = get_store_analytics_report(
        api_url="http://autostop-app:8000/internal/agent/v1",
        read_token="secret",
        query=query,
    )

    assert result["ok"] is True
    assert captured["body"]["period"] == expected_period
    assert answer_fragment in result["answer"]


def test_private_or_raw_backend_fields_fail_closed(monkeypatch):
    payload = _payload()
    payload["rawEvents"] = [{"visitorId": "private", "query": "VIN"}]
    monkeypatch.setattr(store_analytics, "urlopen", lambda *_args, **_kwargs: _Response(payload))

    result = get_store_analytics_report(
        api_url="http://autostop-app:8000/internal/agent/v1",
        read_token="secret",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "store_analytics_response_invalid"
    assert "private" not in str(result)
    assert "VIN" not in str(result)


@pytest.mark.parametrize("mutation", ["raw_events", "timezone", "unequal_period"])
def test_report_rejects_raw_container_wrong_timezone_and_unequal_comparison(
    monkeypatch,
    mutation,
):
    payload = _payload()
    if mutation == "raw_events":
        payload["rawEvents"] = [{"type": "page_view"}]
    elif mutation == "timezone":
        payload["timezone"] = "UTC"
    else:
        payload["previousPeriod"]["start"] = "2026-07-15T00:00:00+07:00"
    monkeypatch.setattr(store_analytics, "urlopen", lambda *_args, **_kwargs: _Response(payload))

    result = get_store_analytics_report(
        api_url="http://autostop-app:8000/internal/agent/v1",
        read_token="secret",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "store_analytics_response_invalid"


def test_report_rejects_unapproved_url_and_never_follows_authorization_redirect():
    assert (
        get_store_analytics_report(
            api_url="http://autostop24.shop/internal/agent/v1",
            read_token="secret",
        )["error"]["code"]
        == "store_analytics_url_invalid"
    )
    assert (
        get_store_analytics_report(
            api_url="https://attacker.example/internal/agent/v1",
            read_token="secret",
        )["error"]["code"]
        == "store_analytics_url_invalid"
    )
    assert (
        get_store_analytics_report(
            api_url="https://autostop24.shop/internal/agent/v1",
            read_token="secret",
        )["error"]["code"]
        == "store_analytics_url_invalid"
    )

    destination_authorizations: list[str | None] = []

    class DestinationHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            destination_authorizations.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *_args):
            return

    destination = ThreadingHTTPServer(("127.0.0.1", 0), DestinationHandler)

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{destination.server_port}/internal/agent/v1/analytics/report",
            )
            self.end_headers()

        def log_message(self, *_args):
            return

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    threads = [
        threading.Thread(target=destination.serve_forever, daemon=True),
        threading.Thread(target=redirect.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        result = get_store_analytics_report(
            api_url=f"http://127.0.0.1:{redirect.server_port}/internal/agent/v1",
            read_token="redirect-secret",
        )
    finally:
        redirect.shutdown()
        destination.shutdown()
        redirect.server_close()
        destination.server_close()
        for thread in threads:
            thread.join(timeout=2)

    assert result["error"]["code"] == "store_analytics_http_302"
    assert destination_authorizations == []


def test_unconfigured_invalid_range_http_network_and_oversize_errors_do_not_leak_bodies(monkeypatch):
    assert (
        get_store_analytics_report(api_url="", read_token="secret")["error"]["code"] == "store_analytics_not_configured"
    )
    invalid = get_store_analytics_report(
        api_url="http://autostop-app:8000/internal/agent/v1",
        read_token="secret",
        period="custom",
        date_from="2026-07-10",
        date_to="2026-07-01",
    )
    assert invalid["status"] == "blocked"
    assert invalid["error"]["code"] == "store_analytics_custom_range_invalid"

    monkeypatch.setattr(
        store_analytics,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            HTTPError("http://autostop-app:8000", 401, "private backend body", {}, None)
        ),
    )
    unauthorized = get_store_analytics_report(
        api_url="http://autostop-app:8000/internal/agent/v1",
        read_token="secret",
    )
    assert unauthorized["error"]["code"] == "store_analytics_http_401"
    assert "private backend body" not in str(unauthorized)

    monkeypatch.setattr(
        store_analytics,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("secret network detail")),
    )
    unavailable = get_store_analytics_report(
        api_url="http://autostop-app:8000/internal/agent/v1",
        read_token="secret",
    )
    assert unavailable["error"]["code"] == "store_analytics_unavailable"
    assert "secret network detail" not in str(unavailable)

    monkeypatch.setattr(store_analytics, "urlopen", lambda *_args, **_kwargs: _Response(b"x" * 2049))
    oversize = get_store_analytics_report(
        api_url="http://autostop-app:8000/internal/agent/v1",
        read_token="secret",
        max_response_bytes=2048,
    )
    assert oversize["error"]["code"] == "store_analytics_response_too_large"
