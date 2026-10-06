"""Independent E4 sources and a pure, source-aware identity reconciler.

Observations are transient caller-supplied evidence, not authenticated attestations.
No decoder dispatch, dependency installation or database refresh happens in reconciliation.
"""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
from importlib.resources import files
import json
import math
import re
from typing import Any

from . import vehicle_identity as legacy
from . import vin_lookup
from .vehicle_identity_inputs import validate_identity_input
from .vehicle_identity_policy import build_parts_lookup_readiness
from .vehicle_identity_transport import wmi_for_vin


OBSERVATION_SCHEMA = "VehicleIdentityObservation"
SOURCE_TOOLS = {
    "decode_vin_vpic": ("nhtsa_vpic", "provider"),
    "decode_wmi_vpic": ("nhtsa_vpic", "provider"),
    "decode_wmi_local": ("autostop_wmi_hints", "local_hint"),
    "vininfo_decode": ("vininfo", "local_rule"),
    "corgi_decode": ("nhtsa_vpic", "provider"),
    "vin_brand_details": ("autostop_local_rules", "local_rule"),
    "decode_frame_local": ("autostop_local_rules", "local_rule"),
}
_WMI = re.compile(r"^[A-HJ-NPR-Z0-9]{3}(?:[A-HJ-NPR-Z0-9]{3})?$")
_NUMERIC_FIELDS = {
    "model_year",
    "production_year",
    "transmission_speeds",
    "engine_cylinders",
    "engine_displacement_l",
    "engine_power_hp",
    "engine_power_kw",
    "power_kw",
    "power_hp",
    "displacement_cc",
}
_FIELDS = (
    frozenset(legacy.IDENTITY_FIELDS)
    | _NUMERIC_FIELDS
    | {
        "model_family",
        "platform",
        "manufacturer",
        "manufacturer_country",
        "vehicle_type",
        "body_class",
        "plant_country",
        "plant_city",
        "fuel_type",
        "transmission_code",
        "series2",
    }
)
_EXACT_FIELDS = frozenset(legacy.IDENTITY_FIELDS) - {"make", "model", "market"}
_PARTIAL_FIELDS = {"make", "model", "model_family", "platform", "manufacturer", "manufacturer_country", "vehicle_type"}
_VIN_IN_TEXT = re.compile(r"(?<![A-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9])", re.IGNORECASE)


def _redact_output(value: Any, identifier: str) -> Any:
    value = vin_lookup._redact_identifier_payload(value, identifier)
    if isinstance(value, dict):
        return {key: _redact_output(item, identifier) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_output(item, identifier) for item in value]
    if isinstance(value, str):
        return _VIN_IN_TEXT.sub("[REDACTED_VIN]", value)
    return value


def _canonical_identifier(identifier: str, scope: str) -> str:
    if scope == "wmi":
        return vin_lookup.normalize_vin(identifier)
    classification = vin_lookup.classify_identifier(identifier)
    normalized = classification.normalized
    return normalized.replace("-", "") if scope == "frame" else normalized


def _scope(identifier: str, tool: str, identifier_type: str = "auto") -> str:
    if tool in {"decode_wmi_local", "decode_wmi_vpic"}:
        return "wmi"
    if tool == "decode_frame_local":
        return "frame"
    kind = vin_lookup.classify_identifier(identifier, identifier_type=identifier_type).kind
    if kind in {"frame_number", "market_code"}:
        return "frame"
    return "partial" if kind == "vin_partial" else "vin"


def _digest(identifier: str, scope: str, hints: dict[str, Any] | None = None) -> str:
    canonical = _canonical_identifier(identifier, scope)
    hint_json = json.dumps(hints or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"e4-observation-v1:{scope}:{canonical}:{hint_json}".encode()).hexdigest()


