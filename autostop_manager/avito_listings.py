"""Bounded, read-only lookup of public Avito listings through ReefAPI."""

from __future__ import annotations

from datetime import UTC, datetime
import html
import http.client
import json
import math
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .j1_fetch import contains_sensitive, redact_sensitive

_API_BASE_URL = "https://api.reefapi.com"
_API_KEY_ENV = "REEFAPI_API_KEY"
_REQUEST_TIMEOUT_SECONDS = 30.0
_MAX_RESPONSE_BYTES = 5_000_000
_MAX_QUERY_CHARS = 256
_MAX_TEXT_CHARS = 8_000
_AD_ID = re.compile(r"^\d{6,20}$")
_SAFE_PROVIDER_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


class _NoRedirectHandler(HTTPRedirectHandler):
    """Keep the ReefAPI key on the configured API origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_request(request: Request, *, timeout: float):
    return build_opener(_NoRedirectHandler()).open(request, timeout=timeout)


def avito_search_listings(
    query: str,
    location: str = "krasnoyarsk",
    category: str = "zapchasti_i_aksessuary",
    page: int = 1,
    limit: int = 50,
    price_min: int | None = None,
    price_max: int | None = None,
    delivery_only: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Search one bounded Avito results page by text, location and category.

    ``limit`` bounds the normalized response. ReefAPI returns at most 50 rows
    per page, so values above 50 are rejected instead of silently ignored.
    """

    plan = _search_request(
        query=query,
        location=location,
        category=category,
        page=page,
        limit=limit,
        price_min=price_min,
        price_max=price_max,
        delivery_only=delivery_only,
    )
    if not plan["ok"]:
        return _failure(plan["error"])

    request_payload = plan["payload"]
    if dry_run:
        return {
            "ok": True,
            "source": "avito",
            "dry_run": True,
            "verification": "configuration_only",
            "request": {**request_payload, "limit": limit},
            "count": 0,
            "listings": [],
        }

    response = _request_json("search", request_payload)
    if not response["ok"]:
        return _failure(response["error"])

    data = response["data"]
    rows = data.get("listings") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return _failure("malformed_response")
    if not rows:
        return {
            "ok": True,
            "source": "avito",
            "verification": "provider_response_received",
            "count": 0,
            "listings": [],
        }

    listings = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    for item in rows[:limit]:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_listing(item)
        if normalized is None:
            continue
        listing_id = normalized["listing_id"]
        url = normalized["url"]
        if listing_id in seen_ids or url in seen_urls:
            continue
        seen_ids.add(listing_id)
        seen_urls.add(url)
        listings.append(normalized)
    if not listings:
        return _failure("malformed_response")
    return {
        "ok": True,
        "source": "avito",
        "verification": "provider_response_received",
        "count": len(listings),
        "listings": listings,
    }


def avito_read_listing(ad_id: str, dry_run: bool = False) -> dict[str, Any]:
    """Read one public Avito listing by numeric ID or an Avito listing URL."""

    parsed = _parse_listing_identifier(ad_id)
    if not parsed["ok"]:
        return _failure(parsed["error"])
    normalized_id = parsed["listing_id"]
    fallback_url = parsed["url"]
    if dry_run:
        return {
            "ok": True,
            "source": "avito",
            "dry_run": True,
            "verification": "configuration_only",
            "request": {"listing_id": normalized_id},
        }

    response = _request_json("listing", {"ad_id": normalized_id})
    if not response["ok"]:
        return _failure(response["error"])

    raw = response["data"]
    if isinstance(raw, dict) and isinstance(raw.get("listing"), dict):
        raw = raw["listing"]
    if not isinstance(raw, dict):
        return _failure("malformed_response")
    listing = _normalize_listing(raw, fallback_id=normalized_id, fallback_url=fallback_url)
    if listing is None:
        return _failure("malformed_response")
    return {
        "ok": True,
        "source": "avito",
        "verification": "provider_response_received",
        "listing": listing,
    }


