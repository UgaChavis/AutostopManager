from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

from .catalog_clients import partsapi_catalog_lookup, resolve_partsapi_category
from .parts_intent import normalize_part_intent
from .vehicle_identity import decode_vehicle_identity, identity_values_agree
from .vin_lookup import classify_identifier


def _compact(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _normalize_compare_value(value: Any) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", str(value or "").casefold())


def _vin_fitment_state(value: Any) -> bool | None:
    """Accept only an explicit affirmative/negative VIN-fitment assertion."""

    if value is True or value == 1:
        return True
    if value is False or value == 0:
        return False
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes"}:
            return True
        if normalized in {"0", "false", "no"}:
            return False
    return None


def _axle_hints(value: Any) -> set[str]:
    text = re.sub(r"[_/\\-]+", " ", str(value or "").casefold())
    hints: set[str] = set()
    if re.search(r"\b(?:front|передн\w*)\b", text):
        hints.add("front")
    if re.search(r"\b(?:rear|задн\w*)\b", text):
        hints.add("rear")
    return hints


def _side_hints(value: Any) -> set[str]:
    text = re.sub(r"[_/\\-]+", " ", str(value or "").casefold())
    hints: set[str] = set()
    if re.search(r"\b(?:left|lh|лев\w*)\b", text):
        hints.add("left")
    if re.search(r"\b(?:right|rh|прав\w*)\b", text):
        hints.add("right")
    return hints


def _inner_outer_hints(value: Any) -> set[str]:
    text = re.sub(r"[_/\\-]+", " ", str(value or "").casefold())
    hints: set[str] = set()
    if re.search(r"\b(?:inner|internal|inboard|внутрен\w*)\b", text):
        hints.add("inner")
    if re.search(r"\b(?:outer|external|outboard|наружн\w*)\b", text):
        hints.add("outer")
    return hints


def _single_coordinate_hint(value: Any, parser: Callable[[Any], set[str]]) -> str | None:
    hints = parser(value)
    return next(iter(hints)) if len(hints) == 1 else None


def _requested_position_coordinates(part_profile: dict[str, Any]) -> dict[str, str]:
    context = part_profile.get("explicit_position_context")
    explicit = context if isinstance(context, dict) else {}
    intent_id = str(part_profile.get("intent_id") or "")
    raw = part_profile.get("raw")

    axle = _single_coordinate_hint(explicit.get("axle"), _axle_hints)
    if axle is None and intent_id == "front_brake_pads":
        axle = "front"
    elif axle is None and intent_id == "rear_brake_pads":
        axle = "rear"
    if axle is None:
        axle = _single_coordinate_hint(raw, _axle_hints)

    side = _single_coordinate_hint(explicit.get("side"), _side_hints)
    if side is None:
        side = _single_coordinate_hint(raw, _side_hints)

    inner_outer = _single_coordinate_hint(explicit.get("inner_outer"), _inner_outer_hints)
    if inner_outer is None and intent_id == "inner_cv_joint":
        inner_outer = "inner"
    elif inner_outer is None and intent_id == "outer_cv_joint":
        inner_outer = "outer"
    if inner_outer is None:
        inner_outer = _single_coordinate_hint(raw, _inner_outer_hints)

    return {
        key: value for key, value in (("axle", axle), ("side", side), ("inner_outer", inner_outer)) if value is not None
    }


def _coordinate_match(requested: str, hints: set[str]) -> str:
    if not hints:
        return "not_proved"
    if hints == {requested}:
        return "matched"
    if requested not in hints:
        return "conflict"
    return "ambiguous"


def _candidate_position_assessment(candidate: dict[str, Any], part_profile: dict[str, Any]) -> dict[str, Any]:
    """Compare independent requested and candidate position coordinates.

    Absence of any requested coordinate is deliberately not treated as a match.
    Requirement labels such as ``front_or_rear_required`` are schema metadata,
    not position evidence.
    """

    requested = _requested_position_coordinates(part_profile)
    if not requested:
        return {
            "requested_coordinates": {},
            "candidate_coordinate_hints": {},
            "coordinate_matches": {},
            "requested_axle": None,
            "candidate_axle_hints": [],
            "position_match": "not_required",
        }

    fitment = candidate.get("fitment_evidence") or {}
    candidate_values = [
        candidate.get("name"),
        candidate.get("description"),
        candidate.get("part_name"),
        candidate.get("position"),
        candidate.get("axle"),
        candidate.get("side"),
        candidate.get("inner_outer"),
        candidate.get("group"),
        candidate.get("category"),
    ]
    if isinstance(fitment, dict):
        candidate_values.extend(
            fitment.get(key)
            for key in (
                "group",
                "category_name",
                "shortname",
                "applicability",
                "position",
                "axle",
                "side",
                "inner_outer",
                "name",
                "description",
            )
        )

    parsers: dict[str, Callable[[Any], set[str]]] = {
        "axle": _axle_hints,
        "side": _side_hints,
        "inner_outer": _inner_outer_hints,
    }
    candidate_hints: dict[str, set[str]] = {coordinate: set() for coordinate in requested}
    for value in candidate_values:
        for coordinate, parser in parsers.items():
            if coordinate in candidate_hints:
                candidate_hints[coordinate].update(parser(value))

    matches = {
        coordinate: _coordinate_match(requested_value, candidate_hints[coordinate])
        for coordinate, requested_value in requested.items()
    }
    if "conflict" in matches.values():
        overall_match = "conflict"
    elif "ambiguous" in matches.values():
        overall_match = "ambiguous"
    elif "not_proved" in matches.values():
        overall_match = "not_proved"
    else:
        overall_match = "matched"

    return {
        "requested_coordinates": requested,
        "candidate_coordinate_hints": {coordinate: sorted(hints) for coordinate, hints in candidate_hints.items()},
        "coordinate_matches": matches,
        "requested_axle": requested.get("axle"),
        "candidate_axle_hints": sorted(candidate_hints.get("axle", set())),
        "position_match": overall_match,
    }


def _redact_identifier(identifier: str) -> dict[str, Any]:
    compact = "".join(str(identifier or "").split()).upper()
    if not compact:
        return {"display": "", "length": 0, "prefix": ""}
    if len(compact) <= 6:
        return {"display": f"{compact[:2]}***", "length": len(compact), "prefix": compact[:2]}
    return {"display": f"{compact[:3]}***{compact[-3:]}", "length": len(compact), "prefix": compact[:3]}


def _hash_candidate(*parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def _call_digest(call: dict[str, Any]) -> dict[str, Any]:
    request_plan = call.get("request_plan") or {}
    return {
        "provider": call.get("provider"),
        "operation": call.get("operation"),
        "partsapi_method": call.get("partsapi_method"),
        "ok": bool(call.get("ok")),
        "dry_run": bool(call.get("dry_run")),
        "empty_payload": bool(call.get("empty_payload")),
        "outcome": call.get("outcome"),
        "failure_class": call.get("failure_class"),
        "retryable": bool(call.get("retryable")),
        "requires_fallback": bool(call.get("requires_fallback")),
        "quota_cost_estimate": call.get("quota_cost_estimate"),
        "attempt_count": call.get("attempt_count"),
        "max_attempts": call.get("max_attempts"),
        "attempts": call.get("attempts", []),
        "missing_env_names": call.get("missing_env_names") or request_plan.get("missing_env_names") or [],
        "missing_params": call.get("missing_params") or [],
        "error": call.get("error"),
        "request_plan": request_plan,
        "vehicle_profile_count": len(call.get("vehicle_profiles") or []),
        "oem_candidate_count": len(call.get("oem_candidates") or []),
        "cross_candidate_count": len(call.get("cross_candidates") or []),
        "article_candidate_count": len(call.get("article_candidates") or []),
    }


def _identity_digest(identity: dict[str, Any]) -> dict[str, Any]:
    readiness = identity.get("parts_lookup_readiness") or {}
    profile = identity.get("vehicle_profile") or {}
    return {
        "confidence": identity.get("confidence"),
        "confidence_label": identity.get("confidence_label"),
        "ready_for_oem_lookup": readiness.get("ready_for_oem_lookup"),
        "ready_for_oem_candidate_lookup": readiness.get(
            "ready_for_oem_candidate_lookup", readiness.get("ready_for_oem_lookup")
        ),
        "ready_for_crm_writeback": readiness.get("ready_for_crm_writeback", False),
        "cross_source_agreement": readiness.get("cross_source_agreement") or {},
        "blocking_reasons": readiness.get("blocking_reasons") or [],
        "vehicle_profile": {
            key: profile.get(key)
            for key in (
                "make",
                "model",
                "model_family",
                "platform",
                "model_year",
                "engine",
                "transmission",
                "market",
                "production_date",
            )
            if profile.get(key) not in (None, "")
        },
        "conflict_count": len(identity.get("conflicts") or []),
        "high_severity_conflict_count": sum(
            1 for item in identity.get("conflicts") or [] if item.get("severity") == "high"
        ),
        "warning_count": len(identity.get("warnings") or []),
    }


def _partsapi_oe_profile(call: dict[str, Any]) -> dict[str, Any]:
    profiles = [profile for profile in call.get("vehicle_profiles") or [] if isinstance(profile, dict)]
    return profiles[0] if profiles else {}


def _assess_partsapi_oe_agreement(identity: dict[str, Any], call: dict[str, Any]) -> dict[str, Any]:
    if call.get("dry_run"):
        return {"status": "not_checked", "matched_fields": [], "conflicting_fields": []}
    if not call.get("ok"):
        return {"status": "provider_failed", "matched_fields": [], "conflicting_fields": [], "error": call.get("error")}
    oe_profile = _partsapi_oe_profile(call)
    if not oe_profile:
        return {"status": "no_profile", "matched_fields": [], "conflicting_fields": []}
    profile = identity.get("vehicle_profile") or {}
    compare_fields = {
        "make": (profile.get("make"), oe_profile.get("make")),
        "model": (
            profile.get("model") or profile.get("model_family"),
            oe_profile.get("model") or oe_profile.get("model_family"),
        ),
        "transmission": (profile.get("transmission"), oe_profile.get("transmission")),
    }
    matched: list[str] = []
    conflicts: list[dict[str, Any]] = []
    for field, (left, right) in compare_fields.items():
        left_norm = _normalize_compare_value(left)
        right_norm = _normalize_compare_value(right)
        if not left_norm or not right_norm:
            continue
        if identity_values_agree(field, left, right):
            matched.append(field)
        else:
            conflicts.append({"field": field, "identity": left, "partsapi_oe": right})
    if conflicts:
        return {
            "status": "conflict",
            "matched_fields": matched,
            "conflicting_fields": conflicts,
            "partsapi_profile": oe_profile,
        }
    if matched:
        return {
            "status": "matched",
            "matched_fields": matched,
            "conflicting_fields": [],
            "partsapi_profile": oe_profile,
        }
    return {
        "status": "profile_present_uncompared",
        "matched_fields": [],
        "conflicting_fields": [],
        "partsapi_profile": oe_profile,
    }


def _identity_with_partsapi_agreement(identity: dict[str, Any], call: dict[str, Any]) -> dict[str, Any]:
    updated = {**identity}
    readiness = dict(identity.get("parts_lookup_readiness") or {})
    agreement = _assess_partsapi_oe_agreement(identity, call)
    high_conflict = any(item.get("severity") == "high" for item in identity.get("conflicts") or [])
    confidence_label = str(identity.get("confidence_label") or "")
    can_read = bool(readiness.get("ready_for_oem_candidate_lookup"))
    if agreement["status"] == "matched" and confidence_label in {"medium", "high"} and not high_conflict:
        can_read = True
    if agreement["status"] == "conflict" or high_conflict:
        can_read = False
    blocking = list(readiness.get("blocking_reasons") or [])
    if can_read:
        blocking = [reason for reason in blocking if reason != "identity_confidence_below_high"]
    if agreement["status"] == "conflict" and "partsapi_oe_identity_conflict" not in blocking:
        blocking.append("partsapi_oe_identity_conflict")
    readiness.update(
        {
            "ready_for_oem_lookup": can_read,
            "ready_for_oem_candidate_lookup": can_read,
            "ready_for_crm_writeback": False,
            "cross_source_agreement": agreement,
            "blocking_reasons": blocking,
        }
    )
    updated["parts_lookup_readiness"] = readiness
    return updated


def _rank_oem_candidate(
    candidate: dict[str, Any],
    *,
    index: int,
    category_resolution: dict[str, Any],
    part_profile: dict[str, Any],
    identity: dict[str, Any],
) -> dict[str, Any]:
    source_operation = candidate.get("source_operation")
    fitment = candidate.get("fitment_evidence") or {}
    blockers: list[str] = []
    position_assessment = _candidate_position_assessment(candidate, part_profile)
    position_match = position_assessment["position_match"]
    if part_profile.get("clarification_required"):
        blockers.append("part_position_clarification_required")
    if category_resolution.get("category_unresolved"):
        blockers.append("partsapi_category_unresolved")
    if any(item.get("severity") == "high" for item in identity.get("conflicts") or []):
        blockers.append("high_severity_identity_conflict")
    if category_resolution.get("validation_required"):
        blockers.append("category_validation_required")
    if position_match == "conflict":
        blockers.append("candidate_position_conflicts_requested_position")
    elif position_match == "ambiguous":
        blockers.append("candidate_position_ambiguous")
    elif position_match == "not_proved":
        blockers.append("candidate_position_not_confirmed")

    source_is_vin_specific = (
        source_operation == "parts_by_vin" and _vin_fitment_state(fitment.get("is_fit_for_this_vin")) is True
    )
    requested_position_confirmed = source_is_vin_specific and position_match in {"matched", "not_required"}
    base_score = float(candidate.get("confidence") or 0.55)
    if requested_position_confirmed and not blockers:
        confidence_label = "high"
        score = max(base_score, 0.92)
    elif source_is_vin_specific and position_match not in {"conflict", "ambiguous", "not_proved"}:
        confidence_label = "medium"
        score = max(min(base_score, 0.86), 0.72)
    else:
        confidence_label = "low"
        score = min(base_score, 0.6)
    if position_match == "conflict":
        confidence_label = "low"
        score = min(score, 0.35)

    part_number = str(candidate.get("part_number") or "").strip()
    brand = candidate.get("brand")
    return {
        "candidate_id": f"oem-{index}-{_hash_candidate(candidate.get('provider'), brand, part_number, candidate.get('name'))}",
        "part_number": part_number,
        "brand": brand,
        "name": candidate.get("name"),
        "source": candidate.get("provider"),
        "source_operation": source_operation,
        "category_id": category_resolution.get("category")
        if category_resolution.get("category_kind") == "numeric_id"
        else None,
        "partsapi_category": category_resolution.get("category"),
        "partsapi_category_kind": category_resolution.get("category_kind"),
        "partsapi_category_mode": category_resolution.get("category_mode"),
        "fitment_scope": (
            "vin_specific"
            if requested_position_confirmed
            else ("vin_specific_position_unconfirmed" if source_is_vin_specific else "not_vin_specific")
        ),
        "position_match": position_match,
        "requested_axle": position_assessment["requested_axle"],
        "candidate_axle_hints": position_assessment["candidate_axle_hints"],
        "requested_position_coordinates": position_assessment["requested_coordinates"],
        "candidate_position_hints": position_assessment["candidate_coordinate_hints"],
        "position_coordinate_matches": position_assessment["coordinate_matches"],
        "quantity_basis": part_profile.get("quantity_basis"),
        "confidence_label": confidence_label,
        "confidence_score": round(score, 4),
        "blocking_reasons": blockers,
        "manual_review_required": True,
        "fitment_evidence": fitment,
    }


def _with_applicability(candidate: dict[str, Any], call: dict[str, Any]) -> dict[str, Any]:
    part_number = _normalize_compare_value(candidate.get("part_number"))
    matches = [
        item
        for item in call.get("oem_candidates") or []
        if isinstance(item, dict) and _normalize_compare_value(item.get("part_number")) == part_number
    ]
    if call.get("dry_run"):
        status, blocker = "not_checked", "applicability_not_checked"
    elif not call.get("ok"):
        status, blocker = "check_failed", "applicability_check_failed"
    elif not matches:
        status, blocker = "not_found", "applicability_not_confirmed"
    elif any(
        _vin_fitment_state((item.get("fitment_evidence") or {}).get("is_fit_for_this_vin")) is False for item in matches
    ):
        status, blocker = "rejected", "applicability_rejected"
    else:
        status, blocker = "catalog_evidence_found", None

    updated = {**candidate, "applicability_status": status, "applicability_evidence_count": len(matches)}
    score = float(updated.get("confidence_score") or 0.0)
    if status == "rejected":
        updated.update(confidence_label="low", confidence_score=min(score, 0.35))
    elif status in {"check_failed", "not_found"}:
        updated["confidence_score"] = min(score, 0.78)
        if updated.get("confidence_label") == "high":
            updated["confidence_label"] = "medium"
    blockers = list(updated.get("blocking_reasons") or [])
    if blocker and blocker not in blockers:
        blockers.append(blocker)
    updated["blocking_reasons"] = blockers
    return updated


def _prioritize_applicability(
    candidates: list[dict[str, Any]],
    *,
    max_candidates: int,
    live_allowed: bool,
    lookup: Callable[..., dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    evidence: list[dict[str, Any]] = []
    checks: dict[str, list[dict[str, Any]]] = {}
    for index, original in enumerate(candidates[:max_candidates]):
        part_number = original.get("part_number")
        if not part_number:
            continue
        call = lookup("oe_applicability", live_allowed=live_allowed, part_number=part_number)
        candidate = _with_applicability(original, call)
        candidates[index] = candidate
        checks[candidate["candidate_id"]] = [call]
        evidence.append(
            {
                "candidate_id": candidate["candidate_id"],
                "operation": "oe_applicability",
                "ok": bool(call.get("ok")),
                "empty_payload": bool(call.get("empty_payload")),
                "oem_candidate_count": len(call.get("oem_candidates") or []),
                "status": candidate.get("applicability_status"),
            }
        )

    order = {"catalog_evidence_found": 0, "not_checked": 1, "not_found": 2, "check_failed": 3, "rejected": 4}
    candidates.sort(
        key=lambda item: (
            order.get(str(item.get("applicability_status") or "not_checked"), 1),
            -float(item.get("confidence_score") or 0.0),
            item.get("part_number") or "",
        )
    )
    return candidates, evidence, checks


def _manual_action(code: str, message: str | None = None, *, priority: int = 1, **context: Any) -> dict[str, Any]:
    return {"code": code, "priority": priority, **({"message": message} if message else {}), **context}


def _oem_lookup_outcome(call: dict[str, Any] | None) -> dict[str, Any]:
    """Summarise the OEM lookup without turning provider failure into no-result."""

    if call is None:
        return {"outcome": "not_attempted", "provider_outcome": None, "retryable": False}
    if call.get("dry_run"):
        return {"outcome": "not_run", "provider_outcome": call.get("outcome"), "retryable": False}
    if not call.get("ok"):
        return {
            "outcome": "provider_failed",
            "provider_outcome": call.get("outcome") or call.get("failure_class") or "provider_failed",
            "failure_class": call.get("failure_class") or call.get("outcome"),
            "retryable": bool(call.get("retryable")),
            "requires_fallback": bool(call.get("requires_fallback")),
        }
    if call.get("outcome") == "empty_result":
        return {"outcome": "empty_result", "provider_outcome": "empty_result", "retryable": False}
    return {
        "outcome": "candidates_found" if call.get("oem_candidates") else "response_received",
        "provider_outcome": call.get("outcome") or "success",
        "retryable": False,
    }


def _append_oem_failure_blocker(blockers: list[dict[str, Any]], oem_lookup: dict[str, Any]) -> None:
    if oem_lookup["outcome"] == "provider_failed":
        blockers.append(
            {
                "stage": "partsapi_oem_lookup",
                "operation": "parts_by_vin",
                "outcome": oem_lookup["provider_outcome"],
                "failure_class": oem_lookup.get("failure_class"),
                "retryable": oem_lookup["retryable"],
                "requires_fallback": oem_lookup.get("requires_fallback", True),
            }
        )


def _append_no_candidate_manual_action(
    manual_actions: list[dict[str, Any]],
    *,
    oem_lookup: dict[str, Any],
    readiness: dict[str, Any],
    live_partsapi_oem: bool,
    category_resolution: dict[str, Any],
) -> None:
    if oem_lookup["outcome"] == "provider_failed":
        manual_actions.append(
            _manual_action(
                "retry_or_manual_epc",
                "getPartsbyVIN did not return a usable result; retry only if marked retryable, otherwise use EPC.",
                priority=2,
                provider_outcome=oem_lookup["provider_outcome"],
                retryable=oem_lookup["retryable"],
            )
        )
    elif readiness["ready_for_oem_candidate_lookup"] and live_partsapi_oem:
        manual_actions.append(
            _manual_action("manual_epc_fallback", "OEM-кандидаты не найдены: проверить брендовый EPC вручную.")
        )
    elif readiness["ready_for_oem_candidate_lookup"]:
        manual_actions.append(
            _manual_action(
                "run_live_get_parts_by_vin",
                f"Вызвать getPartsbyVIN cat={category_resolution.get('category')} с лимитом live-запросов.",
            )
        )


def _status(
    readiness: dict[str, Any], *, candidates: list[dict[str, Any]], live_partsapi_oem: bool, oem_lookup: dict[str, Any]
) -> str:
    if not readiness.get("has_identifier"):
        return "needs_vin_or_frame"
    if not readiness.get("ready_for_identity_crosscheck"):
        return "needs_identity_confirmation"
    if not readiness.get("ready_for_category_lookup"):
        return "needs_part_clarification"
    if readiness.get("needs_partsapi_category_mapping"):
        return "needs_partsapi_category_mapping"
    if not readiness.get("ready_for_oem_candidate_lookup"):
        return "needs_identity_confirmation"
    if candidates:
        return "oem_candidates_found_needs_manual_confirmation"
    if live_partsapi_oem:
        if oem_lookup.get("outcome") == "provider_failed":
            return "oem_lookup_provider_failed"
        return "no_oem_candidate_found_needs_manual_epc"
    return "ready_for_live_oem_candidate_lookup"


def resolve_vin_oem_parts(
    *,
    identifier: str,
    requested_part: str,
    make: str | None = None,
    model: str | None = None,
    model_year: int | None = None,
    engine: str | None = None,
    transmission: str | None = None,
    market: str | None = None,
    drivetrain: str | None = None,
    axle: str | None = None,
    side: str | None = None,
    position: str | None = None,
    inner_outer: str | None = None,
    live_vpic: bool = True,
    live_partsapi_identity: bool = False,
    live_partsapi_oem: bool = False,
    max_live_calls: int = 3,
    max_candidates: int = 3,
    timeout: float = 20.0,
    max_attempts: int = 1,
    partsapi_category_index: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    raw_identifier = _compact(identifier)
    part_text = _compact(requested_part)
    classification = classify_identifier(raw_identifier)
    context = {
        "make": _compact(make),
        "model": _compact(model),
        "model_year": model_year,
        "engine": _compact(engine),
        "transmission": _compact(transmission),
        "market": _compact(market),
        "drivetrain": _compact(drivetrain),
        "axle": _compact(axle),
        "side": _compact(side),
        "position": _compact(position),
        "inner_outer": _compact(inner_outer),
        "requested_part": part_text,
    }
    part_profile = normalize_part_intent(
        part_text,
        axle=axle,
        side=side,
        position=position,
        inner_outer=inner_outer,
    )
    category_resolution = resolve_partsapi_category(
        part_text,
        category_index_path=partsapi_category_index,
    )

    calls: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    manual_actions: list[dict[str, Any]] = []
    live_calls_used = 0

    identity = (
        decode_vehicle_identity(
            raw_identifier,
            crm_context=context,
            model_year=model_year,
            make_hint=make,
            live_vpic=live_vpic and not dry_run,
            # WMI is another live vPIC endpoint.  A resolver dry-run must not
            # leak into it when the full VIN decoder has been disabled.
            live_wmi=live_vpic and not dry_run,
        )
        if raw_identifier
        else {
            "confidence": 0.0,
            "confidence_label": "low",
            "parts_lookup_readiness": {
                "ready_for_oem_lookup": False,
                "ready_for_oem_candidate_lookup": False,
                "ready_for_crm_writeback": False,
                "blocking_reasons": ["missing_identifier"],
            },
            "vehicle_profile": {},
            "conflicts": [],
            "warnings": [],
        }
    )

    def partsapi_call(operation: str, *, live_allowed: bool, **kwargs: Any) -> dict[str, Any]:
        nonlocal live_calls_used
        call_is_live = bool(live_allowed and not dry_run and live_calls_used < max(0, int(max_live_calls)))
        if live_allowed and not call_is_live and not dry_run:
            blockers.append(
                {
                    "stage": "partsapi_live_budget",
                    "operation": operation,
                    "error": "PartsAPI live call budget exhausted before this operation.",
                }
            )
        call = partsapi_catalog_lookup(
            operation=operation,
            timeout=timeout,
            max_attempts=max_attempts,
            dry_run=not call_is_live,
            **kwargs,
        )
        # A credentials/input rejection never left the process, so it must not
        # consume the small live budget that could still reach another source.
        if call_is_live and int(call.get("attempt_count") or 0) > 0:
            live_calls_used += 1
        calls.append(call)
        return call

    identity_call = None
    if raw_identifier:
        identity_call = partsapi_call(
            "vin_decode_oe",
            live_allowed=live_partsapi_identity,
            identifier=raw_identifier,
        )
        identity = _identity_with_partsapi_agreement(identity, identity_call)

    raw_identity_readiness = identity.get("parts_lookup_readiness")
    identity_readiness: dict[str, Any] = (
        {str(key): value for key, value in raw_identity_readiness.items()}
        if isinstance(raw_identity_readiness, dict)
        else {}
    )
    raw_conflicts = identity.get("conflicts")
    conflicts = [item for item in raw_conflicts if isinstance(item, dict)] if isinstance(raw_conflicts, list) else []
    high_identity_conflict = any(item.get("severity") == "high" for item in conflicts)
    has_identifier = bool(raw_identifier)
    part_actionable = bool(part_profile.get("recognized")) and not bool(part_profile.get("clarification_required"))
    category_queryable = bool(
        category_resolution.get(
            "category_queryable",
            category_resolution.get("category_kind") == "numeric_id"
            and not category_resolution.get("category_unresolved"),
        )
    )
    raw_blocking_reasons = identity_readiness.get("blocking_reasons")
    blocking_reasons = list(raw_blocking_reasons) if isinstance(raw_blocking_reasons, list) else []
    ready_for_oem_candidate_lookup = (
        bool(identity_readiness.get("ready_for_oem_candidate_lookup")) and part_actionable and category_queryable
    )
    readiness: dict[str, Any] = {
        "has_identifier": has_identifier,
        "ready_for_identity_crosscheck": has_identifier and not high_identity_conflict,
        "ready_for_category_lookup": part_actionable,
        "needs_partsapi_category_mapping": part_actionable and not category_queryable,
        "ready_for_oem_candidate_lookup": ready_for_oem_candidate_lookup,
        "ready_for_applicability_enrichment": False,
        "ready_for_crm_writeback": False,
        "blocking_reasons": blocking_reasons,
    }
    if not has_identifier:
        manual_actions.append(
            _manual_action("request_identifier", "Указать VIN, frame или body number перед OEM-поиском.")
        )
    if not part_profile.get("recognized"):
        manual_actions.append(
            _manual_action("clarify_part", "Уточнить точную группу детали и старый номер/OEM при наличии.")
        )
    elif part_profile.get("clarification_required"):
        manual_actions.append(
            _manual_action(
                "clarify_part_position",
                fields=list(part_profile.get("clarification_fields") or []),
            )
        )
    if part_actionable and not category_queryable:
        manual_actions.append(
            _manual_action(
                "map_partsapi_category",
                "Построить или обновить PartsAPI category index и выбрать контролируемый cat для getPartsbyVIN.",
            )
        )
    if not identity_readiness.get("ready_for_oem_candidate_lookup"):
        manual_actions.append(
            _manual_action(
                "confirm_identity",
                "Подтвердить identity через VINdecodeOE/vPIC/CRM перед поиском OEM-кандидатов.",
                priority=2,
            )
        )

    parts_call = None
    if (
        raw_identifier
        and category_resolution.get("category")
        and category_queryable
        and (readiness["ready_for_oem_candidate_lookup"] or dry_run or not live_partsapi_oem)
    ):
        parts_call = partsapi_call(
            "parts_by_vin",
            live_allowed=bool(live_partsapi_oem and ready_for_oem_candidate_lookup),
            identifier=raw_identifier,
            part_type="oem",
            category=str(category_resolution.get("category")),
        )
    elif part_actionable:
        blockers.append(
            {
                "stage": "partsapi_category",
                "operation": "parts_by_vin",
                "error": "A controlled PartsAPI category is required before getPartsbyVIN.",
                "category_resolution": category_resolution,
            }
        )

    raw_candidates = [
        candidate for candidate in (parts_call or {}).get("oem_candidates", []) if isinstance(candidate, dict)
    ]
    oem_lookup = _oem_lookup_outcome(parts_call)
    _append_oem_failure_blocker(blockers, oem_lookup)
    ranked_candidates = [
        _rank_oem_candidate(
            candidate,
            index=index,
            category_resolution=category_resolution,
            part_profile=part_profile,
            identity=identity,
        )
        for index, candidate in enumerate(raw_candidates[:max_candidates], start=1)
    ]
    ranked_candidates.sort(
        key=lambda item: (-float(item.get("confidence_score") or 0.0), item.get("part_number") or "")
    )
    if ranked_candidates:
        readiness["ready_for_applicability_enrichment"] = True
        manual_actions.append(
            _manual_action(
                "review_oem_candidates",
                "Проверить OEM-кандидаты, применимость, quantity basis и выбрать строку для ручного подтверждения.",
            )
        )
    else:
        _append_no_candidate_manual_action(
            manual_actions,
            oem_lookup=oem_lookup,
            readiness=readiness,
            live_partsapi_oem=live_partsapi_oem,
            category_resolution=category_resolution,
        )

    article_enrichment: list[dict[str, Any]] = []
    cross_candidates: list[dict[str, Any]] = []
    ranked_candidates, applicability_evidence, checks_by_candidate = _prioritize_applicability(
        ranked_candidates,
        max_candidates=max_candidates,
        live_allowed=live_partsapi_oem,
        lookup=partsapi_call,
    )

    # Applicability is decision-changing, so every bounded candidate gets its
    # chance at the live-call budget before article and cross enrichment.
    for candidate in ranked_candidates[:max_candidates]:
        part_number = candidate.get("part_number")
        brand = candidate.get("brand")
        if not part_number:
            continue
        candidate_checks = checks_by_candidate.get(candidate["candidate_id"], [])
        search_call = partsapi_call("search_articles", live_allowed=live_partsapi_oem, part_number=part_number)
        candidate_checks.append(search_call)
        for article in (search_call.get("article_candidates") or [])[:max_candidates]:
            article_id = article.get("article_id") if isinstance(article, dict) else None
            if article_id not in (None, ""):
                candidate_checks.append(
                    partsapi_call("article_crosses", live_allowed=live_partsapi_oem, article_id=article_id)
                )
        candidate_checks.append(partsapi_call("crosses_title", live_allowed=live_partsapi_oem, part_number=part_number))
        if brand:
            candidate_checks.append(
                partsapi_call(
                    "crosses_with_brand", live_allowed=live_partsapi_oem, part_number=part_number, brand=str(brand)
                )
            )
        else:
            candidate_checks.append(partsapi_call("crosses", live_allowed=live_partsapi_oem, part_number=part_number))
        article_enrichment.append(
            {
                "candidate_id": candidate["candidate_id"],
                "article_candidates": [
                    article
                    for call in candidate_checks
                    for article in (call.get("article_candidates") or [])[:max_candidates]
                    if isinstance(article, dict)
                ][:max_candidates],
                "checks": [_call_digest(call) for call in candidate_checks],
            }
        )
        cross_candidates.extend(
            {
                **cross,
                "candidate_id": candidate["candidate_id"],
            }
            for call in candidate_checks
            for cross in (call.get("cross_candidates") or [])[:max_candidates]
            if isinstance(cross, dict)
        )

    current_status = _status(
        readiness,
        candidates=ranked_candidates,
        live_partsapi_oem=live_partsapi_oem and not dry_run,
        oem_lookup=oem_lookup,
    )
    return {
        "ok": True,
        "schema": "VinOemResolution",
        "mode": "read_only_vin_oem_resolution",
        "status": current_status,
        "identifier": {
            "redacted": _redact_identifier(raw_identifier),
            "kind": classification.kind,
            "market_hint": classification.market_hint,
            "raw_identifier_is_sensitive": True,
        },
        "identity": _identity_digest(identity),
        "part_intent": part_profile,
        "category_resolution": category_resolution,
        "readiness": readiness,
        "oem_candidates": ranked_candidates,
        "candidate_count": len(ranked_candidates),
        "oem_lookup_outcome": oem_lookup,
        "enrichment": {
            "applicability_evidence": applicability_evidence[:max_candidates],
            "article_enrichment": article_enrichment[:max_candidates],
            "cross_candidates": cross_candidates[:max_candidates],
        },
        "calls": [_call_digest(call) for call in calls],
        "call_count": len(calls),
        "live_call_count": live_calls_used,
        "max_live_calls": max_live_calls,
        "blockers": blockers,
        "manual_actions": manual_actions,
        "crm_writeback_gate": {
            "can_write_final_material_line_now": False,
            "can_prepare_manual_writeback": bool(ranked_candidates),
            "requires_manual_confirmation_before_writeback": True,
            "ready_for_crm_writeback": False,
            "selected_candidate_id": None,
        },
        "privacy": {
            "raw_identifier_is_sensitive": True,
            "raw_identifier_redacted_from_output": True,
            "secret_exposed": False,
        },
    }
