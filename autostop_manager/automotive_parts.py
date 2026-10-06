"""Pure request, catalog, relation and fitment operations over supplied evidence."""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date
from typing import Any

from .automotive_contracts import (
    MAX_ROWS,
    catalog_ref,
    content_digest,
    finite_number,
    invalid,
    result,
    source_metadata_errors,
    valid_binding,
)
from .parts_intent import normalize_part_intent
from .vehicle_identity import identity_values_agree
from .vin_lookup import normalize_part_number


def _split_request(text: str) -> list[str]:
    rows = re.split(r"[;\n]+|,(?!\d)", text)
    output = []
    for row in rows:
        profile = normalize_part_intent(row)
        output.extend(re.split(r"\s+и\s+", row) if profile.get("intent_id") == "multiple_parts" else [row])
    return [row.strip() for row in output if row.strip()]


def _quantity(raw: str) -> tuple[float | None, str | None]:
    match = re.search(
        r"(?<![\w-])(\d+(?:[.,]\d+)?)\s*(шт(?:ук[аи]?)?\.?|комплект(?:а|ов)?|компл\.?|пар[аы]?|л(?:итр(?:а|ов)?)?)(?!\w)",
        raw,
        re.I,
    )
    if not match:
        return None, None
    unit = match[2].casefold().rstrip(".")
    return finite_number(match[1]), "set" if unit.startswith("комп") else "pair" if unit.startswith(
        "пар"
    ) else "liter" if unit.startswith("л") else "piece"


def _request_item(row: str | dict[str, Any], index: int) -> dict[str, Any]:
    supplied = row if isinstance(row, dict) else {}
    raw = str(supplied.get("raw") or supplied.get("description") or (row if isinstance(row, str) else ""))
    intent = normalize_part_intent(
        raw, axle=supplied.get("axle"), side=supplied.get("side"), position=supplied.get("position")
    )
    inferred_quantity, inferred_unit = _quantity(raw)
    quantity = finite_number(supplied["quantity"]) if "quantity" in supplied else inferred_quantity
    unit = supplied.get("unit", inferred_unit)
    number = supplied.get("part_number")
    missing = [field for field, value in (("quantity", quantity), ("unit", unit)) if value is None]
    if intent.get("intent_id") in {"unknown", "multiple_parts"}:
        missing.append("part_intent")
    return {
        "item_id": str(supplied.get("item_id") or "part-" + content_digest({"index": index, "raw": raw})[:16]),
        "raw": raw,
        "intent": intent,
        "quantity": quantity,
        "unit": unit,
        "quantity_status": "invalid"
        if supplied.get("quantity") is not None and quantity is None
        else "known"
        if quantity is not None
        else "unknown",
        "side": supplied.get("side")
        or intent.get("explicit_position_context", {}).get("side")
        or intent.get("inferred_position_context", {}).get("side"),
        "axle": supplied.get("axle")
        or intent.get("explicit_position_context", {}).get("axle")
        or intent.get("inferred_position_context", {}).get("axle"),
        "position": supplied.get("position"),
        "brand": supplied.get("brand"),
        "raw_part_number": number,
        "normalized_part_number": normalize_part_number(number) if isinstance(number, str) else None,
        "ean": supplied.get("ean"),
        "number_kind": supplied.get("number_kind", "unknown"),
        "missing_fields": missing,
    }


def normalize_parts_request(text: str = "", items: list[dict[str, Any] | str] | None = None) -> dict[str, Any]:
    if not isinstance(text, str) or len(text) > 20000 or (items is not None and not isinstance(items, list)):
        return invalid("normalize_parts_request", "text_or_items")
    source = items if items is not None else _split_request(text)
    if not source or len(source) > MAX_ROWS or any(not isinstance(row, (dict, str)) for row in source):
        return invalid("normalize_parts_request", "items")
    normalized = [_request_item(row, index) for index, row in enumerate(source)]
    ids = [row["item_id"] for row in normalized]
    if len(ids) != len(set(ids)):
        return invalid("normalize_parts_request", "duplicate_item_id")
    if any(row["quantity_status"] == "invalid" for row in normalized):
        return result("normalize_parts_request", "invalid_input", {"items": normalized}, missing_fields=["quantity"])
    missing = [f"{row['item_id']}.{field}" for row in normalized for field in row["missing_fields"]]
    return result(
        "normalize_parts_request", "partial" if missing else "success", {"items": normalized}, missing_fields=missing
    )


