from __future__ import annotations

import json
import math
import re
import unicodedata
from itertools import combinations
from typing import Any

from .automotive_local_registry import PLATFORM_RULES, WMI_HINTS, PlatformRule
from .catalog_adapters import catalog_provider_status
from .vehicle_identity_inputs import MAX_IDENTITY_ITEMS, validate_identity_input, validate_identity_item
from .vehicle_identity_policy import build_parts_lookup_readiness
from .vehicle_identity_transport import wmi_for_vin
from .vin_lookup import (
    _finite_number,
    _public_identifier,
    _redact_identifier,
    _redact_identifier_payload,
    build_lookup_plan,
    classify_identifier,
    _identifier_binding,
)
from .vin_sources import load_source_registry


YEAR_CODE_SEQUENCE = "ABCDEFGHJKLMNPRSTVWXY123456789"

TRANSLITERATION = {
    **{str(i): i for i in range(10)},
    **dict.fromkeys("AJ", 1),
    **dict.fromkeys("BKS", 2),
    **dict.fromkeys("CLT", 3),
    **dict.fromkeys("DMU", 4),
    **dict.fromkeys("ENV", 5),
    **dict.fromkeys("FW", 6),
    **dict.fromkeys("GPX", 7),
    **dict.fromkeys("HY", 8),
    **dict.fromkeys("RZ", 9),
}
VIN_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]


def _compact(value: Any) -> str:
    return str(value or "").strip()


def _normalize_make(value: Any) -> Any:
    text = _compact(value)
    unaccented = "".join(char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char))
    key = re.sub(r"[^a-z0-9]+", "", unaccented.casefold())
    for corporate_prefix, canonical_key in (
        ("toyotamotor", "toyota"),
        ("mitsubishimotors", "mitsubishi"),
    ):
        if key.startswith(corporate_prefix):
            key = canonical_key
            break
    aliases = {
        "volkskwagen": "Volkswagen",
        "volkswagen": "Volkswagen",
        "vw": "Volkswagen",
        "mercedesbenz": "Mercedes-Benz",
        "mercedes": "Mercedes-Benz",
        "skoda": "Skoda",
        "toyota": "Toyota",
        "mitsubishi": "Mitsubishi",
        "suzuki": "Suzuki",
        "honda": "Honda",
        "jeep": "Jeep",
        "changan": "Changan",
    }
    return aliases.get(key, value)


def _normalize_model(value: Any) -> Any:
    text = _compact(value)
    if not text:
        return value
    # CRM entries sometimes use the Cyrillic capital Ye instead of Latin E.
    return text.replace("Е", "E").replace("е", "e")


def _normalize_identity_field(field: str, value: Any) -> Any:
    if field == "make":
        return _normalize_make(value)
    if field == "model":
        return _normalize_model(value)
    return value


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


def _flatten_crm_context(context: dict[str, Any] | None) -> dict[str, Any]:
    context = context or {}
    profile: dict[str, Any] = {}
    for key in ["vehicle_profile", "vehicle_profile_compact", "crm_vehicle_profile"]:
        profile.update(_as_mapping(context.get(key)))

    merged = {**profile, **context}
    aliases = {
        "make_display": "make",
        "model_display": "model",
        "engine_model": "engine",
        "gearbox_model": "transmission",
        "chassis_number": "frame",
        "body_number": "frame",
    }
    for source, target in aliases.items():
        if merged.get(source) not in (None, "") and merged.get(target) in (None, ""):
            merged[target] = merged[source]
    if merged.get("display_name") and merged.get("vehicle") in (None, ""):
        merged["vehicle"] = merged["display_name"]
    if merged.get("make") not in (None, ""):
        merged["make"] = _normalize_make(merged["make"])
    if merged.get("model") not in (None, ""):
        merged["model"] = _normalize_model(merged["model"])
    return merged


def _clean_context(context: dict[str, Any] | None) -> dict[str, Any]:
    context = _flatten_crm_context(context)
    allowed = [
        "vehicle",
        "make",
        "model",
        "model_year",
        "production_year",
        "production_date",
        "modification",
        "trim",
        "series",
        "options",
        "transmission_speeds",
        "transmission_code",
        "engine",
        "engine_model",
        "transmission",
        "gearbox_model",
        "drivetrain",
        "market",
        "source_summary",
        "source_confidence",
        "oem_notes",
        "input_alias_conflicts",
    ]
    result = {key: context.get(key) for key in allowed if context.get(key) not in (None, "")}
    if "engine_model" in result and "engine" not in result:
        result["engine"] = result["engine_model"]
    if "gearbox_model" in result and "transmission" not in result:
        result["transmission"] = result["gearbox_model"]
    return result


def _vin_model_year(vin: str) -> dict[str, Any]:
    if len(vin) != 17:
        return {"status": "not_applicable", "note": "identifier is not a 17-character VIN"}
    code = vin[9]
    years = [1980 + index for index, value in enumerate(YEAR_CODE_SEQUENCE) if value == code]
    years += [year + 30 for year in years if year + 30 < 2040]
    if not years:
        return {
            "status": "unknown_or_row",
            "code": code,
            "note": "10th symbol is not a standard North-American model-year code; many ROW/JDM VINs need EPC.",
        }
    return {"status": "decoded", "code": code, "candidate_years": years}


def _check_digit(vin: str) -> dict[str, Any]:
    if len(vin) != 17:
        return {"status": "not_applicable", "note": "identifier is not a 17-character VIN"}
    if any(char not in TRANSLITERATION for char in vin):
        invalid = sorted({char for char in vin if char not in TRANSLITERATION})
        return {"status": "invalid_characters", "invalid": invalid}
    total = sum(TRANSLITERATION[char] * weight for char, weight in zip(vin, VIN_WEIGHTS, strict=True))
    expected_value = total % 11
    expected = "X" if expected_value == 10 else str(expected_value)
    return {
        "status": "pass" if vin[8] == expected else "fail",
        "expected": expected,
        "actual": vin[8],
        "note": "North-American check digit; ROW VINs may still need OEM/EPC confirmation.",
    }


def _frame_query_hint(identifier: str) -> str | None:
    match = re.match(r"^([A-Z]{2}\d)(\d{6,7})$", identifier)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    match = re.match(r"^([A-Z]{1,4}\d{1,2}[A-Z]?)(\d{5,7})$", identifier)
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}"


def _merge_field(
    profile: dict[str, Any], evidence: list[dict[str, Any]], field: str, value: Any, source: str, confidence: float
) -> None:
    if value in (None, "", []):
        return
    raw_value = value
    value = _normalize_identity_field(field, value)
    key = field
    current = profile.get(key)
    if current in (None, ""):
        profile[key] = value
    evidence.append({"source": source, "field": key, "value": value, "raw_value": raw_value, "confidence": confidence})


def _matching_platform_rule(identifier: str) -> PlatformRule | None:
    normalized = identifier.upper().replace(" ", "")
    for rule in PLATFORM_RULES:
        if rule.matches(normalized):
            return rule
    return None