def _safe_fields(fields: Any) -> bool:
    if not isinstance(fields, dict) or any(key not in _FIELDS for key in fields):
        return False
    for key, value in fields.items():
        if key == "options":
            if not isinstance(value, list) or len(value) > 100 or any(not isinstance(item, str) for item in value):
                return False
        elif key in _NUMERIC_FIELDS:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                return False
            try:
                number = float(value)
                if not math.isfinite(number) or number < 0:
                    return False
                if key in {"model_year", "production_year"} and (not number.is_integer() or not 1886 <= number <= 2100):
                    return False
            except (TypeError, ValueError, OverflowError):
                return False
        elif not isinstance(value, str) or len(value) > 4096:
            return False
    return True


def make_observation(
    tool: str,
    identifier: str,
    *,
    fields: dict[str, Any] | None = None,
    outcome: str = "success",
    diagnostics: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    model_year: int | None = None,
    version: str = "1",
    field_candidates: dict[str, list[Any]] | None = None,
    identifier_type: str = "auto",
) -> dict[str, Any]:
    """Build redacted source evidence; shared adapter entry point, without I/O."""
    if tool not in SOURCE_TOOLS:
        raise ValueError("unsupported_e4_source")
    identifier = identifier if isinstance(identifier, str) else ""
    scope = _scope(identifier, tool, identifier_type)
    origin, _kind = SOURCE_TOOLS[tool]
    profile = dict(fields or {})
    diagnostics = deepcopy(diagnostics or {})
    if not _safe_fields(profile):
        profile, outcome = {}, "adapter_malformed_payload"
    evidence: list[dict[str, Any]] = []
    for field, values in {**{key: [value] for key, value in profile.items()}, **(field_candidates or {})}.items():
        if not isinstance(values, list):
            profile, evidence, outcome = {}, [], "adapter_malformed_payload"
            break
        for value in values:
            if not _safe_fields({field: value}):
                profile, evidence, outcome = {}, [], "adapter_malformed_payload"
                break
            dependencies = (
                ["caller.model_year"]
                if field == "model_year" and model_year is not None and str(value) == str(model_year)
                else []
            )
            evidence.append({"field": field, "value": value, "depends_on": dependencies, "strength": "candidate"})
        if len(values) != 1:
            profile.pop(field, None)
        elif outcome in {"success", "partial"}:
            profile[field] = values[0]
    diagnostics["model_year_hint_requested"] = model_year
    hints = {"model_year": model_year} if model_year is not None else {}
    result = {
        "schema": OBSERVATION_SCHEMA,
        "schema_version": 1,
        "tool": tool,
        "ok": outcome in {"success", "partial"},
        "outcome": outcome,
        "identifier": {
            "kind": scope,
            "redacted": vin_lookup._redact_identifier(identifier),
        },
        "input_binding": {"scope": scope, "digest": _digest(identifier, scope, hints), "hints": hints},
        "source": {"id": tool, "data_origin_id": origin, "version": str(version)},
        "vehicle_profile": profile,
        "field_evidence": evidence,
        "diagnostics": diagnostics,
        "warnings": list(warnings or []),
        "errors": [] if outcome in {"success", "partial"} else [{"code": outcome, "stage": "source_decode"}],
        "missing_fields": [field for field in legacy.IDENTITY_FIELDS if field not in profile],
        "alternatives": {
            field: values
            for field, values in (field_candidates or {}).items()
            if isinstance(values, list) and len(values) > 1
        },
    }
    return _redact_output(result, identifier)


def inspect_vehicle_identifier(identifier: str, *, identifier_type: str = "auto") -> dict[str, Any]:
    validated = validate_identity_input(identifier, identifier_type=identifier_type)
    if not validated["ok"]:
        return legacy.invalid_identity_result(validated["errors"])
    classification = vin_lookup.classify_identifier(validated["identifier"], identifier_type=identifier_type)
    diagnostics = {
        "model_year": legacy._vin_model_year(classification.normalized),
        "check_digit": legacy._check_digit(classification.normalized),
        "frame_query_hint": legacy._frame_query_hint(classification.normalized),
    }
    result = {
        "ok": classification.kind != "unknown",
        "status": "ok" if classification.kind != "unknown" else "invalid_input",
        "identifier": vin_lookup._public_identifier(classification),
        "diagnostics": diagnostics,
        "warnings": list(classification.notes),
        "errors": [] if classification.kind != "unknown" else [{"code": "identifier_unrecognized"}],
    }
    return vin_lookup._redact_identifier_payload(result, classification.normalized)


