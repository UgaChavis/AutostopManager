"""Shared evidence contracts for independently callable automotive operations."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from functools import lru_cache
from typing import Any

from .vin_lookup import classify_identifier

SCHEMA_VERSION = "autostop.automotive-result.v1"
MAX_ROWS = 500
PUBLIC_OEM_CATALOG_NAMESPACES = {
    "elcats_catalog": "elcats_epc",
    "japancats_catalog": "japancats_epc",
    "exist_ssangyong_catalog": "exist_ssangyong_epc",
}
_PUBLIC_OEM_PARENT_KINDS = {
    "group": {"modification", "group"},
    "diagram": {"group"},
    "part": {"diagram"},
}


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


_CALLER_FALSE_FLAGS = {
    "bound",
    "independent",
    "verified",
    "fitment_confirmed",
    "vin_fitment_confirmed",
    "oem_confirmed",
    "exact_applicability_confirmed",
    "independent_vehicle_confirmation",
    "provider_identifier_is_vehicle_confirmation",
    "can_write_final_material_line_now",
    "can_prepare_manual_writeback",
    "source_authenticated",
    "independent_source_verified",
    "binding_authenticates_source",
    "exact_fitment_confirmed",
}


@lru_cache(maxsize=1)
def _vehicle_claim_fields() -> frozenset[str]:
    from .automotive_identity import FIELDS
    from .tecdoc_vehicle_selection import _FIELDS
    from .vehicle_identity import IDENTITY_FIELDS

    return frozenset((*IDENTITY_FIELDS, *_FIELDS, *FIELDS))


def caller_supplied_claims(value: Any, *, depth: int = 0) -> Any:
    """Copy public JSON as unverified claims; a request digest is not authentication."""
    if depth > 32:
        raise ValueError("caller_claims_too_deep")
    if isinstance(value, list):
        return [caller_supplied_claims(item, depth=depth + 1) for item in value]
    if not isinstance(value, dict):
        return value
    clean: dict[str, Any] = {}
    for key, item in value.items():
        if key == "identifier_binding" and isinstance(item, dict):
            # Keep the caller's reported status for context, but do not reuse it
            # as an authenticated provider binding.
            clean["reported_identifier_binding"] = {"status": item.get("status"), "verified": False}
        elif isinstance(key, str) and (key.startswith("ready_for_") or key in _CALLER_FALSE_FLAGS):
            clean[key] = False
        elif key == "source_kind":
            clean[key] = "caller"
        elif key == "strength" and item == "supported":
            clean[key] = "candidate"
        elif key in {"status", "state"} and item == "supported":
            clean[key] = "candidate"
        elif key == "trust":
            clean[key] = {"state": "caller_supplied_unverified", "binding_authenticates_source": False}
        elif key == "confidence_label":
            clean[key] = "low"
        elif key == "confidence" and isinstance(item, (float, int)) and not isinstance(item, bool):
            clean[key] = 0.0
        elif key == "field_statuses" and isinstance(item, dict):
            clean[key] = {
                field: "candidate" if status == "supported" else caller_supplied_claims(status, depth=depth + 1)
                for field, status in item.items()
            }
        elif key == "cross_source_agreement" and isinstance(item, dict):
            agreement = caller_supplied_claims(item, depth=depth + 1)
            agreement["reported_status"] = item.get("status")
            agreement["status"] = "caller_supplied_unverified"
            agreement["matched_fields"] = []
            clean[key] = agreement
        else:
            clean[key] = caller_supplied_claims(item, depth=depth + 1)
    field = value.get("field")
    source_claim = any(key in value for key in ("source", "provider", "primary_lineage")) and (
        field is None or (isinstance(field, str) and field in _vehicle_claim_fields())
    )
    if source_claim:
        clean.update(source_kind="caller", independent=False, bound=False)
        if "strength" in value:
            clean["strength"] = "candidate"
    if depth == 0 or source_claim:
        clean["trust"] = {"state": "caller_supplied_unverified", "binding_authenticates_source": False}
    return clean


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
    if conflicts:
        return None, conflicts
    try:
        row = caller_supplied_claims(row)
    except ValueError:
        return None, [{"field": "vehicle_identity", "code": "caller_claims_too_deep"}]
    readiness = row.get("parts_lookup_readiness")
    if isinstance(readiness, dict):
        readiness["ready_for_family_lookup"] = bool(
            profile.get("make") and (profile.get("model") or profile.get("model_family"))
        )
    return row, []


def _reported_identity_field_conflicts(envelope: dict[str, Any], data: dict[str, Any]) -> list[dict[str, Any]]:
    from .vehicle_identity import identity_values_agree

    profile = data.get("vehicle_profile")
    if not isinstance(profile, dict):
        return []
    claims: list[tuple[Any, Any]] = []
    for row in (envelope,) if data is envelope else (envelope, data):
        evidence = row.get("field_evidence")
        if isinstance(evidence, list):
            claims.extend((item.get("field"), item.get("value")) for item in evidence if isinstance(item, dict))
        provenance = row.get("provenance")
        if isinstance(provenance, dict):
            for field, entries in provenance.items():
                if isinstance(entries, list):
                    claims.extend((field, item.get("value")) for item in entries if isinstance(item, dict))
    conflicts: list[dict[str, Any]] = []
    for field, claimed in claims:
        if not isinstance(field, str) or profile.get(field) in (None, "") or claimed in (None, ""):
            continue
        if type(profile[field]) is type(claimed) and profile[field] == claimed:
            continue
        if not identity_values_agree(field, profile[field], claimed):
            conflict = {"field": field, "code": "ready_identity_evidence_conflict"}
            if conflict not in conflicts:
                conflicts.append(conflict)
    return conflicts


def identity_errors(envelope: dict[str, Any], data: dict[str, Any]) -> list[dict[str, Any]]:
    """A request digest never overrides provider failure or disputed identity."""
    errors: list[dict[str, Any]] = []
    for row in (envelope,) if data is envelope else (envelope, data):
        if row.get("ok") is False or row.get("outcome") not in (None, "success", "partial"):
            errors.append({"field": "vehicle_identity", "code": "failed_ready_identity"})
        supplied = row.get("conflicts", [])
        if not isinstance(supplied, list) or any(not isinstance(item, dict) for item in supplied):
            errors.append({"field": "vehicle_identity", "code": "invalid_identity_conflicts"})
        else:
            errors.extend(item for item in supplied if item not in errors)
        statuses = row.get("field_statuses")
        if isinstance(statuses, dict):
            for field, status in statuses.items():
                if (status.get("status") if isinstance(status, dict) else status) == "disputed":
                    errors.append({"field": field, "code": "disputed_ready_identity"})
        readiness = row.get("parts_lookup_readiness")
        agreements = [row.get("cross_source_agreement")]
        if isinstance(readiness, dict):
            agreements.append(readiness.get("cross_source_agreement"))
        if any(
            isinstance(agreement, dict)
            and (
                agreement.get("status") == "conflict"
                or agreement.get("status") == "identifier_mismatch"
                or agreement.get("status") == "ambiguous_vehicle_modification"
                or bool(agreement.get("conflicting_fields"))
            )
            for agreement in agreements
        ):
            errors.append({"field": "vehicle_identity", "code": "unresolved_identity_agreement"})
        provider_binding = row.get("identifier_binding")
        if provider_binding is not None and (
            not isinstance(provider_binding, dict)
            or provider_binding.get("status") not in ("exact", "bound", "matched")
            or ("verified" in provider_binding and provider_binding["verified"] is not True)
        ):
            errors.append({"field": "identifier", "code": "provider_identifier_unverified"})
    if (
        envelope.get("input_binding") is not None
        and data.get("input_binding") is not None
        and envelope["input_binding"] != data["input_binding"]
    ):
        errors.append({"field": "identifier", "code": "ready_identity_binding_conflict"})
    errors.extend(item for item in _reported_identity_field_conflicts(envelope, data) if item not in errors)
    return errors


def field_origins(envelope: dict[str, Any], field: str, value: Any) -> list[dict[str, Any]]:
    """Prefer evidence for this value over a result's unrelated source summary."""
    from .vehicle_identity import identity_values_agree

    data = envelope["data"] if isinstance(envelope.get("data"), dict) else envelope
    origins: list[dict[str, Any]] = []
    for row in (envelope,) if data is envelope else (envelope, data):
        for key in ("field_evidence", "evidence"):
            supplied = row.get(key)
            if isinstance(supplied, list):
                origins.extend(item for item in supplied if isinstance(item, dict) and item.get("field") == field)
    matched = [item for item in origins if identity_values_agree(field, item.get("value"), value)]
    if matched:
        return matched
    provenance = data.get("provenance")
    if isinstance(provenance, dict) and isinstance(provenance.get(field), list):
        matched = [
            item
            for item in provenance[field]
            if isinstance(item, dict) and identity_values_agree(field, item.get("value"), value)
        ]
        if matched:
            return matched
    declared = data.get("field_provenance")
    if isinstance(declared, dict) and isinstance(declared.get(field), str):
        return [{"value": value, "primary_lineage": declared[field], "independent": False}]
    supplied = envelope.get("evidence") or envelope.get("evidence_sources") or []
    if not isinstance(supplied, list):
        supplied = []
    # A mixed result does not establish which primary source supplied this field.
    known = {
        origin
        for item in supplied
        if isinstance(item, dict)
        for origin in (item.get("primary_lineage") or item.get("provider") or item.get("source"),)
        if isinstance(origin, str) and origin.strip()
    }
    return [{"value": value, "primary_lineage": next(iter(known)) if len(known) == 1 else None, "independent": False}]


