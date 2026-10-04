"""Read-only price summary for a supplied sample of normalized Avito part listings."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from statistics import median
from typing import Any
from urllib.parse import urlsplit

from .avito_listing_identity import clean_avito_listing_url
from .j1_fetch import contains_sensitive


_MAX_LISTINGS = 60  # One search page plus a few separately read listing details.
_MAX_PRICE_RUB = 10_000_000
_LISTING_ID = re.compile(r"\d{6,20}\Z")
_ARTICLE_TOKEN = re.compile(r"[^\W_][\w._/\\-]*", re.UNICODE)
_PART_NUMBER = re.compile(r"[\w./\\-]+\Z", re.UNICODE)
_CONDITION_ALIASES = {
    "new": "new",
    "новый": "new",
    "новая": "new",
    "новое": "new",
    "новые": "new",
    "used": "used",
    "б/у": "used",
    "бу": "used",
    "с пробегом": "used",
}
_CITY_ALIASES = {"krasnoyarsk": "красноярск"}


def _compact(value: Any, *, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _article_key(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


def _article_in_listing(text: str, part_number: str) -> bool:
    return any(_article_key(match.group()) == part_number for match in _ARTICLE_TOKEN.finditer(text))


def _condition_key(value: Any) -> str | None:
    return _CONDITION_ALIASES.get(_compact(value, limit=120).casefold())


def _city_key(value: str) -> str:
    key = value.casefold()
    return _CITY_ALIASES.get(key, key)


def _avito_url(value: Any, listing_id: str) -> str | None:
    url = clean_avito_listing_url(value)
    if url is None:
        return None
    path_id = re.search(r"(?:_|/)(\d{6,20})/?\Z", urlsplit(url).path)
    if path_id is None or path_id.group(1) != listing_id:
        return None
    return url


def _observed_at(value: Any) -> tuple[str, datetime] | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        if len(value) == 10:
            observed = datetime.combine(date.fromisoformat(value), datetime.min.time(), tzinfo=UTC)
            display = observed.date().isoformat()
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            observed = parsed.astimezone(UTC)
            display = observed.isoformat()
    except (ValueError, OverflowError):
        return None
    if observed > datetime.now(UTC):
        return None
    return display, observed


def _candidate(row: Any, *, index: int, part_number: str, condition: str | None, city: str | None):
    if not isinstance(row, Mapping):
        return None, {"listing_index": index, "code": "listing_not_object"}
    listing_id = row.get("listing_id")
    if not isinstance(listing_id, str) or not _LISTING_ID.fullmatch(listing_id):
        return None, {"listing_index": index, "code": "source_identity_invalid"}
    url = _avito_url(row.get("url"), listing_id)
    if row.get("source") != "avito" or url is None:
        return None, {"listing_index": index, "code": "source_identity_invalid"}
    title = row.get("title")
    description = row.get("description")
    if (
        not isinstance(title, str)
        or not title
        or len(title) > 300
        or not isinstance(description, str)
        or len(description) > 8000
    ):
        return None, {"listing_index": index, "code": "listing_text_invalid"}
    article_field = (
        "title"
        if _article_in_listing(title, part_number)
        else "description"
        if _article_in_listing(description, part_number)
        else None
    )
    if article_field is None:
        return None, {"listing_index": index, "code": "part_number_not_in_listing"}
    price = row.get("price_rub")
    if (
        row.get("price_qualifier") != "fixed"
        or isinstance(price, bool)
        or not isinstance(price, int)
        or not 1 <= price <= _MAX_PRICE_RUB
    ):
        return None, {"listing_index": index, "code": "fixed_price_missing"}
    listing_condition = _condition_key(row.get("condition"))
    if listing_condition is None:
        return None, {"listing_index": index, "code": "condition_unknown"}
    if condition is not None and listing_condition != condition:
        return None, {"listing_index": index, "code": "condition_mismatch"}
    listing_city = _compact(row.get("city"), limit=120)
    if not listing_city or contains_sensitive(listing_city):
        return None, {"listing_index": index, "code": "city_unknown"}
    if city is not None and _city_key(listing_city) != city:
        return None, {"listing_index": index, "code": "city_mismatch"}
    observed = _observed_at(row.get("observed_at"))
    if observed is None:
        return None, {"listing_index": index, "code": "observed_at_invalid"}
    observed_display, observed_datetime = observed
    return (
        {
            "listing_index": index,
            "listing_id": listing_id,
            "url": url,
            "price_rub": price,
            "condition": listing_condition,
            "city": listing_city,
            "observed_at": observed_display,
            "article_evidence_field": article_field,
            "_observed_datetime": observed_datetime,
        },
        None,
    )


def assess_avito_price_sample(
    *,
    part_number: str,
    listings: list[dict[str, Any]] | None,
    condition: str | None = None,
    city: str | None = None,
) -> dict[str, Any]:
    """Summarize supplied Avito listing leads without fetching or confirming offers.

    The median, when available, describes this one provider's sample only.
    It is never the independent-source market median from E9.
    """

    raw_part = _compact(part_number, limit=80)
    normalized_part = _article_key(raw_part)
    if (
        not isinstance(part_number, str)
        or len(part_number) > 80
        or not _PART_NUMBER.fullmatch(raw_part)
        or not 3 <= len(normalized_part) <= 48
        or contains_sensitive(raw_part)
    ):
        return {"ok": False, "schema": "AvitoPriceSampleV1", "error_code": "part_number_invalid"}
    if not isinstance(listings, list) or len(listings) > _MAX_LISTINGS:
        return {"ok": False, "schema": "AvitoPriceSampleV1", "error_code": "listings_invalid"}
    condition_filter = _condition_key(condition) if condition is not None else None
    if condition is not None and condition_filter is None:
        return {"ok": False, "schema": "AvitoPriceSampleV1", "error_code": "condition_invalid"}
    city_filter = _city_key(_compact(city, limit=120)) if city is not None else None
    if city is not None and (
        not isinstance(city, str) or not city_filter or len(city) > 120 or contains_sensitive(city)
    ):
        return {"ok": False, "schema": "AvitoPriceSampleV1", "error_code": "city_invalid"}

    candidates = []
    excluded = []
    for index, row in enumerate(listings):
        candidate, rejection = _candidate(
            row, index=index, part_number=normalized_part, condition=condition_filter, city=city_filter
        )
        if candidate is not None:
            candidates.append(candidate)
        elif rejection is not None:
            excluded.append(rejection)

    # A search row and a later detail read may describe the same ad. Keep the
    # newest valid observation; never increase the sample count for one ad.
    candidates.sort(key=lambda row: (row["_observed_datetime"], row["listing_index"]), reverse=True)
    selected = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    for row in candidates:
        if row["listing_id"] in seen_ids:
            excluded.append({"listing_index": row["listing_index"], "code": "duplicate_listing_id"})
            continue
        if row["url"] in seen_urls:
            excluded.append({"listing_index": row["listing_index"], "code": "duplicate_listing_url"})
            continue
        seen_ids.add(row["listing_id"])
        seen_urls.add(row["url"])
        selected.append(row)
    selected.sort(key=lambda row: row["listing_index"])
    excluded.sort(key=lambda row: row["listing_index"])

    by_segment: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in selected:
        by_segment.setdefault((row["condition"], _city_key(row["city"])), []).append(row)
    segments = []
    for key, rows in sorted(by_segment.items()):
        prices = sorted(row["price_rub"] for row in rows)
        sample_median = median(prices) if len(prices) >= 3 else None
        segments.append(
            {
                "condition": key[0],
                "city": rows[0]["city"],
                "sample_count": len(rows),
                "min_price_rub": prices[0],
                "max_price_rub": prices[-1],
                "sample_median_price_rub": sample_median,
                "sample_median_available": sample_median is not None,
            }
        )
    public_rows = [{key: value for key, value in row.items() if not key.startswith("_")} for row in selected]
    return {
        "ok": True,
        "schema": "AvitoPriceSampleV1",
        "read_only": True,
        "verification": "supplied_listing_data_only",
        "status": "sample_summary"
        if any(segment["sample_median_available"] for segment in segments)
        else "insufficient_sample"
        if selected
        else "no_valid_listings",
        "target": {"part_number": normalized_part, "condition": condition_filter, "city": city},
        "input_count": len(listings),
        "accepted_count": len(public_rows),
        "excluded_count": len(excluded),
        "accepted_listings": public_rows,
        "excluded_listings": excluded,
        "segments": segments,
        "independent_source_count": 1 if public_rows else 0,
        "independent_source_market_median_price_rub": None,
        "fitment_confirmed": False,
        "availability_confirmed": False,
        "warnings": [
            "Медиана, если показана, описывает только предоставленную выборку объявлений Авито, а не независимые рыночные источники.",
            "Охват и порядок объявлений зависят от переданной выборки; цена, наличие и применимость требуют подтверждения.",
        ],
    }


__all__ = ["assess_avito_price_sample"]
