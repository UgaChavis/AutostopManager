from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, unquote, urlparse
from urllib.request import Request, urlopen

from .j1_fetch import contains_sensitive

PUBLIC_RESEARCH_USER_AGENT = "AutostopManager/0.1 public labor research"
PUBLIC_RESEARCH_TIMEOUT_SECONDS = 4
MAX_PUBLIC_SEARCHES = 4
LABOR_ONLY_MARKERS = (
    "без запчаст",
    "без учета запчаст",
    "запчасти не включ",
    "работа без материал",
    "материалы не включ",
    "только работа",
    "только за работу",
)
PARTS_INCLUDED_MARKERS = (
    "включая запчаст",
    "не только работа",
    "работа и запчаст",
    "работа плюс запчаст",
    "работа + запчаст",
    "работа с запчаст",
    "с запчастями",
    "с материалами",
)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _parse_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = _clean_text(value).casefold()
    if text in {"1", "true", "yes", "y", "да", "истина", "включая", "included"}:
        return True
    if text in {"0", "false", "no", "n", "нет", "ложь", "не включая", "excluded"}:
        return False
    return None


def _strip_html(value: str) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", value, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(_clean_text(text))


def _safe_source_from_url(url: str) -> str:
    if "uddg=" in url:
        match = re.search(r"[?&]uddg=([^&]+)", url)
        if match:
            return _safe_source_from_url(unquote(match.group(1)))
    parsed = urlparse(url)
    if parsed.netloc:
        return parsed.netloc.removeprefix("www.")
    return "public_web_search"


def _ddg_search(query: str, *, timeout_seconds: float = PUBLIC_RESEARCH_TIMEOUT_SECONDS) -> dict[str, Any]:
    if contains_sensitive(query):
        raise ValueError("sensitive_public_research_query")
    url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
    request = Request(url, headers={"User-Agent": PUBLIC_RESEARCH_USER_AGENT})
    with urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read(250_000).decode("utf-8", "ignore")
    results: list[dict[str, str]] = []
    blocks = re.findall(r"<div class=\"result(?: results_links)?\".*?</div>\s*</div>", raw, flags=re.I | re.S)
    if not blocks:
        blocks = re.findall(
            r"<a rel=\"nofollow\" class=\"result__a\".*?</a>.*?(?:<a class=\"result__snippet\".*?</a>|</div>)",
            raw,
            flags=re.I | re.S,
        )
    for block in blocks[:5]:
        link_match = re.search(r"class=\"result__a\"[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", block, flags=re.I | re.S)
        snippet_match = re.search(r"class=\"result__snippet\"[^>]*>(.*?)</a>", block, flags=re.I | re.S)
        if not link_match:
            continue
        url_value = html.unescape(link_match.group(1))
        title = _strip_html(link_match.group(2))
        snippet = _strip_html(snippet_match.group(1) if snippet_match else block)
        results.append(
            {
                "source": _safe_source_from_url(url_value),
                "title": title[:140],
                "url": url_value,
                "snippet": snippet[:240],
            }
        )
    return {"query": query, "url": url, "results": results}


def build_public_research_queries(
    *,
    vehicle_context: dict[str, Any],
    operations: list[dict[str, Any]],
    city: str,
) -> dict[str, list[str]]:
    # Keep distinct metadata fields from becoming one apparent split VIN.
    vehicle_bits = ", ".join(
        _clean_text(vehicle_context.get(key))
        for key in ("make", "model", "year", "engine", "transmission", "vehicle")
        if _clean_text(vehicle_context.get(key))
    )
    if not vehicle_bits:
        vehicle_bits = _clean_text(vehicle_context.get("vehicle_class")) or "легковой автомобиль"

    price_queries: list[str] = []
    labor_time_queries: list[str] = []
    for operation in operations:
        name = _clean_text(operation.get("normalized_name") or operation.get("input"))
        if not name:
            continue
        price_queries.extend(
            [
                f"{name} {vehicle_bits} стоимость работы без запчастей СТО Россия",
                f"{name} прайс СТО работа {city}",
            ]
        )
        labor_time_queries.extend(
            [
                f"{name} {vehicle_bits} нормо часы",
                f"{name} норма времени трудоемкость снять установить",
            ]
        )
    return {
        "labor_prices": _dedupe([query for query in price_queries if not contains_sensitive(query)]),
        "labor_times": _dedupe([query for query in labor_time_queries if not contains_sensitive(query)]),
    }


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _prices_from_text(text: str) -> list[int]:
    values: list[int] = []
    for match in re.finditer(
        r"(?<![\d,.])((?:[-+−]\s*)?(?:\d{3,6}(?:[ .]\d{3})?|\d{1,3}(?:[ .]\d{3})+))\s*(?:₽|руб|р\.)",
        text,
        flags=re.I,
    ):
        raw = match.group(1).replace(" ", "").replace(".", "").replace("−", "-")
        try:
            price = int(raw)
        except ValueError:
            continue
        if 300 <= price <= 250_000:
            values.append(price)
    return values[:3]


