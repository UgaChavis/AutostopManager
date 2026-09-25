from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

from .catalog_clients import partsapi_catalog_lookup
from .parts_intent import normalize_part_intent
from .vehicle_identity import decode_vehicle_identity, identity_values_agree
from .vin_lookup import classify_identifier

_VIN_IN_TEXT = re.compile(r"(?<![A-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9])", re.IGNORECASE)


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


def _redact_sensitive_output(value: Any, identifier: str) -> Any:
    """Remove VINs in free text as well as the primary identifier from public results."""

    if isinstance(value, str):
        redacted = value
        compact = re.sub(r"[\s-]+", "", identifier).upper()
        if len(compact) >= 4:
            variants = {identifier.strip(), compact}
            frame_parts = re.match(r"^([A-Z]{1,4}\d{1,3}[A-Z]?)(\d{5,7})$", compact)
            if frame_parts:
                variants.add(f"{frame_parts.group(1)}-{frame_parts.group(2)}")
            for variant in sorted(variants, key=len, reverse=True):
                if variant:
                    redacted = re.sub(re.escape(variant), "[REDACTED_IDENTIFIER]", redacted, flags=re.IGNORECASE)
        return _VIN_IN_TEXT.sub("[REDACTED_VIN]", redacted)
    if isinstance(value, dict):
        return {key: _redact_sensitive_output(item, identifier) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive_output(item, identifier) for item in value]
    return value


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
        "ready_for_tecdoc_candidate_lookup": readiness.get("ready_for_tecdoc_candidate_lookup", False),
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


def _partsapi_vehicle_profile(call: dict[str, Any]) -> dict[str, Any]:
    profiles = [profile for profile in call.get("vehicle_profiles") or [] if isinstance(profile, dict)]
    return profiles[0] if len(profiles) == 1 else {}