def resolve_catalog_group(
    tree: dict[str, Any], intent: str, modification: dict[str, Any], selected_node_id: str | int | None = None
) -> dict[str, Any]:
    from .vin_oem_resolver import _tree_node_resolution

    if (
        not isinstance(tree, dict)
        or not isinstance(intent, str)
        or not catalog_ref(modification, namespace="tecdoc", entity_kind="modification")
    ):
        return invalid("resolve_catalog_group", "tree_or_modification")
    if tree.get("modification") != modification or modification.get("carType") not in {"PC", "CV", "Motorcycle"}:
        return result(
            "resolve_catalog_group",
            "invalid_input",
            {},
            conflicts=[{"code": "tree_modification_mismatch", "field": "modification"}],
        )
    rows = tree.get("rows")
    if not isinstance(rows, list) or len(rows) > MAX_ROWS or any(not isinstance(row, dict) for row in rows):
        return invalid("resolve_catalog_group", "tree.rows")
    selected = _tree_node_resolution(rows, normalize_part_intent(intent), intent, selected_node_id)
    digest = content_digest(rows)
    nodes = [
        {
            "provider": "partsapi_ru",
            "namespace": "tecdoc",
            "entity_kind": "tree_node",
            "carType": modification["carType"],
            "id": node_id,
            "parent": modification,
            "tree_sha256": digest,
        }
        for node_id in selected["candidate_node_ids"]
    ]
    return result(
        "resolve_catalog_group",
        "success" if selected["category_queryable"] else "partial",
        {**selected, "nodes": nodes, "tree_sha256": digest},
        missing_fields=[] if selected["category_queryable"] else ["catalog_group"],
        evidence=tree.get("evidence") or [],
    )


def capture_oem_evidence(
    part_number: str,
    source: dict[str, Any],
    scope: str,
    vehicle_binding: dict[str, Any] | None = None,
    brand: str | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(part_number, str)
        or not part_number.strip()
        or not isinstance(source, dict)
        or scope not in {"family", "modification", "exact_identifier"}
    ):
        return invalid("capture_oem_evidence", "number_or_source_or_scope")
    missing = source_metadata_errors(source)
    if normalize_part_number(str(source.get("part_number") or "")) != normalize_part_number(part_number):
        missing.append("source_part_number")
    if not isinstance(brand, str) or not brand.strip() or str(source.get("brand") or "").casefold() != brand.casefold():
        missing.append("source_brand")
    if source.get("scope") != scope:
        missing.append("source_scope")
    primary = source.get("document_kind") in {"official_epc", "oem_catalog_document", "physical_oe_marking"}
    if not primary or source.get("declares_oem") is not True:
        missing.append("primary_oem_evidence")
    if scope == "exact_identifier" and (
        not valid_binding(vehicle_binding) or source.get("identifier_binding") != vehicle_binding
    ):
        missing.append("exact_vehicle_binding")
    if scope == "modification" and (
        not catalog_ref(vehicle_binding, namespace="tecdoc", entity_kind="modification")
        or source.get("catalog_ref") != vehicle_binding
    ):
        missing.append("modification_binding")
    candidate = {
        "raw_number": part_number,
        "normalized_number": normalize_part_number(part_number),
        "brand": brand,
        "kind": "oem" if not missing else "unknown",
        "scope": scope,
        "vehicle_binding": vehicle_binding,
        "oem_confirmed": not missing,
        "fitment_confirmed": False,
    }
    return result(
        "capture_oem_evidence",
        "partial" if missing else "success",
        {"candidate": candidate},
        evidence=[source],
        missing_fields=missing,
    )