def decode_vin_vpic(
    identifier: str, *, model_year: int | None = None, identifier_type: str = "auto", timeout: float = 8.0
) -> dict[str, Any]:
    checked = validate_identity_input(identifier, model_year=model_year, identifier_type=identifier_type)
    classification = vin_lookup.classify_identifier(identifier, identifier_type=identifier_type)
    if not checked["ok"] or classification.kind not in {"vin", "vin_partial"}:
        return make_observation(
            "decode_vin_vpic", identifier, outcome="invalid_input" if not checked["ok"] else "unsupported"
        )
    from .vehicle_identity_transport import IdentityBudget, collect_source_provider_results

    collected = collect_source_provider_results(
        [checked],
        source="vin",
        use_vpic_batch=False,
        budget=IdentityBudget(max_attempts=2, deadline_seconds=min(30.0, timeout * 2)),
    )
    observation = vpic_observation(
        identifier,
        collected["results"][0],
        model_year=checked["context"].get("model_year"),
        identifier_type=identifier_type,
    )
    observation["processing"] = collected["processing"]
    return observation


def vpic_observation(
    identifier: str,
    result: dict[str, Any] | None,
    *,
    model_year: int | None = None,
    wmi: bool = False,
    identifier_type: str = "auto",
) -> dict[str, Any]:
    """Convert one transport-bound result without making another request."""
    tool = "decode_wmi_vpic" if wmi else "decode_vin_vpic"
    result = result or {"ok": False, "outcome": "provider_failed"}
    profile: dict[str, Any] = {}
    evidence: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    warnings: list[str] = []
    if wmi:
        result = legacy._bound_wmi_result(result, _valid_wmi(identifier)) or {}
        legacy._merge_wmi_result(
            result, wmi=identifier, profile=profile, field_evidence=evidence, evidence_sources=sources
        )
    else:
        classification = vin_lookup.classify_identifier(identifier, identifier_type=identifier_type)
        result = (
            legacy._bound_vpic_result(result, classification.normalized, partial=classification.kind == "vin_partial")
            or {}
        )
        legacy._merge_vpic_result(
            result,
            identifier_kind=classification.kind,
            profile=profile,
            field_evidence=evidence,
            evidence_sources=sources,
            warnings=warnings,
        )
    diagnostics = {
        "provider_clean": legacy._vpic_has_clean_diagnostics(result),
        "identifier_verified": bool((result.get("identifier_binding") or {}).get("verified")),
        "provider_outcome": result.get("outcome"),
        "error_code": result.get("error_code"),
        "identifier_binding": result.get("identifier_binding"),
    }
    if wmi and (result.get("identifier_binding") or {}).get("status") == "missing":
        diagnostics["unverified_provider_fields"] = result.get("unverified_wmi_profile") or {}
    descriptor = profile.pop("vehicle_descriptor", None)
    if descriptor:
        diagnostics["vehicle_descriptor"] = descriptor
    return make_observation(
        tool,
        identifier,
        fields=profile,
        outcome=result.get("outcome", "provider_failed"),
        diagnostics=diagnostics,
        warnings=warnings,
        model_year=model_year,
        identifier_type=identifier_type,
    )


def _valid_wmi(wmi: Any) -> str:
    if not isinstance(wmi, str):
        return ""
    normalized = vin_lookup.normalize_vin(wmi)
    return normalized if _WMI.fullmatch(normalized) else ""


def decode_wmi_vpic(wmi: str, *, timeout: float = 8.0) -> dict[str, Any]:
    normalized = _valid_wmi(wmi)
    if not normalized:
        return make_observation("decode_wmi_vpic", wmi, outcome="invalid_input")
    from .vehicle_identity_transport import IdentityBudget, collect_source_provider_results

    collected = collect_source_provider_results(
        [{"ok": True, "identifier": normalized, "context": {}}],
        source="wmi",
        budget=IdentityBudget(max_attempts=2, deadline_seconds=min(30.0, timeout * 2)),
    )
    observation = vpic_observation(normalized, collected["results"][0], wmi=True)
    observation["processing"] = collected["processing"]
    return observation


