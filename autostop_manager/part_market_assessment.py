"""Read-only, evidence-gated assessment of public part-market observations."""

from __future__ import annotations

import ipaddress
import re
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, date, datetime
from statistics import median
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_MAX_OBSERVATIONS = 60
_MAX_PRICE_RUB = 10_000_000
_CURRENT_OBSERVATION_MAX_AGE_DAYS = 7
_ARTICLE_TOKEN = re.compile(r"[^\W_][\w._/\\-]*", re.UNICODE)
_PRICE_IN_RUB = re.compile(
    r"(?<!\d)(\d{1,3}(?:[\s\u00a0]\d{3})+|\d{1,9})\s*(?:₽|руб(?:\.|лей|ля)?|р\.?|rub)(?=$|[\s,.;:])",
    re.IGNORECASE,
)
_DOMAIN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.IGNORECASE)
_VIN_LIKE = re.compile(r"(?<![A-HJ-NPR-Z0-9])(?:[A-HJ-NPR-Z0-9][ ._/\\-]?){17}(?![A-HJ-NPR-Z0-9])", re.I)
_KIND_ALIASES = {
    "original": "original",
    "оригинал": "original",
    "oem": "original",
    "analog": "analog",
    "аналог": "analog",
    "aftermarket": "analog",
}
_CONDITION_ALIASES = {
    "new": "new",
    "новый": "new",
    "новая": "new",
    "новое": "new",
    "used": "used",
    "б/у": "used",
    "бу": "used",
    "unknown": "unknown",
    "неизвестно": "unknown",
}
_CONDITION_MARKERS = {
    "new": ("новый", "новая", "новое", "new", "не использ"),
    "used": ("б/у", " бу ", "used", "с разборки", "контрактн"),
}
_SEGMENT_ORDER = tuple(
    (kind, condition, region_scope)
    for kind in ("original", "analog")
    for condition in ("new", "used", "unknown")
    for region_scope in ("krasnoyarsk", "rf")
)


