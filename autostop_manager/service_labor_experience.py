from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import mean, median
from typing import Any
from zoneinfo import ZoneInfo

from .config import PROJECT_ROOT
from .service_pricing_experience import canonicalize_work_name

DEFAULT_LABOR_EXPERIENCE_PATH = PROJECT_ROOT / "data" / "private_knowledge" / "service_labor_experience.json"
DEFAULT_EXECUTOR_REPORT_PATH = (
    PROJECT_ROOT / "data" / "private_knowledge" / "restricted" / "service_labor_executor_report.json"
)
DEFAULT_LABOR_REPORT_PATH = PROJECT_ROOT / "data" / "private_knowledge" / "reports" / "service_labor_analysis.md"
LABOR_SNAPSHOT_SCHEMA_VERSION = "autostop_service_labor_experience_v1"
EXECUTOR_REPORT_SCHEMA_VERSION = "autostop_service_labor_executor_report_v1"
DEFAULT_RECENCY_HALF_LIFE_DAYS = 90
CRM_TIMEZONE = ZoneInfo("Asia/Krasnoyarsk")

_SPACE_RE = re.compile(r"\s+")
_SAFE_KEY_RE = re.compile(r"[^0-9a-zа-яё]+", re.IGNORECASE)
_LABOR_ALIASES: tuple[tuple[str, str, str], ...] = (
    (r"^диагностика (?:ходовой|подвески)$", "диагностика подвески", "diagnostics"),
    (r"^компьютерная диагностика$", "компьютерная диагностика", "diagnostics"),
    (r"^замена масл[ао]? (?:в )?(?:двс|двигател[ья])(?: с масляным фильтром)?$", "замена масла двс", "maintenance"),
    (r"^замена воздушного фильтра$", "замена воздушного фильтра", "maintenance"),
    (r"^замена салонного фильтра$", "замена салонного фильтра", "maintenance"),
    (r"^замена топливного фильтра$", "замена топливного фильтра", "maintenance"),
    (r"^развал схождение$", "развал схождение", "other"),
    (r"^замена передних тормозных колодок$", "замена передних тормозных колодок", "brakes"),
    (r"^замена задних тормозных колодок$", "замена задних тормозных колодок", "brakes"),
    (r"^замена тормозных колодок$", "замена тормозных колодок", "brakes"),
    (
        r"^замена передних тормозных (?:дисков и колодок|дисков колодок)$",
        "замена передних тормозных дисков и колодок",
        "brakes",
    ),
    (
        r"^замена задних тормозных (?:дисков и колодок|дисков колодок)$",
        "замена задних тормозных дисков и колодок",
        "brakes",
    ),
    (r"^доставка(?: .*)?$", "доставка", "other"),
)


def _clean_text(value: Any) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()


def _fold_text(value: Any) -> str:
    return _clean_text(value).casefold().replace("ё", "е")


def canonicalize_labor_name(value: Any) -> dict[str, str]:
    text = _fold_text(value)
    text = re.sub(r"^\s*\d+[.)-]?\s*", "", text)
    text = _SPACE_RE.sub(" ", _SAFE_KEY_RE.sub(" ", text)).strip()
    for pattern, canonical_name, category in _LABOR_ALIASES:
        if re.fullmatch(pattern, text):
            return {
                "key": _SAFE_KEY_RE.sub("_", canonical_name).strip("_"),
                "name": canonical_name,
                "category": category,
            }
    fallback = canonicalize_work_name(text)
    canonical_name = " ".join(text.split()[:20]) or "прочая работа"
    return {
        "key": _SAFE_KEY_RE.sub("_", canonical_name).strip("_"),
        "name": canonical_name,
        "category": fallback["category"],
    }


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
    return None if value is None else round(float(value), 2)


def _round_to_100(value: float | None) -> int | None:
    return None if value is None else int(round(value / 100.0) * 100)


def _parse_closed_at(value: Any) -> datetime | None:
    text = _clean_text(value)
    if not text or text.casefold() == "нет даты":
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CRM_TIMEZONE)
        return parsed.astimezone(UTC)
    for pattern in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=CRM_TIMEZONE).astimezone(UTC)
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
    upper_weight = position - lower
    return round(ordered[lower] * (1 - upper_weight) + ordered[upper] * upper_weight, 2)


