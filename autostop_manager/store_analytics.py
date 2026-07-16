from __future__ import annotations

from datetime import date, datetime, time, timedelta
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo


STORE_ANALYTICS_FORMAT = "store_analytics_report_v1"
STORE_ANALYTICS_TIMEZONE = ZoneInfo("Asia/Krasnoyarsk")
STORE_ANALYTICS_PATH = "/analytics/report"
STORE_ANALYTICS_MAX_RESPONSE_BYTES = 256 * 1024
STORE_ANALYTICS_PERIODS = frozenset({"auto", "today", "yesterday", "last_7_days", "last_30_days", "custom"})
_SUMMARY_FIELDS = (
    "visitors",
    "sessions",
    "pageViews",
    "engagedSessions",
    "averageEngagedSeconds",
    "medianEngagedSeconds",
    "searches",
    "zeroResultSearches",
    "zeroResultRate",
    "cartAdditions",
    "quoteSubmissions",
    "orders",
    "meaningfulClicks",
)
_FUNNEL_FIELDS = (
    "productViewSessions",
    "cartSessions",
    "quoteOrOrderSessions",
    "orderSessions",
    "viewToCartRate",
    "cartToQuoteOrOrderRate",
    "cartToOrderRate",
    "viewToOrderRate",
)
_FORBIDDEN_RESPONSE_KEYS = frozenset(
    {
        "analytics_events",
        "events",
        "raw_events",
        "rawevents",
        "event_name",
        "occurred_at",
        "ip",
        "ip_address",
        "user_agent",
        "visitor_id",
        "visitorid",
        "session_id",
        "sessionid",
        "customer_id",
        "account_id",
        "email",
        "phone",
        "vin",
        "query",
        "form_data",
    }
)


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl


_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler())


def urlopen(request: Request, timeout: float):
    """Open without redirects so Authorization never crosses an origin boundary."""

    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


