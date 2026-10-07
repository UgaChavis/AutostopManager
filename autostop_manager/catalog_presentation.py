"""Bound catalog display after the provider's full parsing and safety checks."""

from __future__ import annotations

import re
from typing import Any, Literal

CatalogDetail = Literal["summary", "full"]
SUMMARY_LIST_LIMIT = 25
_NORMALIZED_LISTS = (
    "vehicle_profiles",
    "oem_candidates",
    "cross_candidates",
    "article_candidates",
    "autonorms_rows",
    "fill_volumes",
    "search_tree_rows",
    "article_criteria_rows",
)
_PREVIEW_FIELDS = frozenset(
    {
        "id",
        "name",
        "title",
        "label",
        "makeid",
        "makename",
        "modelid",
        "modelname",
        "carid",
        "cartype",
        "cartypes",
        "carname",
        "brandid",
        "brandname",
        "manufacturerid",
        "manufacturername",
        "manuid",
        "manuname",
        "typeid",
        "typename",
        "number",
        "partnumber",
        "artid",
        "artnum",
        "supid",
        "supname",
        "strid",
        "strdes",
        "groupid",
        "groupname",
    }
)
_VIN = re.compile(r"[A-HJ-NPR-Z0-9]{17}", re.IGNORECASE)


def invalid_catalog_detail(detail: Any) -> dict[str, Any] | None:
    if detail in ("summary", "full"):
        return None
    return {
        "ok": False,
        "outcome": "invalid_detail",
        "failure_class": "invalid_detail",
        "error": "detail must be summary or full.",
        "available_details": ["summary", "full"],
        "attempt_count": 0,
        "attempts": [],
        "retryable": False,
    }


def catalog_execution(payload: dict[str, Any], *, elapsed_ms: float, dry_run: bool = False) -> dict[str, Any]:
    """Preserve backend accounting; legacy attempt_count counts actual HTTP attempts."""
    output = dict(payload)
    execution = dict(payload.get("execution") or {})
    processing = dict(payload.get("processing") or {})
    reused = execution.get("reused", processing.get("reused", payload.get("reused", False)))
    attempts = execution.get("attempts", payload.get("attempts", []))
    calls = 0 if dry_run or payload.get("dry_run") or reused else payload.get("attempt_count", len(attempts))
    outcome = payload.get("outcome", "success" if payload.get("ok") else "invalid_input")
    completeness = payload.get(
        "completeness",
        "not_requested"
        if dry_run or payload.get("dry_run")
        else "partial"
        if payload.get("requires_fallback")
        else "unknown",
    )
    for key, value in {
        "network_calls": calls,
        "attempts": attempts,
        "elapsed_ms": round(elapsed_ms, 3),
        "outcome": outcome,
        "completeness": completeness,
        "reused": reused,
    }.items():
        execution.setdefault(key, processing.get(key, value))
    processing.setdefault("elapsed_ms", execution["elapsed_ms"])
    processing.setdefault("outcome", execution["outcome"])
    processing.setdefault("completeness", execution["completeness"])
    processing.setdefault("reused", execution["reused"])
    output["execution"] = execution
    output["processing"] = processing
    return output


def _preview_rows(value: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 8:
        return []
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        for key in ("data", "result", "array", "items", "rows", "records"):
            if key in value:
                rows = _preview_rows(value[key], depth + 1)
                if rows:
                    return rows
    return []


def present_catalog(payload: dict[str, Any], detail: CatalogDetail = "full") -> dict[str, Any]:
    if detail == "full":
        return payload
    metadata: dict[str, dict[str, Any]] = {}

    def compact(value: Any, path: str) -> Any:
        if isinstance(value, list):
            total = len(value)
            metadata[path] = {
                "total": total,
                "returned": min(total, SUMMARY_LIST_LIMIT),
                "truncated": total > SUMMARY_LIST_LIMIT,
            }
            return [compact(row, f"{path}[{index}]") for index, row in enumerate(value[:SUMMARY_LIST_LIMIT])]
        if isinstance(value, dict):
            output = {}
            for key, nested in value.items():
                if key in {"payload", "raw_payload", "raw_response", "raw_keys"}:
                    continue
                if key == "request_plan" and isinstance(nested, dict):
                    # Configuration details and echoed inputs remain available in full.
                    # Summary retains failures needed to choose a supported next call.
                    nested = {
                        field: nested[field]
                        for field in ("ok", "configured", "partsapi_method", "missing_env_names", "error")
                        if field in nested
                    }
                if key == "operation_status" and isinstance(nested, dict):
                    nested = [
                        {
                            "operation": operation,
                            **{
                                field: status[field]
                                for field in ("configured", "outcome", "required_params", "missing_env_names")
                                if field in status
                            },
                        }
                        for operation, status in nested.items()
                    ]
                output[key] = compact(nested, f"{path}.{key}" if path else key)
            return output
        return value

    source = dict(payload)
    if not any(payload.get(key) for key in _NORMALIZED_LISTS):
        rows = _preview_rows(payload.get("payload"))
        previews = [
            {
                key: _VIN.sub("[REDACTED_IDENTIFIER]", value) if isinstance(value, str) else value
                for key, value in row.items()
                if str(key).replace("_", "").casefold() in _PREVIEW_FIELDS
                and isinstance(value, (str, int, float, bool))
            }
            for row in rows
        ]
        previews = [row for row in previews if row]
        if previews:
            source["data_preview"] = {
                "records": previews,
                "source_total": len(rows),
                "previewable_total": len(previews),
                "fitment_confirmed": False,
            }
    execution = source.get("execution")
    if isinstance(execution, dict) and source.get("attempts") == execution.get("attempts"):
        source.pop("attempts", None)
    output = compact(source, "")
    output["presentation"] = {"detail": "summary", "list_limit": SUMMARY_LIST_LIMIT, "lists": metadata}
    return output