def _weighted_quantile(values: list[tuple[float, float]], probability: float) -> float | None:
    usable = sorted((value, weight) for value, weight in values if weight > 0)
    if not usable:
        return None
    total_weight = sum(weight for _, weight in usable)
    threshold = total_weight * probability
    cumulative = 0.0
    for value, weight in usable:
        cumulative += weight
        if cumulative >= threshold:
            return round(value, 2)
    return round(usable[-1][0], 2)


def _confidence(count: int) -> str:
    if count >= 10:
        return "stable"
    if count >= 5:
        return "working"
    if count >= 3:
        return "preliminary"
    return "context_only"


def _vehicle_segment(vehicle: Any) -> str:
    text = f" {_fold_text(vehicle)} "
    if any(token in text for token in ("камаз", "газель", "isuzu", "iveco", " man ", "груз")):
        return "commercial"
    if any(token in text for token in ("porsche", "maybach", "bmw", "mercedes", "audi", "land rover", "lexus")):
        return "premium"
    return "passenger"


def _line_observation(row: dict[str, Any]) -> tuple[float | None, float | None, list[str]]:
    reasons: list[str] = []
    quantity = _decimal(row.get("quantity"))
    price = _decimal(row.get("price"))
    total = _decimal(row.get("total"))
    if quantity is None or quantity <= 0:
        return None, _money(quantity), ["invalid_quantity"]
    if total is None or total <= 0:
        return None, _money(quantity), ["zero_or_missing_total"]
    unit_price = total / quantity
    if price is not None and price > 0:
        delta = abs((price * quantity) - total)
        if delta > max(Decimal(1), total * Decimal("0.02")):
            reasons.append("price_total_mismatch")
    return _money(unit_price), _money(quantity), reasons


def _outlier_mask(values: list[float]) -> list[bool]:
    if len(values) < 5:
        return [False] * len(values)
    q1 = _quantile(values, 0.25)
    q3 = _quantile(values, 0.75)
    if q1 is None or q3 is None:
        return [False] * len(values)
    iqr = q3 - q1
    if iqr > 0:
        lower = q1 - 1.5 * iqr
        upper = q3 + 1.5 * iqr
        return [value < lower or value > upper for value in values]
    center = float(median(values))
    if center <= 0:
        return [False] * len(values)
    return [value < center / 3 or value > center * 3 for value in values]


def _recency_weight(closed_at: datetime | None, generated_at: datetime, half_life_days: int) -> float:
    if closed_at is None:
        return 0.0
    age_days = max(0.0, (generated_at - closed_at).total_seconds() / 86_400)
    return 0.5 ** (age_days / half_life_days)


def _aggregate_stats(
    rows: list[dict[str, Any]],
    *,
    generated_at: datetime,
    half_life_days: int,
) -> dict[str, Any]:
    values = [float(row["unit_price_rub"]) for row in rows]
    outlier_flags = _outlier_mask(values)
    inlier_rows = [row for row, is_outlier in zip(rows, outlier_flags, strict=True) if not is_outlier]
    inlier_values = [float(row["unit_price_rub"]) for row in inlier_rows]
    weighted_values = [
        (
            float(row["unit_price_rub"]),
            _recency_weight(row.get("closed_at"), generated_at, half_life_days),
        )
        for row in inlier_rows
    ]
    weighted_median = _weighted_quantile(weighted_values, 0.5)
    unweighted_median = _money(median(inlier_values)) if inlier_values else None
    anchor = weighted_median if weighted_median is not None else unweighted_median
    return {
        "sample_count": len(rows),
        "inlier_sample_count": len(inlier_rows),
        "outlier_count": sum(outlier_flags),
        "min_rub": _money(min(values)) if values else None,
        "p25_rub": _money(_quantile(inlier_values, 0.25)),
        "median_rub": unweighted_median,
        "weighted_median_rub": _money(weighted_median),
        "p75_rub": _money(_quantile(inlier_values, 0.75)),
        "max_rub": _money(max(values)) if values else None,
        "mean_rub": _money(mean(inlier_values)) if inlier_values else None,
        "recommended_anchor_rub": _round_to_100(anchor),
        "confidence": _confidence(len(inlier_rows)),
    }


def _executor_identity(row: dict[str, Any]) -> tuple[str, str]:
    executor_id = _clean_text(row.get("work_executor_id_snapshot")) or _clean_text(row.get("executor_id"))
    executor_name = _clean_text(row.get("work_executor_name_snapshot")) or _clean_text(row.get("executor_name"))
    if not executor_id and executor_name:
        executor_id = f"name:{_SAFE_KEY_RE.sub('_', _fold_text(executor_name)).strip('_')}"
    return executor_id, executor_name


