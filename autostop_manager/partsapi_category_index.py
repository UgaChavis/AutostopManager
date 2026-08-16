from __future__ import annotations

from contextlib import suppress
import json
import re
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT


DEFAULT_CATEGORY_INDEX_PATH = PROJECT_ROOT / "docs" / "agent" / "partsapi_category_index.json"


def _compact(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _tokens(value: Any) -> set[str]:
    compact = _compact(value)
    return {token for token in re.split(r"[^0-9a-zа-яё]+", compact) if token}


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value in (None, ""):
        return []
    return [str(value).strip()]


def load_partsapi_category_index(path: str | Path | None = None) -> dict[str, Any]:
    index_path = Path(path) if path else DEFAULT_CATEGORY_INDEX_PATH
    if not index_path.exists():
        return {
            "schema": "PartsApiCategoryIndexV1",
            "version": 0,
            "path": str(index_path),
            "categories": [],
            "missing": True,
        }
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "schema": "PartsApiCategoryIndexV1",
            "version": 0,
            "path": str(index_path),
            "categories": [],
            "missing": True,
            "error": "unreadable" if isinstance(exc, OSError) else "invalid_json",
            "error_detail": str(exc),
        }
    if not isinstance(payload, dict):
        return {
            "schema": "PartsApiCategoryIndexV1",
            "version": 0,
            "path": str(index_path),
            "categories": [],
            "missing": True,
            "error": "invalid_structure",
            "error_detail": type(payload).__name__,
        }
    categories = payload.get("categories")
    if categories is not None and not isinstance(categories, list):
        return {
            "schema": payload.get("schema", "PartsApiCategoryIndexV1"),
            "version": payload.get("version", 0),
            "path": str(index_path),
            "categories": [],
            "missing": True,
            "error": "invalid_categories",
            "error_detail": type(categories).__name__,
        }
    return {
        **payload,
        "path": str(index_path),
        "categories": [row for row in categories or [] if isinstance(row, dict)],
        "missing": False,
    }


def _category_text(row: dict[str, Any]) -> str:
    chunks: list[str] = []
    for key in ("cat_id", "parent_ru", "parent_en", "source"):
        if row.get(key) not in (None, ""):
            chunks.append(str(row[key]))
    for key in ("names_ru", "names_en", "synonyms", "intent_ids"):
        chunks.extend(_as_list(row.get(key)))
    return " ".join(chunks)


def _score_category(row: dict[str, Any], *, query: str | None = None, intent_id: str | None = None) -> float:
    score = 0.0
    matched = False
    if intent_id and intent_id in _as_list(row.get("intent_ids")):
        score += 3.0
        matched = True
    query_text = _compact(query)
    if query_text:
        haystack = _compact(_category_text(row))
        if query_text in haystack:
            score += 2.0
            matched = True
        query_tokens = _tokens(query_text)
        haystack_tokens = _tokens(haystack)
        if query_tokens:
            overlap = len(query_tokens & haystack_tokens) / len(query_tokens)
            if overlap:
                score += overlap
                matched = True
    if not matched:
        return 0.0
    with suppress(TypeError, ValueError):
        score += min(max(float(row.get("confidence") or 0.0), 0.0), 1.0)
    return score


def _category_digest(row: dict[str, Any], *, score: float, matched_by: list[str]) -> dict[str, Any]:
    return {
        "cat_id": str(row.get("cat_id") or "").strip(),
        "names_ru": _as_list(row.get("names_ru")),
        "names_en": _as_list(row.get("names_en")),
        "parent_ru": row.get("parent_ru"),
        "parent_en": row.get("parent_en"),
        "synonyms": _as_list(row.get("synonyms")),
        "intent_ids": _as_list(row.get("intent_ids")),
        "source": row.get("source"),
        "confidence": row.get("confidence"),
        "validation_required": bool(row.get("validation_required")),
        "score": round(score, 4),
        "matched_by": matched_by,
    }


