"""Independent identity reads and pure reconciliation; no implicit fallback."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from collections.abc import Callable
from typing import Any, Literal

from . import vin_lookup
from .automotive_contracts import (
    MAX_ROWS,
    binding,
    context_field_evidence,
    finite_number,
    field_origins,
    identity_errors,
    invalid,
    primary_lineage,
    result,
)
from .vehicle_identity import _checked_vpic_diagnostics, identity_values_agree
from .vehicle_identity_inputs import validate_identity_input
from .vehicle_identity_policy import build_parts_lookup_readiness

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
        profile = {
            key: value for key, value in profile.items() if key in {"manufacturer", "make", "country", "vehicle_type"}
        }
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
    return result(
        tool_id,
        str(outcome),
        data,
        evidence=evidence,
        network_calls=1,
        attempts=[{"provider": "nhtsa_vpic", "method": tool_id, "outcome": outcome}],
        missing_fields=[field for field in ("make",) if profile.get(field) in (None, "")],
    )


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
        try:
            row = _expand_reconciliation_summary(row)
        except (ValueError, AttributeError):
            return invalid("reconcile_vehicle_identity", "results.summary_references")
        data: dict[str, Any] = row["data"] if isinstance(row.get("data"), dict) else row
        upstream_errors = identity_errors(row, data)
        if upstream_errors:
            conflicts.extend({**item, "result_index": index} for item in upstream_errors)
            disputed_fields.update(
                item["field"]
                for item in upstream_errors
                if item.get("code") == "disputed_ready_identity" and item.get("field") in FIELDS
            )
            warnings.append(f"result_{index}_not_usable_as_vehicle_facts")
            continue
        wmi = data.get("wmi")
        if data.get("input_binding") != expected and wmi != prepared["identifier"][:3]:
            conflicts.append({"field": "identifier", "code": "result_binding_mismatch", "result_index": index})
            continue
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
        profile = _profile(row)
        supplied_evidence = row.get("evidence") or row.get("field_evidence") or []
        if not isinstance(supplied_evidence, list) or any(not isinstance(item, dict) for item in supplied_evidence):
            return invalid("reconcile_vehicle_identity", "results.evidence")
        for field in FIELDS:
            if profile.get(field) not in (None, "") and (
                not wmi or field in {"make", "manufacturer", "country", "manufacturer_country", "vehicle_type"}
            ):
                origins = field_origins(row, field, profile[field])
                level = _field_level(row, data, field, origins, wmi=bool(wmi))
                if level == "missing":
                    continue
                levels[field].append(level)
                for origin in origins:
                    values[field].append(
                        {
                            **origin,
                            "value": profile[field],
                            "primary_lineage": primary_lineage(origin),
                            "independent": origin.get("independent") is True,
                            "result_index": index,
                        }
                    )
        evidence.extend(supplied_evidence)
    context_origins = {row["field"]: row for row in context_field_evidence(context or {})}
    for field, value in prepared["context"].items():
        if field in values and value not in (None, ""):
            values[field].append(
                {
                    "value": value,
                    "primary_lineage": context_origins.get(field, {}).get("primary_lineage", "explicit_context"),
                }
            )
    profile, statuses, provenance = {}, {}, {}
    for field, observations in values.items():
        if not observations:
            continue
        if any(not identity_values_agree(field, observations[0]["value"], row["value"]) for row in observations[1:]):
            conflicts.append({"field": field, "code": "conflicting_vehicle_fields", "alternatives": observations})
            statuses[field] = "disputed"
        else:
            profile[field] = observations[0]["value"]
            statuses[field] = (
                "supported"
                if "supported" in levels[field]
                else "candidate"
                if "candidate" in levels[field]
                else "observed"
            )
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
    comparisons: list[dict[str, Any]] = [
        {"candidate": row, "conflicts": _profile_conflicts(row, values)} for row in candidates
    ]
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


def decode_vehicle_batch(
    items: list[dict[str, Any] | str], decoder: str, deadline_seconds: float = 30
) -> dict[str, Any]:
    from . import automotive_offline

    decoders: dict[str, Callable[..., dict[str, Any]]] = {
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
    deadline = finite_number(deadline_seconds, minimum=0.1)
    if (
        not isinstance(items, list)
        or len(items) > MAX_ROWS
        or decoder not in decoders
        or deadline is None
        or deadline > 120
    ):
        return invalid("decode_vehicle_batch", "items_or_decoder_or_deadline")
    started = time.monotonic()
    rows: list[dict[str, Any]] = []
    network_calls = 0
    attempts: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        remaining = deadline - (time.monotonic() - started)
        identifier = item.get("identifier") if isinstance(item, dict) else item
        if remaining <= 0:
            row = result(decoder, "deadline_exceeded", {}, warnings=["batch_deadline_exceeded"])
        elif not isinstance(identifier, str):
            row = invalid(decoder, "identifier")
        else:
            options: dict[str, Any] = (
                {"timeout_seconds": min(8, remaining)}
                if decoder in {"decode_vin_vpic", "decode_wmi_vpic", "corgi_decode"}
                else {}
            )
            try:
                row = decoders[decoder](identifier, **options)
            except (OSError, ValueError, TypeError) as exc:
                network = decoder in {"decode_vin_vpic", "decode_wmi_vpic"}
                row = result(
                    decoder,
                    "provider_error",
                    {},
                    warnings=[type(exc).__name__],
                    network_calls=int(network),
                    attempts=[{"method": decoder, "outcome": "unknown"}] if network else [],
                )
        rows.append({"item_index": index, "result": row})
        network_calls += row["execution"]["network_calls"]
        attempts.extend(row["execution"]["attempts"])
    return result(
        "decode_vehicle_batch",
        "partial" if any(row["result"]["outcome"] != "success" for row in rows) else "success",
        {"decoder": decoder, "items": rows, "deadline_seconds": deadline},
        network_calls=network_calls,
        attempts=attempts,
        warnings=[
            "Already-issued synchronous transport is bounded by its timeout; disconnect does not prove cancellation upstream."
        ],
    )


async def decode_vehicle_batch_async(
    items: list[dict[str, Any] | str], decoder: str, deadline_seconds: float = 30
) -> dict[str, Any]:
    """Stop issuing rows when the request is cancelled; an issued read retains its transport timeout."""
    import asyncio

    deadline = finite_number(deadline_seconds, minimum=0.1)
    if not isinstance(items, list) or len(items) > MAX_ROWS or deadline is None or deadline > 120:
        return invalid("decode_vehicle_batch", "items_or_deadline")
    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    calls = 0
    attempts: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        remaining = deadline - (time.monotonic() - started)
        if remaining <= 0:
            row = result("decode_vehicle_batch", "deadline_exceeded", {})
        else:
            # One row per await means cancellation prevents all subsequent rows.
            one = await asyncio.to_thread(decode_vehicle_batch, [item], decoder, remaining)
            if one["outcome"] == "invalid_input":
                return one
            row = one["data"]["items"][0]["result"]
        rows.append({"item_index": index, "result": row})
        calls += row["execution"]["network_calls"]
        attempts.extend(row["execution"]["attempts"])
    return result(
        "decode_vehicle_batch",
        "partial" if any(row["result"]["outcome"] != "success" for row in rows) else "success",
        {"decoder": decoder, "items": rows, "deadline_seconds": deadline},
        network_calls=calls,
        attempts=attempts,
    )
