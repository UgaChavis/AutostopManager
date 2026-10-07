"""Independent identity reads and pure reconciliation; no implicit fallback."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from collections.abc import Callable
from typing import Any

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
from .vehicle_identity import identity_values_agree
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
    data = {
        "vehicle_profile": profile,
        "input_binding": binding(identifier, "vin") if not wmi else None,
        "wmi": identifier if wmi else None,
        "identifier_binding": bound,
        "diagnostics": response.get("diagnostics") or response.get("provider_errors") or [],
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


def reconcile_vehicle_identity(
    identifier: str,
    results: list[dict[str, Any]],
    context: dict[str, Any] | None = None,
    identifier_type: str = "auto",
) -> dict[str, Any]:
    prepared = validate_identity_input(identifier, context, identifier_type=identifier_type)
    if (
        not prepared["ok"]
        or not isinstance(results, list)
        or len(results) > MAX_ROWS
        or any(not isinstance(row, dict) for row in results)
    ):
        return invalid("reconcile_vehicle_identity", "identifier_or_results")
    expected = binding(identifier, identifier_type)
    values: dict[str, list[dict[str, Any]]] = {field: [] for field in FIELDS}
    warnings: list[str] = []
    conflicts: list[dict[str, Any]] = []
    variants: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    year_candidates: list[Any] = []
    for index, row in enumerate(results):
        data: dict[str, Any] = row["data"] if isinstance(row.get("data"), dict) else row
        lineage = _source_lineage(row, index)
        variants.append(
            {"profile": _profile(row), "source_lineage": lineage, "result_index": index, "outcome": row.get("outcome")}
        )
        for key in ("variants", "family_candidates"):
            supplied_variants = data.get(key) or []
            if not isinstance(supplied_variants, list) or any(not isinstance(item, dict) for item in supplied_variants):
                return invalid("reconcile_vehicle_identity", "results." + key)
            variants.extend(
                {**item, "result_index": index, "source_lineage": lineage}
                for item in supplied_variants
                if item not in variants
            )
        supplied_years = data.get("model_year_candidates") or []
        if not isinstance(supplied_years, list):
            return invalid("reconcile_vehicle_identity", "results.model_year_candidates")
        year_candidates.extend(item for item in supplied_years if item not in year_candidates)
        upstream_errors = identity_errors(row, data)
        if upstream_errors:
            conflicts.extend({**item, "result_index": index} for item in upstream_errors)
            warnings.append(f"result_{index}_not_usable_as_vehicle_facts")
            continue
        wmi = data.get("wmi")
        if data.get("input_binding") != expected and wmi != prepared["identifier"][:3]:
            conflicts.append({"field": "identifier", "code": "result_binding_mismatch", "result_index": index})
            continue
        profile = _profile(row)
        supplied_evidence = row.get("evidence") or row.get("field_evidence") or []
        if not isinstance(supplied_evidence, list) or any(not isinstance(item, dict) for item in supplied_evidence):
            return invalid("reconcile_vehicle_identity", "results.evidence")
        for field in FIELDS:
            if profile.get(field) not in (None, "") and (
                not wmi or field in {"make", "manufacturer", "country", "vehicle_type"}
            ):
                for origin in field_origins(row, field, profile[field]):
                    values[field].append(
                        {
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
            statuses[field] = "observed"
        provenance[field] = observations
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
        "diagnostics": {},
        "confidence": 0.0,
    }
    data["parts_lookup_readiness"] = build_parts_lookup_readiness(data)
    return result(
        "reconcile_vehicle_identity",
        "partial" if conflicts or missing else "success",
        data,
        conflicts=conflicts,
        missing_fields=missing,
        warnings=warnings,
        evidence=evidence,
    )


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