def _labor_only_flags(text: str) -> tuple[bool | None, bool]:
    normalized = _clean_text(text).casefold()
    if any(marker in normalized for marker in PARTS_INCLUDED_MARKERS):
        return True, False
    if any(marker in normalized for marker in LABOR_ONLY_MARKERS):
        return False, True
    return None, False


def _hours_from_text(text: str) -> list[tuple[float, float]]:
    values: list[tuple[float, float]] = []
    decimal = r"(?<![\d,.])(?:[-+−]\s*)?\d+(?:[,.]\d+)?(?![\d,.])"
    patterns = [
        rf"({decimal})\s*[-–]\s*({decimal})\s*(?:н/?ч|нормо[- ]?час|час|ч\.)",
        rf"(?:н/?ч|нормо[- ]?час|норма времени|трудоемкость|время выполнения)[^\d+−-]{{0,24}}({decimal})",
        rf"({decimal})\s*(?:н/?ч|нормо[- ]?час|час(?:а|ов)?|ч\.)",
    ]
    range_spans: list[tuple[int, int]] = []
    observed_spans: list[tuple[int, int]] = []
    for pattern_index, pattern in enumerate(patterns):
        for match in re.finditer(pattern, text, flags=re.I):
            if pattern_index == 0:
                range_spans.append(match.span())
            elif any(match.start() < end and match.end() > start for start, end in range_spans):
                continue
            if any(match.start() < end and match.end() > start for start, end in observed_spans):
                continue
            try:
                if len(match.groups()) >= 2 and match.group(2):
                    start = float(re.sub(r"\s+", "", match.group(1)).replace(",", ".").replace("−", "-"))
                    end = float(re.sub(r"\s+", "", match.group(2)).replace(",", ".").replace("−", "-"))
                else:
                    start = end = float(re.sub(r"\s+", "", match.group(1)).replace(",", ".").replace("−", "-"))
            except (TypeError, ValueError):
                continue
            if 0 < start <= end <= 80:
                observed_spans.append(match.span())
                values.append((round(start, 2), round(end, 2)))
    return values[:3]


def _observed_city(snippet: str) -> str:
    cities = []
    if re.search(r"\bкрасноярск\w*", snippet, re.I):
        cities.append("Красноярск")
    if re.search(r"\b(?:санкт[- ]петербург\w*|спб)\b", snippet, re.I):
        cities.append("Санкт-Петербург")
    return cities[0] if len(cities) == 1 else ""


