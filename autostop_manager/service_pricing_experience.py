from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import mean, median
from typing import Any

from .config import PROJECT_ROOT

DEFAULT_EXPERIENCE_PATH = PROJECT_ROOT / "data" / "private_knowledge" / "service_pricing_experience.json"
SNAPSHOT_SCHEMA_VERSION = "autostop_service_pricing_experience_v1"
MIN_BASELINE_SAMPLES = 3

_SPACE_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^0-9a-zа-яё]+", re.IGNORECASE)
_SIDE_WORDS_RE = re.compile(
    r"\b(?:лев(?:ый|ая|ое|ого|ой)?|прав(?:ый|ая|ое|ого|ой)?|передн(?:ий|яя|ее|его|ей)?|"
    r"задн(?:ий|яя|ее|его|ей)?|л\.?|п\.?)\b",
    re.IGNORECASE,
)
_GENERIC_WORDS = {
    "работа",
    "работы",
    "работ",
    "по",
    "и",
    "или",
    "на",
    "автомобиле",
    "автомобиля",
    "шт",
}


def _clean_text(value: Any) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()


def _fold_text(value: Any) -> str:
    return _clean_text(value).casefold().replace("ё", "е")


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    text = _clean_text(value).replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _money(value: Decimal | float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 2)


def _parse_crm_datetime(value: Any) -> datetime | None:
    text = _clean_text(value)
    if not text or text.casefold() == "нет даты":
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    for pattern in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 2)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 2)


def _round_to_100(value: float | None) -> int | None:
    if value is None:
        return None
    return int(round(value / 100.0) * 100)


def _operation_category(text: str) -> str:
    if any(token in text for token in ("диагност", "дефектов", "провер")):
        return "diagnostics"
    if any(token in text for token in ("масл", "фильтр", "техническ обслуж", " то ")):
        return "maintenance"
    if any(token in text for token in ("тормоз", "колод", "суппорт", "диск")):
        return "brakes"
    if any(token in text for token in ("подвес", "амортиз", "стойк", "рычаг", "сайлент", "ступиц", "шаров")):
        return "suspension"
    if any(token in text for token in ("акпп", "кпп", "dsg", "сцеплен", "короб", "мехатрон")):
        return "transmission"
    if any(token in text for token in ("двигател", "двс", "грм", "турбин", "форсунк", "свеч")):
        return "engine"
    if any(token in text for token in ("рулев", "рейк", "гур")):
        return "steering"
    if any(token in text for token in ("кондицион", "климат", "фреон")):
        return "climate"
    if any(token in text for token in ("электр", "генератор", "стартер", "сигнализац", "провод")):
        return "electrical"
    if any(token in text for token in ("шиномонтаж", "колес", "шина", "баланс")):
        return "wheels"
    return "other"


