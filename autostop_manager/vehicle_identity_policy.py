"""Pure lookup policy shared by E4 and its catalog consumers."""

from __future__ import annotations

from typing import Any


def _scope(scope: str) -> str:
    if scope not in {"vehicle", "modification", "exact", "family"}:
        raise ValueError("unsupported_identity_scope")
    return "family" if scope == "family" else "vehicle"


def identity_has_blocking_conflicts(identity: dict[str, Any], scope: str = "vehicle") -> bool:
    """Honor explicit scopes; preserve conservative handling of legacy conflicts."""
    scope = _scope(scope)
    if identity.get("ok") is False or identity.get("errors"):
        return True
    validation = identity.get("identifier_validation")
    if isinstance(validation, dict) and validation.get("ok") is False:
        return True
    if scope == "vehicle" and isinstance(validation, dict) and validation.get("valid_for_vehicle_lookup") is False:
        return True
    for conflict in identity.get("conflicts") or []:
        if not isinstance(conflict, dict):
            continue
        scopes = conflict.get("blocking_scopes")
        if isinstance(scopes, list):
            if scope in scopes:
                return True
        elif scope == "vehicle" or conflict.get("severity") == "high":
            return True
    if scope == "vehicle":
        statuses = identity.get("field_statuses") or {}
        if isinstance(statuses, dict) and any(
            isinstance(item, dict) and item.get("status") == "disputed" for item in statuses.values()
        ):
            return True
    return False


def identity_allows_lookup(identity: dict[str, Any], scope: str = "vehicle") -> bool:
    """Permit bounded family research without upgrading disputed vehicle facts."""
    scope = _scope(scope)
    if identity_has_blocking_conflicts(identity, scope):
        return False
    readiness = identity.get("parts_lookup_readiness") or {}
    key = "ready_for_family_lookup" if scope == "family" else "ready_for_vehicle_lookup"
    if key in readiness:
        return bool(readiness[key])
    if "field_statuses" in identity:
        return bool(build_parts_lookup_readiness(identity)[key])
    # Older producer payloads remain research-only; no write permission is inferred.
    if scope == "family":
        profile = identity.get("vehicle_profile") or {}
        return bool(profile.get("make") and (profile.get("model") or profile.get("model_family")))
    return bool(readiness.get("ready_for_oem_candidate_lookup", readiness.get("ready_for_oem_lookup")))


def build_parts_lookup_readiness(identity: dict[str, Any]) -> dict[str, Any]:
    """Compute stage readiness from facts and diagnostics, never from writeback fallbacks."""
    profile = identity.get("vehicle_profile") or {}
    families = identity.get("family_candidates") or []
    family_ready = bool(families) and not identity_has_blocking_conflicts(identity, "family")
    vehicle_ready = bool(
        family_ready
        and profile.get("make")
        and profile.get("model")
        and identity.get("confidence_label") == "high"
        and not identity_has_blocking_conflicts(identity, "vehicle")
    )
    statuses = identity.get("field_statuses") or {}
    modification_ready = bool(
        vehicle_ready
        and all(
            (statuses.get(field) or {}).get("status") == "supported"
            for field in ("model_year", "engine", "transmission", "market")
        )
    )
    blocking = []
    if identity_has_blocking_conflicts(identity, "vehicle"):
        blocking.append("unresolved_vehicle_identity")
    if not family_ready:
        blocking.append("vehicle_family_unresolved")
    if identity.get("confidence_label") != "high":
        blocking.append("identity_confidence_below_high")
    if any(item.get("severity") == "high" for item in identity.get("conflicts") or []):
        blocking.append("high_severity_identity_conflict")
    return {
        "ready_for_family_lookup": family_ready,
        "ready_for_vehicle_lookup": vehicle_ready,
        "ready_for_modification_lookup": modification_ready,
        "ready_for_oem_lookup": vehicle_ready,
        "ready_for_oem_candidate_lookup": vehicle_ready,
        "ready_for_tecdoc_candidate_lookup": False,
        "ready_for_crm_writeback": False,
        "exact_applicability_confirmed": False,
        "lookup_scope": "vehicle" if vehicle_ready else "family" if family_ready else "blocked",
        "identity_is_lead": True,
        "epc_confirmation_required": True,
        "cross_source_agreement": {
            "status": "not_checked",
            "sources": ["NHTSA vPIC", "PartsAPI VINdecode"],
            "matched_fields": [],
            "conflicting_fields": [],
        },
        "blocking_reasons": blocking,
        "reason": "Read-only family or vehicle research; exact fitment and writeback require separate evidence.",
    }
