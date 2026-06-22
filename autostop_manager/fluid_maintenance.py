from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from .config import PROJECT_ROOT
from .source_catalog import recommend_automotive_sources

FLUID_SOURCE_PATH = PROJECT_ROOT / "docs" / "agent" / "automotive_sources" / "fluid_maintenance_sources.json"

UNIT_ALIASES: dict[str, set[str]] = {
    "engine_oil": {"engine", "engine_oil", "motor_oil", "oil", "двигатель", "мотор", "масло_двигателя", "двс"},
    "automatic_transmission": {"at", "automatic", "automatic_transmission", "акпп", "автомат", "atf"},
    "manual_transmission": {"mt", "manual", "manual_transmission", "мкпп", "механика"},
    "cvt": {"cvt", "вариатор", "вариаторная_кпп"},
    "dct": {"dct", "dsg", "dual_clutch", "робот", "роботизированная_кпп"},
    "transfer_case": {"transfer", "transfer_case", "раздатка", "раздаточная_коробка"},
    "front_differential": {"front_diff", "front_differential", "передний_дифференциал", "передний_редуктор"},
    "rear_differential": {"rear_diff", "rear_differential", "задний_дифференциал", "задний_редуктор", "мост"},
    "center_differential": {"center_diff", "center_differential", "межосевой_дифференциал"},
    "haldex_coupling": {"haldex", "awd_coupling", "муфта", "муфта_awd", "халдекс"},
    "power_steering": {"power_steering", "psf", "гур", "рулевое"},
    "brake_fluid": {"brake", "brake_fluid", "тормозная", "тормозная_жидкость"},
    "coolant": {"coolant", "antifreeze", "антифриз", "охлаждающая", "охлаждающая_жидкость"},
}

UNIT_REQUIREMENTS: dict[str, list[str]] = {
    "engine_oil": ["VIN/chassis or exact model", "market", "year", "engine_code", "oil/filter service type"],
    "automatic_transmission": ["VIN/chassis", "market", "transmission_code", "drivetrain", "service operation", "level-check temperature/procedure"],
    "manual_transmission": ["VIN/chassis", "market", "transmission_code", "drivetrain"],
    "cvt": ["VIN/chassis", "market", "transmission_code", "fluid generation/spec", "level-check temperature/procedure"],
    "dct": ["VIN/chassis", "market", "transmission_code", "wet/dry clutch type", "mechatronic/clutch oil distinction"],
    "transfer_case": ["VIN/chassis", "market", "transfer_case_code", "drivetrain"],
    "front_differential": ["VIN/chassis", "market", "axle code", "drivetrain", "LSD/open differential"],
    "rear_differential": ["VIN/chassis", "market", "axle code", "drivetrain", "LSD/open differential"],
    "center_differential": ["VIN/chassis", "market", "drivetrain", "center differential type"],
    "haldex_coupling": ["VIN/chassis", "market", "coupling generation", "filter presence"],
    "power_steering": ["VIN/chassis", "market", "steering system type"],
    "brake_fluid": ["VIN/chassis", "market", "ABS/ESC system", "DOT/OEM spec"],
    "coolant": ["VIN/chassis", "market", "engine_code", "hybrid/EV loop distinction"],
}

UNIT_DATA_TYPES: dict[str, list[str]] = {
    "engine_oil": ["fluids", "maintenance", "owners_manuals"],
    "automatic_transmission": ["fluids", "repair_manuals", "maintenance_data"],
    "manual_transmission": ["fluids", "repair_manuals", "maintenance_data"],
    "cvt": ["fluids", "repair_manuals", "maintenance_data"],
    "dct": ["fluids", "repair_manuals", "maintenance_data"],
    "transfer_case": ["fluids", "repair_manuals", "maintenance_data"],
    "front_differential": ["fluids", "repair_manuals", "maintenance_data"],
    "rear_differential": ["fluids", "repair_manuals", "maintenance_data"],
    "center_differential": ["fluids", "repair_manuals", "maintenance_data"],
    "haldex_coupling": ["fluids", "repair_manuals", "maintenance_data"],
    "power_steering": ["fluids", "repair_manuals"],
    "brake_fluid": ["fluids", "maintenance", "repair_manuals"],
    "coolant": ["fluids", "maintenance", "repair_manuals"],
}

HIGH_RISK_UNITS = {
    "automatic_transmission",
    "cvt",
    "dct",
    "transfer_case",
    "front_differential",
    "rear_differential",
    "center_differential",
    "haldex_coupling",
}


def _normalize_key(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9а-яё]+", "_", value.casefold()).strip("_")