def canonicalize_work_name(value: Any) -> dict[str, str]:
    raw = _fold_text(value)
    text = re.sub(r"^\s*\d+[.)-]?\s*", "", raw)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = _SPACE_RE.sub(" ", text).strip(" .,:;/-")
    category = _operation_category(f" {text} ")

    rules: list[tuple[str, str, str]] = [
        (r"диагност.*(?:ходов|подвес)", "диагностика подвески", "diagnostics"),
        (r"(?:компьютерн|электронн).*диагност|диагност.*(?:скан|ошиб)", "компьютерная диагностика", "diagnostics"),
        (r"диагност.*(?:акпп|кпп|короб|dsg)", "диагностика трансмиссии", "diagnostics"),
        (r"диагност", "диагностика", "diagnostics"),
        (r"замен.*масл.*(?:двс|двигател)|\bто\b.*(?:масл|фильтр)", "замена масла двс", "maintenance"),
        (r"замен.*масл.*(?:акпп|кпп|короб|dsg)", "замена масла трансмиссии", "maintenance"),
        (r"замен.*масл.*(?:раздат|редукт)", "замена масла раздатка редуктор", "maintenance"),
        (r"замен.*(?:воздушн).*фильтр", "замена воздушного фильтра", "maintenance"),
        (r"замен.*(?:салонн).*фильтр", "замена салонного фильтра", "maintenance"),
        (r"замен.*(?:топливн).*фильтр", "замена топливного фильтра", "maintenance"),
        (r"замен.*(?:маслян).*фильтр", "замена масляного фильтра", "maintenance"),
        (r"замен.*(?:диск.*колод|колод.*диск).*перед", "замена передних тормозных дисков и колодок", "brakes"),
        (r"замен.*(?:диск.*колод|колод.*диск).*зад", "замена задних тормозных дисков и колодок", "brakes"),
        (r"замен.*колод.*перед", "замена передних тормозных колодок", "brakes"),
        (r"замен.*колод.*зад", "замена задних тормозных колодок", "brakes"),
        (r"замен.*колод", "замена тормозных колодок", "brakes"),
        (r"замен.*тормоз.*диск.*перед", "замена передних тормозных дисков", "brakes"),
        (r"замен.*тормоз.*диск.*зад", "замена задних тормозных дисков", "brakes"),
        (r"обслуж.*суппорт|профилак.*суппорт", "обслуживание тормозных суппортов", "brakes"),
        (r"замен.*(?:стойк|втул).*стабилиз", "замена элементов стабилизатора", "suspension"),
        (r"замен.*ступич.*подшип|замен.*подшип.*ступиц", "замена ступичного подшипника", "suspension"),
        (r"замен.*амортиз|замен.*стойк.*амортиз", "замена амортизатора", "suspension"),
        (r"замен.*сайлент", "замена сайлентблока", "suspension"),
        (r"замен.*рычаг", "замена рычага подвески", "suspension"),
        (r"замен.*шаров", "замена шаровой опоры", "suspension"),
        (r"замен.*сцеплен", "замена сцепления", "transmission"),
        (
            r"(?:снят|демонтаж|установ).*(?:акпп|кпп|короб)|"
            r"замен\w*\s+(?:акпп|кпп|короб(?:к[аиу])?)(?:\s|$)",
            "снятие установка трансмиссии",
            "transmission",
        ),
        (r"замен.*мехатрон", "замена мехатроника", "transmission"),
        (r"замен.*грм", "замена грм", "engine"),
        (r"замен.*свеч", "замена свечей зажигания", "engine"),
        (r"замен.*двс|замен.*двигател", "снятие установка двигателя", "engine"),
        (r"замен.*турбин|снят.*турбин|установ.*турбин", "снятие установка турбины", "engine"),
        (r"замен.*рулев.*рейк", "замена рулевой рейки", "steering"),
        (r"заправ.*кондицион", "заправка кондиционера", "climate"),
        (r"ремонт.*кондицион", "ремонт кондиционера", "climate"),
        (r"шиномонтаж", "шиномонтаж", "wheels"),
    ]
    for pattern, canonical, rule_category in rules:
        if re.search(pattern, text):
            return {
                "key": _NON_WORD_RE.sub("_", canonical).strip("_"),
                "name": canonical,
                "category": rule_category,
            }

    reduced = _SIDE_WORDS_RE.sub(" ", text)
    tokens = [
        token for token in _NON_WORD_RE.sub(" ", reduced).split() if len(token) >= 2 and token not in _GENERIC_WORDS
    ]
    canonical = " ".join(tokens[:12]) or "прочая работа"
    return {
        "key": _NON_WORD_RE.sub("_", canonical).strip("_"),
        "name": canonical,
        "category": category,
    }