def get_store_analytics_report(
    *,
    api_url: str,
    read_token: str,
    query: str = "",
    period: str = "auto",
    date_from: str | None = None,
    date_to: str | None = None,
    top_limit: int = 10,
    timeout: float = 8.0,
    max_response_bytes: int = STORE_ANALYTICS_MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    """Read one bounded aggregate report; never return backend errors or raw events."""

    normalized_url = str(api_url or "").strip().rstrip("/")
    token = str(read_token or "").strip()
    if not normalized_url or not token:
        return _error("store_analytics_not_configured")
    if not _valid_store_api_url(normalized_url):
        return _error("store_analytics_url_invalid", status="blocked")
    try:
        request_body = _build_request(
            query=query,
            period=period,
            date_from=date_from,
            date_to=date_to,
            top_limit=top_limit,
        )
    except ValueError as exc:
        return _error(str(exc), status="blocked")

    body = json.dumps(request_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        f"{normalized_url}{STORE_ANALYTICS_PATH}",
        data=body,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "AutostopManager/analytics",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=max(0.1, min(float(timeout), 30.0))) as response:
            raw = response.read(max(1024, int(max_response_bytes)) + 1)
    except HTTPError as exc:
        return _error(f"store_analytics_http_{int(exc.code)}")
    except (TimeoutError, URLError, OSError):
        return _error("store_analytics_unavailable")
    if len(raw) > max_response_bytes:
        return _error("store_analytics_response_too_large")
    try:
        payload = json.loads(raw.decode("utf-8"))
        report = _allowlisted_report(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        return _error("store_analytics_response_invalid")
    report["answer"] = _natural_answer(report, query=query)
    return report


def _build_request(
    *,
    query: str,
    period: str,
    date_from: str | None,
    date_to: str | None,
    top_limit: int,
) -> dict[str, Any]:
    normalized_period = str(period or "auto").strip().casefold()
    if normalized_period not in STORE_ANALYTICS_PERIODS:
        raise ValueError("store_analytics_period_invalid")
    if normalized_period == "auto":
        normalized_period = _period_from_query(query)
    try:
        limit = max(1, min(int(top_limit), 20))
    except (TypeError, ValueError) as exc:
        raise ValueError("store_analytics_top_limit_invalid") from exc
    result: dict[str, Any] = {
        "period": normalized_period,
        "comparePrevious": True,
        "topLimit": limit,
    }
    if normalized_period == "custom":
        if not date_from or not date_to:
            raise ValueError("store_analytics_custom_dates_required")
        try:
            start_date = date.fromisoformat(str(date_from))
            end_date = date.fromisoformat(str(date_to))
        except ValueError as exc:
            raise ValueError("store_analytics_custom_dates_invalid") from exc
        if start_date > end_date or (end_date - start_date).days >= 62:
            raise ValueError("store_analytics_custom_range_invalid")
        start_at = datetime.combine(start_date, time.min, tzinfo=STORE_ANALYTICS_TIMEZONE)
        end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=STORE_ANALYTICS_TIMEZONE)
        result.update({"startAt": start_at.isoformat(), "endAt": end_at.isoformat()})
    elif date_from is not None or date_to is not None:
        raise ValueError("store_analytics_dates_only_for_custom")
    return result


def _valid_store_api_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return False
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if parsed.path.rstrip("/") != "/internal/agent/v1":
        return False
    if hostname == "autostop24.shop":
        return parsed.scheme == "https" and port in {None, 443}
    if hostname in {"127.0.0.1", "localhost", "::1"}:
        return parsed.scheme in {"http", "https"}
    return False


def _period_from_query(query: str) -> str:
    lowered = str(query or "").casefold()
    if "вчера" in lowered:
        return "yesterday"
    if any(term in lowered for term in ("недел", "7 дней", "семь дней")):
        return "last_7_days"
    if any(term in lowered for term in ("месяц", "30 дней", "тридцать дней")):
        return "last_30_days"
    return "today"


def _allowlisted_report(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema") != STORE_ANALYTICS_FORMAT:
        raise ValueError("unexpected analytics schema")
    if _contains_forbidden_key(payload):
        raise ValueError("analytics response contains raw or private fields")
    if payload.get("timezone") != "Asia/Krasnoyarsk":
        raise ValueError("unexpected analytics timezone")
    meta = payload.get("meta")
    if (
        not isinstance(meta, dict)
        or meta.get("aggregatedOnly") is not True
        or meta.get("rawEventsIncluded") is not False
    ):
        raise ValueError("analytics response is not aggregate-only")
    summary = _number_map(payload.get("summary"), _SUMMARY_FIELDS)
    funnel = _number_map(payload.get("funnel"), _FUNNEL_FIELDS)
    period = _period(payload.get("period"))
    previous_period = _period(payload.get("previousPeriod"))
    _validate_equal_periods(period, previous_period)
    return {
        "ok": bool(payload.get("ok")),
        "format": STORE_ANALYTICS_FORMAT,
        "status": "completed" if payload.get("ok") else "failed",
        "timezone": "Asia/Krasnoyarsk",
        "periodPreset": _safe_text(payload.get("periodPreset"), 32),
        "period": period,
        "previousPeriod": previous_period,
        "summary": summary,
        "topPages": _top_pages(payload.get("topPages")),
        "topProducts": _top_products(payload.get("topProducts")),
        "clicks": _clicks(payload.get("clicks")),
        "funnel": funnel,
        "comparison": _comparison(payload.get("comparison")),
        "meta": {
            "aggregatedOnly": True,
            "rawEventsIncluded": False,
            "retentionDays": _integer(meta.get("retentionDays")),
            "engagedTimeUnit": "seconds_per_session",
            "source": "autostop_store_aggregate_api",
        },
    }


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized in _FORBIDDEN_RESPONSE_KEYS:
                return True
            if _contains_forbidden_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _period(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid analytics period")
    start = _safe_text(value.get("start"), 64)
    end = _safe_text(value.get("end"), 64)
    if not start or not end:
        raise ValueError("incomplete analytics period")
    return {"start": start, "end": end}


def _validate_equal_periods(
    period: dict[str, str] | None,
    previous_period: dict[str, str] | None,
) -> None:
    if period is None or previous_period is None:
        raise ValueError("analytics comparison period is missing")
    try:
        current_start = datetime.fromisoformat(period["start"])
        current_end = datetime.fromisoformat(period["end"])
        previous_start = datetime.fromisoformat(previous_period["start"])
        previous_end = datetime.fromisoformat(previous_period["end"])
    except ValueError as exc:
        raise ValueError("invalid analytics period timestamp") from exc
    values = (current_start, current_end, previous_start, previous_end)
    if any(value.tzinfo is None for value in values):
        raise ValueError("analytics period timezone is missing")
    current_duration = current_end - current_start
    previous_duration = previous_end - previous_start
    if current_duration <= timedelta(0) or current_duration != previous_duration:
        raise ValueError("analytics comparison periods are not equal")


def _number_map(value: Any, fields: tuple[str, ...]) -> dict[str, int | float]:
    if not isinstance(value, dict):
        raise ValueError("invalid analytics metric group")
    return {field: _number(value.get(field)) for field in fields}


def _top_pages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("invalid top pages")
    return [
        {"path": _safe_text(item.get("path"), 240), "views": _integer(item.get("views"))}
        for item in value[:20]
        if isinstance(item, dict)
    ]


def _top_products(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("invalid top products")
    return [
        {
            "productId": _safe_text(item.get("productId"), 512),
            "name": _safe_text(item.get("name"), 300) or None,
            "sku": _safe_text(item.get("sku"), 80) or None,
            "views": _integer(item.get("views")),
            "cartAdditions": _integer(item.get("cartAdditions")),
        }
        for item in value[:20]
        if isinstance(item, dict)
    ]


def _clicks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("invalid clicks")
    return [
        {
            "event": _safe_text(item.get("event"), 32),
            "label": _safe_text(item.get("label"), 120),
            "count": _integer(item.get("count")),
        }
        for item in value[:20]
        if isinstance(item, dict)
    ]


def _comparison(value: Any) -> dict[str, dict[str, int | float | None]]:
    if not isinstance(value, dict):
        raise ValueError("invalid analytics comparison")
    allowed = {*_SUMMARY_FIELDS, *_FUNNEL_FIELDS}
    result: dict[str, dict[str, int | float | None]] = {}
    for field in allowed:
        item = value.get(field)
        if not isinstance(item, dict):
            continue
        result[field] = {
            key: None if item.get(key) is None else _number(item.get(key))
            for key in ("current", "previous", "delta", "percentChange", "deltaPercentagePoints")
            if key in item
        }
    return result


def _natural_answer(report: dict[str, Any], *, query: str) -> str:
    lowered = str(query or "").casefold()
    summary = report["summary"]
    funnel = report["funnel"]
    if any(term in lowered for term in ("время", "сколько времени", "проводят на сайте")):
        return (
            f"Активное время на сессию: в среднем {summary['averageEngagedSeconds']:.1f} с, "
            f"медиана {summary['medianEngagedSeconds']:.1f} с; измерено сессий: {summary['engagedSessions']}."
        )
    if any(term in lowered for term in ("товар", "смотрел", "популярн")):
        items = report["topProducts"][:5]
        if not items:
            return "За выбранный период просмотров товаров пока нет."
        rendered = "; ".join(
            f"{item.get('name') or item.get('sku') or item.get('productId')}: {item['views']}" for item in items
        )
        return f"Чаще всего смотрели: {rendered}."
    if any(term in lowered for term in ("нажим", "клик", "куда чаще")):
        items = report["clicks"][:5]
        if not items:
            return "За выбранный период значимых нажатий пока нет."
        return "Значимые нажатия: " + "; ".join(f"{item['label']}: {item['count']}" for item in items) + "."
    if any(term in lowered for term in ("конверс", "ворон", "в корзину", "в заказ")):
        return (
            f"Конверсия с просмотра товара в корзину — {funnel['viewToCartRate']:.2f}%, "
            f"в заказ — {funnel['viewToOrderRate']:.2f}%; заказов: {summary['orders']}, "
            f"заявок: {summary['quoteSubmissions']}."
        )
    return (
        f"За выбранный период: посетителей {summary['visitors']}, сессий {summary['sessions']}, "
        f"просмотров страниц {summary['pageViews']}."
    )


def _number(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("analytics metric must be numeric")
    return value


def _integer(value: Any) -> int:
    number = _number(value)
    return int(number)


def _safe_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("analytics text field must be a string")
    return value[:limit]


def _error(code: str, *, status: str = "failed") -> dict[str, Any]:
    return {
        "ok": False,
        "format": STORE_ANALYTICS_FORMAT,
        "status": status,
        "error": {"code": str(code)},
        "warnings": [str(code)],
        "meta": {
            "aggregatedOnly": True,
            "rawEventsIncluded": False,
            "source": "autostop_store_aggregate_api",
        },
    }
