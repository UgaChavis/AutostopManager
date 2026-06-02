from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from .config import PROJECT_ROOT

REGISTRY_PATH = PROJECT_ROOT / "docs" / "agent" / "vin_oem_sources.json"

_MAKE_SOURCE_MAP: dict[str, list[str]] = {
    "BMW": [
        "partslink24 Mobile",
        "BMW AIR/ETK via AOS",
        "BMW Aftersales Online System (AOS)",
        "BMW Technical Information System",
        "partslink24 Product Info",
    ],
    "MINI": [
        "partslink24 Mobile",
        "BMW AIR/ETK via AOS",
        "BMW Aftersales Online System (AOS)",
        "BMW Technical Information System",
        "partslink24 Product Info",
    ],
    "VAG": ["partslink24 Mobile", "Volkswagen Group ETKA", "Volkswagen erWin", "Audi erWin", "partslink24 Product Info"],
    "VOLKSWAGEN": ["partslink24 Mobile", "Volkswagen Group ETKA", "Volkswagen erWin", "partslink24 Product Info"],
    "VW": ["partslink24 Mobile", "Volkswagen Group ETKA", "Volkswagen erWin", "partslink24 Product Info"],
    "AUDI": ["partslink24 Mobile", "Volkswagen Group ETKA", "Audi erWin", "partslink24 Product Info"],
    "SKODA": ["partslink24 Mobile", "Volkswagen Group ETKA", "Volkswagen erWin", "partslink24 Product Info"],
    "SEAT": ["partslink24 Mobile", "Volkswagen Group ETKA", "Volkswagen erWin", "partslink24 Product Info"],
    "CUPRA": ["partslink24 Mobile", "Volkswagen Group ETKA", "Volkswagen erWin", "partslink24 Product Info"],
    "TOYOTA": ["Toyota Japan EPC Help", "Toyota EPC Mirror", "Toyota Recall Search"],
    "LEXUS": ["Toyota Japan EPC Help", "Toyota EPC Mirror", "Toyota Recall Search"],
    "HONDA": ["Honda EPC Mirror", "Honda Recall Lookup", "partslink24 Mobile", "partslink24 Product Info"],
    "NISSAN": ["Nissan EPC Mirror", "Nissan Recall Search", "partslink24 Mobile", "partslink24 Product Info"],
    "MAZDA": ["Mazda Recall Search", "partslink24 Mobile", "partslink24 Product Info"],
    "SUBARU": ["Subaru EPC Mirror", "Subaru Recall Search", "partslink24 Mobile", "partslink24 Product Info"],
    "HYUNDAI": ["Hyundai EPC Mirror", "partslink24 Mobile", "partslink24 Product Info"],
    "KIA": ["Kia EPC Mirror", "partslink24 Mobile", "partslink24 Product Info"],
    "RENAULT": ["Renault EPC Mirror", "partslink24 Mobile", "partslink24 Product Info"],
    "SUZUKI": ["epc-data manual catalog", "PartSouq manual catalog", "Parts-Catalogs API", "17VIN API", "PARTSAPI.RU"],
    "MITSUBISHI": ["epc-data manual catalog", "Parts-Catalogs API", "17VIN API", "PARTSAPI.RU", "AUTOPOISK"],
    "CHANGAN": ["Parts-Catalogs API", "17VIN API", "PARTSAPI.RU", "AUTOPOISK"],
    "JEEP": ["Parts-Catalogs API", "17VIN API", "PARTSAPI.RU", "AUTOPOISK"],
    "MERCEDESBENZ": ["partslink24 Mobile", "partslink24 Product Info", "Parts-Catalogs API", "17VIN API", "PARTSAPI.RU"],
    "MERCEDES": ["partslink24 Mobile", "partslink24 Product Info", "Parts-Catalogs API", "17VIN API", "PARTSAPI.RU"],
}


@lru_cache(maxsize=1)
def load_source_registry() -> dict[str, Any]:
    if not REGISTRY_PATH.exists():
        return {"version": 0, "purpose": "missing", "sources": []}
    with REGISTRY_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def normalize_make(make: str | None) -> str:
    if not make:
        return ""
    return re.sub(r"[^A-Z0-9]+", "", make.upper())


def source_index() -> dict[str, dict[str, Any]]:
    registry = load_source_registry()
    index: dict[str, dict[str, Any]] = {}
    for source in registry.get("sources", []):
        name = str(source.get("name") or "").strip()
        if name:
            index[name] = source
    return index


def source_names_for_make(make: str | None) -> list[str]:
    key = normalize_make(make)
    if not key:
        return []
    for prefix, names in _MAKE_SOURCE_MAP.items():
        if key.startswith(prefix):
            return names
    return []


def sources_for_make(make: str | None) -> list[dict[str, Any]]:
    index = source_index()
    result: list[dict[str, Any]] = []
    for name in source_names_for_make(make):
        source = index.get(name)
        if source is not None:
            result.append(source)
    return result


def sources_for_inputs(*inputs: str) -> list[dict[str, Any]]:
    wanted = {item for item in inputs if item}
    registry = load_source_registry()
    result: list[dict[str, Any]] = []
    for source in registry.get("sources", []):
        source_inputs = set(source.get("inputs", []))
        if wanted & source_inputs:
            result.append(source)
    return result
