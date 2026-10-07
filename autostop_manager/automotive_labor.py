"""Labor evidence acquisition is explicit; normalization and calculation are pure."""

from __future__ import annotations

import re
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from urllib.error import HTTPError, URLError

from .automotive_contracts import MAX_ROWS, content_digest, finite_number, invalid, result

UNITS = {
    "hours": 1.0,
    "hour": 1.0,
    "h": 1.0,
    "нормочас": 1.0,
    "norm_hours": 1.0,
    "minutes": 1 / 60,
    "minute": 1 / 60,
    "min": 1 / 60,
    "мин": 1 / 60,
    "seconds": 1 / 3600,
}


def _labor_structure_valid(row: dict[str, Any]) -> bool:
    operation_id = row.get("operation_id")
    if operation_id is not None and (isinstance(operation_id, bool) or not isinstance(operation_id, (str, int))):
        return False
    for field in ("included_operations", "overlaps_with"):
        values = row.get(field, [])
        if not isinstance(values, list) or any(
            isinstance(value, bool) or not isinstance(value, (str, int)) or value == "" for value in values
        ):
            return False
    if "overlap_resolved" in row and not isinstance(row["overlap_resolved"], bool):
        return False
    source = row.get("source")
    return source is None or isinstance(source, (dict, str))


def _has_labor_source(source: Any) -> bool:
    if isinstance(source, str):
        return bool(source.strip())
    return isinstance(source, dict) and any(
        isinstance(source.get(key), str) and source[key].strip()
        for key in ("provider", "primary_lineage", "source", "name", "locator", "url")
    )


def _operation_key(row: dict[str, Any], fallback: str | int) -> str:
    value = row.get("operation_id")
    return str(fallback if value is None or value == "" else value)


def _labor_row(row: dict[str, Any], unit: str | None, source: dict[str, Any] | None, index: int) -> dict[str, Any]:
    raw_time = next(
        (row[key] for key in ("workTime", "raw_time", "time", "hours", "labor_hours", "norm_hours") if key in row), None
    )
    operation = next((row[key] for key in ("workName", "operation_name", "operation", "name") if key in row), None)
    raw_unit = row.get("unit", unit)
    if raw_unit is None and any(key in row for key in ("hours", "labor_hours", "norm_hours")):
        raw_unit = "hours"
    factor = UNITS.get(str(raw_unit).casefold())
    number = finite_number(raw_time)
    hours = number * factor if number is not None and factor is not None else None
    provenance = row.get("source", source)
    missing = [
        field
        for field, value in (("operation_name", operation), ("unit", factor), ("time", number), ("source", provenance))
        if value is None or value == ""
    ]
    if not _has_labor_source(provenance) and "source" not in missing:
        missing.append("source")
    return {
        "operation_id": _operation_key(row, "labor-" + content_digest({"index": index, "operation": operation})[:16]),
        "operation_name": operation,
        "raw_time": raw_time,
        "raw_unit": raw_unit,
        "hours": hours,
        "source": provenance,
        "catalog_ref": row.get("catalog_ref"),
        "scope": row.get("scope"),
        "conditions": row.get("conditions") or {},
        "included_operations": row.get("included_operations") or [],
        "overlaps_with": row.get("overlaps_with") or [],
        "missing_fields": missing,
    }


def normalize_labor_time(
    rows: list[dict[str, Any]], unit: str | None = None, source: dict[str, Any] | None = None
) -> dict[str, Any]:
    if not isinstance(rows, list) or len(rows) > MAX_ROWS or any(not isinstance(row, dict) for row in rows):
        return invalid("normalize_labor_time", "rows")
    if source is not None and not isinstance(source, dict):
        return invalid("normalize_labor_time", "source")
    if any(not _labor_structure_valid(row) for row in rows):
        return invalid("normalize_labor_time", "rows.structure")
    normalized = [_labor_row(row, unit, source, index) for index, row in enumerate(rows)]
    missing = [f"rows[{index}].{field}" for index, row in enumerate(normalized) for field in row["missing_fields"]]
    overlaps = [
        {
            "operation_id": row["operation_id"],
            "included_operations": row["included_operations"],
            "overlaps_with": row["overlaps_with"],
        }
        for row in normalized
        if row["included_operations"] or row["overlaps_with"]
    ]
    return result(
        "normalize_labor_time",
        "partial" if missing else "success",
        {"labor": normalized, "overlaps": overlaps},
        missing_fields=missing,
    )


