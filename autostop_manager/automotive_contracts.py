"""Shared evidence contracts for independently callable automotive operations."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any

from .vin_lookup import classify_identifier

SCHEMA_VERSION = "autostop.automotive-result.v1"
MAX_ROWS = 500


def result(
    tool_id: str,
    outcome: str,
    data: Any,
    *,
    evidence: list[dict[str, Any]] | None = None,
    missing_fields: list[str] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
    network_calls: int = 0,
    attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "tool_id": tool_id if "." in tool_id else "manager." + tool_id,
        "ok": outcome in {"success", "partial", "empty", "unsupported"},
        "outcome": outcome,
        "data": data,
        "evidence": [
            {
                **dict.fromkeys(
                    ("provider", "primary_lineage", "method", "fetched_at", "locator", "scope", "identifier_binding")
                ),
                **row,
            }
            for row in (evidence or [])
        ],
        "missing_fields": sorted(set(missing_fields or [])),
        "conflicts": conflicts or [],
        "warnings": warnings or [],
        "execution": {"network_calls": network_calls, "attempts": attempts or []},
    }


def invalid(tool_id: str, *fields: str) -> dict[str, Any]:
    return result(tool_id, "invalid_input", {}, missing_fields=list(fields))


def finite_number(value: Any, *, minimum: float = 0) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value.replace(",", ".") if isinstance(value, str) else value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= minimum else None


def binding(identifier: str, identifier_type: str = "auto") -> dict[str, Any]:
    classified = classify_identifier(identifier, identifier_type=identifier_type)
    digest = hashlib.sha256((classified.kind + ":" + classified.normalized).encode()).hexdigest()
    return {"identifier_kind": classified.kind, "identifier_sha256": digest, "version": 1}


def valid_binding(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and type(value.get("version")) is int
        and value.get("version") == 1
        and value.get("identifier_kind") in ("vin", "vin_partial", "frame_number", "market_code")
        and isinstance(value.get("identifier_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["identifier_sha256"]) is not None
    )


def ready_identity(
    identity: Any, identifier: str, *, identifier_type: str = "auto", context: dict[str, Any] | None = None
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Check de-identified public reuse before any provider request."""
    from .vehicle_identity import identity_values_agree

    if not isinstance(identity, dict):
        return None, [{"field": "vehicle_identity", "code": "invalid_ready_identity"}]
    row: dict[str, Any] = dict(identity["data"] if isinstance(identity.get("data"), dict) else identity)
    errors = identity_errors(identity, row)
    if errors:
        return None, errors
    if row.get("input_binding") != binding(identifier, identifier_type):
        return None, [{"field": "identifier", "code": "ready_identity_binding_mismatch"}]
    profile = row.get("vehicle_profile")
    if not isinstance(profile, dict) or row.get("ok") is False:
        return None, [{"field": "vehicle_identity", "code": "invalid_ready_identity"}]
    conflicts = []
    for key in ("evidence", "missing_fields", "warnings"):
        if key not in row and key in identity:
            row[key] = identity[key]
    for field, value in (context or {}).items():
        if (
            value not in (None, "")
            and profile.get(field) not in (None, "")
            and not identity_values_agree(field, value, profile[field])
        ):
            conflicts.append({"field": field, "code": "ready_identity_context_mismatch"})
    return (None, conflicts) if conflicts else (row, [])


def identity_errors(envelope: dict[str, Any], data: dict[str, Any]) -> list[dict[str, Any]]:
    """A request digest never overrides provider failure or disputed identity."""
    errors: list[dict[str, Any]] = []
    for row in (envelope, data):
        if row.get("ok") is False or row.get("outcome") not in (None, "success", "partial"):
            errors.append({"field": "vehicle_identity", "code": "failed_ready_identity"})
        supplied = row.get("conflicts") or []
        if not isinstance(supplied, list) or any(not isinstance(item, dict) for item in supplied):
            errors.append({"field": "vehicle_identity", "code": "invalid_identity_conflicts"})
        else:
            errors.extend(item for item in supplied if item not in errors)
    provider_binding = data.get("identifier_binding")
    if provider_binding is not None and (
        not isinstance(provider_binding, dict) or provider_binding.get("status") not in ("exact", "bound", "matched")
    ):
        errors.append({"field": "identifier", "code": "provider_identifier_unverified"})
    return errors


def source_metadata_errors(source: dict[str, Any]) -> list[str]:
    errors = [
        key
        for key in ("provider", "primary_lineage", "method", "locator", "fetched_at")
        if not isinstance(source.get(key), str) or not source[key].strip()
    ]
    if "fetched_at" not in errors:
        try:
            observed = datetime.fromisoformat(source["fetched_at"].replace("Z", "+00:00"))
            if observed.tzinfo is None or observed.utcoffset() is None:
                errors.append("fetched_at")
        except ValueError:
            errors.append("fetched_at")
    return errors


def catalog_ref(value: Any, *, namespace: str, entity_kind: str) -> bool:
    return (
        isinstance(value, dict)
        and value.get("provider") == "partsapi_ru"
        and value.get("namespace") == namespace
        and value.get("entity_kind") == entity_kind
        and isinstance(value.get("id"), (str, int))
        and not isinstance(value.get("id"), bool)
        and str(value["id"]).isdigit()
        and int(value["id"]) > 0
        and (namespace != "tecdoc" or value.get("carType") in ("PC", "CV", "Motorcycle"))
    )


