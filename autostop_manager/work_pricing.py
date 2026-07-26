from __future__ import annotations

import re
from datetime import UTC, datetime
from statistics import median
from typing import Any

from .service_labor_experience import load_service_labor_experience
from .service_pricing_experience import find_labor_experience, load_service_pricing_experience
from .work_pricing_research import collect_public_work_pricing_research

ROUNDING_STEP_RUB = 100
AUTOSTOP_MARKUP = 1.50
MIN_CONFIDENT_QUOTES = 3

COMMON_OPERATION_WORDS = {
    "замена",
    "поменять",
    "поменял",
    "снять",
    "снятие",
    "установка",
    "установить",
    "ремонт",
    "работа",
    "работы",
    "работ",
    "по",
    "и",
    "или",
    "перед",
    "зад",
    "лев",
    "прав",
    "передний",
    "передняя",
    "задний",
    "задняя",
}

SAFETY_CONTEXT_BY_CATEGORY: dict[str, list[str]] = {
    "steering": ["vin_or_chassis", "exact_operation", "labor_only_public_quotes"],
    "brakes": ["vin_or_chassis", "exact_operation", "labor_only_public_quotes"],
    "suspension": ["vin_or_chassis", "exact_operation", "labor_only_public_quotes"],
    "drivetrain": ["vin_or_chassis", "exact_operation", "labor_only_public_quotes"],
    "transmission": ["vin_or_chassis", "transmission_code", "exact_operation", "labor_only_public_quotes"],
    "engine": ["vin_or_chassis", "engine", "exact_operation", "labor_only_public_quotes"],
    "srs_adas_hv": ["vin_or_chassis", "oem_service_source_check", "exact_operation", "labor_only_public_quotes"],
}


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _normalize_key(value: str | None) -> str:
    text = _clean_text(value)
    return re.sub(r"[^a-z0-9а-яё]+", "_", text).strip("_")


def _as_text_list(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(item) for item in value if str(item).strip()]


def _identifier_variants(*values: Any) -> list[str]:
    variants: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        compact = re.sub(r"[^A-Za-z0-9А-Яа-я]+", "", text).upper()
        for item in (text, text.upper(), text.casefold(), compact, compact.casefold()):
            if item and len(item) >= 6:
                variants.add(item)
    return sorted(variants, key=len, reverse=True)


def _redact_identifier(value: Any) -> str:
    compact = re.sub(r"[^A-Za-z0-9А-Яа-я]+", "", str(value or "")).upper()
    if not compact:
        return ""
    if len(compact) <= 6:
        return f"{compact[:2]}***"
    return f"{compact[:3]}***{compact[-3:]}"


def _replace_identifiers(value: Any, identifiers: list[str], replacement: str) -> Any:
    if not isinstance(value, str):
        return value
    text = value
    for identifier in identifiers:
        text = text.replace(identifier, replacement)
    return re.sub(r"\s+", " ", text).strip()


def _public_vehicle_context(vehicle_context: dict[str, Any]) -> dict[str, Any]:
    identifiers = _identifier_variants(vehicle_context.get("vin"), vehicle_context.get("chassis"))
    public = dict(vehicle_context)
    for key in ("vehicle", "make", "model", "engine", "transmission"):
        public[key] = _replace_identifiers(public.get(key), identifiers, "[REDACTED_IDENTIFIER]")
    if vehicle_context.get("vin"):
        public["vin"] = _redact_identifier(vehicle_context.get("vin"))
    if vehicle_context.get("chassis"):
        public["chassis"] = _redact_identifier(vehicle_context.get("chassis"))
    return public


def _research_vehicle_context(vehicle_context: dict[str, Any]) -> dict[str, Any]:
    identifiers = _identifier_variants(vehicle_context.get("vin"), vehicle_context.get("chassis"))
    public = dict(vehicle_context)
    public["vin"] = None
    public["chassis"] = None
    for key in ("vehicle", "make", "model", "engine", "transmission"):
        public[key] = _replace_identifiers(public.get(key), identifiers, "")
    return public


def _operation_category(text: str) -> str:
    if any(token in text for token in ("рейк", "рулев", "руль", "steering", "rack")):
        return "steering"
    if any(token in text for token in ("мехатрон", "dsg", "s tronic", "акпп", "кпп", "короб", "transmission")):
        return "transmission"
    if any(token in text for token in ("тормоз", "brake")):
        return "brakes"
    if any(token in text for token in ("подвес", "амортиз", "стойк", "опорн", "рычаг", "сайлент", "suspension")):
        return "suspension"
    if any(token in text for token in ("шрус", "привод", "пыльник", "cv joint", "driveshaft")):
        return "drivetrain"
    if any(token in text for token in ("двигател", "мотор", "грм", "engine")):
        return "engine"
    if any(token in text for token in ("srs", "airbag", "подушк", "adas", "камера", "радар", "hv", "высоковольт")):
        return "srs_adas_hv"
    if any(token in text for token in ("диагност", "провер", "scan", "скан")):
        return "diagnostics"
    return "general"