@lru_cache(maxsize=1)
def load_fluid_source_catalog() -> dict[str, Any]:
    if not FLUID_SOURCE_PATH.exists():
        return {}
    try:
        payload = json.loads(FLUID_SOURCE_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    selectors = payload.get("lubricant_product_selectors")
    if selectors is not None and not isinstance(selectors, list):
        return {k: v for k, v in payload.items() if k != "lubricant_product_selectors"}
    if isinstance(selectors, list):
        payload = {**payload, "lubricant_product_selectors": [selector for selector in selectors if isinstance(selector, dict)]}
    return payload


def normalize_unit(unit: str | None) -> str | None:
    if not unit:
        return None
    normalized = _normalize_key(unit)
    for canonical, aliases in UNIT_ALIASES.items():
        if normalized == canonical or normalized in {_normalize_key(alias) for alias in aliases}:
            return canonical
    return normalized


def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for source in sources:
        key = str(source.get("source_id") or source.get("url") or source.get("name") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(source)
    return deduped


def _source_routes(brand: str | None, unit: str | None, include_licensed: bool, limit: int) -> list[dict[str, Any]]:
    data_types = UNIT_DATA_TYPES.get(unit or "", ["fluids", "maintenance"])
    routes: list[dict[str, Any]] = []
    for data_type in data_types:
        result = recommend_automotive_sources(
            brand=brand,
            data_type=data_type,
            include_licensed=include_licensed,
            limit=limit,
        )
        for source in result.get("sources", []):
            if brand and source.get("category") == "oem_service_portal" and not source.get("brand_match"):
                continue
            row = dict(source)
            row["matched_data_type_route"] = data_type
            routes.append(row)
    return _dedupe_sources(routes)[: max(1, min(limit, 50))]


def _selector_sources(limit: int) -> list[dict[str, Any]]:
    selectors = load_fluid_source_catalog().get("lubricant_product_selectors", [])
    return selectors[: max(1, min(limit, 50))]


def build_fluid_maintenance_plan(
    *,
    brand: str | None = None,
    unit: str | None = None,
    vin: str | None = None,
    chassis: str | None = None,
    model: str | None = None,
    year: int | None = None,
    engine_code: str | None = None,
    transmission_code: str | None = None,
    drivetrain: str | None = None,
    market: str | None = None,
    include_licensed: bool = True,
    limit: int = 10,
) -> dict[str, Any]:
    canonical_unit = normalize_unit(unit)
    required_inputs = UNIT_REQUIREMENTS.get(canonical_unit or "", ["VIN/chassis", "market", "year", "make", "model", "unit"])
    source_catalog = load_fluid_source_catalog()
    source_routes = _source_routes(brand, canonical_unit, include_licensed, limit)
    warnings = [
        "Do not provide a capacity, viscosity, approval, or fluid spec without source-backed verification.",
        "Distinguish drain/refill, dry fill, filter replacement, pan removal, cooler-line drain, and level-check procedure.",
        "Use OEM approval/specification first; viscosity alone is not enough.",
    ]
    if canonical_unit in HIGH_RISK_UNITS:
        warnings.append("High-risk driveline unit: verify exact transmission/axle/transfer-case code before recommending fluid or quantity.")
    if not vin and not chassis:
        warnings.append("VIN or chassis/frame number is missing; treat the plan as routing only, not confirmed fitment.")
    if not canonical_unit:
        warnings.append("Unit is missing; ask which unit needs service before selecting oil/fluid.")

    missing_context: list[str] = []
    context_values = {
        "VIN/chassis": bool(vin or chassis),
        "market": bool(market),
        "year": year is not None,
        "make": bool(brand),
        "model": bool(model),
        "engine_code": bool(engine_code),
        "transmission_code": bool(transmission_code),
        "drivetrain": bool(drivetrain),
        "unit": bool(canonical_unit),
    }
    for requirement in required_inputs:
        token = requirement.split()[0].split("/")[0].strip(",").casefold()
        if token in {"vin", "vin/chassis"} and not context_values["VIN/chassis"]:
            missing_context.append(requirement)
        elif token == "market" and not market:
            missing_context.append(requirement)
        elif token == "year" and year is None:
            missing_context.append(requirement)
        elif token == "engine_code" and not engine_code:
            missing_context.append(requirement)
        elif token == "transmission_code" and not transmission_code:
            missing_context.append(requirement)
        elif token == "drivetrain" and not drivetrain:
            missing_context.append(requirement)

    return {
        "ok": True,
        "vehicle_context": {
            "vin": vin,
            "chassis": chassis,
            "market": market,
            "year": year,
            "brand": brand,
            "model": model,
            "engine_code": engine_code,
            "transmission_code": transmission_code,
            "drivetrain": drivetrain,
        },
        "unit": canonical_unit,
        "unit_input": unit,
        "required_inputs": required_inputs,
        "missing_context": list(dict.fromkeys(missing_context)),
        "source_priority": source_catalog.get("source_priority", []),
        "authority_source_routes": source_routes,
        "public_oem_owner_manual_sources": source_catalog.get("public_oem_owner_manual_sources", [])[: max(1, min(limit, 50))],
        "lubricant_product_selectors": _selector_sources(limit),
        "checks": [
            "Confirm exact unit variant by VIN/chassis before final capacity.",
            "Find OEM spec/approval and service operation before selecting brand product.",
            "Cross-check product selector against OEM spec and local market availability.",
            "Return uncertainty explicitly when only selector or non-OEM data is available.",
        ],
        "warnings": warnings,
        "stop_phrase": "Требуется проверка по OEM-сервисной информации для конкретного VIN.",
        "output_template": {
            "vehicle": "VIN/chassis, market, year, make, model, engine, transmission, drivetrain",
            "unit": "unit and service operation",
            "fluid_spec": "OEM spec/approval plus viscosity/API/ACEA/ILSAC/GL/DOT where applicable",
            "capacity": "value with fill type: oil-only/filter/drain-refill/dry fill/pan/cooler",
            "source": "source name, URL, document type, license status, date checked",
            "uncertainty": "what is still unverified",
        },
    }
