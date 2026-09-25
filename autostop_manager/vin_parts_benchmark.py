from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote_plus

from .catalog_adapters import build_oem_parts_provider_plan, catalog_provider_status
from .catalog_clients import partsapi_catalog_lookup
from .parts_intent import normalize_part_intent
from .vehicle_identity import decode_vehicle_identities, identity_values_agree
from .vin_oem_resolver import _redact_sensitive_output, resolve_vin_oem_parts
from .vin_lookup import classify_identifier, normalize_vin


def _compact(value: Any) -> str:
    return str(value or "").strip()


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _merged_item_context(item: dict[str, Any]) -> dict[str, Any]:
    profile: dict[str, Any] = {}
    for key in ["vehicle_profile", "vehicle_profile_compact", "crm_vehicle_profile", "crm_context"]:
        profile.update(_as_mapping(item.get(key)))
    merged = {**profile, **item}
    aliases = {
        "make_display": "make",
        "model_display": "model",
        "production_year": "model_year",
        "engine_model": "engine",
        "gearbox_model": "transmission",
        "chassis_number": "frame",
        "body_number": "frame",
    }
    for source, target in aliases.items():
        if merged.get(source) not in (None, "") and merged.get(target) in (None, ""):
            merged[target] = merged[source]
    return merged


def _item_identifier(item: dict[str, Any]) -> str:
    context = _merged_item_context(item)
    return _compact(
        item.get("identifier")
        or item.get("vin")
        or item.get("frame")
        or item.get("body_number")
        or context.get("vin")
        or context.get("frame")
        or context.get("body_number")
        or context.get("chassis_number")
    )


def _redact_identifier(identifier: str) -> dict[str, Any]:
    compact = "".join(str(identifier or "").split()).upper()
    if len(compact) <= 6:
        display = compact[:2] + "***" if compact else ""
    else:
        display = f"{compact[:3]}***{compact[-3:]}"
    return {
        "display": display,
        "length": len(compact),
        "prefix": compact[:3] if len(compact) >= 3 else compact,
    }


def _identifier_variants(identifier: str) -> set[str]:
    variants = {_compact(identifier), normalize_vin(identifier)}
    compact = normalize_vin(identifier)
    match = re.match(r"^([A-Z]{1,4}\d{1,3}[A-Z]?)(\d{5,7})$", compact)
    if match:
        variants.add(f"{match.group(1)}-{match.group(2)}")
    encoded = set()
    for value in variants:
        if value:
            encoded.add(quote_plus(value))
    return {value for value in variants | encoded if value}


def _redact_text(value: Any, identifier: str) -> str:
    text = str(value or "")
    for variant in sorted(_identifier_variants(identifier), key=len, reverse=True):
        text = text.replace(variant, "[REDACTED_IDENTIFIER]")
    return text


def _contains_identifier(value: Any, identifier: str) -> bool:
    text = str(value or "")
    return any(variant in text for variant in _identifier_variants(identifier))


