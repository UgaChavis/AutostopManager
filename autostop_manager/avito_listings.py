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

from .avito_listing_identity import clean_avito_listing_url
from .j1_fetch import contains_sensitive, redact_sensitive

_API_BASE_URL = "https://api.reefapi.com"
_API_KEY_ENV = "REEFAPI_API_KEY"
_REQUEST_TIMEOUT_SECONDS = 30.0
_MAX_RESPONSE_BYTES = 5_000_000
_MAX_ERROR_RESPONSE_BYTES = 64_000
_MAX_PROVIDER_ROWS = 50
_MAX_QUERY_CHARS = 256
_MAX_TEXT_CHARS = 8_000
_AD_ID = re.compile(r"^[0-9]{6,20}$")
_PROVIDER_ERRORS = {
    "MISSING_PARAM": ("request_rejected", False),
    "INVALID_PARAM": ("request_rejected", False),
    "AUTH_FAILED": ("authentication_failed", False),
    "QUOTA_EXCEEDED": ("quota_exceeded", False),
    "NOT_FOUND": ("listing_not_found", False),
    "RATE_LIMITED": ("rate_limited", True),
    "TARGET_BLOCKED": ("source_blocked", True),
    "UPSTREAM_TIMEOUT": ("provider_timeout", True),
    "PARSE_ERROR": ("provider_parse_error", False),
    "DISABLED": ("provider_disabled", False),
    "INTERNAL": ("provider_unavailable", True),
}
_RETRYABLE_ERRORS = frozenset({"rate_limited", "source_blocked", "provider_timeout", "provider_unavailable"})


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
        return _failure(response["error"], details=response)

    data = response["data"]
    rows = data.get("listings") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return _failure("malformed_response")
    listings = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    scanned_count = rejected_count = duplicate_count = 0
    for item in rows[:_MAX_PROVIDER_ROWS]:
        scanned_count += 1
        if not isinstance(item, dict):
            rejected_count += 1
            continue
        normalized = _normalize_listing(item)
        if normalized is None:
            rejected_count += 1
            continue
        listing_id = normalized["listing_id"]
        url = normalized["url"]
        if listing_id in seen_ids or url in seen_urls:
            duplicate_count += 1
            continue
        seen_ids.add(listing_id)
        seen_urls.add(url)
        listings.append(normalized)
        if len(listings) == limit:
            break
    counts = {
        "provider_count": len(rows),
        "scanned_count": scanned_count,
        "rejected_count": rejected_count,
        "duplicate_count": duplicate_count,
        "unscanned_count": len(rows) - scanned_count,
    }
    if rows and not listings:
        return {**_failure("malformed_response"), **counts}
    return {
        "ok": True,
        "source": "avito",
        "verification": "provider_response_received",
        "count": len(listings),
        "listings": listings,
        **counts,
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
        return _failure(response["error"], details=response)

    raw = response["data"]
    if isinstance(raw, dict) and isinstance(raw.get("listing"), dict):
        raw = raw["listing"]
    if not isinstance(raw, dict):
        return _failure("malformed_response")
    listing = _normalize_listing(raw, fallback_id=normalized_id, fallback_url=fallback_url)
    if listing is None or listing["listing_id"] != normalized_id:
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
    if not raw or len(raw) > 2_048:
        return {"ok": False, "error": "invalid_or_sensitive_listing_id"}
    if _AD_ID.fullmatch(raw):
        return {"ok": True, "listing_id": raw, "url": None}

    url = clean_avito_listing_url(raw, allow_http=True, allow_tracking=True)
    if url is None:
        return {"ok": False, "error": "invalid_or_sensitive_listing_id"}
    match = re.search(r"(?:_|/)([0-9]{6,20})\Z", urlsplit(url).path)
    if not match:
        return {"ok": False, "error": "invalid_listing_url"}
    listing_id = match.group(1)
    return {"ok": True, "listing_id": listing_id, "url": url}


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
    http_status = None
    try:
        with _open_request(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            http_status = _safe_http_status(getattr(response, "status", 200))
            body = response.read(_MAX_RESPONSE_BYTES + 1)
        if not isinstance(body, bytes):
            return _provider_failure("malformed_response", http_status=http_status)
        if len(body) > _MAX_RESPONSE_BYTES:
            return _provider_failure("response_too_large", http_status=http_status)
        parsed_body = json.loads(body.decode("utf-8"))
    except HTTPError as exc:
        return _http_failure(exc)
    except TimeoutError:
        return _provider_failure("provider_timeout", http_status=http_status)
    except URLError as exc:
        error = "provider_timeout" if isinstance(exc.reason, TimeoutError) else "provider_unavailable"
        return _provider_failure(error, http_status=http_status)
    except (OSError, http.client.HTTPException):
        return _provider_failure("provider_unavailable", http_status=http_status)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return _provider_failure("malformed_response", http_status=http_status)

    if not isinstance(parsed_body, dict):
        return _provider_failure("malformed_response", http_status=http_status)
    if parsed_body.get("ok") is not True:
        return _normalized_provider_error(parsed_body.get("error"), http_status=http_status)
    if "data" not in parsed_body:
        return _provider_failure("malformed_response", http_status=http_status)
    return {"ok": True, "data": parsed_body["data"]}


def _safe_http_status(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599 else None


def _provider_failure(
    error: str, *, http_status: int | None = None, provider_code: str | None = None, retryable: bool | None = None
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": error,
        "provider_code": provider_code if provider_code in _PROVIDER_ERRORS else None,
        "http_status": _safe_http_status(http_status),
        "retryable": retryable if isinstance(retryable, bool) else error in _RETRYABLE_ERRORS,
    }


def _normalized_provider_error(value: Any, *, http_status: int | None) -> dict[str, Any]:
    code = value.get("code") if isinstance(value, dict) else None
    if isinstance(code, str) and code in _PROVIDER_ERRORS:
        error, default_retryable = _PROVIDER_ERRORS[code]
        retryable = value.get("retryable")
        return _provider_failure(
            error,
            http_status=http_status,
            provider_code=code,
            retryable=retryable if isinstance(retryable, bool) else default_retryable,
        )
    error = _http_error_code(http_status) if http_status is not None and http_status >= 400 else "provider_error"
    return _provider_failure(error, http_status=http_status)


def _http_failure(exc: HTTPError) -> dict[str, Any]:
    provider_error = None
    try:
        if exc.fp is not None:
            body = exc.read(_MAX_ERROR_RESPONSE_BYTES + 1)
            if isinstance(body, bytes) and len(body) <= _MAX_ERROR_RESPONSE_BYTES:
                parsed = json.loads(body.decode("utf-8"))
                if isinstance(parsed, dict) and parsed.get("ok") is False:
                    provider_error = parsed.get("error")
    except (OSError, http.client.HTTPException, UnicodeDecodeError, ValueError, RecursionError):
        pass  # The HTTP status remains useful even if its error body is bad.
    finally:
        exc.close()
    return _normalized_provider_error(provider_error, http_status=_safe_http_status(exc.code))


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
    if status == 402:
        return "quota_exceeded"
    if status == 404:
        return "listing_not_found"
    if status == 429:
        return "rate_limited"
    if status == 504:
        return "provider_timeout"
    if 500 <= status <= 599:
        return "provider_unavailable"
    return "request_rejected"


def _normalize_listing(
    raw: dict[str, Any], *, fallback_id: str | None = None, fallback_url: str | None = None
) -> dict[str, Any] | None:
    raw_id = raw.get("ad_id", raw.get("id"))
    listing_id = _normalize_listing_id(fallback_id if raw_id is None else raw_id)
    if listing_id is None:
        return None

    title = _clean_text(raw.get("title"), limit=300)
    if not title:
        return None
    raw_url = raw.get("url")
    url = fallback_url if raw_url is None else _clean_avito_url(raw_url)
    if url is None:
        return None
    source_identity = _parse_listing_identifier(url)
    if not source_identity["ok"] or source_identity["listing_id"] != listing_id:
        return None
    price_rub, price_text, price_qualifier = _normalize_price(raw)
    location = raw.get("location")
    city_value = location.get("name") if isinstance(location, dict) else location
    if not isinstance(city_value, str):
        city_value = raw.get("city")
    description = _clean_text(raw.get("description"), limit=_MAX_TEXT_CHARS)
    description_source = "description" if description else None
    if not description:
        description = _clean_text(raw.get("description_snippet"), limit=_MAX_TEXT_CHARS)
        if description:
            description_source = "description_snippet"

    return {
        "source": "avito",
        "listing_id": listing_id,
        "url": url,
        "title": title,
        "description": description,
        "description_source": description_source,
        "price_rub": price_rub,
        "price_text": price_text,
        "price_qualifier": price_qualifier,
        "city": _clean_text(city_value, limit=120) or None,
        "condition": _listing_condition(raw),
        "seller": _public_seller_fields(raw.get("seller")),
        "delivery": _listing_delivery(raw),
        "published_at": _clean_text(raw.get("published_at"), limit=80) or None,
        "published_or_raised_text": _clean_text(raw.get("published_or_raised_text"), limit=120) or None,
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
    return clean_avito_listing_url(value, allow_http=True, allow_tracking=True)


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
    parameters = raw.get("params")
    if isinstance(parameters, list):
        for parameter in parameters:
            if isinstance(parameter, dict) and _condition_parameter_name(parameter):
                return _condition_parameter_value(parameter)
    direct = raw.get("condition")
    if isinstance(direct, str) and direct.strip():
        return _clean_text(direct, limit=120)
    parameters = raw.get("parameters")
    if not isinstance(parameters, list):
        return None
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        if _condition_parameter_name(parameter):
            return _condition_parameter_value(parameter)
    return None


def _condition_parameter_name(value: dict[str, Any]) -> bool:
    name = value.get("name", value.get("title"))
    return isinstance(name, str) and name.strip().casefold() in {"состояние", "condition"}


def _condition_parameter_value(value: dict[str, Any]) -> str | None:
    return _clean_text(value.get("value", value.get("description")), limit=120) or None


def _listing_delivery(raw: dict[str, Any]) -> dict[str, Any] | None:
    available = raw.get("delivery_available")
    label = _clean_text(raw.get("delivery_text"), limit=100) or None
    legacy = _public_delivery_field(
        raw.get("delivery") if raw.get("delivery") is not None else raw.get("avito_delivery")
    )
    if isinstance(available, bool):
        if label is None and legacy is not None and legacy["available"] is available:
            label = legacy["label"]
        return {"available": available, "label": label}
    if label is not None:
        return {"available": legacy["available"] if legacy is not None else None, "label": label}
    return legacy


def _public_seller_fields(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    name = _clean_text(value.get("name"), limit=120) or None
    seller_type = _clean_text(value.get("type"), limit=60) or None
    rating = value.get("rating")
    try:
        valid_rating = not isinstance(rating, bool) and isinstance(rating, (int, float)) and math.isfinite(rating)
    except OverflowError:
        valid_rating = False
    if not valid_rating:
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


def _failure(code: Any, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
    safe = code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,48}", code) else "provider_error"
    result: dict[str, Any] = {"ok": False, "source": "avito", "error": safe}
    if details is not None and isinstance(details.get("retryable"), bool):
        provider_code = details.get("provider_code")
        result.update(
            provider_code=provider_code
            if isinstance(provider_code, str) and provider_code in _PROVIDER_ERRORS
            else None,
            http_status=_safe_http_status(details.get("http_status")),
            retryable=details["retryable"],
        )
    return result
