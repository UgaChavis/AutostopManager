from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT


DEFAULT_CATEGORY_INDEX_PATH = PROJECT_ROOT / "docs" / "agent" / "partsapi_category_index.json"
DEFAULT_CATEGORY_INDEX_ROOT = DEFAULT_CATEGORY_INDEX_PATH.parent
MAX_CATEGORY_INDEX_BYTES = 2 * 1024 * 1024
MAX_CATEGORY_COUNT = 5_000
MAX_CATEGORY_ROW_KEYS = 64
MAX_CATEGORY_LIST_ITEMS = 256
MAX_CATEGORY_TEXT_LENGTH = 4_096
MAX_CATEGORY_JSON_DEPTH = 4
MAX_CATEGORY_JSON_NODES = 100_000


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


def _index_result(
    index_path: Path,
    *,
    error: str | None = None,
    error_detail: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "PartsApiCategoryIndexV1",
        "version": 0,
        "path": str(index_path),
        "categories": [],
        "missing": True,
    }
    if error:
        result["error"] = error
    if error_detail:
        result["error_detail"] = error_detail
    return result


def _resolve_index_path(
    path: str | Path | None,
    *,
    allowed_root: str | Path | None,
) -> tuple[Path, Path, str | None]:
    root = Path(allowed_root) if allowed_root is not None else DEFAULT_CATEGORY_INDEX_ROOT
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return root, root, "allowed_root_unavailable"

    index_path = Path(path) if path is not None else DEFAULT_CATEGORY_INDEX_PATH
    if not index_path.is_absolute():
        index_path = PROJECT_ROOT / index_path
    if index_path.suffix.casefold() != ".json":
        return index_path, resolved_root, "json_extension_required"
    try:
        lexical_path = Path(os.path.abspath(index_path))
        lexical_relative = lexical_path.relative_to(resolved_root)
        current = resolved_root
        for component in lexical_relative.parts:
            current /= component
            if current.is_symlink():
                return index_path, resolved_root, "symlink_not_allowed"
        resolved_path = index_path.resolve(strict=False)
    except ValueError:
        return index_path, resolved_root, "outside_allowed_root"
    except OSError:
        return index_path, resolved_root, "unreadable"
    if not resolved_path.is_relative_to(resolved_root):
        return index_path, resolved_root, "outside_allowed_root"
    return resolved_path, resolved_root, None