def primary_lineage(origin: dict[str, Any]) -> str | None:
    """Canonical aliases share one origin; opaque combined labels stay unverified."""
    value = origin.get("primary_lineage") or origin.get("provider") or origin.get("source")
    if not isinstance(value, str) or not value.strip() or "+" in value:
        return None
    value = value.strip()
    lower = value.casefold()
    if lower in {"local wmi hint", "local wmi hints", "local platform rule", "local_family_rule", "local_wmi_hint"}:
        return "autostop_local_vehicle_rules"
    if lower in {"nhtsa vpic", "nhtsa vpic wmi", "nhtsa_vpic_via_corgi"}:
        return "nhtsa_vpic"
    return value


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


def _valid_catalog_id(value: Any, *, minimum: int = 1) -> bool:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return False
    try:
        return str(value).isdigit() and int(value) >= minimum
    except ValueError:
        return False


def catalog_ref(value: Any, *, namespace: str, entity_kind: str) -> bool:
    return (
        isinstance(value, dict)
        and value.get("provider") == "partsapi_ru"
        and value.get("namespace") == namespace
        and value.get("entity_kind") == entity_kind
        and _valid_catalog_id(value.get("id"))
        and (namespace != "tecdoc" or value.get("carType") in ("PC", "CV", "Motorcycle"))
    )