def _source_requirements(identifier_kind: str, profile: dict[str, Any]) -> list[dict[str, str]]:
    make = _compact(profile.get("make")).lower()
    requirements: list[dict[str, str]] = []
    requirements.append(
        {
            "source_id": "partsapi_ru",
            "reason": "VINdecode can add vehicle identity and TecDoc article/cross candidates; exact OEM applicability still needs EPC evidence.",
        }
    )
    if make in {"mercedes-benz", "volkswagen", "skoda", "bmw", "audi"}:
        requirements.append(
            {
                "source_id": "partslink24_or_oem_epc",
                "reason": "European VINs need brand EPC/partslink24 for PR/options, production date, and exact OEM part applicability.",
            }
        )
    return requirements


def _uses_strict_north_american_vin(profile: dict[str, Any]) -> bool:
    market = _compact(profile.get("market")).lower()
    manufacturer = _compact(profile.get("manufacturer")).lower()
    # Assembly location is not a sales-market assertion.
    return "north america" in market or "fca us" in manufacturer


def _normalized_identity_value(field: str, value: Any) -> str:
    normalized_value = _normalize_identity_field(field, value)
    return re.sub(r"[^0-9a-zа-яё]+", "", _compact(normalized_value).casefold())


def _identity_tokens(value: Any) -> tuple[str, ...]:
    return tuple(re.findall(r"[0-9a-zа-яё]+", _compact(value).casefold()))


def _model_family_values_agree(left: Any, right: Any) -> bool:
    left_tokens = _identity_tokens(left)
    right_tokens = _identity_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    if left_tokens == right_tokens:
        return True
    if len(left_tokens) == len(right_tokens):
        return False
    shorter, longer = sorted((left_tokens, right_tokens), key=len)
    # A multi-token family emitted by one decoder may be refined with a
    # platform/generation suffix by another.  Single-token prefixes such as
    # Corolla/Cross, A8/A80 or 3/320 remain deliberately incompatible.
    return len(shorter) >= 2 and longer[: len(shorter)] == shorter


def _transmission_signature(value: Any) -> tuple[str | None, frozenset[int], frozenset[str]]:
    tokens = _identity_tokens(value)
    kinds: set[str] = set()
    gear_counts: set[int] = set()
    gearbox_codes: set[str] = set()

    for index, token in enumerate(tokens):
        if token in {"cvt", "вариатор"} or token.startswith("вариатор"):
            kinds.add("cvt")
        elif token in {"dct", "dsg", "amt", "robot"} or token.startswith("робот"):
            kinds.add("dual_clutch_or_robot")
        elif token in {"manual", "mt", "мкпп"} or token.startswith("механ"):
            kinds.add("manual")
        elif token in {"automatic", "auto", "at", "акпп"} or token.startswith("автомат"):
            kinds.add("automatic")

        compact_match = re.fullmatch(r"(\d{1,2})(at|mt|dct|dsg)", token)
        if compact_match:
            gear_counts.add(int(compact_match.group(1)))
            kinds.add(
                {
                    "at": "automatic",
                    "mt": "manual",
                    "dct": "dual_clutch_or_robot",
                    "dsg": "dual_clutch_or_robot",
                }[compact_match.group(2)]
            )
        if token.isdigit() and 3 <= int(token) <= 10:
            neighbours = tokens[max(0, index - 1) : index] + tokens[index + 1 : index + 3]
            if any(
                neighbour in {"speed", "speeds", "automatic", "auto", "at", "manual", "mt", "dct", "dsg"}
                or neighbour.startswith(("ступ", "автомат", "механ"))
                for neighbour in neighbours
            ):
                gear_counts.add(int(token))
        if re.fullmatch(r"dq\d{3}", token):
            kinds.add("dual_clutch_or_robot")
            gearbox_codes.add(token)

    kind = next(iter(kinds)) if len(kinds) == 1 else None
    return kind, frozenset(gear_counts), frozenset(gearbox_codes)


def _transmission_values_agree(left: Any, right: Any) -> bool:
    left_kind, left_gears, left_codes = _transmission_signature(left)
    right_kind, right_gears, right_codes = _transmission_signature(right)
    if not left_kind or left_kind != right_kind:
        return False
    if left_gears and right_gears and left_gears != right_gears:
        return False
    if left_codes and right_codes and left_codes != right_codes:
        return False
    return True


def identity_values_agree(field: str, left: Any, right: Any) -> bool:
    """Compare identity facts conservatively without substring-prefix matches."""

    left_normalized = _normalized_identity_value(field, left)
    right_normalized = _normalized_identity_value(field, right)
    if not left_normalized or not right_normalized:
        return False
    if left_normalized == right_normalized:
        return True
    if field == "model":
        return _model_family_values_agree(left, right)
    if field == "transmission":
        return _transmission_values_agree(left, right)
    if field == "engine":
        return _engine_values_agree(left, right)
    if field == "market":
        return _market_values_agree(left, right)
    if field == "drivetrain":
        aliases = {"4wd": "awd", "4wd4wheeldrive4x4": "awd", "4wheeldrive4x4": "awd", "allwheeldrive": "awd"}
        return aliases.get(left_normalized, left_normalized) == aliases.get(right_normalized, right_normalized)
    return False


def _engine_values_agree(left: Any, right: Any) -> bool:
    def engine_code(value: Any) -> str | None:
        for token in re.findall(r"[A-Z0-9]+(?:[.-][A-Z0-9]+)*", _compact(value).upper()):
            code = re.sub(r"[^A-Z0-9]", "", token)
            if (
                len(code) >= 3
                and any(char.isdigit() for char in code)
                and any(char.isalpha() for char in code)
                and not code.endswith(("CC", "HP", "KW"))
                and not re.fullmatch(r"\d+(?:L|T|V|PS|BHP|RPM)|EURO\d+", code)
            ):
                return code
        return None

    left_code, right_code = engine_code(left), engine_code(right)
    return bool(left_code and left_code == right_code)


def _market_values_agree(left: Any, right: Any) -> bool:
    aliases = {"eu": "europe", "usa": "northamerica", "us": "northamerica", "jp": "japan"}
    a, b = (_normalized_identity_value("market", value) for value in (left, right))
    a, b = aliases.get(a, a), aliases.get(b, b)
    if a == b:
        return True
    # A broad local coverage hint is compatible, but never sales-market proof.
    broad = {"global", "row", "europeglobal", "europerow"}
    return a in broad or b in broad