def _directory_context(operation: str, params: dict[str, Any], context: dict[str, Any]) -> list[str] | None:
    references = {
        "getModels": ("tecdoc", {"make": ("make", "makeId")}),
        "getCars": ("tecdoc", {"make": ("make", "makeId"), "model": ("model", "modelId")}),
        "toModels": ("maintenance", {"brand": ("brand", "brandId")}),
        "toTypes": ("maintenance", {"model": ("model", "modelId")}),
        "toParts": ("maintenance", {"type": ("modification", "typeId")}),
        "toDopusk": ("maintenance", {"type": ("modification", "typeId")}),
        "toOils": ("maintenance", {"type": ("modification", "typeId")}),
        "norms_motors": ("autonorms", {"model": ("model", "modelId")}),
        "norms_times": ("autonorms", {"motor": ("motor", "motorId")}),
        "fill_volumes": ("autonorms", {"car": ("modification", "carId")}),
    }
    specification = references.get(operation)
    if specification is None:
        return None
    namespace, fields = specification
    errors = []
    for key, (kind, parameter) in fields.items():
        reference = context.get(key)
        if not isinstance(reference, dict) or not catalog_ref(reference, namespace=namespace, entity_kind=kind):
            errors.append("invalid_" + key + "_reference")
        elif str(reference["id"]) != str(params.get(parameter)):
            errors.append(key + "_reference_mismatch")
        elif namespace == "tecdoc" and reference.get("carType") != params.get("carType"):
            errors.append("vehicle_type_reference_mismatch")
    return errors


def validate_catalog_context(operation: str, params: dict[str, Any], context: Any) -> list[str]:
    """Optional typed context strengthens direct calls; never infer cross-namespace IDs."""
    parameter_errors = catalog_parameter_errors(params)
    if parameter_errors or context is None:
        return parameter_errors
    if not isinstance(context, dict):
        return ["invalid_catalog_context"]
    directory_errors = _directory_context(operation, params, context)
    if directory_errors is not None:
        return directory_errors
    if operation not in {"search_tree", "articles", "engine_info", "getPassengerCarInfo"}:
        return ["unsupported_catalog_context_operation"]
    modification = context.get("modification")
    if not isinstance(modification, dict) or not catalog_ref(
        modification, namespace="tecdoc", entity_kind="modification"
    ):
        return ["invalid_modification_reference"]
    requested = params.get("carId", params.get("TYPE_ID"))
    car_type = params.get("carType", params.get("TYPE"))
    if requested is not None and str(requested) != str(modification["id"]):
        return ["modification_reference_mismatch"]
    if car_type is not None and car_type != modification.get("carType"):
        return ["vehicle_type_reference_mismatch"]
    if operation == "articles":
        node = context.get("tree_node")
        if not isinstance(node, dict) or not catalog_ref(node, namespace="tecdoc", entity_kind="tree_node"):
            return ["invalid_tree_node_reference"]
        if str(node["id"]) != str(params.get("strId")) or node.get("parent") != modification:
            return ["tree_node_reference_mismatch"]
        if not isinstance(node.get("tree_sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", node["tree_sha256"]) is None:
            return ["tree_evidence_missing"]
    return []


def catalog_parameter_errors(params: dict[str, Any]) -> list[str]:
    errors = []
    for name, value in params.items():
        if value is None or value == "":
            continue  # Existing required-parameter check supplies the missing stop.
        if name in {"carType", "TYPE"} and value not in ("PC", "CV", "Motorcycle"):
            errors.append("invalid_" + name)
        elif name in {
            "carId",
            "TYPE_ID",
            "strId",
            "ART_ID",
            "SUP_ID",
            "makeId",
            "modelId",
            "typeId",
            "brandId",
            "motorId",
            "TopCategoryId",
            "SubCategoryId",
            "TopCatId",
            "SubCatId",
            "LANG",
        }:
            minimum = 0 if name in {"TopCategoryId", "SubCategoryId", "TopCatId", "SubCatId"} else 1
            if (
                isinstance(value, bool)
                or not isinstance(value, (str, int))
                or not str(value).isdigit()
                or int(value) < minimum
            ):
                errors.append("invalid_" + name)
    return errors


def content_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def context_lineage(context: dict[str, Any], inherited: str | None = None) -> str | None:
    """Retain declared provider origin when a ready profile is carried as context."""
    declared = context.get("primary_lineage") or context.get("provider")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    summary = str(context.get("source_summary") or context.get("source") or "").casefold()
    if any(token in summary for token in ("partsapi", "tecdoc", "vindecode")):
        return "partsapi_ru"
    if any(token in summary for token in ("vpic", "nhtsa", "corgi")):
        return "nhtsa_vpic"
    return inherited


def context_field_evidence(
    context: dict[str, Any], *, inherited: str | None = None, depth: int = 0
) -> list[dict[str, Any]]:
    if depth > 8:
        return []
    lineage = context_lineage(context, inherited)
    rows: list[dict[str, Any]] = []
    provenance: dict[str, Any] = context["provenance"] if isinstance(context.get("provenance"), dict) else {}
    for key in ("vehicle_profile", "vehicle_profile_compact", "crm_vehicle_profile"):
        nested = context.get(key)
        if isinstance(nested, dict):
            rows.extend(context_field_evidence(nested, inherited=lineage, depth=depth + 1))
    for field, value in context.items():
        declared = provenance.get(field) or []
        origins = (
            [
                item.get("primary_lineage") or item.get("provider") or item.get("source")
                for item in declared
                if isinstance(item, dict)
            ]
            if isinstance(declared, list)
            else []
        )
        origin = next((str(item) for item in origins if item), lineage)
        rows.append(
            {
                "field": field,
                "value": value,
                "source": origin or "CRM context",
                "primary_lineage": origin or "explicit_context",
                "derived": origin is not None,
            }
        )
    return rows