def _public_vehicle_context(value: Any) -> bool:
    if not isinstance(value, dict) or len(value) > 64:
        return False
    if any(not isinstance(value.get(field), str) or not value[field].strip() for field in ("make", "model")):
        return False
    private_fields = {"vin", "frame", "frame_number", "identifier", "registration_number", "license_plate"}
    if any(not isinstance(key, str) or key.casefold() in private_fields for key in value):
        return False
    try:
        return len(json.dumps(value, allow_nan=False)) <= 16384
    except (TypeError, ValueError, RecursionError):
        return False


def _public_context_matches(context: dict[str, Any], profile: dict[str, Any]) -> bool:
    from .vehicle_identity import identity_values_agree

    if any(profile.get(field) in (None, "") for field in ("make", "model")):
        return False
    for field, value in context.items():
        actual = profile.get(field)
        if value in (None, "", []):
            continue
        if actual in (None, "", []):
            return False
        if isinstance(value, list) and isinstance(actual, list):
            if sorted(map(str, value)) != sorted(map(str, actual)):
                return False
        elif not identity_values_agree(field, value, actual):
            return False
    return True


def public_oem_catalog_ref(
    value: Any,
    *,
    entity_kind: str = "modification",
    vehicle_profile: dict[str, Any] | None = None,
    _depth: int = 0,
) -> bool:
    """Validate native public-catalog refs without interpreting their IDs as TecDoc IDs."""
    if (
        not isinstance(value, dict)
        or _depth > 8
        or not isinstance(entity_kind, str)
        or entity_kind not in {"modification", *_PUBLIC_OEM_PARENT_KINDS}
    ):
        return False
    provider = value.get("provider")
    if (
        not isinstance(provider, str)
        or provider not in PUBLIC_OEM_CATALOG_NAMESPACES
        or value.get("namespace") != PUBLIC_OEM_CATALOG_NAMESPACES[provider]
        or value.get("entity_kind") != entity_kind
    ):
        return False
    if any(
        not isinstance(value.get(field), str)
        or not value[field].strip()
        or len(value[field]) > 2048
        or any(ord(character) < 32 for character in value[field])
        for field in ("id", "entry_id")
    ):
        return False
    context = value.get("vehicle_context")
    if not isinstance(context, dict) or not _public_vehicle_context(context):
        return False
    if vehicle_profile is not None and (
        not isinstance(vehicle_profile, dict) or not _public_context_matches(context, vehicle_profile)
    ):
        return False
    parent = value.get("parent_ref")
    if entity_kind == "modification":
        return parent is None
    if (
        not isinstance(parent, dict)
        or not isinstance(parent.get("entity_kind"), str)
        or parent["entity_kind"] not in _PUBLIC_OEM_PARENT_KINDS[entity_kind]
    ):
        return False
    if any(parent.get(field) != value.get(field) for field in ("provider", "namespace", "entry_id", "vehicle_context")):
        return False
    return public_oem_catalog_ref(parent, entity_kind=parent["entity_kind"], _depth=_depth + 1)


