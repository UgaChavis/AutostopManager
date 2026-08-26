from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Any

from .catalog_adapters import catalog_provider_status
from .vin_lookup import (
    _public_identifier,
    _redact_identifier,
    _redact_identifier_payload,
    build_lookup_plan,
    classify_identifier,
    decode_vin_vpic,
    decode_vins_vpic_batch,
    decode_wmi_vpic,
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

WMI_HINTS = {
    "WDD": {
        "make": "Mercedes-Benz",
        "manufacturer": "Mercedes-Benz Cars",
        "market": "Europe/global",
        "country": "Germany",
        "vehicle_type": "Passenger car",
    },
    "WDC": {
        "make": "Mercedes-Benz",
        "manufacturer": "Mercedes-Benz Cars / Mercedes-Benz USA market dependent",
        "market": "Europe/global",
        "country": "Germany/ROW market dependent",
        "vehicle_type": "MPV/SUV",
    },
    "WAU": {
        "make": "Audi",
        "manufacturer": "Audi AG",
        "market": "Europe/global",
        "country": "Germany",
        "vehicle_type": "Passenger car",
    },
    "WVW": {
        "make": "Volkswagen",
        "manufacturer": "Volkswagen AG",
        "market": "Europe/global",
        "country": "Germany",
        "vehicle_type": "Passenger car",
    },
    "VSK": {
        "make": "Nissan",
        "manufacturer": "Nissan Motor Iberica / Europe market dependent",
        "market": "Europe/ROW",
        "country": "Spain/ROW market dependent",
        "vehicle_type": "Passenger/SUV",
    },
    "X4X": {
        "make": "BMW",
        "manufacturer": "BMW local assembly / Russia market dependent",
        "market": "Russia/CIS",
        "country": "Russia",
        "vehicle_type": "Passenger car",
    },
    "JMZ": {
        "make": "Mazda",
        "manufacturer": "Mazda Motor Corporation",
        "market": "Europe/global",
        "country": "Japan/ROW market dependent",
        "vehicle_type": "Passenger/MPV/SUV",
    },
    "1C4": {
        "make": "Jeep",
        "manufacturer": "FCA US LLC",
        "market": "North America",
        "country": "United States",
        "vehicle_type": "MPV/SUV",
    },
    "JHL": {
        "make": "Honda",
        "manufacturer": "Honda Motor Co., Ltd.",
        "market": "Japan/global",
        "country": "Japan",
        "vehicle_type": "MPV/SUV",
    },
    "JTE": {
        "make": "Toyota",
        "manufacturer": "Toyota Motor Corporation",
        "market": "Japan/global",
        "country": "Japan",
        "vehicle_type": "MPV/SUV",
    },
    "XW8": {
        "make": "Volkswagen Group",
        "manufacturer": "Volkswagen Group Rus / local assembly",
        "market": "Russia/CIS",
        "country": "Russia",
        "vehicle_type": "Passenger car",
    },
    "MMC": {
        "make": "Mitsubishi",
        "manufacturer": "Mitsubishi Motors",
        "market": "Asia/ROW",
        "country": "Thailand/Japan-market dependent",
        "vehicle_type": "Pickup/SUV",
    },
    "LSC": {
        "make": "Changan",
        "manufacturer": "Changan Automobile",
        "market": "China/ROW",
        "country": "China",
        "vehicle_type": "Passenger/pickup",
    },
}


@dataclass(frozen=True)
class PlatformRule:
    rule_id: str
    pattern: str
    kind: str
    fields: dict[str, Any]
    evidence: str
    confidence: float
    notes: str = ""

    def matches(self, identifier: str) -> bool:
        return re.match(self.pattern, identifier, flags=re.IGNORECASE) is not None


PLATFORM_RULES: tuple[PlatformRule, ...] = (
    PlatformRule(
        "mercedes_wdd212",
        r"^WDD212",
        "vin_prefix",
        {"make": "Mercedes-Benz", "platform": "W212 E-Class", "model_family": "E-Class"},
        "VIN WMI WDD plus Mercedes 212 platform prefix.",
        0.72,
    ),
    PlatformRule(
        "vw_russia_polo_61",
        r"^XW8ZZZ61",
        "vin_prefix",
        {"make": "Volkswagen", "model_family": "Polo / Polo Sedan", "market": "Russia/CIS"},
        "XW8 local VW Group WMI plus 61 model family prefix used by Polo-class vehicles.",
        0.68,
    ),
    PlatformRule(
        "audi_a8_d4_4h",
        r"^WAUZZZ4H",
        "vin_prefix",
        {"make": "Audi", "model": "A8", "platform": "D4 / 4H", "market": "Europe/ROW"},
        "Audi WMI plus 4H model-platform prefix; exact engine/options need Audi EPC/ETKA.",
        0.8,
    ),
    PlatformRule(
        "vw_golf_mk7_au",
        r"^WVWZZZAU",
        "vin_prefix",
        {"make": "Volkswagen", "model_family": "Golf", "platform": "Mk7 / MQB AU", "market": "Europe/ROW"},
        "Volkswagen WMI plus AU Golf/MQB platform prefix; PR/options need ETKA/partslink24.",
        0.78,
    ),
    PlatformRule(
        "mercedes_gle_c292_wdc292",
        r"^WDC292",
        "vin_prefix",
        {
            "make": "Mercedes-Benz",
            "model_family": "GLE Coupe / GLE-Class",
            "platform": "C292/W292",
            "market": "Europe/ROW",
        },
        "Mercedes-Benz WDC WMI plus 292 GLE Coupe/GLE family platform prefix; exact options need Mercedes EPC.",
        0.78,
    ),
    PlatformRule(
        "nissan_pathfinder_r51_vskjvwr51",
        r"^VSKJVWR51",
        "vin_prefix",
        {"make": "Nissan", "model": "Pathfinder", "platform": "R51", "market": "Europe/ROW"},
        "Nissan Europe WMI plus R51 Pathfinder prefix; exact trim/options need Nissan EPC.",
        0.78,
    ),
    PlatformRule(
        "mazda_cx5_ke_jmzke",
        r"^JMZKE",
        "vin_prefix",
        {"make": "Mazda", "model": "CX-5", "platform": "KE", "market": "Europe/ROW"},
        "Mazda WMI plus KE CX-5 platform prefix; exact engine/options need Mazda EPC.",
        0.78,
    ),
    PlatformRule(
        "bmw_russia_g30_x4xjd19",
        r"^X4XJD19",
        "vin_prefix",
        {"make": "BMW", "model_family": "5 Series", "platform": "G30/G31 family", "market": "Russia/CIS"},
        "BMW local-assembly WMI plus CRM-observed 5-series prefix; exact variant/options need BMW ETK/AIR.",
        0.72,
    ),
    PlatformRule(
        "bmw_russia_e90_x4xva98",
        r"^X4XVA98",
        "vin_prefix",
        {"make": "BMW", "model_family": "3 Series", "platform": "E90/E91/E92 family", "market": "Russia/CIS"},
        "BMW local-assembly WMI plus CRM-observed 3-series prefix; exact variant/options need BMW ETK/AIR.",
        0.72,
    ),
    PlatformRule(
        "jeep_wk2_overland_5_7",
        r"^1C4RJFCT",
        "vin_prefix",
        {
            "make": "Jeep",
            "model": "Grand Cherokee",
            "platform": "WK2",
            "trim": "Overland",
            "engine": "5.7 V8 gasoline",
            "drivetrain": "4WD",
        },
        "North-American VIN prefix and vPIC-clean pattern for WK2 Grand Cherokee Overland 5.7.",
        0.9,
    ),
    PlatformRule(
        "suzuki_hustler_mr41s",
        r"^MR41S[-]?\d{5,7}$",
        "jdm_frame",
        {"make": "Suzuki", "model": "Hustler", "platform": "MR41S", "engine": "R06A 0.66L kei", "market": "Japan"},
        "Japanese frame/model code MR41S; requires Suzuki EPC for production/options.",
        0.76,
    ),
    PlatformRule(
        "honda_crv_rd5",
        r"^JHLRD5",
        "vin_prefix",
        {"make": "Honda", "model_family": "CR-V", "platform": "RD5/RD-series", "market": "Japan/global"},
        "Honda JHL WMI plus RD5 CR-V platform prefix.",
        0.74,
    ),
    PlatformRule(
        "honda_civic_es1_frame",
        r"^ES1[-]?\d{6,7}$",
        "jdm_frame",
        {"make": "Honda", "model": "Civic", "platform": "ES1", "market": "Japan/ROW"},
        "Honda ES1 frame/body-number pattern; exact production and options need Honda/Japan EPC.",
        0.84,
    ),
    PlatformRule(
        "mitsubishi_l200_mmcjjjkl",
        r"^MMCJJJKL",
        "vin_prefix",
        {
            "make": "Mitsubishi",
            "model_family": "L200 / Triton",
            "engine": "4N15 2.4 diesel likely when CRM confirms",
            "market": "Asia/ROW",
        },
        "Mitsubishi MMC WMI plus L200/Triton-style prefix; exact trim needs Mitsubishi EPC.",
        0.7,
    ),
    PlatformRule(
        "changan_hunter_lscbbz2a",
        r"^LSCBBZ2A",
        "vin_prefix",
        {"make": "Changan", "model_family": "Hunter Plus / SC10", "market": "China/ROW"},
        "Changan LSC WMI plus CRM-matching Hunter/SC10 prefix.",
        0.68,
    ),
    PlatformRule(
        "skoda_rapid_russia_xw8ac2nh",
        r"^XW8AC2NH",
        "vin_prefix",
        {"make": "Skoda", "model": "Rapid", "market": "Russia/CIS"},
        "XW8 local VW Group WMI plus Skoda Rapid-style prefix.",
        0.68,
    ),
    PlatformRule(
        "toyota_prado_150_jtebu3fj",
        r"^JTEBU3FJ",
        "vin_prefix",
        {
            "make": "Toyota",
            "model": "Land Cruiser Prado 150",
            "engine": "1GR-FE 4.0 V6 gasoline",
            "drivetrain": "4WD",
            "market": "Japan/ROW",
        },
        "Toyota JTE WMI plus Prado 150 1GR-FE prefix; exact production/options need Toyota EPC.",
        0.82,
    ),
    PlatformRule(
        "toyota_prado_120_jtebu29j",
        r"^JTEBU29J",
        "vin_prefix",
        {
            "make": "Toyota",
            "model": "Land Cruiser Prado 120",
            "engine": "1GR-FE 4.0 V6 gasoline",
            "drivetrain": "4WD",
            "market": "Japan/ROW",
        },
        "Toyota JTE WMI plus Prado 120 1GR-FE prefix; exact production/options need Toyota EPC.",
        0.82,
    ),
)


def _compact(value: Any) -> str:
    return str(value or "").strip()


def _normalize_make(value: Any) -> Any:
    text = _compact(value)
    key = re.sub(r"[^a-z0-9]+", "", text.casefold())
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
        "production_year": "model_year",
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
        "engine",
        "engine_model",
        "transmission",
        "gearbox_model",
        "drivetrain",
        "market",
        "source_summary",
        "source_confidence",
        "oem_notes",
    ]
    result = {key: context.get(key) for key in allowed if context.get(key) not in (None, "")}
    if "production_year" in result and "model_year" not in result:
        result["model_year"] = result["production_year"]
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
    key = field
    current = profile.get(key)
    if current in (None, ""):
        profile[key] = value
    evidence.append({"source": source, "field": key, "value": value, "confidence": confidence})


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
            "reason": "Needed for VINdecodeOE/OE applicability and cross/analog confidence before parts writeback.",
        }
    )
    requirements.append(
        {
            "source_id": "vin17_api",
            "reason": "Useful second source for all/common parts by VIN and OE search, especially ROW/KDM/JDM coverage.",
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
    plant_country = _compact(profile.get("plant_country")).lower()
    return (
        "north america" in market
        or "fca us" in manufacturer
        or plant_country
        in {
            "united states",
            "united states (usa)",
            "canada",
            "mexico",
        }
    )


def _conflicts(
    profile: dict[str, Any], crm_context: dict[str, Any], diagnostics: dict[str, Any]
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
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
    for field in ("make", "model", "model_year", "engine", "transmission", "drivetrain", "market"):
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
            _merge_field(profile, field_evidence, "plant_country", value, "local WMI hint", 0.55)
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
        evidence_sources.append({"source": "NHTSA vPIC", "status": "failed", "error": result.get("error")})
        return

    error_code = str(result.get("error_code") or "")
    vpic_clean = error_code in {"", "0"}
    vpic_field_confidence = 0.75 if vpic_clean else 0.45
    field_map = {
        "make": "make",
        "model": "model",
        "modelyear": "model_year",
        "bodyclass": "body_class",
        "vehicletype": "vehicle_type",
        "plantcountry": "plant_country",
        "plantcity": "plant_city",
        "enginemodel": "engine",
        "enginecylinders": "engine_cylinders",
        "drivetype": "drivetrain",
        "transmissionstyle": "transmission",
        "fueltypeprimary": "fuel_type",
        "displacementl": "engine_displacement_l",
        "enginehp": "engine_power_hp",
        "vehicledescriptor": "vehicle_descriptor",
    }
    for source_field, target_field in field_map.items():
        if source_field != "modelyear" or vpic_clean:
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
            "limitations": "Basic manufacturer-reported VIN decode; not an EPC and often partial for ROW/JDM/Russia/CIS VINs.",
            "request_url": result.get("request_url"),
        }
    )
    if not vpic_clean:
        warnings.append("vPIC returned non-clean diagnostics; use as partial evidence only.")
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
            {"source": "NHTSA vPIC WMI", "status": "failed", "wmi": wmi, "error": result.get("error")}
        )
        return
    field_map = {
        "name": "manufacturer",
        "manufacturername": "manufacturer",
        "make": "make",
        "vehicletype": "vehicle_type",
        "country": "plant_country",
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
        score += 0.3 if str(vpic_result.get("error_code") or "") in {"", "0"} else 0.12
    if wmi_result and wmi_result.get("ok"):
        score += 0.06
    if crm:
        score += _bounded_confidence(crm.get("source_confidence"), default=0.75) * 0.2
    has_high_conflict = any(item.get("severity") == "high" for item in conflicts)
    if crm and platform_rule is not None and not has_high_conflict:
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


def decode_vehicle_identity(
    identifier: str,
    *,
    crm_context: dict[str, Any] | None = None,
    model_year: int | None = None,
    make_hint: str | None = None,
    live_vpic: bool = True,
    vpic_result: dict[str, Any] | None = None,
    live_wmi: bool = True,
) -> dict[str, Any]:
    classification = classify_identifier(identifier)
    normalized = classification.normalized
    crm = _clean_context(crm_context)
    if model_year is not None and "model_year" not in crm:
        crm["model_year"] = model_year
    if make_hint and "make" not in crm:
        crm["make"] = make_hint

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

    wmi = normalized[:3] if len(normalized) >= 3 else ""
    wmi_hint = _merge_local_wmi_hint(wmi, profile, field_evidence, evidence_sources)
    platform_rule = _merge_platform_rule(normalized, profile, field_evidence, evidence_sources)

    if vpic_result is None and live_vpic and classification.kind in {"vin", "vin_partial"}:
        vpic_result = decode_vin_vpic(normalized, model_year=model_year or crm.get("model_year"))
    _merge_vpic_result(
        vpic_result,
        identifier_kind=classification.kind,
        profile=profile,
        field_evidence=field_evidence,
        evidence_sources=evidence_sources,
        warnings=warnings,
    )

    wmi_result: dict[str, Any] | None = None
    if live_wmi and classification.kind in {"vin", "vin_partial"} and wmi:
        raw_vpic_vehicle = vpic_result.get("vehicle") if vpic_result else None
        vpic_vehicle = raw_vpic_vehicle if isinstance(raw_vpic_vehicle, dict) else {}
        needs_wmi = (
            not vpic_result
            or not vpic_vehicle.get("make")
            or any(_compact(source.get("source")) == "local WMI hints" for source in evidence_sources)
        )
        if needs_wmi:
            wmi_result = decode_wmi_vpic(wmi)
            _merge_wmi_result(
                wmi_result,
                wmi=wmi,
                profile=profile,
                field_evidence=field_evidence,
                evidence_sources=evidence_sources,
            )

    if diagnostics["frame_query_hint"]:
        warnings.append(f"Try frame query form {diagnostics['frame_query_hint']} in Japan/EPC catalogs.")
    if diagnostics["check_digit"].get("status") in {"fail", "invalid_characters"}:
        warnings.append("VIN requires document/EPC verification before VIN-critical parts orders.")
    if classification.kind == "market_code":
        warnings.append("Identifier is market/JDM-frame-like; do not treat it as a 17-character ISO VIN.")

    conflicts = _conflicts(profile, crm, diagnostics)
    lookup_plan = build_lookup_plan(
        normalized,
        model_year=model_year or crm.get("model_year"),
        make_hint=profile.get("make") or make_hint,
        live_vpic=live_vpic,
        vpic_result=vpic_result,
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

    required_sources = _source_requirements(classification.kind, profile)
    adapters = [
        provider
        for provider in catalog_provider_status()["providers"]
        if provider["stage"] in {"oem_catalog", "catalog_cross"}
    ]
    has_high_conflict = any(item["severity"] == "high" for item in conflicts)
    ready_for_parts = _confidence_label(score) == "high" and not has_high_conflict
    blocking_reasons = []
    if has_high_conflict:
        blocking_reasons.append("high_severity_identity_conflict")
    if _confidence_label(score) != "high":
        blocking_reasons.append("identity_confidence_below_high")

    result = {
        "ok": True,
        "identifier": _public_identifier(classification),
        "normalized_query": _redact_identifier(normalized)["display"],
        "privacy": {
            "raw_identifier_is_sensitive": True,
            "raw_identifier_redacted_from_output": True,
            "persistence_rule": "Do not store raw customer VIN/frame in durable memory or Git fixtures.",
        },
        "vehicle_profile": profile,
        "diagnostics": diagnostics,
        "confidence": score,
        "confidence_label": _confidence_label(score),
        "parts_lookup_readiness": {
            "ready_for_oem_lookup": ready_for_parts,
            "ready_for_oem_candidate_lookup": ready_for_parts,
            "ready_for_crm_writeback": ready_for_parts,
            "cross_source_agreement": {
                "status": "not_checked",
                "sources": ["NHTSA vPIC", "PartsAPI VINdecodeOE"],
                "matched_fields": [],
                "conflicting_fields": [],
            },
            "blocking_reasons": blocking_reasons,
            "reason": "High-confidence identity without high-severity conflicts is required before OEM/parts writeback."
            if not ready_for_parts
            else "Identity is sufficient for OEM lookup, but part fitment still requires EPC/source attribution.",
        },
        "field_evidence": field_evidence,
        "evidence_sources": evidence_sources,
        "conflicts": conflicts,
        "warnings": warnings,
        "required_next_sources": required_sources,
        "adapter_status": adapters,
        "lookup_plan": lookup_plan,
        "registry_version": load_source_registry().get("version", 0),
    }
    return _redact_identifier_payload(result, normalized)


def decode_vehicle_identities(
    items: list[dict[str, Any]], *, live_vpic: bool = True, use_vpic_batch: bool = True
) -> dict[str, Any]:
    results = []
    prepared_items: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for item in items:
        context = _flatten_crm_context(item.get("crm_context") or item)
        identifier = str(
            item.get("identifier")
            or item.get("vin")
            or item.get("frame")
            or context.get("vin")
            or context.get("frame")
            or ""
        )
        prepared_items.append((item, context, identifier))

    batch_result = (
        decode_vins_vpic_batch(
            [
                {
                    "identifier": identifier,
                    "model_year": item.get("model_year") or item.get("production_year") or context.get("model_year"),
                }
                for item, context, identifier in prepared_items
            ]
        )
        if live_vpic and use_vpic_batch
        else {"ok": True, "results_by_vin": {}}
    )
    raw_batch_by_vin = batch_result.get("results_by_vin")
    batch_by_vin = raw_batch_by_vin if isinstance(raw_batch_by_vin, dict) else {}

    for item, context, identifier in prepared_items:
        batch_vpic = batch_by_vin.get(str(identifier).upper().replace(" ", "").replace("-", ""))
        results.append(
            decode_vehicle_identity(
                identifier,
                crm_context=context,
                model_year=item.get("model_year") or item.get("production_year") or context.get("model_year"),
                make_hint=item.get("make") or item.get("make_display") or context.get("make"),
                live_vpic=live_vpic and batch_vpic is None,
                vpic_result=batch_vpic,
            )
        )
    high = sum(1 for item in results if item["confidence_label"] == "high")
    medium = sum(1 for item in results if item["confidence_label"] == "medium")
    low = sum(1 for item in results if item["confidence_label"] == "low")
    ready_oem = sum(1 for item in results if item["parts_lookup_readiness"]["ready_for_oem_lookup"])
    ready_candidate = sum(1 for item in results if item["parts_lookup_readiness"].get("ready_for_oem_candidate_lookup"))
    ready_writeback = sum(1 for item in results if item["parts_lookup_readiness"].get("ready_for_crm_writeback"))
    return {
        "ok": True,
        "count": len(results),
        "high_confidence_count": high,
        "medium_confidence_count": medium,
        "low_confidence_count": low,
        "identity_coverage": {
            "high_ratio": round(high / len(results), 2) if results else 0,
            "ready_for_oem_lookup_count": ready_oem,
            "ready_for_oem_candidate_lookup_count": ready_candidate,
            "ready_for_crm_writeback_count": ready_writeback,
            "needs_epc_or_document_check_count": sum(1 for item in results if item["required_next_sources"]),
        },
        "vpic_batch": {
            "attempted": bool(live_vpic and use_vpic_batch),
            "ok": bool(batch_result.get("ok")),
            "decoded_count": len(batch_by_vin),
            "error": batch_result.get("error"),
        },
        "configured_paid_sources": [
            source["source_id"]
            for source in catalog_provider_status()["providers"]
            if source["configured"]
            and source["stage"] in {"oem_catalog", "catalog_cross", "procurement_price", "market_price"}
        ],
        "missing_paid_sources": [
            source["source_id"]
            for source in catalog_provider_status()["providers"]
            if not source["configured"]
            and source["stage"] in {"oem_catalog", "catalog_cross", "procurement_price", "market_price"}
        ],
        "results": results,
    }
