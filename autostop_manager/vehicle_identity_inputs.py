"""Validate vehicle identity inputs without I/O or provider-specific assumptions."""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, date, datetime
from typing import Any

MAX_IDENTITY_ITEMS = 500
IDENTIFIER_TYPES = {"auto", "vin", "vin_partial", "frame_number", "market_code"}
PROFILE_KEYS = ("vehicle_profile", "vehicle_profile_compact", "crm_vehicle_profile")
ALIASES = {
    "make_display": "make",
    "model_display": "model",
    "engine_model": "engine",
    "gearbox_model": "transmission",
    "chassis_number": "frame",
    "body_number": "frame",
    "display_name": "vehicle",
}
STRING_FIELDS = {
    "vehicle",
    "make",
    "model",
    "engine",
    "transmission",
    "drivetrain",
    "market",
    "production_date",
    "modification",
    "trim",
    "series",
    "source_summary",
    "oem_notes",
    "vin",
    "frame",
    "engine_type",
    "fuel_type",
}
TECHNICAL_NUMERIC_FIELDS = {"displacement_cc", "power_kw", "power_hp"}
CONTEXT_FIELDS = (
    STRING_FIELDS
    | TECHNICAL_NUMERIC_FIELDS
    | {
        "model_year",
        "production_year",
        "transmission_speeds",
        "source_confidence",
        "options",
    }
)


def _issue(code: str, field: str) -> dict[str, str]:
    return {"code": code, "field": field, "stage": "input_validation"}


def _mapping(value: Any, field: str, errors: list[dict[str, str]]) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            errors.append(_issue("invalid_profile_json", field))
            return {}
    if not isinstance(value, dict):
        errors.append(_issue("expected_object", field))
        return {}
    return value


def _merge_nonempty(target: dict[str, Any], values: dict[str, Any]) -> None:
    for key, value in values.items():
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        target[key] = value


def _alias_conflicts(value: Any, errors: list[dict[str, str]]) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(_issue("expected_list", "input_alias_conflicts"))
        return []
    conflicts: list[dict[str, Any]] = []
    for index, row in enumerate(value):
        path = f"input_alias_conflicts[{index}]"
        if not isinstance(row, dict):
            errors.append(_issue("expected_object", path))
            continue
        field = row.get("field")
        if not isinstance(field, str):
            errors.append(_issue("expected_string", f"{path}.field"))
            continue
        if field not in STRING_FIELDS - {"frame", "vin", "vehicle"}:
            continue
        invalid_keys = [
            key for key in ("canonical_value", "alias_value", "source") if not isinstance(row.get(key), str)
        ]
        if invalid_keys:
            errors.extend(_issue("expected_string", f"{path}.{key}") for key in invalid_keys)
            continue
        conflicts.append({key: row[key] for key in ("field", "canonical_value", "alias_value", "source")})
    return conflicts


def flatten_identity_context(value: Any) -> tuple[dict[str, Any], list[dict[str, str]], list[dict[str, Any]]]:
    """Keep supported facts and alias disagreements; production year is not MY."""
    errors: list[dict[str, str]] = []
    if value is None:
        value = {}
    if not isinstance(value, dict):
        return {}, [_issue("expected_object", "crm_context")], []
    merged: dict[str, Any] = {}
    for key in PROFILE_KEYS:
        _merge_nonempty(merged, _mapping(value.get(key), key, errors))
    _merge_nonempty(merged, value)
    alias_conflicts = _alias_conflicts(merged.get("input_alias_conflicts"), errors)
    for alias, canonical in ALIASES.items():
        alternate = merged.get(alias)
        chosen = merged.get(canonical)
        if alternate in (None, ""):
            continue
        if not isinstance(alternate, str):
            errors.append(_issue("expected_string", alias))
            continue
        if chosen in (None, ""):
            merged[canonical] = alternate
        elif chosen != alternate and canonical not in {"frame", "vehicle"}:
            conflict = {"field": canonical, "canonical_value": chosen, "alias_value": alternate, "source": alias}
            if conflict not in alias_conflicts:
                alias_conflicts.append(conflict)
    return {key: value for key, value in merged.items() if key in CONTEXT_FIELDS}, errors, alias_conflicts


def _year(value: Any, field: str, errors: list[dict[str, str]], notes: list[dict[str, str]]) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}", value.strip()):
        value = int(value.strip())
        notes.append({"code": "year_string_normalized", "field": field})
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(_issue("expected_integer_year", field))
        return None
    if not 1900 <= value <= datetime.now(UTC).year + 1:
        errors.append(_issue("year_out_of_range", field))
        return None
    return value