def _assess_partsapi_identity_agreement(identity: dict[str, Any], call: dict[str, Any]) -> dict[str, Any]:
    if call.get("dry_run"):
        return {"status": "not_checked", "matched_fields": [], "conflicting_fields": []}
    if not call.get("ok"):
        return {"status": "provider_failed", "matched_fields": [], "conflicting_fields": [], "error": call.get("error")}
    profiles = [profile for profile in call.get("vehicle_profiles") or [] if isinstance(profile, dict)]
    if len(profiles) > 1:
        return {"status": "ambiguous_vehicle_modification", "matched_fields": [], "conflicting_fields": []}
    vehicle_profile = _partsapi_vehicle_profile(call)
    if not vehicle_profile:
        return {"status": "no_profile", "matched_fields": [], "conflicting_fields": []}
    if vehicle_profile.get("identifier_matches_request") is False:
        return {"status": "identifier_mismatch", "matched_fields": [], "conflicting_fields": []}
    profile = identity.get("vehicle_profile") or {}
    compare_fields = {
        "make": (profile.get("make"), vehicle_profile.get("make")),
        "model": (
            profile.get("model") or profile.get("model_family"),
            vehicle_profile.get("model") or vehicle_profile.get("model_family"),
        ),
        "transmission": (profile.get("transmission"), vehicle_profile.get("transmission")),
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
            conflicts.append({"field": field, "identity": left, "partsapi_value": right})
    if conflicts:
        return {
            "status": "conflict",
            "matched_fields": matched,
            "conflicting_fields": conflicts,
            "partsapi_profile": vehicle_profile,
        }
    if {"make", "model"}.issubset(matched):
        return {
            "status": "matched",
            "matched_fields": matched,
            "conflicting_fields": [],
            "partsapi_profile": vehicle_profile,
        }
    return {
        "status": "partial_match" if matched else "profile_present_uncompared",
        "matched_fields": matched,
        "conflicting_fields": [],
        "partsapi_profile": vehicle_profile,
    }


def _identity_with_partsapi_agreement(identity: dict[str, Any], call: dict[str, Any]) -> dict[str, Any]:
    updated = {**identity}
    readiness = dict(identity.get("parts_lookup_readiness") or {})
    agreement = _assess_partsapi_identity_agreement(identity, call)
    high_conflict = any(item.get("severity") == "high" for item in identity.get("conflicts") or [])
    confidence_label = str(identity.get("confidence_label") or "")
    can_read = agreement["status"] == "matched" and confidence_label in {"medium", "high"} and not high_conflict
    if not _positive_tecdoc_id(_partsapi_vehicle_profile(call).get("tecdoc_car_id")):
        can_read = False
    blocking = list(readiness.get("blocking_reasons") or [])
    if can_read:
        blocking = [reason for reason in blocking if reason != "identity_confidence_below_high"]
    if agreement["status"] in {"conflict", "identifier_mismatch", "ambiguous_vehicle_modification"}:
        reason = f"partsapi_identity_{agreement['status']}"
        if reason not in blocking:
            blocking.append(reason)
    if agreement["status"] in {"partial_match", "profile_present_uncompared"}:
        reason = "partsapi_identity_insufficient_agreement"
        if reason not in blocking:
            blocking.append(reason)
    if (
        call.get("ok")
        and not call.get("dry_run")
        and not _positive_tecdoc_id(_partsapi_vehicle_profile(call).get("tecdoc_car_id"))
    ):
        reason = "partsapi_identity_missing_tecdoc_car_id"
        if reason not in blocking:
            blocking.append(reason)
    readiness.update(
        {
            "ready_for_tecdoc_candidate_lookup": can_read,
            "ready_for_crm_writeback": False,
            "cross_source_agreement": agreement,
            "blocking_reasons": blocking,
        }
    )
    updated["parts_lookup_readiness"] = readiness
    return updated


def _positive_tecdoc_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    text = str(value or "").strip()
    return text if text.isascii() and text.isdigit() and int(text) > 0 else None


def _canonical_vehicle_type(value: Any) -> str:
    text = _compact(value)
    return {"pc": "PC", "cv": "CV", "motorcycle": "Motorcycle"}.get(text.casefold(), text)


def _manual_action(code: str, message: str | None = None, *, priority: int = 1, **context: Any) -> dict[str, Any]:
    return {"code": code, "priority": priority, **({"message": message} if message else {}), **context}


def _tree_node_resolution(
    rows: list[dict[str, Any]],
    part_profile: dict[str, Any],
    requested_part: str,
    selected_id: str | int | None,
) -> dict[str, Any]:
    """Use an exact named node or a caller-selected node present in this car's tree."""

    levels = (
        ("NODE_3_TEXT", "NODE_3_STR_ID"),
        ("NODE_2_TEXT", "NODE_2_STR_ID"),
        ("NODE_1_TEXT", "NODE_1_STR_ID"),
        ("ROOT_NODE_TEXT", "ROOT_NODE_STR_ID"),
    )
    nodes: dict[str, dict[str, Any]] = {}
    all_nodes: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for depth, (text_key, id_key) in enumerate(levels):
            node_id = _positive_tecdoc_id(row.get(id_key))
            title = _compact(row.get(text_key))
            if node_id and title:
                current = all_nodes.get(node_id)
                if current is None or depth < current["depth"]:
                    all_nodes[node_id] = {"id": node_id, "title": title, "depth": depth, "source_field": id_key}
                if not any(
                    _positive_tecdoc_id(row.get(deeper_id)) and _compact(row.get(deeper_text))
                    for deeper_text, deeper_id in levels[:depth]
                ):
                    # Auto-match only the most specific node in this path.
                    nodes[node_id] = all_nodes[node_id]
    if selected_id not in (None, ""):
        node_id = _positive_tecdoc_id(selected_id)
        selected = all_nodes.get(node_id or "")
        return {
            "category": node_id if selected else None,
            "category_kind": "tecdoc_tree_node_id",
            "category_mode": "explicit_tree_node" if selected else "unresolved",
            "category_queryable": bool(selected),
            "category_unresolved": not bool(selected),
            "candidate_node_ids": [node_id] if selected else [],
            "reason": None if selected else "Selected strId is absent from this vehicle's search tree.",
        }
    specific_names = [
        requested_part,
        part_profile.get("canonical_name_ru"),
        *(part_profile.get("partsapi_cat_candidates") or []),
    ]
    terms = {_normalize_compare_value(value) for value in specific_names if _normalize_compare_value(value)}
    specific_words = [set(re.findall(r"[0-9a-zа-яё]{4,}", str(value or "").casefold())) for value in specific_names]
    for value in part_profile.get("catalog_search_terms") or []:
        words = set(re.findall(r"[0-9a-zа-яё]{4,}", str(value or "").casefold()))
        if any(len(words & reference) >= 2 for reference in specific_words):
            terms.add(_normalize_compare_value(value))
    matches = [node for node in nodes.values() if _normalize_compare_value(node["title"]) in terms]
    if matches:
        deepest = min(node["depth"] for node in matches)
        matches = [node for node in matches if node["depth"] == deepest]
    selected = matches[0] if len(matches) == 1 else None
    return {
        "category": selected["id"] if selected else None,
        "category_kind": "tecdoc_tree_node_id",
        "category_mode": "exact_tree_name" if selected else "unresolved",
        "category_queryable": bool(selected),
        "category_unresolved": not bool(selected),
        "candidate_node_ids": sorted(node["id"] for node in matches)[:10],
        "reason": None
        if selected
        else ("Several equally named tree nodes match." if matches else "No exact product-tree node matched the part."),
    }


def _rank_article_candidate(
    candidate: dict[str, Any], *, index: int, part_profile: dict[str, Any], category: str
) -> dict[str, Any]:
    position = _candidate_position_assessment(candidate, part_profile)
    blockers = ["vin_fitment_not_confirmed", "oem_number_not_confirmed"]
    if position["position_match"] in {"conflict", "ambiguous", "not_proved"}:
        blockers.append(f"candidate_position_{position['position_match']}")
    number = _compact(candidate.get("part_number"))
    brand = _compact(candidate.get("brand"))
    return {
        "candidate_id": f"tec-{index}-{_hash_candidate(brand, number, candidate.get('article_id'))}",
        "part_number": number or None,
        "article_id": candidate.get("article_id"),
        "brand": brand or None,
        "name": candidate.get("product_name"),
        "source": candidate.get("provider") or "partsapi_ru",
        "source_operation": "articles",
        "tecdoc_tree_node_id": category,
        "candidate_kind": "tecdoc_aftermarket_article",
        "fitment_scope": "vehicle_modification_catalog_candidate",
        "vin_fitment_confirmed": False,
        "oem_number_confirmed": False,
        "position_match": position["position_match"],
        "requested_position_coordinates": position["requested_coordinates"],
        "candidate_position_hints": position["candidate_coordinate_hints"],
        "quantity_basis": part_profile.get("quantity_basis"),
        "confidence_label": "low",
        "confidence_score": min(float(candidate.get("confidence") or 0.5), 0.6),
        "blocking_reasons": blockers,
        "manual_review_required": True,
        "fitment_evidence": {"fitment_confirmed": False},
    }


def _is_parser_gap(call: dict[str, Any] | None, expected_field: str) -> bool:
    if not call or call.get("dry_run"):
        return False
    if call.get("failure_class") == "adapter_unparsed_response" or call.get("outcome") == "unparsed_response":
        return True
    return bool(call.get("ok") and call.get("empty_payload") is False and not call.get(expected_field))


def _tecdoc_lookup_outcome(call: dict[str, Any] | None) -> dict[str, Any]:
    if call is None:
        return {"outcome": "not_attempted", "provider_outcome": None, "retryable": False}
    if call.get("dry_run"):
        return {"outcome": "not_run", "provider_outcome": call.get("outcome"), "retryable": False}
    if _is_parser_gap(call, "article_candidates"):
        return {
            "outcome": "parser_gap",
            "provider_outcome": call.get("outcome"),
            "failure_class": call.get("failure_class") or "adapter_unparsed_response",
            "retryable": False,
        }
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
        "outcome": "candidates_found" if call.get("article_candidates") else "response_received",
        "provider_outcome": call.get("outcome") or "success",
        "retryable": False,
    }