def decode_wmi_local(wmi: str) -> dict[str, Any]:
    normalized = _valid_wmi(wmi)
    if not normalized:
        return make_observation("decode_wmi_local", wmi, outcome="invalid_input")
    profile: dict[str, Any] = {}
    hint = legacy._merge_local_wmi_hint(normalized[:3], profile, [], [])
    return make_observation(
        "decode_wmi_local",
        normalized,
        fields=profile,
        outcome="success" if hint else "not_found",
        diagnostics={"prefix_only": len(normalized) == 6},
    )


@lru_cache(maxsize=1)
def _rules() -> list[dict[str, Any]]:
    rules = [
        {
            "id": rule.rule_id,
            "pattern": rule.pattern,
            "kind": rule.kind,
            "fields": rule.fields,
            "source_url": "",
            "source_commit": None,
            "license": "project",
            "market": None,
            "year_from": None,
            "year_to": None,
            "verification": "legacy_unverified",
            "validation": "legacy_unverified",
            "version": "legacy-v1",
            "data_origin_id": "autostop_local_rules",
        }
        for rule in legacy.PLATFORM_RULES
    ]
    additional = json.loads(
        files("autostop_manager").joinpath("resources/e4/brand_rules/initial.json").read_text(encoding="utf-8")
    )
    return rules + additional["rules"]


def _rule_matches(rule: dict[str, Any], identifier: str, model_year: int | None, market: str | None) -> bool:
    if not re.match(rule["pattern"], identifier, re.IGNORECASE):
        return False
    if model_year is not None and (
        (rule.get("year_from") and model_year < rule["year_from"])
        or (rule.get("year_to") and model_year > rule["year_to"])
    ):
        return False
    aliases = {"eu": "europe", "european": "europe", "jdm": "japan", "us": "north america"}
    canonical_market = aliases.get(market.casefold(), market.casefold()) if market else ""
    return not (canonical_market and rule.get("market") and canonical_market not in str(rule["market"]).casefold())


def _decode_rules(
    tool: str,
    identifier: str,
    *,
    frame: bool,
    model_year: int | None = None,
    market: str | None = None,
    make_hint: str | None = None,
    identifier_type: str = "auto",
) -> dict[str, Any]:
    checked = validate_identity_input(
        identifier, model_year=model_year, make_hint=make_hint, identifier_type=identifier_type
    )
    if not checked["ok"] or (market is not None and not isinstance(market, str)):
        return make_observation(tool, identifier, outcome="invalid_input")
    classification = vin_lookup.classify_identifier(identifier, identifier_type=identifier_type)
    allowed = {"frame_number", "market_code"} if frame else {"vin", "vin_partial"}
    if classification.kind not in allowed:
        return make_observation(tool, identifier, outcome="unsupported")
    matched = [
        rule
        for rule in _rules()
        if rule["kind"].endswith("frame") == frame
        and _rule_matches(rule, classification.normalized, checked["context"].get("model_year"), market)
        and (not make_hint or legacy.identity_values_agree("make", make_hint, rule["fields"].get("make")))
    ]
    candidates: dict[str, list[Any]] = {}
    for rule in matched:
        for field, value in rule["fields"].items():
            if field not in _PARTIAL_FIELDS:
                continue
            if value not in candidates.setdefault(field, []):
                candidates[field].append(value)
    observation = make_observation(
        tool,
        identifier,
        field_candidates=candidates,
        outcome="success" if matched else "not_found",
        diagnostics={
            "rules": [
                {
                    key: rule.get(key)
                    for key in (
                        "id",
                        "source_url",
                        "source_commit",
                        "version",
                        "license",
                        "market",
                        "year_from",
                        "year_to",
                        "verification",
                        "validation",
                        "data_origin_id",
                    )
                }
                for rule in matched
            ]
        },
        model_year=checked["context"].get("model_year"),
        identifier_type=identifier_type,
    )
    observation["field_evidence"] = [
        {"field": field, "value": value, "depends_on": [], "strength": "candidate", "rule_id": rule["id"]}
        for rule in matched
        for field, value in rule["fields"].items()
        if field in _PARTIAL_FIELDS
    ]
    return observation


