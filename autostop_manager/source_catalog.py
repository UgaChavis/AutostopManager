from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT

CATALOG_DIR = PROJECT_ROOT / "docs" / "agent" / "automotive_sources"
SOURCE_CATALOG_PATH = CATALOG_DIR / "automotive_repair_sources_catalog.json"
OPEN_DATASET_ENDPOINTS_PATH = CATALOG_DIR / "open_dataset_endpoints.json"

_LICENSED_STATUSES = {
    "licensed_subscription_required",
    "commercial_license_required",
    "commercial_or_library_license_required",
    "book_purchase_required",
    "standards_purchase_required",
    "paid_training_license_required",
    "license_dependent",
    "licensed_or_link_only",
    "link_or_license_dependent",
}

_SAFETY_CRITICAL_DATA_TYPES = {
    "adas",
    "adas_safety_features",
    "brake",
    "brakes",
    "calibration",
    "diagnostics_reprogramming",
    "electrical_diagrams",
    "fuel_system",
    "hv",
    "hybrid_drive",
    "immobilizer_codes",
    "pcm_programming",
    "programming",
    "srs",
    "steering",
    "suspension",
    "wiring_diagrams",
}

_DATA_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "timing": ("repair_procedures", "torque_specs", "special_tools", "service_information"),
    "timing_belt": ("repair_procedures", "torque_specs", "special_tools"),
    "timing_chain": ("repair_procedures", "torque_specs", "special_tools"),
    "camshaft_timing": ("repair_procedures", "torque_specs", "special_tools"),
    "maintenance_intervals": ("maintenance", "maintenance_intervals", "service_information"),
    "service_intervals": ("maintenance", "maintenance_intervals", "service_information"),
}