def _first_nonempty(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _partsapi_vehicle_profile(call: dict[str, Any]) -> dict[str, Any]:
    profiles = [profile for profile in call.get("vehicle_profiles") or [] if isinstance(profile, dict)]
    return profiles[0] if len(profiles) == 1 else {}


def _assess_partsapi_identity_agreement(identity: dict[str, Any], call: dict[str, Any]) -> dict[str, Any]:
    if call.get("dry_run"):
        return {"status": "not_checked", "source": "PartsAPI VINdecode", "matched_fields": [], "conflicting_fields": []}
    if not call.get("ok"):
        return {
            "status": "provider_failed",
            "source": "PartsAPI VINdecode",
            "matched_fields": [],
            "conflicting_fields": [],
            "error": call.get("error"),
            "missing_env_names": call.get("missing_env_names")
            or (call.get("request_plan") or {}).get("missing_env_names")
            or [],
        }

    local_profile = identity.get("vehicle_profile") or {}
    profiles = [profile for profile in call.get("vehicle_profiles") or [] if isinstance(profile, dict)]
    if len(profiles) > 1:
        return {
            "status": "ambiguous_vehicle_modification",
            "source": "PartsAPI VINdecode",
            "matched_fields": [],
            "conflicting_fields": [],
        }
    vehicle_profile = _partsapi_vehicle_profile(call)
    if not vehicle_profile:
        return {
            "status": "no_profile",
            "source": "PartsAPI VINdecode",
            "matched_fields": [],
            "conflicting_fields": [],
        }
    if vehicle_profile.get("identifier_matches_request") is False:
        return {
            "status": "identifier_mismatch",
            "source": "PartsAPI VINdecode",
            "matched_fields": [],
            "conflicting_fields": [],
        }

    field_pairs = {
        "make": (local_profile.get("make"), vehicle_profile.get("make")),
        "model": (
            _first_nonempty(local_profile.get("model"), local_profile.get("model_family")),
            _first_nonempty(vehicle_profile.get("model"), vehicle_profile.get("model_family")),
        ),
        "model_year": (local_profile.get("model_year"), vehicle_profile.get("model_year")),
        "transmission": (local_profile.get("transmission"), vehicle_profile.get("transmission")),
    }
    matched_fields: list[str] = []
    conflicting_fields: list[dict[str, Any]] = []
    compared_fields: list[str] = []
    for field, (left, right) in field_pairs.items():
        if left in (None, "") or right in (None, ""):
            continue
        compared_fields.append(field)
        if identity_values_agree(field, left, right):
            matched_fields.append(field)
        else:
            conflicting_fields.append({"field": field, "identity_value": left, "partsapi_value": right})

    if conflicting_fields:
        status = "conflict"
    elif {"make", "model"}.issubset(matched_fields):
        status = "matched"
    elif matched_fields:
        status = "partial_match"
    else:
        status = "profile_present_uncompared"

    return {
        "status": status,
        "source": "PartsAPI VINdecode",
        "matched_fields": matched_fields,
        "conflicting_fields": conflicting_fields,
        "compared_fields": sorted(set(compared_fields)),
        "partsapi_profile": {
            key: vehicle_profile.get(key)
            for key in ("make", "model", "model_family", "model_year", "transmission", "tecdoc_car_id", "vehicle_type")
            if vehicle_profile.get(key) not in (None, "")
        },
    }


def _identity_with_partsapi_agreement(identity: dict[str, Any], call: dict[str, Any]) -> dict[str, Any]:
    updated = {**identity}
    readiness = dict(identity.get("parts_lookup_readiness") or {})
    agreement = _assess_partsapi_identity_agreement(identity, call)
    high_conflict = any(item.get("severity") == "high" for item in identity.get("conflicts", []))
    confidence_label = str(identity.get("confidence_label") or "")
    can_read_candidates = (
        agreement["status"] == "matched" and confidence_label in {"medium", "high"} and not high_conflict
    )
    car_id = str(_partsapi_vehicle_profile(call).get("tecdoc_car_id") or "").strip()
    if not (car_id.isascii() and car_id.isdigit() and int(car_id) > 0):
        can_read_candidates = False

    blocking_reasons = list(readiness.get("blocking_reasons") or [])
    if can_read_candidates:
        blocking_reasons = [reason for reason in blocking_reasons if reason != "identity_confidence_below_high"]
    if agreement["status"] in {"conflict", "identifier_mismatch", "ambiguous_vehicle_modification"}:
        reason = f"partsapi_identity_{agreement['status']}"
        if reason not in blocking_reasons:
            blocking_reasons.append(reason)
    if agreement["status"] in {"partial_match", "profile_present_uncompared"}:
        reason = "partsapi_identity_insufficient_agreement"
        if reason not in blocking_reasons:
            blocking_reasons.append(reason)
    if not can_read_candidates and agreement["status"] in {
        "provider_failed",
        "no_profile",
        "profile_present_uncompared",
    }:
        reason = f"partsapi_identity_{agreement['status']}"
        if reason not in blocking_reasons:
            blocking_reasons.append(reason)
    if not car_id and call.get("ok") and not call.get("dry_run"):
        reason = "partsapi_identity_missing_tecdoc_car_id"
        if reason not in blocking_reasons:
            blocking_reasons.append(reason)

    readiness.update(
        {
            "ready_for_tecdoc_candidate_lookup": can_read_candidates,
            "ready_for_crm_writeback": False,
            "cross_source_agreement": agreement,
            "blocking_reasons": blocking_reasons,
            "reason": (
                "PartsAPI VINdecode agrees with decoded identity; read-only TecDoc research is allowed, while fitment and OEM still need manual confirmation."
                if can_read_candidates
                else readiness.get("reason")
            ),
        }
    )
    updated["parts_lookup_readiness"] = readiness
    evidence_sources = list(updated.get("evidence_sources") or [])
    evidence_sources.append(
        {
            "source": "PartsAPI VINdecode",
            "status": agreement["status"],
            "matched_fields": agreement.get("matched_fields", []),
        }
    )
    updated["evidence_sources"] = evidence_sources
    return updated


def _safe_public_queries(rows: list[dict[str, Any]], identifier: str) -> list[dict[str, Any]]:
    safe_rows = []
    for row in rows:
        raw_hit = _contains_identifier(row.get("query"), identifier) or _contains_identifier(row.get("url"), identifier)
        safe_rows.append(
            {
                "source_id": row.get("source_id"),
                "role": row.get("role"),
                "query": _redact_text(row.get("query"), identifier),
                "url": _redact_text(row.get("url"), identifier),
                "needs": row.get("needs"),
                "raw_identifier_in_query": raw_hit,
            }
        )
    return safe_rows


def _identity_digest(identity: dict[str, Any], identifier: str) -> dict[str, Any]:
    diagnostics = identity.get("diagnostics") or {}
    profile = identity.get("vehicle_profile") or {}
    readiness = identity.get("parts_lookup_readiness") or {}
    return {
        "confidence": identity.get("confidence"),
        "confidence_label": identity.get("confidence_label"),
        "ready_for_oem_lookup": readiness.get("ready_for_oem_lookup"),
        "ready_for_oem_candidate_lookup": readiness.get(
            "ready_for_oem_candidate_lookup", readiness.get("ready_for_oem_lookup")
        ),
        "ready_for_tecdoc_candidate_lookup": readiness.get("ready_for_tecdoc_candidate_lookup", False),
        "ready_for_crm_writeback": readiness.get("ready_for_crm_writeback", readiness.get("ready_for_oem_lookup")),
        "cross_source_agreement": readiness.get("cross_source_agreement") or {},
        "blocking_reasons": readiness.get("blocking_reasons") or [],
        "vehicle_profile": {
            key: profile.get(key)
            for key in [
                "make",
                "model",
                "model_family",
                "platform",
                "model_year",
                "engine",
                "transmission",
                "drivetrain",
                "market",
                "plant_country",
            ]
            if profile.get(key) not in (None, "")
        },
        "diagnostics": {
            "model_year_status": (diagnostics.get("model_year") or {}).get("status"),
            "model_year_candidates": (diagnostics.get("model_year") or {}).get("candidate_years"),
            "check_digit_status": (diagnostics.get("check_digit") or {}).get("status"),
            "has_frame_query_hint": bool((diagnostics.get("frame_query_hint") or "").strip()),
        },
        "evidence_sources": [
            {
                "source": source.get("source"),
                "status": source.get("status"),
                "mode": source.get("mode"),
                "rule_id": source.get("rule_id"),
            }
            for source in identity.get("evidence_sources", [])
        ],
        "conflict_count": len(identity.get("conflicts", [])),
        "high_severity_conflict_count": sum(
            1 for item in identity.get("conflicts", []) if item.get("severity") == "high"
        ),
        "warning_count": len(identity.get("warnings", [])),
        "warnings": [_redact_text(value, identifier) for value in identity.get("warnings", [])[:5]],
        "required_next_sources": identity.get("required_next_sources", []),
    }


def _partsapi_lookup_calls(
    identifier: str,
    part_profile: dict[str, Any],
    *,
    identity_call: dict[str, Any] | None = None,
    vehicle_type: str | None = None,
    dry_run: bool = True,
) -> list[dict[str, Any]]:
    if classify_identifier(identifier).kind != "vin":
        return []
    calls = (
        [identity_call]
        if identity_call is not None
        else [partsapi_catalog_lookup(operation="vin_decode", identifier=identifier, dry_run=dry_run)]
    )
    profiles = [profile for profile in (identity_call or {}).get("vehicle_profiles") or [] if isinstance(profile, dict)]
    if (
        identity_call
        and identity_call.get("ok")
        and len(profiles) == 1
        and profiles[0].get("identifier_matches_request") is not False
    ):
        car_id = str(profiles[0].get("tecdoc_car_id") or "").strip()
    else:
        car_id = ""
    provider_vehicle_type = str(profiles[0].get("vehicle_type") or "").strip() if profiles else ""
    canonical_types = {"pc": "PC", "cv": "CV", "motorcycle": "Motorcycle"}
    provider_vehicle_type = canonical_types.get(provider_vehicle_type.casefold(), "")
    explicit_vehicle_type = canonical_types.get(str(vehicle_type or "").strip().casefold(), "")
    vehicle_type = (
        explicit_vehicle_type or provider_vehicle_type
        if not (provider_vehicle_type and explicit_vehicle_type and provider_vehicle_type != explicit_vehicle_type)
        else ""
    )
    if (
        car_id.isascii()
        and car_id.isdigit()
        and int(car_id) > 0
        and vehicle_type
        and not part_profile.get("clarification_required")
    ):
        calls.append(
            partsapi_catalog_lookup(
                operation="search_tree",
                type_id=car_id,
                vehicle_type=vehicle_type,
                dry_run=True,
            )
        )
    return [_adapter_digest(call) for call in calls]


def _adapter_digest(call: dict[str, Any]) -> dict[str, Any]:
    request_plan = call.get("request_plan") or {}
    params = request_plan.get("params") or {}
    return {
        "provider": call.get("provider"),
        "operation": call.get("operation"),
        "ok": bool(call.get("ok")),
        "dry_run": bool(call.get("dry_run")),
        "docs_url": call.get("docs_url"),
        "partsapi_method": call.get("partsapi_method"),
        "configured": bool(request_plan.get("configured")),
        "missing_env_names": call.get("missing_env_names") or request_plan.get("missing_env_names") or [],
        "missing_params": call.get("missing_params") or [],
        "request_param_names": sorted(params),
        "error": call.get("error"),
        "privacy": {"raw_identifier_redacted_from_benchmark": True, "secret_exposed": False},
    }


def _missing_env_from_plan(plan: dict[str, Any]) -> list[str]:
    names = set()
    for blocker in plan.get("blockers", []):
        names.update(_missing_env_names_from_blocker(blocker))
    return sorted(names)


def _missing_env_names_from_blocker(blocker: dict[str, Any]) -> list[str]:
    names: set[str] = set()
    for field in ("missing_env_names", "missing_env"):
        value = blocker.get(field)
        if isinstance(value, (list, tuple, set)):
            names.update(str(name).strip() for name in value if str(name).strip())
        elif value not in (None, ""):
            names.add(str(value).strip())
    return sorted(names)


def _blockers_by_stage(items: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        for blocker in item.get("blockers", []):
            stage = str(blocker.get("stage") or "unknown")
            row = grouped.setdefault(stage, {"count": 0, "missing_env_names": set(), "reasons": set()})
            row["count"] += 1
            row["missing_env_names"].update(_missing_env_names_from_blocker(blocker))
            if blocker.get("reason"):
                row["reasons"].add(blocker["reason"])
    return {
        stage: {
            "count": row["count"],
            "missing_env_names": sorted(row["missing_env_names"]),
            "reasons": sorted(row["reasons"]),
        }
        for stage, row in grouped.items()
    }


def _benchmark_status(summary: dict[str, Any]) -> str:
    if summary["count"] == 0:
        return "no_items"
    if summary["full_auto_lookup_count"] == summary["count"]:
        return "live_full_auto_lookup_ready"
    if (
        summary.get("ready_for_oem_candidate_lookup_count") == summary["count"]
        and summary["part_intent_actionable_count"] == summary["count"]
    ):
        return "identity_ready_but_blocked_by_live_catalog_or_supplier_credentials"
    return "partial_identity_or_part_intent_coverage"


def benchmark_vin_parts_lookup(
    items: list[dict[str, Any]],
    *,
    requested_part: str,
    city: str = "Красноярск",
    live_vpic: bool = True,
    use_vpic_batch: bool = True,
    include_partsapi_dry_run: bool = True,
    live_partsapi_identity: bool = False,
    live_partsapi_oem: bool = False,
    resolve_oem: bool = False,
    max_live_calls: int = 3,
    max_candidates: int = 3,
    partsapi_category_index: str | None = None,
    partsapi_timeout: float = 20.0,
) -> dict[str, Any]:
    """Benchmark VIN/frame identity and guarded TecDoc article research.

    The report is read-only and redacts raw identifiers from benchmark output.
    Live vPIC may be used for public VIN decode; PartsAPI calls require the
    explicit live flags. TecDoc article rows are not confirmed OEM fitment.
    """

    identity_batch = decode_vehicle_identities(items, live_vpic=live_vpic, use_vpic_batch=use_vpic_batch)
    benchmark_items: list[dict[str, Any]] = []
    missing_env_names: set[str] = set()
    partsapi_live_calls_used = 0

    for index, (item, identity) in enumerate(zip(items, identity_batch.get("results", []), strict=False), start=1):
        identifier = _item_identifier(item)
        classification = classify_identifier(identifier)
        item_requested_part = _compact(item.get("requested_part")) or requested_part
        context = _merged_item_context(item)
        part_profile = normalize_part_intent(
            item_requested_part,
            axle=_compact(item.get("axle") or context.get("axle")),
            side=_compact(item.get("side") or context.get("side")),
            position=_compact(item.get("position") or context.get("position")),
            inner_outer=_compact(item.get("inner_outer") or context.get("inner_outer")),
        )
        partsapi_identity_call = None
        if live_partsapi_identity and not resolve_oem and classification.kind == "vin":
            call_is_live = partsapi_live_calls_used < max(0, int(max_live_calls))
            partsapi_identity_call = partsapi_catalog_lookup(
                operation="vin_decode",
                identifier=identifier,
                timeout=partsapi_timeout,
                max_attempts=1,
                dry_run=not call_is_live,
            )
            if call_is_live:
                partsapi_live_calls_used += max(0, int(partsapi_identity_call.get("attempt_count") or 0))
                identity = _identity_with_partsapi_agreement(identity, partsapi_identity_call)
        oem_resolution = None
        if resolve_oem:
            oem_resolution = resolve_vin_oem_parts(
                identifier=identifier,
                requested_part=item_requested_part,
                make=_compact(item.get("make") or context.get("make")),
                model=_compact(item.get("model") or context.get("model")),
                model_year=item.get("model_year") or context.get("model_year"),
                engine=_compact(item.get("engine") or context.get("engine")),
                transmission=_compact(item.get("transmission") or context.get("transmission")),
                market=_compact(item.get("market") or context.get("market")),
                drivetrain=_compact(item.get("drivetrain") or context.get("drivetrain")),
                axle=_compact(item.get("axle") or context.get("axle")),
                side=_compact(item.get("side") or context.get("side")),
                position=_compact(item.get("position") or context.get("position")),
                inner_outer=_compact(item.get("inner_outer") or context.get("inner_outer")),
                live_vpic=live_vpic,
                live_partsapi_identity=live_partsapi_identity,
                live_partsapi_oem=live_partsapi_oem,
                vehicle_type=_compact(
                    item.get("vehicle_type")
                    or item.get("car_type")
                    or context.get("vehicle_type")
                    or context.get("car_type")
                )
                or None,
                max_live_calls=max(0, int(max_live_calls) - partsapi_live_calls_used),
                max_candidates=max_candidates,
                timeout=partsapi_timeout,
                partsapi_category_index=partsapi_category_index,
            )
            partsapi_live_calls_used += max(0, int(oem_resolution.get("live_call_count") or 0))
        provider_plan = build_oem_parts_provider_plan(
            identifier=identifier,
            requested_part=item_requested_part,
            vehicle_identity=identity,
            city=city,
        )
        public_queries = _safe_public_queries(provider_plan.get("manual_public_search_queries", []), identifier)
        partsapi_calls = (
            _partsapi_lookup_calls(
                identifier,
                part_profile,
                identity_call=partsapi_identity_call,
                vehicle_type=_compact(
                    item.get("vehicle_type")
                    or item.get("car_type")
                    or context.get("vehicle_type")
                    or context.get("car_type")
                )
                or None,
                dry_run=True,
            )
            if include_partsapi_dry_run
            else []
        )

        missing_env_names.update(_missing_env_from_plan(provider_plan))
        for call in partsapi_calls:
            missing_env_names.update(call.get("missing_env_names") or [])
        if oem_resolution:
            for call in oem_resolution.get("calls", []):
                missing_env_names.update(call.get("missing_env_names") or [])

        benchmark_items.append(
            {
                "index": index,
                "identifier": {
                    "redacted": _redact_identifier(identifier),
                    "kind": classification.kind,
                    "market_hint": classification.market_hint,
                    "confidence": classification.confidence,
                    "notes": classification.notes,
                    "raw_identifier_is_sensitive": True,
                },
                "identity": oem_resolution["identity"] if oem_resolution else _identity_digest(identity, identifier),
                "requested_part": {
                    "raw": item_requested_part,
                    "recognized": bool(part_profile.get("recognized")),
                    "intent_id": part_profile.get("intent_id"),
                    "confidence": part_profile.get("confidence"),
                    "canonical_name_ru": part_profile.get("canonical_name_ru"),
                    "catalog_search_terms": (part_profile.get("catalog_search_terms") or [])[:8],
                    "critical_vehicle_fields": part_profile.get("critical_vehicle_fields", []),
                    "quantity_basis": part_profile.get("quantity_basis"),
                    "clarification_required": bool(part_profile.get("clarification_required")),
                    "clarification_fields": part_profile.get("clarification_fields", []),
                },
                "live_capability": provider_plan.get("live_capability", {}),
                "blockers": provider_plan.get("blockers", []),
                "prepared_calls": {
                    "partsapi": partsapi_calls,
                },
                "oem_resolution": oem_resolution,
                "manual_public_search": {
                    "count": len(public_queries),
                    "queries": public_queries,
                    "raw_identifier_in_any_query": any(row["raw_identifier_in_query"] for row in public_queries),
                },
            }
        )

    count = len(benchmark_items)
    summary = {
        "count": count,
        "high_identity_count": sum(1 for item in benchmark_items if item["identity"]["confidence_label"] == "high"),
        "medium_identity_count": sum(1 for item in benchmark_items if item["identity"]["confidence_label"] == "medium"),
        "low_identity_count": sum(1 for item in benchmark_items if item["identity"]["confidence_label"] == "low"),
        "ready_for_oem_lookup_count": sum(1 for item in benchmark_items if item["identity"]["ready_for_oem_lookup"]),
        "ready_for_oem_candidate_lookup_count": sum(
            1 for item in benchmark_items if item["identity"]["ready_for_oem_candidate_lookup"]
        ),
        "ready_for_tecdoc_candidate_lookup_count": sum(
            1 for item in benchmark_items if item["identity"].get("ready_for_tecdoc_candidate_lookup")
        ),
        "ready_for_crm_writeback_count": sum(
            1 for item in benchmark_items if item["identity"]["ready_for_crm_writeback"]
        ),
        "part_intent_recognized_count": sum(1 for item in benchmark_items if item["requested_part"]["recognized"]),
        "part_intent_actionable_count": sum(
            1
            for item in benchmark_items
            if item["requested_part"]["recognized"] and not item["requested_part"]["clarification_required"]
        ),
        "part_intent_clarification_required_count": sum(
            1 for item in benchmark_items if item["requested_part"]["clarification_required"]
        ),
        "manual_public_search_count": sum(item["manual_public_search"]["count"] for item in benchmark_items),
        "manual_public_queries_with_raw_identifier_count": sum(
            1
            for item in benchmark_items
            for row in item["manual_public_search"]["queries"]
            if row["raw_identifier_in_query"]
        ),
        "live_oem_ready_count": sum(
            1 for item in benchmark_items if item["live_capability"].get("live_oem_catalog_available")
        ),
        "live_price_ready_count": sum(
            1 for item in benchmark_items if item["live_capability"].get("live_procurement_available")
        ),
        "full_auto_lookup_count": sum(
            1 for item in benchmark_items if item["live_capability"].get("can_complete_full_auto_lookup_now")
        ),
        "partsapi_request_shape_count": sum(len(item["prepared_calls"]["partsapi"]) for item in benchmark_items),
        "oem_resolution_count": sum(1 for item in benchmark_items if item.get("oem_resolution")),
        "oem_candidate_count": sum(
            int((item.get("oem_resolution") or {}).get("candidate_count") or 0) for item in benchmark_items
        ),
        "tecdoc_article_candidate_count": sum(
            int((item.get("oem_resolution") or {}).get("article_candidate_count") or 0) for item in benchmark_items
        ),
        "partsapi_live_call_count": partsapi_live_calls_used,
        "partsapi_max_live_calls": max_live_calls,
        "missing_env_names": sorted(missing_env_names),
    }
    summary["benchmark_status"] = _benchmark_status(summary)

    result = {
        "ok": True,
        "mode": "read_only_vin_parts_benchmark",
        "requested_part": requested_part,
        "city": city,
        "summary": summary,
        "identity_batch": {
            "count": identity_batch.get("count"),
            "high_confidence_count": identity_batch.get("high_confidence_count"),
            "medium_confidence_count": identity_batch.get("medium_confidence_count"),
            "low_confidence_count": identity_batch.get("low_confidence_count"),
            "identity_coverage": identity_batch.get("identity_coverage"),
            "ready_for_oem_candidate_lookup_count": sum(
                1 for item in benchmark_items if item["identity"]["ready_for_oem_candidate_lookup"]
            ),
            "ready_for_tecdoc_candidate_lookup_count": sum(
                1 for item in benchmark_items if item["identity"].get("ready_for_tecdoc_candidate_lookup")
            ),
            "ready_for_crm_writeback_count": sum(
                1 for item in benchmark_items if item["identity"]["ready_for_crm_writeback"]
            ),
            "vpic_batch": identity_batch.get("vpic_batch"),
        },
        "provider_status_summary": {
            key: value
            for key, value in catalog_provider_status().items()
            if key in {"configured_count", "live_callable_count", "missing_provider_ids"}
        },
        "blockers_by_stage": _blockers_by_stage(benchmark_items),
        "next_requirements": [
            {
                "priority": 1,
                "need": "VIN-specific OEM catalog confirmation for production date, options, OEM groups and exact applicability.",
                "candidate_sources": ["PartSouq", "Amayama", "manufacturer EPC"],
                "env_names": [],
            },
            {
                "priority": 2,
                "need": "Catalog cross/applicability API for OEM -> analog/cross checks.",
                "candidate_sources": ["PARTSAPI.RU", "MANN", "DENSO"],
                "env_names": ["PARTSAPI_CROSSES_KEY", "PARTSAPI_CROSSES_WITH_BRAND_KEY", "PARTSAPI_BASE_URL"],
            },
            {
                "priority": 3,
                "need": "Verify current supplier price, availability and delivery before writing prices to CRM.",
                "candidate_sources": ["Exist", "Emex public search", "EuroAuto public catalog"],
                "env_names": [],
            },
        ],
        "privacy": {
            "raw_identifier_is_sensitive": True,
            "raw_identifier_redacted_from_output": True,
            "do_not_store_raw_customer_vin_frame_in_git_or_durable_memory": True,
        },
        "items": benchmark_items,
    }
    for item in items:
        result = _redact_sensitive_output(result, _item_identifier(item))
    return result