def _canonical_operation(raw: str) -> str:
    text = _clean_text(raw)
    if not text:
        return ""
    if "опорн" in text and any(token in text for token in ("подшип", "стойк", "амортиз")):
        return "замена опорных подшипников передних стоек"
    if "рычаг" in text and any(token in text for token in ("перед", "сайлент", "перепресс")):
        return "замена передних рычагов с перепрессовкой сайлентблоков"
    if "вентил" in text and any(token in text for token in ("радиатор", "кассет", "охлажд")):
        return "замена кассеты вентиляторов радиатора"
    if "пыльник" in text and "шрус" in text:
        return "замена пыльника переднего ШРУС"
    if "мехатрон" in text and any(token in text for token in ("dsg", "s tronic", "0am", "0cw", "02e", "0d9", "0gc")):
        return "замена мехатроника DSG"
    if "мехатрон" in text:
        return "замена мехатроника КПП"
    if "рейк" in text and any(token in text for token in ("рулев", "руль", "rack")):
        return "замена рулевой рейки"
    if "рейк" in text and any(token in text for token in ("помен", "замен", "сня", "установ")):
        return "замена рулевой рейки"
    if any(token in text for token in ("диагност", "провер", "scan", "скан")):
        if any(token in text for token in ("ходов", "подвес")):
            return "диагностика подвески"
        if any(token in text for token in ("акпп", "кпп", "dsg", "короб", "transmission")):
            return "диагностика трансмиссии"
        return "диагностика"
    return text


def _significant_tokens(value: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9а-яё]{3,}", _clean_text(value)))
    return {token for token in tokens if token not in COMMON_OPERATION_WORDS}


def _operations_match(operation_name: str, quote_operation_name: str) -> bool:
    op = _canonical_operation(operation_name)
    quote_op = _canonical_operation(quote_operation_name)
    if not op or not quote_op:
        return False
    if op == quote_op:
        return True
    if _operation_category(op) != _operation_category(quote_op):
        return False
    overlap = _significant_tokens(op) & _significant_tokens(quote_op)
    return len(overlap) >= 1


def _parse_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = _clean_text(str(value))
    if text in {"1", "true", "yes", "y", "да", "истина", "включая", "included"}:
        return True
    if text in {"0", "false", "no", "n", "нет", "ложь", "не включая", "excluded"}:
        return False
    return None