def _status(
    readiness: dict[str, Any],
    *,
    candidates: list[dict[str, Any]],
    live_partsapi_oem: bool,
    lookup: dict[str, Any],
    identity_call: dict[str, Any] | None,
    search_call: dict[str, Any] | None,
    article_call: dict[str, Any] | None,
) -> str:
    if not readiness["has_identifier"]:
        return "needs_vin_or_frame"
    if not readiness["vin_decode_supported"]:
        return "unsupported_identifier_for_vin_decode"
    if not readiness["ready_for_identity_crosscheck"]:
        return "needs_identity_confirmation"
    if not readiness["ready_for_category_lookup"]:
        return "needs_part_clarification"
    if _is_parser_gap(identity_call, "vehicle_profiles"):
        return "tecdoc_parser_gap"
    if identity_call and not identity_call.get("dry_run") and not identity_call.get("ok"):
        return "tecdoc_lookup_provider_failed"
    if not readiness["has_tecdoc_car_id"]:
        return (
            "needs_vehicle_modification"
            if identity_call and not identity_call.get("dry_run")
            else "ready_for_live_tecdoc_lookup"
        )
    if not readiness["ready_for_tecdoc_identity"]:
        return "needs_identity_confirmation"
    if not readiness["vehicle_type_known"]:
        return "needs_vehicle_type"
    if _is_parser_gap(search_call, "search_tree_rows"):
        return "tecdoc_parser_gap"
    if search_call and not search_call.get("dry_run") and not search_call.get("ok"):
        return "tecdoc_lookup_provider_failed"
    if search_call is None or search_call.get("dry_run"):
        return "ready_for_live_tecdoc_lookup"
    if readiness["search_tree_empty"]:
        return "no_tecdoc_tree_nodes_needs_manual_lookup"
    if readiness["needs_tecdoc_tree_node"]:
        return "needs_tecdoc_tree_node"
    if article_call is None or article_call.get("dry_run"):
        return "ready_for_live_tecdoc_lookup"
    if _is_parser_gap(article_call, "article_candidates"):
        return "tecdoc_parser_gap"
    if candidates:
        return "tecdoc_articles_found_needs_manual_fitment"
    if live_partsapi_oem:
        if lookup["outcome"] == "provider_failed":
            return "tecdoc_lookup_provider_failed"
        return "no_tecdoc_articles_found_needs_manual_lookup"
    return "ready_for_live_tecdoc_lookup"