def _read_index_bytes(index_path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(index_path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("not_regular_file")
        if metadata.st_size > MAX_CATEGORY_INDEX_BYTES:
            raise ValueError("file_too_large")
        raw = os.read(descriptor, MAX_CATEGORY_INDEX_BYTES + 1)
        if len(raw) > MAX_CATEGORY_INDEX_BYTES:
            raise ValueError("file_too_large")
        return raw
    finally:
        os.close(descriptor)


def _category_structure_error(payload: dict[str, Any]) -> str | None:
    categories = payload.get("categories")
    if isinstance(categories, list) and len(categories) > MAX_CATEGORY_COUNT:
        return "too_many_categories"
    stack: list[tuple[Any, int]] = [(payload, 0)]
    visited = 0
    while stack:
        value, depth = stack.pop()
        visited += 1
        if visited > MAX_CATEGORY_JSON_NODES:
            return "json_structure_too_large"
        if depth > MAX_CATEGORY_JSON_DEPTH:
            return "json_structure_too_deep"
        if isinstance(value, dict):
            if len(value) > MAX_CATEGORY_COUNT:
                return "json_structure_too_large"
            for key, child in value.items():
                if not isinstance(key, str) or len(key) > 128:
                    return "invalid_category_key"
                stack.append((child, depth + 1))
        elif isinstance(value, list):
            if len(value) > MAX_CATEGORY_COUNT:
                return "json_structure_too_large"
            stack.extend((child, depth + 1) for child in value)
        elif isinstance(value, str):
            if len(value) > MAX_CATEGORY_TEXT_LENGTH:
                return "category_text_too_long"
        elif not isinstance(value, (int, float, bool, type(None))):
            return "invalid_category_value"

    if categories is None:
        return None
    if not isinstance(categories, list):
        return "invalid_categories"
    if len(categories) > MAX_CATEGORY_COUNT:
        return "too_many_categories"
    for row in categories:
        if not isinstance(row, dict) or len(row) > MAX_CATEGORY_ROW_KEYS:
            return "invalid_category_row"
        for key, value in row.items():
            if not isinstance(key, str) or len(key) > 128:
                return "invalid_category_key"
            values = value if isinstance(value, list) else [value]
            if isinstance(value, (dict, tuple, set)) or len(values) > MAX_CATEGORY_LIST_ITEMS:
                return "invalid_category_value"
            for item in values:
                if isinstance(item, (dict, list, tuple, set)):
                    return "invalid_category_value"
                if isinstance(item, str) and len(item) > MAX_CATEGORY_TEXT_LENGTH:
                    return "category_text_too_long"
                if not isinstance(item, (str, int, float, bool, type(None))):
                    return "invalid_category_value"
    return None


def load_partsapi_category_index(
    path: str | Path | None = None,
    *,
    allowed_root: str | Path | None = None,
) -> dict[str, Any]:
    index_path, _resolved_root, path_error = _resolve_index_path(path, allowed_root=allowed_root)
    if path_error:
        return _index_result(index_path, error=path_error)
    try:
        metadata = index_path.lstat()
    except FileNotFoundError:
        return _index_result(index_path)
    except OSError as exc:
        return _index_result(index_path, error="unreadable", error_detail=str(exc))
    if not stat.S_ISREG(metadata.st_mode):
        return _index_result(index_path, error="not_regular_file")
    if metadata.st_size > MAX_CATEGORY_INDEX_BYTES:
        return _index_result(index_path, error="file_too_large")
    try:
        payload = json.loads(_read_index_bytes(index_path).decode("utf-8-sig"))
    except ValueError as exc:
        code = str(exc)
        if code in {"not_regular_file", "file_too_large"}:
            return _index_result(index_path, error=code)
        return _index_result(index_path, error="invalid_json", error_detail=code)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        return _index_result(
            index_path,
            error="unreadable" if isinstance(exc, OSError) else "invalid_json",
            error_detail=str(exc),
        )
    if not isinstance(payload, dict):
        return _index_result(index_path, error="invalid_structure", error_detail=type(payload).__name__)
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
    structure_error = _category_structure_error(payload)
    if structure_error:
        return _index_result(index_path, error=structure_error)
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
    try:
        score += min(max(float(row.get("confidence") or 0.0), 0.0), 1.0)
    except (TypeError, ValueError):
        pass
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
    allowed_root: str | Path | None = None,
) -> dict[str, Any]:
    index = load_partsapi_category_index(path, allowed_root=allowed_root)
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
    try:
        safe_limit = max(1, min(int(limit or 8), 50))
    except (TypeError, ValueError):
        safe_limit = 8
    return {
        "ok": not bool(index.get("missing")),
        "schema": index.get("schema", "PartsApiCategoryIndexV1"),
        "version": index.get("version", 0),
        "path": index.get("path"),
        "query": query,
        "intent_id": intent_id,
        "count": len(rows[:safe_limit]),
        "matches": rows[:safe_limit],
        "missing": bool(index.get("missing")),
        "error": index.get("error"),
        "error_detail": index.get("error_detail"),
    }


def explain_partsapi_category_for_intent(
    intent_id: str,
    *,
    query: str | None = None,
    path: str | Path | None = None,
    allowed_root: str | Path | None = None,
) -> dict[str, Any]:
    result = search_partsapi_category_index(query, intent_id=intent_id, path=path, limit=5, allowed_root=allowed_root)
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


def validate_partsapi_category_index(
    path: str | Path | None = None,
    *,
    allowed_root: str | Path | None = None,
) -> dict[str, Any]:
    index = load_partsapi_category_index(path, allowed_root=allowed_root)
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
        "load_error": index.get("error"),
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
    try:
        attempts = max(1, min(int(max_attempts), 3))
    except (TypeError, ValueError):
        attempts = 1
    try:
        timeout_seconds = float(timeout)
    except (TypeError, ValueError):
        timeout_seconds = 20.0
    if timeout_seconds != timeout_seconds or timeout_seconds in {float("inf"), float("-inf")}:
        timeout_seconds = 20.0
    timeout_seconds = max(1.0, min(timeout_seconds, 30.0, 60.0 / attempts))
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
            "timeout": timeout_seconds,
            "max_attempts": attempts,
            "dry_run": not live,
        },
        "write_policy": "This command reports the read-only source call plan; updating the tracked fixture must be an explicit code change.",
        "privacy": {"secret_exposed": False, "raw_identifier_is_sensitive": False},
    }
