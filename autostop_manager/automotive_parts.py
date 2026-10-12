"""Pure request, catalog, relation and fitment operations over supplied evidence."""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date
from typing import Any

from .automotive_contracts import (
    MAX_ROWS,
    PUBLIC_OEM_CATALOG_NAMESPACES,
    catalog_ref,
    content_digest,
    finite_number,
    identity_errors,
    invalid,
    oem_catalog_ref,
    public_oem_catalog_ref,
    result,
    same_oem_catalog_ref,
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
    evidence = tree.get("evidence", [])
    if not isinstance(evidence, list) or len(evidence) > MAX_ROWS or any(not isinstance(row, dict) for row in evidence):
        return invalid("resolve_catalog_group", "tree.evidence")
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
        evidence=evidence,
    )


def _public_catalog_source(source: dict[str, Any]) -> bool:
    provider = source.get("provider")
    return isinstance(provider, str) and provider in PUBLIC_OEM_CATALOG_NAMESPACES


def _mentions_public_catalog(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    provider, namespace = value.get("provider"), value.get("namespace")
    return (isinstance(provider, str) and provider in PUBLIC_OEM_CATALOG_NAMESPACES) or (
        isinstance(namespace, str) and namespace in PUBLIC_OEM_CATALOG_NAMESPACES.values()
    )


def _native_catalog_gaps(
    source: dict[str, Any],
    vehicle_ref: Any,
    *,
    scope: str,
    profile: dict[str, Any] | None = None,
) -> list[str]:
    source_ref = source.get("catalog_ref")
    source_native, vehicle_native = _mentions_public_catalog(source_ref), _mentions_public_catalog(vehicle_ref)
    gaps = []
    if source_native and not public_oem_catalog_ref(source_ref, vehicle_profile=profile):
        gaps.append("source_catalog_context")
    if vehicle_native and not public_oem_catalog_ref(vehicle_ref, vehicle_profile=profile):
        gaps.append("vehicle_catalog_context")
    if (
        (source_native or vehicle_native)
        and source_ref is not None
        and vehicle_ref is not None
        and not same_oem_catalog_ref(source_ref, vehicle_ref)
    ):
        gaps.append("source_vehicle_catalog_mismatch")
    if _public_catalog_source(source) and not source_native:
        gaps.append("source_catalog_context")
    if (
        (source_native or vehicle_native or _public_catalog_source(source))
        and scope == "exact_identifier"
        and source.get("identifier_verified") is not True
    ):
        gaps.append("primary_identifier_verification")
    return gaps


def _unresolved_restrictions(row: dict[str, Any], *, prefix: str = "") -> list[str]:
    return [
        prefix + field
        for field in ("unparsed_conditions", "unparsed_restrictions", "inherited_restrictions", "inherited_conditions")
        if field in row and (not isinstance(row[field], list) or row[field])
    ]


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
    primary = not _public_catalog_source(source) and source.get("document_kind") in {
        "official_epc",
        "oem_catalog_document",
        "physical_oe_marking",
    }
    if not primary or source.get("declares_oem") is not True:
        missing.append("primary_oem_evidence")
    if scope == "exact_identifier" and (
        not valid_binding(vehicle_binding) or source.get("identifier_binding") != vehicle_binding
    ):
        missing.append("exact_vehicle_binding")
    if scope == "modification" and (
        not oem_catalog_ref(vehicle_binding) or not same_oem_catalog_ref(source.get("catalog_ref"), vehicle_binding)
    ):
        missing.append("modification_binding")
    missing.extend(_native_catalog_gaps(source, vehicle_binding if scope != "exact_identifier" else None, scope=scope))
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


def _pr_code_set(value: Any) -> set[str] | None:
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        return None
    if any(not isinstance(code, str) or re.fullmatch(r"[A-Z0-9]{3}", code.strip().upper()) is None for code in value):
        return None
    return {code.strip().upper() for code in value}


def _pr_codes_criterion(expected: Any, actual: Any, *, complete: bool) -> str:
    if not isinstance(expected, dict) or not expected or set(expected) - {"all_of", "any_of", "none_of"}:
        return "unknown"
    required = {operator: _pr_code_set(codes) for operator, codes in expected.items()}
    observed = _pr_code_set(actual)
    if observed is None or any(codes is None for codes in required.values()) or not any(required.values()):
        return "unknown"
    states = []
    all_of, any_of, none_of = (required.get(operator) or set() for operator in ("all_of", "any_of", "none_of"))
    if all_of:
        states.append("supported" if all_of <= observed else "rejected" if complete is True else "unknown")
    if any_of:
        states.append("supported" if any_of & observed else "rejected" if complete is True else "unknown")
    if none_of:
        states.append("rejected" if none_of & observed else "supported" if complete is True else "unknown")
    return "rejected" if "rejected" in states else "unknown" if "unknown" in states else "supported"


def _criterion(field: str, expected: Any, actual: Any, *, pr_codes_complete: bool = False) -> str:
    if field == "pr_codes":
        return _pr_codes_criterion(expected, actual, complete=pr_codes_complete)
    if actual is None or actual == "" or expected is None or expected == "" or expected == []:
        return "unknown"
    if isinstance(expected, list):
        return "supported" if any(identity_values_agree(field, value, actual) for value in expected) else "rejected"
    if isinstance(expected, dict):
        if "from" not in expected and "to" not in expected:
            return "unknown"
        return _range_criterion(field, expected, actual)
    return "supported" if identity_values_agree(field, expected, actual) else "rejected"


def _checked_conditions(
    profile: dict[str, Any], conditions: dict[str, Any], *, source_index: int | None = None
) -> list[dict[str, Any]]:
    return [
        {
            "field": field,
            "required": expected,
            "actual": profile.get(field),
            "state": _criterion(
                field, expected, profile.get(field), pr_codes_complete=profile.get("pr_codes_complete") is True
            ),
            **({"actual_complete": profile.get("pr_codes_complete") is True} if field == "pr_codes" else {}),
            **({"source_index": source_index} if source_index is not None else {}),
        }
        for field, expected in conditions.items()
    ]


def _fitment_scope_bound(source: dict[str, Any], vehicle: dict[str, Any], profile: dict[str, Any], scope: str) -> bool:
    vehicle_ref = vehicle.get("catalog_ref")
    if _native_catalog_gaps(source, vehicle_ref, scope=scope, profile=profile):
        return False
    if scope == "family":
        return True
    if scope == "exact_identifier":
        return valid_binding(vehicle.get("input_binding")) and source.get("identifier_binding") == vehicle.get(
            "input_binding"
        )
    return oem_catalog_ref(vehicle_ref, vehicle_profile=profile) and same_oem_catalog_ref(
        source.get("catalog_ref"), vehicle_ref
    )


def _fitment_sources(
    evidence: list[dict[str, Any]],
    number: str,
    brand: str,
    vehicle: dict[str, Any],
    profile: dict[str, Any],
    scope: str,
) -> tuple[list[dict[str, Any]], list[tuple[int, dict[str, Any]]], list[str]]:
    matched = [
        (index, row)
        for index, row in enumerate(evidence)
        if number
        and brand
        and normalize_part_number(str(row.get("part_number") or "")) == number
        and str(row.get("brand") or "").casefold() == brand
        and row.get("scope") == scope
    ]
    primary = [
        (index, row)
        for index, row in matched
        if row.get("fitment_assertion") is True
        and not _public_catalog_source(row)
        and row.get("document_kind") in {"official_epc", "manufacturer_fitment_catalog"}
        and not source_metadata_errors(row)
        and _fitment_scope_bound(row, vehicle, profile, scope)
    ]
    primary_indices = {index for index, _row in primary}
    condition_sources = [
        (index, row)
        for index, row in matched
        if index in primary_indices
        or (_public_catalog_source(row) and _fitment_scope_bound(row, vehicle, profile, scope))
    ]
    gaps = []
    for index, row in matched:
        prefix = f"evidence[{index}]."
        gaps.extend(_unresolved_restrictions(row, prefix=prefix))
        gaps.extend(
            prefix + field
            for field in _native_catalog_gaps(row, vehicle.get("catalog_ref"), scope=scope, profile=profile)
        )
    return [row for _index, row in primary], condition_sources, gaps


def _vehicle_catalog_context(
    envelope: dict[str, Any], data: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    outer_ref, inner_ref = envelope.get("catalog_ref"), data.get("catalog_ref")
    if data is envelope or outer_ref is None:
        return data, []
    if inner_ref is not None and not same_oem_catalog_ref(outer_ref, inner_ref):
        return data, [{"field": "catalog_ref", "code": "vehicle_catalog_context_mismatch"}]
    return {**data, "catalog_ref": outer_ref}, []


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
    identity_data = vehicle["data"] if isinstance(vehicle.get("data"), dict) else vehicle
    upstream_errors = identity_errors(vehicle, identity_data)
    identity_data, catalog_errors = _vehicle_catalog_context(vehicle, identity_data)
    upstream_errors.extend(catalog_errors)
    if upstream_errors:
        return result(
            "assess_part_fitment",
            "partial",
            {"state": "conflict", "scope": scope, "part": part, "checked_conditions": []},
            evidence=evidence,
            conflicts=upstream_errors,
            missing_fields=["usable_vehicle_identity"],
        )
    vehicle = identity_data
    profile: dict[str, Any] = (
        vehicle["vehicle_profile"] if isinstance(vehicle.get("vehicle_profile"), dict) else vehicle
    )
    checked = _checked_conditions(profile, criteria)
    missing = [row["field"] for row in checked if row["state"] == "unknown"]
    conflicts = [row for row in checked if row["state"] == "rejected"]
    missing.extend(_unresolved_restrictions(part, prefix="part."))
    missing.extend(_unresolved_restrictions(vehicle, prefix="vehicle."))
    missing.extend(_unresolved_restrictions(profile, prefix="vehicle_profile."))
    if "scope" in part and part["scope"] != scope:
        missing.append("part_scope")
    if _mentions_public_catalog(vehicle.get("catalog_ref")) and not public_oem_catalog_ref(
        vehicle.get("catalog_ref"), vehicle_profile=profile
    ):
        missing.append("vehicle_catalog_context")
    number = normalize_part_number(
        str(
            part.get("number")
            or part.get("part_number")
            or part.get("normalized_number")
            or part.get("raw_number")
            or ""
        )
    )
    brand = str(part.get("brand") or "").casefold()
    primary, condition_sources, source_gaps = _fitment_sources(evidence, number, brand, vehicle, profile, scope)
    missing.extend(source_gaps)
    if not primary:
        missing.append("primary_bound_fitment_evidence")
    for index, source in condition_sources:
        conditions = source.get("conditions", {})
        if not isinstance(conditions, dict):
            missing.append(f"evidence[{index}].conditions")
            continue
        for condition in _checked_conditions(profile, conditions, source_index=index):
            checked.append(condition)
            if condition["state"] == "unknown":
                missing.append(condition["field"])
            elif condition["state"] == "rejected":
                conflicts.append(condition)
    if scope == "exact_identifier" and (
        not valid_binding(vehicle.get("input_binding"))
        or not any(row.get("identifier_binding") == vehicle.get("input_binding") for row in primary)
    ):
        missing.append("exact_identifier_binding")
    if scope == "modification" and (
        not oem_catalog_ref(vehicle.get("catalog_ref"), vehicle_profile=profile)
        or not any(same_oem_catalog_ref(row.get("catalog_ref"), vehicle.get("catalog_ref")) for row in primary)
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