def vin_brand_details(
    identifier: str, *, model_year: int | None = None, market: str | None = None, identifier_type: str = "auto"
) -> dict[str, Any]:
    return _decode_rules(
        "vin_brand_details",
        identifier,
        frame=False,
        model_year=model_year,
        market=market,
        identifier_type=identifier_type,
    )


def decode_frame_local(
    identifier: str, *, make_hint: str | None = None, identifier_type: str = "auto"
) -> dict[str, Any]:
    return _decode_rules(
        "decode_frame_local", identifier, frame=True, make_hint=make_hint, identifier_type=identifier_type
    )


def _binding_matches(observation: dict[str, Any], identifier: str, kind: str) -> bool:
    binding = observation.get("input_binding") or {}
    scope = binding.get("scope")
    hints = binding.get("hints") or {}
    if scope == "wmi":
        if kind not in {"vin", "vin_partial"}:
            return False
        canonical = vin_lookup.classify_identifier(identifier).normalized
        expected = wmi_for_vin(canonical)
        # A three-character WMI hint is valid for a low-volume VIN, but only as a prefix hint.
        return binding.get("digest") in {_digest(expected, "wmi", hints), _digest(expected[:3], "wmi", hints)}
    expected_scope = (
        "partial" if kind == "vin_partial" else "frame" if kind in {"frame_number", "market_code"} else "vin"
    )
    return scope == expected_scope and binding.get("digest") == _digest(identifier, scope, hints)


def _rule_evidence_valid(row: dict[str, Any], tool: str, identifier: str, year: int | None) -> bool:
    rule_id = row.get("rule_id")
    if not rule_id:
        return True
    if not isinstance(rule_id, str):
        return False
    rule = next((rule for rule in _rules() if rule["id"] == rule_id), None)
    canonical = vin_lookup.classify_identifier(identifier).normalized
    return bool(
        rule
        and tool in {"vin_brand_details", "decode_frame_local"}
        and _rule_matches(rule, canonical, year, None)
        and row["value"] == rule["fields"].get(row["field"])
    )