def _search_request(
    *,
    query: str,
    location: str,
    category: str,
    page: int,
    limit: int,
    price_min: int | None,
    price_max: int | None,
    delivery_only: bool,
) -> dict[str, Any]:
    clean_query = _validated_text_input(query, max_chars=_MAX_QUERY_CHARS)
    if clean_query is None:
        return {"ok": False, "error": "invalid_or_sensitive_query"}
    clean_location = _validated_text_input(location, max_chars=100)
    if clean_location is None:
        return {"ok": False, "error": "invalid_location"}
    clean_category = _validated_text_input(category, max_chars=160)
    if clean_category is None:
        return {"ok": False, "error": "invalid_category"}
    if not _is_bounded_int(page, minimum=1, maximum=30):
        return {"ok": False, "error": "invalid_page"}
    if not _is_bounded_int(limit, minimum=1, maximum=50):
        return {"ok": False, "error": "invalid_limit"}
    if not isinstance(delivery_only, bool):
        return {"ok": False, "error": "invalid_delivery_filter"}
    if price_min is not None and not _is_nonnegative_price(price_min):
        return {"ok": False, "error": "invalid_price_min"}
    if price_max is not None and not _is_nonnegative_price(price_max):
        return {"ok": False, "error": "invalid_price_max"}
    if price_min is not None and price_max is not None and price_min > price_max:
        return {"ok": False, "error": "invalid_price_range"}

    payload: dict[str, Any] = {
        "query": clean_query,
        "location": clean_location,
        "category": clean_category,
        "page": page,
        "delivery_only": delivery_only,
    }
    if price_min is not None:
        payload["price_min"] = price_min
    if price_max is not None:
        payload["price_max"] = price_max
    return {"ok": True, "payload": payload}