def _line_observation(row: dict[str, Any]) -> tuple[float | None, float | None, list[str]]:
    reasons: list[str] = []
    quantity = _decimal(row.get("quantity"))
    price = _decimal(row.get("price"))
    total = _decimal(row.get("total"))
    if quantity is None or quantity <= 0:
        reasons.append("invalid_quantity")
        quantity = Decimal(1)
    if total is not None and total > 0:
        unit_price = total / quantity
        if price is not None and price > 0:
            delta = abs((price * quantity) - total)
            if delta > max(Decimal(1), total * Decimal("0.02")):
                reasons.append("price_total_mismatch")
    elif price is not None and price > 0:
        unit_price = price
        total = price * quantity
        reasons.append("total_derived_from_price")
    else:
        return None, _money(quantity), [*reasons, "zero_or_missing_price"]
    return _money(unit_price), _money(quantity), reasons


def _sample_confidence(count: int) -> str:
    if count >= 10:
        return "high"
    if count >= 5:
        return "medium"
    if count >= MIN_BASELINE_SAMPLES:
        return "emerging"
    return "anecdotal"


def _stats(values: list[float]) -> dict[str, Any]:
    return {
        "sample_count": len(values),
        "min_rub": _money(min(values)) if values else None,
        "p25_rub": _money(_quantile(values, 0.25)),
        "median_rub": _money(median(values)) if values else None,
        "p75_rub": _money(_quantile(values, 0.75)),
        "max_rub": _money(max(values)) if values else None,
        "mean_rub": _money(mean(values)) if values else None,
        "recommended_anchor_rub": _round_to_100(float(median(values))) if values else None,
        "confidence": _sample_confidence(len(values)),
    }


def _vehicle_segment(vehicle: Any) -> str:
    text = _fold_text(vehicle)
    if any(token in text for token in ("камаз", "газель", "isuzu", "iveco", "man ", "mercedes-benz 223")):
        return "commercial"
    if any(token in text for token in ("porsche", "lamborghini", "maybach", "bmw", "mercedes", "audi", "land rover")):
        return "premium"
    return "passenger"


@dataclass(frozen=True)
class _OrderRef:
    source_index: int
    closed_at: datetime
    card: dict[str, Any]


def _latest_closed_orders(state: dict[str, Any], limit: int) -> list[_OrderRef]:
    rows: list[_OrderRef] = []
    for index, card in enumerate(state.get("cards") or []):
        if not isinstance(card, dict):
            continue
        repair_order = card.get("repair_order")
        if not isinstance(repair_order, dict) or repair_order.get("status") != "closed":
            continue
        closed_at = _parse_crm_datetime(repair_order.get("closed_at"))
        if closed_at is None:
            continue
        rows.append(_OrderRef(source_index=index, closed_at=closed_at, card=card))
    rows.sort(key=lambda item: (item.closed_at, item.source_index), reverse=True)
    return rows[: max(0, limit)]


