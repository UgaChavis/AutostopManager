from __future__ import annotations

import hashlib
import re
from typing import Any

from .catalog_clients import partsapi_catalog_lookup, resolve_partsapi_category
from .parts_intent import normalize_part_intent
from .vehicle_identity import decode_vehicle_identity
from .vin_lookup import classify_identifier


def _compact(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _normalize_compare_value(value: Any) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", str(value or "").casefold())


def _redact_identifier(identifier: str) -> dict[str, Any]:
    compact = "".join(str(identifier or "").split()).upper()
    if not compact:
        return {"display": "", "length": 0, "prefix": ""}
    if len(compact) <= 6:
        return {"display": f"{compact[:2]}***", "length": len(compact), "prefix": compact[:2]}
    return {"display": f"{compact[:3]}***{compact[-3:]}", "length": len(compact), "prefix": compact[:3]}


def _hash_candidate(*parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _call_digest(call: dict[str, Any]) -> dict[str, Any]:
    request_plan = call.get("request_plan") or {}
    return {
        "provider": call.get("provider"),
        "operation": call.get("operation"),
        "partsapi_method": call.get("partsapi_method"),
        "ok": bool(call.get("ok")),
        "dry_run": bool(call.get("dry_run")),
        "empty_payload": bool(call.get("empty_payload")),
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
        "ready_for_oem_candidate_lookup": readiness.get("ready_for_oem_candidate_lookup", readiness.get("ready_for_oem_lookup")),
        "ready_for_crm_writeback": readiness.get("ready_for_crm_writeback", False),
        "cross_source_agreement": readiness.get("cross_source_agreement") or {},
        "blocking_reasons": readiness.get("blocking_reasons") or [],
        "vehicle_profile": {
            key: profile.get(key)
            for key in ("make", "model", "model_family", "platform", "model_year", "engine", "transmission", "market", "production_date")
            if profile.get(key) not in (None, "")
        },
        "conflict_count": len(identity.get("conflicts") or []),
        "high_severity_conflict_count": sum(1 for item in identity.get("conflicts") or [] if item.get("severity") == "high"),
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
        "model": (profile.get("model") or profile.get("model_family"), oe_profile.get("model") or oe_profile.get("model_family")),
        "transmission": (profile.get("transmission"), oe_profile.get("transmission")),
    }
    matched: list[str] = []
    conflicts: list[dict[str, Any]] = []
    for field, (left, right) in compare_fields.items():
        left_norm = _normalize_compare_value(left)
        right_norm = _normalize_compare_value(right)
        if not left_norm or not right_norm:
            continue
        if left_norm in right_norm or right_norm in left_norm:
            matched.append(field)
        else:
            conflicts.append({"field": field, "identity": left, "partsapi_oe": right})
    if conflicts:
        return {"status": "conflict", "matched_fields": matched, "conflicting_fields": conflicts, "partsapi_profile": oe_profile}
    if matched:
        return {"status": "matched", "matched_fields": matched, "conflicting_fields": [], "partsapi_profile": oe_profile}
    return {"status": "profile_present_uncompared", "matched_fields": [], "conflicting_fields": [], "partsapi_profile": oe_profile}


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
    if part_profile.get("clarification_required"):
        blockers.append("part_position_clarification_required")
    if category_resolution.get("category_unresolved"):
        blockers.append("partsapi_category_unresolved")
    if any(item.get("severity") == "high" for item in identity.get("conflicts") or []):
        blockers.append("high_severity_identity_conflict")
    if category_resolution.get("validation_required"):
        blockers.append("category_validation_required")

    source_is_vin_specific = source_operation == "parts_by_vin" and bool(fitment.get("is_fit_for_this_vin"))
    position_match = "blocked" if part_profile.get("clarification_required") else "matched_or_not_required"
    base_score = float(candidate.get("confidence") or 0.55)
    if source_is_vin_specific and not blockers:
        confidence_label = "high"
        score = max(base_score, 0.92)
    elif source_is_vin_specific:
        confidence_label = "medium"
        score = max(min(base_score, 0.86), 0.72)
    else:
        confidence_label = "low"
        score = min(base_score, 0.6)

    part_number = str(candidate.get("part_number") or "").strip()
    brand = candidate.get("brand")
    return {
        "candidate_id": f"oem-{index}-{_hash_candidate(candidate.get('provider'), brand, part_number, candidate.get('name'))}",
        "part_number": part_number,
        "brand": brand,
        "name": candidate.get("name"),
        "source": candidate.get("provider"),
        "source_operation": source_operation,
        "category_id": category_resolution.get("category") if category_resolution.get("category_kind") == "numeric_id" else None,
        "fitment_scope": "vin_specific" if source_is_vin_specific else "not_vin_specific",
        "position_match": position_match,
        "quantity_basis": part_profile.get("quantity_basis"),
        "confidence_label": confidence_label,
        "confidence_score": round(score, 4),
        "blocking_reasons": blockers,
        "manual_review_required": True,
        "fitment_evidence": fitment,
    }


def _manual_action(code: str, message: str, *, priority: int = 1) -> dict[str, Any]:
    return {"code": code, "priority": priority, "message": message}


def _status(readiness: dict[str, Any], *, candidates: list[dict[str, Any]], live_partsapi_oem: bool) -> str:
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
        "requested_part": part_text,
    }
    part_profile = normalize_part_intent(part_text, axle=axle, side=side, position=position)
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
        if call_is_live:
            live_calls_used += 1
        call = partsapi_catalog_lookup(
            operation=operation,
            timeout=timeout,
            max_attempts=max_attempts,
            dry_run=not call_is_live,
            **kwargs,
        )
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

    identity_readiness = identity.get("parts_lookup_readiness") or {}
    high_identity_conflict = any(item.get("severity") == "high" for item in identity.get("conflicts") or [])
    has_identifier = bool(raw_identifier)
    part_actionable = bool(part_profile.get("recognized")) and not bool(part_profile.get("clarification_required"))
    category_numeric = category_resolution.get("category_kind") == "numeric_id" and not category_resolution.get("category_unresolved")
    readiness = {
        "has_identifier": has_identifier,
        "ready_for_identity_crosscheck": has_identifier and not high_identity_conflict,
        "ready_for_category_lookup": part_actionable,
        "needs_partsapi_category_mapping": part_actionable and not category_numeric,
        "ready_for_oem_candidate_lookup": bool(identity_readiness.get("ready_for_oem_candidate_lookup")) and part_actionable and category_numeric,
        "ready_for_applicability_enrichment": False,
        "ready_for_crm_writeback": False,
        "blocking_reasons": list(identity_readiness.get("blocking_reasons") or []),
    }
    if not has_identifier:
        manual_actions.append(_manual_action("request_identifier", "Указать VIN, frame или body number перед OEM-поиском."))
    if not part_profile.get("recognized"):
        manual_actions.append(_manual_action("clarify_part", "Уточнить точную группу детали и старый номер/OEM при наличии."))
    elif part_profile.get("clarification_required"):
        manual_actions.append(_manual_action("clarify_part_position", part_profile.get("crm_clarification_prompt") or part_profile.get("clarification_prompt") or "Уточнить позицию детали."))
    if part_actionable and not category_numeric:
        manual_actions.append(_manual_action("map_partsapi_category", "Построить или обновить PartsAPI category index и выбрать numeric cat для getPartsbyVIN."))
    if not identity_readiness.get("ready_for_oem_candidate_lookup"):
        manual_actions.append(_manual_action("confirm_identity", "Подтвердить identity через VINdecodeOE/vPIC/CRM перед поиском OEM-кандидатов.", priority=2))

    parts_call = None
    if raw_identifier and category_resolution.get("category") and (readiness["ready_for_oem_candidate_lookup"] or dry_run or not live_partsapi_oem):
        parts_call = partsapi_call(
            "parts_by_vin",
            live_allowed=live_partsapi_oem and readiness["ready_for_oem_candidate_lookup"],
            identifier=raw_identifier,
            part_type="oem",
            category=str(category_resolution.get("category")),
        )
    elif part_actionable:
        blockers.append(
            {
                "stage": "partsapi_category",
                "operation": "parts_by_vin",
                "error": "Numeric PartsAPI category is required before getPartsbyVIN.",
                "category_resolution": category_resolution,
            }
        )

    raw_candidates = [candidate for candidate in (parts_call or {}).get("oem_candidates", []) if isinstance(candidate, dict)]
    ranked_candidates = [
        _rank_oem_candidate(candidate, index=index, category_resolution=category_resolution, part_profile=part_profile, identity=identity)
        for index, candidate in enumerate(raw_candidates[:max_candidates], start=1)
    ]
    ranked_candidates.sort(key=lambda item: (-float(item.get("confidence_score") or 0.0), item.get("part_number") or ""))
    if ranked_candidates:
        readiness["ready_for_applicability_enrichment"] = True
        manual_actions.append(_manual_action("review_oem_candidates", "Проверить OEM-кандидаты, применимость, quantity basis и выбрать строку для ручного подтверждения."))
    elif readiness["ready_for_oem_candidate_lookup"] and live_partsapi_oem:
        manual_actions.append(_manual_action("manual_epc_fallback", "OEM-кандидаты не найдены: проверить брендовый EPC/Parts-Catalogs/17VIN вручную."))
    elif readiness["ready_for_oem_candidate_lookup"]:
        manual_actions.append(_manual_action("run_live_get_parts_by_vin", f"Вызвать getPartsbyVIN cat={category_resolution.get('category')} с лимитом live-запросов."))

    article_enrichment: list[dict[str, Any]] = []
    applicability_evidence: list[dict[str, Any]] = []
    cross_candidates: list[dict[str, Any]] = []
    for candidate in ranked_candidates[:max_candidates]:
        part_number = candidate.get("part_number")
        brand = candidate.get("brand")
        if not part_number:
            continue
        candidate_checks: list[dict[str, Any]] = []
        search_call = partsapi_call("search_articles", live_allowed=live_partsapi_oem, part_number=part_number)
        candidate_checks.append(search_call)
        for article in (search_call.get("article_candidates") or [])[:max_candidates]:
            article_id = article.get("article_id") if isinstance(article, dict) else None
            if article_id not in (None, ""):
                candidate_checks.append(partsapi_call("article_crosses", live_allowed=live_partsapi_oem, article_id=article_id))
        candidate_checks.append(partsapi_call("oe_applicability", live_allowed=live_partsapi_oem, part_number=part_number))
        candidate_checks.append(partsapi_call("crosses_title", live_allowed=live_partsapi_oem, part_number=part_number))
        if brand:
            candidate_checks.append(partsapi_call("crosses_with_brand", live_allowed=live_partsapi_oem, part_number=part_number, brand=str(brand)))
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
        applicability_evidence.extend(
            {
                "candidate_id": candidate["candidate_id"],
                "operation": call.get("operation"),
                "ok": bool(call.get("ok")),
                "empty_payload": bool(call.get("empty_payload")),
                "oem_candidate_count": len(call.get("oem_candidates") or []),
            }
            for call in candidate_checks
            if call.get("operation") == "oe_applicability"
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

    current_status = _status(readiness, candidates=ranked_candidates, live_partsapi_oem=live_partsapi_oem and not dry_run)
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