def _coerce_price(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        price = round(float(value))
        return price if price > 0 else None
    text = str(value).replace(",", ".")
    match = re.search(r"\d+(?:[ .]\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?", text)
    if not match:
        return None
    raw = match.group(0).replace(" ", "")
    try:
        price = round(float(raw))
    except ValueError:
        return None
    return price if price > 0 else None


def _coerce_first_price(*values: Any) -> int | None:
    for value in values:
        price = _coerce_price(value)
        if price is not None:
            return price
    return None


def _quote_rows(quotes_json: Any) -> list[dict[str, Any]]:
    if quotes_json is None:
        return []
    if isinstance(quotes_json, dict):
        if isinstance(quotes_json.get("quotes"), list):
            return [row for row in quotes_json["quotes"] if isinstance(row, dict)]
        if isinstance(quotes_json.get("market_sample"), list):
            return [row for row in quotes_json["market_sample"] if isinstance(row, dict)]
        return [quotes_json]
    if isinstance(quotes_json, list):
        return [row for row in quotes_json if isinstance(row, dict)]
    return []


def _labor_time_rows(source_json: Any) -> list[dict[str, Any]]:
    if source_json is None:
        return []
    if isinstance(source_json, dict):
        for key in ("labor_time_sample", "labor_times", "norm_hours_sample", "norm_hours"):
            if isinstance(source_json.get(key), list):
                return [row for row in source_json[key] if isinstance(row, dict)]
        return []
    if isinstance(source_json, list):
        return [row for row in source_json if isinstance(row, dict)]
    return []


def _normalize_quote(row: dict[str, Any]) -> dict[str, Any]:
    source = str(row.get("source") or row.get("source_name") or "").strip()
    city_region = str(row.get("city") or row.get("region") or row.get("city_region") or "").strip()
    operation_name = str(row.get("operation_name") or row.get("operation") or row.get("work_item") or "").strip()
    includes_parts = _parse_bool(row.get("includes_parts"))
    labor_only = _parse_bool(row.get("labor_only"))
    price = _coerce_first_price(row.get("price_rub"), row.get("price"))
    captured_at = str(row.get("captured_at") or datetime.now(UTC).date().isoformat())
    confidence = _normalize_key(str(row.get("confidence") or "medium"))

    reasons: list[str] = []
    if not source:
        reasons.append("missing_source")
    if not operation_name:
        reasons.append("missing_operation_name")
    if price is None:
        reasons.append("missing_or_invalid_price_rub")
    if includes_parts is True:
        reasons.append("includes_parts")
    if includes_parts is None and labor_only is not True:
        reasons.append("labor_only_not_confirmed")

    return {
        "source": source,
        "city_region": city_region,
        "operation_name": operation_name,
        "normalized_operation": _canonical_operation(operation_name),
        "price_rub": price,
        "includes_parts": includes_parts is True,
        "captured_at": captured_at,
        "confidence": confidence if confidence in {"high", "medium", "low"} else "medium",
        "valid_input": not reasons,
        "reasons": reasons,
    }


def _coerce_hours(value: Any) -> tuple[float | None, list[float] | None]:
    if isinstance(value, bool) or value is None:
        return None, None
    if isinstance(value, (int, float)):
        hours = round(float(value), 2)
        return (hours, [hours, hours]) if 0 < hours <= 80 else (None, None)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            start = round(float(str(value[0]).replace(",", ".")), 2)
            end = round(float(str(value[1]).replace(",", ".")), 2)
        except ValueError:
            return None, None
        if 0 < start <= end <= 80:
            return round((start + end) / 2, 2), [start, end]
        return None, None

    text = str(value).replace(",", ".")
    range_match = re.search(r"(\d{1,2}(?:\.\d{1,2})?)\s*[-–]\s*(\d{1,2}(?:\.\d{1,2})?)", text)
    if range_match:
        start = round(float(range_match.group(1)), 2)
        end = round(float(range_match.group(2)), 2)
        if 0 < start <= end <= 80:
            return round((start + end) / 2, 2), [start, end]

    match = re.search(r"\d{1,2}(?:\.\d{1,2})?", text)
    if not match:
        return None, None
    hours = round(float(match.group(0)), 2)
    return (hours, [hours, hours]) if 0 < hours <= 80 else (None, None)


def _coerce_first_hours(*values: Any) -> tuple[float | None, list[float] | None]:
    for value in values:
        hours, hours_range = _coerce_hours(value)
        if hours is not None and hours_range is not None:
            return hours, hours_range
    return None, None


def _normalize_labor_time_row(row: dict[str, Any]) -> dict[str, Any]:
    source = str(row.get("source") or row.get("source_name") or "").strip()
    city_region = str(row.get("city") or row.get("region") or row.get("city_region") or "").strip()
    operation_name = str(row.get("operation_name") or row.get("operation") or row.get("work_item") or "").strip()
    captured_at = str(row.get("captured_at") or datetime.now(UTC).date().isoformat())
    confidence = _normalize_key(str(row.get("confidence") or "low"))
    public_source = _parse_bool(row.get("public_source"))
    official = _parse_bool(row.get("official"))
    hours, hours_range = _coerce_first_hours(
        row.get("hours"), row.get("labor_hours"), row.get("norm_hours"), row.get("time_hours"), row.get("range_hours")
    )

    reasons: list[str] = []
    if not source:
        reasons.append("missing_source")
    if not operation_name:
        reasons.append("missing_operation_name")
    if hours is None or hours_range is None:
        reasons.append("missing_or_invalid_labor_time_hours")
    if public_source is False:
        reasons.append("not_public_source")

    return {
        "source": source,
        "city_region": city_region,
        "operation_name": operation_name,
        "normalized_operation": _canonical_operation(operation_name),
        "hours": hours,
        "range_hours": hours_range,
        "captured_at": captured_at,
        "confidence": confidence if confidence in {"high", "medium", "low"} else "low",
        "public_source": public_source is not False,
        "official": official is True,
        "capture_method": str(row.get("capture_method") or "public_source").strip(),
        "evidence": str(row.get("evidence") or "").strip()[:180],
        "valid_input": not reasons,
        "reasons": reasons,
    }


def _round_to_100(value: float | int | None) -> int | None:
    if value is None:
        return None
    return int(((float(value) + ROUNDING_STEP_RUB / 2) // ROUNDING_STEP_RUB) * ROUNDING_STEP_RUB)


def _outlier_filter(quotes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(quotes) < 5:
        return quotes, []
    prices = [int(quote["price_rub"]) for quote in quotes if quote.get("price_rub") is not None]
    if not prices:
        return [], quotes
    center = median(prices)
    deviations = [abs(price - center) for price in prices]
    mad = median(deviations)
    kept: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for quote in quotes:
        price = int(quote["price_rub"])
        is_outlier = False
        reason = ""
        if mad > 0 and abs(price - center) > 2.5 * mad:
            is_outlier = True
            reason = f"price_outlier_mad_center_{int(center)}"
        elif mad == 0 and (price > center * 3 or price < center / 3):
            is_outlier = True
            reason = f"price_outlier_ratio_center_{int(center)}"
        if is_outlier:
            row = dict(quote)
            row["exclude_reason"] = reason
            excluded.append(row)
        else:
            kept.append(quote)
    return kept, excluded


def _vehicle_class(vehicle_context: dict[str, Any]) -> str:
    text = _clean_text(" ".join(str(value) for value in vehicle_context.values() if value))
    if any(
        token in text
        for token in ("x5", "x6", "gle", "q7", "touareg", "range rover", "premium", "bmw", "mercedes", "audi")
    ):
        return "premium_or_large_suv"
    if any(token in text for token in ("dsg", "s tronic", "0am", "0cw", "02e", "0d9", "0gc")):
        return "vag_dsg"
    if any(token in text for token in ("груз", "truck", "коммерч")):
        return "commercial"
    return "passenger_car"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = _normalize_key(value)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _operation_context_requirements(operation: dict[str, Any], vehicle_context: dict[str, Any]) -> list[str]:
    category = str(operation.get("category") or "general")
    required = list(SAFETY_CONTEXT_BY_CATEGORY.get(category, ["exact_operation", "labor_only_public_quotes"]))
    missing: list[str] = []
    for item in required:
        if item == "vin_or_chassis" and (vehicle_context.get("vin") or vehicle_context.get("chassis")):
            continue
        if item == "transmission_code" and vehicle_context.get("transmission"):
            continue
        if item == "engine" and vehicle_context.get("engine"):
            continue
        if item == "exact_operation" and operation.get("normalized_name"):
            continue
        if item == "labor_only_public_quotes":
            continue
        missing.append(item)
    return missing


def _normalize_operations(work_items: list[str], complaint: str | None) -> tuple[list[dict[str, Any]], bool]:
    complaint_only = False
    items = [item for item in work_items if item.strip()]
    if not items and complaint:
        complaint_only = True
        items = ["диагностика"]
    operations: list[dict[str, Any]] = []
    for item in items:
        normalized_name = _canonical_operation(item)
        category = _operation_category(normalized_name or item)
        safety_flags = []
        if category in SAFETY_CONTEXT_BY_CATEGORY:
            safety_flags.append("safety_critical_verify_vin_or_oem_service_source")
        operations.append(
            {
                "input": item,
                "normalized_name": normalized_name,
                "category": category,
                "labor_only": True,
                "safety_flags": safety_flags,
            }
        )
    return operations, complaint_only


def _operation_estimate(
    *,
    operation: dict[str, Any],
    quotes: list[dict[str, Any]],
    vehicle_context: dict[str, Any],
) -> dict[str, Any]:
    operation_name = str(operation.get("input") or operation.get("normalized_name") or "")
    matched: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for quote in quotes:
        if not quote.get("valid_input"):
            row = dict(quote)
            row["exclude_reason"] = ",".join(row.get("reasons", []))
            excluded.append(row)
            continue
        if not _operations_match(operation_name, str(quote.get("operation_name") or "")):
            continue
        row = dict(quote)
        row["matched_operation"] = operation_name
        matched.append(row)

    after_outliers, outliers = _outlier_filter(matched)
    prices = [int(quote["price_rub"]) for quote in after_outliers if quote.get("price_rub") is not None]
    weak_average = round(sum(prices) / len(prices)) if prices else None
    enough_quotes = len(prices) >= MIN_CONFIDENT_QUOTES
    russia_average = weak_average if enough_quotes else None
    autostop_price = _round_to_100(russia_average * AUTOSTOP_MARKUP) if russia_average is not None else None

    missing_context = _operation_context_requirements(operation, vehicle_context)
    if len(prices) < MIN_CONFIDENT_QUOTES:
        missing_context.append("at_least_3_comparable_labor_only_public_prices")
    if not matched:
        missing_context.append("public_russia_labor_only_price_sample")

    source_count = len({quote.get("source") for quote in after_outliers if quote.get("source")})
    low_quote_confidence = any(quote.get("confidence") == "low" for quote in after_outliers)
    if not prices:
        confidence = "blocked"
    elif not enough_quotes or missing_context:
        confidence = "low"
    elif len(prices) >= 5 and source_count >= 3 and not low_quote_confidence:
        confidence = "high"
    else:
        confidence = "medium"

    return {
        "operation": operation_name,
        "category": operation.get("category"),
        "sample": {
            "valid_quotes": after_outliers,
            "valid_count": len(after_outliers),
            "raw_matched_count": len(matched),
            "excluded_outliers": outliers,
            "invalid_quotes": excluded,
            "source_count": source_count,
            "cities": sorted({quote.get("city_region") for quote in after_outliers if quote.get("city_region")}),
        },
        "weak_average_rub": weak_average,
        "russia_average_rub": russia_average,
        "autostop_price_rub": autostop_price,
        "confidence": confidence,
        "missing_context": _dedupe(missing_context),
    }


def _operation_labor_time_analysis(
    *,
    operation: dict[str, Any],
    labor_time_rows: list[dict[str, Any]],
    russia_average_rub: int | None,
    autostop_price_rub: int | None,
) -> dict[str, Any]:
    operation_name = str(operation.get("normalized_name") or operation.get("input") or "")
    matched: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for row in labor_time_rows:
        if not row.get("valid_input"):
            invalid.append(row)
            continue
        if not _operations_match(operation_name, str(row.get("operation_name") or "")):
            continue
        matched.append(row)

    ranges = [row["range_hours"] for row in matched if row.get("range_hours")]
    hours = [float(row["hours"]) for row in matched if row.get("hours") is not None]
    source_count = len({row.get("source") for row in matched if row.get("source")})
    average_hours = round(sum(hours) / len(hours), 2) if hours else None
    range_hours = [min(row[0] for row in ranges), max(row[1] for row in ranges)] if ranges else None

    if not matched:
        confidence = "blocked"
    elif len(matched) >= 2 and source_count >= 2 and not any(row.get("confidence") == "low" for row in matched):
        confidence = "high"
    elif len(matched) >= 1:
        confidence = "medium" if not all(row.get("confidence") == "low" for row in matched) else "low"
    else:
        confidence = "blocked"

    market_rate = round(russia_average_rub / average_hours) if russia_average_rub and average_hours else None
    autostop_rate = round(autostop_price_rub / average_hours) if autostop_price_rub and average_hours else None
    if average_hours is None:
        cross_check = "blocked"
    elif autostop_rate is not None and autostop_rate < 500:
        cross_check = "too_low"
    elif autostop_rate is not None and autostop_rate > 12_000:
        cross_check = "too_high"
    else:
        cross_check = "ok"

    return {
        "operation": operation_name,
        "sample": matched,
        "valid_count": len(matched),
        "invalid_count": len(invalid),
        "source_count": source_count,
        "range_hours": range_hours,
        "average_hours": average_hours,
        "confidence": confidence,
        "cross_check": cross_check,
        "effective_market_rate_rub_per_hour": market_rate,
        "autostop_effective_rate_rub_per_hour": autostop_rate,
        "rule": "public labor-time rows are a plausibility layer, not the primary price basis",
    }


def _attach_internal_experience(
    *,
    estimate: dict[str, Any],
    operation: dict[str, Any],
    experience_snapshot: dict[str, Any] | None,
) -> None:
    operation_name = str(operation.get("normalized_name") or operation.get("input") or "")
    matches = find_labor_experience(operation_name, snapshot=experience_snapshot, limit=3)
    exact = matches[0] if matches and float(matches[0].get("match_score") or 0) >= 0.8 else None
    estimate["internal_experience"] = {
        "available": exact is not None,
        "matches": matches,
        "selected": exact,
        "rule": "historical closed-order prices are an internal anchor, not a final quote",
    }

    public_price = estimate.get("autostop_price_rub")
    internal_price = exact.get("recommended_anchor_rub") if exact else None
    internal_count = int(exact.get("sample_count") or 0) if exact else 0
    recommended: int | None
    recommended_range: list[int | None] | None
    basis: str
    source_families: list[str] = []
    if public_price is not None:
        source_families.append("public_russia_sto_market")
    if internal_price is not None:
        source_families.append("internal_closed_repair_order_experience")

    if public_price is not None and internal_price is not None:
        assert exact is not None
        public_weight = max(1, min(5, int(estimate.get("sample", {}).get("valid_count") or 1)))
        internal_weight = max(1, min(10, internal_count))
        recommended = _round_to_100(
            (float(public_price) * public_weight + float(internal_price) * internal_weight)
            / (public_weight + internal_weight)
        )
        range_values = [
            float(value) for value in (exact.get("p25_rub"), exact.get("p75_rub"), public_price) if value is not None
        ]
        recommended_range = [
            _round_to_100(min(range_values)),
            _round_to_100(max(range_values)),
        ]
        basis = "weighted_public_market_and_internal_experience"
    elif internal_price is not None and internal_count >= 3:
        assert exact is not None
        recommended = _round_to_100(float(internal_price))
        recommended_range = [
            _round_to_100(float(exact.get("p25_rub") or internal_price)),
            _round_to_100(float(exact.get("p75_rub") or internal_price)),
        ]
        basis = "internal_experience_provisional"
    else:
        recommended = public_price
        recommended_range = [public_price, public_price] if public_price is not None else None
        basis = "public_market_only" if public_price is not None else "insufficient_evidence"

    estimate["recommended_price_rub"] = recommended
    estimate["recommended_range_rub"] = recommended_range
    estimate["recommendation_basis"] = basis
    estimate["evidence_source_families"] = source_families


def _finalize_evidence_confidence(
    estimate: dict[str, Any],
    labor_analysis: dict[str, Any],
) -> None:
    source_families = list(estimate.get("evidence_source_families") or [])
    if labor_analysis.get("valid_count"):
        source_families.append("vehicle_or_operation_labor_time")
    source_families = list(dict.fromkeys(source_families))
    estimate["evidence_source_families"] = source_families
    family_count = len(source_families)
    recommendation = estimate.get("recommended_price_rub")
    if recommendation is None:
        confidence = "blocked"
    elif family_count >= 3 and not estimate.get("missing_context"):
        confidence = "high"
    elif family_count >= 2:
        confidence = "medium"
    else:
        confidence = "low"
    estimate["decision_confidence"] = confidence
    estimate["evidence_bundle"] = {
        "source_family_count": family_count,
        "source_families": source_families,
        "high_confidence_requirement": "three independent source families plus exact vehicle/work context",
        "conflict_rule": "show a range and lower confidence when internal, market and labor-time evidence disagree",
    }


def _detect_overlap_adjustments(normalized_operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    adjustments: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for operation in normalized_operations:
        name = str(operation.get("normalized_name") or operation.get("input") or "")
        operation_key = _normalize_key(name)
        if operation_key in seen:
            adjustments.append(
                {
                    "type": "duplicate_operation",
                    "operations": [seen[operation_key], name],
                    "action": "do_not_price_twice",
                }
            )
        else:
            seen[operation_key] = name

    names = [
        str(operation.get("normalized_name") or operation.get("input") or "") for operation in normalized_operations
    ]
    for left in names:
        left_text = _clean_text(left)
        for right in names:
            if left == right:
                continue
            right_text = _clean_text(right)
            if "опорн" in left_text and any(token in right_text for token in ("стойк", "амортиз")):
                adjustments.append(
                    {
                        "type": "possible_included_remove_install",
                        "operations": [left, right],
                        "action": "verify_strut_remove_install_is_not_counted_twice",
                    }
                )
            if (
                "пыльник" in left_text
                and "шрус" in left_text
                and any(token in right_text for token in ("привод", "шрус"))
            ):
                adjustments.append(
                    {
                        "type": "possible_included_driveshaft_operation",
                        "operations": [left, right],
                        "action": "verify_cv_joint_or_driveshaft_remove_install_is_not_counted_twice",
                    }
                )
    unique: list[dict[str, Any]] = []
    seen_adjustments: set[tuple[str, tuple[str, ...]]] = set()
    for adjustment in adjustments:
        key = (str(adjustment.get("type")), tuple(sorted(str(item) for item in adjustment.get("operations", []))))
        if key in seen_adjustments:
            continue
        seen_adjustments.add(key)
        unique.append(adjustment)
    return unique


def _diagnostic_checklist(complaint: str | None, vehicle_context: dict[str, Any]) -> list[str]:
    text = _clean_text(complaint)
    checklist = [
        "Подтвердить жалобу на тест-драйве или при приемке.",
        "Считать ошибки и сохранить короткий список DTC/симптомов.",
        "Разделить диагностику, запчасти и финальный ремонт в смете.",
    ]
    if any(token in text for token in ("пина", "толч", "рыв", "dsg", "акпп", "кпп", "короб")):
        checklist.extend(
            [
                "Проверить коробку: код КПП, уровень/состояние масла, адаптации, ошибки TCM.",
                "Не оценивать мехатроник/ремонт КПП до результата диагностики.",
            ]
        )
    if not (vehicle_context.get("vin") or vehicle_context.get("chassis")):
        checklist.append("Запросить VIN или номер кузова перед точной технической сметой.")
    return checklist


def _pricing_totals(
    operation_estimates: list[dict[str, Any]],
    normalized_operations: list[dict[str, Any]],
    quote_sample: list[dict[str, Any]],
    labor_time_analysis: list[dict[str, Any]],
    *,
    complaint_only: bool,
) -> dict[str, Any]:
    confident_prices = [
        estimate["autostop_price_rub"]
        for estimate in operation_estimates
        if estimate.get("autostop_price_rub") is not None and estimate.get("confidence") in {"high", "medium"}
    ]
    all_operations_priced = bool(operation_estimates) and len(confident_prices) == len(operation_estimates)
    total_works_rub = int(sum(confident_prices)) if all_operations_priced else None
    russia_average_values = [
        estimate["russia_average_rub"]
        for estimate in operation_estimates
        if estimate.get("russia_average_rub") is not None
    ]
    russia_average_rub = int(sum(russia_average_values)) if all_operations_priced else None

    operation_confidences = [str(estimate.get("confidence")) for estimate in operation_estimates]
    if not normalized_operations:
        confidence = "blocked"
    elif all(conf == "high" for conf in operation_confidences):
        confidence = "high"
    elif all_operations_priced and all(conf in {"high", "medium"} for conf in operation_confidences):
        confidence = "medium"
    elif any(conf == "blocked" for conf in operation_confidences) and not quote_sample:
        confidence = "blocked"
    else:
        confidence = "low"
    if complaint_only:
        return {
            "all_operations_priced": all_operations_priced,
            "russia_average_rub": None,
            "autostop_price_rub": None,
            "total_works_rub": None,
            "confidence": "low",
        }
    if confidence == "high" and not any(item.get("confidence") in {"high", "medium"} for item in labor_time_analysis):
        confidence = "medium"
    return {
        "all_operations_priced": all_operations_priced,
        "russia_average_rub": russia_average_rub,
        "autostop_price_rub": total_works_rub,
        "total_works_rub": total_works_rub,
        "confidence": confidence,
    }


def _labor_time_summary(
    normalized_operations: list[dict[str, Any]],
    labor_time_sample: list[dict[str, Any]],
    labor_time_analysis: list[dict[str, Any]],
) -> dict[str, Any]:
    valid_rows = [row for row in labor_time_sample if row.get("valid_input")]
    operation_averages = [
        item["average_hours"] for item in labor_time_analysis if item.get("average_hours") is not None
    ]
    range_hours = None
    if operation_averages:
        ranges = [item["range_hours"] for item in labor_time_analysis if item.get("range_hours")]
        if ranges:
            range_hours = [
                round(sum(item[0] for item in ranges), 2),
                round(sum(item[1] for item in ranges), 2),
            ]

    if not normalized_operations:
        confidence = "blocked"
    elif len(operation_averages) == len(normalized_operations) and all(
        item.get("confidence") in {"high", "medium"} for item in labor_time_analysis
    ):
        confidence = "medium"
    elif valid_rows:
        confidence = "low"
    else:
        confidence = "blocked"

    cross_checks = {str(item.get("cross_check")) for item in labor_time_analysis if item.get("cross_check")}
    if cross_checks.intersection({"too_high", "too_low"}):
        cross_check = "needs_review"
    elif cross_checks == {"ok"}:
        cross_check = "ok"
    elif valid_rows:
        cross_check = "partial"
    else:
        cross_check = "blocked"
    return {
        "valid_rows": valid_rows,
        "operation_averages": operation_averages,
        "average_hours": round(sum(operation_averages), 2) if operation_averages else None,
        "range_hours": range_hours,
        "confidence": confidence,
        "cross_check": cross_check,
    }


def _pricing_next_actions(
    *,
    complaint_only: bool,
    complaint: str | None,
    vehicle_context: dict[str, Any],
    operation_estimates: list[dict[str, Any]],
    normalized_operations: list[dict[str, Any]],
    labor_time_confidence: str,
    overlap_adjustments: list[dict[str, Any]],
) -> list[str]:
    next_actions: list[str] = []
    if complaint_only:
        next_actions.append("Оценить только диагностику; финальный ремонт считать после результата диагностики.")
        next_actions.extend(_diagnostic_checklist(complaint, vehicle_context))
    if any(estimate.get("autostop_price_rub") is None for estimate in operation_estimates):
        next_actions.append("Собрать минимум 3 сопоставимые публичные labor-only цены СТО по России.")
    if labor_time_confidence == "blocked" and normalized_operations:
        next_actions.append(
            "Автоматически найти публичные нормо-часы/трудоемкость не удалось; использовать цену как рыночную оценку без второго слоя."
        )
    if overlap_adjustments:
        next_actions.append("Проверить пересечения работ при оформлении ЗН, чтобы не считать одну операцию дважды.")
    if any(operation.get("safety_flags") for operation in normalized_operations):
        next_actions.append(
            "Для safety-critical работ сверить состав операции по VIN/OEM или профессиональному service-source."
        )
    if not next_actions:
        next_actions.append(
            "Перед записью в ЗН проверить, что цена без запчастей и операция совпадает с фактической работой."
        )
    return next_actions


def estimate_repair_work_cost(
    *,
    vehicle: str | None = None,
    vin: str | None = None,
    chassis: str | None = None,
    make: str | None = None,
    model: str | None = None,
    year: int | str | None = None,
    engine: str | None = None,
    transmission: str | None = None,
    work_items: str | list[str] | tuple[str, ...] | None = None,
    complaint: str | None = None,
    city: str = "Красноярск",
    quotes_json: Any = None,
    auto_research: bool = True,
    labor_time_policy: str = "public_only",
    internal_experience_json: Any = None,
    use_internal_experience: bool = True,
) -> dict[str, Any]:
    """Build a read-only multi-source labor-price estimate."""

    vehicle_context = {
        "vehicle": vehicle,
        "vin": vin,
        "chassis": chassis,
        "make": make,
        "model": model,
        "year": year,
        "engine": engine,
        "transmission": transmission,
        "vehicle_class": None,
        "city": city,
    }
    vehicle_context["vehicle_class"] = _vehicle_class(vehicle_context)

    normalized_operations, complaint_only = _normalize_operations(_as_text_list(work_items), complaint)
    manual_quote_rows = _quote_rows(quotes_json)
    embedded_labor_time_rows = _labor_time_rows(quotes_json)
    experience_snapshot: dict[str, Any] | None
    if isinstance(internal_experience_json, dict):
        experience_snapshot = internal_experience_json
    elif use_internal_experience:
        experience_snapshot = load_service_labor_experience() or load_service_pricing_experience()
    else:
        experience_snapshot = None
    research = collect_public_work_pricing_research(
        vehicle_context=_research_vehicle_context(vehicle_context),
        operations=normalized_operations,
        city=city,
        auto_research=bool(auto_research and not manual_quote_rows),
        labor_time_policy=labor_time_policy,
    )
    quote_sample = [_normalize_quote(row) for row in [*manual_quote_rows, *research.get("quotes", [])]]
    labor_time_sample = [
        _normalize_labor_time_row(row) for row in [*embedded_labor_time_rows, *research.get("labor_time_sample", [])]
    ]

    missing_context: list[str] = []
    if not any((vehicle, make, model, vin, chassis)):
        missing_context.append("vehicle_identity")
    if not normalized_operations:
        missing_context.append("exact_work_items")

    operation_estimates = [
        _operation_estimate(operation=operation, quotes=quote_sample, vehicle_context=vehicle_context)
        for operation in normalized_operations
    ]
    for operation, estimate in zip(normalized_operations, operation_estimates, strict=False):
        _attach_internal_experience(
            estimate=estimate,
            operation=operation,
            experience_snapshot=experience_snapshot,
        )
    labor_time_analysis = [
        _operation_labor_time_analysis(
            operation=operation,
            labor_time_rows=labor_time_sample,
            russia_average_rub=estimate.get("russia_average_rub"),
            autostop_price_rub=estimate.get("autostop_price_rub"),
        )
        for operation, estimate in zip(normalized_operations, operation_estimates, strict=False)
    ]
    for estimate, labor_analysis in zip(operation_estimates, labor_time_analysis, strict=False):
        estimate["labor_time_analysis"] = labor_analysis
        _finalize_evidence_confidence(estimate, labor_analysis)

    for operation in operation_estimates:
        missing_context.extend(operation.get("missing_context", []))
    if normalized_operations and not any(item.get("valid_count") for item in labor_time_analysis):
        missing_context.append("public_labor_time_or_norm_hours_sample")
    if complaint_only:
        missing_context.extend(["confirmed_repair_work_items", "diagnostic_result_before_final_repair_estimate"])

    totals = _pricing_totals(
        operation_estimates,
        normalized_operations,
        quote_sample,
        labor_time_analysis,
        complaint_only=complaint_only,
    )

    valid_quotes = [
        quote for estimate in operation_estimates for quote in estimate.get("sample", {}).get("valid_quotes", [])
    ]
    excluded_outliers = [
        quote for estimate in operation_estimates for quote in estimate.get("sample", {}).get("excluded_outliers", [])
    ]
    invalid_quotes = [quote for quote in quote_sample if not quote.get("valid_input")]
    labor_summary = _labor_time_summary(normalized_operations, labor_time_sample, labor_time_analysis)
    overlap_adjustments = _detect_overlap_adjustments(normalized_operations)
    next_actions = _pricing_next_actions(
        complaint_only=complaint_only,
        complaint=complaint,
        vehicle_context=vehicle_context,
        operation_estimates=operation_estimates,
        normalized_operations=normalized_operations,
        labor_time_confidence=str(labor_summary["confidence"]),
        overlap_adjustments=overlap_adjustments,
    )

    manager_lines = [
        {
            "operation": estimate.get("operation"),
            "russia_average_rub": estimate.get("russia_average_rub"),
            "autostop_price_rub": estimate.get("autostop_price_rub"),
            "recommended_price_rub": estimate.get("recommended_price_rub"),
            "recommended_range_rub": estimate.get("recommended_range_rub"),
            "confidence": estimate.get("confidence"),
            "decision_confidence": estimate.get("decision_confidence"),
            "norm_hours": estimate.get("labor_time_analysis", {}).get("average_hours"),
            "source_families": estimate.get("evidence_source_families"),
        }
        for estimate in operation_estimates
    ]
    recommended_values = [
        int(estimate["recommended_price_rub"])
        for estimate in operation_estimates
        if estimate.get("recommended_price_rub") is not None
    ]
    recommended_total = (
        sum(recommended_values)
        if operation_estimates and len(recommended_values) == len(operation_estimates) and not complaint_only
        else None
    )
    decision_confidences = [str(estimate.get("decision_confidence")) for estimate in operation_estimates]
    if not decision_confidences or all(value == "blocked" for value in decision_confidences):
        decision_confidence = "blocked"
    elif all(value == "high" for value in decision_confidences):
        decision_confidence = "high"
    elif recommended_total is not None and all(value in {"high", "medium"} for value in decision_confidences):
        decision_confidence = "medium"
    else:
        decision_confidence = "low"

    return {
        "ok": True,
        "mode": "diagnostic_first" if complaint_only else "work_estimate",
        "read_only": True,
        "crm_write_allowed": False,
        "vehicle_context": _public_vehicle_context(vehicle_context),
        "normalized_operations": normalized_operations,
        "operation_estimates": operation_estimates,
        "labor_time_sample": {
            "rows": labor_summary["valid_rows"],
            "valid_count": len(labor_summary["valid_rows"]),
            "invalid_count": len([row for row in labor_time_sample if not row.get("valid_input")]),
            "sample_rules": [
                "public labor-time mentions only",
                "do not present public rows as official OEM norm-hours",
                "use as plausibility and overlap layer, not as the primary price basis",
            ],
        },
        "labor_time_analysis": labor_time_analysis,
        "labor_time_range_hours": labor_summary["range_hours"],
        "labor_time_average_hours": labor_summary["average_hours"],
        "labor_time_confidence": labor_summary["confidence"],
        "labor_time_cross_check": labor_summary["cross_check"],
        "overlap_adjustments": overlap_adjustments,
        "sources_checked": research.get("sources_checked", []),
        "pricing_basis": {
            "model": "adaptive_multi_source_evidence_bundle",
            "primary": "public_russia_sto_labor_only_prices",
            "secondary": "public_labor_time_plausibility_layer",
            "internal": "aggregate_only_closed_repair_order_experience",
            "market": "public_russia_sto_labor_only_prices",
            "labor_time": "vehicle_or_operation_labor_time_plausibility",
            "vehicle_context": "live_crm_or_verified_vehicle_identity",
            "labor_time_policy": labor_time_policy,
            "auto_research": bool(auto_research),
            "internal_experience_enabled": bool(use_internal_experience),
            "internal_experience_available": experience_snapshot is not None,
            "internal_experience_schema_version": (
                experience_snapshot.get("schema_version") if experience_snapshot else None
            ),
            "manual_owner_labor_time_required": False,
            "rule": "No single source creates a high-confidence final price; reconcile internal experience, current market, labor time and exact scope.",
        },
        "market_sample": {
            "quotes": valid_quotes,
            "valid_count": len(valid_quotes),
            "invalid_count": len(invalid_quotes),
            "excluded_outliers": excluded_outliers,
            "source_count": len({quote.get("source") for quote in valid_quotes if quote.get("source")}),
            "cities": sorted({quote.get("city_region") for quote in valid_quotes if quote.get("city_region")}),
            "sample_rules": [
                "labor-only prices only",
                "public Russia STO prices as primary basis",
                "comparable operation and vehicle class",
                "exclude outliers before arithmetic mean",
            ],
        },
        "russia_average_rub": totals["russia_average_rub"],
        "autostop_price_rub": totals["autostop_price_rub"],
        "total_works_rub": totals["total_works_rub"],
        "confidence": totals["confidence"],
        "recommended_total_works_rub": recommended_total,
        "decision_confidence": decision_confidence,
        "missing_context": _dedupe(missing_context),
        "next_actions": _dedupe(next_actions),
        "manager_summary": {
            "lines": manager_lines,
            "card_text_rule": "В карточку писать только короткий итог: работа, AutoStop цена, уверенность, что проверить.",
        },
        "formula": {
            "basis": "arithmetic_mean(valid_public_labor_only_russia_quotes_after_outlier_filter)",
            "markup_multiplier": AUTOSTOP_MARKUP,
            "rounding": "round_to_nearest_100_rub",
            "minimum_confident_quotes": MIN_CONFIDENT_QUOTES,
        },
        "warnings": [
            "Do not call replace_repair_order_works from this estimate.",
            "Parts, fluids, materials, and procurement markup are separate from labor pricing.",
            "Public labor-time rows are plausibility checks, not the primary price basis or official OEM norm-hours.",
            "Internal repair-order aggregates are historical anchors; verify current scope and market before a customer quote.",
        ],
        "privacy": {
            "raw_vehicle_identifier_redacted_from_output": bool(
                vehicle_context.get("vin") or vehicle_context.get("chassis")
            ),
            "raw_vehicle_identifier_removed_from_public_research_queries": bool(
                vehicle_context.get("vin") or vehicle_context.get("chassis")
            ),
        },
        "research": {
            "enabled": research.get("enabled", False),
            "policy": research.get("policy"),
            "access_mode": research.get("access_mode"),
            "search_queries": research.get("search_queries", {}),
            "warnings": research.get("warnings", []),
        },
        "playbook": "docs/agent/work_labor_pricing_playbook.md",
        "source_catalog": "docs/agent/labor_pricing_sources.json",
    }