def _resolution_manual_actions(
    *,
    existing: list[dict[str, Any]],
    raw_identifier: str,
    part_profile: dict[str, Any],
    identity_conflict: bool,
    vin_supported: bool,
    identity_call: dict[str, Any] | None,
    car_id: str | None,
    can_read_catalog: bool,
    vehicle_type_known: bool,
    search_call: dict[str, Any] | None,
    needs_tree_node: bool,
    category_resolution: dict[str, Any],
    article_call: dict[str, Any] | None,
    article_candidates: list[dict[str, Any]],
    blockers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    actions = list(existing)
    if not raw_identifier:
        actions.append(_manual_action("request_identifier", "Указать полный VIN для поиска TecDoc."))
    if not part_profile.get("recognized"):
        actions.append(_manual_action("clarify_part", "Уточнить конкретную деталь."))
    elif part_profile.get("clarification_required"):
        actions.append(
            _manual_action("clarify_part_position", fields=list(part_profile.get("clarification_fields") or []))
        )
    if identity_conflict:
        actions.append(
            _manual_action(
                "confirm_identity", "Сверить VIN и модификацию автомобиля перед поиском деталей.", priority=2
            )
        )
    elif vin_supported and identity_call and identity_call.get("dry_run"):
        actions.append(_manual_action("run_live_vin_decode", "Получить carId через VINdecode."))
    elif vin_supported and identity_call and identity_call.get("ok") and not car_id:
        actions.append(
            _manual_action(
                "select_tecdoc_vehicle_modification", "VINdecode не дал однозначный carId; выбрать модификацию вручную."
            )
        )
    elif car_id and not can_read_catalog:
        actions.append(
            _manual_action(
                "confirm_identity", "Сверить VINdecode с данными автомобиля перед запросом TecDoc.", priority=2
            )
        )
    if car_id and not vehicle_type_known:
        actions.append(
            _manual_action("confirm_vehicle_type", "Указать carType: PC, CV или Motorcycle перед запросом TecDoc.")
        )
    if search_call and search_call.get("dry_run"):
        actions.append(_manual_action("run_live_tecdoc_search_tree", "Получить дерево getSearchTree."))
    elif search_call and search_call.get("ok") and search_call.get("empty_payload") is True:
        actions.append(
            _manual_action("manual_catalog_fallback", "Дерево getSearchTree пусто; проверить каталог вручную.")
        )
    elif needs_tree_node:
        actions.append(
            _manual_action(
                "select_tecdoc_tree_node",
                "Выбрать точный strId из дерева getSearchTree для запрошенной детали.",
                candidate_node_ids=category_resolution["candidate_node_ids"],
            )
        )
    if article_call and article_call.get("dry_run"):
        actions.append(_manual_action("run_live_tecdoc_articles", "Получить артикулы getArticles."))
    if article_candidates:
        actions.append(
            _manual_action("review_tecdoc_articles", "Проверить применимость артикула, положение и количество вручную.")
        )
        actions.append(
            _manual_action("confirm_oem_in_epc", "Проверить оригинальный номер и VIN-применимость в брендовом EPC.")
        )
    elif (
        article_call
        and not article_call.get("dry_run")
        and article_call.get("ok")
        and not _is_parser_gap(article_call, "article_candidates")
    ):
        actions.append(
            _manual_action(
                "manual_catalog_fallback", "По выбранной группе артикулы не найдены; проверить каталог вручную."
            )
        )
    if any(blocker.get("stage") == "partsapi_payload_parse" for blocker in blockers):
        actions.append(
            _manual_action(
                "inspect_provider_response",
                "Ответ PartsAPI не распознан; проверить формат без вывода сырого ответа в отчёт.",
            )
        )
    if any(blocker.get("stage") == "partsapi_tecdoc_lookup" for blocker in blockers):
        actions.append(
            _manual_action("retry_or_manual_catalog", "Проверить сбой провайдера и при необходимости искать вручную.")
        )

    return actions


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
    tecdoc_tree_node_id: str | int | None = None,
    vehicle_type: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Research TecDoc articles using the VINdecode -> tree -> articles chain.

    A TecDoc article is aftermarket catalog evidence. Neither the VIN decode
    nor a modification-linked article proves the exact OEM number or VIN fitment.
    """

    _ = partsapi_category_index  # Retained for older callers; getArticles uses a tree strId.
    raw_identifier = _compact(identifier)
    classification = classify_identifier(raw_identifier)
    vin_supported = classification.kind == "vin"
    part_text = _compact(requested_part)
    requested_vehicle_type = _canonical_vehicle_type(vehicle_type)
    part_profile = normalize_part_intent(part_text, axle=axle, side=side, position=position, inner_outer=inner_outer)
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
    identity: dict[str, Any] = (
        decode_vehicle_identity(
            raw_identifier,
            crm_context=context,
            model_year=model_year,
            make_hint=make,
            live_vpic=live_vpic and not dry_run,
            live_wmi=live_vpic and not dry_run,
        )
        if raw_identifier
        else {
            "confidence": 0.0,
            "confidence_label": "low",
            "vehicle_profile": {},
            "parts_lookup_readiness": {
                "ready_for_oem_lookup": False,
                "ready_for_oem_candidate_lookup": False,
                "ready_for_crm_writeback": False,
                "blocking_reasons": ["missing_identifier"],
            },
            "conflicts": [],
            "warnings": [],
        }
    )

    calls: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    manual_actions: list[dict[str, Any]] = []
    live_calls_used = 0

    def partsapi_call(operation: str, *, live_allowed: bool, **kwargs: Any) -> dict[str, Any]:
        nonlocal live_calls_used
        remaining = max(0, int(max_live_calls) - live_calls_used)
        call_is_live = bool(live_allowed and not dry_run and remaining > 0)
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
            max_attempts=min(max(1, int(max_attempts)), remaining) if call_is_live else 1,
            dry_run=not call_is_live,
            **kwargs,
        )
        # An input/credential rejection did not consume an external request.
        if call_is_live:
            live_calls_used += max(0, int(call.get("attempt_count") or 0))
        calls.append(call)
        return call

    identity_call: dict[str, Any] | None = None
    if vin_supported:
        identity_call = partsapi_call(
            "vin_decode",
            live_allowed=live_partsapi_identity or live_partsapi_oem,
            identifier=classification.normalized,
        )
        identity = _identity_with_partsapi_agreement(identity, identity_call)
    elif raw_identifier:
        manual_actions.append(
            _manual_action(
                "confirm_identifier", "VINdecode принимает полный 17-значный VIN; для frame нужен другой источник."
            )
        )

    identity_readiness = identity.get("parts_lookup_readiness") or {}
    agreement = identity_readiness.get("cross_source_agreement") or {}
    identity_conflict = agreement.get("status") in {
        "conflict",
        "identifier_mismatch",
        "ambiguous_vehicle_modification",
    } or any(item.get("severity") == "high" for item in identity.get("conflicts") or [])
    can_read_catalog = bool(identity_readiness.get("ready_for_tecdoc_candidate_lookup")) and not identity_conflict
    part_actionable = bool(part_profile.get("recognized")) and not bool(part_profile.get("clarification_required"))
    vehicle_profile = _partsapi_vehicle_profile(identity_call or {})
    provider_vehicle_type = _canonical_vehicle_type(vehicle_profile.get("vehicle_type"))
    normalized_vehicle_type = requested_vehicle_type or provider_vehicle_type
    vehicle_type_source = "caller" if requested_vehicle_type else "vin_decode" if provider_vehicle_type else "unknown"
    vehicle_type_conflict = bool(
        requested_vehicle_type and provider_vehicle_type and requested_vehicle_type != provider_vehicle_type
    )
    car_id = (
        _positive_tecdoc_id(vehicle_profile.get("tecdoc_car_id"))
        if identity_call
        and identity_call.get("ok")
        and not identity_call.get("dry_run")
        and not identity_conflict
        and vehicle_profile.get("identifier_matches_request") is not False
        else None
    )
    vehicle_type_known = normalized_vehicle_type in {"PC", "CV", "Motorcycle"} and not vehicle_type_conflict
    if not vehicle_type_known:
        blockers.append(
            {
                "stage": "partsapi_vehicle_type",
                "operation": "search_tree",
                "error": (
                    "Caller vehicle_type conflicts with VINdecode."
                    if vehicle_type_conflict
                    else "Confirm carType as PC, CV or Motorcycle before getSearchTree."
                ),
            }
        )

    search_call: dict[str, Any] | None = None
    tree_rows: list[dict[str, Any]] = []
    if car_id and can_read_catalog and part_actionable and vehicle_type_known:
        search_call = partsapi_call(
            "search_tree",
            live_allowed=live_partsapi_oem,
            type_id=car_id,
            vehicle_type=normalized_vehicle_type,
        )
        tree_rows = [row for row in search_call.get("search_tree_rows") or [] if isinstance(row, dict)]
    category_resolution = _tree_node_resolution(tree_rows, part_profile, part_text, tecdoc_tree_node_id)
    selected_node_id: str | None = (
        _positive_tecdoc_id(category_resolution.get("category")) if search_call and search_call.get("ok") else None
    )
    article_call: dict[str, Any] | None = None
    if selected_node_id and car_id and can_read_catalog and part_actionable and vehicle_type_known:
        article_call = partsapi_call(
            "articles",
            live_allowed=live_partsapi_oem,
            type_id=car_id,
            category=selected_node_id,
            vehicle_type=normalized_vehicle_type,
        )
    raw_articles = (
        [item for item in (article_call or {}).get("article_candidates") or [] if isinstance(item, dict)]
        if article_call and article_call.get("ok") and not article_call.get("dry_run")
        else []
    )
    article_candidates = (
        [
            _rank_article_candidate(candidate, index=index, part_profile=part_profile, category=selected_node_id)
            for index, candidate in enumerate(raw_articles[: max(0, max_candidates)], start=1)
        ]
        if selected_node_id is not None
        else []
    )
    lookup = _tecdoc_lookup_outcome(article_call)
    for operation, call, expected in (
        ("vin_decode", identity_call, "vehicle_profiles"),
        ("search_tree", search_call, "search_tree_rows"),
        ("articles", article_call, "article_candidates"),
    ):
        if _is_parser_gap(call, expected):
            blockers.append(
                {
                    "stage": "partsapi_payload_parse",
                    "operation": operation,
                    "failure_class": "adapter_unparsed_response",
                    "response_shape": call.get("response_shape") if call else None,
                }
            )
            continue
        if call and not call.get("dry_run") and not call.get("ok"):
            blockers.append(
                {
                    "stage": "partsapi_tecdoc_lookup",
                    "operation": operation,
                    "outcome": call.get("outcome") or call.get("failure_class") or "provider_failed",
                    "failure_class": call.get("failure_class"),
                    "retryable": bool(call.get("retryable")),
                }
            )

    needs_tree_node = bool(
        car_id
        and search_call
        and search_call.get("ok")
        and not search_call.get("dry_run")
        and not selected_node_id
        and search_call.get("empty_payload") is not True
        and not _is_parser_gap(search_call, "search_tree_rows")
    )
    readiness: dict[str, Any] = {
        "has_identifier": bool(raw_identifier),
        "vin_decode_supported": vin_supported,
        "ready_for_identity_crosscheck": bool(raw_identifier) and not identity_conflict,
        "ready_for_category_lookup": part_actionable,
        "ready_for_tecdoc_identity": can_read_catalog,
        "has_tecdoc_car_id": bool(car_id),
        "vehicle_type_known": vehicle_type_known,
        "search_tree_empty": bool(search_call and search_call.get("ok") and search_call.get("empty_payload") is True),
        "needs_tecdoc_tree_node": needs_tree_node,
        "needs_partsapi_category_mapping": needs_tree_node,  # Legacy probe field; now a tree strId.
        "ready_for_tecdoc_candidate_lookup": bool(can_read_catalog and part_actionable and car_id and selected_node_id),
        "ready_for_oem_candidate_lookup": False,
        "ready_for_applicability_enrichment": False,
        "ready_for_crm_writeback": False,
        "blocking_reasons": list(identity_readiness.get("blocking_reasons") or []),
    }
    manual_actions = _resolution_manual_actions(
        existing=manual_actions,
        raw_identifier=raw_identifier,
        part_profile=part_profile,
        identity_conflict=identity_conflict,
        vin_supported=vin_supported,
        identity_call=identity_call,
        car_id=car_id,
        can_read_catalog=can_read_catalog,
        vehicle_type_known=vehicle_type_known,
        search_call=search_call,
        needs_tree_node=needs_tree_node,
        category_resolution=category_resolution,
        article_call=article_call,
        article_candidates=article_candidates,
        blockers=blockers,
    )

    current_status = _status(
        readiness,
        candidates=article_candidates,
        live_partsapi_oem=live_partsapi_oem and not dry_run,
        lookup=lookup,
        identity_call=identity_call,
        search_call=search_call,
        article_call=article_call,
    )
    result = {
        "ok": True,
        "schema": "VinOemResolution",  # Existing MCP consumer contract.
        "mode": "read_only_tecdoc_catalog_research",
        "status": current_status,
        "identifier": {
            "redacted": _redact_identifier(raw_identifier),
            "kind": classification.kind,
            "market_hint": classification.market_hint,
            "raw_identifier_is_sensitive": True,
        },
        "identity": _identity_digest(identity),
        "part_intent": part_profile,
        "tecdoc_vehicle": {
            "car_id": car_id,
            "vehicle_type": normalized_vehicle_type,
            "vehicle_type_source": vehicle_type_source,
            "profile_count": len((identity_call or {}).get("vehicle_profiles") or []),
            "identity_agreement": agreement.get("status"),
        },
        "category_resolution": category_resolution,
        "readiness": readiness,
        "article_candidates": article_candidates,
        "article_candidate_count": len(article_candidates),
        "oem_candidates": [],
        "candidate_count": 0,
        "tecdoc_lookup_outcome": lookup,
        "oem_lookup_outcome": {"outcome": "not_available_from_current_partsapi_methods"},
        "enrichment": {"applicability_evidence": [], "article_enrichment": [], "cross_candidates": []},
        "calls": [_call_digest(call) for call in calls],
        "call_count": len(calls),
        "live_call_count": live_calls_used,
        "max_live_calls": max_live_calls,
        "blockers": blockers,
        "manual_actions": manual_actions,
        "crm_writeback_gate": {
            "can_write_final_material_line_now": False,
            "can_prepare_manual_writeback": False,
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
    return _redact_sensitive_output(result, raw_identifier)