def _validate_date(value: str, errors: list[dict[str, str]]) -> None:
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
            parsed = date.fromisoformat(value + "-01")
        elif re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            parsed = date.fromisoformat(value)
        else:
            raise ValueError("unsupported_date_format")
        if not 1900 <= parsed.year <= datetime.now(UTC).year + 1:
            raise ValueError("date_out_of_range")
    except ValueError:
        errors.append(_issue("invalid_production_date", "production_date"))


def _validate_confidence(value: Any, errors: list[dict[str, str]]) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        errors.append(_issue("expected_finite_confidence", "source_confidence"))
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        errors.append(_issue("expected_finite_confidence", "source_confidence"))
        return None
    if not math.isfinite(number) or not 0 <= number <= 1:
        errors.append(_issue("confidence_out_of_range", "source_confidence"))
        return None
    return number


def _clean_fields(context: dict[str, Any], errors: list[dict[str, str]], notes: list[dict[str, str]]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in context.items():
        if value is None or value == "":
            continue
        if key in STRING_FIELDS:
            if not isinstance(value, str):
                errors.append(_issue("expected_string", key))
            elif value.strip():
                cleaned[key] = value.strip()
        elif key in {"model_year", "production_year"}:
            normalized = _year(value, key, errors, notes)
            if normalized is not None:
                cleaned[key] = normalized
        elif key == "source_confidence":
            normalized_confidence = _validate_confidence(value, errors)
            if normalized_confidence is not None:
                cleaned[key] = normalized_confidence
        elif key in TECHNICAL_NUMERIC_FIELDS:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                errors.append(_issue("expected_positive_finite_number", key))
            else:
                cleaned[key] = value
        elif key == "transmission_speeds":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 30:
                errors.append(_issue("invalid_transmission_speeds", key))
            else:
                cleaned[key] = value
        elif key == "options":
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                errors.append(_issue("expected_string_list", key))
            else:
                cleaned[key] = list(dict.fromkeys(item.strip() for item in value if item.strip()))
    if "production_date" in cleaned:
        _validate_date(cleaned["production_date"], errors)
    return cleaned


def validate_identity_input(
    identifier: Any,
    crm_context: Any = None,
    *,
    model_year: Any = None,
    make_hint: Any = None,
    identifier_type: Any = "auto",
) -> dict[str, Any]:
    context, errors, alias_conflicts = flatten_identity_context(crm_context)
    notes: list[dict[str, str]] = []
    if model_year is not None and context.get("model_year") in (None, ""):
        context["model_year"] = model_year
    if make_hint is not None and context.get("make") in (None, ""):
        context["make"] = make_hint
    if not isinstance(identifier, str):
        errors.append(_issue("expected_string", "identifier"))
        identifier = ""
    if identifier_type is None:
        identifier_type = "auto"
    if not isinstance(identifier_type, str) or identifier_type not in IDENTIFIER_TYPES:
        errors.append(_issue("unsupported_identifier_type", "identifier_type"))
        identifier_type = "auto"
    cleaned = _clean_fields(context, errors, notes)
    if alias_conflicts:
        cleaned["input_alias_conflicts"] = alias_conflicts
    return {
        "ok": not errors,
        "identifier": identifier.strip(),
        "context": cleaned,
        "identifier_type": identifier_type,
        "errors": errors,
        "normalization_notes": notes,
    }


def validate_identity_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return validate_identity_input(None, crm_context=item)
    nested = item.get("crm_context")
    if "crm_context" in item and nested is not None and not isinstance(nested, dict):
        return validate_identity_input(item.get("identifier", ""), crm_context=nested)
    context = dict(nested or {})
    _merge_nonempty(context, {key: value for key, value in item.items() if key != "crm_context"})
    flattened, _errors, _aliases = flatten_identity_context(context)
    identifier = next(
        (
            item[key]
            for key in ("identifier", "vin", "frame", "chassis_number", "body_number")
            if key in item and item[key] not in (None, "")
        ),
        flattened.get("vin") or flattened.get("frame") or "",
    )
    return validate_identity_input(
        identifier,
        context,
        model_year=item.get("model_year"),
        make_hint=item.get("make"),
        identifier_type=item.get("identifier_type", "auto"),
    )
