"""Independent identity reads and pure reconciliation; no implicit fallback."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from contextvars import copy_context
from datetime import UTC, datetime
from collections.abc import Callable
from typing import Any, Literal

from . import vin_lookup
from .automotive_contracts import (
    MAX_ROWS,
    binding,
    caller_supplied_claims,
    context_field_evidence,
    finite_number,
    field_origins,
    identity_errors,
    invalid,
    primary_lineage,
    result,
)
from .vehicle_identity import (
    _checked_vpic_diagnostics,
    _normalized_identity_value,
    _vpic_has_clean_diagnostics,
    identity_values_agree,
)
from .vehicle_identity_inputs import CONTEXT_FIELDS, validate_identity_input
from .vehicle_identity_policy import build_parts_lookup_readiness
from .vehicle_identity_transport import wmi_for_vin
from .listing_executor import BoundedListingExecutor, ListingExecutionError, read_execution_deadline

_BATCH_READ_EXECUTOR = BoundedListingExecutor(max_workers=2, max_waiting_calls=2, timeout_seconds=120)

FIELDS = (
    "make",
    "manufacturer",
    "model",
    "model_family",
    "model_year",
    "production_year",
    "production_date",
    "engine",
    "engine_code",
    "displacement_cc",
    "power_hp",
    "power_kw",
    "transmission",
    "transmission_speeds",
    "drivetrain",
    "market",
    "modification",
    "series",
    "trim",
    "options",
    "country",
    "manufacturer_country",
    "vehicle_type",
)


def _validated_profile(profile: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]], list[dict[str, str]]]:
    """Apply the same scalar/year/options guards as direct E2 input before reuse."""
    profile = dict(profile)
    speeds = profile.get("transmission_speeds")
    if isinstance(speeds, str) and re.fullmatch(r"[0-9]{1,2}", speeds.strip()):
        # The main E2 provider merge historically retains vPIC's numeric string.
        profile["transmission_speeds"] = int(speeds.strip())
    prepared = validate_identity_input("", {key: value for key, value in profile.items() if key in FIELDS})
    cleaned = prepared["context"]
    errors = list(prepared["errors"])
    for field in FIELDS:
        value = profile.get(field)
        if field in CONTEXT_FIELDS or value in (None, ""):
            continue
        if not isinstance(value, str):
            errors.append({"field": field, "code": "expected_string", "stage": "input_validation"})
        elif value.strip():
            cleaned[field] = value.strip()
    for error in errors:
        cleaned.pop(error["field"], None)
    return cleaned, errors, prepared["normalization_notes"]


def _vpic_profile(response: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    raw = response.get("vehicle")
    if not isinstance(raw, dict):
        return {}, []
    fields = {
        "manufacturername": "manufacturer",
        "modelyear": "model_year",
        "enginemodel": "engine",
        "transmissionstyle": "transmission",
        "transmissionspeeds": "transmission_speeds",
        "drivetype": "drivetrain",
        "displacementcc": "displacement_cc",
        "enginehp": "power_hp",
        "vehicletype": "vehicle_type",
        "bodyclass": "body_class",
        "plantcountry": "plant_country",
        "plantcity": "plant_city",
        "plantcompanyname": "plant_company_name",
        "plantstate": "plant_state",
        "engineconfiguration": "engine_configuration",
        "enginecylinders": "engine_cylinders",
        "displacementl": "engine_displacement_l",
        "fueltypeprimary": "fuel_type",
        "fueltypesecondary": "fuel_type_secondary",
        "vehicledescriptor": "vehicle_descriptor",
        "series2": "series2",
    }
    family_fields = {
        "make",
        "manufacturer",
        "manufacturername",
        "model",
        "vehicletype",
        "bodyclass",
        "plantcountry",
        "plantcity",
        "plantcompanyname",
        "plantstate",
        "vehicledescriptor",
    }
    profile = {
        fields.get(str(key), str(key)): value
        for key, value in raw.items()
        if key != "vin" and (_vpic_has_clean_diagnostics(response) or key in family_fields)
    }
    for field in ("transmission_speeds", "displacement_cc", "power_hp"):
        if field in profile:
            number = finite_number(profile[field], minimum=0)
            if number is not None:
                profile[field] = int(number) if field == "transmission_speeds" and number.is_integer() else number
    cleaned, errors, _notes = _validated_profile(profile)
    profile = {**{key: value for key, value in profile.items() if key not in FIELDS}, **cleaned}
    return profile, errors


def inspect_vehicle_identifier(identifier: str, identifier_type: str = "auto") -> dict[str, Any]:
    prepared = validate_identity_input(identifier, identifier_type=identifier_type)
    if not prepared["ok"]:
        return invalid("inspect_vehicle_identifier", *[row["field"] for row in prepared["errors"]])
    classified = vin_lookup.classify_identifier(prepared["identifier"], identifier_type=identifier_type)
    if identifier_type != "auto" and classified.kind != identifier_type:
        return invalid("inspect_vehicle_identifier", "identifier_type")
    data = {
        "identifier_kind": classified.kind,
        "valid": classified.kind != "unknown",
        "wmi": classified.normalized[:3] if classified.kind == "vin" else None,
        "input_binding": binding(identifier, identifier_type),
        "format": {"length": len(classified.normalized), "notes": classified.notes},
    }
    return result("inspect_vehicle_identifier", "success" if data["valid"] else "unsupported", data)


def _provider_result(tool_id: str, response: dict[str, Any], identifier: str, *, wmi: bool = False) -> dict[str, Any]:
    outcome = response.get("outcome") or ("success" if response.get("ok") else "provider_error")
    outcome = {
        "empty_result": "empty",
        "adapter_malformed_payload": "parse_error",
        "identity_mismatch": "partial",
        "identity_unverified": "partial",
        "transport_timeout": "provider_error",
    }.get(str(outcome), outcome)
    profile = response.get("vehicle") or response.get("manufacturer") or {}
    if not isinstance(profile, dict):
        profile = {}
    if wmi:
        wmi_profile = response.get("wmi_profile") or {}
        profile = {
            "manufacturer": wmi_profile.get("name") or wmi_profile.get("manufacturername"),
            "make": wmi_profile.get("make"),
            "country": wmi_profile.get("country"),
            "vehicle_type": wmi_profile.get("vehicletype"),
        }
        profile, field_errors, _notes = _validated_profile(profile)
        # Preserve the public WMI shape; null slots remain unknown family fields.
        profile = {field: profile.get(field) for field in ("manufacturer", "make", "country", "vehicle_type")}
    else:
        profile, field_errors = _vpic_profile(response)
    bound = response.get("identifier_binding") or {}
    if bound.get("status") in {"mismatch", "unverified", "missing"}:
        outcome = "partial"
    evidence = [
        {
            "provider": "nhtsa_vpic",
            "primary_lineage": "nhtsa_vpic",
            "method": tool_id,
            "fetched_at": datetime.now(UTC).isoformat(),
            "locator": "https://vpic.nhtsa.dot.gov/api/vehicles/" + ("DecodeWMI" if wmi else "DecodeVinValues"),
            "scope": "wmi" if wmi else "decoded_vin",
            "identifier_binding": bound,
        }
    ]
    diagnostics = response.get("diagnostics") or response.get("provider_errors") or []
    if not wmi and any(
        key in response for key in ("error_codes", "error_code", "has_error_text", "diagnostics_status")
    ):
        diagnostics = _checked_vpic_diagnostics(response)
        coverage = response.get("coverage")
        if isinstance(coverage, str) and coverage in {"basic", "partial_or_unsupported"}:
            diagnostics["coverage"] = coverage
    data = {
        "vehicle_profile": profile,
        "input_binding": binding(identifier, "vin") if not wmi else None,
        "wmi": identifier if wmi else None,
        "identifier_binding": bound,
        "diagnostics": diagnostics,
    }
    if response.get("model_year_hint_requested") is not None:
        data["model_year_hint_requested"] = response["model_year_hint_requested"]
        if identity_values_agree("model_year", profile.get("model_year"), response["model_year_hint_requested"]):
            data["field_statuses"] = {"model_year": "candidate"}
    if field_errors:
        outcome = "partial"
        data["field_validation_errors"] = field_errors
    response = result(
        tool_id,
        str(outcome),
        data,
        evidence=evidence,
        network_calls=1,
        attempts=[{"provider": "nhtsa_vpic", "method": tool_id, "outcome": outcome}],
        missing_fields=[field for field in ("make",) if profile.get(field) in (None, "")],
    )
    return response if wmi else vin_lookup._redact_identifier_payload(response, vin_lookup.normalize_vin(identifier))


def decode_vin_vpic(
    identifier: str, model_year: int | None = None, timeout_seconds: float = 8, extended: bool = False
) -> dict[str, Any]:
    prepared = validate_identity_input(identifier, model_year=model_year, identifier_type="vin")
    timeout = finite_number(timeout_seconds, minimum=0.1)
    if (
        not prepared["ok"]
        or vin_lookup.classify_identifier(prepared["identifier"], identifier_type="vin").kind != "vin"
        or timeout is None
        or timeout > 8
        or not isinstance(extended, bool)
    ):
        return invalid("decode_vin_vpic", "identifier_or_options")
    response = vin_lookup.decode_vin_vpic(
        prepared["identifier"], model_year=model_year, timeout=timeout, extended=extended
    )
    return _provider_result("decode_vin_vpic", response, prepared["identifier"])


def decode_wmi_vpic(wmi: str, timeout_seconds: float = 8) -> dict[str, Any]:
    timeout = finite_number(timeout_seconds, minimum=0.1)
    if not isinstance(wmi, str) or len(wmi) not in {3, 6} or timeout is None or timeout > 8:
        return invalid("decode_wmi_vpic", "wmi_or_timeout")
    try:
        vin_lookup._wmi_request(wmi)
    except (ValueError, TypeError):
        return invalid("decode_wmi_vpic", "wmi")
    return _provider_result("decode_wmi_vpic", vin_lookup.decode_wmi_vpic(wmi, timeout=timeout), wmi.upper(), wmi=True)


def _profile(row: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = row["data"] if isinstance(row.get("data"), dict) else row
    profile = data.get("vehicle_profile")
    return profile if isinstance(profile, dict) else {}


def _source_lineage(row: dict[str, Any], index: int) -> str:
    evidence = row.get("evidence") or row.get("evidence_sources") or []
    if not isinstance(evidence, list):
        return f"supplied_result_{index}"
    primary = sorted(
        {
            str(item.get("primary_lineage") or item.get("provider") or item.get("source"))
            for item in evidence
            if isinstance(item, dict)
        }
    )
    return "+".join(primary) if primary else f"supplied_result_{index}"


def _field_level(
    row: dict[str, Any], data: dict[str, Any], field: str, origins: list[dict[str, Any]], *, wmi: bool
) -> str:
    """Keep an upstream level; agreement never upgrades a candidate."""
    statuses = data.get("field_statuses")
    declared = statuses.get(field) if isinstance(statuses, dict) else None
    if declared is None:
        statuses = row.get("field_statuses")
        declared = statuses.get(field) if isinstance(statuses, dict) else None
    declared = declared.get("status") if isinstance(declared, dict) else declared
    if isinstance(declared, str) and declared in {"candidate", "supported", "observed", "disputed", "missing"}:
        return "candidate" if wmi and declared == "supported" else declared
    if wmi:
        return "candidate"
    strengths = {origin.get("strength") for origin in origins if isinstance(origin.get("strength"), str)}
    if "supported" in strengths:
        return "supported"
    return "candidate" if "candidate" in strengths else "observed"


def _reconciliation_variants(
    row: dict[str, Any], data: dict[str, Any], lineage: str, index: int
) -> tuple[list[dict[str, Any]], str | None]:
    variant = {
        "profile": _profile(row),
        "source_lineage": lineage,
        "result_index": index,
        "outcome": row.get("outcome"),
        "trust": {"state": "caller_supplied_unverified", "binding_authenticates_source": False},
    }
    for key in ("scope", "field_statuses", "field_evidence", "provenance", "field_provenance", "diagnostics"):
        if key in data:
            variant[key] = data[key]
        elif key in row:
            variant[key] = row[key]
    variants = [variant]
    for key in ("variants", "family_candidates"):
        supplied = data.get(key) or []
        if not isinstance(supplied, list) or any(not isinstance(item, dict) for item in supplied):
            return [], "results." + key
        for item in supplied:
            child = {**item, "result_index": index}
            child["trust"] = {"state": "caller_supplied_unverified", "binding_authenticates_source": False}
            child.setdefault("source_lineage", lineage)
            if "scope" in variant:
                child.setdefault("scope", variant["scope"])
            if child not in variants:
                variants.append(child)
    return variants, None


def _reconciliation_years(data: dict[str, Any]) -> tuple[list[Any], str | None]:
    diagnostics = data.get("diagnostics")
    year_diagnostics = diagnostics.get("model_year") if isinstance(diagnostics, dict) else None
    diagnostic_years = year_diagnostics.get("candidate_years", []) if isinstance(year_diagnostics, dict) else []
    years: list[Any] = []
    for path, supplied in (
        ("model_year_candidates", data.get("model_year_candidates", [])),
        ("diagnostics.model_year.candidate_years", diagnostic_years),
    ):
        if not isinstance(supplied, list):
            return [], "results." + path
        years.extend(item for item in supplied if item not in years)
    return years, None


def _caller_supplied_result(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return caller_supplied_claims(row)
    except ValueError:
        return None


def _append_context_observations(
    values: dict[str, list[dict[str, Any]]], context: dict[str, Any] | None, prepared: dict[str, Any]
) -> None:
    origins = {row["field"]: row for row in context_field_evidence(context or {})}
    for field, value in prepared.items():
        if field in values and value not in (None, ""):
            values[field].append(
                {
                    "value": value,
                    "primary_lineage": origins.get(field, {}).get("primary_lineage", "explicit_context"),
                    "source_kind": "caller",
                    "strength": "candidate",
                    "bound": False,
                    "independent": False,
                }
            )


def _reconciliation_profile(
    row: dict[str, Any], variant: dict[str, Any], conflicts: list[dict[str, Any]], index: int
) -> dict[str, Any]:
    profile, errors, notes = _validated_profile(_profile(row))
    if errors:
        conflicts.extend(
            {**error, "code": "invalid_vehicle_field", "reason": error["code"], "result_index": index}
            for error in errors
        )
        variant["field_validation_errors"] = errors
    if notes:
        variant["normalization_notes"] = notes
    return profile


def _present_reconciliation_summary(response: dict[str, Any], identifier: str) -> dict[str, Any]:
    """Keep reusable root origins; variants reference their existing source records."""
    private_payload_keys = {"payload", "raw_payload", "raw_response", "provider_payload", "raw_body", "body_html"}

    def public_value(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: public_value(item) for key, item in value.items() if key not in private_payload_keys}
        if isinstance(value, list):
            return [public_value(item) for item in value]
        if isinstance(value, str) and identifier:
            return value.replace(identifier, "[identifier-redacted]")
        return value

    compact = public_value(response)
    data = compact["data"]
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for field, origins in data.get("provenance", {}).items():
        for index, origin in enumerate(origins):
            candidates.append((origin, {"path": "data.provenance", "field": field, "index": index}))
    for index, origin in enumerate(compact.get("evidence", [])):
        candidates.append((origin, {"path": "evidence", "index": index}))
    additional: list[dict[str, Any]] = []

    def origin_reference(origin: dict[str, Any]) -> dict[str, Any]:
        for stored, reference in candidates:
            same_field = origin.get("field") and origin.get("field") == stored.get("field")
            if stored == origin or (
                same_field and all(key in stored and stored[key] == value for key, value in origin.items())
            ):
                return dict(reference)
        reference = {"path": "data.source_evidence", "index": len(additional)}
        additional.append(origin)
        candidates.append((origin, reference))
        return dict(reference)

    def compact_variant(variant: dict[str, Any]) -> dict[str, Any]:
        projected = dict(variant)
        for key in ("field_evidence", "evidence"):
            origins = projected.get(key)
            if isinstance(origins, list) and all(isinstance(origin, dict) for origin in origins):
                projected.pop(key)
                projected[key + "_refs"] = [origin_reference(origin) for origin in origins]
        provenance = projected.get("provenance")
        if isinstance(provenance, dict) and all(
            isinstance(origins, list) and all(isinstance(origin, dict) for origin in origins)
            for origins in provenance.values()
        ):
            projected.pop("provenance")
            projected["provenance_refs"] = {
                field: [origin_reference(origin) for origin in origins] for field, origins in provenance.items()
            }
        return projected

    variants = data.get("variants", [])
    data["variants"] = [compact_variant(variant) for variant in variants]
    data.pop("family_candidates", None)
    data["family_candidate_refs"] = list(range(len(variants)))
    if additional:
        data["source_evidence"] = additional
    compact["presentation"] = {
        "detail": "summary",
        "family_candidates_ref": "data.variants",
        "origin_references": "path, optional field, index",
    }
    return compact


def _expand_reconciliation_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Resolve only our variant references; reusable root provenance stays plain."""
    data = row["data"] if isinstance(row.get("data"), dict) else row
    if "family_candidate_refs" not in data:
        return row

    def resolve(reference: Any) -> dict[str, Any]:
        if not isinstance(reference, dict) or set(reference) - {"path", "field", "index"}:
            raise ValueError("invalid_summary_origin_reference")
        path, index = reference.get("path"), reference.get("index")
        if path == "data.provenance":
            if not isinstance(reference.get("field"), str):
                raise ValueError("invalid_summary_origin_reference")
            records = (data.get("provenance") or {}).get(reference["field"])
        elif path == "data.source_evidence":
            records = data.get("source_evidence")
        elif path == "evidence":
            records = row.get("evidence")
        else:
            raise ValueError("invalid_summary_origin_reference")
        if not isinstance(records, list) or type(index) is not int or not 0 <= index < len(records):
            raise ValueError("invalid_summary_origin_reference")
        origin = records[index]
        if not isinstance(origin, dict):
            raise ValueError("invalid_summary_origin_reference")
        return dict(origin)

    variants = data.get("variants")
    if not isinstance(variants, list) or any(not isinstance(variant, dict) for variant in variants):
        raise ValueError("invalid_summary_variants")
    expanded = []
    for variant in variants:
        item = dict(variant)
        for key in ("field_evidence", "evidence"):
            references = item.pop(key + "_refs", None)
            if references is not None:
                if not isinstance(references, list):
                    raise ValueError("invalid_summary_origin_reference")
                item[key] = [resolve(reference) for reference in references]
        references = item.pop("provenance_refs", None)
        if references is not None:
            if not isinstance(references, dict) or any(not isinstance(values, list) for values in references.values()):
                raise ValueError("invalid_summary_origin_reference")
            item["provenance"] = {
                field: [resolve(reference) for reference in values] for field, values in references.items()
            }
        expanded.append(item)
    indices = data["family_candidate_refs"]
    if not isinstance(indices, list) or any(
        type(index) is not int or not 0 <= index < len(expanded) for index in indices
    ):
        raise ValueError("invalid_summary_family_reference")
    data = {**data, "variants": expanded, "family_candidates": [expanded[index] for index in indices]}
    data.pop("family_candidate_refs")
    return {**row, "data": data} if isinstance(row.get("data"), dict) else data