def _normalize_key(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _key_tokens(value: str) -> list[str]:
    return [token for token in _normalize_key(value).split("_") if token]


def _token_matches(query_token: str, key_tokens: list[str]) -> bool:
    if query_token in key_tokens:
        return True
    if len(query_token) < 4:
        return False
    return any(key_token.startswith(query_token) or query_token.startswith(key_token) for key_token in key_tokens)


def _matches_map_key(query: str, key: str) -> bool:
    query_tokens = _key_tokens(query)
    key_tokens = _key_tokens(key)
    if not query_tokens or not key_tokens:
        return False
    return all(_token_matches(token, key_tokens) for token in query_tokens)


def _read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return fallback
    return payload if isinstance(payload, dict) else fallback


@lru_cache(maxsize=1)
def load_source_catalog() -> dict[str, Any]:
    payload = _read_json(SOURCE_CATALOG_PATH, {"sources": [], "source_count": 0})
    sources = payload.get("sources")
    if not isinstance(sources, list):
        return {"sources": [], "source_count": 0}
    return {**payload, "sources": [source for source in sources if isinstance(source, dict)]}


@lru_cache(maxsize=1)
def load_brand_source_map() -> dict[str, list[dict[str, Any]]]:
    return _project_source_map("brands")


@lru_cache(maxsize=1)
def load_data_type_source_map() -> dict[str, list[dict[str, Any]]]:
    return _project_source_map("data_types")


def _project_source_map(dimension: str) -> dict[str, list[dict[str, Any]]]:
    """Derive routing maps from the canonical source catalog.

    Keeping separate generated JSON projections made documentation reviews
    noisy and allowed the maps to drift from the catalog.
    """
    fields = ("name", "category", "access", "priority_score_1_5", "legal_ingestion_status", "url")
    result: dict[str, list[dict[str, Any]]] = {}
    for source in load_source_catalog().get("sources", []):
        source_id = _source_id(source)
        if not source_id:
            continue
        row = {"source_id": source_id, **{field: source.get(field) for field in fields}}
        values = source.get(dimension)
        if not isinstance(values, list):
            continue
        for value in values:
            key = str(value or "").strip()
            if key:
                result.setdefault(key, []).append(row)
    return result


@lru_cache(maxsize=1)
def load_open_dataset_endpoints() -> dict[str, Any]:
    payload = _read_json(OPEN_DATASET_ENDPOINTS_PATH, {"endpoints": []})
    endpoints = payload.get("endpoints")
    if not isinstance(endpoints, list):
        return {"endpoints": []}
    normalized = []
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        row = dict(endpoint)
        if not row.get("source_id") and row.get("id"):
            row["source_id"] = row["id"]
        if not row.get("url") and row.get("url_template"):
            row["url"] = row["url_template"]
        normalized.append(row)
    return {**payload, "endpoints": normalized}


def _source_id(source: dict[str, Any]) -> str:
    return str(source.get("source_id") or source.get("id") or "").strip()


def _source_index() -> dict[str, dict[str, Any]]:
    return {_source_id(source): source for source in load_source_catalog().get("sources", []) if _source_id(source)}


def _find_map_values(
    mapping: dict[str, list[dict[str, Any]]], query: str | None
) -> tuple[str | None, list[dict[str, Any]]]:
    if not query:
        return None, []
    normalized = _normalize_key(query)
    for key, values in mapping.items():
        if _normalize_key(key) == normalized:
            return key, [value for value in values if isinstance(value, dict)] if isinstance(values, list) else []
    for key, values in mapping.items():
        key_normalized = _normalize_key(key)
        if normalized and _matches_map_key(normalized, key_normalized):
            return key, [value for value in values if isinstance(value, dict)] if isinstance(values, list) else []
    return None, []


def _is_licensed(source: dict[str, Any]) -> bool:
    status = str(source.get("legal_ingestion_status") or "").strip()
    access = str(source.get("access") or "").strip()
    return status in _LICENSED_STATUSES or access.startswith("paid")


def _citation_shape(source: dict[str, Any]) -> dict[str, str]:
    return {
        "source_id": _source_id(source),
        "source_url": str(source.get("url") or ""),
        "document_type": str(source.get("category") or source.get("source_type") or ""),
        "license_status": str(source.get("legal_ingestion_status") or ""),
    }


def _score_source(source: dict[str, Any], *, brand_match: bool, data_type_match: bool) -> int:
    score = int(source.get("priority_score_1_5") or 0) * 10
    # For a make-specific request, a source which matches both the requested
    # make and data type must outrank another maker's generic/OEM portal. A
    # brand-only official source remains the useful fallback when the catalog
    # does not have a precise data-type tag yet.
    if brand_match and data_type_match:
        score += 120
    elif brand_match:
        score += 70
    elif data_type_match:
        score += 20
    if source.get("category") == "oem_service_portal":
        score += 4
    if source.get("category") == "open_government_data":
        score += 2
    if bool(source.get("forum_or_unofficial")):
        score -= 50
    return score


def recommend_automotive_sources(
    *,
    brand: str | None = None,
    data_type: str | None = None,
    include_licensed: bool = True,
    limit: int = 10,
) -> dict[str, Any]:
    """Return source routes for a repair question without copying source content."""
    brand_key, brand_sources = _find_map_values(load_brand_source_map(), brand)
    data_type_key, data_type_sources = _find_map_values(load_data_type_source_map(), data_type)
    normalized_data_type = _normalize_key(data_type)
    if not data_type_sources and normalized_data_type in _DATA_TYPE_ALIASES:
        data_type_key = normalized_data_type
        source_map = load_data_type_source_map()
        merged: dict[str, dict[str, Any]] = {}
        for alias in _DATA_TYPE_ALIASES[normalized_data_type]:
            _, alias_sources = _find_map_values(source_map, alias)
            for source in alias_sources:
                source_id = _source_id(source)
                if source_id:
                    merged.setdefault(source_id, source)
        data_type_sources = list(merged.values())
    catalog = _source_index()

    by_id: dict[str, dict[str, Any]] = {}
    brand_ids = {_source_id(source) for source in brand_sources if _source_id(source)}
    data_type_ids = {_source_id(source) for source in data_type_sources if _source_id(source)}
    brand_order = {_source_id(source): index for index, source in enumerate(brand_sources) if _source_id(source)}
    data_type_order = {
        _source_id(source): index for index, source in enumerate(data_type_sources) if _source_id(source)
    }
    for source in [*brand_sources, *data_type_sources]:
        source_id = _source_id(source)
        if not source_id:
            continue
        merged = dict(catalog.get(source_id, {}))
        merged.update(source)
        if not include_licensed and _is_licensed(merged):
            continue
        by_id[source_id] = merged

    if not by_id and not brand and not data_type:
        for source in load_source_catalog().get("sources", []):
            if include_licensed or not _is_licensed(source):
                by_id[_source_id(source)] = source

    rows: list[dict[str, Any]] = []
    for source_id, source in by_id.items():
        brand_match = source_id in brand_ids
        data_type_match = source_id in data_type_ids
        rows.append(
            {
                "source_id": source_id,
                "name": source.get("name", ""),
                "category": source.get("category", ""),
                "access": source.get("access", ""),
                "legal_ingestion_status": source.get("legal_ingestion_status", ""),
                "priority_score_1_5": source.get("priority_score_1_5", 0),
                "recommended_ingestion_route": source.get("recommended_ingestion_route", ""),
                "url": source.get("url", ""),
                "brand_match": brand_match,
                "data_type_match": data_type_match,
                "requires_license": _is_licensed(source),
                "citation": _citation_shape(source),
                "_score": _score_source(source, brand_match=brand_match, data_type_match=data_type_match),
                "_order": min(brand_order.get(source_id, 10_000), data_type_order.get(source_id, 10_000)),
            }
        )
    rows.sort(key=lambda item: (-int(item["_score"]), int(item["_order"]), str(item["source_id"])))
    for item in rows:
        item.pop("_score", None)
        item.pop("_order", None)

    warnings: list[str] = []
    if brand and not brand_key:
        warnings.append(f"No exact brand route found for {brand}; use multi-brand and official sources.")
    if data_type and not data_type_key:
        warnings.append(f"No exact data-type route found for {data_type}; use repair source playbook.")
    if not include_licensed and (brand_key or data_type_key) and not rows:
        warnings.append("No open-only source route remained after licensed or license-dependent sources were filtered.")
    if normalized_data_type in _SAFETY_CRITICAL_DATA_TYPES:
        warnings.append("Safety-critical route: use OEM or licensed professional sources only.")

    endpoints = []
    if data_type and _normalize_key(data_type) in {"recalls", "technical_service_bulletins", "tsb", "vin_decode"}:
        endpoints = load_open_dataset_endpoints().get("endpoints", [])

    return {
        "ok": True,
        "brand": brand,
        "matched_brand_key": brand_key,
        "data_type": data_type,
        "matched_data_type_key": data_type_key,
        "include_licensed": include_licensed,
        "sources": rows[: max(1, min(limit, 50))],
        "open_dataset_endpoints": endpoints,
        "warnings": warnings,
        "rules": [
            "Use OEM service information first for VIN-specific repair facts.",
            "Do not copy paid manuals, standards, wiring diagrams, or professional database records without license.",
            "Carry source_id, source_url, document_type, and license_status with technical facts.",
        ],
    }