def _closed_cards(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        card
        for card in state.get("cards") or []
        if isinstance(card, dict)
        and isinstance(card.get("repair_order"), dict)
        and card["repair_order"].get("status") == "closed"
    ]


def _build_baselines(
    samples: dict[str, list[dict[str, Any]]],
    *,
    generated_at: datetime,
    half_life_days: int,
) -> list[dict[str, Any]]:
    baselines: list[dict[str, Any]] = []
    for operation_key, rows in samples.items():
        segments = Counter(str(row["vehicle_segment"]) for row in rows)
        dated = [row["closed_at"] for row in rows if row.get("closed_at") is not None]
        segment_baselines = []
        for segment, segment_count in sorted(segments.items()):
            if segment_count < 5:
                continue
            segment_rows = [row for row in rows if row["vehicle_segment"] == segment]
            segment_baselines.append(
                {
                    "vehicle_segment": segment,
                    **_aggregate_stats(
                        segment_rows,
                        generated_at=generated_at,
                        half_life_days=half_life_days,
                    ),
                }
            )
        monthly_baselines = []
        monthly_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            closed_at = row.get("closed_at")
            if isinstance(closed_at, datetime):
                monthly_rows[closed_at.astimezone(CRM_TIMEZONE).strftime("%Y-%m")].append(row)
        for month, month_rows in sorted(monthly_rows.items()):
            if len(month_rows) < 3:
                continue
            monthly_baselines.append(
                {
                    "month": month,
                    **_aggregate_stats(
                        month_rows,
                        generated_at=generated_at,
                        half_life_days=half_life_days,
                    ),
                }
            )
        baselines.append(
            {
                "operation_key": operation_key,
                "operation_name": rows[0]["canonical_name"],
                "category": rows[0]["category"],
                **_aggregate_stats(rows, generated_at=generated_at, half_life_days=half_life_days),
                "latest_closed_date": (max(dated).astimezone(CRM_TIMEZONE).date().isoformat() if dated else None),
                "vehicle_segment_counts": dict(sorted(segments.items())),
                "vehicle_segment_baselines": segment_baselines,
                "monthly_baselines": monthly_baselines,
                "recency_half_life_days": half_life_days,
                "use_rule": "historical_internal_anchor_not_final_price",
            }
        )
    baselines.sort(key=lambda row: (-int(row["sample_count"]), str(row["operation_name"])))
    return baselines


def _build_executor_rows(
    executor_samples: dict[str, list[dict[str, Any]]],
    *,
    executor_names: dict[str, str],
    executor_row_counts: Counter[str],
    generated_at: datetime,
    half_life_days: int,
) -> list[dict[str, Any]]:
    executors: list[dict[str, Any]] = []
    for executor_id in executor_row_counts:
        valid_rows = executor_samples.get(executor_id, [])
        operations: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in valid_rows:
            operations[str(row["operation_key"])].append(row)
        top_operations: list[dict[str, Any]] = []
        for operation_rows in operations.values():
            stats = _aggregate_stats(
                operation_rows,
                generated_at=generated_at,
                half_life_days=half_life_days,
            )
            top_operations.append(
                {
                    "operation_name": operation_rows[0]["canonical_name"],
                    "sample_count": stats["sample_count"],
                    "median_rub": stats["median_rub"],
                    "weighted_median_rub": stats["weighted_median_rub"],
                }
            )
        top_operations.sort(key=lambda row: (-int(row["sample_count"]), str(row["operation_name"])))
        total_rows = int(executor_row_counts[executor_id])
        stats = _aggregate_stats(valid_rows, generated_at=generated_at, half_life_days=half_life_days)
        executors.append(
            {
                "executor_id": executor_id,
                "executor_name": executor_names.get(executor_id) or None,
                "work_rows_total": total_rows,
                "valid_price_rows": len(valid_rows),
                "incomplete_price_rows": max(0, total_rows - len(valid_rows)),
                "valid_price_share": round(len(valid_rows) / total_rows, 4) if total_rows else 0.0,
                "price_distribution": stats,
                "top_operations": top_operations[:15],
            }
        )
    executors.sort(key=lambda row: (-int(row["work_rows_total"]), str(row.get("executor_name") or "")))
    return executors


