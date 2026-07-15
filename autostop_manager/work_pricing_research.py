from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, unquote, urlparse
from urllib.request import Request, urlopen


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


def _ddg_search(query: str, *, timeout_seconds: int = PUBLIC_RESEARCH_TIMEOUT_SECONDS) -> dict[str, Any]:
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
    vehicle_bits = " ".join(
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
        "labor_prices": _dedupe(price_queries),
        "labor_times": _dedupe(labor_time_queries),
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
    for match in re.finditer(r"(?<!\d)(\d{3,6}(?:[ .]\d{3})?|\d{1,3}(?:[ .]\d{3})+)\s*(?:₽|руб|р\.)", text, flags=re.I):
        raw = match.group(1).replace(" ", "").replace(".", "")
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
    decimal = r"\d{1,2}(?:[,.]\d{1,2})?"
    patterns = [
        rf"({decimal})\s*[-–]\s*({decimal})\s*(?:н/?ч|нормо[- ]?час|час|ч\.)",
        rf"(?:н/?ч|нормо[- ]?час|норма времени|трудоемкость|время выполнения)[^\d]{{0,24}}({decimal})",
        rf"({decimal})\s*(?:н/?ч|нормо[- ]?час|час(?:а|ов)?|ч\.)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            try:
                if len(match.groups()) >= 2 and match.group(2):
                    start = float(match.group(1).replace(",", "."))
                    end = float(match.group(2).replace(",", "."))
                else:
                    start = end = float(match.group(1).replace(",", "."))
            except (TypeError, ValueError):
                continue
            if 0 < start <= end <= 80:
                values.append((round(start, 2), round(end, 2)))
    return values[:3]


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

    today = datetime.now(UTC).date().isoformat()
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
            ) as exc:  # pragma: no cover - network-dependent safety rail
                checked["status"] = "error"
                checked["error"] = type(exc).__name__
                result["sources_checked"].append(checked)
                continue

            rows_found = 0
            for row in search["results"]:
                snippet = " ".join(part for part in (row.get("title"), row.get("snippet")) if part)
                if query_type == "labor_prices":
                    includes_parts, labor_only = _labor_only_flags(snippet)
                    for price in _prices_from_text(snippet):
                        result["quotes"].append(
                            {
                                "source": row.get("source") or "public_web_search",
                                "city": "",
                                "operation_name": _operation_name_for_query(query, operations),
                                "price_rub": price,
                                "includes_parts": includes_parts,
                                "labor_only": labor_only,
                                "captured_at": today,
                                "confidence": "low",
                                "capture_method": "public_search_snippet",
                            }
                        )
                        rows_found += 1
                else:
                    for start, end in _hours_from_text(snippet):
                        hours = round((start + end) / 2, 2)
                        result["labor_time_sample"].append(
                            {
                                "source": row.get("source") or "public_web_search",
                                "operation_name": _operation_name_for_query(query, operations),
                                "hours": hours,
                                "range_hours": [start, end],
                                "captured_at": today,
                                "confidence": "low",
                                "public_source": True,
                                "official": False,
                                "capture_method": "public_search_snippet",
                                "evidence": snippet[:180],
                            }
                        )
                        rows_found += 1
            checked["rows_found"] = rows_found
            result["sources_checked"].append(checked)

    result["quotes"] = _dedupe_quote_rows(result["quotes"])
    result["labor_time_sample"] = _dedupe_rows(result["labor_time_sample"], ("source", "operation_name", "hours"))
    if not result["labor_time_sample"]:
        result["warnings"].append("Public labor-time mentions were not found automatically.")
    if not any(quote.get("labor_only") is True and quote.get("includes_parts") is False for quote in result["quotes"]):
        result["warnings"].append("Public labor-only price quotes were not found automatically.")
    return result


def _operation_name_for_query(query: str, operations: list[dict[str, Any]]) -> str:
    query_text = query.casefold()
    for operation in operations:
        name = _clean_text(operation.get("normalized_name") or operation.get("input"))
        if name and name.casefold() in query_text:
            return name
    return _clean_text(operations[0].get("normalized_name") or operations[0].get("input")) if operations else ""


def _dedupe_rows(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    seen: set[tuple[str, ...]] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        key = tuple(str(row.get(field, "")).casefold() for field in fields)
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _dedupe_quote_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = ("source", "operation_name", "price_rub")
    by_key: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(str(row.get(field, "")).casefold() for field in fields)
        existing = by_key.get(key)
        if existing is None:
            existing = dict(row)
            by_key[key] = existing
            continue
        if existing.get("includes_parts") is True or row.get("includes_parts") is True:
            existing["includes_parts"] = True
            existing["labor_only"] = False
        elif existing.get("labor_only") is True or row.get("labor_only") is True:
            existing["includes_parts"] = False
            existing["labor_only"] = True
        else:
            existing["includes_parts"] = None
            existing["labor_only"] = False
    return list(by_key.values())
