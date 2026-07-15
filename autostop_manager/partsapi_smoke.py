from __future__ import annotations

import random
from typing import Any

from .catalog_clients import partsapi_catalog_lookup, resolve_partsapi_category
from .parts_intent import normalize_part_intent
from .vehicle_identity import decode_vehicle_identity
from .vin_lookup import classify_identifier


def _redact_identifier(identifier: str) -> str:
    compact = "".join(str(identifier or "").split()).upper()
    if len(compact) <= 6:
        return compact[:2] + "***" if compact else ""
    return f"{compact[:3]}***{compact[-3:]}"


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _item_identifier(item: dict[str, Any]) -> str:
    return _first_text(item.get("vin"), item.get("frame"), item.get("body_number"), item.get("identifier"))


def _item_requested_part(item: dict[str, Any]) -> str:
    return _first_text(item.get("requested_part"), item.get("reason"), item.get("heading"), item.get("summary"))


def _identity_digest(identity: dict[str, Any]) -> dict[str, Any]:
    profile = identity.get("vehicle_profile") or {}
    readiness = identity.get("parts_lookup_readiness") or {}
    return {
        "confidence": identity.get("confidence"),
        "confidence_label": identity.get("confidence_label"),
        "ready_for_oem_lookup": readiness.get("ready_for_oem_lookup"),
        "ready_for_oem_candidate_lookup": readiness.get(
            "ready_for_oem_candidate_lookup", readiness.get("ready_for_oem_lookup")
        ),
        "ready_for_crm_writeback": readiness.get("ready_for_crm_writeback", readiness.get("ready_for_oem_lookup")),
        "cross_source_agreement": readiness.get("cross_source_agreement") or {},
        "blocking_reasons": readiness.get("blocking_reasons") or [],
        "vehicle_profile": {
            key: profile.get(key)
            for key in ("make", "model", "model_family", "platform", "model_year", "engine", "transmission", "market")
            if profile.get(key) not in (None, "")
        },
        "warning_count": len(identity.get("warnings", [])),
        "conflict_count": len(identity.get("conflicts", [])),
    }


def _safe_call_digest(call: dict[str, Any]) -> dict[str, Any]:
    request_plan = call.get("request_plan") or {}
    return {
        "provider": call.get("provider"),
        "operation": call.get("operation"),
        "partsapi_method": call.get("partsapi_method"),
        "ok": bool(call.get("ok")),
        "dry_run": bool(call.get("dry_run")),
        "empty_payload": bool(call.get("empty_payload")),
        "missing_env_names": call.get("missing_env_names") or request_plan.get("missing_env_names") or [],
        "missing_params": call.get("missing_params") or [],
        "error": call.get("error"),
        "request_plan": request_plan,
        "vehicle_profile_count": len(call.get("vehicle_profiles") or []),
        "oem_candidate_count": len(call.get("oem_candidates") or []),
        "cross_candidate_count": len(call.get("cross_candidates") or []),
        "article_candidate_count": len(call.get("article_candidates") or []),
    }


def _call_missing_env_names(call: dict[str, Any]) -> list[str]:
    request_plan = call.get("request_plan") or {}
    return call.get("missing_env_names") or request_plan.get("missing_env_names") or []


def _candidate_digest(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": candidate.get("provider"),
        "brand": candidate.get("brand"),
        "part_number": candidate.get("part_number"),
        "name": candidate.get("name") or candidate.get("product_name"),
        "confidence": candidate.get("confidence"),
        "fitment_evidence": candidate.get("fitment_evidence") or {},
    }


def _article_digest(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "article_id": candidate.get("article_id"),
        "brand": candidate.get("brand"),
        "part_number": candidate.get("part_number"),
        "product_name": candidate.get("product_name"),
        "found_via": candidate.get("found_via"),
        "confidence": candidate.get("confidence"),
    }