def _public_safe(value: Any) -> bool:
    text = str(value)
    return not re.search(
        r"[A-HJ-NPR-Z0-9]{17}|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?:\+7|8)[\s()-]*\d[\d\s()-]{8,}", text, re.I
    )


def collect_work_price_evidence(
    work_items: list[str],
    vehicle_context: dict[str, Any] | None = None,
    city: str = "Красноярск",
    sources: list[str] | None = None,
    aggregate_evidence: dict[str, Any] | None = None,
    deadline_seconds: float = 20,
    max_queries: int = 4,
) -> dict[str, Any]:
    from . import work_pricing_research as research

    chosen = sources if sources is not None else ["public_web"]
    deadline = finite_number(deadline_seconds, minimum=0.1)
    if (
        not isinstance(work_items, list)
        or not work_items
        or len(work_items) > 20
        or any(not isinstance(row, str) or len(row) > 500 or not _public_safe(row) for row in work_items)
        or not isinstance(chosen, list)
        or not set(chosen).issubset({"public_web", "provided_aggregate"})
        or not isinstance(max_queries, int)
        or isinstance(max_queries, bool)
        or not 1 <= max_queries <= 4
        or deadline is None
        or deadline > 60
    ):
        return invalid("collect_work_price_evidence", "work_items_sources_budget")
    context = vehicle_context or {}
    if (
        not isinstance(context, dict)
        or set(context).difference({"make", "model", "year", "engine", "transmission", "vehicle_class"})
        or not _public_safe(context)
        or not _public_safe(city)
    ):
        return invalid("collect_work_price_evidence", "deidentified_vehicle_context")
    if "provided_aggregate" in chosen and not isinstance(aggregate_evidence, dict):
        return invalid("collect_work_price_evidence", "aggregate_evidence")
    operations = [{"normalized_name": name, "input": name} for name in work_items]
    observations: list[dict[str, Any]] = []
    labor: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    warnings: list[str] = []
    started = time.monotonic()
    if "public_web" in chosen:
        queries = research.build_public_research_queries(vehicle_context=context, operations=operations, city=city)
        selected = [(kind, query) for kind, values in queries.items() for query in values][:max_queries]
        for kind, query in selected:
            remaining = deadline - (time.monotonic() - started)
            if remaining <= 0:
                warnings.append("deadline_exceeded")
                break
            attempt = {"provider": "duckduckgo_html", "method": "public_search", "outcome": "success"}
            try:
                found = research._ddg_search(query, timeout_seconds=min(4, remaining))
            except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
                attempt["outcome"] = "provider_error"
                warnings.append(type(exc).__name__)
                attempts.append(attempt)
                continue
            attempts.append(attempt)
            for row in found.get("results", []):
                parsed, source = research.parse_public_work_evidence(row, query, operations, kind)
                evidence.append(source)
                if kind == "labor_prices":
                    observations.extend(parsed)
                else:
                    labor.extend(parsed)
    data = {
        "observations": observations,
        "labor": labor,
        "sources": chosen,
        "vehicle_context": context,
        "provided_aggregate": aggregate_evidence if "provided_aggregate" in chosen else None,
        "work_items": work_items,
        "deadline_seconds": deadline,
    }
    missing = [] if observations or aggregate_evidence is not None else ["work_price_evidence"]
    return result(
        "collect_work_price_evidence",
        "partial" if missing or warnings else "success",
        data,
        evidence=evidence,
        missing_fields=missing,
        warnings=warnings,
        network_calls=len(attempts),
        attempts=attempts,
    )


