from __future__ import annotations

import re
from bisect import bisect_left
from datetime import UTC, datetime
from html.parser import HTMLParser
from itertools import pairwise
from typing import Any
from unicodedata import category
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, unquote, urlparse
from urllib.request import Request

from .bounded_http_read import BufferedResponse, urlopen as bounded_urlopen
from .j1_fetch import contains_sensitive
from .work_operation_matching import OperationMatcher

PUBLIC_RESEARCH_USER_AGENT = "AutostopManager/0.1 public labor research"
PUBLIC_RESEARCH_TIMEOUT_SECONDS = 4
MAX_PUBLIC_SEARCHES = 4
MAX_PUBLIC_RESPONSE_BYTES = 250_000
_PRICE_PATTERN = r"(?<![\d,.])((?:[-+−]\s*)?(?:\d{3,6}(?:[ .]\d{3})?|\d{1,3}(?:[ .]\d{3})+))\s*(?:₽|руб|р\.)"
_DECIMAL_PATTERN = r"(?<![\d,.])(?:[-+−]\s*)?\d+(?:[,.]\d+)?(?!\d|[,.][\d,.])"
_HOUR_PATTERNS = (
    rf"({_DECIMAL_PATTERN})\s*[-–]\s*({_DECIMAL_PATTERN})\s*(?:н/?ч|нормо[- ]?час|час|ч\.)",
    rf"(?:н/?ч|нормо[- ]?час|норма времени|трудоемкость|время выполнения)[^\d+−-]{{0,24}}({_DECIMAL_PATTERN})",
    rf"({_DECIMAL_PATTERN})\s*(?:н/?ч|нормо[- ]?час|час(?:а|ов)?|ч\.)",
)
_ObservationSignature = tuple[int, int, tuple[bool | None, bool]]
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


def _source_identity(value: Any, *, normalize_whitespace: bool = False) -> str:
    """Preserve URI labels and retain the caller's ordinary-label whitespace policy."""
    source = str(value)
    stripped = source.strip()
    if re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", stripped):
        return stripped
    return re.sub(r"\s+", " ", source.casefold()).strip() if normalize_whitespace else source.casefold()


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


def _safe_source_from_url(url: str) -> str:
    # A provider-controlled locator must not recurse or decode indefinitely.
    for _ in range(8):
        match = re.search(r"[?&]uddg=([^&]+)", url)
        if not match:
            break
        url = unquote(match.group(1))
    else:
        return "public_web_search"
    try:
        parsed = urlparse(url)
    except ValueError:
        return "public_web_search"
    if parsed.netloc:
        return parsed.netloc[4:] if parsed.netloc[:4].casefold() == "www." else parsed.netloc
    return "public_web_search"