def build_service_pricing_experience(
    state: dict[str, Any],
    *,
    limit: int = 100,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    selected = _latest_closed_orders(state, limit)
    work_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    part_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    quality: Counter[str] = Counter()
    segment_counts: Counter[str] = Counter()

    for order_position, order_ref in enumerate(selected):
        card = order_ref.card
        repair_order = card.get("repair_order") or {}
        segment = _vehicle_segment(repair_order.get("vehicle") or card.get("vehicle"))
        segment_counts[segment] += 1
        works_value = repair_order.get("works")
        materials_value = repair_order.get("materials")
        works: list[Any] = works_value if isinstance(works_value, list) else []
        materials: list[Any] = materials_value if isinstance(materials_value, list) else []
        if not works:
            quality["orders_without_work_rows"] += 1
        if not materials:
            quality["orders_without_material_rows"] += 1

        for row in works:
            quality["work_rows_total"] += 1
            if not isinstance(row, dict):
                quality["work_rows_invalid_structure"] += 1
                continue
            name = _clean_text(row.get("name"))
            unit_price, quantity, reasons = _line_observation(row)
            if not name:
                quality["work_rows_missing_name"] += 1
            if unit_price is None:
                quality["work_rows_invalid_price"] += 1
            if reasons:
                quality["work_rows_with_quality_flags"] += 1
                for reason in reasons:
                    quality[f"work_flag_{reason}"] += 1
            if not name or unit_price is None:
                continue
            canonical = canonicalize_work_name(name)
            work_samples[canonical["key"]].append(
                {
                    "canonical_name": canonical["name"],
                    "category": canonical["category"],
                    "variant": _fold_text(name),
                    "unit_price_rub": unit_price,
                    "quantity": quantity,
                    "order_position": order_position,
                    "closed_at": order_ref.closed_at.date().isoformat(),
                    "vehicle_segment": segment,
                }
            )
            quality["work_rows_valid"] += 1

        for row in materials:
            quality["material_rows_total"] += 1
            if not isinstance(row, dict):
                quality["material_rows_invalid_structure"] += 1
                continue
            catalog_number = re.sub(r"[^0-9A-ZА-Я]+", "", _clean_text(row.get("catalog_number")).upper())
            name = _clean_text(row.get("name"))
            sale_price, quantity, reasons = _line_observation(row)
            cost_price = _decimal(row.get("cost_price"))
            if not catalog_number:
                quality["material_rows_without_catalog_number"] += 1
            if sale_price is None:
                quality["material_rows_invalid_sale_price"] += 1
            if reasons:
                quality["material_rows_with_quality_flags"] += 1
            if not catalog_number or not name or sale_price is None:
                continue
            part_samples[catalog_number].append(
                {
                    "catalog_number": catalog_number,
                    "part_name": _fold_text(name),
                    "sale_unit_price_rub": sale_price,
                    "cost_unit_price_rub": _money(cost_price) if cost_price is not None and cost_price > 0 else None,
                    "quantity": quantity,
                    "closed_at": order_ref.closed_at.date().isoformat(),
                }
            )
            quality["material_rows_valid_for_article_reference"] += 1

    labor_baselines: list[dict[str, Any]] = []
    for key, rows in work_samples.items():
        values = [float(row["unit_price_rub"]) for row in rows]
        variants = Counter(str(row["variant"]) for row in rows)
        segments = Counter(str(row["vehicle_segment"]) for row in rows)
        latest = max(str(row["closed_at"]) for row in rows)
        baseline = {
            "operation_key": key,
            "operation_name": rows[0]["canonical_name"],
            "category": rows[0]["category"],
            **_stats(values),
            "latest_closed_date": latest,
            "vehicle_segment_counts": dict(sorted(segments.items())),
            "observed_variants": [{"name": name, "count": count} for name, count in variants.most_common(5)],
            "use_rule": "historical_internal_anchor_not_final_price",
        }
        labor_baselines.append(baseline)
    labor_baselines.sort(key=lambda row: (-int(row["sample_count"]), str(row["operation_name"])))

    part_price_references: list[dict[str, Any]] = []
    for catalog_number, rows in part_samples.items():
        sale_values = [float(row["sale_unit_price_rub"]) for row in rows]
        cost_values = [float(row["cost_unit_price_rub"]) for row in rows if row.get("cost_unit_price_rub") is not None]
        names = Counter(str(row["part_name"]) for row in rows)
        part_price_references.append(
            {
                "catalog_number": catalog_number,
                "part_name": names.most_common(1)[0][0],
                "sale_price": _stats(sale_values),
                "cost_price": _stats(cost_values) if cost_values else None,
                "latest_closed_date": max(str(row["closed_at"]) for row in rows),
                "use_rule": "historical_sale_reference_verify_live_stock_supplier_and_fitment",
            }
        )
    part_price_references.sort(key=lambda row: (-int(row["sale_price"]["sample_count"]), str(row["catalog_number"])))

    generated = (generated_at or datetime.now(UTC)).astimezone(UTC)
    dates = [item.closed_at.date().isoformat() for item in selected]
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": generated.isoformat(),
        "privacy": {
            "aggregate_only": True,
            "contains_order_ids": False,
            "contains_client_identity": False,
            "contains_phone_vin_or_plate": False,
            "contains_payment_rows": False,
            "raw_repair_orders_persisted": False,
        },
        "scope": {
            "requested_latest_closed_orders": limit,
            "selected_closed_orders": len(selected),
            "closed_date_from": min(dates) if dates else None,
            "closed_date_to": max(dates) if dates else None,
            "currency": "RUB",
            "labor_price_basis": "CRM work-row unit price before separately displayed order tax",
            "part_price_basis": "CRM material-row unit sale/cost price before separately displayed order tax",
            "vehicle_segment_counts": dict(sorted(segment_counts.items())),
        },
        "data_quality": dict(sorted(quality.items())),
        "labor_baselines": labor_baselines,
        "part_price_references": part_price_references,
        "decision_policy": {
            "high_confidence_requires_independent_source_families": 3,
            "source_families": [
                "internal_closed_repair_order_experience",
                "vehicle_specific_norm_hours_or_service_data",
                "current_public_or_supplier_market",
                "live_crm_vehicle_and_scope_context",
            ],
            "labor_rule": "Internal median and interquartile range are historical anchors. Recheck exact scope, vehicle, AUTONORMS/OEM labor time, overlap and current market before final price.",
            "parts_rule": "Historical article prices are stale references. Verify current AutoStop App stock/cost, supplier offers, public market and VIN/OEM applicability.",
        },
    }