def _price_round(value: float, quantum: float) -> float:
    return float(
        (Decimal(str(value)) / Decimal(str(quantum))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        * Decimal(str(quantum))
    )


def _operation_price(
    row: dict[str, Any], rate: float | None, observations: list[dict[str, Any]], basis: str
) -> tuple[float | None, list[float] | None]:
    if basis == "hourly_rate":
        hours = finite_number(row.get("hours"))
        bounds = row.get("range_hours")
        if rate is None:
            return None, None
        if isinstance(bounds, list) and len(bounds) == 2 and all(finite_number(value) is not None for value in bounds):
            low, high = float(bounds[0]) * rate, float(bounds[1]) * rate
            return ((low + high) / 2, [low, high]) if low <= high else (None, None)
        if "range_hours" in row:
            return None, None
        return (hours * rate, [hours * rate, hours * rate]) if hours is not None else (None, None)
    sample = [
        finite_number(item.get("price_rub"))
        for item in observations
        if item.get("operation_name") == row.get("operation_name")
        and item.get("labor_only") is True
        and item.get("includes_parts") is False
    ]
    prices = sorted(value for value in sample if value is not None)
    if not prices:
        return None, None
    middle = len(prices) // 2
    median = prices[middle] if len(prices) % 2 else (prices[middle - 1] + prices[middle]) / 2
    return median, [prices[0], prices[-1]]


def calculate_work_price(
    labor: list[dict[str, Any]],
    policy: dict[str, Any],
    hourly_rate: float | None = None,
    observations: list[dict[str, Any]] | None = None,
    unknown_costs: list[str] | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(labor, list)
        or len(labor) > MAX_ROWS
        or any(not isinstance(row, dict) for row in labor)
        or not isinstance(policy, dict)
    ):
        return invalid("calculate_work_price", "labor_or_policy")
    basis = policy.get("basis")
    coefficient = finite_number(policy.get("coefficient", 1), minimum=0.00001)
    quantum = finite_number(policy.get("rounding", 1), minimum=0.00001)
    rate = finite_number(hourly_rate)
    sample = observations or []
    if (
        not policy.get("version")
        or basis not in {"hourly_rate", "public_observations"}
        or coefficient is None
        or quantum is None
        or (basis == "hourly_rate" and rate is None)
    ):
        return invalid("calculate_work_price", "policy_version_basis_rate")
    if not isinstance(sample, list) or len(sample) > MAX_ROWS or any(not isinstance(row, dict) for row in sample):
        return invalid("calculate_work_price", "observations")
    if any(not _labor_structure_valid(row) for row in labor):
        return invalid("calculate_work_price", "labor.structure")
    if unknown_costs is not None and (
        not isinstance(unknown_costs, list) or any(not isinstance(row, str) for row in unknown_costs)
    ):
        return invalid("calculate_work_price", "unknown_costs")
    prices: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    missing = list(unknown_costs or [])
    seen: set[str] = set()
    included = {str(operation) for row in labor for operation in row.get("included_operations", [])}
    for index, row in enumerate(labor):
        key = _operation_key(row, row.get("operation_name") or index)
        if key in seen or key in included:
            exclusions.append({"operation_id": key, "reason": "duplicate_or_included_operation"})
            continue
        seen.add(key)
        if row.get("overlaps_with") and not row.get("overlap_resolved"):
            missing.append(key + ".overlap_adjustment")
            exclusions.append({"operation_id": key, "reason": "unresolved_overlap"})
            continue
        price, bounds = _operation_price(row, rate, sample, str(basis))
        if price is None or bounds is None:
            missing.append(key + ".price_basis")
            exclusions.append({"operation_id": key, "reason": "unknown_labor_or_price"})
            continue
        prices.append(
            {
                "operation_id": key,
                "operation_name": row.get("operation_name"),
                "price": _price_round(price * coefficient, quantum),
                "range": [_price_round(value * coefficient, quantum) for value in bounds],
            }
        )
    subtotal = sum(row["price"] for row in prices)
    total_range = [sum(row["range"][index] for row in prices) for index in (0, 1)]
    data = {
        "basis": basis,
        "hourly_rate": rate,
        "policy": policy,
        "currency": policy.get("currency", "RUB"),
        "operations": prices,
        "known_subtotal": subtotal,
        "total": None if missing else subtotal,
        "range": None if missing else total_range,
        "exclusions": exclusions,
        "unknown_costs": missing,
    }
    return result("calculate_work_price", "partial" if missing else "success", data, missing_fields=missing)