def _observation_error(observation: Any, identifier: str, kind: str, context: dict[str, Any]) -> str | None:
    if (
        not isinstance(observation, dict)
        or observation.get("schema") != OBSERVATION_SCHEMA
        or type(observation.get("schema_version")) is not int
        or observation["schema_version"] != 1
    ):
        return "invalid_identity_observation"
    tool = observation.get("tool")
    if not isinstance(tool, str) or tool not in SOURCE_TOOLS:
        return "unsupported_identity_source"
    source = observation.get("source")
    if (
        not isinstance(source, dict)
        or source.get("id") != tool
        or source.get("data_origin_id") != SOURCE_TOOLS[tool][0]
    ):
        return "invalid_identity_source"
    binding = observation.get("input_binding")
    if (
        not isinstance(binding, dict)
        or not isinstance(binding.get("scope"), str)
        or binding["scope"] not in {"vin", "partial", "frame", "wmi"}
    ):
        return "invalid_identity_observation"
    hints = binding.get("hints")
    if not isinstance(hints, dict) or any(key != "model_year" for key in hints):
        return "invalid_identity_observation"
    year = hints.get("model_year")
    if year is not None and (isinstance(year, bool) or not isinstance(year, int) or not 1886 <= year <= 2100):
        return "invalid_identity_observation"
    if year is not None and context.get("model_year") not in (None, year):
        return "identity_observation_hint_mismatch"
    if not _binding_matches(observation, identifier, kind):
        return "identity_observation_input_mismatch"
    supported_scopes = {
        "decode_wmi_local": {"wmi"},
        "decode_wmi_vpic": {"wmi"},
        "decode_frame_local": {"frame"},
        "vininfo_decode": {"vin"},
        "corgi_decode": {"vin"},
    }
    if binding["scope"] not in supported_scopes.get(tool, {"vin", "partial"}):
        return "invalid_identity_observation"
    evidence = observation.get("field_evidence")
    if not isinstance(evidence, list) or len(evidence) > 500 or not _safe_fields(observation.get("vehicle_profile")):
        return "invalid_identity_observation"
    for row in evidence:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("field"), str)
            or not _safe_fields({row["field"]: row.get("value")})
        ):
            return "invalid_identity_observation"
        if not isinstance(row.get("depends_on", []), list) or any(
            item != "caller.model_year" for item in row.get("depends_on", [])
        ):
            return "invalid_identity_observation"
        if not _rule_evidence_valid(row, tool, identifier, year):
            return "invalid_identity_observation"
    if (
        not isinstance(observation.get("diagnostics"), dict)
        or not isinstance(observation.get("ok"), bool)
        or not isinstance(observation.get("outcome"), str)
    ):
        return "invalid_identity_observation"
    if observation["diagnostics"].get("model_year_hint_requested") != year:
        return "invalid_identity_observation"
    if not isinstance(observation.get("warnings"), list) or any(
        not isinstance(warning, str) for warning in observation["warnings"]
    ):
        return "invalid_identity_observation"
    if not isinstance(source.get("version"), str) or not isinstance(observation.get("errors"), list):
        return "invalid_identity_observation"
    return None


def _supported(observation: dict[str, Any], row: dict[str, Any]) -> bool:
    diagnostics = observation["diagnostics"]
    if observation["tool"] == "decode_vin_vpic":
        binding = diagnostics.get("identifier_binding")
        if (
            diagnostics.get("error_code") not in (0, "0")
            or not isinstance(binding, dict)
            or binding.get("status") != "exact"
            or binding.get("verified") is not True
        ):
            return False
    if observation["tool"] == "corgi_decode":
        codes = diagnostics.get("provider_error_codes")
        if observation["outcome"] != "success" or not isinstance(codes, list) or codes:
            return False
    return bool(
        observation["tool"] in {"decode_vin_vpic", "corgi_decode"}
        and observation["input_binding"]["scope"] == "vin"
        and diagnostics.get("provider_clean") is True
        and diagnostics.get("identifier_verified") is True
        and not row.get("depends_on")
    )


def _append_source_evidence(
    profile: dict[str, Any],
    evidence: list[dict[str, Any]],
    observation: dict[str, Any],
    seen: set[tuple[str, str, str]],
) -> None:
    tool = observation["tool"]
    origin, kind = SOURCE_TOOLS[tool]
    for index, supplied in enumerate(observation["field_evidence"]):
        field, value = supplied["field"], supplied["value"]
        if observation["input_binding"]["scope"] in {"partial", "wmi"} and field not in _PARTIAL_FIELDS:
            continue
        # Caller booleans/statuses/readiness and strength are never used as policy overrides.
        dependencies = list(supplied.get("depends_on") or [])
        hint = observation["diagnostics"].get("model_year_hint_requested")
        if field == "model_year" and hint is not None and str(hint) == str(value):
            dependencies = ["caller.model_year"]
        row = dict(supplied, depends_on=dependencies)
        rule_id = supplied.get("rule_id")
        rule = next((rule for rule in _rules() if rule["id"] == rule_id), None) if rule_id else None
        row_origin = rule["data_origin_id"] if rule else origin
        row_source = f"{tool}:{rule_id}" if rule else tool
        key = (row_origin, field, json.dumps(value, sort_keys=True, ensure_ascii=False))
        strength = "supported" if _supported(observation, row) else "candidate"
        if key in seen:
            for existing in evidence:
                if (
                    existing.get("data_origin_id") == row_origin
                    and existing["field"] == field
                    and legacy.identity_values_agree(field, existing["value"], value)
                    and strength == "supported"
                    and existing.get("strength") != "supported"
                ):
                    existing.update(
                        strength="supported",
                        confidence=0.75,
                        depends_on=dependencies,
                        independent=not dependencies,
                        source=row_source,
                        source_kind=kind,
                        source_version=observation["source"]["version"],
                        bound=True,
                        identifier_binding="exact",
                        observation_index=index,
                        raw_value=value,
                    )
            continue
        seen.add(key)
        legacy._merge_field(profile, evidence, field, value, row_source, 0.75 if strength == "supported" else 0.55)
        evidence[-1].update(
            source_kind=kind,
            data_origin_id=row_origin,
            source_version=observation["source"]["version"],
            independent=not dependencies,
            bound=observation["input_binding"]["scope"] == "vin",
            strength=strength,
            depends_on=dependencies,
            identifier_binding="exact" if observation["input_binding"]["scope"] == "vin" else "local_pattern",
            observation_index=index,
        )