def _compact(value: Any, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _normalize_article(value: Any) -> str:
    text = _compact(value, limit=80).upper()
    normalized = "".join(character for character in text if character.isalnum())
    return normalized if 3 <= len(normalized) <= 48 else ""


def _normalize_brand(value: Any) -> str:
    normalized = "".join(character for character in _compact(value, limit=80).upper() if character.isalnum())
    return normalized if 2 <= len(normalized) <= 48 else ""


def _contains_article(excerpt: str, article: str) -> bool:
    return any(_normalize_article(token) == article for token in _ARTICLE_TOKEN.findall(excerpt))


def _contains_brand(excerpt: str, raw_brand: Any) -> bool:
    tokens = re.findall(r"[^\W_]+", _compact(raw_brand, limit=80), re.UNICODE)
    if not tokens:
        return False
    pattern = r"(?<![^\W_])" + r"[\s._/\\-]*".join(re.escape(token) for token in tokens) + r"(?![^\W_])"
    return re.search(pattern, excerpt, re.IGNORECASE) is not None


def _prices_in_excerpt(excerpt: str) -> set[int]:
    prices: set[int] = set()
    for match in _PRICE_IN_RUB.finditer(excerpt):
        numeric = re.sub(r"\D", "", match.group(1))
        if numeric:
            prices.add(int(numeric))
    return prices


def _price(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not number.is_integer() or not 1 <= number <= _MAX_PRICE_RUB:
        return None
    return int(number)


def _public_source_url(value: Any) -> tuple[str, str] | None:
    raw = _compact(value, limit=2048)
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").casefold().rstrip(".")
    except ValueError:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or (address is not None and not address.is_global)
        or (address is None and not _DOMAIN.fullmatch(host))
    ):
        return None
    source_key = host[4:] if host.startswith("www.") else host
    authority = f"[{host}]" if address is not None and address.version == 6 else host
    if port is not None:
        authority = f"{authority}:{port}"
    return urlunsplit(("https", authority, parsed.path or "/", "", "")), source_key


def _date_value(value: Any) -> date | None:
    raw = _compact(value, limit=10)
    try:
        value_date = date.fromisoformat(raw)
    except ValueError:
        return None
    return value_date if value_date <= datetime.now(UTC).date() else None


def _observed_freshness(value: Any) -> tuple[str, int, str] | None:
    observed = _date_value(value)
    if observed is None:
        return None
    age_days = (datetime.now(UTC).date() - observed).days
    return (
        observed.isoformat(),
        age_days,
        "stale" if age_days > _CURRENT_OBSERVATION_MAX_AGE_DAYS else "recent",
    )


def _publication_freshness(value: Any) -> tuple[str | None, int | None, str] | None:
    raw = _compact(value, limit=10)
    if not raw:
        return None, None, "publication_date_missing"
    published = _date_value(raw)
    if published is None:
        return None
    age_days = (datetime.now(UTC).date() - published).days
    return published.isoformat(), age_days, "stale" if age_days > 30 else "fresh"


def _kind(value: Any) -> str:
    return _KIND_ALIASES.get(_compact(value, limit=30).casefold(), "")


def _condition(value: Any) -> str:
    return _CONDITION_ALIASES.get(_compact(value, limit=30).casefold(), "")


def _condition_supported(excerpt: str, condition: str) -> bool:
    if condition == "unknown":
        return True
    folded = f" {excerpt.casefold()} "
    return any(marker in folded for marker in _CONDITION_MARKERS[condition])


def _region_scope(value: Any, target_region: str) -> str:
    region = _compact(value, limit=100).casefold()
    target = _compact(target_region, limit=100).casefold()
    if target and target in region:
        return "krasnoyarsk"
    if any(marker in region for marker in ("россия", "российская федерация", "рф", "russia")):
        return "rf"
    return ""


def _reject(index: int, code: str) -> dict[str, Any]:
    return {"observation_index": index, "code": code}


def _validated_observation(
    row: Any,
    *,
    index: int,
    target_article: str,
    target_brand: str,
    target_region: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(row, Mapping):
        return None, _reject(index, "observation_not_object")
    article = _normalize_article(row.get("article"))
    brand = _normalize_brand(row.get("brand"))
    source = _compact(row.get("source"), limit=120)
    excerpt = _VIN_LIKE.sub("[redacted]", _compact(row.get("source_excerpt"), limit=1200))
    url = _public_source_url(row.get("url"))
    observed = _observed_freshness(row.get("observed_at"))
    publication = _publication_freshness(row.get("published_at"))
    price_rub = _price(row.get("price_rub"))
    kind = _kind(row.get("offer_kind"))
    condition = _condition(row.get("condition"))
    region_scope = _region_scope(row.get("region"), target_region)
    if not all((article, brand, source, excerpt, url, observed, publication, price_rub, kind, condition, region_scope)):
        return None, _reject(index, "observation_fields_invalid")
    if not _contains_article(excerpt, article):
        return None, _reject(index, "article_not_in_source_excerpt")
    if not _contains_brand(excerpt, row.get("brand")):
        return None, _reject(index, "brand_not_in_source_excerpt")
    if price_rub not in _prices_in_excerpt(excerpt):
        return None, _reject(index, "price_not_in_source_excerpt")
    if not _condition_supported(excerpt, condition):
        return None, _reject(index, "condition_not_in_source_excerpt")
    if kind == "original" and (article != target_article or (target_brand and brand != target_brand)):
        return None, _reject(index, "original_not_exact_target_match")
    if kind == "analog" and (article == target_article or not _contains_article(excerpt, target_article)):
        return None, _reject(index, "analog_target_cross_reference_not_in_source_excerpt")
    safe_url, source_key = url
    observed_at, observed_age_days, observation_freshness = observed
    published_at, page_age_days, price_freshness = publication
    return (
        {
            "observation_index": index,
            "article": article,
            "brand": brand,
            "price_rub": price_rub,
            "source": source,
            "source_key": source_key,
            "url": safe_url,
            "observed_at": observed_at,
            "observed_age_days": observed_age_days,
            "observation_freshness": observation_freshness,
            "published_at": published_at,
            "page_age_days": page_age_days,
            "price_freshness": price_freshness,
            "offer_kind": kind,
            "condition": condition,
            "region_scope": region_scope,
            "source_excerpt": excerpt[:400],
        },
        None,
    )


def _median_or_none(values: list[int]) -> int | float | None:
    if len(values) < 3:
        return None
    result = median(values)
    return int(result) if isinstance(result, float) and result.is_integer() else result


def _segments(offers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_segment: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for offer in offers:
        by_segment[(offer["offer_kind"], offer["condition"], offer["region_scope"])].append(offer)
    result: list[dict[str, Any]] = []
    for kind, condition, region_scope in _SEGMENT_ORDER:
        segment_offers = by_segment[(kind, condition, region_scope)]
        current_offers = [
            offer for offer in segment_offers if condition != "unknown" and offer["price_freshness"] != "stale"
        ]
        prices = sorted(int(offer["price_rub"]) for offer in current_offers)
        median_eligible = condition != "unknown"
        median_price = _median_or_none(prices) if median_eligible else None
        freshness = {(str(offer["price_freshness"]), str(offer["observation_freshness"])) for offer in current_offers}
        exclusion_reasons: dict[str, int] = {}
        if condition == "unknown" and segment_offers:
            exclusion_reasons["unknown_condition"] = len(segment_offers)
        stale_page_count = sum(offer["price_freshness"] == "stale" for offer in segment_offers)
        if stale_page_count:
            exclusion_reasons["stale_published_page"] = stale_page_count
        result.append(
            {
                "offer_kind": kind,
                "condition": condition,
                "region_scope": region_scope,
                "independent_offer_count": len(segment_offers),
                "median_eligible": median_eligible,
                "median_input_offer_count": len(current_offers),
                "excluded_from_current_median_count": len(segment_offers) - len(current_offers),
                "median_exclusion_reasons": exclusion_reasons,
                "median_price_rub": median_price,
                "median_available": median_price is not None,
                "median_confidence": (
                    "unavailable" if median_price is None else "medium" if freshness == {("fresh", "recent")} else "low"
                ),
                "min_price_rub": prices[0] if prices else None,
                "max_price_rub": prices[-1] if prices else None,
                "offers": segment_offers,
            }
        )
    return result


def assess_part_market(
    *,
    article: str,
    observations: list[dict[str, Any]] | None,
    brand: str | None = None,
    target_region: str = "Красноярск",
) -> dict[str, Any]:
    """Assess supplied public offers only; it never searches, writes, or confirms fitment."""

    target_article = _normalize_article(article)
    target_brand = _normalize_brand(brand) if brand else ""
    safe_target_region = _compact(target_region, limit=100)
    if (
        not target_article
        or not safe_target_region
        or (brand is not None and _compact(brand, limit=80) and not target_brand)
    ):
        return {"ok": False, "schema": "PartMarketAssessmentV1", "error_code": "market_target_invalid"}
    if not isinstance(observations, list) or not observations or len(observations) > _MAX_OBSERVATIONS:
        return {"ok": False, "schema": "PartMarketAssessmentV1", "error_code": "market_observations_invalid"}

    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, row in enumerate(observations):
        candidate, rejection = _validated_observation(
            row,
            index=index,
            target_article=target_article,
            target_brand=target_brand,
            target_region=safe_target_region,
        )
        if candidate is not None:
            candidates.append(candidate)
        elif rejection is not None:
            rejected.append(rejection)

    selected: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        grouped[
            (candidate["offer_kind"], candidate["condition"], candidate["region_scope"], candidate["source_key"])
        ].append(candidate)
    for rows in grouped.values():
        rows.sort(key=lambda row: (row["observed_at"], -int(row["observation_index"])), reverse=True)
        selected.append(rows[0])
        rejected.extend(_reject(int(row["observation_index"]), "duplicate_source_in_segment") for row in rows[1:])
    selected.sort(key=lambda row: int(row["observation_index"]))
    rejected.sort(key=lambda row: int(row["observation_index"]))
    segments = _segments(selected)
    median_count = sum(1 for segment in segments if segment["median_available"])
    return {
        "ok": bool(selected),
        "schema": "PartMarketAssessmentV1",
        "read_only": True,
        "target": {"article": target_article, "brand": target_brand or None, "target_region": safe_target_region},
        "status": "assessed"
        if median_count
        else "insufficient_independent_offers"
        if selected
        else "no_valid_public_evidence",
        "accepted_offer_count": len(selected),
        "rejected_observation_count": len(rejected),
        "segments": segments,
        "rejected_observations": rejected,
        "warnings": [
            "Медиана показана только при трёх независимых источниках в одном сегменте.",
            "Оценка не подтверждает применимость детали, наличие, закупочную стоимость или цену продажи.",
        ],
    }


__all__ = ["assess_part_market"]
