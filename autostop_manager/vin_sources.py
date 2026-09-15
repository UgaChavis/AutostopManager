from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from .config import PROJECT_ROOT


REGISTRY_PATH = PROJECT_ROOT / "docs" / "agent" / "vin_oem_sources.json"

PARTSOUQ_SOURCE_ID = "partsouq_catalog"
AMAYAMA_SOURCE_ID = "amayama_catalog"
PUBLIC_CATALOG_SOURCE_ALIASES: dict[str, tuple[str, ...]] = {
    PARTSOUQ_SOURCE_ID: (
        "partsouq_catalog_manual",
        "PartSouq manual catalog",
        "PartSouq public catalog",
        "PartSouq",
    ),
    AMAYAMA_SOURCE_ID: (
        "amayama_catalog_manual",
        "Amayama public catalog",
        "Amayama manual catalog",
        "Amayama",
    ),
}


def _source_reference_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


_PUBLIC_CATALOG_ALIAS_INDEX = {
    _source_reference_key(reference): source_id
    for source_id, aliases in PUBLIC_CATALOG_SOURCE_ALIASES.items()
    for reference in (source_id, *aliases)
}


def canonical_source_id(value: Any) -> str:
    """Collapse public-catalog legacy names/IDs onto one stable source ID."""

    raw = str(value or "").strip()
    return _PUBLIC_CATALOG_ALIAS_INDEX.get(_source_reference_key(raw), raw)


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
_CANONICAL_MAKE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("TOYOTAMOTOR", "TOYOTA"),
    ("MITSUBISHIMOTORS", "MITSUBISHI"),
)

_MAKE_SOURCE_MAP: dict[str, list[str]] = {
    "BMW": [
        "BMW AIR/ETK via AOS",
        "BMW Aftersales Online System (AOS)",
        "BMW Technical Information System",
    ],
    "MINI": [
        "BMW AIR/ETK via AOS",
        "BMW Aftersales Online System (AOS)",
        "BMW Technical Information System",
    ],
    "VAG": [
        "Volkswagen Group ETKA",
        "Volkswagen erWin",
        "Audi erWin",
    ],
    "VOLKSWAGEN": ["Volkswagen Group ETKA", "Volkswagen erWin"],
    "VW": ["Volkswagen Group ETKA", "Volkswagen erWin"],
    "AUDI": ["Volkswagen Group ETKA", "Audi erWin"],
    "SKODA": ["Volkswagen Group ETKA", "Volkswagen erWin"],
    "SEAT": ["Volkswagen Group ETKA", "Volkswagen erWin"],
    "CUPRA": ["Volkswagen Group ETKA", "Volkswagen erWin"],
    "TOYOTA": ["Toyota Japan EPC Help", "Toyota EPC Mirror", "Toyota Recall Search"],
    "LEXUS": ["Toyota Japan EPC Help", "Toyota EPC Mirror", "Toyota Recall Search"],
    "HONDA": ["Honda EPC Mirror", "Honda Recall Lookup"],
    "NISSAN": ["Nissan EPC Mirror", "Nissan Recall Search"],
    "MAZDA": ["Mazda Recall Search"],
    "SUBARU": ["Subaru EPC Mirror", "Subaru Recall Search"],
    "HYUNDAI": ["Hyundai EPC Mirror"],
    "KIA": ["Kia EPC Mirror"],
    "RENAULT": ["Renault EPC Mirror"],
    "SUZUKI": ["epc-data manual catalog", "PartSouq manual catalog", "PARTSAPI.RU"],
    "MITSUBISHI": ["epc-data manual catalog", "PARTSAPI.RU"],
    "CHANGAN": ["PARTSAPI.RU"],
    "JEEP": ["PARTSAPI.RU"],
    "MERCEDESBENZ": [
        "PARTSAPI.RU",
    ],
    "MERCEDES": ["PARTSAPI.RU"],
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
    key = re.sub(r"[^A-Z0-9]+", "", make.upper())
    for prefix, canonical in _CANONICAL_MAKE_PREFIXES:
        if key.startswith(prefix):
            return canonical
    return key


def source_index() -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for source in load_source_registry().get("sources", []):
        references = [source.get("source_id"), source.get("name"), *_string_list(source.get("aliases"))]
        for reference in references:
            raw = str(reference or "").strip()
            if raw:
                index[raw] = source
                index[canonical_source_id(raw)] = source
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
        names.extend((PARTSOUQ_SOURCE_ID, AMAYAMA_SOURCE_ID))
    return list(dict.fromkeys(names))


def sources_for_make(make: str | None) -> list[dict[str, Any]]:
    index = source_index()
    result: list[dict[str, Any]] = []
    emitted_source_ids: set[str] = set()
    for reference in source_names_for_make(make):
        source = index.get(reference)
        if source is None:
            continue
        source_id = canonical_source_id(source.get("source_id") or source.get("name"))
        if source_id in emitted_source_ids:
            continue
        emitted_source_ids.add(source_id)
        result.append(source)
    return result


def sources_for_inputs(*inputs: str) -> list[dict[str, Any]]:
    wanted = {item for item in inputs if item}
    result: list[dict[str, Any]] = []
    for source in load_source_registry().get("sources", []):
        source_inputs = set(_string_list(source.get("inputs")))
        if wanted & source_inputs:
            result.append(source)
    return result


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values: list[Any] = [value]
    elif isinstance(value, dict):
        return []
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
    else:
        text = str(value).strip()
        return [text] if text else []
    result: list[str] = []
    for item in values:
        text = str(item).strip()
        if text:
            result.append(text)
    return result
