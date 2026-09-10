from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from .config import PROJECT_ROOT
from .storage import _string_list

REGISTRY_PATH = PROJECT_ROOT / "docs" / "agent" / "vin_oem_sources.json"

_JAPANESE_MAKES = frozenset(
    {
        "TOYOTA",
        "LEXUS",
        "HONDA",
        "NISSAN",
        "INFINITI",
        "MAZDA",
        "SUBARU",
        "SUZUKI",
        "MITSUBISHI",
        "DAIHATSU",
        "ISUZU",
        "HINO",
    }
)
_PUBLIC_JAPANESE_WEB_CATALOGS: tuple[dict[str, Any], ...] = (
    {
        "name": "PartSouq manual catalog",
        "kind": "portal",
        "authority": "public_commercial_catalog_store",
        "inputs": ["vin", "frame_number", "part_number", "vehicle_parameters", "part_name"],
        "outputs": [
            "oem_part_numbers",
            "diagram_url",
            "part_page_url",
            "applicability_conditions",
            "production_period",
            "model_code",
            "engine",
            "transmission",
            "market",
            "position",
            "quantity",
            "supersessions",
        ],
        "url": "https://partsouq.com/en/",
        "access_mode": "public",
        "trust_level": "public_reference",
        "adapter_status": ["manual_capture"],
        "preferred_for": ["jdm_frame", "vin_or_frame", "diagram", "oem_part_candidate"],
        "notes": (
            "Public manual catalog route: enter VIN/frame in the interactive form, then capture the matching "
            "diagram/page URL, OEM number, and applicability fields. JavaScript, cookies, or an anti-bot check may "
            "be required; do not bypass it. A captured candidate is not final fitment proof."
        ),
    },
    {
        "name": "Amayama public catalog",
        "kind": "portal",
        "authority": "public_commercial_catalog_store",
        "inputs": ["vin", "frame_number", "part_number", "vehicle_parameters", "part_name"],
        "outputs": [
            "oem_part_numbers",
            "diagram_url",
            "part_page_url",
            "applicability_conditions",
            "production_period",
            "model_code",
            "engine",
            "transmission",
            "market",
            "position",
            "quantity",
            "supersessions",
        ],
        "url": "https://www.amayama.com/en/genuine-catalogs",
        "access_mode": "public",
        "trust_level": "public_reference",
        "adapter_status": ["manual_capture"],
        "preferred_for": ["jdm_frame", "vin_or_frame", "diagram", "oem_part_candidate"],
        "notes": (
            "Public manual catalog route: choose the exact model/version or enter VIN/frame where offered, then capture "
            "the diagram/page URL, OEM number, and applicability conditions. Pages may require JavaScript or cookies; "
            "do not bypass a challenge. Catalog fitment remains a candidate until a human verifies the listed conditions."
        ),
    },
)
_PUBLIC_JAPANESE_WEB_CATALOG_NAMES = tuple(source["name"] for source in _PUBLIC_JAPANESE_WEB_CATALOGS)

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
    "VAG": [
        "partslink24 Mobile",
        "Volkswagen Group ETKA",
        "Volkswagen erWin",
        "Audi erWin",
        "partslink24 Product Info",
    ],
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
    "SUZUKI": ["epc-data manual catalog", "PartSouq manual catalog", "17VIN API", "PARTSAPI.RU"],
    "MITSUBISHI": ["epc-data manual catalog", "17VIN API", "PARTSAPI.RU", "AUTOPOISK"],
    "CHANGAN": ["17VIN API", "PARTSAPI.RU", "AUTOPOISK"],
    "JEEP": ["17VIN API", "PARTSAPI.RU", "AUTOPOISK"],
    "MERCEDESBENZ": [
        "partslink24 Mobile",
        "partslink24 Product Info",
        "17VIN API",
        "PARTSAPI.RU",
    ],
    "MERCEDES": ["partslink24 Mobile", "partslink24 Product Info", "17VIN API", "PARTSAPI.RU"],
}


@lru_cache(maxsize=1)
def load_source_registry() -> dict[str, Any]:
    if not REGISTRY_PATH.exists():
        return {"version": 0, "purpose": "missing", "sources": []}
    try:
        payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"version": 0, "purpose": "missing", "sources": []}
    if not isinstance(payload, dict):
        return {"version": 0, "purpose": "missing", "sources": []}
    sources = payload.get("sources")
    if not isinstance(sources, list):
        return {"version": payload.get("version", 0), "purpose": payload.get("purpose", "missing"), "sources": []}
    return {**payload, "sources": [source for source in sources if isinstance(source, dict)]}


def normalize_make(make: str | None) -> str:
    if not make:
        return ""
    return re.sub(r"[^A-Z0-9]+", "", make.upper())


def _runtime_sources() -> list[dict[str, Any]]:
    """Enrich the legacy registry only when its public PartSouq route is present.

    The registry predates structured diagram/applicability fields. Keeping the
    enrichment here makes existing route consumers receive the same safe
    manual-only metadata without changing their public schema or turning a
    browser-protected catalog into an automated adapter.
    """

    registry_sources = [
        dict(source) for source in load_source_registry().get("sources", []) if isinstance(source, dict)
    ]
    source_names = {str(source.get("name") or "").strip() for source in registry_sources}
    if "PartSouq manual catalog" not in source_names:
        return registry_sources

    enrichments = {source["name"]: source for source in _PUBLIC_JAPANESE_WEB_CATALOGS}
    result: list[dict[str, Any]] = []
    for source in registry_sources:
        name = str(source.get("name") or "").strip()
        result.append({**source, **enrichments[name]} if name in enrichments else source)
    for source in _PUBLIC_JAPANESE_WEB_CATALOGS:
        if source["name"] not in source_names:
            result.append(dict(source))
    return result


def source_index() -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for source in _runtime_sources():
        name = str(source.get("name") or "").strip()
        if name:
            index[name] = source
    return index


def source_names_for_make(make: str | None) -> list[str]:
    key = normalize_make(make)
    if not key:
        return []
    names: list[str] = []
    for prefix, mapped_names in _MAKE_SOURCE_MAP.items():
        if key.startswith(prefix):
            names = list(mapped_names)
            break
    if key in _JAPANESE_MAKES:
        names.extend(_PUBLIC_JAPANESE_WEB_CATALOG_NAMES)
    return list(dict.fromkeys(names))


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
    result: list[dict[str, Any]] = []
    for source in _runtime_sources():
        source_inputs = set(_string_list(source.get("inputs")))
        if wanted & source_inputs:
            result.append(source)
    return result