def _consensus_vin_evidence_conflicts(
    crm_context: dict[str, Any], field_evidence: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return only strong CRM conflicts corroborated by two VIN-derived sources.

    A local WMI hint and a non-clean vPIC reply are intentionally excluded by
    the confidence floor.  The gate must fail closed only when a platform rule
    and a clean decoder (or equivalent independent evidence) agree against an
    explicit CRM make/model.
    """

    conflicts: list[dict[str, Any]] = []
    for field in ("make", "model"):
        crm_value = crm_context.get(field)
        if crm_value in (None, ""):
            continue
        decoded = [
            item
            for item in field_evidence
            if item.get("field") == field
            and item.get("source") != "CRM context"
            and _bounded_confidence(item.get("confidence"), default=0.0) >= 0.7
            and item.get("value") not in (None, "")
        ]
        for item in decoded:
            value = item.get("value")
            agreeing = [
                candidate for candidate in decoded if identity_values_agree(field, value, candidate.get("value"))
            ]
            evidence_sources = sorted(
                {str(candidate.get("source") or "") for candidate in agreeing if candidate.get("source")}
            )
            if len(evidence_sources) < 2 or identity_values_agree(field, crm_value, value):
                continue
            conflicts.append(
                {
                    "field": field,
                    "crm_value": crm_value,
                    "decoded_value": value,
                    "evidence_sources": evidence_sources,
                    "severity": "high",
                    "note": "CRM make/model conflicts with two consistent VIN-derived sources; verify documents or EPC before VIN-critical lookup.",
                }
            )
            break
    return conflicts


def _conflicts(
    profile: dict[str, Any],
    crm_context: dict[str, Any],
    diagnostics: dict[str, Any],
    field_evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    conflicts.extend(_consensus_vin_evidence_conflicts(crm_context, field_evidence))
    if crm_context.get("model_year") and diagnostics.get("model_year", {}).get("candidate_years"):
        years = diagnostics["model_year"]["candidate_years"]
        try:
            crm_year = int(crm_context["model_year"])
        except (TypeError, ValueError):
            crm_year = None
        if (
            crm_year is not None
            and crm_year not in [int(year) for year in years]
            and _uses_strict_north_american_vin(profile)
        ):
            conflicts.append(
                {
                    "field": "model_year",
                    "crm_value": crm_context["model_year"],
                    "decoded_candidates": years,
                    "severity": "medium",
                    "note": "CRM year may be registration/production year, or VIN may use ROW-specific year encoding; verify by document/EPC.",
                }
            )
    if diagnostics.get("check_digit", {}).get("status") == "fail" and _uses_strict_north_american_vin(profile):
        conflicts.append(
            {
                "field": "vin_check_digit",
                "crm_value": diagnostics["check_digit"].get("actual"),
                "decoded_candidates": [diagnostics["check_digit"].get("expected")],
                "severity": "high",
                "note": "North-American VIN check digit did not pass; verify the identifier from documents before VIN-critical parts orders.",
            }
        )
    return conflicts


def _confidence_label(score: float) -> str:
    if score >= 0.85:
        return "high"
    if score >= 0.65:
        return "medium"
    return "low"


def _merge_crm_context_fields(
    profile: dict[str, Any],
    field_evidence: list[dict[str, Any]],
    crm: dict[str, Any],
) -> None:
    for field in (
        "make",
        "model",
        "model_year",
        "production_year",
        "production_date",
        "engine",
        "transmission",
        "transmission_speeds",
        "transmission_code",
        "drivetrain",
        "market",
        "modification",
        "trim",
        "series",
        "options",
    ):
        if crm.get(field) not in (None, ""):
            _merge_field(profile, field_evidence, field, crm[field], "CRM context", 0.55)
    if crm.get("vehicle") and not profile.get("model"):
        _merge_field(profile, field_evidence, "vehicle_text", crm["vehicle"], "CRM context", 0.45)


def _merge_local_wmi_hint(
    wmi: str,
    profile: dict[str, Any],
    field_evidence: list[dict[str, Any]],
    evidence_sources: list[dict[str, Any]],
) -> dict[str, Any] | None:
    wmi_hint = WMI_HINTS.get(wmi)
    if not wmi_hint:
        return None
    for key, value in wmi_hint.items():
        if key == "country":
            _merge_field(profile, field_evidence, "manufacturer_country", value, "local WMI hint", 0.55)
        elif key == "vehicle_type":
            _merge_field(profile, field_evidence, "vehicle_type", value, "local WMI hint", 0.5)
        else:
            _merge_field(profile, field_evidence, key, value, "local WMI hint", 0.55)
    evidence_sources.append({"source": "local WMI hints", "status": "matched", "wmi": wmi, "confidence": 0.55})
    return wmi_hint


def _merge_platform_rule(
    normalized: str,
    profile: dict[str, Any],
    field_evidence: list[dict[str, Any]],
    evidence_sources: list[dict[str, Any]],
) -> PlatformRule | None:
    platform_rule = _matching_platform_rule(normalized)
    if platform_rule is None:
        return None
    for field, value in platform_rule.fields.items():
        _merge_field(profile, field_evidence, field, value, platform_rule.rule_id, platform_rule.confidence)
    evidence_sources.append(
        {
            "source": "local platform rule",
            "status": "matched",
            "rule_id": platform_rule.rule_id,
            "kind": platform_rule.kind,
            "evidence": platform_rule.evidence,
            "confidence": platform_rule.confidence,
        }
    )
    return platform_rule


def _merge_vpic_result(
    result: dict[str, Any] | None,
    *,
    identifier_kind: str,
    profile: dict[str, Any],
    field_evidence: list[dict[str, Any]],
    evidence_sources: list[dict[str, Any]],
    warnings: list[str],
) -> None:
    if result is None or identifier_kind not in {"vin", "vin_partial"}:
        return
    raw_vehicle = result.get("vehicle")
    vehicle = raw_vehicle if isinstance(raw_vehicle, dict) else {}
    if not result.get("ok"):
        warnings.append(str(result.get("error") or "vPIC decode failed"))
        evidence_sources.append(
            {
                "source": "NHTSA vPIC",
                "status": "failed",
                "error": result.get("error"),
                "outcome": result.get("outcome"),
                "retryable": bool(result.get("retryable")),
                "identifier_binding": result.get("identifier_binding"),
            }
        )
        return

    vpic_clean = _vpic_has_clean_diagnostics(result) and bool((result.get("identifier_binding") or {}).get("verified"))
    coverage = str(result.get("coverage") or ("basic" if vpic_clean else "partial_or_unsupported"))
    vpic_field_confidence = 0.75 if vpic_clean else 0.45
    field_map = {
        "make": "make",
        "model": "model",
        "modelyear": "model_year",
        "trim": "trim",
        "series": "series",
        "series2": "series2",
        "bodyclass": "body_class",
        "vehicletype": "vehicle_type",
        "plantcountry": "plant_country",
        "plantcity": "plant_city",
        "enginemodel": "engine",
        "enginecylinders": "engine_cylinders",
        "drivetype": "drivetrain",
        "transmissionstyle": "transmission",
        "transmissionspeeds": "transmission_speeds",
        "fueltypeprimary": "fuel_type",
        "displacementl": "engine_displacement_l",
        "enginehp": "engine_power_hp",
        "vehicledescriptor": "vehicle_descriptor",
    }
    variant_fields = {
        "modelyear",
        "enginemodel",
        "enginecylinders",
        "drivetype",
        "transmissionstyle",
        "transmissionspeeds",
        "trim",
        "series",
        "series2",
        "fueltypeprimary",
        "displacementl",
        "enginehp",
    }
    for source_field, target_field in field_map.items():
        if source_field not in variant_fields or vpic_clean:
            _merge_field(
                profile,
                field_evidence,
                target_field,
                vehicle.get(source_field),
                "NHTSA vPIC",
                vpic_field_confidence,
            )
    evidence_sources.append(
        {
            "source": "NHTSA vPIC",
            "status": "ok",
            "mode": "batch" if result.get("batch") else ("extended" if result.get("extended") else "single"),
            "decoded_fields": sorted(str(key) for key in vehicle),
            "error_code": result.get("error_code"),
            "error_text": result.get("error_text"),
            "coverage": coverage,
            "epc_confirmed": False,
            "limitations": "Basic manufacturer-reported VIN decode; not an EPC and often partial for ROW/JDM/Russia/CIS VINs.",
            "request_url": result.get("request_url"),
            "identifier_binding": result.get("identifier_binding"),
        }
    )
    if not vpic_clean:
        warnings.append(
            "vPIC returned non-clean diagnostics; use as partial evidence only. Variant, engine and transmission fields were not promoted to the vehicle profile."
        )
    if not vehicle.get("make"):
        warnings.append("vPIC returned no make; route to ROW/EPC catalog.")


def _merge_wmi_result(
    result: dict[str, Any],
    *,
    wmi: str,
    profile: dict[str, Any],
    field_evidence: list[dict[str, Any]],
    evidence_sources: list[dict[str, Any]],
) -> None:
    raw_profile = result.get("wmi_profile")
    profile_wmi = raw_profile if isinstance(raw_profile, dict) else {}
    if not result.get("ok"):
        evidence_sources.append(
            {
                "source": "NHTSA vPIC WMI",
                "status": "failed",
                "wmi": wmi,
                "error": result.get("error"),
                "outcome": result.get("outcome"),
                "retryable": bool(result.get("retryable")),
                "identifier_binding": result.get("identifier_binding"),
            }
        )
        return
    field_map = {
        "name": "manufacturer",
        "manufacturername": "manufacturer",
        "make": "make",
        "vehicletype": "vehicle_type",
        "country": "manufacturer_country",
    }
    for source_field, target_field in field_map.items():
        _merge_field(profile, field_evidence, target_field, profile_wmi.get(source_field), "NHTSA vPIC WMI", 0.6)
    evidence_sources.append(
        {
            "source": "NHTSA vPIC WMI",
            "status": "ok",
            "wmi": wmi,
            "decoded_fields": sorted(str(key) for key in profile_wmi),
            "request_url": result.get("request_url"),
            "identifier_binding": result.get("identifier_binding"),
        }
    )


def _bounded_confidence(value: Any, *, default: float) -> float:
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number):
        return default
    return max(0.0, min(number, 1.0))


def _vpic_has_clean_diagnostics(result: dict[str, Any]) -> bool:
    value = result.get("error_code")
    return not isinstance(value, bool) and value in ("0", 0)


def _identity_score(
    *,
    classification_confidence: float,
    identifier_kind: str,
    profile: dict[str, Any],
    crm: dict[str, Any],
    wmi_hint: dict[str, Any] | None,
    platform_rule: PlatformRule | None,
    vpic_result: dict[str, Any] | None,
    wmi_result: dict[str, Any] | None,
    conflicts: list[dict[str, Any]],
) -> float:
    score = max(classification_confidence * 0.25, 0.0)
    score += 0.15 if wmi_hint else 0.0
    score += platform_rule.confidence * 0.45 if platform_rule is not None else 0.0
    raw_vpic_vehicle = vpic_result.get("vehicle") if vpic_result else None
    vpic_vehicle = raw_vpic_vehicle if isinstance(raw_vpic_vehicle, dict) else {}
    if vpic_result and vpic_result.get("ok") and vpic_vehicle.get("make"):
        score += 0.3 if _vpic_has_clean_diagnostics(vpic_result) else 0.12
    if wmi_result and wmi_result.get("ok"):
        score += 0.06
    has_identity_context = any(
        crm.get(field) not in (None, "", [])
        for field in ("make", "model", "model_year", "engine", "transmission", "market", "drivetrain")
    )
    if has_identity_context:
        score += _bounded_confidence(crm.get("source_confidence"), default=0.75) * 0.2
    has_high_conflict = any(item.get("severity") == "high" for item in conflicts)
    if has_identity_context and platform_rule is not None and not has_high_conflict:
        crm_source_confidence = _bounded_confidence(crm.get("source_confidence"), default=0.0)
        if profile.get("make") and (profile.get("model") or profile.get("model_family")):
            if identifier_kind in {"frame_number", "market_code"} and platform_rule.kind.endswith("frame"):
                score += 0.16
            elif crm_source_confidence >= 0.9:
                score += 0.06
    if conflicts:
        score -= 0.15 if has_high_conflict else 0.08
    if has_high_conflict:
        score = min(score, 0.79)
    return max(0.0, min(round(score, 2), 0.95))


IDENTITY_FIELDS = (
    "make",
    "model",
    "model_year",
    "production_year",
    "production_date",
    "engine",
    "transmission",
    "transmission_speeds",
    "drivetrain",
    "market",
    "modification",
    "trim",
    "series",
    "options",
)


def invalid_identity_result(errors: list[dict[str, Any]], item_index: int | None = None) -> dict[str, Any]:
    """Return a complete row for invalid input without echoing rejected values."""
    result: dict[str, Any] = {
        "ok": False,
        "input_binding": None,
        "status": "invalid_input",
        "schema_version": 2,
        "identifier": _public_identifier(classify_identifier("")),
        "normalized_query": "",
        "privacy": {
            "raw_identifier_is_sensitive": True,
            "raw_identifier_redacted_from_output": True,
            "persistence_rule": "Do not store raw customer VIN/frame in durable memory or Git fixtures.",
        },
        "vehicle_profile": {},
        "diagnostics": {
            "model_year": {"status": "not_applicable"},
            "check_digit": {"status": "not_applicable"},
            "frame_query_hint": None,
        },
        "identifier_validation": {"ok": False, "valid_for_vehicle_lookup": False, "status": "invalid_input"},
        "confidence": 0.0,
        "confidence_label": "low",
        "confidence_semantics": "Heuristic evidence score, not a calibrated probability or measured vehicle accuracy.",
        "field_evidence": [],
        "field_statuses": {
            field: {"status": "missing", "evidence_ids": [], "alternatives": []} for field in IDENTITY_FIELDS
        },
        "provenance": {},
        "missing_fields": list(IDENTITY_FIELDS),
        "family_candidates": [],
        "evidence_sources": [],
        "conflicts": [],
        "warnings": [],
        "errors": [
            {key: row[key] for key in ("code", "field", "stage") if key in row}
            for row in errors
            if isinstance(row, dict)
        ],
        "provider_errors": [],
        "normalization_notes": [],
        "required_next_sources": [],
        "adapter_status": [],
        "lookup_plan": {"ok": False, "steps": [], "hints": [], "warnings": ["invalid_input"]},
        "registry_version": load_source_registry().get("version", 0),
    }
    if item_index is not None:
        result["item_index"] = item_index
    result["parts_lookup_readiness"] = build_parts_lookup_readiness(result)
    return result


def summarize_identity_batch(
    results: list[dict[str, Any]], *, vpic_batch: dict[str, Any] | None = None, processing: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Preserve row order and legacy coverage counters while exposing partial failures."""
    counts = {
        label: sum(row.get("confidence_label") == label for row in results) for label in ("high", "medium", "low")
    }
    successful = sum(row.get("ok") is not False for row in results)
    partial = sum(row.get("status") == "partial" for row in results)
    providers = catalog_provider_status().get("providers") or []
    paid = [
        row
        for row in providers
        if row.get("stage") in {"oem_catalog", "catalog_cross", "procurement_price", "market_price"}
    ]
    readiness = [row.get("parts_lookup_readiness") or {} for row in results]
    result: dict[str, Any] = {
        "ok": successful == len(results),
        "status": "ok" if successful == len(results) and not partial else "partial" if successful else "failed",
        "count": len(results),
        "success_count": successful,
        "error_count": len(results) - successful,
        "partial_count": partial,
        "high_confidence_count": counts["high"],
        "medium_confidence_count": counts["medium"],
        "low_confidence_count": counts["low"],
        "identity_coverage": {
            "high_ratio": round(counts["high"] / len(results), 2) if results else 0,
            "ready_for_family_lookup_count": sum(bool(row.get("ready_for_family_lookup")) for row in readiness),
            "ready_for_vehicle_lookup_count": sum(bool(row.get("ready_for_vehicle_lookup")) for row in readiness),
            "ready_for_oem_lookup_count": sum(bool(row.get("ready_for_oem_lookup")) for row in readiness),
            "ready_for_oem_candidate_lookup_count": sum(
                bool(row.get("ready_for_oem_candidate_lookup")) for row in readiness
            ),
            "ready_for_crm_writeback_count": sum(bool(row.get("ready_for_crm_writeback")) for row in readiness),
            "needs_epc_or_document_check_count": sum(bool(row.get("required_next_sources")) for row in results),
        },
        "vpic_batch": vpic_batch or {"attempted": False, "ok": True, "decoded_count": 0, "error": None},
        "configured_paid_sources": [row["source_id"] for row in paid if row.get("configured")],
        "missing_paid_sources": [row["source_id"] for row in paid if not row.get("configured")],
        "results": results,
    }
    if processing is not None:
        result["processing"] = processing
    return result


def _bound_vpic_result(result: dict[str, Any] | None, identifier: str, *, partial: bool) -> dict[str, Any] | None:
    if result is None:
        return None
    if not isinstance(result, dict):
        return _malformed_provider_result("NHTSA vPIC")
    safe = dict(result)
    raw_vehicle = result.get("vehicle")
    vehicle: dict[str, Any] = raw_vehicle if isinstance(raw_vehicle, dict) else {}
    if result.get("ok"):
        if not isinstance(raw_vehicle, dict) or not _provider_fields_are_valid(vehicle, wmi=False):
            return _malformed_provider_result("NHTSA vPIC")
        binding = _identifier_binding(identifier, vehicle.get("vin") or result.get("vin"), partial=partial)
        safe["identifier_binding"] = binding
        if binding["status"] in {"mismatch", "missing"}:
            safe.update(
                ok=False,
                vehicle={},
                outcome="identity_mismatch" if binding["status"] == "mismatch" else "identity_unverified",
                error="Provider vehicle identity does not bind to the requested identifier.",
            )
    return safe


def _bound_wmi_result(result: dict[str, Any] | None, wmi: str) -> dict[str, Any] | None:
    if result is None:
        return None
    if not isinstance(result, dict):
        return _malformed_provider_result("NHTSA vPIC WMI")
    safe = dict(result)
    raw_profile = result.get("wmi_profile")
    profile: dict[str, Any] = raw_profile if isinstance(raw_profile, dict) else {}
    if result.get("ok"):
        if not isinstance(raw_profile, dict) or not _provider_fields_are_valid(profile, wmi=True):
            return _malformed_provider_result("NHTSA vPIC WMI")
        echo = profile.get("wmi") or result.get("wmi")
        normalized_echo = re.sub(r"[\s-]+", "", echo.upper()) if isinstance(echo, str) else ""
        matches = normalized_echo == wmi and bool(wmi)
        binding = {"status": "exact" if matches else "mismatch" if normalized_echo else "missing", "verified": matches}
        safe["identifier_binding"] = binding
        if not binding["verified"]:
            safe.update(
                ok=False,
                wmi_profile={},
                outcome="identity_mismatch" if binding["status"] == "mismatch" else "identity_unverified",
                error="Provider WMI identity does not bind to the requested WMI.",
            )
    return safe


def _malformed_provider_result(source: str) -> dict[str, Any]:
    return {
        "ok": False,
        "source": source,
        "outcome": "adapter_malformed_payload",
        "retryable": False,
        "vehicle": {},
        "wmi_profile": {},
        "error": "Provider returned malformed vehicle fields.",
    }


def _provider_fields_are_valid(fields: dict[str, Any], *, wmi: bool) -> bool:
    numeric = (
        {"manufacturerid", "vehicletypeid", "id"}
        if wmi
        else {"modelyear", "transmissionspeeds", "enginecylinders", "displacementl", "enginehp"}
    )
    for field, value in fields.items():
        if value in (None, ""):
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return False
        if isinstance(value, (int, float)) and (field not in numeric or not _finite_number(value)):
            return False
        if field == "modelyear" and not validate_identity_input("", {"model_year": value})["ok"]:
            return False
    return True


def _evidence_metadata(
    evidence: list[dict[str, Any]],
    crm: dict[str, Any],
    vpic_result: dict[str, Any] | None,
    wmi_result: dict[str, Any] | None,
    raw_context: dict[str, Any],
) -> None:
    from .automotive_contracts import context_field_evidence

    origins = {item["field"]: item for item in context_field_evidence(raw_context)}
    for alias in crm.get("input_alias_conflicts") or []:
        if isinstance(alias, dict) and alias.get("field") in IDENTITY_FIELDS:
            evidence.append(
                {
                    "source": "CRM alias:" + str(alias.get("source") or "alias"),
                    "field": alias["field"],
                    "value": _normalize_identity_field(alias["field"], alias.get("alias_value")),
                    "raw_value": alias.get("alias_value"),
                    "confidence": 0.55,
                }
            )
    for index, row in enumerate(evidence):
        source = row["source"]
        origin = origins.get(row["field"], {}) if source.startswith("CRM") else {}
        if origin.get("derived"):
            source = row["source"] = origin["source"]
            row["primary_lineage"] = origin["primary_lineage"]
        row["raw_value"] = (
            raw_context.get(row["field"], row["value"])
            if source == "CRM context"
            else row.get("raw_value", row["value"])
        )
        row["normalization"] = {
            "method": "make_alias_unicode"
            if row["field"] == "make"
            else "model_script_alias"
            if row["field"] == "model"
            else "validated_scalar",
            "changed": row["value"] != row["raw_value"],
        }
        provider = vpic_result if source == "NHTSA vPIC" else wmi_result if source == "NHTSA vPIC WMI" else None
        binding = (provider or {}).get("identifier_binding") or {}
        clean = bool(
            provider and provider.get("ok") and (source == "NHTSA vPIC WMI" or _vpic_has_clean_diagnostics(provider))
        )
        caller_derived = bool(origin.get("derived")) or bool(
            source == "NHTSA vPIC"
            and row["field"] == "model_year"
            and provider is not None
            and provider.get("model_year_hint_requested") is not None
            and identity_values_agree("model_year", row["value"], provider["model_year_hint_requested"])
        )
        source_kind = (
            "caller"
            if source.startswith("CRM")
            else "provider"
            if provider or origin.get("derived")
            else "local_hint"
            if source in {"local WMI hint", "local WMI hints"}
            else "local_rule"
        )
        row.update(
            evidence_id=f"e{index + 1:04d}",
            source_kind=source_kind,
            independent=bool(source_kind != "caller" and not caller_derived),
            identifier_binding=binding.get("status", "local_pattern" if source_kind.startswith("local") else "caller"),
            bound=bool(binding.get("verified")),
            strength="supported" if clean and binding.get("verified") and not caller_derived else "candidate",
            depends_on=["caller.model_year"] if caller_derived else [],
        )


def _effective_transmission(row: dict[str, Any], evidence: list[dict[str, Any]]) -> Any:
    speeds = next(
        (
            item["value"]
            for item in evidence
            if item["source"] == row["source"] and item["field"] == "transmission_speeds"
        ),
        None,
    )
    return f"{speeds}-speed {row['value']}" if speeds not in (None, "") else row["value"]


def _evidence_pair_agrees(
    field: str, left: dict[str, Any], right: dict[str, Any], evidence: list[dict[str, Any]]
) -> bool:
    if field == "transmission":
        return identity_values_agree(
            field, _effective_transmission(left, evidence), _effective_transmission(right, evidence)
        )
    return identity_values_agree(field, left["value"], right["value"])


def _is_conflict_evidence(row: dict[str, Any]) -> bool:
    return bool(
        row.get("source_kind") == "caller"
        or row.get("strength") == "supported"
        or (row.get("source_kind") == "local_rule" and row.get("confidence", 0) >= 0.7)
    )


def _semantic_field_conflicts(evidence: list[dict[str, Any]], legacy: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts = [dict(row, blocking_scopes=["vehicle"], code="vin_diagnostic_conflict") for row in legacy]
    for field in IDENTITY_FIELDS:
        rows = [row for row in evidence if row["field"] == field and _is_conflict_evidence(row)]
        differing = [
            (a, b)
            for a, b in combinations(rows, 2)
            if a["source"] != b["source"] and not _evidence_pair_agrees(field, a, b, evidence)
        ]
        if not differing:
            continue
        ids = sorted({row["evidence_id"] for pair in differing for row in pair})
        sources = sorted({row["source"] for pair in differing for row in pair})
        existing = next((row for row in conflicts if row["field"] == field), None)
        if existing:
            existing.update(code="field_disagreement", evidence_ids=ids)
            continue
        provider_disagreement = any(a["source_kind"] != "caller" and b["source_kind"] != "caller" for a, b in differing)
        conflicts.append(
            {
                "field": field,
                "code": "source_disagreement" if provider_disagreement else "caller_disagreement",
                "severity": "high" if provider_disagreement and field in {"make", "model"} else "medium",
                "blocking_scopes": ["vehicle"],
                "evidence_ids": ids,
                "evidence_sources": sources,
                "note": "Contradictory identity facts require reconciliation; bounded family research remains available.",
            }
        )
    return conflicts


def _field_status_and_provenance(
    profile: dict[str, Any], evidence: list[dict[str, Any]], conflicts: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    statuses: dict[str, Any] = {}
    provenance: dict[str, Any] = {}
    disputed = {row["field"] for row in conflicts if "vehicle" in row.get("blocking_scopes", [])}
    for field in dict.fromkeys((*IDENTITY_FIELDS, *(row["field"] for row in evidence))):
        rows = [row for row in evidence if row["field"] == field]
        alternatives: list[dict[str, Any]] = []
        for row in rows:
            alternate = next(
                (item for item in alternatives if identity_values_agree(field, item["value"], row["value"])), None
            )
            if alternate is None:
                alternatives.append({"value": row["value"], "evidence_ids": [row["evidence_id"]]})
            else:
                alternate["evidence_ids"].append(row["evidence_id"])
        status = (
            "disputed"
            if field in disputed
            else "missing"
            if not rows
            else "supported"
            if any(row.get("strength") == "supported" for row in rows)
            else "candidate"
        )
        statuses[field] = {
            "status": status,
            "evidence_ids": [row["evidence_id"] for row in rows],
            "alternatives": alternatives,
        }
        provenance[field] = [
            {
                key: row[key]
                for key in (
                    "evidence_id",
                    "source",
                    "source_kind",
                    "value",
                    "raw_value",
                    "normalization",
                    "independent",
                    "identifier_binding",
                    "depends_on",
                )
            }
            for row in rows
        ]
        if status == "disputed":
            profile.pop(field, None)
    missing = [field for field in IDENTITY_FIELDS if statuses[field]["status"] == "missing"]
    return statuses, provenance, missing


def _family_candidates(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for source in dict.fromkeys(row["source"] for row in evidence):
        fields = {row["field"]: row for row in evidence if row["source"] == source}
        make, model = fields.get("make"), fields.get("model") or fields.get("model_family")
        if not make or not model:
            continue
        candidate = next(
            (
                row
                for row in candidates
                if identity_values_agree("make", row["make"], make["value"])
                and identity_values_agree("model", row["model"], model["value"])
            ),
            None,
        )
        ids = [make["evidence_id"], model["evidence_id"]]
        if candidate is None:
            candidates.append({"make": make["value"], "model": model["value"], "source": source, "evidence_ids": ids})
        else:
            candidate["evidence_ids"].extend(ids)
    return candidates


def _binding_conflicts(vpic_result: dict[str, Any] | None, wmi_result: dict[str, Any] | None) -> list[dict[str, Any]]:
    conflicts = []
    for source, result in (("NHTSA vPIC", vpic_result), ("NHTSA vPIC WMI", wmi_result)):
        binding = (result or {}).get("identifier_binding") or {}
        if binding.get("status") not in {"mismatch", "missing"}:
            continue
        conflicts.append(
            {
                "field": "identifier_binding",
                "code": "provider_identity_" + binding["status"],
                "source": source,
                "severity": "high" if binding["status"] == "mismatch" else "medium",
                "blocking_scopes": ["vehicle"],
                "note": "Provider response cannot confirm the requested vehicle; local family research remains possible.",
            }
        )
    return conflicts


def _year_relationship_conflicts(
    profile: dict[str, Any],
    diagnostics: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    conflicts = []
    years = diagnostics["model_year"].get("candidate_years") or []
    if years and _uses_strict_north_american_vin(profile):
        disputed = [
            row
            for row in evidence
            if row["field"] == "model_year" and str(row["value"]) not in {str(year) for year in years}
        ]
        if disputed:
            conflicts.append(
                {
                    "field": "model_year",
                    "code": "vin_model_year_disagreement",
                    "severity": "medium",
                    "blocking_scopes": ["vehicle"],
                    "decoded_candidates": years,
                    "evidence_ids": [row["evidence_id"] for row in disputed],
                    "evidence_sources": ["VIN model-year encoding", *sorted({row["source"] for row in disputed})],
                    "note": "VIN model-year encoding disagrees with a reported model year; production/registration dates are separate facts.",
                }
            )
    year, build_date = profile.get("production_year"), profile.get("production_date")
    if year is not None and build_date and str(year) != str(build_date)[:4]:
        for field in ("production_year", "production_date"):
            conflicts.append(
                {
                    "field": field,
                    "code": "production_date_year_disagreement",
                    "severity": "medium",
                    "blocking_scopes": ["vehicle"],
                    "evidence_ids": [
                        row["evidence_id"] for row in evidence if row["field"] in {"production_year", "production_date"}
                    ],
                    "note": "Production year and build date disagree; model year is a separate field.",
                }
            )
    return conflicts


def _checked_vpic_diagnostics(result: dict[str, Any]) -> dict[str, Any]:
    raw_codes = result.get("error_codes")
    if not isinstance(raw_codes, list):
        raw_code = result.get("error_code")
        raw_codes = re.split(r"[,;]", raw_code) if isinstance(raw_code, str) and len(raw_code) <= 4096 else [raw_code]
    codes = []
    for value in raw_codes[:64]:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            continue
        code = str(value).strip()
        if re.fullmatch(r"[0-9]{1,4}", code) and code != "0" and code not in codes:
            codes.append(code)
    status = result.get("diagnostics_status")
    if status not in ("reported", "missing"):
        value = result.get("error_code")
        status = (
            "reported" if isinstance(value, (str, int)) and not isinstance(value, bool) and value != "" else "missing"
        )
    text = result.get("error_text")
    return {
        "error_codes": codes,
        "has_error_text": result.get("has_error_text") is True or (isinstance(text, str) and bool(text.strip())),
        "diagnostics_status": status,
        "coverage": "partial_or_unsupported",
    }


def _provider_diagnostics(
    vpic_result: dict[str, Any] | None,
    wmi_result: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    rows = []
    for source, result in (("NHTSA vPIC", vpic_result), ("NHTSA vPIC WMI", wmi_result)):
        if result is not None and not result.get("ok"):
            rows.append(
                {
                    "code": result.get("outcome") or "provider_error",
                    "source": source,
                    "stage": "provider_decode",
                    **(_checked_vpic_diagnostics(result) if source == "NHTSA vPIC" else {}),
                }
            )
    if vpic_result and vpic_result.get("ok") and not _vpic_has_clean_diagnostics(vpic_result):
        rows.append(
            {
                "code": "provider_partial_evidence",
                "source": "NHTSA vPIC",
                "stage": "provider_decode",
                **_checked_vpic_diagnostics(vpic_result),
            }
        )
    return rows


def _identifier_validation(
    classification: Any,
    profile: dict[str, Any],
    diagnostics: dict[str, Any],
    platform_rule: PlatformRule | None,
) -> dict[str, Any]:
    valid = classification.kind == "vin" or bool(
        classification.kind in {"frame_number", "market_code"}
        and platform_rule
        and platform_rule.kind.endswith("frame")
    )
    reasons = []
    if classification.kind == "unknown":
        reasons.append("identifier_missing" if not classification.normalized else "identifier_unrecognized")
    if classification.kind == "vin_partial":
        reasons.append("partial_identifier")
    if (
        classification.kind == "vin"
        and diagnostics["check_digit"].get("status") == "fail"
        and _uses_strict_north_american_vin(profile)
    ):
        valid = False
        reasons.append("north_american_check_digit_failed")
    return {
        "ok": True,
        "status": "valid" if valid else "partial" if classification.kind == "vin_partial" else "unresolved",
        "valid_for_vehicle_lookup": valid,
        "reasons": reasons,
    }


def decode_vehicle_identity(
    identifier: str,
    *,
    crm_context: dict[str, Any] | None = None,
    model_year: int | None = None,
    make_hint: str | None = None,
    live_vpic: bool = True,
    vpic_result: dict[str, Any] | None = None,
    live_wmi: bool = True,
    wmi_result: dict[str, Any] | None = None,
    identifier_type: str = "auto",
) -> dict[str, Any]:
    from .automotive_contracts import binding

    validated = validate_identity_input(
        identifier,
        crm_context,
        model_year=model_year,
        make_hint=make_hint,
        identifier_type=identifier_type,
    )
    if not validated["ok"]:
        return invalid_identity_result(validated["errors"])
    identifier = validated["identifier"]
    classification = classify_identifier(identifier, identifier_type=validated["identifier_type"])
    normalized = classification.normalized
    crm = _clean_context(validated["context"])
    processing = None
    if (live_vpic and vpic_result is None) or (live_wmi and wmi_result is None):
        from .vehicle_identity_transport import (
            SINGLE_HTTP_ATTEMPT_CAP,
            IdentityBudget,
            collect_identity_provider_results,
        )

        collected = collect_identity_provider_results(
            [validated],
            live_vpic=live_vpic and vpic_result is None,
            live_wmi=live_wmi and wmi_result is None,
            use_vpic_batch=False,
            budget=IdentityBudget(max_attempts=SINGLE_HTTP_ATTEMPT_CAP),
        )
        vpic_result = vpic_result if vpic_result is not None else collected["vpic_results"][0]
        wmi_result = wmi_result if wmi_result is not None else collected["wmi_results"][0]
        processing = collected["processing"]
    # A pure merge also checks injected data; transport binding metadata alone is insufficient.
    vpic_result = _bound_vpic_result(vpic_result, normalized, partial=classification.kind == "vin_partial")
    wmi = wmi_for_vin(normalized)
    wmi_result = _bound_wmi_result(wmi_result, wmi)
    profile: dict[str, Any] = {}
    field_evidence: list[dict[str, Any]] = []
    evidence_sources: list[dict[str, Any]] = []
    warnings: list[str] = []
    _merge_crm_context_fields(profile, field_evidence, crm)
    diagnostics: dict[str, Any] = {
        "model_year": _vin_model_year(normalized),
        "check_digit": _check_digit(normalized),
        "frame_query_hint": _frame_query_hint(normalized),
    }
    wmi_hint = _merge_local_wmi_hint(wmi[:3], profile, field_evidence, evidence_sources)
    platform_rule = _merge_platform_rule(normalized, profile, field_evidence, evidence_sources)
    if classification.kind not in {"vin", "vin_partial"}:
        vpic_result = None
        wmi_result = None
    _merge_vpic_result(
        vpic_result,
        identifier_kind=classification.kind,
        profile=profile,
        field_evidence=field_evidence,
        evidence_sources=evidence_sources,
        warnings=warnings,
    )
    if wmi_result is not None:
        _merge_wmi_result(
            wmi_result, wmi=wmi, profile=profile, field_evidence=field_evidence, evidence_sources=evidence_sources
        )
    if diagnostics["frame_query_hint"]:
        warnings.append(f"Try frame query form {diagnostics['frame_query_hint']} in Japan/EPC catalogs.")
    strict_check_digit = _uses_strict_north_american_vin(profile)
    diagnostics["check_digit"]["applicability"] = (
        "required" if strict_check_digit else "not_applicable" if len(normalized) != 17 else "not_established"
    )
    if diagnostics["check_digit"].get("status") == "fail" and not strict_check_digit:
        warnings.append("North-American check-digit applicability is not established; verify ROW identity by OEM/EPC.")
    elif diagnostics["check_digit"].get("status") in {"fail", "invalid_characters"}:
        warnings.append("VIN requires document/EPC verification before VIN-critical parts orders.")
    if classification.kind == "market_code":
        warnings.append("Identifier is market/JDM-frame-like; do not treat it as a 17-character ISO VIN.")
    validation = _identifier_validation(classification, profile, diagnostics, platform_rule)
    _evidence_metadata(field_evidence, crm, vpic_result, wmi_result, crm_context or validated["context"])
    conflicts = _semantic_field_conflicts(field_evidence, _conflicts(profile, crm, diagnostics, field_evidence))
    conflicts.extend(_year_relationship_conflicts(profile, diagnostics, field_evidence))
    conflicts.extend(_binding_conflicts(vpic_result, wmi_result))
    provider_diagnostics = _provider_diagnostics(vpic_result, wmi_result)
    if any(row["code"] == "provider_partial_evidence" for row in provider_diagnostics):
        conflicts.append(
            {
                "field": "provider_decode",
                "code": "provider_partial_evidence",
                "severity": "low",
                "blocking_scopes": ["vehicle"],
                "note": "A partial decoder response supports family candidates, not a resolved vehicle configuration.",
            }
        )
    if not validation["valid_for_vehicle_lookup"]:
        conflicts.append(
            {
                "field": "identifier",
                "code": validation["reasons"][0] if validation["reasons"] else "identifier_unresolved",
                "severity": "medium",
                "blocking_scopes": ["vehicle"],
                "note": "Identifier is not sufficient for vehicle-specific lookup; family research remains possible.",
            }
        )
    score = _identity_score(
        classification_confidence=classification.confidence,
        identifier_kind=classification.kind,
        profile=profile,
        crm=crm,
        wmi_hint=wmi_hint,
        platform_rule=platform_rule,
        vpic_result=vpic_result,
        wmi_result=wmi_result,
        conflicts=conflicts,
    )
    if conflicts:
        score = min(score, 0.79)
    if classification.kind == "unknown":
        score = min(score, 0.64)
    statuses, provenance, missing = _field_status_and_provenance(profile, field_evidence, conflicts)
    families = _family_candidates(field_evidence)
    # Route from reconciled fields only. Disputed provider fields cannot silently choose a brand.
    lookup_plan = build_lookup_plan(
        identifier,
        model_year=profile.get("model_year"),
        make_hint=profile.get("make"),
        live_vpic=False,
        identifier_type=validated["identifier_type"],
        vpic_result={"ok": bool(profile.get("make")), "vehicle": {"make": profile.get("make")}},
    )
    lookup_plan["family_candidates"] = families
    lookup_plan["routing_basis"] = "reconciled_identity"
    provider_partial = bool(provider_diagnostics)
    result: dict[str, Any] = {
        "ok": True,
        "input_binding": binding(identifier, validated["identifier_type"]),
        "status": "partial" if provider_partial else "ok",
        "schema_version": 2,
        "identifier": _public_identifier(classification),
        "normalized_query": _redact_identifier(normalized)["display"],
        "privacy": {
            "raw_identifier_is_sensitive": True,
            "raw_identifier_redacted_from_output": True,
            "persistence_rule": "Do not store raw customer VIN/frame in durable memory or Git fixtures.",
        },
        "vehicle_profile": profile,
        "diagnostics": diagnostics,
        "identifier_validation": validation,
        "confidence": score,
        "confidence_label": _confidence_label(score),
        "confidence_semantics": "Heuristic evidence score, not a calibrated probability or measured vehicle accuracy.",
        "field_statuses": statuses,
        "provenance": provenance,
        "missing_fields": missing,
        "family_candidates": families,
        "field_evidence": field_evidence,
        "evidence_sources": evidence_sources,
        "conflicts": conflicts,
        "warnings": warnings,
        "errors": [],
        "provider_errors": provider_diagnostics,
        "normalization_notes": validated["normalization_notes"],
        "required_next_sources": _source_requirements(classification.kind, profile),
        "adapter_status": [
            provider
            for provider in catalog_provider_status()["providers"]
            if provider["stage"] in {"oem_catalog", "catalog_cross"}
        ],
        "lookup_plan": lookup_plan,
        "registry_version": load_source_registry().get("version", 0),
    }
    result["parts_lookup_readiness"] = build_parts_lookup_readiness(result)
    if processing is not None:
        result["processing"] = processing
    return _redact_identifier_payload(result, normalized)


def decode_vehicle_identities(
    items: list[dict[str, Any]],
    *,
    live_vpic: bool = True,
    use_vpic_batch: bool = True,
) -> dict[str, Any]:
    if not isinstance(items, list) or len(items) > MAX_IDENTITY_ITEMS:
        return {
            "ok": False,
            "status": "invalid_input",
            "count": 0,
            "results": [],
            "errors": [
                {
                    "code": "identity_batch_too_large" if isinstance(items, list) else "expected_list",
                    "field": "items",
                    "stage": "input_validation",
                }
            ],
        }
    from .vehicle_identity_transport import (
        BATCH_HTTP_ATTEMPT_CAP,
        IdentityBudget,
        collect_identity_provider_results,
    )

    prepared = [validate_identity_item(item) for item in items]
    collected = collect_identity_provider_results(
        prepared,
        live_vpic=live_vpic,
        live_wmi=live_vpic,
        use_vpic_batch=use_vpic_batch,
        budget=IdentityBudget(max_attempts=BATCH_HTTP_ATTEMPT_CAP),
    )
    results = []
    for index, item in enumerate(prepared):
        if not item["ok"]:
            results.append(invalid_identity_result(item["errors"], item_index=index))
            continue
        try:
            result = decode_vehicle_identity(
                item["identifier"],
                crm_context=item["context"],
                identifier_type=item["identifier_type"],
                live_vpic=False,
                live_wmi=False,
                vpic_result=collected["vpic_results"][index],
                wmi_result=collected["wmi_results"][index],
            )
        except (TypeError, ValueError, AttributeError, OverflowError):
            result = invalid_identity_result(
                [
                    {"code": "identity_processing_error", "field": "item", "stage": "identity_merge"},
                ],
                item_index=index,
            )
        result["item_index"] = index
        if item["normalization_notes"]:
            result["normalization_notes"] = item["normalization_notes"]
        results.append(result)
    return summarize_identity_batch(results, vpic_batch=collected["vpic_batch"], processing=collected["processing"])