def _relation(row: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    kind = row.get("type", "unknown")
    missing = []
    evidence = row.get("evidence") or []
    if kind not in {"cross", "analog", "oe_reference", "supersession"}:
        kind = "unknown"
        missing.append("relation_type")
    if kind == "supersession":
        primary = any(
            isinstance(item, dict)
            and item.get("document_kind") in {"manufacturer_supersession", "official_epc"}
            and not source_metadata_errors(item)
            and item.get("direction") == row.get("direction")
            and all(
                isinstance(item.get(key), dict)
                and normalize_part_number(str(item[key].get("number") or ""))
                == normalize_part_number(row[key]["number"])
                and bool(row[key].get("brand"))
                and str(item[key].get("brand") or "").casefold() == str(row[key]["brand"]).casefold()
                for key in ("from", "to")
            )
            for item in evidence
        )
        if not primary or row.get("direction") not in {"from_to", "to_from"}:
            missing.append("supersession_direction_and_primary_evidence")
            kind = "unverified_supersession"
    normalized = {
        **row,
        "type": kind,
        "from": {**row["from"], "normalized_number": normalize_part_number(row["from"]["number"])},
        "to": {**row["to"], "normalized_number": normalize_part_number(row["to"]["number"])},
        "qualifiers": row.get("qualifiers") or [],
        "fitment_confirmed": False,
    }
    return normalized, missing


def compare_part_relations(relations: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(relations, list) or len(relations) > MAX_ROWS:
        return invalid("compare_part_relations", "relations")
    for row in relations:
        if not isinstance(row, dict) or any(
            not isinstance(row.get(key), dict)
            or not isinstance(row[key].get("number"), str)
            or not row[key]["number"].strip()
            for key in ("from", "to")
        ):
            return invalid("compare_part_relations", "relation_endpoints")
        supplied = row.get("evidence") or []
        if not isinstance(supplied, list) or any(not isinstance(item, dict) for item in supplied):
            return invalid("compare_part_relations", "relation_evidence")
    normalized: list[dict[str, Any]] = []
    missing: list[str] = []
    conflicts: list[dict[str, Any]] = []
    for index, row in enumerate(relations):
        value, unknown = _relation(row)
        normalized.append(value)
        missing.extend(f"relations[{index}].{field}" for field in unknown)
    for row in normalized:
        if row["type"] != "supersession":
            continue
        alternatives = [
            other
            for other in normalized
            if other["type"] == "supersession" and other["from"] == row["from"] and other["to"] != row["to"]
        ]
        if alternatives:
            conflicts.append(
                {
                    "code": "supersession_branches",
                    "from": row["from"],
                    "alternatives": [row["to"], *[other["to"] for other in alternatives]],
                }
            )
    return result(
        "compare_part_relations",
        "partial" if missing or conflicts else "success",
        {"relations": normalized},
        missing_fields=missing,
        conflicts=conflicts,
    )


def _date_interval(value: Any) -> tuple[date, date] | None:
    if not isinstance(value, str):
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}", value):
            first = date.fromisoformat(value + "-01")
            return first, first.replace(day=monthrange(first.year, first.month)[1])
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            observed = date.fromisoformat(value)
            return observed, observed
    except ValueError:
        pass
    return None


def _date_range_criterion(expected: dict[str, Any], actual: Any) -> str:
    observed = _date_interval(actual)
    lower, upper = _date_interval(expected.get("from")), _date_interval(expected.get("to"))
    if (
        observed is None
        or (expected.get("from") is not None and lower is None)
        or (expected.get("to") is not None and upper is None)
    ):
        return "unknown"
    if lower and upper and lower[0] > upper[1]:
        return "unknown"
    if (lower and observed[1] < lower[0]) or (upper and observed[0] > upper[1]):
        return "rejected"
    if (lower and observed[0] < lower[0]) or (upper and observed[1] > upper[1]):
        return "unknown"
    return "supported"


def _range_criterion(field: str, expected: dict[str, Any], actual: Any) -> str:
    if expected.get("from") is None and expected.get("to") is None:
        return "unknown"
    if field == "production_date":
        return _date_range_criterion(expected, actual)
    observed = finite_number(actual)
    lower, upper = finite_number(expected.get("from")), finite_number(expected.get("to"))
    if (
        observed is None
        or (expected.get("from") is not None and lower is None)
        or (expected.get("to") is not None and upper is None)
    ):
        return "unknown"
    if lower is not None and upper is not None and lower > upper:
        return "unknown"
    if field in {"production_year", "model_year"} and any(
        value is not None and (not value.is_integer() or value < 1900) for value in (observed, lower, upper)
    ):
        return "unknown"
    return (
        "rejected"
        if (lower is not None and observed < lower) or (upper is not None and observed > upper)
        else "supported"
    )


def _criterion(field: str, expected: Any, actual: Any) -> str:
    if actual is None or actual == "" or expected is None or expected == "" or expected == []:
        return "unknown"
    if isinstance(expected, list):
        return "supported" if any(identity_values_agree(field, value, actual) for value in expected) else "rejected"
    if isinstance(expected, dict):
        if "from" not in expected and "to" not in expected:
            return "unknown"
        return _range_criterion(field, expected, actual)
    return "supported" if identity_values_agree(field, expected, actual) else "rejected"


def assess_part_fitment(
    vehicle: dict[str, Any],
    part: dict[str, Any],
    criteria: dict[str, Any],
    evidence: list[dict[str, Any]],
    scope: str = "modification",
) -> dict[str, Any]:
    if (
        not all(isinstance(row, dict) for row in (vehicle, part, criteria))
        or not isinstance(evidence, list)
        or scope not in {"family", "modification", "exact_identifier"}
    ):
        return invalid("assess_part_fitment", "vehicle_part_criteria_evidence_scope")
    if len(evidence) > MAX_ROWS or any(not isinstance(row, dict) for row in evidence):
        return invalid("assess_part_fitment", "evidence")
    profile: dict[str, Any] = (
        vehicle["vehicle_profile"] if isinstance(vehicle.get("vehicle_profile"), dict) else vehicle
    )
    checked = [
        {
            "field": field,
            "required": expected,
            "actual": profile.get(field),
            "state": _criterion(field, expected, profile.get(field)),
        }
        for field, expected in criteria.items()
    ]
    missing = [row["field"] for row in checked if row["state"] == "unknown"]
    conflicts = [row for row in checked if row["state"] == "rejected"]
    number = normalize_part_number(str(part.get("number") or part.get("part_number") or ""))
    brand = str(part.get("brand") or "").casefold()
    primary = [
        row
        for row in evidence
        if row.get("fitment_assertion") is True
        and row.get("document_kind") in {"official_epc", "manufacturer_fitment_catalog"}
        and normalize_part_number(str(row.get("part_number") or "")) == number
        and number
        and str(row.get("brand") or "").casefold() == brand
        and brand
        and row.get("scope") == scope
        and not source_metadata_errors(row)
        and (
            scope == "family"
            or (
                scope == "exact_identifier"
                and valid_binding(vehicle.get("input_binding"))
                and row.get("identifier_binding") == vehicle.get("input_binding")
            )
            or (
                scope == "modification"
                and catalog_ref(vehicle.get("catalog_ref"), namespace="tecdoc", entity_kind="modification")
                and row.get("catalog_ref") == vehicle.get("catalog_ref")
            )
        )
    ]
    if not primary:
        missing.append("primary_bound_fitment_evidence")
    for index, source in enumerate(primary):
        conditions = source.get("conditions", {})
        if not isinstance(conditions, dict):
            missing.append(f"evidence[{index}].conditions")
            continue
        for field, expected in conditions.items():
            condition = {
                "field": field,
                "required": expected,
                "actual": profile.get(field),
                "state": _criterion(field, expected, profile.get(field)),
                "source_index": index,
            }
            checked.append(condition)
            if condition["state"] == "unknown":
                missing.append(field)
            elif condition["state"] == "rejected":
                conflicts.append(condition)
    if scope == "exact_identifier" and (
        not valid_binding(vehicle.get("input_binding"))
        or not any(row.get("identifier_binding") == vehicle.get("input_binding") for row in primary)
    ):
        missing.append("exact_identifier_binding")
    if scope == "modification" and (
        not catalog_ref(vehicle.get("catalog_ref"), namespace="tecdoc", entity_kind="modification")
        or not any(row.get("catalog_ref") == vehicle.get("catalog_ref") for row in primary)
    ):
        missing.append("modification_binding")
    known_conflicts = vehicle.get("conflicts") or []
    if not isinstance(known_conflicts, list) or any(not isinstance(row, dict) for row in known_conflicts):
        return invalid("assess_part_fitment", "vehicle.conflicts")
    conflicts.extend(
        row
        for row in known_conflicts
        if row.get("field") in set(criteria) | {"identifier", "engine", "transmission", "drivetrain", "modification"}
    )
    state = (
        "conflict"
        if known_conflicts and conflicts
        else "rejected"
        if conflicts
        else "unknown"
        if missing
        else "supported"
    )
    return result(
        "assess_part_fitment",
        "partial" if state in {"unknown", "conflict"} else "success",
        {"state": state, "scope": scope, "part": part, "checked_conditions": checked},
        evidence=evidence,
        missing_fields=missing,
        conflicts=conflicts,
    )