def build_service_labor_experience(
    state: dict[str, Any],
    *,
    generated_at: datetime | None = None,
    source_sha256: str | None = None,
    recency_half_life_days: int = DEFAULT_RECENCY_HALF_LIFE_DAYS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if recency_half_life_days <= 0:
        raise ValueError("recency_half_life_days must be positive")
    generated = (generated_at or datetime.now(UTC)).astimezone(UTC)
    cards = _closed_cards(state)
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    executor_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    executor_names: dict[str, str] = {}
    executor_row_counts: Counter[str] = Counter()
    segment_counts: Counter[str] = Counter()
    quality: Counter[str] = Counter()
    closed_dates: list[datetime] = []

    for card in cards:
        repair_order = card["repair_order"]
        closed_at = _parse_closed_at(repair_order.get("closed_at"))
        if closed_at is None:
            quality["closed_orders_missing_or_invalid_closed_at"] += 1
        else:
            closed_dates.append(closed_at)
        segment = _vehicle_segment(repair_order.get("vehicle") or card.get("vehicle"))
        segment_counts[segment] += 1
        works_value = repair_order.get("works")
        works: list[Any] = works_value if isinstance(works_value, list) else []
        if not works:
            quality["closed_orders_without_work_rows"] += 1
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
                quality["work_rows_invalid_price_or_quantity"] += 1
            if reasons:
                quality["work_rows_with_quality_flags"] += 1
                for reason in reasons:
                    quality[f"work_flag_{reason}"] += 1

            executor_id, executor_name = _executor_identity(row)
            executor_key = executor_id or "__unassigned__"
            executor_row_counts[executor_key] += 1
            if executor_name:
                executor_names[executor_key] = executor_name
            if not executor_id:
                quality["work_rows_without_executor"] += 1

            if not name or unit_price is None:
                quality["work_rows_excluded_from_price_baseline"] += 1
                continue
            canonical = canonicalize_labor_name(name)
            observation = {
                "canonical_name": canonical["name"],
                "operation_key": canonical["key"],
                "category": canonical["category"],
                "unit_price_rub": unit_price,
                "quantity": quantity,
                "closed_at": closed_at,
                "vehicle_segment": segment,
            }
            samples[canonical["key"]].append(observation)
            executor_samples[executor_key].append(observation)
            quality["work_rows_valid"] += 1

    baselines = _build_baselines(samples, generated_at=generated, half_life_days=recency_half_life_days)
    executor_rows = _build_executor_rows(
        executor_samples,
        executor_names=executor_names,
        executor_row_counts=executor_row_counts,
        generated_at=generated,
        half_life_days=recency_half_life_days,
    )
    total_work_rows = int(quality["work_rows_total"])
    valid_work_rows = int(quality["work_rows_valid"])
    classified_executor_rows = sum(executor_row_counts.values())
    snapshot = {
        "schema_version": LABOR_SNAPSHOT_SCHEMA_VERSION,
        "generated_at": generated.isoformat(),
        "source": {
            "system": "AutoStop CRM",
            "dataset": "read_only_state_snapshot",
            "sha256": source_sha256,
            "single_read_snapshot": True,
        },
        "privacy": {
            "aggregate_only": True,
            "contains_order_ids": False,
            "contains_client_identity": False,
            "contains_phone_vin_or_plate": False,
            "contains_payment_rows": False,
            "contains_executor_identity": False,
            "contains_salary_cost_or_margin": False,
            "raw_repair_orders_persisted": False,
        },
        "scope": {
            "selection": "all_closed_repair_orders",
            "selected_closed_orders": len(cards),
            "closed_date_from": (
                min(closed_dates).astimezone(CRM_TIMEZONE).date().isoformat() if closed_dates else None
            ),
            "closed_date_to": (max(closed_dates).astimezone(CRM_TIMEZONE).date().isoformat() if closed_dates else None),
            "timezone": "Asia/Krasnoyarsk",
            "currency": "RUB",
            "work_rows_total": total_work_rows,
            "valid_work_rows": valid_work_rows,
            "valid_work_row_share": round(valid_work_rows / total_work_rows, 4) if total_work_rows else 0.0,
            "vehicle_segment_counts": dict(sorted(segment_counts.items())),
            "labor_price_basis": "CRM work-row total divided by positive quantity; total controls on mismatch",
            "recency_weighting": {
                "method": "exponential_decay",
                "half_life_days": recency_half_life_days,
                "undated_rows": "included_in_unweighted_distribution_but_excluded_from_recency_weight",
            },
        },
        "data_quality": dict(sorted(quality.items())),
        "labor_baselines": baselines,
        "decision_policy": {
            "internal_confidence": {
                "stable": "10+ inlier observations",
                "working": "5-9 inlier observations",
                "preliminary": "3-4 inlier observations",
                "context_only": "fewer than 3 inlier observations",
            },
            "high_confidence_requires_independent_source_families": 3,
            "labor_rule": (
                "Use the recency-weighted internal median as a historical anchor. "
                "Recheck exact scope, vehicle, labor time and current market before final price."
            ),
            "parts_rule": "Parts and materials are outside this snapshot and require live supplier and fitment evidence.",
            "executor_rule": "Executor analytics never influence the customer labor-price recommendation.",
        },
    }
    executor_report = {
        "schema_version": EXECUTOR_REPORT_SCHEMA_VERSION,
        "generated_at": generated.isoformat(),
        "source_sha256": source_sha256,
        "privacy": {
            "restricted_internal_report": True,
            "contains_executor_identity": True,
            "contains_client_order_vin_phone_or_payment_identity": False,
            "contains_salary_cost_or_margin": False,
            "must_not_feed_customer_pricing": True,
            "must_not_enter_agent_memory": True,
        },
        "scope": {
            "selected_closed_orders": len(cards),
            "work_rows_total": total_work_rows,
            "assigned_executor_rows": classified_executor_rows - int(quality["work_rows_without_executor"]),
            "unassigned_executor_rows": int(quality["work_rows_without_executor"]),
            "unclassified_invalid_rows": max(0, total_work_rows - classified_executor_rows),
            "currency": "RUB",
        },
        "executors": executor_rows,
    }
    return snapshot, executor_report


def build_service_labor_experience_from_state_file(
    state_path: str | Path,
    *,
    recency_half_life_days: int = DEFAULT_RECENCY_HALF_LIFE_DAYS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source = Path(state_path)
    raw = source.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("CRM state must be a JSON object")
    return build_service_labor_experience(
        payload,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        recency_half_life_days=recency_half_life_days,
    )


def _private_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)


def _backup_existing(path: Path, generated_at: str) -> Path | None:
    if not path.exists():
        return None
    stamp = generated_at.replace(":", "").replace("-", "").replace("+", "_").replace(".", "_")
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    backup_path = backup_dir / f"{path.stem}.{stamp}{path.suffix}"
    shutil.copy2(path, backup_path)
    os.chmod(backup_path, 0o600)
    return backup_path


def _atomic_write(path: Path, payload: str) -> Path:
    _private_parent(path)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
        os.chmod(path, 0o600)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return path


def save_service_labor_artifacts(
    snapshot: dict[str, Any],
    executor_report: dict[str, Any],
    *,
    output_path: str | Path = DEFAULT_LABOR_EXPERIENCE_PATH,
    executor_output_path: str | Path = DEFAULT_EXECUTOR_REPORT_PATH,
    report_output_path: str | Path = DEFAULT_LABOR_REPORT_PATH,
) -> dict[str, str | None]:
    output = Path(output_path)
    executor_output = Path(executor_output_path)
    report_output = Path(report_output_path)
    generated_at = str(snapshot.get("generated_at") or datetime.now(UTC).isoformat())
    snapshot_backup = _backup_existing(output, generated_at)
    executor_backup = _backup_existing(executor_output, generated_at)
    report_backup = _backup_existing(report_output, generated_at)
    _atomic_write(output, json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(executor_output, json.dumps(executor_report, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(report_output, build_service_labor_markdown_report(snapshot))
    return {
        "output_path": str(output),
        "executor_output_path": str(executor_output),
        "report_output_path": str(report_output),
        "snapshot_backup_path": str(snapshot_backup) if snapshot_backup else None,
        "executor_backup_path": str(executor_backup) if executor_backup else None,
        "report_backup_path": str(report_backup) if report_backup else None,
    }


def load_service_labor_experience(
    path: str | Path = DEFAULT_LABOR_EXPERIENCE_PATH,
) -> dict[str, Any] | None:
    source = Path(path)
    if not source.exists():
        return None
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != LABOR_SNAPSHOT_SCHEMA_VERSION:
        return None
    return payload


def summarize_service_labor_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    baselines = [row for row in snapshot.get("labor_baselines") or [] if isinstance(row, dict)]
    reusable = [row for row in baselines if int(row.get("inlier_sample_count") or 0) >= 3]
    return {
        "schema_version": snapshot.get("schema_version"),
        "source_sha256": (snapshot.get("source") or {}).get("sha256"),
        "scope": snapshot.get("scope"),
        "data_quality": snapshot.get("data_quality"),
        "labor_operation_groups": len(baselines),
        "reusable_labor_baselines": len(reusable),
        "top_labor_baselines": [
            {
                "operation_key": row.get("operation_key"),
                "operation_name": row.get("operation_name"),
                "category": row.get("category"),
                "sample_count": row.get("sample_count"),
                "inlier_sample_count": row.get("inlier_sample_count"),
                "outlier_count": row.get("outlier_count"),
                "p25_rub": row.get("p25_rub"),
                "median_rub": row.get("median_rub"),
                "weighted_median_rub": row.get("weighted_median_rub"),
                "p75_rub": row.get("p75_rub"),
                "recommended_anchor_rub": row.get("recommended_anchor_rub"),
                "confidence": row.get("confidence"),
                "latest_closed_date": row.get("latest_closed_date"),
            }
            for row in reusable[:20]
        ],
        "privacy": snapshot.get("privacy"),
    }


def build_service_labor_markdown_report(snapshot: dict[str, Any]) -> str:
    summary = summarize_service_labor_snapshot(snapshot)
    scope = summary.get("scope") or {}
    quality = summary.get("data_quality") or {}
    rows = [
        "# Полный анализ цен выполненных работ AutoStop",
        "",
        f"Срез сформирован: {snapshot.get('generated_at')}.",
        (
            f"Охват: {int(scope.get('selected_closed_orders') or 0)} закрытых ЗН, "
            f"{int(scope.get('work_rows_total') or 0)} строк работ, "
            f"{int(scope.get('valid_work_rows') or 0)} пригодных ценовых наблюдений."
        ),
        (
            f"Период закрытия: {scope.get('closed_date_from') or 'не определён'} — "
            f"{scope.get('closed_date_to') or 'не определён'}; цены в RUB."
        ),
        "",
        "## Вывод для оценки",
        "",
        (
            f"Получено {summary.get('labor_operation_groups', 0)} групп операций, "
            f"из них {summary.get('reusable_labor_baselines', 0)} имеют минимум три "
            "невыбросных наблюдения. Рекомендуемый внутренний ориентир — взвешенная "
            "по свежести медиана; он не является окончательной клиентской ценой."
        ),
        "",
        "## Качество данных",
        "",
        f"- Исключено строк: {int(quality.get('work_rows_excluded_from_price_baseline') or 0)}.",
        f"- Некорректное количество: {int(quality.get('work_flag_invalid_quantity') or 0)}.",
        f"- Нулевая или отсутствующая цена: {int(quality.get('work_flag_zero_or_missing_price') or 0)}.",
        f"- Расхождение цены и итога: {int(quality.get('work_flag_price_total_mismatch') or 0)}.",
        f"- Без исполнителя: {int(quality.get('work_rows_without_executor') or 0)}.",
        "",
        "## Наиболее повторяемые операции",
        "",
        "| Операция | n | Взвешенная медиана | P25–P75 | Надёжность |",
        "|---|---:|---:|---:|---|",
    ]
    for baseline in summary.get("top_labor_baselines") or []:
        rows.append(
            "| {name} | {count} | {weighted:.0f} ₽ | {p25:.0f}–{p75:.0f} ₽ | {confidence} |".format(
                name=str(baseline.get("operation_name") or "").replace("|", "/"),
                count=int(baseline.get("inlier_sample_count") or 0),
                weighted=float(baseline.get("weighted_median_rub") or baseline.get("median_rub") or 0),
                p25=float(baseline.get("p25_rub") or 0),
                p75=float(baseline.get("p75_rub") or 0),
                confidence=baseline.get("confidence") or "",
            )
        )
    rows.extend(
        [
            "",
            "## Правило применения",
            "",
            "История AutoStop — внутренний ориентир. Для клиентской цены менеджер дополнительно "
            "проверяет точный автомобиль и объём, актуальный рынок работ и нормо-часы/сервисные данные. "
            "Запчасти оцениваются отдельно по live-поставщикам и применимости.",
            "",
        ]
    )
    return "\n".join(rows)