class _SearchResultsParser(HTMLParser):
    """Extract complete links and their own snippets from a bounded HTML prefix."""

    _VOID_TAGS = frozenset(
        {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    )
    _INERT_TAGS = frozenset({"script", "style", "template", "textarea", "noscript", "head", "iframe", "svg", "math"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._stack: list[str] = []
        self._positions: dict[str, list[int]] = {}
        self._container: int | None = None
        self._hidden: int | None = None
        self._row: dict[str, str] | None = None
        self._capture: tuple[str, int] | None = None
        self._text: list[str] = []

    def _end_result(self) -> None:
        self._row = None
        self._capture = None
        self._text = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        depth = len(self._stack)
        if tag not in self._VOID_TAGS:
            self._positions.setdefault(tag, []).append(depth)
            self._stack.append(tag)
        if self._hidden is not None:
            return
        values: dict[str, str | None] = {}
        for key, value in attrs:
            values.setdefault(key, value)
        if tag in self._INERT_TAGS or "hidden" in values:
            if self._capture:
                self._text.append(" ")
            if tag not in self._VOID_TAGS:
                self._hidden = depth
            return
        classes = (values.get("class") or "").split()
        if "result" in classes:
            self._end_result()
            self._container = depth if tag not in self._VOID_TAGS else None
        # Nested anchors cannot complete a title or an anchor-shaped snippet.
        # Links inside a div/span snippet remain ordinary snippet content.
        if tag == "a" and self._capture and self._stack[self._capture[1]] == "a":
            self._end_result()
        if tag == "a" and "result__a" in classes:
            self._end_result()
            href = values.get("href") or ""
            if href and len(self.results) < 5:
                self._row = {"url": href, "title": "", "snippet": "", "source": ""}
                self._capture = ("title", depth)
        elif (
            "result__snippet" in classes
            and tag not in self._VOID_TAGS
            and self._row
            and self._row["title"]
            and not self._capture
        ):
            self._capture = ("snippet", depth)
            self._text = []
        elif self._capture:
            self._text.append(" ")

    def _finish_capture(self, *, complete: bool) -> None:
        assert self._capture is not None
        kind, _ = self._capture
        text = _clean_text("".join(self._text))
        if kind == "title":
            if complete and text and self._row:
                self._row.update(source=_safe_source_from_url(self._row["url"]), title=text[:140], snippet=text[:240])
                self.results.append(self._row)
            else:
                self._row = None
        elif complete and text and self._row:
            self._row["snippet"] = text[:240]
        self._capture = None
        self._text = []

    def handle_endtag(self, tag: str) -> None:
        positions = self._positions.get(tag)
        if not positions:
            return
        depth = positions[-1]
        if self._hidden is not None and depth < self._hidden:
            return
        if self._capture and self._capture[1] >= depth:
            self._finish_capture(complete=self._capture[1] == depth)
        if self._container is not None and self._container >= depth:
            self._end_result()
            self._container = None
        if self._hidden is not None and self._hidden >= depth:
            self._hidden = None
        if self._capture and self._hidden is None:
            self._text.append(" ")
        # Each open node is removed once, including malformed ancestor closes.
        # Per-tag positions avoid repeatedly scanning a deep unmatched stack.
        while len(self._stack) > depth:
            popped = self._stack.pop()
            self._positions[popped].pop()
            if not self._positions[popped]:
                del self._positions[popped]

    def handle_data(self, data: str) -> None:
        if self._capture and self._hidden is None:
            self._text.append(data)


def urlopen(request: Request, *, timeout: float) -> BufferedResponse:
    return bounded_urlopen(
        request, timeout=timeout, maximum=MAX_PUBLIC_RESPONSE_BYTES, follow_redirects=True, truncate=True
    )


def _ddg_search(query: str, *, timeout_seconds: float = PUBLIC_RESEARCH_TIMEOUT_SECONDS) -> dict[str, Any]:
    if contains_sensitive(query):
        raise ValueError("sensitive_public_research_query")
    url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
    request = Request(url, headers={"User-Agent": PUBLIC_RESEARCH_USER_AGENT})
    with urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read(MAX_PUBLIC_RESPONSE_BYTES).decode("utf-8", "ignore")
    parser = _SearchResultsParser()
    parser.feed(raw)
    parser.close()
    return {"query": query, "url": url, "results": parser.results}


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
    for match in re.finditer(_PRICE_PATTERN, text, flags=re.I):
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
    range_spans: list[tuple[int, int]] = []
    observed_spans: list[tuple[int, int]] = []
    for pattern_index, pattern in enumerate(_HOUR_PATTERNS):
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
    row: dict[str, Any],
    query: str,
    operations: list[dict[str, Any]],
    kind: str,
    *,
    operation_matcher: OperationMatcher | None = None,
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
    matcher = operation_matcher or OperationMatcher()
    # A rejected title amount can still veto parts on an explicitly named caption.
    can_inherit_title = _measurements_are_unambiguous(str(row.get("title") or ""), kind, expected_count=0)
    title_operation = _title_operation_for_caption(
        str(row.get("title") or ""), str(row.get("snippet") or ""), operations, kind, matcher
    )
    title_veto_operation = title_operation
    if (
        kind == "labor_prices"
        and not title_veto_operation
        and _labor_only_flags(str(row.get("title") or ""))[0] is True
    ):
        title_veto_operation = _title_parts_veto_operation(
            str(row.get("title") or ""), str(row.get("snippet") or ""), operations, matcher
        )
    # The title can supply scope only for an unambiguous measurement caption.
    for field in ("title", "snippet"):
        segments = _observed_operation_segments(str(row.get(field) or ""), operations)
        for segment, bindable in segments:
            can_bind = bindable and _measurements_are_unambiguous(segment, kind)
            items: list[dict[str, Any]]
            if kind == "labor_prices":
                items = [{"price_rub": price} for price in dict.fromkeys(_prices_from_text(segment))]
            else:
                items = [
                    {"hours": round((low + high) / 2, 2), "range_hours": [low, high]}
                    for low, high in dict.fromkeys(_hours_from_text(segment))
                ]
            operation_name = (
                _observed_operation_name(segment, operations, matcher) if can_bind and len(items) == 1 else ""
            )
            inherited_title = (
                can_bind
                and can_inherit_title
                and field == "snippet"
                and not operation_name
                and len(items) == 1
                and title_operation
            )
            if inherited_title:
                operation_name = title_operation
            if kind == "labor_prices":
                flags_text = str(row.get("snippet") or "") if inherited_title else segment
                if bindable and not operation_name and len(segments) == 1:
                    flags_text = snippet
                includes_parts, labor_only = _labor_only_flags(flags_text)
                # Keep parts-inclusive evidence from the same unambiguous caption.
                if (
                    title_veto_operation
                    and matcher.matches(operation_name, title_veto_operation)
                    and _labor_only_flags(str(row.get("title") or ""))[0] is True
                ):
                    includes_parts, labor_only = True, False
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

    operation_matcher = OperationMatcher(vehicle_context)
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
                parsed, _ = parse_public_work_evidence(
                    row, query, operations, query_type, operation_matcher=operation_matcher
                )
                target = "quotes" if query_type == "labor_prices" else "labor_time_sample"
                result[target].extend(legacy_work_evidence_row(item) for item in parsed)
                rows_found += len(parsed)
            checked["rows_found"] = rows_found
            result["sources_checked"].append(checked)

    result["quotes"] = _dedupe_quote_rows(result["quotes"])
    result["labor_time_sample"] = _dedupe_rows(
        result["labor_time_sample"], ("source", "operation_name", "hours", "range_hours")
    )
    if not result["labor_time_sample"]:
        result["warnings"].append("Public labor-time mentions were not found automatically.")
    if not any(quote.get("labor_only") is True and quote.get("includes_parts") is False for quote in result["quotes"]):
        result["warnings"].append("Public labor-only price quotes were not found automatically.")
    return result


def _observed_operation_segments(text: str, operations: list[dict[str, Any]]) -> list[tuple[str, bool]]:
    # Full labels protect internal verbs and punctuation, including aliases.
    spans = {(start, end) for start, end, _ in _observed_operation_matches(text, operations)}
    spans.update(
        match.span()
        for match in re.finditer(r"(?<!\w)(?:замена|ремонт|диагностика|снятие|установка)\s", text, flags=re.I)
    )
    markers: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if markers and start < markers[-1][1]:
            markers[-1] = (markers[-1][0], max(end, markers[-1][1]))
        else:
            markers.append((start, end))
    boundaries = {0, len(text)}
    boundaries.update(
        match.end()
        for match in re.finditer(r"(?<!руб\.)(?<!р\.)(?<!ч\.)(?<=[.!?])\s+|;+\s*|[\r\n\u2028\u2029]+", text, flags=re.I)
        if not any(start < match.end() < end for start, end in markers)
    )
    result: list[tuple[str, bool]] = []
    for start, end in pairwise(sorted(boundaries)):
        clause = text[start:end]
        local = [(left - start, right - start) for left, right in markers if start <= left < end]
        result.extend(_split_observed_clause(clause, local))
    return [(_clean_text(segment), bindable) for segment, bindable in result if _clean_text(segment)]


def _raw_measurements(text: str) -> list[tuple[int, int, str]]:
    """Locate logical mentions before value validation, deduplication or truncation."""
    spans = [(*match.span(), "price") for match in re.finditer(_PRICE_PATTERN, text, flags=re.I)]
    hours: list[tuple[int, int, str]] = []
    range_pattern, prefix_pattern, suffix_pattern = _HOUR_PATTERNS
    # Prefer a value's own unit over a prefix in an earlier operation's record.
    for pattern in (range_pattern, suffix_pattern, prefix_pattern):
        for match in re.finditer(pattern, text, flags=re.I):
            if not any(match.start() < end and match.end() > start for start, end, _ in hours):
                hours.append((*match.span(), "hours"))
    return sorted(spans + hours)


def _measurements_are_unambiguous(text: str, kind: str, *, expected_count: int = 1) -> bool:
    """Count logical mentions before numeric validation, caps or value deduplication."""
    kinds = [measurement_kind for _, _, measurement_kind in _raw_measurements(text)]
    own_kind = "price" if kind == "labor_prices" else "hours"
    return kinds.count(own_kind) == expected_count and len(kinds) == len(set(kinds))


def _forward_observation_headers(
    text: str, markers: list[tuple[int, int]], measurements: list[tuple[int, int, str]]
) -> list[int]:
    headers = []
    for index, (_, end) in enumerate(markers):
        limit = markers[index + 1][0] if index + 1 < len(markers) else len(text)
        match = re.match(r"[^:;,|•.!?\r\n\u2028\u2029]*:", text[end:limit])
        header = end + match.end() if match else 0
        if any(end <= start < header for start, _, _ in measurements):
            header = 0
        headers.append(header)
    return headers


def _split_observed_clause(text: str, markers: list[tuple[int, int]]) -> list[tuple[str, bool]]:
    if len(markers) < 2:
        return [(text, True)]
    measurements = _raw_measurements(text)
    headers = _forward_observation_headers(text, markers, measurements)
    starts = [start for start, _, _ in measurements]
    separators = [match.end() for match in re.finditer(r"(?<!\d),|,(?!\d)|[|•]|\s+/\s+", text)]
    cuts = [[0]]
    for (_, left), (right, _) in pairwise(markers):
        options = {right, *(cut for cut in separators if left <= cut <= right)}
        options = {cut for cut in options if not any(start < cut < end for start, end, _ in measurements)}
        # Excessively many alternatives stay visible as unbound evidence.
        if len(options) > 64:
            return [(text, False)]
        cuts.append(sorted(options))
    cuts.append([len(text)])

    # A layered DAG keeps possible boundaries without enumerating partitions.
    # Each record needs one raw mention per kind at most; price + time is valid.
    layers: list[dict[int, list[tuple[int, _ObservationSignature]]]] = []
    reachable = {0}
    for index, candidates in enumerate(cuts[1:]):
        layer: dict[int, list[tuple[int, _ObservationSignature]]] = {}
        for end in candidates:
            for start in reachable:
                first, last = bisect_left(starts, start), bisect_left(starts, end)
                observed = measurements[first:last]
                if not observed and not (headers[index] or (index == len(markers) - 1 and headers[index - 1])):
                    continue
                if len(observed) > 2 or len({kind for _, _, kind in observed}) != len(observed):
                    continue
                # A forward header cannot take a preceding value, even when
                # a different kind of measurement has filled the previous row.
                if headers[index] and any(
                    re.search(r"\d", text[left : min(right, headers[index])]) for left, right, _ in observed
                ):
                    continue
                signature = (first, last, _labor_only_flags(text[start:end]))
                layer.setdefault(end, []).append((start, signature))
        if not layer:
            return [(text, False)]
        layers.append(layer)
        reachable = set(layer)

    reachable = {len(text)}
    for layer in reversed(layers):
        edges = [edge for end in reachable for edge in layer[end]]
        if len({signature for _, signature in edges}) != 1:
            return [(text, False)]
        reachable = {start for start, _ in edges}
    end = len(text)
    boundaries = [end]
    for layer in reversed(layers):
        end = layer[end][0][0]
        boundaries.append(end)
    # A slash at a proved internal record boundary is layout, not a dangling
    # operation connector. Keep an actual final slash visible to the resolver.
    return [
        (re.sub(r"\s+/\s*$", "", text[start:end]) if end < len(text) else text[start:end], True)
        for start, end in pairwise(reversed(boundaries))
    ]


def _observed_operation_matches(text: str, operations: list[dict[str, Any]]) -> list[tuple[int, int, str]]:
    matches: list[tuple[int, int, str]] = []
    for operation in operations:
        name = _clean_text(operation.get("normalized_name") or operation.get("input"))
        if not name:
            continue
        aliases = operation.get("aliases")
        labels = {name.casefold(): name}
        literal_input = _clean_text(operation.get("input"))
        if literal_input:
            labels[literal_input.casefold()] = literal_input
        if isinstance(aliases, (list, tuple)):
            for alias in aliases:
                labels.setdefault(_clean_text(alias).casefold(), name)
        for label, observed_name in labels.items():
            if not label:
                continue
            for match in re.finditer(r"(?<!\w)" + re.escape(label) + r"(?!\w)", text, flags=re.I):
                matches.append((match.start(), match.end(), observed_name))
    return matches


def _observed_operation_name(
    text: str, operations: list[dict[str, Any]], matcher: OperationMatcher | None = None
) -> str:
    matcher = matcher or OperationMatcher()
    captured = _operation_text(text, operations, matcher)
    return _resolve_operation(captured, operations, matcher) if captured is not None else ""


def _resolve_operation(
    captured: tuple[str, list[str]], operations: list[dict[str, Any]], matcher: OperationMatcher
) -> str:
    observed, names = captured
    if not observed:
        return ""
    if not names:
        # A complete observed phrase may contain a known optional qualifier even
        # when no literal label occurs, e.g. an axle inside the operation name.
        names = [
            name
            for operation in operations
            for key in ("input", "normalized_name")
            if (name := _clean_text(operation.get(key)))
        ]
    compatible = [name for name in names if matcher.matches(name, observed)]
    for name in compatible:
        if matcher.identity(name) == matcher.identity(observed):
            return name
    # Retain explicit axle/side/gearbox qualifiers for subsequent matching and
    # strict quote deduplication. A generic label must not erase this scope.
    return observed if compatible else ""


def _operation_text(
    text: str,
    operations: list[dict[str, Any]],
    matcher: OperationMatcher,
    *,
    veto_annotations: list[tuple[int, int]] | None = None,
) -> tuple[str, list[str]] | None:
    matches = _observed_operation_matches(text, operations)
    outer = [
        (start, end, name)
        for start, end, name in matches
        if not any(
            other_start <= start and end <= other_end and (start, end) != (other_start, other_end)
            for other_start, other_end, _ in matches
        )
    ]
    names = {matcher.identity(name): name for _, _, name in outer}
    if len(names) > 1:
        return None
    labels = sorted({(start, end): name for start, end, name in outer}.items())
    if any(left[0][1] > right[0][0] for left, right in pairwise(labels)):
        return None
    protected = [span for span, _ in labels]
    metadata = [
        (start, end)
        for start, end in [*_metadata_spans(text), *(veto_annotations or [])]
        if not any(start < right and end > left for left, right in protected)
    ]
    merged: list[tuple[int, int]] = []
    for start, end in sorted(metadata):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    # Conjunctions can join two proved metadata spans. Never discard a dangling
    # connector beside an operation or invent a phrase across a masked label.
    bridges = [
        (left, right)
        for (_, left), (right, _) in pairwise(merged)
        if re.fullmatch(r"\s*(?:и|плюс|[,+/&])\s*", text[left:right], flags=re.I)
        and not any(left < end and right > start for start, end in protected)
    ]
    characters = list(text)
    for start, end in (*merged, *bridges):
        characters[start:end] = [" "] * (end - start)
    uncovered = characters.copy()
    for start, end in protected:
        uncovered[start:end] = [" "] * (end - start)
    # The matcher accepts a few complete labor-only phrases. Do not let masking
    # an intervening amount synthesize those phrases from unexplained words.
    if re.search(r"(?<!\w)(?:без|только)(?!\w)", "".join(uncovered), flags=re.I):
        return None
    pieces: list[str] = []
    previous = 0
    for (start, end), name in labels:
        pieces.extend(("".join(characters[previous:start]), name))
        previous = end
    pieces.append("".join(characters[previous:]))
    # Validate unknown characters only outside declared labels and aliases.
    # Their own symbols, such as a maintenance number, are already observed.
    punctuation = r"[.;:|•()\[\]{}—–!?\-\"«»]"
    observed = _clean_text(re.sub(punctuation, " ", " ".join(pieces))).strip(" ,")
    if re.search(r"[^\w\s,+/&']", re.sub(punctuation, " ", "".join(uncovered))):
        return None
    return observed, list(names.values())


def _title_operation_for_caption(
    title: str, snippet: str, operations: list[dict[str, Any]], kind: str, matcher: OperationMatcher
) -> str:
    name = _observed_operation_name(title, operations, matcher)
    if not name:
        return ""
    extract = _prices_from_text if kind == "labor_prices" else _hours_from_text
    if not _measurements_are_unambiguous(snippet, kind) or extract(title) or len(extract(snippet)) != 1:
        return ""
    return name if all(_caption_is_metadata(text, operations, name, matcher) for text in (title, snippet)) else ""


def _title_parts_veto_operation(
    title: str, snippet: str, operations: list[dict[str, Any]], matcher: OperationMatcher
) -> str:
    """An unsupported title amount may only veto an independently bound price."""
    if (
        _prices_from_text(title)
        or not _measurements_are_unambiguous(snippet, "labor_prices")
        or len(_prices_from_text(snippet)) != 1
    ):
        return ""
    explicit_name = _observed_operation_name(snippet, operations, matcher)
    if not explicit_name:
        return ""
    # Full token boundaries prevent turning an identifier such as 0AM1000USD
    # into a known gearbox code. These annotations never enter positive binding.
    symbols = "".join(sorted({character for character in title if category(character) == "Sc"}))
    currency = rf"(?:[{re.escape(symbols)}]|USD|EUR|GBP)" if symbols else r"(?:USD|EUR|GBP)"
    number = r"(?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[,.]\d+)?"
    sign = r"(?:[-+−]\s*)?"
    annotations = [
        match.span()
        for match in re.finditer(
            rf"(?<![\w,.+−-])(?:{sign}{number}\s*{currency}|{sign}{currency}\s*{number}|"
            rf"{currency}\s*[-+−]\s*{number})(?![\w+−-]|[,.][\d,.])",
            title,
            flags=re.I,
        )
    ]
    if len(annotations) != 1:
        return ""
    captured = _operation_text(title, operations, matcher, veto_annotations=annotations)
    name = _resolve_operation(captured, operations, matcher) if captured is not None else ""
    return name if matcher.matches(explicit_name, name) else ""


def _caption_is_metadata(text: str, operations: list[dict[str, Any]], name: str, matcher: OperationMatcher) -> bool:
    captured = _operation_text(text, operations, matcher)
    return captured is not None and (
        not captured[0] or matcher.matches(name, _resolve_operation(captured, operations, matcher))
    )


def _metadata_spans(text: str) -> list[tuple[int, int]]:
    """Prove metadata on the original text, before masking operation labels."""
    spans = _measurement_metadata_spans(text)
    spans.extend(
        match.span()
        for match in re.finditer(
            r"(?<!\w)(?:(?:г\.?\s+)?(?:красноярск(?:е|а|у|ом)?|санкт[- ]петербург(?:е|а|у|ом)?|спб)"
            r"|(?:стоимость|цена)(?:\s+работы)?|норматив\s+времени)(?!\w)",
            text,
            flags=re.I,
        )
    )
    noun = r"(?:запчаст\w*|материал\w*)"
    connector = r"(?:\s+(?:и|плюс)\s+|\s*[,+/]\s*)"
    noun_lists = [
        match.span() for match in re.finditer(rf"(?<!\w){noun}(?:{connector}{noun})*(?!\w)", text, flags=re.I)
    ]
    starts = [start for start, _ in noun_lists]
    # Locate noun lists separately: a failed suffix must not rescan every prefix.
    for marker in (*LABOR_ONLY_MARKERS, *PARTS_INCLUDED_MARKERS):
        words = marker.split()
        pattern = r"\s+".join(re.escape(word) for word in words)
        if words[-1] in {"запчаст", "материал", "включ"}:
            pattern += r"\w*"
        for match in re.finditer(rf"(?<!\w){pattern}(?!\w)", text, flags=re.I):
            start, end = match.span()
            if words[0] in {"запчасти", "материалы"}:
                index = bisect_left(starts, start + 1) - 1
                if index >= 0 and start < noun_lists[index][1]:
                    start = noun_lists[index][0]
            if words[-1].startswith(("запчаст", "материал")):
                index = bisect_left(starts, end) - 1
                if index >= 0 and end <= noun_lists[index][1]:
                    end = noun_lists[index][1]
            spans.append((start, end))
    return spans


def _measurement_metadata_spans(text: str) -> list[tuple[int, int]]:
    # Raw mention spans deliberately include rejected values and, for prefix
    # hours, arbitrary intervening text. Only numeric captures and proved units
    # are metadata; an intervening "не" or another object must survive.
    spans: list[tuple[int, int]] = []
    currency = r"\s*(?:₽|руб(?:лей|ля|ль)?\.?|р\.)(?!\w)"
    unit = r"\s*(?:н/?ч|нормо[- ]?час(?:а|ов)?|час(?:а|ов)?|ч\.)(?!\w)"
    header = r"(?:н/?ч|нормо[- ]?час|норма времени|трудоемкость|время выполнения)(?!\w)"
    for match in re.finditer(_PRICE_PATTERN, text, flags=re.I):
        suffix = re.match(currency, text[match.end(1) :], flags=re.I)
        if suffix:
            spans.append((match.start(1), match.end(1) + suffix.end()))
    for index, pattern in enumerate(_HOUR_PATTERNS):
        for match in re.finditer(pattern, text, flags=re.I):
            if index == 1:
                prefix = re.match(header, text[match.start() :], flags=re.I)
                if prefix:
                    spans.extend(((match.start(), match.start() + prefix.end()), match.span(1)))
            else:
                suffix = re.match(unit, text[match.end(2 if index == 0 else 1) :], flags=re.I)
                if suffix:
                    spans.append((match.start(1), match.end(2 if index == 0 else 1) + suffix.end()))
    for start, _ in list(spans):
        prefix = re.search(r"(?<!\w)(?:от|до|работа|работы|за работу)\s*$", text[:start], flags=re.I)
        if prefix:
            spans.append(prefix.span())
    return spans


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
        key = tuple(
            _source_identity(row.get(field, "")) if field == "source" else str(row.get(field, "")).casefold()
            for field in fields
        )
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
        key = tuple(
            _source_identity(identity.get(field, "")) if field == "source" else str(identity.get(field, "")).casefold()
            for field in fields
        )
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