def _validated_text_input(value: Any, *, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    if not clean or len(clean) > max_chars or any(ord(char) < 32 for char in clean) or contains_sensitive(clean):
        return None
    return clean


def _is_bounded_int(value: Any, *, minimum: int, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


def _is_nonnegative_price(value: Any) -> bool:
    return _is_bounded_int(value, minimum=0, maximum=2_000_000_000)


def _parse_listing_identifier(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {"ok": False, "error": "invalid_listing_id"}
    raw = value.strip()
    if not raw or len(raw) > 2_048 or contains_sensitive(raw):
        return {"ok": False, "error": "invalid_or_sensitive_listing_id"}
    if _AD_ID.fullmatch(raw):
        return {"ok": True, "listing_id": raw, "url": None}

    try:
        parsed = urlsplit(raw)
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return {"ok": False, "error": "invalid_listing_url"}
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
        or not _is_avito_host(hostname)
    ):
        return {"ok": False, "error": "listing_url_must_be_avito"}
    match = re.search(r"(?:_|/)(\d{6,20})/?$", parsed.path)
    if not match:
        return {"ok": False, "error": "invalid_listing_url"}
    listing_id = match.group(1)
    # Tracking parameters and fragments are unnecessary for a listing lookup.
    clean_path = parsed.path.rstrip("/")
    url = urlunsplit(("https", hostname, clean_path, "", ""))
    return {"ok": True, "listing_id": listing_id, "url": url}


def _is_avito_host(hostname: str) -> bool:
    return hostname == "avito.ru" or hostname.endswith(".avito.ru")


def _request_json(action: str, payload: dict[str, Any], *, base_url: str | None = None) -> dict[str, Any]:
    api_key = os.getenv(_API_KEY_ENV, "").strip()
    if not api_key:
        return {"ok": False, "error": "api_key_missing"}
    if len(api_key) > 512 or not api_key.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in api_key):
        return {"ok": False, "error": "api_key_invalid"}
    if action not in {"search", "listing"}:
        return {"ok": False, "error": "invalid_action"}
    actual_base_url = _safe_base_url(base_url or _API_BASE_URL)
    if actual_base_url is None:
        return {"ok": False, "error": "invalid_api_base_url"}

    url = f"{actual_base_url}/avito/v1/{action}"
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"x-api-key": api_key, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with _open_request(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
        if not isinstance(body, bytes):
            return {"ok": False, "error": "malformed_response"}
        if len(body) > _MAX_RESPONSE_BYTES:
            return {"ok": False, "error": "response_too_large"}
        parsed_body = json.loads(body.decode("utf-8"))
    except HTTPError as exc:
        return {"ok": False, "error": _http_error_code(exc.code)}
    except (TimeoutError, URLError, OSError, http.client.HTTPException):
        return {"ok": False, "error": "provider_unavailable"}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"ok": False, "error": "malformed_response"}

    if not isinstance(parsed_body, dict):
        return {"ok": False, "error": "malformed_response"}
    if parsed_body.get("ok") is not True:
        provider_error = parsed_body.get("error")
        provider_code = provider_error.get("code") if isinstance(provider_error, dict) else None
        safe_code = (
            provider_code if isinstance(provider_code, str) and _SAFE_PROVIDER_CODE.fullmatch(provider_code) else None
        )
        if safe_code == "RATE_LIMITED":
            return {"ok": False, "error": "rate_limited"}
        if safe_code == "AUTH_FAILED":
            return {"ok": False, "error": "authentication_failed"}
        if safe_code == "TARGET_BLOCKED":
            return {"ok": False, "error": "source_blocked"}
        return {"ok": False, "error": "provider_error"}
    if "data" not in parsed_body:
        return {"ok": False, "error": "malformed_response"}
    return {"ok": True, "data": parsed_body["data"]}


def _safe_base_url(value: str) -> str | None:
    try:
        parsed = urlsplit(str(value))
        host = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return None
    production_origin = parsed.scheme == "https" and host == "api.reefapi.com" and port in {None, 443}
    test_loopback = (
        parsed.scheme in {"http", "https"}
        and host in {"localhost", "127.0.0.1", "::1"}
        and parsed.username is None
        and parsed.password is None
    )
    if (
        not (production_origin or test_loopback)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def _http_error_code(status: int) -> str:
    if status == 401:
        return "authentication_failed"
    if status == 403:
        return "provider_forbidden"
    if status == 429:
        return "rate_limited"
    if 500 <= status <= 599:
        return "provider_unavailable"
    return "request_rejected"


def _normalize_listing(
    raw: dict[str, Any], *, fallback_id: str | None = None, fallback_url: str | None = None
) -> dict[str, Any] | None:
    listing_id = _normalize_listing_id(raw.get("ad_id", raw.get("id", fallback_id)))
    if listing_id is None:
        listing_id = _normalize_listing_id(fallback_id)
    if listing_id is None:
        return None

    title = _clean_text(raw.get("title"), limit=300)
    if not title:
        return None
    url = _clean_avito_url(raw.get("url")) or fallback_url
    if url is None:
        return None
    price_rub, price_text, price_qualifier = _normalize_price(raw)
    location = raw.get("location")
    city_value = location.get("name") if isinstance(location, dict) else location
    if not isinstance(city_value, str):
        city_value = raw.get("city")

    return {
        "source": "avito",
        "listing_id": listing_id,
        "url": url,
        "title": title,
        "description": _clean_text(raw.get("description"), limit=_MAX_TEXT_CHARS),
        "price_rub": price_rub,
        "price_text": price_text,
        "price_qualifier": price_qualifier,
        "city": _clean_text(city_value, limit=120) or None,
        "condition": _listing_condition(raw),
        "seller": _public_seller_fields(raw.get("seller")),
        "delivery": _public_delivery_field(
            raw.get("delivery") if raw.get("delivery") is not None else raw.get("avito_delivery")
        ),
        "published_at": _clean_text(raw.get("published_at"), limit=80) or None,
        "availability": None,
        "observed_at": datetime.now(UTC).isoformat(),
        "status": "lead",
        "fitment_confirmed": False,
        "availability_confirmed": False,
    }


def _normalize_listing_id(value: Any) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if isinstance(value, str) and _AD_ID.fullmatch(value.strip()):
        return value.strip()
    return None


def _clean_avito_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 2_048:
        return None
    try:
        parsed = urlsplit(value.strip())
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
        or not _is_avito_host(hostname)
    ):
        return None
    if contains_sensitive(value):
        return None
    return urlunsplit(("https", hostname, parsed.path, "", ""))


def _normalize_price(raw: dict[str, Any]) -> tuple[int | None, str | None, str]:
    price = _nonnegative_integer(raw.get("price"))
    minimum = _nonnegative_integer(raw.get("price_min"))
    maximum = _nonnegative_integer(raw.get("price_max"))
    price_text = _clean_text(raw.get("price_text"), limit=100) or None
    price_text_lower = (price_text or "").casefold()
    text_says_free = "бесплатно" in price_text_lower or "даром" in price_text_lower
    text_says_unpublished = bool(re.search(r"не\s*(?:указан|опубликован)|по\s+запросу", price_text_lower))
    text_says_from = bool(re.match(r"^(?:от|from)\b", price_text_lower))
    text_says_range = bool(re.search(r"\d\s*[-–—]\s*\d", price_text_lower))
    if raw.get("is_free") is True or text_says_free:
        qualifier = "free"
        price = 0
    elif raw.get("price_not_published") is True or text_says_unpublished:
        qualifier = "unpublished"
        price = None
    elif minimum is not None or maximum is not None or text_says_range:
        qualifier = "range"
        price = None
    elif raw.get("price_is_from") is True or text_says_from:
        qualifier = "from"
    elif price is not None:
        qualifier = "fixed"
    else:
        qualifier = "unknown"

    if price_text is None:
        if qualifier == "free":
            price_text = "Бесплатно"
        elif qualifier == "unpublished":
            price_text = "Цена не указана"
        elif qualifier == "range":
            low = minimum
            high = maximum
            if low is not None and high is not None:
                price_text = f"{low}–{high} ₽"
            elif low is not None:
                price_text = f"от {low} ₽"
            elif high is not None:
                price_text = f"до {high} ₽"
        elif price is not None:
            price_text = f"{'от ' if qualifier == 'from' else ''}{price} ₽"
    return price, price_text, qualifier


def _nonnegative_integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and math.isfinite(value) and value >= 0 and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\s*\d{1,12}\s*", value):
        return int(value)
    return None


def _listing_condition(raw: dict[str, Any]) -> str | None:
    direct = raw.get("condition")
    if isinstance(direct, str) and direct.strip():
        return _clean_text(direct, limit=120)
    parameters = raw.get("parameters")
    if not isinstance(parameters, list):
        return None
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        name = parameter.get("name", parameter.get("title"))
        if isinstance(name, str) and name.strip().casefold() in {"состояние", "condition"}:
            value = parameter.get("value", parameter.get("description"))
            return _clean_text(value, limit=120) or None
    return None


def _public_seller_fields(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    name = _clean_text(value.get("name"), limit=120) or None
    seller_type = _clean_text(value.get("type"), limit=60) or None
    rating = value.get("rating")
    if isinstance(rating, bool) or not isinstance(rating, (int, float)) or not math.isfinite(rating):
        rating = None
    reviews = _clean_text(value.get("reviews"), limit=80) or None
    reviews_count = _nonnegative_integer(value.get("reviews_count"))
    if name is None and seller_type is None and rating is None and reviews is None and reviews_count is None:
        return None
    # Include only the public display name and basic marketplace metadata;
    # seller IDs, phones, addresses, coordinates and summaries are excluded.
    return {
        "name": name,
        "type": seller_type,
        "rating": rating,
        "reviews_count": reviews_count,
        "reviews": reviews,
    }


def _public_delivery_field(value: Any) -> dict[str, Any] | None:
    if isinstance(value, bool):
        return {"available": value, "label": None}
    if isinstance(value, str):
        label = _clean_text(value, limit=100) or None
        return {"available": None, "label": label} if label is not None else None
    if not isinstance(value, dict):
        return None
    available = value.get("available", value.get("is_available"))
    if not isinstance(available, bool):
        available = None
    label = _clean_text(value.get("label", value.get("type", value.get("name"))), limit=100) or None
    if available is None and label is None:
        return None
    return {"available": available, "label": label}


def _clean_text(value: Any, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    # Search snippets and descriptions are untrusted listing content. Strip
    # markup, then redact contact details, secrets and full VINs before return.
    text = re.sub(r"<[^>]*>", " ", html.unescape(value))
    text = " ".join(text.split())
    return redact_sensitive(text, limit=limit)


def _failure(code: Any) -> dict[str, Any]:
    safe = code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,48}", code) else "provider_error"
    return {"ok": False, "source": "avito", "error": safe}