def reconcile_vehicle_identity(
    identifier: str,
    results: list[dict[str, Any]],
    context: dict[str, Any] | None = None,
    identifier_type: str = "auto",
    detail: Literal["summary", "full"] = "full",
) -> dict[str, Any]:
    prepared = validate_identity_input(identifier, context, identifier_type=identifier_type)
    if (
        detail not in {"summary", "full"}
        or not prepared["ok"]
        or not isinstance(results, list)
        or len(results) > MAX_ROWS
        or any(not isinstance(row, dict) for row in results)
    ):
        return invalid(
            "reconcile_vehicle_identity", "detail" if detail not in {"summary", "full"} else "identifier_or_results"
        )
    expected = binding(identifier, identifier_type)
    values: dict[str, list[dict[str, Any]]] = {field: [] for field in FIELDS}
    levels: dict[str, list[str]] = {field: [] for field in FIELDS}
    disputed_fields: set[str] = set()
    warnings: list[str] = []
    conflicts: list[dict[str, Any]] = []
    variants: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    year_candidates: list[Any] = []
    for index, row in enumerate(results):
        original = row
        try:
            row = _expand_reconciliation_summary(row)
        except (ValueError, AttributeError):
            return invalid("reconcile_vehicle_identity", "results.summary_references")
        data: dict[str, Any] = row["data"] if isinstance(row.get("data"), dict) else row
        original_data = original["data"] if isinstance(original.get("data"), dict) else original
        upstream_errors = identity_errors(original, original_data)
        if upstream_errors:
            conflicts.extend({**item, "result_index": index} for item in upstream_errors)
            disputed_fields.update(
                item["field"]
                for item in upstream_errors
                if item.get("code") in ("disputed_ready_identity", "ready_identity_evidence_conflict")
                and item.get("field") in FIELDS
            )
            warnings.append(f"result_{index}_not_usable_as_vehicle_facts")
            continue
        wmi = data.get("wmi")
        classified = vin_lookup.classify_identifier(prepared["identifier"], identifier_type=identifier_type)
        wmi_matches = (
            isinstance(wmi, str)
            and classified.kind == "vin"
            and wmi.strip().upper() == wmi_for_vin(classified.normalized)
        )
        if data.get("input_binding") != expected and not wmi_matches:
            conflicts.append({"field": "identifier", "code": "result_binding_mismatch", "result_index": index})
            continue
        cleaned_row = _caller_supplied_result(row)
        if cleaned_row is None:
            return invalid("reconcile_vehicle_identity", "results.caller_claims_too_deep")
        row = cleaned_row
        data = row["data"] if isinstance(row.get("data"), dict) else row
        supplied_evidence = row.get("evidence") or row.get("field_evidence") or []
        if not isinstance(supplied_evidence, list) or any(not isinstance(item, dict) for item in supplied_evidence):
            return invalid("reconcile_vehicle_identity", "results.evidence")
        lineage = _source_lineage(row, index)
        row_variants, error = _reconciliation_variants(row, data, lineage, index)
        if error:
            return invalid("reconcile_vehicle_identity", error)
        variants.extend(row_variants)
        # A WMI-only match cannot bind model-year alternatives to this VIN.
        row_years, error = _reconciliation_years(data) if not wmi else ([], None)
        if error:
            return invalid("reconcile_vehicle_identity", error)
        if row_years:
            row_variants[0]["model_year_candidates"] = row_years
            year_candidates.extend(item for item in row_years if item not in year_candidates)
        profile = _reconciliation_profile(row, row_variants[0], conflicts, index)
        for field in FIELDS:
            if profile.get(field) not in (None, "") and (
                not wmi or field in {"make", "manufacturer", "country", "manufacturer_country", "vehicle_type"}
            ):
                origins = field_origins(row, field, profile[field])
                level = _field_level(row, data, field, origins, wmi=bool(wmi))
                if level == "missing":
                    continue
                levels[field].append("candidate" if level == "supported" else level)
                for origin in origins:
                    values[field].append(
                        {
                            **origin,
                            "value": profile[field],
                            "primary_lineage": primary_lineage(origin),
                            "independent": False,
                            "result_index": index,
                        }
                    )
        evidence.extend(supplied_evidence)
    _append_context_observations(values, context, prepared["context"])
    profile, statuses, provenance = {}, {}, {}
    for field, observations in values.items():
        if not observations:
            continue
        # Equal nonempty keys are the comparator's first success branch. Keep
        # every raw pair for fallback: its model-family relation is not transitive.
        normalized = [_normalized_identity_value(field, row["value"]) for row in observations]
        if any(
            not (
                (normalized[index] and normalized[index] == normalized[right_index])
                or identity_values_agree(field, left["value"], observations[right_index]["value"])
            )
            for index, left in enumerate(observations)
            for right_index in range(index + 1, len(observations))
        ):
            conflicts.append({"field": field, "code": "conflicting_vehicle_fields", "alternatives": observations})
            statuses[field] = "disputed"
        else:
            profile[field] = observations[0]["value"]
            statuses[field] = "candidate" if "candidate" in levels[field] else "observed"
        provenance[field] = observations
    for field in disputed_fields:
        profile.pop(field, None)
        statuses[field] = "disputed"
    missing = [
        field
        for field in ("make", "model", "engine", "transmission", "market", "production_date")
        if field not in profile
    ]
    data = {
        "ok": True,
        "schema_version": 2,
        "input_binding": expected,
        "vehicle_profile": profile,
        "field_statuses": statuses,
        "provenance": provenance,
        "variants": variants,
        "model_year_candidates": year_candidates,
        "family_candidates": variants,
        "conflicts": conflicts,
        "missing_fields": missing,
        "identifier": {"kind": expected["identifier_kind"]},
        "diagnostics": {"model_year": {"candidate_years": year_candidates}} if year_candidates else {},
        "confidence": 0.0,
        "trust": {"state": "caller_supplied_unverified", "binding_authenticates_source": False},
    }
    data["parts_lookup_readiness"] = build_parts_lookup_readiness(data)
    response = result(
        "reconcile_vehicle_identity",
        "partial" if conflicts or missing else "success",
        data,
        conflicts=conflicts,
        missing_fields=missing,
        warnings=warnings,
        evidence=evidence,
    )
    return _present_reconciliation_summary(response, prepared["identifier"]) if detail == "summary" else response