def _evidence_details(evidence: list[dict[str, Any]]) -> None:
    for index, row in enumerate(evidence):
        row.update(evidence_id=f"e{index + 1:04d}")
        row.setdefault("source_kind", "caller")
        row.setdefault("data_origin_id", "caller")
        row.setdefault("independent", False)
        row.setdefault("bound", False)
        row.setdefault("strength", "candidate")
        row.setdefault("depends_on", [])
        row.setdefault("identifier_binding", "caller")
        row["normalization"] = {"method": "validated_scalar", "changed": row["value"] != row["raw_value"]}


def _score(evidence: list[dict[str, Any]], conflicts: list[dict[str, Any]]) -> float:
    supported = [row for row in evidence if row.get("strength") == "supported"]
    if {"make", "model"}.issubset({row["field"] for row in supported}):
        score = 0.85
    elif any(row["field"] in {"model", "model_family"} for row in evidence):
        score = 0.65
    elif evidence:
        score = 0.4
    else:
        score = 0.0
    if conflicts:
        score = min(score, 0.64)
    return score


def _reconciliation_conflicts(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts = []
    for field in dict.fromkeys(row["field"] for row in evidence):
        rows = [row for row in evidence if row["field"] == field]
        disagreements: list[str] = []
        for index, left in enumerate(rows):
            for right in rows[index + 1 :]:
                # A broad manufacturer group is compatible with its constituent brands.
                makes = {str(left["value"]).casefold(), str(right["value"]).casefold()}
                group = (
                    field == "make"
                    and "volkswagen group" in makes
                    and bool(makes & {"volkswagen", "audi", "skoda", "seat", "cupra"})
                )
                if not group and not legacy._evidence_pair_agrees(field, left, right, evidence):
                    disagreements.extend((left["evidence_id"], right["evidence_id"]))
        if disagreements:
            conflicts.append(
                {
                    "field": field,
                    "code": "field_disagreement",
                    "severity": "medium",
                    "blocking_scopes": ["vehicle"],
                    "evidence_ids": sorted(set(disagreements)),
                }
            )
    return conflicts


def _finalize_identity(identifier: str, checked: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    classification = vin_lookup.classify_identifier(identifier, identifier_type=checked["identifier_type"])
    profile: dict[str, Any] = {}
    evidence: list[dict[str, Any]] = []
    legacy._merge_crm_context_fields(profile, evidence, checked["context"])
    seen: set[tuple[str, str, str]] = set()
    failures = []
    for observation in observations:
        if observation["ok"] and observation["outcome"] in {"success", "partial"}:
            _append_source_evidence(profile, evidence, observation, seen)
        else:
            failures.append({"source": observation["tool"], "code": observation["outcome"], "stage": "source_decode"})
    _evidence_details(evidence)
    diagnostics: dict[str, Any] = {
        "model_year": legacy._vin_model_year(classification.normalized),
        "check_digit": legacy._check_digit(classification.normalized),
        "frame_query_hint": legacy._frame_query_hint(classification.normalized),
    }
    conflicts = _reconciliation_conflicts(evidence)
    conflicts.extend(legacy._year_relationship_conflicts(profile, diagnostics, evidence))
    frame_matched = classification.kind in {"frame_number", "market_code"} and any(
        observation["tool"] == "decode_frame_local" and observation["ok"] for observation in observations
    )
    valid = classification.kind == "vin" or frame_matched
    if diagnostics["check_digit"].get("status") == "fail" and legacy._uses_strict_north_american_vin(profile):
        valid = False
    if not valid:
        conflicts.append(
            {
                "field": "identifier",
                "code": "identifier_unresolved",
                "severity": "medium",
                "blocking_scopes": ["vehicle"],
            }
        )
    statuses, provenance, missing = legacy._field_status_and_provenance(profile, evidence, conflicts)
    by_id = {row["evidence_id"]: row for row in evidence}
    for rows in provenance.values():
        for row in rows:
            source_evidence = by_id[row["evidence_id"]]
            row["data_origin_id"] = source_evidence["data_origin_id"]
            row["source_version"] = source_evidence.get("source_version")
    families = legacy._family_candidates(evidence)
    score = _score(evidence, conflicts)
    lookup_plan = vin_lookup.build_lookup_plan(
        identifier,
        make_hint=profile.get("make"),
        model_year=profile.get("model_year"),
        identifier_type=checked["identifier_type"],
        live_vpic=False,
        vpic_result={"ok": bool(profile.get("make")), "vehicle": {"make": profile.get("make")}},
    )
    lookup_plan.update(family_candidates=families, routing_basis="reconciled_identity")
    result = legacy.invalid_identity_result([])
    result.update(
        ok=True,
        status="partial" if failures else "ok",
        identifier=vin_lookup._public_identifier(classification),
        normalized_query=vin_lookup._redact_identifier(classification.normalized)["display"],
        vehicle_profile=profile,
        diagnostics=diagnostics,
        identifier_validation={
            "ok": True,
            "status": "valid" if valid else "unresolved",
            "valid_for_vehicle_lookup": valid,
            "reasons": [] if valid else ["identifier_unresolved"],
        },
        confidence=score,
        confidence_label=legacy._confidence_label(score),
        field_evidence=evidence,
        field_statuses=statuses,
        provenance=provenance,
        missing_fields=missing,
        family_candidates=families,
        conflicts=conflicts,
        evidence_sources=[observation["source"] for observation in observations],
        provider_errors=failures,
        warnings=[
            warning
            for observation in observations
            for warning in observation.get("warnings", [])
            if isinstance(warning, str)
        ],
        errors=[],
        normalization_notes=checked["normalization_notes"],
        lookup_plan=lookup_plan,
        required_next_sources=legacy._source_requirements(classification.kind, profile),
        processing={
            "network_calls": 0,
            "observation_count": len(observations),
            "independent_origin_count": len({row["data_origin_id"] for row in evidence if row["independent"]}),
        },
    )
    result["parts_lookup_readiness"] = build_parts_lookup_readiness(result)
    return _redact_output(result, classification.normalized)


def reconcile_vehicle_identity(
    identifier: str,
    results: list[dict[str, Any]],
    *,
    crm_context: dict[str, Any] | None = None,
    identifier_type: str = "auto",
) -> dict[str, Any]:
    checked = validate_identity_input(identifier, crm_context, identifier_type=identifier_type)
    if not checked["ok"]:
        return legacy.invalid_identity_result(checked["errors"])
    if not isinstance(results, list) or len(results) > 100:
        return legacy.invalid_identity_result([{"code": "invalid_identity_observations", "field": "results"}])
    classification = vin_lookup.classify_identifier(identifier, identifier_type=identifier_type)
    for observation in results:
        error = _observation_error(observation, identifier, classification.kind, checked["context"])
        if error:
            return legacy.invalid_identity_result([{"code": error, "field": "results", "stage": "identity_binding"}])
    return _finalize_identity(identifier, checked, results)