def search_partsapi_category_index(
    query: str | None = None,
    *,
    intent_id: str | None = None,
    path: str | Path | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    index = load_partsapi_category_index(path)
    limit = max(limit, 0)
    rows = []
    for row in index.get("categories", []):
        score = _score_category(row, query=query, intent_id=intent_id)
        if score <= 0:
            continue
        matched_by = []
        if intent_id and intent_id in _as_list(row.get("intent_ids")):
            matched_by.append("intent_id")
        if query and _compact(query) and _compact(query) in _compact(_category_text(row)):
            matched_by.append("query")
        rows.append(_category_digest(row, score=score, matched_by=matched_by or ["tokens"]))
    rows.sort(key=lambda item: (-float(item.get("score") or 0.0), item.get("cat_id") or ""))
    return {
        "ok": True,
        "schema": index.get("schema", "PartsApiCategoryIndexV1"),
        "version": index.get("version", 0),
        "path": index.get("path"),
        "query": query,
        "intent_id": intent_id,
        "count": len(rows[:limit]),
        "matches": rows[:limit],
        "missing": bool(index.get("missing")),
    }


def explain_partsapi_category_for_intent(
    intent_id: str,
    *,
    query: str | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    result = search_partsapi_category_index(query, intent_id=intent_id, path=path, limit=5)
    top = result["matches"][0] if result["matches"] else None
    return {
        **result,
        "selected_category": top,
        "category_unresolved": top is None,
        "explanation": (
            "Numeric PartsAPI category selected from local category index; validate source before CRM writeback."
            if top
            else "No numeric PartsAPI category matched this intent/query."
        ),
    }


def validate_partsapi_category_index(path: str | Path | None = None) -> dict[str, Any]:
    index = load_partsapi_category_index(path)
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, row in enumerate(index.get("categories", []), start=1):
        cat_id = str(row.get("cat_id") or "").strip()
        if not cat_id or not cat_id.isdigit():
            errors.append({"position": position, "code": "invalid_cat_id", "cat_id": cat_id})
        if cat_id and cat_id in seen:
            errors.append({"position": position, "code": "duplicate_cat_id", "cat_id": cat_id})
        seen.add(cat_id)
        if not _as_list(row.get("intent_ids")):
            errors.append({"position": position, "code": "missing_intent_ids", "cat_id": cat_id})
        if not (_as_list(row.get("names_ru")) or _as_list(row.get("names_en"))):
            errors.append({"position": position, "code": "missing_names", "cat_id": cat_id})
    return {
        "ok": not errors and not bool(index.get("missing")),
        "schema": index.get("schema", "PartsApiCategoryIndexV1"),
        "version": index.get("version", 0),
        "path": index.get("path"),
        "category_count": len(index.get("categories", [])),
        "errors": errors,
        "privacy": {"secret_exposed": False, "raw_identifier_is_sensitive": False},
    }


def build_partsapi_category_index_plan(
    *,
    live: bool = False,
    vehicle_type: str = "PC",
    type_id: str | None = None,
    lang_id: int = 16,
    timeout: float = 20.0,
    max_attempts: int = 1,
) -> dict[str, Any]:
    return {
        "ok": True,
        "schema": "PartsApiCategoryIndexBuildPlanV1",
        "mode": "live_readonly" if live else "dry_run",
        "target_path": str(DEFAULT_CATEGORY_INDEX_PATH),
        "required_partsapi_operation": "search_tree",
        "request": {
            "operation": "search_tree",
            "vehicle_type": vehicle_type,
            "type_id": type_id,
            "lang_id": lang_id,
            "timeout": timeout,
            "max_attempts": max_attempts,
            "dry_run": not live,
        },
        "write_policy": "This command reports the read-only source call plan; updating the tracked fixture must be an explicit code change.",
        "privacy": {"secret_exposed": False, "raw_identifier_is_sensitive": False},
    }