def build_service_pricing_experience_from_state_file(
    state_path: str | Path,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    payload = json.loads(Path(state_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("CRM state must be a JSON object")
    return build_service_pricing_experience(payload, limit=limit)


def save_service_pricing_experience(
    snapshot: dict[str, Any],
    output_path: str | Path = DEFAULT_EXPERIENCE_PATH,
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load_service_pricing_experience(
    path: str | Path = DEFAULT_EXPERIENCE_PATH,
) -> dict[str, Any] | None:
    source = Path(path)
    if not source.exists():
        return None
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        return None
    return payload


def _tokens(value: Any) -> set[str]:
    return {
        token
        for token in _NON_WORD_RE.sub(" ", _fold_text(value)).split()
        if len(token) >= 3 and token not in _GENERIC_WORDS
    }


def find_labor_experience(
    operation_name: str,
    *,
    snapshot: dict[str, Any] | None = None,
    path: str | Path = DEFAULT_EXPERIENCE_PATH,
    limit: int = 3,
) -> list[dict[str, Any]]:
    source = snapshot if snapshot is not None else load_service_pricing_experience(path)
    if not source:
        return []
    canonical = canonicalize_work_name(operation_name)
    query_tokens = _tokens(canonical["name"])
    matches: list[tuple[float, dict[str, Any]]] = []
    for row in source.get("labor_baselines") or []:
        if not isinstance(row, dict):
            continue
        if row.get("operation_key") == canonical["key"]:
            score = 1.0
        else:
            row_tokens = _tokens(row.get("operation_name"))
            union = query_tokens | row_tokens
            score = (len(query_tokens & row_tokens) / len(union)) if union else 0.0
            if row.get("category") == canonical["category"]:
                score += 0.1
        if score >= 0.45:
            public = {key: value for key, value in row.items() if key not in {"observed_variants"}}
            public["match_score"] = round(min(score, 1.0), 3)
            matches.append((score, public))
    matches.sort(
        key=lambda item: (
            -item[0],
            -int(item[1].get("sample_count") or 0),
        )
    )
    return [item for _, item in matches[: max(0, limit)]]


def summarize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    baselines = snapshot.get("labor_baselines") or []
    parts = snapshot.get("part_price_references") or []
    reusable = [row for row in baselines if int(row.get("sample_count") or 0) >= MIN_BASELINE_SAMPLES]
    return {
        "schema_version": snapshot.get("schema_version"),
        "scope": snapshot.get("scope"),
        "data_quality": snapshot.get("data_quality"),
        "labor_operation_groups": len(baselines),
        "reusable_labor_baselines": len(reusable),
        "article_price_references": len(parts),
        "top_labor_baselines": reusable[:15],
        "privacy": snapshot.get("privacy"),
    }