def select_crm_partsapi_smoke_case(
    repair_orders: list[dict[str, Any]],
    *,
    random_seed: int = 0,
    include_raw_identifier: bool = False,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for order in repair_orders:
        identifier = _item_identifier(order)
        requested_part = _item_requested_part(order)
        if not identifier or not requested_part:
            continue
        part_profile = normalize_part_intent(requested_part)
        if not part_profile.get("recognized"):
            continue
        classification = classify_identifier(identifier)
        candidates.append(
            {
                "order": order,
                "score": (
                    2 if classification.kind in {"vin", "frame_number"} else 0,
                    float(part_profile.get("confidence") or 0.0),
                ),
            }
        )

    if not candidates:
        return {
            "ok": False,
            "error": "No open CRM order with a usable VIN/frame and recognized part intent was found.",
            "candidate_count": 0,
        }

    top_score = max(candidate["score"] for candidate in candidates)
    top = [candidate["order"] for candidate in candidates if candidate["score"] == top_score]
    selected = random.Random(random_seed).choice(top)
    identifier = _item_identifier(selected)
    selected_item = {
        "card_id": selected.get("card_id"),
        "number": selected.get("number"),
        "vehicle": selected.get("vehicle"),
        "identifier": _redact_identifier(identifier),
        "raw_identifier_is_sensitive": True,
        "requested_part": _item_requested_part(selected),
    }
    if include_raw_identifier:
        selected_item["raw_identifier"] = identifier
    return {
        "ok": True,
        "candidate_count": len(candidates),
        "selected": selected_item,
    }


def build_partsapi_vin_smoke_report(
    item: dict[str, Any],
    *,
    requested_part: str | None = None,
    partsapi_category: str | None = None,
    part_type: str = "oem",
    max_candidates: int = 3,
    timeout: float = 20.0,
    live_vpic: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    identifier = _item_identifier(item)
    part_text = str(requested_part or _item_requested_part(item)).strip()
    if not identifier:
        return {"ok": False, "error": "VIN/frame/body identifier is required.", "privacy": {"secret_exposed": False}}
    if not part_text:
        return {
            "ok": False,
            "error": "Requested part text is required.",
            "privacy": {"raw_identifier_is_sensitive": True, "secret_exposed": False},
        }

    classification = classify_identifier(identifier)
    part_profile = normalize_part_intent(part_text)
    category_resolution = resolve_partsapi_category(part_text, explicit_category=partsapi_category)
    identity = decode_vehicle_identity(identifier=identifier, crm_context=item, live_vpic=live_vpic)

    calls: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    identity_calls: tuple[tuple[str, dict[str, Any]], ...] = (
        ("vin_decode", {"identifier": identifier, "lang": "ru"}),
        ("vin_decode_oe", {"identifier": identifier}),
    )
    for operation, kwargs in identity_calls:
        call = partsapi_catalog_lookup(operation=operation, timeout=timeout, dry_run=dry_run, **kwargs)
        calls.append(call)

    category = category_resolution.get("category")
    can_call_parts_by_vin = bool(category) and (dry_run or category_resolution.get("category_kind") == "numeric_id")
    if can_call_parts_by_vin:
        parts_call = partsapi_catalog_lookup(
            operation="parts_by_vin",
            identifier=identifier,
            part_type=part_type,
            category=str(category),
            timeout=timeout,
            dry_run=dry_run,
        )
        calls.append(parts_call)
    else:
        blockers.append(
            {
                "stage": "parts_by_vin",
                "code": "category_unresolved",
                "message": "Numeric PartsAPI cat id is required for live getPartsbyVIN.",
                "category_resolution": category_resolution,
            }
        )
        parts_call = {"oem_candidates": []}

    oem_candidates = [
        candidate for candidate in (parts_call.get("oem_candidates") or []) if isinstance(candidate, dict)
    ]
    enriched: list[dict[str, Any]] = []
    for candidate in oem_candidates[:max_candidates]:
        part_number = str(candidate.get("part_number") or "").strip()
        brand = str(candidate.get("brand") or "").strip()
        if not part_number:
            continue
        search_call = partsapi_catalog_lookup(
            operation="search_articles", part_number=part_number, timeout=timeout, dry_run=dry_run
        )
        candidate_calls = [search_call]
        for article in (search_call.get("article_candidates") or [])[:max_candidates]:
            article_id = article.get("article_id") if isinstance(article, dict) else None
            if article_id not in (None, ""):
                candidate_calls.append(
                    partsapi_catalog_lookup(
                        operation="article_crosses", article_id=article_id, timeout=timeout, dry_run=dry_run
                    )
                )
        candidate_calls.extend(
            [
                partsapi_catalog_lookup(
                    operation="oe_applicability", part_number=part_number, timeout=timeout, dry_run=dry_run
                ),
                partsapi_catalog_lookup(
                    operation="crosses_title", part_number=part_number, timeout=timeout, dry_run=dry_run
                ),
            ]
        )
        if brand:
            candidate_calls.append(
                partsapi_catalog_lookup(
                    operation="crosses_with_brand",
                    part_number=part_number,
                    brand=brand,
                    timeout=timeout,
                    dry_run=dry_run,
                )
            )
        else:
            candidate_calls.append(
                partsapi_catalog_lookup(operation="crosses", part_number=part_number, timeout=timeout, dry_run=dry_run)
            )
        calls.extend(candidate_calls)
        enriched.append(
            {
                "candidate": _candidate_digest(candidate),
                "checks": [_safe_call_digest(call) for call in candidate_calls],
                "article_candidates": [
                    _article_digest(article)
                    for call in candidate_calls
                    for article in (call.get("article_candidates") or [])[:max_candidates]
                    if isinstance(article, dict)
                ][:max_candidates],
                "cross_candidates": [
                    _candidate_digest(cross)
                    for call in candidate_calls
                    for cross in (call.get("cross_candidates") or [])[:max_candidates]
                    if isinstance(cross, dict)
                ][:max_candidates],
            }
        )

    required_operations = {"vin_decode_oe"}
    if can_call_parts_by_vin:
        required_operations.add("parts_by_vin")
    required_calls = [call for call in calls if call.get("operation") in required_operations]
    optional_calls = [call for call in calls if call.get("operation") not in required_operations]
    missing_env_names = sorted({name for call in required_calls for name in _call_missing_env_names(call)})
    optional_missing_env_names = sorted({name for call in optional_calls for name in _call_missing_env_names(call)})
    failed_required_calls = [call for call in required_calls if not call.get("ok")]
    return {
        "ok": not missing_env_names and not blockers and not failed_required_calls,
        "provider": "partsapi_vin_smoke",
        "crm_order": {
            "number": item.get("number"),
            "card_id": item.get("card_id"),
            "vehicle": item.get("vehicle"),
        },
        "identifier": {
            "redacted": _redact_identifier(identifier),
            "kind": classification.kind,
            "market_hint": classification.market_hint,
            "raw_identifier_is_sensitive": True,
        },
        "requested_part": {
            "recognized": bool(part_profile.get("recognized")),
            "intent_id": part_profile.get("intent_id"),
            "canonical_name_ru": part_profile.get("canonical_name_ru"),
            "confidence": part_profile.get("confidence"),
        },
        "identity": _identity_digest(identity),
        "partsapi_category_resolution": category_resolution,
        "call_count": len(calls),
        "calls": [_safe_call_digest(call) for call in calls],
        "candidate_count": len(oem_candidates),
        "oem_candidates": [_candidate_digest(candidate) for candidate in oem_candidates[:max_candidates]],
        "enrichment": enriched[:max_candidates],
        "blockers": blockers,
        "failed_required_calls": [_safe_call_digest(call) for call in failed_required_calls],
        "missing_env_names": missing_env_names,
        "optional_missing_env_names": optional_missing_env_names,
        "privacy": {"raw_identifier_is_sensitive": True, "secret_exposed": False},
    }