def compare_vehicle_modifications(context: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    from .tecdoc_vehicle_selection import _FIELDS, _missing_discriminators, _profile_conflicts

    if not isinstance(context, dict) or not isinstance(candidates, list) or len(candidates) > MAX_ROWS:
        return invalid("compare_vehicle_modifications", "context_or_candidates")
    if any(not isinstance(row, dict) for row in candidates):
        return invalid("compare_vehicle_modifications", "candidates")
    values = {field: [context[field]] if context.get(field) not in (None, "") else [] for field in _FIELDS}
    known_year = _validated_profile({"production_year": context.get("production_year")})[0].get("production_year")
    comparisons: list[dict[str, Any]] = []
    for row in candidates:
        conflicts = _profile_conflicts(row, values)
        if known_year is not None:
            candidate_year = _validated_profile({"production_year": row.get("production_year")})[0].get(
                "production_year"
            )
            if (
                candidate_year is not None
                and candidate_year != known_year
                and not any(item.get("field") == "production_year" for item in conflicts)
            ):
                conflicts.append(
                    {
                        "field": "production_year",
                        "identity": context["production_year"],
                        "catalogue": row["production_year"],
                    }
                )
        comparisons.append({"candidate": row, "conflicts": conflicts})
    alternatives = [row["candidate"] for row in comparisons if not row["conflicts"]]
    missing = _missing_discriminators(alternatives, values) if len(alternatives) > 1 else []
    return result(
        "compare_vehicle_modifications",
        "partial" if missing else "success",
        {
            "comparisons": comparisons,
            "alternatives": alternatives,
            "selected": alternatives[0] if len(alternatives) == 1 else None,
        },
        missing_fields=missing,
        conflicts=[conflict for row in comparisons for conflict in row["conflicts"]],
    )


def _selected_decoders() -> dict[str, Callable[..., dict[str, Any]]]:
    from . import automotive_offline

    return {
        "decode_vin_vpic": decode_vin_vpic,
        "decode_wmi_vpic": decode_wmi_vpic,
        **{
            name: getattr(automotive_offline, name)
            for name in (
                "decode_wmi_local",
                "decode_frame_local",
                "vin_brand_details",
                "vininfo_decode",
                "corgi_decode",
            )
        },
    }


def _expired_batch_row(decoder: str) -> dict[str, Any]:
    return result(decoder, "deadline_exceeded", {}, warnings=["batch_deadline_exceeded"])


class _QueuedDecode:
    """Serialize admission with abandonment while the real executor owns the worker."""

    def __init__(self, item: dict[str, Any] | str, decoder: str, run: Callable[..., dict[str, Any]], expires: float):
        self.item, self.decoder, self.run, self.expires = item, decoder, run, expires
        self.lock = threading.Lock()
        self.abandoned = False
        self.started = False
        self.completed: dict[str, Any] | None = None
        self.finished_at: float | None = None

    def execute(self) -> dict[str, Any]:
        identifier = self.item.get("identifier") if isinstance(self.item, dict) else self.item
        with self.lock:
            remaining = self.expires - time.monotonic()
            timed = self.decoder in {"decode_vin_vpic", "decode_wmi_vpic", "corgi_decode"}
            if self.abandoned or remaining <= 0 or (timed and remaining < 0.1):
                return _expired_batch_row(self.decoder)
            if not isinstance(identifier, str):
                return invalid(self.decoder, "identifier")
            options = {"timeout_seconds": min(8, remaining)} if timed else {}
            self.started = True
        try:
            row = self.run(identifier, **options)
        except (OSError, ValueError, TypeError) as exc:
            network = self.decoder in {"decode_vin_vpic", "decode_wmi_vpic"}
            row = result(
                self.decoder,
                "provider_error",
                {},
                warnings=[type(exc).__name__],
                network_calls=int(network),
                attempts=[{"method": self.decoder, "outcome": "unknown"}] if network else [],
            )
        with self.lock:
            self.completed, self.finished_at = row, time.monotonic()
        return row

    def abandon(self) -> dict[str, Any]:
        with self.lock:
            self.abandoned = True
            if self.completed is not None and self.finished_at is not None and self.finished_at <= self.expires:
                return self.completed
            row = _expired_batch_row(self.decoder)
            if self.completed is not None:
                row["execution"] = dict(self.completed["execution"])
            elif self.started and self.decoder in {"decode_vin_vpic", "decode_wmi_vpic"}:
                row["execution"] = {
                    "network_calls": None,
                    "network_calls_lower_bound": 0,
                    "decoder_started": True,
                    "attempts": [{"method": self.decoder, "outcome": "unknown"}],
                }
                row["warnings"].append("Issued synchronous read may continue; its network count is not yet known.")
            return row


def _batch_response(decoder: str, rows: list[dict[str, Any]], deadline: float) -> dict[str, Any]:
    known_calls = sum(row["result"]["execution"].get("network_calls") or 0 for row in rows)
    unknown = any(row["result"]["execution"].get("network_calls") is None for row in rows)
    response = result(
        "decode_vehicle_batch",
        "partial" if any(row["result"]["outcome"] != "success" for row in rows) else "success",
        {"decoder": decoder, "items": rows, "deadline_seconds": deadline},
        network_calls=known_calls,
        attempts=[attempt for row in rows for attempt in row["result"]["execution"]["attempts"]],
        warnings=[
            "Already-issued synchronous transport is bounded by its timeout; disconnect does not prove cancellation upstream."
        ],
    )
    if unknown:
        response["execution"].update(network_calls=None, network_calls_lower_bound=known_calls)
    return response


async def _run_batch_read(work: _QueuedDecode) -> dict[str, Any]:
    token = read_execution_deadline.set(work.expires)
    context = copy_context()
    try:
        return await _BATCH_READ_EXECUTOR.run(lambda: context.run(work.execute))
    except ListingExecutionError as error:
        row = work.abandon()
        if error.code != "provider_wait_timeout":
            outcome = (
                error.code if error.code in {"provider_busy", "provider_executor_unavailable"} else "provider_error"
            )
            row.update(ok=False, outcome=outcome, warnings=[error.code])
        row.update(
            error=error.code,
            stage=error.stage,
            adapter_started=work.started,
            adapter_may_continue=error.adapter_may_continue,
            retry_attempted=False,
            retryable=False,
        )
        return row
    except asyncio.CancelledError:
        work.abandon()
        raise
    finally:
        read_execution_deadline.reset(token)


def decode_vehicle_batch(
    items: list[dict[str, Any] | str], decoder: str, deadline_seconds: float = 30
) -> dict[str, Any]:
    decoders = _selected_decoders()
    deadline = finite_number(deadline_seconds, minimum=0.1)
    if (
        not isinstance(items, list)
        or len(items) > MAX_ROWS
        or decoder not in decoders
        or deadline is None
        or deadline > 120
    ):
        return invalid("decode_vehicle_batch", "items_or_decoder_or_deadline")
    expires = time.monotonic() + deadline
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        row = _QueuedDecode(item, decoder, decoders[decoder], expires).execute()
        rows.append({"item_index": index, "result": row})
    return _batch_response(decoder, rows, deadline)


async def decode_vehicle_batch_async(
    items: list[dict[str, Any] | str], decoder: str, deadline_seconds: float = 30
) -> dict[str, Any]:
    """Queue wait uses the original deadline; abandoned rows cannot acquire admission."""
    decoders = _selected_decoders()
    deadline = finite_number(deadline_seconds, minimum=0.1)
    if (
        not isinstance(items, list)
        or len(items) > MAX_ROWS
        or decoder not in decoders
        or deadline is None
        or deadline > 120
    ):
        return invalid("decode_vehicle_batch", "items_or_deadline")
    rows: list[dict[str, Any]] = []
    expires = time.monotonic() + deadline
    inherited_deadline = read_execution_deadline.get()
    if inherited_deadline is not None:
        expires = min(expires, inherited_deadline)
    for index, item in enumerate(items):
        remaining = expires - time.monotonic()
        if remaining <= 0:
            row = _expired_batch_row(decoder)
        else:
            work = _QueuedDecode(item, decoder, decoders[decoder], expires)
            row = await _run_batch_read(work)
        rows.append({"item_index": index, "result": row})
    return _batch_response(decoder, rows, deadline)