def oem_catalog_ref(
    value: Any, *, entity_kind: str = "modification", vehicle_profile: dict[str, Any] | None = None
) -> bool:
    """OEM capture/fitment dispatcher; PartsAPI directory validators stay unchanged."""
    return catalog_ref(value, namespace="tecdoc", entity_kind=entity_kind) or public_oem_catalog_ref(
        value, entity_kind=entity_kind, vehicle_profile=vehicle_profile
    )


def _public_ref_identity(value: dict[str, Any]) -> dict[str, Any]:
    identity = {
        key: value[key] for key in ("provider", "namespace", "entity_kind", "id", "entry_id", "vehicle_context")
    }
    if value.get("parent_ref") is not None:
        identity["parent_ref"] = _public_ref_identity(value["parent_ref"])
    return identity


def same_oem_catalog_ref(left: Any, right: Any) -> bool:
    """Transport routes do not change a native ref's semantic vehicle/catalog identity."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    kind = left.get("entity_kind")
    if (
        not isinstance(kind, str)
        or not oem_catalog_ref(left, entity_kind=kind)
        or not oem_catalog_ref(right, entity_kind=kind)
        or left.get("provider") != right.get("provider")
        or left.get("namespace") != right.get("namespace")
    ):
        return False
    if left.get("provider") == "partsapi_ru":
        return left == right
    return _public_ref_identity(left) == _public_ref_identity(right)


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
            if not _valid_catalog_id(value, minimum=minimum):
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
    field_provenance = context.get("field_provenance")
    field_provenance = field_provenance if isinstance(field_provenance, dict) else {}
    for key in ("vehicle_profile", "vehicle_profile_compact", "crm_vehicle_profile"):
        nested = context.get(key)
        if isinstance(nested, str):
            try:
                nested = json.loads(nested)
            except (ValueError, TypeError):
                nested = None
        if isinstance(nested, dict):
            rows.extend(context_field_evidence(nested, inherited=lineage, depth=depth + 1))
    for field, value in context.items():
        # Match flatten_identity_context: a blank outer value retains the nested fact.
        # A nonempty explicit override still wins, even over a declared provider origin.
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
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
        field_origin = field_provenance.get(field)
        field_origin = field_origin.strip() if isinstance(field_origin, str) and field_origin.strip() else lineage
        origin = next((str(item) for item in origins if item), field_origin)
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