def parse_public_work_evidence(
    row: dict[str, Any], query: str, operations: list[dict[str, Any]], kind: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Bind amounts to observed operation segments, never to search intent."""
    snippet = " ".join(_clean_text(row.get(key)) for key in ("title", "snippet"))
    observed_at = datetime.now(UTC).isoformat()
    source = {
        "provider": "public_web",
        "primary_lineage": row.get("source") or _safe_source_from_url(str(row.get("url") or "")),
        "locator": row.get("url"),
        "scope": "public_price_snippet" if kind == "labor_prices" else "public_labor_time_snippet",
        "method": "public_search",
        "fetched_at": observed_at,
    }
    common = {
        "source": source,
        "city": _observed_city(snippet),
        "captured_at": observed_at,
        "confidence": "low",
        "capture_method": "public_search_snippet",
        "public_source": True,
        "official": False,
        "evidence": snippet[:180],
    }
    rows: list[dict[str, Any]] = []
    title_operation = _title_operation_for_caption(
        str(row.get("title") or ""), str(row.get("snippet") or ""), operations, kind
    )
    # The title can supply scope only for an unambiguous measurement caption.
    for field in ("title", "snippet"):
        for segment in _observed_operation_segments(str(row.get(field) or ""), operations):
            items: list[dict[str, Any]]
            if kind == "labor_prices":
                items = [{"price_rub": price} for price in dict.fromkeys(_prices_from_text(segment))]
            else:
                items = [
                    {"hours": round((low + high) / 2, 2), "range_hours": [low, high]}
                    for low, high in dict.fromkeys(_hours_from_text(segment))
                ]
            operation_name = _observed_operation_name(segment, operations) if len(items) == 1 else ""
            inherited_title = field == "snippet" and not operation_name and len(items) == 1 and title_operation
            if inherited_title:
                operation_name = title_operation
            if kind == "labor_prices":
                flags_text = (
                    str(row.get("snippet") or "") if inherited_title else segment if operation_name else snippet
                )
                includes_parts, labor_only = _labor_only_flags(flags_text)
                for item in items:
                    item.update(includes_parts=includes_parts, labor_only=labor_only)
            rows.extend({**common, "operation_name": operation_name, **item} for item in items)
    return rows, source


def legacy_work_evidence_row(row: dict[str, Any]) -> dict[str, Any]:
    """Keep structured provenance while adapting the legacy source-string contract."""
    source = row.get("source")
    if not isinstance(source, dict):
        return row
    return {
        **row,
        "source": source.get("primary_lineage") or source.get("provider") or "",
        "evidence_source": source,
        "captured_at": row.get("captured_at") or source.get("fetched_at"),
        "public_source": row.get("public_source", source.get("provider") == "public_web"),
    }


def collect_public_work_pricing_research(
    *,
    vehicle_context: dict[str, Any],
    operations: list[dict[str, Any]],
    city: str,
    auto_research: bool,
    labor_time_policy: str = "public_only",
    timeout_seconds: int = PUBLIC_RESEARCH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    queries = build_public_research_queries(vehicle_context=vehicle_context, operations=operations, city=city)
    result: dict[str, Any] = {
        "enabled": bool(auto_research),
        "policy": labor_time_policy,
        "access_mode": "public_web_only",
        "search_queries": queries,
        "quotes": [],
        "labor_time_sample": [],
        "sources_checked": [],
        "warnings": [],
    }
    if not auto_research:
        result["sources_checked"].append(
            {
                "source_id": "public_web_search",
                "status": "disabled",
                "reason": "auto_research_false",
            }
        )
        return result
    if labor_time_policy != "public_only":
        result["warnings"].append("Only public_only labor-time policy is supported in this implementation.")

    searches = 0
    for query_type, query_list in (("labor_prices", queries["labor_prices"]), ("labor_times", queries["labor_times"])):
        for query in query_list:
            if searches >= MAX_PUBLIC_SEARCHES:
                break
            searches += 1
            checked: dict[str, Any] = {
                "source_id": "duckduckgo_html",
                "query_type": query_type,
                "query": query,
                "status": "ok",
            }
            try:
                search = _ddg_search(query, timeout_seconds=timeout_seconds)
            except (
                HTTPError,
                URLError,
                TimeoutError,
                ValueError,
                OSError,
            ) as exc:  # pragma: no cover - network-dependent safety rail
                checked["status"] = "error"
                checked["error"] = type(exc).__name__
                result["sources_checked"].append(checked)
                continue

            rows_found = 0
            for row in search["results"]:
                parsed, _ = parse_public_work_evidence(row, query, operations, query_type)
                target = "quotes" if query_type == "labor_prices" else "labor_time_sample"
                result[target].extend(legacy_work_evidence_row(item) for item in parsed)
                rows_found += len(parsed)
            checked["rows_found"] = rows_found
            result["sources_checked"].append(checked)

    result["quotes"] = _dedupe_quote_rows(result["quotes"])
    result["labor_time_sample"] = _dedupe_rows(result["labor_time_sample"], ("source", "operation_name", "hours"))
    if not result["labor_time_sample"]:
        result["warnings"].append("Public labor-time mentions were not found automatically.")
    if not any(quote.get("labor_only") is True and quote.get("includes_parts") is False for quote in result["quotes"]):
        result["warnings"].append("Public labor-only price quotes were not found automatically.")
    return result


def _observed_operation_segments(text: str, operations: list[dict[str, Any]]) -> list[str]:
    boundaries = {0, len(text)}
    boundaries.update(
        match.end() for match in re.finditer(r"(?<!руб\.)(?<!р\.)(?<!ч\.)(?<=[.!?;])\s+|[\r\n]+", text, flags=re.I)
    )
    markers = list(re.finditer(r"(?<!\w)(?:замена|ремонт|диагностика|снятие|установка)\s", text, flags=re.I))
    if len(markers) > 1:
        observed = _observed_operation_matches(text, operations)
        boundaries.update(
            match.start() for match in markers if not any(start < match.start() < end for start, end, _ in observed)
        )
    positions = sorted(boundaries)
    return [_clean_text(text[start:end]) for start, end in pairwise(positions) if _clean_text(text[start:end])]


def _observed_operation_matches(text: str, operations: list[dict[str, Any]]) -> list[tuple[int, int, str]]:
    matches: list[tuple[int, int, str]] = []
    for operation in operations:
        name = _clean_text(operation.get("normalized_name") or operation.get("input"))
        if not name:
            continue
        aliases = operation.get("aliases")
        labels = [name, _clean_text(operation.get("input"))]
        if isinstance(aliases, (list, tuple)):
            labels.extend(_clean_text(alias) for alias in aliases)
        for label in set(labels) - {""}:
            for match in re.finditer(r"(?<!\w)" + re.escape(label) + r"(?!\w)", text, flags=re.I):
                matches.append((match.start(), match.end(), name))
    return matches


def _observed_operation_name(text: str, operations: list[dict[str, Any]]) -> str:
    matches = _observed_operation_matches(text, operations)
    names = {
        name
        for start, end, name in matches
        if not any(
            other_start <= start and end <= other_end and (start, end) != (other_start, other_end)
            for other_start, other_end, _ in matches
        )
    }
    return names.pop() if len(names) == 1 else ""


def _title_operation_for_caption(title: str, snippet: str, operations: list[dict[str, Any]], kind: str) -> str:
    name = _observed_operation_name(title, operations)
    if not name:
        return ""
    extract = _prices_from_text if kind == "labor_prices" else _hours_from_text
    if extract(title) or len(extract(snippet)) != 1:
        return ""
    return name if all(_caption_is_metadata(text, operations, name) for text in (title, snippet)) else ""


def _caption_is_metadata(text: str, operations: list[dict[str, Any]], name: str) -> bool:
    characters = list(text)
    for start, end, observed in _observed_operation_matches(text, operations):
        if observed == name:
            characters[start:end] = [" "] * (end - start)
    # A subject such as "свечи" is not price/time metadata, even without an operation verb.
    caption = re.sub(r"[-+−]?\d+(?:[ ,.\-–]\d+)*", " ", "".join(characters).casefold())
    caption = re.sub(
        r"(?<!\w)(?:красноярск\w*|санкт[- ]петербург\w*|спб|только|за|работ(?:а|ы|у)?|без|учета|запчаст\w*|материал\w*|не|включ\w*|и|плюс|с|стоимость|цена|от|до|руб\w*|р|н|ч|нормо[- ]?час\w*|час\w*|норма|норматив|времени|трудоемкость|время|выполнения)(?!\w)",
        " ",
        caption,
    )
    return not re.search(r"[^\W\d_]", caption)


def _observation_location(row: dict[str, Any]) -> tuple[str, str]:
    supplied = row.get("evidence_source")
    if not isinstance(supplied, dict):
        supplied = row.get("source")
    source = supplied if isinstance(supplied, dict) else {}
    city = str(row.get("city") or row.get("city_region") or row.get("region") or "").strip().casefold()
    locator = str(row.get("locator") or row.get("url") or source.get("locator") or "").strip()
    return city, locator


def _dedupe_rows(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    seen: set[tuple[str, ...]] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        key = tuple(str(row.get(field, "")).casefold() for field in fields)
        key += _observation_location(row)
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _dedupe_quote_rows(
    rows: list[dict[str, Any]], *, identities: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    fields = ("source", "operation_name", "price_rub")
    by_key: dict[tuple[str, ...], dict[str, Any]] = {}
    for row, identity in zip(rows, identities if identities is not None else rows, strict=True):
        row = dict(row)
        for field in ("includes_parts", "labor_only"):
            if field in row:
                row[field] = _parse_bool(row[field])
        key = tuple(str(identity.get(field, "")).casefold() for field in fields)
        key += _observation_location(row)
        existing = by_key.get(key)
        if existing is None:
            existing = dict(row)
            by_key[key] = existing
            continue
        if existing.get("includes_parts") is True or row.get("includes_parts") is True:
            existing["includes_parts"] = True
            existing["labor_only"] = False
        elif (
            existing.get("labor_only") is True
            or row.get("labor_only") is True
            or existing.get("includes_parts") is False
            or row.get("includes_parts") is False
        ):
            existing["includes_parts"] = False
            existing["labor_only"] = True
        else:
            existing["includes_parts"] = None
            existing["labor_only"] = False
    return list(by_key.values())
