from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, get_db_path


WORKFLOW_TERMINAL_STATES = {"completed", "failed", "cancelled"}
WORKFLOW_TRANSITIONS = {
    "planned": {"executing", "failed", "cancelled"},
    "executing": {"external_wait", "verifying", "compensating", "failed", "cancelled"},
    "external_wait": {"executing", "verifying", "compensating", "failed", "cancelled"},
    "verifying": {"completed", "compensating", "failed", "cancelled"},
    "compensating": {"completed", "failed", "cancelled"},
    # Compatibility for runs created by the v1 ledger.
    "running": {"executing", "external_wait", "verifying", "compensating", "completed", "failed", "cancelled"},
}

_VERIFICATION_FAILURE_BOOL_KEYS = {
    "executor_ok",
    "executor_success",
    "execution_ok",
    "execution_success",
    "verification_ok",
    "verification_passed",
    "verified",
    "passed",
}
_VERIFICATION_FAILURE_CONTEXT_TOKENS = {
    "executor",
    "execution",
    "verification",
    "verify",
    "readback",
    "check",
}
_VERIFICATION_FAILURE_STRINGS = {
    "blocked",
    "error",
    "failed",
    "failure",
    "false",
    "invalid",
    "not_passed",
    "rejected",
}

EXTERNAL_REF_KEYS = {
    "message_id",
    "message_ids",
    "thread_id",
    "thread_ids",
    "draft_id",
    "attachment_id",
    "attachment_ids",
    "file_id",
    "file_ids",
    "label_id",
    "label_ids",
    "external_ref",
    "provider",
    "status",
    "sent_at",
    "completed_at",
    "recipient_count",
    "subject_hash",
    "error_code",
}
EXTERNAL_BODY_KEYS = {
    "body",
    "body_text",
    "body_html",
    "html",
    "content",
    "raw",
    "raw_body",
    "message_body",
    "thread_body",
    "snippet",
}

ACTIVE_WORKFLOW_STATES = {"planned", "executing", "external_wait", "verifying", "compensating", "running"}
STORE_CHECKPOINT_STREAMS = frozenset({"store_digest", "store_bootstrap"})
STORE_CHECKPOINT_REF_KEYS = {"entity", "id", "version", "updated_at"}
STORE_LEDGER_REF_ENTITIES = frozenset(
    {
        "store_batch",
        "store_marketplace_listing",
        "store_order",
        "store_part",
        "store_quote_request",
        "store_state",
        "store_supplier",
        "store_warehouse_operation",
    }
)
STORE_WORKFLOW_OPERATIONS = frozenset(
    {
        "assign_quote_request",
        "set_quote_request_status",
        "update_quote_request_comment",
        "set_batch_storage_location",
        "mark_order_ready",
    }
)
STORE_LEDGER_SAFE_CHECKPOINT_KEYS = {
    "baseline",
    "compact_refs",
    "counts",
    "cursor",
    "entity",
    "error_code",
    "last_success_at",
    "mode",
    "next_action",
    "operation",
    "page_count",
    "pages_complete",
    "phase",
    "snapshot_at",
    "state_version",
    "status",
    "target_id",
    "target_version",
    "verification",
}
STORE_LEDGER_SAFE_START_KEYS = {
    "compact_refs",
    "contract_id",
    "correlation_id",
    "counts",
    "domain",
    "dry_run_proof_expires_at",
    "dry_run_proof_ttl_seconds",
    "error_code",
    "idempotency_key",
    "mode",
    "operation",
    "request_fingerprint",
    "request_id",
    "source",
    "state_version",
    "status",
    "target_id",
    "target_version",
    "updated_at",
    "verification",
    "workflow_id",
}
STORE_LEDGER_SAFE_EVENT_TYPES = frozenset(
    {
        "checkpoint",
        "compensation",
        "planned_action",
        "preflight",
        "reconciliation",
        "risk",
        "skip",
        "state_transition",
        "verification",
        "workflow_started",
        "write",
    }
)
_STORE_LEDGER_FORBIDDEN_KEYS = {
    "address",
    "client",
    "clients",
    "comment",
    "content",
    "changes",
    "customer",
    "customers",
    "description",
    "email",
    "emails",
    "item",
    "items",
    "line",
    "line_items",
    "lines",
    "order",
    "order_items",
    "orders",
    "payload",
    "planned_changes",
    "phone",
    "phones",
    "product",
    "products",
    "raw",
    "raw_payload",
    "refresh_token",
    "response",
    "result",
    "rows",
    "stock",
    "stock_rows",
    "secret",
    "token",
    "password",
    "authorization",
    "access_token",
    "api_key",
    "warehouse_rows",
}
_STORE_SENSITIVE_KEY_TOKENS = {
    "address",
    "client",
    "comment",
    "customer",
    "description",
    "email",
    "item",
    "line",
    "location",
    "order",
    "phone",
    "product",
    "stock",
    "secret",
    "token",
    "password",
    "authorization",
    "vin",
    "license",
}
_STORE_VERIFICATION_STRING_KEYS = {
    "entity",
    "error_code",
    "mode",
    "operation",
    "phase",
    "status",
    "target_id",
    "target_version",
}
_STORE_MACHINE_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-=]{0,4095}$")
_STORE_SECRET_VALUE_RE = re.compile(
    r"^(?:sk[-_]|gh[opusr]_|github_pat_|xox[a-z]-|aiza|akia[0-9a-z]{8,}|ya29\.)",
    re.IGNORECASE,
)
_STORE_JWT_VALUE_RE = re.compile(r"^eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_STORE_VIN_VALUE_RE = re.compile(r"^(?=.*[A-HJ-NPR-Z])(?=.*[0-9])[A-HJ-NPR-Z0-9]{17}$", re.IGNORECASE)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _required_lastrowid(cursor: sqlite3.Cursor) -> int:
    row_id = cursor.lastrowid
    if row_id is None:
        raise RuntimeError("SQLite insert did not return a row id")
    return row_id


def _json_list(values: list[str] | None) -> str:
    return json.dumps(values or [], ensure_ascii=False)


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


def _decode_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _verification_failure_paths(value: Any, *, prefix: str = "") -> list[str]:
    """Return explicit executor/readback failure markers from completion evidence."""

    failures: list[str] = []
    if isinstance(value, dict):
        for raw_key, nested in value.items():
            key = str(raw_key or "").strip().casefold().replace("-", "_")
            path = f"{prefix}.{key}" if prefix else key
            path_tokens = set(path.replace(".", "_").split("_"))
            failure_context = bool(path_tokens & _VERIFICATION_FAILURE_CONTEXT_TOKENS)
            if nested is False and (
                key in _VERIFICATION_FAILURE_BOOL_KEYS
                or bool(set(key.split("_")) & _VERIFICATION_FAILURE_CONTEXT_TOKENS)
                or (failure_context and key in {"ok", "passed", "success", "verified"})
            ):
                failures.append(path)
            elif (
                isinstance(nested, str)
                and nested.strip().casefold().replace(" ", "_") in _VERIFICATION_FAILURE_STRINGS
                and (failure_context or key in {"executor", "execution", "verification", "readback"})
            ):
                failures.append(path)
            failures.extend(_verification_failure_paths(nested, prefix=path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            failures.extend(_verification_failure_paths(nested, prefix=f"{prefix}[{index}]"))
    return failures


def _workflow_state_conflict(
    run_id: int,
    *,
    expected_state_version: int | None,
    current_state_version: int,
) -> dict[str, Any] | None:
    if expected_state_version is None or int(expected_state_version) == current_state_version:
        return None
    return {
        "ok": False,
        "error": "workflow_state_conflict",
        "run_id": run_id,
        "expected_state_version": int(expected_state_version),
        "current_state_version": current_state_version,
    }


def _completion_verification_error(
    run_id: int, *, current_status: str, verification: dict[str, Any]
) -> dict[str, Any] | None:
    if not verification:
        return {
            "ok": False,
            "error": "verification_required_before_completion",
            "run_id": run_id,
            "status": current_status,
        }
    failure_paths = sorted(set(_verification_failure_paths(verification)))
    if failure_paths:
        return {
            "ok": False,
            "error": "verification_failed_before_completion",
            "run_id": run_id,
            "status": current_status,
            "failure_paths": failure_paths,
        }
    return None


def _unique_string_values(values: list[str] | None, *, limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _find_forbidden_body_keys(value: Any, *, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for raw_key, nested in value.items():
            key = str(raw_key or "").strip().casefold()
            path = f"{prefix}.{key}" if prefix else key
            if key in EXTERNAL_BODY_KEYS or "body" in key:
                found.append(path)
            found.extend(_find_forbidden_body_keys(nested, prefix=path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found.extend(_find_forbidden_body_keys(nested, prefix=f"{prefix}[{index}]"))
    return list(dict.fromkeys(found))


def _store_workflow_operation(
    *,
    workflow_id: str = "",
    intent: str = "",
    scope: dict[str, Any] | None = None,
) -> str:
    scope_payload = scope if isinstance(scope, dict) else {}
    candidates = [str(scope_payload.get("operation") or "").strip().casefold()]
    normalized_workflow_id = str(workflow_id or "").strip().casefold()
    normalized_intent = str(intent or "").strip().casefold()
    if normalized_workflow_id.startswith("inventory:"):
        candidates.append(normalized_workflow_id.partition(":")[2])
    if normalized_intent.startswith("inventory_"):
        candidates.append(normalized_intent.removeprefix("inventory_"))
    for candidate in candidates:
        if candidate in STORE_WORKFLOW_OPERATIONS:
            return candidate
    return ""


def _is_store_workflow(*, workflow_id: str = "", intent: str = "", scope: dict[str, Any] | None = None) -> bool:
    if _store_workflow_operation(workflow_id=workflow_id, intent=intent, scope=scope):
        return True
    identifiers = {str(workflow_id or "").casefold(), str(intent or "").casefold()}
    if any(value.startswith(("store_", "store-", "store:")) for value in identifiers):
        return True
    scope_payload = scope if isinstance(scope, dict) else {}
    return any("store" in str(scope_payload.get(key) or "").casefold() for key in ("domain", "source", "workflow_id"))


def _store_machine_value_is_safe(value: Any) -> bool:
    if not isinstance(value, str):
        return isinstance(value, (bool, int)) or value is None or (isinstance(value, float) and math.isfinite(value))
    normalized = value.strip()
    if not normalized or len(normalized) > 4096 or _STORE_MACHINE_VALUE_RE.fullmatch(normalized) is None:
        return False
    if re.fullmatch(r"\d{10,15}", normalized) is not None:
        return False
    if _STORE_SECRET_VALUE_RE.match(normalized) is not None or _STORE_JWT_VALUE_RE.fullmatch(normalized) is not None:
        return False
    return _STORE_VIN_VALUE_RE.fullmatch(normalized) is None


def _find_unsafe_store_machine_values(value: Any, *, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for raw_key, nested in value.items():
            key = str(raw_key or "").strip().casefold().replace("-", "_")
            path = f"{prefix}.{key}" if prefix else key
            found.extend(_find_unsafe_store_machine_values(nested, prefix=path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found.extend(_find_unsafe_store_machine_values(nested, prefix=f"{prefix}[{index}]"))
    elif not _store_machine_value_is_safe(value):
        found.append(prefix or "value")
    return list(dict.fromkeys(found))


def _store_start_channel_forbidden(
    *,
    workflow_id: str,
    intent: str,
    query: str,
    request_id: str,
    idempotency_key: str,
    correlation_id: str,
    actor: str,
    source: str,
    scope: dict[str, Any],
    metadata: dict[str, Any],
    selected_ids: list[str],
) -> list[str]:
    operation = _store_workflow_operation(workflow_id=workflow_id, intent=intent, scope=scope)
    allowed_workflow_ids = {"store_management", "store_management_workflow"}
    allowed_intents = {"store_management", "store_management_workflow"}
    if operation:
        allowed_workflow_ids.update({f"inventory:{operation}", f"store:{operation}", f"store_{operation}"})
        allowed_intents.update({f"inventory_{operation}", f"store_{operation}"})

    forbidden: list[str] = []
    if str(query or "").strip():
        forbidden.append("query")
    if str(workflow_id or "").strip().casefold() not in allowed_workflow_ids:
        forbidden.append("workflow_id")
    if str(intent or "").strip().casefold() not in allowed_intents:
        forbidden.append("intent")
    for container_name, payload in (("scope", scope), ("metadata", metadata)):
        forbidden.extend(
            f"{container_name}.{key}" for key in sorted(set(payload).difference(STORE_LEDGER_SAFE_START_KEYS))
        )
    forbidden.extend(_find_unsafe_store_machine_values({"scope": scope, "metadata": metadata}))
    forbidden.extend(_find_unsafe_store_machine_values(selected_ids, prefix="selected_ids"))
    forbidden.extend(
        _find_unsafe_store_machine_values(
            {
                "request_id": request_id,
                "idempotency_key": idempotency_key,
                "correlation_id": correlation_id,
                "actor": actor,
                "source": source,
            }
        )
    )
    return list(dict.fromkeys(forbidden))


def _store_message_is_allowed(message: str, *, operation: str) -> bool:
    normalized = str(message or "").strip()
    if not normalized:
        return True
    if normalized in {
        "workflow resumed",
        "workflow cancelled",
        "workflow finished through compatibility API",
    }:
        return True
    if not operation:
        return False
    return normalized in {
        f"execute {operation}",
        f"verify {operation}",
        f"completed {operation}",
        f"failed {operation}",
        f"verification failed after executor applied {operation}",
        f"ledger close reconciliation required for {operation}",
    }


def _store_summary_is_allowed(summary: str, *, operation: str) -> bool:
    normalized = str(summary or "").strip().casefold()
    if not normalized:
        return True
    allowed = {"store_management", "store_management_workflow"}
    if operation:
        allowed.update({f"inventory:{operation}", f"store:{operation}"})
    return normalized in allowed


def _find_forbidden_store_payload_keys(value: Any, *, prefix: str = "") -> list[str]:
    """Reject raw store business payload while permitting compact technical refs."""

    found: list[str] = []
    if isinstance(value, dict):
        for raw_key, nested in value.items():
            key = str(raw_key or "").strip().casefold().replace("-", "_")
            path = f"{prefix}.{key}" if prefix else key
            if key == "compact_refs":
                if not isinstance(nested, list):
                    found.append(path)
                    continue
                for index, item in enumerate(nested):
                    if (
                        not isinstance(item, dict)
                        or not set(item).issubset(STORE_CHECKPOINT_REF_KEYS)
                        or str(item.get("entity") or "").strip().casefold() not in STORE_LEDGER_REF_ENTITIES
                    ):
                        found.append(f"{path}[{index}]")
                    elif _find_unsafe_store_machine_values(item):
                        found.append(f"{path}[{index}]")
                continue
            if key in {"counts", "verification"}:
                if _safe_store_scalar_map(nested, kind=key) is None:
                    found.append(path)
                continue
            safe_technical = key.endswith(
                ("_id", "_ids", "_version", "_versions", "_count", "_counts", "_hash", "_at", "_cursor")
            ) or key in {"id", "version", "updated_at", "cursor", "counts"}
            if (
                key in _STORE_LEDGER_FORBIDDEN_KEYS or set(key.split("_")) & _STORE_SENSITIVE_KEY_TOKENS
            ) and not safe_technical:
                found.append(path)
            if key.startswith("raw_") or key.endswith(("_payload", "_body", "_content", "_description")):
                found.append(path)
            found.extend(_find_forbidden_store_payload_keys(nested, prefix=path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            if isinstance(nested, (dict, list)):
                found.extend(_find_forbidden_store_payload_keys(nested, prefix=f"{prefix}[{index}]"))
    return list(dict.fromkeys(found))


def _safe_store_scalar_map(value: Any, *, kind: str) -> dict[str, Any] | None:
    if not isinstance(value, dict) or len(value) > 100:
        return None
    safe: dict[str, Any] = {}
    for raw_key, nested in value.items():
        key = str(raw_key or "").strip().casefold().replace("-", "_")
        if not key or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) is None:
            return None
        if kind != "counts" and (
            key in _STORE_LEDGER_FORBIDDEN_KEYS or set(key.split("_")) & _STORE_SENSITIVE_KEY_TOKENS
        ):
            return None
        if kind == "counts":
            if isinstance(nested, bool) or not isinstance(nested, (int, float)):
                return None
            if isinstance(nested, float) and not math.isfinite(nested):
                return None
        elif isinstance(nested, str):
            string_key_allowed = key in _STORE_VERIFICATION_STRING_KEYS or key.endswith(("_status", "_error_code"))
            if (
                not string_key_allowed
                or len(nested) > 120
                or re.fullmatch(r"[A-Za-z0-9_.:-]+", nested) is None
                or not _store_machine_value_is_safe(nested)
            ):
                return None
        elif not isinstance(nested, (bool, int, float)) and nested is not None:
            return None
        elif isinstance(nested, float) and not math.isfinite(nested):
            return None
        safe[key] = nested
    return safe


def _normalize_store_checkpoint_refs(value: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in value or []:
        if not isinstance(raw, dict) or not set(raw).issubset(STORE_CHECKPOINT_REF_KEYS):
            raise ValueError("store checkpoint refs must contain technical id/version fields only")
        entity = str(raw.get("entity") or "").strip().casefold()
        entity_id = str(raw.get("id") or "").strip()
        version = str(raw.get("version") or "").strip()
        updated_at = str(raw.get("updated_at") or "").strip()
        if entity not in STORE_LEDGER_REF_ENTITIES or not entity_id:
            raise ValueError("store checkpoint ref requires store entity and id")
        if any(not _store_machine_value_is_safe(value) for value in (entity, entity_id, version, updated_at) if value):
            raise ValueError("store checkpoint ref contains an unsafe identifier")
        key = (entity, entity_id, version)
        if key in seen:
            continue
        seen.add(key)
        item = {"entity": entity, "id": entity_id}
        if version:
            item["version"] = version
        if updated_at:
            item["updated_at"] = updated_at
        refs.append(item)
        if len(refs) >= 500:
            break
    encoded = json.dumps(refs, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 16_384:
        raise ValueError("store checkpoint refs are too large")
    return refs


def _safe_bootstrap_checkpoint(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, Any] = {}
    for key in STORE_LEDGER_SAFE_CHECKPOINT_KEYS:
        nested = value.get(key)
        if nested is None:
            continue
        if key == "compact_refs":
            try:
                safe[key] = _normalize_store_checkpoint_refs(nested if isinstance(nested, list) else [])[:20]
            except ValueError:
                continue
        elif key in {"counts", "verification"}:
            sanitized = _safe_store_scalar_map(nested, kind=key)
            if sanitized is not None:
                safe[key] = sanitized
        elif isinstance(nested, (str, int, float, bool)):
            safe[key] = nested
    return safe


def _sanitize_external_refs(value: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    payload = value if isinstance(value, dict) else {}
    forbidden = _find_forbidden_body_keys(payload)
    if forbidden:
        return {}, forbidden
    sanitized: dict[str, Any] = {}
    for raw_key, raw_value in payload.items():
        key = str(raw_key or "").strip().casefold()
        if key not in EXTERNAL_REF_KEYS or raw_value is None:
            continue
        if isinstance(raw_value, (str, int, float, bool)):
            sanitized[key] = raw_value
        elif isinstance(raw_value, list):
            sanitized[key] = [item for item in raw_value if isinstance(item, (str, int, float, bool))][:100]
    return sanitized, []


def _tokens(value: str) -> list[str]:
    aliases = {
        "вин": ["vin"],
        "кузов": ["chassis", "frame"],
        "кузова": ["chassis", "frame"],
        "оригинальный": ["oem", "catalog"],
        "оригинального": ["oem", "catalog"],
        "каталожный": ["catalog", "part_number"],
        "каталожного": ["catalog", "part_number"],
        "фильтра": ["фильтр", "filter"],
        "фильтр": ["filter"],
        "фильтры": ["фильтр", "filter"],
        "запчасти": ["parts", "procurement"],
        "запчасть": ["parts", "procurement"],
        "детали": ["деталь", "part"],
        "деталь": ["part"],
        "рулевую": ["рулевая", "steering"],
        "рейку": ["рейка", "rack", "steering_rack"],
        "контрактную": ["контрактная", "contract", "used"],
        "красноярске": ["красноярск", "krasnoyarsk"],
    }
    tokens: list[str] = []
    for token in re.findall(r"[\w\-]+", value.casefold(), flags=re.UNICODE):
        tokens.append(token)
        tokens.extend(aliases.get(token, []))
    return list(dict.fromkeys(tokens))


def _matches_filter(value: str | None, expected: str | None) -> bool:
    if not expected:
        return True
    return str(value or "").casefold() == expected.casefold()


def _matches_tags(item_tags: list[str] | None, expected: list[str] | None) -> bool:
    if not expected:
        return True
    normalized = {tag.casefold() for tag in (item_tags or [])}
    return all(tag.casefold() in normalized for tag in expected)


def _clamp01(value: float) -> float:
    return max(0.0, min(float(value), 1.0))


def _add_topic(topics: dict[str, dict[str, Any]], name: str, item: dict[str, Any], examples_limit: int) -> None:
    key = name.strip()
    if not key:
        return
    topic = topics.setdefault(key, {"count": 0, "examples": []})
    topic["count"] += 1
    if len(topic["examples"]) < examples_limit:
        topic["examples"].append(
            {
                "kind": item.get("kind"),
                "id": item.get("id"),
                "title": item.get("title") or item.get("content") or item.get("event") or item.get("rule") or "",
            }
        )


def _sort_topic_map(topics: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return dict(sorted(topics.items(), key=lambda entry: (-int(entry[1]["count"]), entry[0].casefold())))


def _memory_context_queries(task: str) -> list[str]:
    lowered = task.casefold()
    queries: list[str] = []
    if any(term in lowered for term in ["vin", "вин", "oem", "каталож", "оригиналь", "номер кузова"]):
        queries.append("vin-oem-lookup-workflow original catalog numbers VIN OEM catalog")
    if any(term in lowered for term in ["рейк", "контракт", "красноярск", "закуп", "наличие", "дром", "zzap", "ззап"]):
        queries.append("parts_sourcing закупочная цена запчастей Красноярск selected part")
    if any(term in lowered for term in ["база знаний", "базу знаний", "knowledge", "индексац", "аннотац"]):
        queries.append("knowledge-intake-boundary knowledge-annotation-index memory-mcp-sync")
    queries.append(task)
    return list(dict.fromkeys(query for query in queries if query.strip()))


def _suppress_board_cleanup_context(task: str) -> bool:
    lowered = task.casefold()
    automotive_lookup = any(
        term in lowered
        for term in [
            "vin",
            "вин",
            "oem",
            "каталож",
            "оригиналь",
            "номер кузова",
            "фильтр",
            "детал",
            "запчаст",
            "рейк",
            "контракт",
            "аналоги",
        ]
    )
    explicit_cleanup = any(
        term in lowered
        for term in [
            "приберись",
            "уборк",
            "очист",
            "доску",
            "board cleanup",
            "cleanup",
            "card cleanup",
        ]
    )
    return automotive_lookup and not explicit_cleanup


def _suppress_admin_context(task: str) -> bool:
    lowered = task.casefold()
    automotive_lookup = any(
        term in lowered
        for term in [
            "vin",
            "вин",
            "oem",
            "каталож",
            "оригиналь",
            "номер кузова",
            "фильтр",
            "детал",
            "запчаст",
            "рейк",
            "контракт",
            "аналоги",
        ]
    )
    explicit_admin = any(
        term in lowered
        for term in [
            "база знаний",
            "базу знаний",
            "knowledge",
            "индексац",
            "аннотац",
            "github",
            "публикац",
            "коммит",
            "репозитор",
        ]
    )
    return automotive_lookup and not explicit_admin


def _suppress_style_context(task: str) -> bool:
    lowered = task.casefold()
    automotive_lookup = any(
        term in lowered
        for term in [
            "vin",
            "вин",
            "oem",
            "каталож",
            "оригиналь",
            "номер кузова",
            "фильтр",
            "детал",
            "запчаст",
            "рейк",
            "контракт",
            "аналоги",
        ]
    )
    explicit_text_work = any(
        term in lowered
        for term in [
            "приберись",
            "описание",
            "оформи",
            "текст",
            "напиши",
            "сообщение",
            "комментарий",
        ]
    )
    return automotive_lookup and not explicit_text_work


def _memory_item_text(item: dict[str, Any]) -> str:
    parts = [
        str(item.get("title") or ""),
        str(item.get("content") or item.get("event") or item.get("rule") or item.get("details") or ""),
        str(item.get("category") or item.get("applies_to") or item.get("scope") or ""),
        str(item.get("source") or ""),
        " ".join(str(tag) for tag in item.get("tags", [])),
    ]
    return " ".join(parts).casefold()


def _is_context_noise(
    item: dict[str, Any],
    *,
    task_text: str,
    suppress_board_cleanup: bool,
    suppress_admin_context: bool,
    suppress_style_context: bool,
) -> bool:
    text = _memory_item_text(item)
    category = str(item.get("category") or item.get("applies_to") or item.get("scope") or "").casefold()
    title = str(item.get("title") or "").casefold()
    if suppress_board_cleanup:
        if category in {"board_cleanup", "board_cleanup_autopilot"}:
            return True
        if title.startswith("board-cleanup"):
            return True
        if any(marker in text for marker in ["board cleanup", "board_cleanup", "приберись"]):
            return True
    if suppress_style_context and category in {"crm_style", "style"}:
        return True
    if suppress_admin_context and title.startswith(("knowledge-", "github-", "documentation-", "memory-")):
        return True
    if suppress_admin_context:
        vehicle_families = [
            (["toyota gr yaris", "yaris gr", "gxpa16", "g16e-gts"], ["toyota", "yaris", "gxpa16", "g16e"]),
            (["bmw f15", "n63"], ["bmw", "f15", "n63", "x5"]),
        ]
        for item_markers, task_markers in vehicle_families:
            if any(marker in text for marker in item_markers) and not any(
                marker in task_text for marker in task_markers
            ):
                return True
    return False


def _unique_memory_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for item in items:
        key = (str(item.get("kind") or ""), int(item.get("id") or 0))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _score_memory_item(item: dict[str, Any], query: str, tokens: list[str]) -> float:
    title = str(item.get("title") or "")
    content = str(item.get("content") or item.get("event") or item.get("rule") or item.get("details") or "")
    category = str(item.get("category") or item.get("scope") or "")
    source = str(item.get("source") or "")
    status = str(item.get("status") or "")
    tags = " ".join(str(tag) for tag in item.get("tags", []))
    haystack = " ".join([title, content, category, source, status, tags]).casefold()
    query_lower = query.casefold()
    score = float(item.get("fts_score") or 0)
    if query_lower and query_lower in haystack:
        score += 20
    for token in tokens:
        token_score = 0.0
        if token in title.casefold():
            token_score += 8
        if token in tags.casefold():
            token_score += 10
        if token in content.casefold():
            token_score += 4
        if token in category.casefold() or token in source.casefold() or token in status.casefold():
            token_score += 2
        if token_score:
            score += token_score
    if item.get("kind") in {"note", "fact"}:
        score += float(item.get("importance") or 0.5) * 8
    if item.get("kind") == "fact":
        score += float(item.get("confidence") or 0.0) * 3
    if item.get("kind") == "rule":
        priority = int(item.get("priority") or 100)
        score += max(0, 30 - priority) / 2
    return score


@dataclass(frozen=True)
class ManagerMemoryStore:
    db_path: Path | None = None

    @property
    def path(self) -> Path:
        return self.db_path or get_db_path()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general',
                    source TEXT NOT NULL DEFAULT 'codex',
                    importance REAL NOT NULL DEFAULT 0.5,
                    expires_at TEXT,
                    supersedes_id INTEGER,
                    sensitivity TEXT NOT NULL DEFAULT 'normal',
                    last_used_at TEXT,
                    archived_at TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general',
                    source TEXT NOT NULL DEFAULT 'codex',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    importance REAL NOT NULL DEFAULT 0.5,
                    expires_at TEXT,
                    supersedes_id INTEGER,
                    sensitivity TEXT NOT NULL DEFAULT 'normal',
                    last_used_at TEXT,
                    archived_at TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS lessons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    applies_to TEXT NOT NULL DEFAULT 'general',
                    signal TEXT NOT NULL DEFAULT 'manager_observation',
                    recommendation TEXT NOT NULL DEFAULT '',
                    avoid TEXT NOT NULL DEFAULT '',
                    importance REAL NOT NULL DEFAULT 0.5,
                    confidence REAL NOT NULL DEFAULT 0.7,
                    source TEXT NOT NULL DEFAULT 'codex',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT,
                    archived_at TEXT
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    due_at TEXT,
                    source TEXT NOT NULL DEFAULT 'codex',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    remind_at TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    source TEXT NOT NULL DEFAULT 'codex',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'codex',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    archived_at TEXT
                );

                CREATE TABLE IF NOT EXISTS manager_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    rule TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'general',
                    priority INTEGER NOT NULL DEFAULT 100,
                    source TEXT NOT NULL DEFAULT 'system',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS knowledge_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT NOT NULL,
                    path TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    document_type TEXT NOT NULL DEFAULT 'file',
                    use_when_json TEXT NOT NULL DEFAULT '[]',
                    content_hash TEXT NOT NULL DEFAULT '',
                    indexed_at TEXT NOT NULL,
                    UNIQUE(domain, path)
                );

                CREATE TABLE IF NOT EXISTS knowledge_route_cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL DEFAULT '',
                    use_when_json TEXT NOT NULL DEFAULT '[]',
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    questions_json TEXT NOT NULL DEFAULT '[]',
                    source_of_truth_json TEXT NOT NULL DEFAULT '[]',
                    primary_files_json TEXT NOT NULL DEFAULT '[]',
                    reference_files_json TEXT NOT NULL DEFAULT '[]',
                    required_context_json TEXT NOT NULL DEFAULT '[]',
                    search_text TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL DEFAULT '',
                    indexed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS knowledge_sections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL,
                    domain TEXT NOT NULL,
                    path TEXT NOT NULL,
                    heading TEXT NOT NULL DEFAULT '',
                    level INTEGER NOT NULL DEFAULT 0,
                    ordinal INTEGER NOT NULL DEFAULT 0,
                    content TEXT NOT NULL DEFAULT '',
                    preview TEXT NOT NULL DEFAULT '',
                    search_text TEXT NOT NULL DEFAULT '',
                    indexed_at TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES knowledge_documents(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS knowledge_annotations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    annotation_id TEXT NOT NULL UNIQUE,
                    domain TEXT NOT NULL,
                    path TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    use_when_json TEXT NOT NULL DEFAULT '[]',
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    questions_json TEXT NOT NULL DEFAULT '[]',
                    source_type TEXT NOT NULL DEFAULT '',
                    trust_level TEXT NOT NULL DEFAULT '',
                    refresh_cadence TEXT NOT NULL DEFAULT '',
                    safety_flags_json TEXT NOT NULL DEFAULT '[]',
                    related_skills_json TEXT NOT NULL DEFAULT '[]',
                    search_text TEXT NOT NULL DEFAULT '',
                    indexed_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_knowledge_documents_domain
                    ON knowledge_documents(domain);

                CREATE INDEX IF NOT EXISTS idx_knowledge_sections_domain
                    ON knowledge_sections(domain);

                CREATE INDEX IF NOT EXISTS idx_knowledge_sections_search
                    ON knowledge_sections(search_text);

                CREATE INDEX IF NOT EXISTS idx_knowledge_route_cards_search
                    ON knowledge_route_cards(search_text);

                CREATE INDEX IF NOT EXISTS idx_knowledge_annotations_domain
                    ON knowledge_annotations(domain);

                CREATE INDEX IF NOT EXISTS idx_knowledge_annotations_search
                    ON knowledge_annotations(search_text);

                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_sections_fts
                USING fts5(domain, path, heading, search_text);

                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_annotations_fts
                USING fts5(domain, path, title, search_text);

                CREATE TABLE IF NOT EXISTS manager_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent TEXT NOT NULL DEFAULT '',
                    workflow_id TEXT NOT NULL DEFAULT '',
                    query TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'running',
                    dry_run INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'codex',
                    request_id TEXT NOT NULL DEFAULT '',
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    correlation_id TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL DEFAULT 'codex-owner-agent',
                    scope_json TEXT NOT NULL DEFAULT '{}',
                    selected_ids_json TEXT NOT NULL DEFAULT '[]',
                    checkpoint_json TEXT NOT NULL DEFAULT '{}',
                    compensation_json TEXT NOT NULL DEFAULT '[]',
                    state_version INTEGER NOT NULL DEFAULT 1,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    summary TEXT NOT NULL DEFAULT '',
                    verification_json TEXT NOT NULL DEFAULT '{}',
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS manager_run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    target_type TEXT NOT NULL DEFAULT '',
                    target_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES manager_runs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS manager_run_external_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    step_id TEXT NOT NULL,
                    connector TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    request_refs_json TEXT NOT NULL DEFAULT '{}',
                    result_refs_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(run_id, step_id),
                    FOREIGN KEY(run_id) REFERENCES manager_runs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS store_checkpoints (
                    stream TEXT PRIMARY KEY,
                    cursor TEXT,
                    last_success_at TEXT,
                    compact_refs_json TEXT NOT NULL DEFAULT '[]',
                    traversal_cursor TEXT,
                    traversal_refs_json TEXT NOT NULL DEFAULT '[]',
                    traversal_baseline INTEGER NOT NULL DEFAULT 0,
                    traversal_snapshot_at TEXT,
                    pending_cursor TEXT,
                    pending_request_cursor TEXT,
                    pending_request_since TEXT,
                    pending_refs_json TEXT NOT NULL DEFAULT '[]',
                    pending_page_refs_json TEXT NOT NULL DEFAULT '[]',
                    pending_baseline INTEGER NOT NULL DEFAULT 0,
                    pending_page_has_more INTEGER NOT NULL DEFAULT 0,
                    pending_page_limit INTEGER NOT NULL DEFAULT 25,
                    pending_snapshot_at TEXT,
                    pending_delivery_token TEXT,
                    last_ack_cursor TEXT,
                    last_ack_delivery_token TEXT,
                    last_ack_snapshot_at TEXT,
                    last_ack_was_final INTEGER NOT NULL DEFAULT 0,
                    last_attempt_status TEXT NOT NULL DEFAULT 'never',
                    last_error_code TEXT NOT NULL DEFAULT '',
                    state_version INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_review_items (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    source_ref TEXT NOT NULL DEFAULT '',
                    proposal_json TEXT NOT NULL DEFAULT '{}',
                    reason TEXT NOT NULL DEFAULT '',
                    risk TEXT NOT NULL DEFAULT 'low',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_manager_runs_status
                    ON manager_runs(status, started_at);

                CREATE INDEX IF NOT EXISTS idx_manager_run_events_run_id
                    ON manager_run_events(run_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_manager_run_external_steps_run_id
                    ON manager_run_external_steps(run_id, status, created_at);

                CREATE INDEX IF NOT EXISTS idx_store_checkpoints_status
                    ON store_checkpoints(last_attempt_status, updated_at);

                CREATE INDEX IF NOT EXISTS idx_memory_review_items_status
                    ON memory_review_items(status, created_at);
                """
            )
            self._ensure_columns(conn)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_manager_runs_idempotency "
                "ON manager_runs(idempotency_key) WHERE idempotency_key <> ''"
            )
            self._ensure_memory_fts(conn)

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        desired = {
            "notes": {
                "importance": "REAL NOT NULL DEFAULT 0.5",
                "expires_at": "TEXT",
                "supersedes_id": "INTEGER",
                "sensitivity": "TEXT NOT NULL DEFAULT 'normal'",
                "last_used_at": "TEXT",
                "archived_at": "TEXT",
            },
            "facts": {
                "importance": "REAL NOT NULL DEFAULT 0.5",
                "expires_at": "TEXT",
                "supersedes_id": "INTEGER",
                "sensitivity": "TEXT NOT NULL DEFAULT 'normal'",
                "last_used_at": "TEXT",
                "archived_at": "TEXT",
            },
            "lessons": {
                "last_used_at": "TEXT",
                "archived_at": "TEXT",
            },
            "journal": {
                "archived_at": "TEXT",
            },
            "knowledge_route_cards": {
                "reference_files_json": "TEXT NOT NULL DEFAULT '[]'",
            },
            "manager_runs": {
                "workflow_id": "TEXT NOT NULL DEFAULT ''",
                "request_id": "TEXT NOT NULL DEFAULT ''",
                "idempotency_key": "TEXT NOT NULL DEFAULT ''",
                "correlation_id": "TEXT NOT NULL DEFAULT ''",
                "actor": "TEXT NOT NULL DEFAULT 'codex-owner-agent'",
                "scope_json": "TEXT NOT NULL DEFAULT '{}'",
                "selected_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "checkpoint_json": "TEXT NOT NULL DEFAULT '{}'",
                "compensation_json": "TEXT NOT NULL DEFAULT '[]'",
                "state_version": "INTEGER NOT NULL DEFAULT 1",
            },
            "store_checkpoints": {
                "traversal_cursor": "TEXT",
                "traversal_refs_json": "TEXT NOT NULL DEFAULT '[]'",
                "traversal_baseline": "INTEGER NOT NULL DEFAULT 0",
                "traversal_snapshot_at": "TEXT",
                "pending_cursor": "TEXT",
                "pending_request_cursor": "TEXT",
                "pending_request_since": "TEXT",
                "pending_refs_json": "TEXT NOT NULL DEFAULT '[]'",
                "pending_page_refs_json": "TEXT NOT NULL DEFAULT '[]'",
                "pending_baseline": "INTEGER NOT NULL DEFAULT 0",
                "pending_page_has_more": "INTEGER NOT NULL DEFAULT 0",
                "pending_page_limit": "INTEGER NOT NULL DEFAULT 25",
                "pending_snapshot_at": "TEXT",
                "pending_delivery_token": "TEXT",
                "last_ack_cursor": "TEXT",
                "last_ack_delivery_token": "TEXT",
                "last_ack_snapshot_at": "TEXT",
                "last_ack_was_final": "INTEGER NOT NULL DEFAULT 0",
            },
        }
        for table, columns in desired.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _ensure_memory_fts(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts
                USING fts5(title, content, category, source, tags)
                """
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
                USING fts5(content, category, source, tags)
                """
            )
        except sqlite3.OperationalError:
            return
        conn.execute(
            """
            INSERT OR REPLACE INTO notes_fts(rowid, title, content, category, source, tags)
            SELECT id, title, content, category, source, tags_json
            FROM notes
            WHERE archived_at IS NULL
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO facts_fts(rowid, content, category, source, tags)
            SELECT id, content, category, source, tags_json
            FROM facts
            WHERE archived_at IS NULL
            """
        )

    def seed_default_rules(self) -> dict[str, Any]:
        self.initialize()
        rules_path = PROJECT_ROOT / "docs" / "agent" / "manager_rules.json"
        if not rules_path.exists():
            return {"ok": False, "error": "manager_rules.json not found", "inserted": 0}
        try:
            payload = json.loads(rules_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "error": "manager_rules.json invalid_json",
                "error_detail": str(exc),
                "inserted": 0,
                "updated": 0,
            }
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "error": "manager_rules.json invalid_structure",
                "error_detail": type(payload).__name__,
                "inserted": 0,
                "updated": 0,
            }
        rules = payload.get("rules")
        if not isinstance(rules, list):
            return {
                "ok": False,
                "error": "manager_rules.json invalid_rules",
                "error_detail": type(rules).__name__,
                "inserted": 0,
                "updated": 0,
            }
        inserted = 0
        updated = 0
        removed = 0
        now = _now()
        source = "docs/agent/manager_rules.json"
        active_titles = {
            str(rule.get("id") or "").strip()
            for rule in rules
            if isinstance(rule, dict) and str(rule.get("id") or "").strip() and str(rule.get("rule") or "").strip()
        }
        with self.connect() as conn:
            stale_rows = conn.execute(
                "SELECT id, title FROM manager_rules WHERE source = ?",
                (source,),
            ).fetchall()
            stale_ids = [int(row["id"]) for row in stale_rows if str(row["title"]) not in active_titles]
            if stale_ids:
                conn.executemany(
                    "DELETE FROM manager_rules WHERE id = ?",
                    [(row_id,) for row_id in stale_ids],
                )
                removed = len(stale_ids)
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                title = str(rule.get("id") or "").strip()
                text = str(rule.get("rule") or "").strip()
                if not title or not text:
                    continue
                scope = str(rule.get("scope") or "general")
                priority = int(rule.get("priority") or 100)
                exists = conn.execute(
                    "SELECT id, rule, scope, priority, source FROM manager_rules WHERE title = ? LIMIT 1",
                    (title,),
                ).fetchone()
                if exists:
                    if (
                        exists["rule"] != text
                        or exists["scope"] != scope
                        or int(exists["priority"]) != priority
                        or exists["source"] != source
                    ):
                        conn.execute(
                            """
                            UPDATE manager_rules
                            SET rule = ?, scope = ?, priority = ?, source = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (text, scope, priority, source, now, exists["id"]),
                        )
                        updated += 1
                    continue
                conn.execute(
                    """
                    INSERT INTO manager_rules (title, rule, scope, priority, source, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        title,
                        text,
                        scope,
                        priority,
                        source,
                        now,
                        now,
                    ),
                )
                inserted += 1
        return {"ok": True, "inserted": inserted, "updated": updated, "removed": removed}

    def remember(
        self,
        content: str,
        *,
        kind: str = "note",
        title: str = "",
        category: str = "general",
        source: str = "codex",
        tags: list[str] | None = None,
        importance: float = 0.5,
        confidence: float = 1.0,
        expires_at: str | None = None,
        supersedes_id: int | None = None,
        sensitivity: str = "normal",
    ) -> dict[str, Any]:
        self.initialize()
        now = _now()
        table = "facts" if kind == "fact" else "notes"
        importance = _clamp01(importance)
        confidence = _clamp01(confidence)
        row_id = 0
        with self.connect() as conn:
            if table == "facts":
                cursor = conn.execute(
                    """
                    INSERT INTO facts
                        (content, category, source, confidence, importance, expires_at, supersedes_id, sensitivity, tags_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        content,
                        category,
                        source,
                        float(confidence),
                        float(importance),
                        expires_at,
                        supersedes_id,
                        sensitivity,
                        _json_list(tags),
                        now,
                        now,
                    ),
                )
                row_id = _required_lastrowid(cursor)
                self._upsert_memory_fts(conn, table, row_id)
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO notes
                        (title, content, category, source, importance, expires_at, supersedes_id, sensitivity, tags_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        title,
                        content,
                        category,
                        source,
                        float(importance),
                        expires_at,
                        supersedes_id,
                        sensitivity,
                        _json_list(tags),
                        now,
                        now,
                    ),
                )
                row_id = _required_lastrowid(cursor)
                self._upsert_memory_fts(conn, table, row_id)
        result = {"ok": True, "kind": table[:-1], "id": row_id, "created_at": now}
        if table == "facts":
            result["confidence"] = float(confidence)
        return result

    def learn_from_feedback(
        self,
        content: str,
        *,
        title: str = "",
        applies_to: str = "general",
        signal: str = "manager_observation",
        recommendation: str = "",
        avoid: str = "",
        importance: float = 0.5,
        confidence: float = 0.7,
        source: str = "codex",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        now = _now()
        importance = _clamp01(importance)
        confidence = _clamp01(confidence)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO lessons (
                    title, content, applies_to, signal, recommendation, avoid,
                    importance, confidence, source, tags_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title,
                    content,
                    applies_to,
                    signal,
                    recommendation,
                    avoid,
                    importance,
                    confidence,
                    source,
                    _json_list(tags),
                    now,
                    now,
                ),
            )
        return {
            "ok": True,
            "kind": "lesson",
            "id": cursor.lastrowid,
            "created_at": now,
            "applies_to": applies_to,
            "signal": signal,
            "importance": importance,
            "confidence": confidence,
        }

    def add_task(
        self,
        title: str,
        *,
        details: str = "",
        due_at: str | None = None,
        source: str = "codex",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        now = _now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tasks (title, details, due_at, source, tags_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (title, details, due_at, source, _json_list(tags), now, now),
            )
        return {"ok": True, "id": cursor.lastrowid, "created_at": now}

    def add_reminder(
        self,
        title: str,
        *,
        remind_at: str,
        details: str = "",
        source: str = "codex",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        now = _now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO reminders (title, remind_at, details, source, tags_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (title, remind_at, details, source, _json_list(tags), now, now),
            )
        return {"ok": True, "id": cursor.lastrowid, "created_at": now}

    def journal(self, event: str, *, source: str = "codex", tags: list[str] | None = None) -> dict[str, Any]:
        self.initialize()
        now = _now()
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO journal (event, source, tags_json, created_at) VALUES (?, ?, ?, ?)",
                (event, source, _json_list(tags), now),
            )
        return {"ok": True, "id": cursor.lastrowid, "created_at": now}

    def recall(
        self,
        query: str = "",
        *,
        limit: int = 20,
        kind: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        limit = max(1, min(limit, 100))
        query = query.strip()
        query_tokens = _tokens(query)
        results: list[dict[str, Any]] = []
        with self.connect() as conn:
            for row_kind, table, order_column in [
                ("note", "notes", "updated_at"),
                ("fact", "facts", "updated_at"),
            ]:
                if kind and row_kind != kind:
                    continue
                rows = conn.execute(
                    f"""
                    SELECT *, ? AS kind FROM {table}
                    WHERE archived_at IS NULL
                        AND (expires_at IS NULL OR expires_at > ?)
                        AND id NOT IN (
                            SELECT supersedes_id FROM {table}
                            WHERE supersedes_id IS NOT NULL AND archived_at IS NULL
                        )
                    ORDER BY {order_column} DESC
                    LIMIT ?
                    """,
                    (row_kind, _now(), max(limit * 10, 100)),
                ).fetchall()
                for row in rows:
                    item = self._row_to_dict(row)
                    if not _matches_filter(item.get("category"), category):
                        continue
                    if not _matches_tags(item.get("tags", []), tags):
                        continue
                    score, matched_fields = self._score_memory_item(item, query, query_tokens)
                    if query_tokens and score <= 0:
                        continue
                    score += int(_clamp01(float(item.get("importance") or 0.5)) * 8)
                    if row_kind == "fact":
                        score += int(_clamp01(float(item.get("confidence") or 0.0)) * 3)
                    item["score"] = score
                    item["matched_fields"] = matched_fields
                    results.append(item)

            searches = [
                ("task", "tasks", "updated_at"),
                ("reminder", "reminders", "updated_at"),
                ("journal", "journal", "created_at"),
                ("rule", "manager_rules", "updated_at"),
                ("lesson", "lessons", "updated_at"),
            ]
            for row_kind, table, order_column in searches:
                if kind and row_kind != kind:
                    continue
                row_limit = 1000 if row_kind == "rule" else max(limit * 10, 100)
                where = "WHERE archived_at IS NULL" if row_kind in {"lesson", "journal"} else ""
                rows = conn.execute(
                    f"""
                    SELECT *, ? AS kind FROM {table}
                    {where}
                    ORDER BY {order_column} DESC
                    LIMIT ?
                    """,
                    (row_kind, row_limit),
                ).fetchall()
                for row in rows:
                    item = self._row_to_dict(row)
                    if not _matches_filter(item.get("category"), category):
                        continue
                    if not _matches_tags(item.get("tags", []), tags):
                        continue
                    score, matched_fields = self._score_memory_item(item, query, query_tokens)
                    if query_tokens and score <= 0:
                        continue
                    if row_kind == "rule":
                        score += max(0, 30 - int(item.get("priority") or 100)) // 2
                    if row_kind == "lesson":
                        score += int(_clamp01(float(item.get("importance") or 0)) * 8)
                        score += int(_clamp01(float(item.get("confidence") or 0)) * 3)
                    item["score"] = score
                    item["matched_fields"] = matched_fields
                    results.append(item)
        results.sort(
            key=lambda item: (
                int(item.get("score") or 0),
                item.get("updated_at") or item.get("created_at") or "",
            ),
            reverse=True,
        )
        selected = results[:limit]
        used_at = _now()
        with self.connect() as conn:
            for item in selected:
                item_kind = item.get("kind")
                if item_kind in {"note", "fact"}:
                    table = "notes" if item_kind == "note" else "facts"
                    conn.execute("UPDATE " + table + " SET last_used_at = ? WHERE id = ?", (used_at, item["id"]))
                    item["last_used_at"] = used_at
        return {
            "ok": True,
            "query": query,
            "filters": {"kind": kind, "category": category, "tags": tags or []},
            "items": selected,
            "total_returned": len(selected),
            "total_matches": len(results),
        }

    def recall_lessons(
        self,
        query: str = "",
        *,
        limit: int = 20,
        applies_to: str | None = None,
        signal: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        limit = max(1, min(limit, 100))
        query = query.strip()
        query_tokens = _tokens(query)
        results: list[dict[str, Any]] = []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *, 'lesson' AS kind FROM lessons
                WHERE archived_at IS NULL
                ORDER BY importance DESC, confidence DESC, updated_at DESC
                LIMIT ?
                """,
                (max(limit * 10, 100),),
            ).fetchall()
            for row in rows:
                item = self._row_to_dict(row)
                if not _matches_filter(item.get("applies_to"), applies_to):
                    continue
                if not _matches_filter(item.get("signal"), signal):
                    continue
                if not _matches_tags(item.get("tags", []), tags):
                    continue
                score, matched_fields = self._score_memory_item(item, query, query_tokens)
                if query_tokens and score <= 0:
                    continue
                item["score"] = score
                item["matched_fields"] = matched_fields
                results.append(item)
        results.sort(
            key=lambda item: (
                int(item.get("score") or 0),
                float(item.get("importance") or 0),
                float(item.get("confidence") or 0),
                item.get("updated_at") or "",
            ),
            reverse=True,
        )
        return {
            "ok": True,
            "query": query,
            "filters": {"applies_to": applies_to, "signal": signal, "tags": tags or []},
            "items": results[:limit],
            "total_returned": min(len(results), limit),
            "total_matches": len(results),
        }

    def memory_map(self) -> dict[str, Any]:
        self.initialize()
        sections = {
            "notes": self._section_summary("notes", "updated_at"),
            "facts": self._section_summary("facts", "updated_at"),
            "lessons": self._section_summary("lessons", "updated_at", where="archived_at IS NULL"),
            "tasks": self._section_summary("tasks", "updated_at", where="status = 'open'"),
            "reminders": self._section_summary("reminders", "updated_at", where="status = 'open'"),
            "journal": self._section_summary("journal", "created_at", where="archived_at IS NULL"),
            "rules": self._section_summary("manager_rules", "updated_at"),
        }
        return {
            "ok": True,
            "generated_at": _now(),
            "sections": sections,
            "recommended_flow": [
                "today_context",
                "memory_context_for",
                "recall_lessons",
                "learn_from_feedback after strong owner/result signals",
                "memory_gaps during memory review",
            ],
        }

    def memory_topics(self, *, examples_limit: int = 3) -> dict[str, Any]:
        self.initialize()
        categories: dict[str, dict[str, Any]] = {}
        tags: dict[str, dict[str, Any]] = {}
        with self.connect() as conn:
            rows: list[dict[str, Any]] = []
            for table, kind in [
                ("notes", "note"),
                ("facts", "fact"),
                ("tasks", "task"),
                ("reminders", "reminder"),
                ("journal", "journal"),
                ("lessons", "lesson"),
                ("manager_rules", "rule"),
            ]:
                order_column = "created_at" if table == "journal" else "updated_at"
                where = "WHERE archived_at IS NULL" if table in {"lessons", "journal"} else ""
                rows.extend(
                    self._row_to_dict(row)
                    for row in conn.execute(
                        f"SELECT *, ? AS kind FROM {table} {where} ORDER BY {order_column} DESC LIMIT 200",
                        (kind,),
                    ).fetchall()
                )
        for item in rows:
            category = str(item.get("category") or item.get("applies_to") or item.get("scope") or "").strip()
            if category:
                _add_topic(categories, category, item, examples_limit)
            for tag in item.get("tags") or []:
                _add_topic(tags, str(tag), item, examples_limit)
        return {
            "ok": True,
            "generated_at": _now(),
            "categories": _sort_topic_map(categories),
            "tags": _sort_topic_map(tags),
        }

    def memory_context_for(self, task: str, *, limit: int = 5) -> dict[str, Any]:
        self.initialize()
        task = task.strip()
        limit = max(1, min(limit, 20))
        context_queries = _memory_context_queries(task)
        suppress_board_cleanup = _suppress_board_cleanup_context(task)
        suppress_admin_context = _suppress_admin_context(task)
        suppress_style_context = _suppress_style_context(task)
        task_text = task.casefold()
        lesson_queries = context_queries[:1] if len(context_queries) > 1 else context_queries
        lessons = [
            item
            for item in _unique_memory_items(
                [item for query in lesson_queries for item in self.recall_lessons(query, limit=limit)["items"]]
            )
            if not _is_context_noise(
                item,
                task_text=task_text,
                suppress_board_cleanup=suppress_board_cleanup,
                suppress_admin_context=suppress_admin_context,
                suppress_style_context=suppress_style_context,
            )
        ][:limit]
        if not lessons and len(context_queries) == 1:
            lessons = [
                item
                for item in self.recall_lessons("", limit=min(limit, 3))["items"]
                if not _is_context_noise(
                    item,
                    task_text=task_text,
                    suppress_board_cleanup=suppress_board_cleanup,
                    suppress_admin_context=suppress_admin_context,
                    suppress_style_context=suppress_style_context,
                )
            ]

        recalled = _unique_memory_items(
            [item for query in context_queries for item in self.recall(query, limit=limit * 3)["items"]]
        )
        recalled = [
            item
            for item in recalled
            if not _is_context_noise(
                item,
                task_text=task_text,
                suppress_board_cleanup=suppress_board_cleanup,
                suppress_admin_context=suppress_admin_context,
                suppress_style_context=suppress_style_context,
            )
        ]
        preferences_or_facts = [
            item for item in recalled if item.get("kind") in {"fact", "note", "rule"} and item.get("kind") != "lesson"
        ][:limit]
        if not preferences_or_facts:
            preferences_or_facts = self.recall("", limit=limit, kind="fact")["items"]

        return {
            "ok": True,
            "query": task,
            "generated_at": _now(),
            "lessons": lessons[:limit],
            "preferences_or_facts": preferences_or_facts[:limit],
            "source_boundaries": [
                "CRM is source of truth for cards, clients, vehicles, repair orders, payments, and cashboxes.",
                "Manager memory stores style, owner preferences, durable lessons, and operating context only.",
                "Use memory as context for judgment, not as a rigid text template.",
            ],
            "suggested_use": [
                "Read lessons and preferences before writing CRM/email/customer-facing text.",
                "Check live CRM data before making factual statements about board state or money.",
                "After strong praise, criticism, success, or failure, write a concise lesson.",
            ],
        }

    def memory_gaps(self) -> dict[str, Any]:
        self.initialize()
        sections = {
            "notes": self._count_rows("notes"),
            "facts": self._count_rows("facts"),
            "lessons": self._count_rows("lessons", where="archived_at IS NULL"),
            "tasks": self._count_rows("tasks", where="status = 'open'"),
            "reminders": self._count_rows("reminders", where="status = 'open'"),
            "journal": self._count_rows("journal", where="archived_at IS NULL"),
            "rules": self._count_rows("manager_rules"),
        }
        empty_sections = {name: count for name, count in sections.items() if count == 0}
        sparse_sections = {name: count for name, count in sections.items() if 0 < count < 2}
        return {
            "ok": True,
            "generated_at": _now(),
            "empty_sections": empty_sections,
            "sparse_sections": sparse_sections,
            "conflicts": [],
            "review_prompts": [
                "Add lessons after strong owner feedback or clearly successful/failed work.",
                "Keep CRM facts in CRM; store only reusable operating conclusions in memory.",
                "Review sparse topics before relying on memory for style-sensitive work.",
            ],
        }

    def _upsert_memory_fts(self, conn: sqlite3.Connection, table: str, row_id: int) -> None:
        try:
            if table == "notes":
                row = conn.execute("SELECT * FROM notes WHERE id = ? LIMIT 1", (row_id,)).fetchone()
                if row:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO notes_fts(rowid, title, content, category, source, tags)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (row["id"], row["title"], row["content"], row["category"], row["source"], row["tags_json"]),
                    )
            elif table == "facts":
                row = conn.execute("SELECT * FROM facts WHERE id = ? LIMIT 1", (row_id,)).fetchone()
                if row:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO facts_fts(rowid, content, category, source, tags)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (row["id"], row["content"], row["category"], row["source"], row["tags_json"]),
                    )
        except sqlite3.OperationalError:
            return

    def today_context(self, *, limit: int = 20) -> dict[str, Any]:
        self.initialize()
        warnings: list[str] = []
        if self._manager_rule_count() == 0:
            seed_result = self.seed_default_rules()
            if not seed_result.get("ok", True):
                warnings.append(f"manager_rules_seed_failed: {seed_result.get('error', 'unknown')}")
        now = _now()
        limit = max(1, min(limit, 100))
        with self.connect() as conn:
            tasks = [
                self._row_to_dict(row)
                for row in conn.execute(
                    """
                    SELECT *, 'task' AS kind FROM tasks
                    WHERE status = 'open' AND (due_at IS NULL OR due_at <= ?)
                    ORDER BY due_at IS NULL, due_at ASC, created_at DESC
                    LIMIT ?
                    """,
                    (now, limit),
                ).fetchall()
            ]
            reminders = [
                self._row_to_dict(row)
                for row in conn.execute(
                    """
                    SELECT *, 'reminder' AS kind FROM reminders
                    WHERE status = 'open' AND remind_at <= ?
                    ORDER BY remind_at ASC
                    LIMIT ?
                    """,
                    (now, limit),
                ).fetchall()
            ]
            journal_rows = [
                self._row_to_dict(row)
                for row in conn.execute(
                    """
                    SELECT *, 'journal' AS kind FROM journal
                    WHERE archived_at IS NULL
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            ]
            rules = [
                self._row_to_dict(row)
                for row in conn.execute(
                    "SELECT *, 'rule' AS kind FROM manager_rules ORDER BY priority ASC, updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            ]
        return {
            "ok": True,
            "generated_at": now,
            "tasks": tasks,
            "reminders": reminders,
            "recent_journal": journal_rows,
            "manager_rules": rules,
            "crm_read_order": [
                "agent_bootstrap",
                "agent_board_digest",
                "agent_search",
                "agent_entity_context",
                "for AutoStop App use store scope/entities; bootstrap uses store_bootstrap and owner digest uses store_digest",
                "named domain workflow in dry_run before apply",
                "discover_raw_capabilities only when no named workflow covers the task",
            ],
            "memory_use_order": [
                "today_context",
                "memory_context_for before context-sensitive CRM/Gmail/writing tasks",
                "recall owner/style/rule terms when the request depends on prior preferences",
                "recall_lessons for similar prior successes or failures",
                "probe_knowledge_base for local knowledge routing",
                "learn_from_feedback after strong praise, criticism, success, or failure",
                "manager_journal after important decisions",
            ],
            "warnings": warnings,
        }

    def get_store_checkpoint(self, stream: str = "store_digest") -> dict[str, Any]:
        self.initialize()
        normalized_stream = str(stream or "").strip().casefold()
        if normalized_stream not in STORE_CHECKPOINT_STREAMS:
            return {"ok": False, "error": "store_checkpoint_stream_invalid"}
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM store_checkpoints WHERE stream = ? LIMIT 1", (normalized_stream,)
            ).fetchone()
        if not row:
            return {
                "ok": True,
                "exists": False,
                "stream": normalized_stream,
                "cursor": None,
                "last_success_at": None,
                "compact_refs": [],
                "traversal_cursor": None,
                "traversal_refs": [],
                "traversal_baseline": False,
                "traversal_snapshot_at": None,
                "pending_cursor": None,
                "pending_request_cursor": None,
                "pending_request_since": None,
                "pending_refs": [],
                "pending_page_refs": [],
                "pending_baseline": False,
                "pending_page_has_more": False,
                "pending_page_limit": 25,
                "pending_snapshot_at": None,
                "pending_delivery_token": None,
                "last_ack_cursor": None,
                "last_ack_delivery_token": None,
                "last_ack_snapshot_at": None,
                "last_ack_was_final": False,
                "last_attempt_status": "never",
                "last_error_code": "",
                "state_version": 0,
            }
        item = dict(row)
        item["compact_refs"] = _decode_json(item.pop("compact_refs_json"), [])
        item["traversal_refs"] = _decode_json(item.pop("traversal_refs_json"), [])
        item["traversal_baseline"] = bool(item.get("traversal_baseline"))
        item["pending_refs"] = _decode_json(item.pop("pending_refs_json"), [])
        item["pending_page_refs"] = _decode_json(item.pop("pending_page_refs_json"), [])
        item["pending_baseline"] = bool(item.get("pending_baseline"))
        item["pending_page_has_more"] = bool(item.get("pending_page_has_more"))
        item["last_ack_was_final"] = bool(item.get("last_ack_was_final"))
        return {"ok": True, "exists": True, **item}

    def record_store_checkpoint_pending(
        self,
        *,
        stream: str,
        next_cursor: str,
        compact_refs: list[dict[str, Any]] | None,
        baseline: bool,
        expected_state_version: int,
        request_cursor: str | None = None,
        request_since: str | None = None,
        page_has_more: bool = True,
        page_limit: int = 25,
        page_refs: list[dict[str, Any]] | None = None,
        snapshot_at: str | None = None,
        delivery_token: str | None = None,
    ) -> dict[str, Any]:
        """Persist one unacknowledged page without advancing its delivered cursor."""

        self.initialize()
        normalized_stream = str(stream or "").strip().casefold()
        normalized_cursor = str(next_cursor or "").strip()
        normalized_request_cursor = str(request_cursor or "").strip() or None
        normalized_request_since = str(request_since or "").strip() or None
        normalized_snapshot_at = str(snapshot_at or "").strip() or None
        normalized_delivery_token = str(delivery_token or "").strip()
        if normalized_stream not in STORE_CHECKPOINT_STREAMS:
            return {"ok": False, "error": "store_checkpoint_stream_invalid"}
        if not normalized_cursor or not normalized_delivery_token:
            return {"ok": False, "error": "store_checkpoint_pending_fields_required"}
        if len(normalized_cursor) > 4096 or (
            normalized_request_cursor is not None and len(normalized_request_cursor) > 4096
        ):
            return {"ok": False, "error": "store_checkpoint_cursor_too_large"}
        if normalized_request_since is not None and len(normalized_request_since) > 160:
            return {"ok": False, "error": "store_checkpoint_since_too_large"}
        if normalized_snapshot_at is not None and len(normalized_snapshot_at) > 160:
            return {"ok": False, "error": "store_checkpoint_snapshot_too_large"}
        if re.fullmatch(r"[0-9a-f]{64}", normalized_delivery_token) is None:
            return {"ok": False, "error": "store_checkpoint_delivery_token_invalid"}
        try:
            normalized_refs = _normalize_store_checkpoint_refs(compact_refs)
            normalized_page_refs = _normalize_store_checkpoint_refs(page_refs)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        normalized_page_limit = int(page_limit)
        if normalized_page_limit < 1 or normalized_page_limit > 100:
            return {"ok": False, "error": "store_checkpoint_page_limit_invalid"}
        refs_json = json.dumps(normalized_refs, ensure_ascii=False, separators=(",", ":"))
        page_refs_json = json.dumps(normalized_page_refs, ensure_ascii=False, separators=(",", ":"))
        now = _now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM store_checkpoints WHERE stream = ? LIMIT 1",
                (normalized_stream,),
            ).fetchone()
            current_version = int(row["state_version"] or 0) if row else 0
            if int(expected_state_version) != current_version:
                return {
                    "ok": False,
                    "error": "store_checkpoint_state_conflict",
                    "stream": normalized_stream,
                    "expected_state_version": int(expected_state_version),
                    "current_state_version": current_version,
                }
            if row:
                same_pending = (
                    str(row["pending_cursor"] or "") == normalized_cursor
                    and (str(row["pending_request_cursor"] or "") or None) == normalized_request_cursor
                    and (str(row["pending_request_since"] or "") or None) == normalized_request_since
                    and _decode_json(row["pending_refs_json"], []) == normalized_refs
                    and _decode_json(row["pending_page_refs_json"], []) == normalized_page_refs
                    and bool(row["pending_baseline"]) == bool(baseline)
                    and bool(row["pending_page_has_more"]) == bool(page_has_more)
                    and int(row["pending_page_limit"] or 0) == normalized_page_limit
                    and (str(row["pending_snapshot_at"] or "") or None) == normalized_snapshot_at
                    and str(row["pending_delivery_token"] or "") == normalized_delivery_token
                    and str(row["last_attempt_status"] or "") == "pending"
                )
                if same_pending:
                    return {
                        "ok": True,
                        "stream": normalized_stream,
                        "cursor": row["cursor"],
                        "pending_cursor": normalized_cursor,
                        "pending_request_cursor": normalized_request_cursor,
                        "pending_request_since": normalized_request_since,
                        "pending_refs": normalized_refs,
                        "pending_page_refs": normalized_page_refs,
                        "pending_baseline": bool(baseline),
                        "pending_page_has_more": bool(page_has_more),
                        "pending_page_limit": normalized_page_limit,
                        "pending_snapshot_at": normalized_snapshot_at,
                        "pending_delivery_token": normalized_delivery_token,
                        "last_attempt_status": "pending",
                        "state_version": current_version,
                        "deduplicated": True,
                    }
            next_version = current_version + 1
            if row:
                conn.execute(
                    """
                    UPDATE store_checkpoints
                    SET pending_cursor = ?, pending_request_cursor = ?, pending_request_since = ?,
                        pending_refs_json = ?, pending_page_refs_json = ?, pending_baseline = ?,
                        pending_page_has_more = ?, pending_page_limit = ?,
                        pending_snapshot_at = ?, pending_delivery_token = ?,
                        last_attempt_status = 'pending', last_error_code = '',
                        state_version = ?, updated_at = ?
                    WHERE stream = ? AND state_version = ?
                    """,
                    (
                        normalized_cursor,
                        normalized_request_cursor,
                        normalized_request_since,
                        refs_json,
                        page_refs_json,
                        1 if baseline else 0,
                        1 if page_has_more else 0,
                        normalized_page_limit,
                        normalized_snapshot_at,
                        normalized_delivery_token,
                        next_version,
                        now,
                        normalized_stream,
                        current_version,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO store_checkpoints (
                        stream, cursor, last_success_at, compact_refs_json,
                        pending_cursor, pending_request_cursor, pending_request_since,
                        pending_refs_json, pending_page_refs_json, pending_baseline,
                        pending_page_has_more, pending_page_limit,
                        pending_snapshot_at, pending_delivery_token,
                        last_attempt_status, last_error_code, state_version, updated_at
                    ) VALUES (
                        ?, NULL, NULL, '[]', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'pending', '', ?, ?
                    )
                    """,
                    (
                        normalized_stream,
                        normalized_cursor,
                        normalized_request_cursor,
                        normalized_request_since,
                        refs_json,
                        page_refs_json,
                        1 if baseline else 0,
                        1 if page_has_more else 0,
                        normalized_page_limit,
                        normalized_snapshot_at,
                        normalized_delivery_token,
                        next_version,
                        now,
                    ),
                )
        return {
            "ok": True,
            "stream": normalized_stream,
            "cursor": row["cursor"] if row else None,
            "pending_cursor": normalized_cursor,
            "pending_request_cursor": normalized_request_cursor,
            "pending_request_since": normalized_request_since,
            "pending_refs": normalized_refs,
            "pending_page_refs": normalized_page_refs,
            "pending_baseline": bool(baseline),
            "pending_page_has_more": bool(page_has_more),
            "pending_page_limit": normalized_page_limit,
            "pending_snapshot_at": normalized_snapshot_at,
            "pending_delivery_token": normalized_delivery_token,
            "last_attempt_status": "pending",
            "state_version": next_version,
            "deduplicated": False,
        }

    def acknowledge_store_checkpoint_page(
        self,
        *,
        stream: str,
        cursor: str,
        delivery_token: str,
        expected_state_version: int,
    ) -> dict[str, Any]:
        """Advance only the acknowledged in-flight traversal, never final high-water."""

        self.initialize()
        normalized_stream = str(stream or "").strip().casefold()
        normalized_cursor = str(cursor or "").strip()
        normalized_token = str(delivery_token or "").strip()
        if normalized_stream not in STORE_CHECKPOINT_STREAMS:
            return {"ok": False, "error": "store_checkpoint_stream_invalid"}
        if not normalized_cursor or re.fullmatch(r"[0-9a-f]{64}", normalized_token) is None:
            return {"ok": False, "error": "store_checkpoint_ack_fields_invalid"}
        now = _now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM store_checkpoints WHERE stream = ? LIMIT 1",
                (normalized_stream,),
            ).fetchone()
            current_version = int(row["state_version"] or 0) if row else 0
            if int(expected_state_version) != current_version:
                return {
                    "ok": False,
                    "error": "store_checkpoint_state_conflict",
                    "stream": normalized_stream,
                    "expected_state_version": int(expected_state_version),
                    "current_state_version": current_version,
                }
            if (
                row is None
                or str(row["pending_cursor"] or "") != normalized_cursor
                or str(row["pending_delivery_token"] or "") != normalized_token
                or not bool(row["pending_page_has_more"])
            ):
                return {"ok": False, "error": "store_checkpoint_ack_stale_or_final"}
            next_version = current_version + 1
            conn.execute(
                """
                UPDATE store_checkpoints
                SET traversal_cursor = pending_cursor,
                    traversal_refs_json = pending_refs_json,
                    traversal_baseline = pending_baseline,
                    traversal_snapshot_at = pending_snapshot_at,
                    pending_cursor = NULL, pending_request_cursor = NULL,
                    pending_request_since = NULL, pending_refs_json = '[]',
                    pending_page_refs_json = '[]', pending_baseline = 0,
                    pending_page_has_more = 0, pending_page_limit = 25,
                    pending_snapshot_at = NULL, pending_delivery_token = NULL,
                    last_ack_cursor = ?, last_ack_delivery_token = ?,
                    last_ack_snapshot_at = ?, last_ack_was_final = 0,
                    last_attempt_status = 'traversing', last_error_code = '',
                    state_version = ?, updated_at = ?
                WHERE stream = ? AND state_version = ?
                """,
                (
                    normalized_cursor,
                    normalized_token,
                    str(row["pending_snapshot_at"] or "") or None,
                    next_version,
                    now,
                    normalized_stream,
                    current_version,
                ),
            )
        return {
            "ok": True,
            "stream": normalized_stream,
            "cursor": row["cursor"],
            "traversal_cursor": normalized_cursor,
            "traversal_refs": _decode_json(row["pending_refs_json"], []),
            "traversal_baseline": bool(row["pending_baseline"]),
            "traversal_snapshot_at": str(row["pending_snapshot_at"] or "") or None,
            "last_ack_cursor": normalized_cursor,
            "last_ack_delivery_token": normalized_token,
            "last_ack_was_final": False,
            "last_attempt_status": "traversing",
            "state_version": next_version,
        }

    def commit_store_checkpoint(
        self,
        *,
        stream: str,
        cursor: str,
        last_success_at: str,
        compact_refs: list[dict[str, Any]] | None,
        expected_state_version: int,
        acknowledged_delivery_token: str | None = None,
    ) -> dict[str, Any]:
        """Atomically commit a fully consumed store cursor using compare-and-swap."""

        self.initialize()
        normalized_stream = str(stream or "").strip().casefold()
        normalized_cursor = str(cursor or "").strip()
        normalized_success_at = str(last_success_at or "").strip()
        normalized_ack_token = str(acknowledged_delivery_token or "").strip() or None
        if normalized_stream not in STORE_CHECKPOINT_STREAMS:
            return {"ok": False, "error": "store_checkpoint_stream_invalid"}
        if not normalized_cursor or not normalized_success_at:
            return {"ok": False, "error": "store_checkpoint_fields_required"}
        if len(normalized_cursor) > 4096:
            return {"ok": False, "error": "store_checkpoint_cursor_too_large"}
        if normalized_ack_token is not None and re.fullmatch(r"[0-9a-f]{64}", normalized_ack_token) is None:
            return {"ok": False, "error": "store_checkpoint_delivery_token_invalid"}
        try:
            normalized_refs = _normalize_store_checkpoint_refs(compact_refs)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        refs_json = json.dumps(normalized_refs, ensure_ascii=False, separators=(",", ":"))
        now = _now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM store_checkpoints WHERE stream = ? LIMIT 1", (normalized_stream,)
            ).fetchone()
            current_version = int(row["state_version"] or 0) if row else 0
            if row:
                same_candidate = (
                    str(row["cursor"] or "") == normalized_cursor
                    and str(row["last_success_at"] or "") == normalized_success_at
                    and _decode_json(row["compact_refs_json"], []) == normalized_refs
                    and str(row["last_attempt_status"] or "") == "success"
                    and not str(row["pending_cursor"] or "")
                    and not str(row["traversal_cursor"] or "")
                )
                if same_candidate:
                    return {
                        "ok": True,
                        "stream": normalized_stream,
                        "cursor": normalized_cursor,
                        "last_success_at": normalized_success_at,
                        "compact_refs": normalized_refs,
                        "last_attempt_status": "success",
                        "state_version": current_version,
                        "deduplicated": True,
                    }
            if int(expected_state_version) != current_version:
                return {
                    "ok": False,
                    "error": "store_checkpoint_state_conflict",
                    "stream": normalized_stream,
                    "expected_state_version": int(expected_state_version),
                    "current_state_version": current_version,
                }
            next_version = current_version + 1
            if row:
                conn.execute(
                    """
                    UPDATE store_checkpoints
                    SET cursor = ?, last_success_at = ?, compact_refs_json = ?,
                        traversal_cursor = NULL, traversal_refs_json = '[]',
                        traversal_baseline = 0, traversal_snapshot_at = NULL,
                        pending_cursor = NULL, pending_request_cursor = NULL,
                        pending_request_since = NULL, pending_refs_json = '[]',
                        pending_page_refs_json = '[]', pending_baseline = 0,
                        pending_page_has_more = 0, pending_page_limit = 25,
                        pending_snapshot_at = NULL, pending_delivery_token = NULL,
                        last_ack_cursor = ?, last_ack_delivery_token = ?,
                        last_ack_snapshot_at = ?, last_ack_was_final = ?,
                        last_attempt_status = 'success', last_error_code = '',
                        state_version = ?, updated_at = ?
                    WHERE stream = ? AND state_version = ?
                    """,
                    (
                        normalized_cursor,
                        normalized_success_at,
                        refs_json,
                        normalized_cursor if normalized_ack_token else None,
                        normalized_ack_token,
                        normalized_success_at if normalized_ack_token else None,
                        1 if normalized_ack_token else 0,
                        next_version,
                        now,
                        normalized_stream,
                        current_version,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO store_checkpoints (
                        stream, cursor, last_success_at, compact_refs_json,
                        traversal_cursor, traversal_refs_json, traversal_baseline,
                        traversal_snapshot_at,
                        pending_cursor, pending_request_cursor, pending_request_since,
                        pending_refs_json, pending_baseline, pending_page_has_more,
                        pending_snapshot_at, pending_delivery_token,
                        last_ack_cursor, last_ack_delivery_token,
                        last_ack_snapshot_at, last_ack_was_final,
                        last_attempt_status, last_error_code, state_version, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, NULL, '[]', 0, NULL,
                        NULL, NULL, NULL, '[]', 0, 0, NULL, NULL,
                        ?, ?, ?, ?,
                        'success', '', ?, ?
                    )
                    """,
                    (
                        normalized_stream,
                        normalized_cursor,
                        normalized_success_at,
                        refs_json,
                        normalized_cursor if normalized_ack_token else None,
                        normalized_ack_token,
                        normalized_success_at if normalized_ack_token else None,
                        1 if normalized_ack_token else 0,
                        next_version,
                        now,
                    ),
                )
        return {
            "ok": True,
            "stream": normalized_stream,
            "cursor": normalized_cursor,
            "last_success_at": normalized_success_at,
            "compact_refs": normalized_refs,
            "last_ack_cursor": normalized_cursor if normalized_ack_token else None,
            "last_ack_delivery_token": normalized_ack_token,
            "last_ack_was_final": bool(normalized_ack_token),
            "last_attempt_status": "success",
            "state_version": next_version,
            "deduplicated": False,
        }

    def record_store_checkpoint_failure(
        self,
        *,
        stream: str,
        error_code: str,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        """Record degraded read state while preserving the last successful cursor."""

        self.initialize()
        normalized_stream = str(stream or "").strip().casefold()
        normalized_error = re.sub(r"[^a-z0-9_]+", "_", str(error_code or "store_read_failed").casefold()).strip("_")[
            :120
        ]
        if normalized_stream not in STORE_CHECKPOINT_STREAMS:
            return {"ok": False, "error": "store_checkpoint_stream_invalid"}
        now = _now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM store_checkpoints WHERE stream = ? LIMIT 1", (normalized_stream,)
            ).fetchone()
            current_version = int(row["state_version"] or 0) if row else 0
            if expected_state_version is not None and int(expected_state_version) != current_version:
                return {
                    "ok": False,
                    "error": "store_checkpoint_state_conflict",
                    "stream": normalized_stream,
                    "expected_state_version": int(expected_state_version),
                    "current_state_version": current_version,
                }
            if row and row["last_attempt_status"] == "degraded" and row["last_error_code"] == normalized_error:
                return {
                    "ok": True,
                    "stream": normalized_stream,
                    "cursor": row["cursor"],
                    "last_success_at": row["last_success_at"],
                    "last_attempt_status": "degraded",
                    "last_error_code": normalized_error,
                    "state_version": current_version,
                    "deduplicated": True,
                }
            next_version = current_version + 1
            if row:
                conn.execute(
                    """
                    UPDATE store_checkpoints
                    SET last_attempt_status = 'degraded', last_error_code = ?,
                        state_version = ?, updated_at = ?
                    WHERE stream = ? AND state_version = ?
                    """,
                    (normalized_error, next_version, now, normalized_stream, current_version),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO store_checkpoints (
                        stream, cursor, last_success_at, compact_refs_json,
                        last_attempt_status, last_error_code, state_version, updated_at
                    ) VALUES (?, NULL, NULL, '[]', 'degraded', ?, ?, ?)
                    """,
                    (normalized_stream, normalized_error, next_version, now),
                )
        return {
            "ok": True,
            "stream": normalized_stream,
            "cursor": row["cursor"] if row else None,
            "last_success_at": row["last_success_at"] if row else None,
            "last_attempt_status": "degraded",
            "last_error_code": normalized_error,
            "state_version": next_version,
            "deduplicated": False,
        }

    def reset_store_checkpoint_for_rebaseline(
        self,
        *,
        stream: str,
        expected_state_version: int,
        reason: str,
    ) -> dict[str, Any]:
        """Reset exactly one Store stream after a verified cursor epoch/restore failure."""

        self.initialize()
        normalized_stream = str(stream or "").strip().casefold()
        if normalized_stream not in STORE_CHECKPOINT_STREAMS:
            return {"ok": False, "error": "store_checkpoint_stream_invalid"}
        normalized_reason = re.sub(
            r"[^a-z0-9_]+",
            "_",
            str(reason or "").strip().casefold(),
        ).strip("_")[:120]
        if normalized_reason not in {
            "cursor_generation_mismatch",
            "cursor_ahead_after_store_restore",
            "operator_verified_rebaseline",
        }:
            return {"ok": False, "error": "store_checkpoint_reset_reason_invalid"}

        now = _now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM store_checkpoints WHERE stream = ? LIMIT 1",
                (normalized_stream,),
            ).fetchone()
            current_version = int(row["state_version"] or 0) if row else 0
            if int(expected_state_version) != current_version:
                return {
                    "ok": False,
                    "error": "store_checkpoint_state_conflict",
                    "stream": normalized_stream,
                    "expected_state_version": int(expected_state_version),
                    "current_state_version": current_version,
                }
            next_version = current_version + 1
            if row:
                conn.execute(
                    """
                    UPDATE store_checkpoints
                    SET cursor = NULL, last_success_at = NULL, compact_refs_json = '[]',
                        traversal_cursor = NULL, traversal_refs_json = '[]',
                        traversal_baseline = 0, traversal_snapshot_at = NULL,
                        pending_cursor = NULL, pending_request_cursor = NULL,
                        pending_request_since = NULL, pending_refs_json = '[]',
                        pending_page_refs_json = '[]', pending_baseline = 0,
                        pending_page_has_more = 0, pending_page_limit = 25,
                        pending_snapshot_at = NULL, pending_delivery_token = NULL,
                        last_ack_cursor = NULL, last_ack_delivery_token = NULL,
                        last_ack_snapshot_at = NULL, last_ack_was_final = 0,
                        last_attempt_status = 'reset', last_error_code = ?,
                        state_version = ?, updated_at = ?
                    WHERE stream = ? AND state_version = ?
                    """,
                    (
                        normalized_reason,
                        next_version,
                        now,
                        normalized_stream,
                        current_version,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO store_checkpoints (
                        stream, cursor, last_success_at, compact_refs_json,
                        traversal_cursor, traversal_refs_json, traversal_baseline,
                        traversal_snapshot_at,
                        pending_cursor, pending_request_cursor, pending_request_since,
                        pending_refs_json, pending_baseline, pending_page_has_more,
                        pending_snapshot_at, pending_delivery_token,
                        last_ack_cursor, last_ack_delivery_token,
                        last_ack_snapshot_at, last_ack_was_final,
                        last_attempt_status, last_error_code, state_version, updated_at
                    ) VALUES (
                        ?, NULL, NULL, '[]', NULL, '[]', 0, NULL,
                        NULL, NULL, NULL, '[]', 0, 0, NULL, NULL,
                        NULL, NULL, NULL, 0, 'reset', ?, ?, ?
                    )
                    """,
                    (normalized_stream, normalized_reason, next_version, now),
                )
        return {
            "ok": True,
            "stream": normalized_stream,
            "cursor": None,
            "pending_cursor": None,
            "last_attempt_status": "reset",
            "last_error_code": normalized_reason,
            "state_version": next_version,
            "rebaseline_required": True,
        }

    def list_active_manager_runs(self, *, limit: int = 100) -> dict[str, Any]:
        """Read every active run directly instead of sampling recent history."""

        self.initialize()
        limit = max(1, min(int(limit), 500))
        placeholders = ",".join("?" for _ in ACTIVE_WORKFLOW_STATES)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, workflow_id, intent, status, checkpoint_json, state_version, updated_at
                FROM manager_runs
                WHERE status IN ({placeholders})
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                [*sorted(ACTIVE_WORKFLOW_STATES), limit],
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["checkpoint"] = _safe_bootstrap_checkpoint(_decode_json(item.pop("checkpoint_json"), {}))
            items.append(item)
        return {"ok": True, "items": items, "total_returned": len(items)}

    def start_manager_run(
        self,
        *,
        intent: str,
        query: str = "",
        dry_run: bool = False,
        source: str = "codex",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        now = _now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO manager_runs
                    (intent, query, status, dry_run, source, metadata_json, started_at, updated_at)
                VALUES (?, ?, 'running', ?, ?, ?, ?, ?)
                """,
                (intent, query, 1 if dry_run else 0, source, json.dumps(metadata or {}, ensure_ascii=False), now, now),
            )
        return {"ok": True, "id": cursor.lastrowid, "started_at": now, "status": "running"}

    def start_workflow_run(
        self,
        *,
        workflow_id: str,
        intent: str,
        query: str = "",
        request_id: str = "",
        idempotency_key: str,
        correlation_id: str = "",
        actor: str = "codex-owner-agent",
        scope: dict[str, Any] | None = None,
        selected_ids: list[str] | None = None,
        dry_run: bool = False,
        source: str = "codex",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start an idempotent v2 workflow without changing any external system."""

        self.initialize()
        workflow_id = str(workflow_id or "").strip()
        intent = str(intent or "").strip()
        idempotency_key = str(idempotency_key or "").strip()
        if not workflow_id:
            return {"ok": False, "error": "workflow_id is required"}
        if not intent:
            return {"ok": False, "error": "intent is required"}
        if not idempotency_key:
            return {"ok": False, "error": "idempotency_key is required"}

        now = _now()
        effective_request_id = str(request_id or "").strip() or str(uuid.uuid4())
        effective_correlation_id = str(correlation_id or "").strip() or effective_request_id
        normalized_ids = _unique_string_values(selected_ids, limit=1000)
        scope_provided = isinstance(scope, dict)
        selected_ids_provided = selected_ids is not None
        scope_payload = dict(scope) if isinstance(scope, dict) else {}
        metadata_payload = metadata if isinstance(metadata, dict) else {}
        forbidden = _find_forbidden_body_keys({"scope": scope_payload, "metadata": metadata_payload})
        if forbidden:
            return {
                "ok": False,
                "error": "raw_external_body_not_allowed_in_manager_ledger",
                "forbidden_keys": forbidden,
            }
        if _is_store_workflow(workflow_id=workflow_id, intent=intent, scope=scope_payload):
            store_forbidden = _find_forbidden_store_payload_keys({"scope": scope_payload, "metadata": metadata_payload})
            store_forbidden.extend(
                _store_start_channel_forbidden(
                    workflow_id=workflow_id,
                    intent=intent,
                    query=query,
                    request_id=effective_request_id,
                    idempotency_key=idempotency_key,
                    correlation_id=effective_correlation_id,
                    actor=str(actor or "codex-owner-agent"),
                    source=str(source or "codex"),
                    scope=scope_payload,
                    metadata=metadata_payload,
                    selected_ids=normalized_ids,
                )
            )
            if store_forbidden:
                return {
                    "ok": False,
                    "error": "raw_store_payload_not_allowed_in_manager_ledger",
                    "forbidden_keys": list(dict.fromkeys(store_forbidden)),
                }
        with self.connect() as conn:
            # Serialize the idempotency lookup and insert so concurrent stateless
            # MCP requests deduplicate instead of racing into the unique index.
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM manager_runs WHERE idempotency_key = ? LIMIT 1",
                (idempotency_key,),
            ).fetchone()
            if existing:
                item = self._row_to_dict(existing)
                conflict_fields: list[str] = []
                if item.get("workflow_id") != workflow_id:
                    conflict_fields.append("workflow_id")
                if item.get("intent") != intent:
                    conflict_fields.append("intent")
                if scope_provided and _decode_json(existing["scope_json"], {}) != scope_payload:
                    conflict_fields.append("scope")
                if selected_ids_provided and _decode_json(existing["selected_ids_json"], []) != normalized_ids:
                    conflict_fields.append("selected_ids")
                if bool(existing["dry_run"]) != bool(dry_run):
                    conflict_fields.append("dry_run")
                if conflict_fields:
                    return {
                        "ok": False,
                        "error": "idempotency_key_conflict",
                        "id": item.get("id"),
                        "status": item.get("status"),
                        "conflict_fields": conflict_fields,
                    }
                return {"ok": True, **item, "deduplicated": True}

            cursor = conn.execute(
                """
                INSERT INTO manager_runs (
                    intent, workflow_id, query, status, dry_run, source, request_id,
                    idempotency_key, correlation_id, actor, scope_json, selected_ids_json,
                    checkpoint_json, compensation_json, state_version, metadata_json,
                    started_at, updated_at
                ) VALUES (?, ?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?, ?, '{}', '[]', 1, ?, ?, ?)
                """,
                (
                    intent,
                    workflow_id,
                    query,
                    1 if dry_run else 0,
                    source,
                    effective_request_id,
                    idempotency_key,
                    effective_correlation_id,
                    str(actor or "codex-owner-agent"),
                    json.dumps(scope_payload, ensure_ascii=False),
                    json.dumps(normalized_ids, ensure_ascii=False),
                    json.dumps(metadata_payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            run_id = _required_lastrowid(cursor)
            conn.execute(
                """
                INSERT INTO manager_run_events
                    (run_id, event_type, message, payload_json, created_at)
                VALUES (?, 'workflow_started', ?, ?, ?)
                """,
                (
                    run_id,
                    workflow_id,
                    json.dumps(
                        {"workflow_id": workflow_id, "request_id": effective_request_id},
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
        return {
            "ok": True,
            "id": run_id,
            "workflow_id": workflow_id,
            "intent": intent,
            "request_id": effective_request_id,
            "correlation_id": effective_correlation_id,
            "idempotency_key": idempotency_key,
            "status": "planned",
            "state_version": 1,
            "started_at": now,
            "deduplicated": False,
        }

    def transition_workflow_run(
        self,
        run_id: int,
        *,
        status: str,
        message: str = "",
        verification: dict[str, Any] | None = None,
        summary: str = "",
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        target_status = str(status or "").strip().casefold()
        now = _now()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM manager_runs WHERE id = ? LIMIT 1", (run_id,)).fetchone()
            if not row:
                return {"ok": False, "error": "manager run not found", "run_id": run_id}
            current = str(row["status"] or "")
            current_version = int(row["state_version"] or 1)
            if _is_store_workflow(
                workflow_id=row["workflow_id"], intent=row["intent"], scope=_decode_json(row["scope_json"], {})
            ):
                if len(str(summary or "").encode("utf-8")) > 4096:
                    return {"ok": False, "error": "store_workflow_summary_too_large", "run_id": run_id}
                scope_payload = _decode_json(row["scope_json"], {})
                operation = _store_workflow_operation(
                    workflow_id=row["workflow_id"],
                    intent=row["intent"],
                    scope=scope_payload,
                )
                store_forbidden = _find_forbidden_store_payload_keys(verification or {})
                if verification and _safe_store_scalar_map(verification, kind="verification") is None:
                    store_forbidden.append("verification")
                if not _store_message_is_allowed(message, operation=operation):
                    store_forbidden.append("message")
                if not _store_summary_is_allowed(summary, operation=operation):
                    store_forbidden.append("summary")
                if store_forbidden:
                    return {
                        "ok": False,
                        "error": "raw_store_payload_not_allowed_in_manager_ledger",
                        "run_id": run_id,
                        "forbidden_keys": list(dict.fromkeys(store_forbidden)),
                    }
            conflict = _workflow_state_conflict(
                run_id,
                expected_state_version=expected_state_version,
                current_state_version=current_version,
            )
            if conflict:
                return conflict
            if current == target_status:
                if target_status == "completed":
                    verification_payload = (
                        verification if isinstance(verification, dict) else _decode_json(row["verification_json"], {})
                    )
                    completion_error = _completion_verification_error(
                        run_id,
                        current_status=current,
                        verification=verification_payload,
                    )
                    if completion_error:
                        return completion_error
                return {
                    "ok": True,
                    "id": run_id,
                    "status": current,
                    "state_version": current_version,
                    "deduplicated": True,
                }
            allowed = WORKFLOW_TRANSITIONS.get(current, set())
            if target_status not in allowed:
                return {
                    "ok": False,
                    "error": "invalid_workflow_transition",
                    "run_id": run_id,
                    "from_status": current,
                    "to_status": target_status,
                    "allowed": sorted(allowed),
                }
            if current == "external_wait" and target_status in {"executing", "verifying"}:
                pending = conn.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM manager_run_external_steps
                    WHERE run_id = ? AND status <> 'completed'
                    """,
                    (run_id,),
                ).fetchone()
                if int(pending["count"] or 0) > 0:
                    return {
                        "ok": False,
                        "error": "external_steps_pending",
                        "run_id": run_id,
                        "status": current,
                    }
            next_version = current_version + 1
            finished_at = now if target_status in WORKFLOW_TERMINAL_STATES else None
            verification_payload = (
                verification if isinstance(verification, dict) else _decode_json(row["verification_json"], {})
            )
            if target_status == "completed":
                completion_error = _completion_verification_error(
                    run_id,
                    current_status=current,
                    verification=verification_payload,
                )
                if completion_error:
                    return completion_error
            effective_summary = str(summary) if summary else str(row["summary"] or "")
            cursor = conn.execute(
                """
                UPDATE manager_runs
                SET status = ?, state_version = ?, summary = ?, verification_json = ?,
                    finished_at = ?, updated_at = ?
                WHERE id = ? AND state_version = ?
                """,
                (
                    target_status,
                    next_version,
                    effective_summary,
                    json.dumps(verification_payload, ensure_ascii=False),
                    finished_at,
                    now,
                    run_id,
                    current_version,
                ),
            )
            if cursor.rowcount != 1:
                return {"ok": False, "error": "workflow_state_conflict", "run_id": run_id}
            conn.execute(
                """
                INSERT INTO manager_run_events
                    (run_id, event_type, message, payload_json, created_at)
                VALUES (?, 'state_transition', ?, ?, ?)
                """,
                (
                    run_id,
                    message,
                    json.dumps(
                        {"from": current, "to": target_status, "state_version": next_version}, ensure_ascii=False
                    ),
                    now,
                ),
            )
        return {
            "ok": True,
            "id": run_id,
            "status": target_status,
            "state_version": next_version,
            "finished_at": finished_at,
            "deduplicated": False,
        }

    def checkpoint_workflow_run(
        self,
        run_id: int,
        *,
        checkpoint: dict[str, Any],
        selected_ids: list[str] | None = None,
        message: str = "",
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        checkpoint_payload = checkpoint if isinstance(checkpoint, dict) else {}
        invalid_keys = _find_forbidden_body_keys(checkpoint_payload)
        if invalid_keys:
            return {
                "ok": False,
                "error": "raw_external_body_not_allowed_in_manager_ledger",
                "forbidden_keys": invalid_keys,
            }
        encoded = json.dumps(checkpoint_payload, ensure_ascii=False, default=str)
        if len(encoded.encode("utf-8")) > 16_384:
            return {"ok": False, "error": "checkpoint_too_large", "max_bytes": 16_384}
        now = _now()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM manager_runs WHERE id = ? LIMIT 1", (run_id,)).fetchone()
            if not row:
                return {"ok": False, "error": "manager run not found", "run_id": run_id}
            if _is_store_workflow(
                workflow_id=row["workflow_id"], intent=row["intent"], scope=_decode_json(row["scope_json"], {})
            ):
                store_forbidden = _find_forbidden_store_payload_keys(checkpoint_payload)
                store_forbidden.extend(_find_unsafe_store_machine_values(checkpoint_payload))
                unknown_keys = sorted(set(checkpoint_payload).difference(STORE_LEDGER_SAFE_CHECKPOINT_KEYS))
                operation = _store_workflow_operation(
                    workflow_id=row["workflow_id"],
                    intent=row["intent"],
                    scope=_decode_json(row["scope_json"], {}),
                )
                if not _store_message_is_allowed(message, operation=operation):
                    store_forbidden.append("message")
                if selected_ids is not None:
                    store_forbidden.extend(
                        _find_unsafe_store_machine_values(
                            _unique_string_values(selected_ids, limit=1000),
                            prefix="selected_ids",
                        )
                    )
                if store_forbidden or unknown_keys:
                    return {
                        "ok": False,
                        "error": "raw_store_payload_not_allowed_in_manager_ledger",
                        "run_id": run_id,
                        "forbidden_keys": list(dict.fromkeys([*store_forbidden, *unknown_keys])),
                    }
            current_version = int(row["state_version"] or 1)
            conflict = _workflow_state_conflict(
                run_id,
                expected_state_version=expected_state_version,
                current_state_version=current_version,
            )
            if conflict:
                return conflict
            if str(row["status"] or "") in WORKFLOW_TERMINAL_STATES:
                return {"ok": False, "error": "workflow_is_terminal", "run_id": run_id, "status": row["status"]}
            next_version = current_version + 1
            ids_json = row["selected_ids_json"]
            if selected_ids is not None:
                ids_json = json.dumps(_unique_string_values(selected_ids, limit=1000), ensure_ascii=False)
            cursor = conn.execute(
                """
                UPDATE manager_runs
                SET checkpoint_json = ?, selected_ids_json = ?, state_version = ?, updated_at = ?
                WHERE id = ? AND state_version = ?
                """,
                (encoded, ids_json, next_version, now, run_id, current_version),
            )
            if cursor.rowcount != 1:
                return {
                    "ok": False,
                    "error": "workflow_state_conflict",
                    "run_id": run_id,
                    "expected_state_version": current_version,
                }
            conn.execute(
                """
                INSERT INTO manager_run_events
                    (run_id, event_type, message, payload_json, created_at)
                VALUES (?, 'checkpoint', ?, ?, ?)
                """,
                (run_id, message, json.dumps({"state_version": next_version}, ensure_ascii=False), now),
            )
        return {
            "ok": True,
            "id": run_id,
            "status": row["status"],
            "state_version": next_version,
            "checkpoint": checkpoint_payload,
        }

    def register_external_step(
        self,
        run_id: int,
        *,
        step_id: str,
        connector: str,
        action: str,
        request_refs: dict[str, Any] | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        step_id = str(step_id or "").strip()
        connector = str(connector or "").strip().casefold()
        action = str(action or "").strip()
        if not step_id or not connector or not action:
            return {"ok": False, "error": "step_id, connector, and action are required"}
        sanitized, forbidden = _sanitize_external_refs(request_refs)
        if forbidden:
            return {
                "ok": False,
                "error": "raw_external_body_not_allowed_in_manager_ledger",
                "forbidden_keys": forbidden,
            }
        now = _now()
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM manager_runs WHERE id = ? LIMIT 1", (run_id,)).fetchone()
            if not run:
                return {"ok": False, "error": "manager run not found", "run_id": run_id}
            status = str(run["status"] or "")
            current_version = int(run["state_version"] or 1)
            conflict = _workflow_state_conflict(
                run_id,
                expected_state_version=expected_state_version,
                current_state_version=current_version,
            )
            if conflict:
                return conflict
            if status not in {"executing", "external_wait"}:
                return {
                    "ok": False,
                    "error": "external_step_requires_executing_workflow",
                    "run_id": run_id,
                    "status": status,
                }
            existing = conn.execute(
                "SELECT * FROM manager_run_external_steps WHERE run_id = ? AND step_id = ? LIMIT 1",
                (run_id, step_id),
            ).fetchone()
            if existing:
                return {
                    "ok": True,
                    "run_id": run_id,
                    "step_id": step_id,
                    "status": existing["status"],
                    "state_version": current_version,
                    "deduplicated": True,
                }
            next_version = current_version + 1
            cursor = conn.execute(
                """
                UPDATE manager_runs
                SET status = 'external_wait', state_version = ?, updated_at = ?
                WHERE id = ? AND state_version = ?
                """,
                (next_version, now, run_id, current_version),
            )
            if cursor.rowcount != 1:
                return {
                    "ok": False,
                    "error": "workflow_state_conflict",
                    "run_id": run_id,
                    "expected_state_version": current_version,
                }
            conn.execute(
                """
                INSERT INTO manager_run_external_steps
                    (run_id, step_id, connector, action, status, request_refs_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (run_id, step_id, connector, action, json.dumps(sanitized, ensure_ascii=False), now, now),
            )
            conn.execute(
                """
                INSERT INTO manager_run_events
                    (run_id, event_type, message, target_type, target_id, payload_json, created_at)
                VALUES (?, 'external_step_requested', ?, 'external_step', ?, ?, ?)
                """,
                (
                    run_id,
                    f"{connector}:{action}",
                    step_id,
                    json.dumps({"connector": connector, "action": action}, ensure_ascii=False),
                    now,
                ),
            )
        return {
            "ok": True,
            "run_id": run_id,
            "step_id": step_id,
            "connector": connector,
            "action": action,
            "status": "pending",
            "workflow_status": "external_wait",
            "state_version": next_version,
            "deduplicated": False,
        }

    def complete_external_step(
        self,
        run_id: int,
        *,
        step_id: str,
        result_refs: dict[str, Any] | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        step_id = str(step_id or "").strip()
        sanitized, forbidden = _sanitize_external_refs(result_refs)
        if forbidden:
            return {
                "ok": False,
                "error": "raw_external_body_not_allowed_in_manager_ledger",
                "forbidden_keys": forbidden,
            }
        if not step_id:
            return {"ok": False, "error": "step_id is required"}
        if not sanitized:
            return {"ok": False, "error": "at least one external result reference is required"}
        now = _now()
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM manager_runs WHERE id = ? LIMIT 1", (run_id,)).fetchone()
            if not run:
                return {"ok": False, "error": "manager run not found", "run_id": run_id}
            current_version = int(run["state_version"] or 1)
            conflict = _workflow_state_conflict(
                run_id,
                expected_state_version=expected_state_version,
                current_state_version=current_version,
            )
            if conflict:
                return conflict
            if str(run["status"] or "") in WORKFLOW_TERMINAL_STATES:
                return {
                    "ok": False,
                    "error": "workflow_is_terminal",
                    "run_id": run_id,
                    "status": run["status"],
                }
            step = conn.execute(
                "SELECT * FROM manager_run_external_steps WHERE run_id = ? AND step_id = ? LIMIT 1",
                (run_id, step_id),
            ).fetchone()
            if not step:
                return {"ok": False, "error": "external step not found", "run_id": run_id, "step_id": step_id}
            if (
                step["connector"] == "gmail"
                and step["action"] in {"send", "forward"}
                and not any(sanitized.get(key) for key in ("message_id", "thread_id", "draft_id", "external_ref"))
            ):
                return {
                    "ok": False,
                    "error": "gmail_message_or_thread_result_ref_required",
                    "run_id": run_id,
                    "step_id": step_id,
                }
            if step["status"] == "completed":
                previous = _decode_json(step["result_refs_json"], {})
                if previous != sanitized:
                    return {
                        "ok": False,
                        "error": "external_step_result_conflict",
                        "run_id": run_id,
                        "step_id": step_id,
                    }
                return {
                    "ok": True,
                    "run_id": run_id,
                    "step_id": step_id,
                    "status": "completed",
                    "state_version": current_version,
                    "deduplicated": True,
                }
            next_version = current_version + 1
            cursor = conn.execute(
                """
                UPDATE manager_runs
                SET state_version = ?, updated_at = ?
                WHERE id = ? AND state_version = ?
                """,
                (next_version, now, run_id, current_version),
            )
            if cursor.rowcount != 1:
                return {
                    "ok": False,
                    "error": "workflow_state_conflict",
                    "run_id": run_id,
                    "expected_state_version": current_version,
                }
            conn.execute(
                """
                UPDATE manager_run_external_steps
                SET status = 'completed', result_refs_json = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (json.dumps(sanitized, ensure_ascii=False), now, now, step["id"]),
            )
            conn.execute(
                """
                INSERT INTO manager_run_events
                    (run_id, event_type, message, target_type, target_id, payload_json, created_at)
                VALUES (?, 'external_step_completed', ?, 'external_step', ?, ?, ?)
                """,
                (
                    run_id,
                    f"{step['connector']}:{step['action']}",
                    step_id,
                    json.dumps({"result_ref_keys": sorted(sanitized)}, ensure_ascii=False),
                    now,
                ),
            )
        return {
            "ok": True,
            "run_id": run_id,
            "step_id": step_id,
            "status": "completed",
            "state_version": next_version,
            "result_refs": sanitized,
            "deduplicated": False,
        }

    def resume_workflow_run(self, run_id: int, *, expected_state_version: int | None = None) -> dict[str, Any]:
        self.initialize()
        run = self.get_manager_run(run_id, include_events=False, include_external_steps=True)
        if not run.get("ok"):
            return run
        item = run["item"]
        status = str(item.get("status") or "")
        current_version = int(item.get("state_version") or 1)
        conflict = _workflow_state_conflict(
            run_id,
            expected_state_version=expected_state_version,
            current_state_version=current_version,
        )
        if conflict:
            return conflict
        if status in WORKFLOW_TERMINAL_STATES:
            return {"ok": False, "error": "workflow_is_terminal", "run_id": run_id, "status": status}
        pending = [step for step in item.get("external_steps", []) if step.get("status") != "completed"]
        if status == "external_wait" and pending:
            return {
                "ok": False,
                "error": "external_steps_pending",
                "run_id": run_id,
                "status": status,
                "pending_step_ids": [step.get("step_id") for step in pending],
                "checkpoint": item.get("checkpoint", {}),
            }
        if status in {"planned", "external_wait"}:
            transitioned = self.transition_workflow_run(
                run_id,
                status="executing",
                message="workflow resumed",
                expected_state_version=current_version,
            )
            if not transitioned.get("ok"):
                return transitioned
            status = "executing"
            current_version = int(transitioned.get("state_version") or current_version)
        return {
            "ok": True,
            "run_id": run_id,
            "status": status,
            "state_version": current_version,
            "checkpoint": item.get("checkpoint", {}),
            "selected_ids": item.get("selected_ids", []),
            "next_action": (item.get("checkpoint") or {}).get("next_action"),
        }

    def cancel_workflow_run(
        self,
        run_id: int,
        *,
        reason: str = "",
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        return self.transition_workflow_run(
            run_id,
            status="cancelled",
            message=reason or "workflow cancelled",
            expected_state_version=expected_state_version,
        )

    def record_manager_run_event(
        self,
        run_id: int,
        *,
        event_type: str,
        message: str = "",
        target_type: str = "",
        target_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        event_payload = payload if isinstance(payload, dict) else {}
        forbidden = _find_forbidden_body_keys(event_payload)
        if forbidden:
            return {
                "ok": False,
                "error": "raw_external_body_not_allowed_in_manager_ledger",
                "forbidden_keys": forbidden,
                "run_id": run_id,
            }
        now = _now()
        with self.connect() as conn:
            run = conn.execute(
                "SELECT id, workflow_id, intent, scope_json FROM manager_runs WHERE id = ? LIMIT 1",
                (run_id,),
            ).fetchone()
            if not run:
                return {"ok": False, "error": "manager run not found", "run_id": run_id}
            if _is_store_workflow(
                workflow_id=run["workflow_id"],
                intent=run["intent"],
                scope=_decode_json(run["scope_json"], {}),
            ):
                store_forbidden = _find_forbidden_store_payload_keys(event_payload)
                if event_payload and _safe_store_scalar_map(event_payload, kind="verification") is None:
                    store_forbidden.append("payload")
                operation = _store_workflow_operation(
                    workflow_id=run["workflow_id"],
                    intent=run["intent"],
                    scope=_decode_json(run["scope_json"], {}),
                )
                if str(event_type or "").strip().casefold() not in STORE_LEDGER_SAFE_EVENT_TYPES:
                    store_forbidden.append("event_type")
                if not _store_message_is_allowed(message, operation=operation):
                    store_forbidden.append("message")
                normalized_target_type = str(target_type or "").strip().casefold()
                if normalized_target_type and normalized_target_type not in STORE_LEDGER_REF_ENTITIES:
                    store_forbidden.append("target_type")
                if str(target_id or "").strip() and not _store_machine_value_is_safe(str(target_id).strip()):
                    store_forbidden.append("target_id")
                if store_forbidden:
                    return {
                        "ok": False,
                        "error": "raw_store_payload_not_allowed_in_manager_ledger",
                        "forbidden_keys": list(dict.fromkeys(store_forbidden)),
                        "run_id": run_id,
                    }
            cursor = conn.execute(
                """
                INSERT INTO manager_run_events
                    (run_id, event_type, message, target_type, target_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    event_type,
                    message,
                    target_type,
                    target_id,
                    json.dumps(event_payload, ensure_ascii=False),
                    now,
                ),
            )
            conn.execute("UPDATE manager_runs SET updated_at = ? WHERE id = ?", (now, run_id))
        return {"ok": True, "id": cursor.lastrowid, "run_id": run_id, "created_at": now}

    def finish_manager_run(
        self,
        run_id: int,
        *,
        status: str = "completed",
        summary: str = "",
        verification: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT workflow_id FROM manager_runs WHERE id = ? LIMIT 1",
                (run_id,),
            ).fetchone()
        if not existing:
            return {"ok": False, "error": "manager run not found", "run_id": run_id}
        if str(existing["workflow_id"] or "").strip():
            return self.transition_workflow_run(
                run_id,
                status=status,
                message="workflow finished through compatibility API",
                summary=summary,
                verification=verification,
            )
        now = _now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE manager_runs
                SET status = ?, summary = ?, verification_json = ?, finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, summary, json.dumps(verification or {}, ensure_ascii=False), now, now, run_id),
            )
        return {"ok": True, "id": run_id, "status": status, "finished_at": now}

    def list_manager_runs(self, *, limit: int = 20, include_events: bool = False) -> dict[str, Any]:
        self.initialize()
        limit = max(1, min(limit, 100))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM manager_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            items = [self._row_to_dict(row) for row in rows]
            if include_events and items:
                events_by_run: dict[int, list[dict[str, Any]]] = {int(item["id"]): [] for item in items}
                placeholders = ",".join("?" for _ in events_by_run)
                event_rows = conn.execute(
                    f"""
                    SELECT * FROM manager_run_events
                    WHERE run_id IN ({placeholders})
                    ORDER BY created_at ASC
                    """,
                    list(events_by_run.keys()),
                ).fetchall()
                for row in event_rows:
                    event = self._row_to_dict(row)
                    events_by_run[int(event["run_id"])].append(event)
                for item in items:
                    item["events"] = events_by_run[int(item["id"])]
        return {"ok": True, "items": items, "total_returned": len(items)}

    def get_manager_run(
        self,
        run_id: int,
        *,
        include_events: bool = True,
        include_external_steps: bool = True,
    ) -> dict[str, Any]:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM manager_runs WHERE id = ? LIMIT 1", (run_id,)).fetchone()
            if not row:
                return {"ok": False, "error": "manager run not found", "run_id": run_id}
            item = self._row_to_dict(row)
            if include_events:
                event_rows = conn.execute(
                    "SELECT * FROM manager_run_events WHERE run_id = ? ORDER BY created_at ASC",
                    (run_id,),
                ).fetchall()
                item["events"] = [self._row_to_dict(event) for event in event_rows]
            if include_external_steps:
                step_rows = conn.execute(
                    "SELECT * FROM manager_run_external_steps WHERE run_id = ? ORDER BY created_at ASC",
                    (run_id,),
                ).fetchall()
                item["external_steps"] = [self._row_to_dict(step) for step in step_rows]
        return {"ok": True, "item": item}

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        if "tags_json" in item:
            item["tags"] = _decode_json(item.pop("tags_json"), [])
        if "metadata_json" in item:
            item["metadata"] = _decode_json(item.pop("metadata_json"), {})
        if "verification_json" in item:
            item["verification"] = _decode_json(item.pop("verification_json"), {})
        if "payload_json" in item:
            item["payload"] = _decode_json(item.pop("payload_json"), {})
        if "scope_json" in item:
            item["scope"] = _decode_json(item.pop("scope_json"), {})
        if "selected_ids_json" in item:
            item["selected_ids"] = _decode_json(item.pop("selected_ids_json"), [])
        if "checkpoint_json" in item:
            item["checkpoint"] = _decode_json(item.pop("checkpoint_json"), {})
        if "compensation_json" in item:
            item["compensation"] = _decode_json(item.pop("compensation_json"), [])
        if "request_refs_json" in item:
            item["request_refs"] = _decode_json(item.pop("request_refs_json"), {})
        if "result_refs_json" in item:
            item["result_refs"] = _decode_json(item.pop("result_refs_json"), {})
        if "dry_run" in item:
            item["dry_run"] = bool(item["dry_run"])
        return item

    def _manager_rule_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM manager_rules").fetchone()
        return int(row["count"] or 0)

    def _count_rows(self, table: str, *, where: str | None = None) -> int:
        query = f"SELECT COUNT(*) AS count FROM {table}"
        if where:
            query += f" WHERE {where}"
        with self.connect() as conn:
            row = conn.execute(query).fetchone()
        return int(row["count"] or 0)

    def _section_summary(self, table: str, order_column: str, *, where: str | None = None) -> dict[str, Any]:
        query = f"SELECT COUNT(*) AS count, MAX({order_column}) AS last_updated FROM {table}"
        if where:
            query += f" WHERE {where}"
        with self.connect() as conn:
            row = conn.execute(query).fetchone()
        return {"count": int(row["count"] or 0), "last_updated": row["last_updated"]}

    def _score_memory_item(self, item: dict[str, Any], query: str, tokens: list[str]) -> tuple[int, list[str]]:
        if not tokens:
            return 0, []
        fields = self._memory_search_fields(item)
        score = 0
        matched_fields: list[str] = []
        normalized_query = query.casefold()
        for field, raw_value in fields.items():
            value = raw_value.casefold()
            if not value:
                continue
            field_score = 0
            if normalized_query and normalized_query in value:
                field_score += 20
            for token in tokens:
                if token in value:
                    field_score += self._memory_field_weight(field)
            if field_score:
                score += field_score
                matched_fields.append(field)
        return score, matched_fields

    def _memory_search_fields(self, item: dict[str, Any]) -> dict[str, str]:
        tags = " ".join(str(tag) for tag in (item.get("tags") or []))
        kind = item.get("kind")
        if kind == "note":
            return {
                "title": str(item.get("title") or ""),
                "content": str(item.get("content") or ""),
                "category": str(item.get("category") or ""),
                "source": str(item.get("source") or ""),
                "tags": tags,
            }
        if kind == "fact":
            return {
                "content": str(item.get("content") or ""),
                "category": str(item.get("category") or ""),
                "source": str(item.get("source") or ""),
                "tags": tags,
            }
        if kind == "lesson":
            return {
                "title": str(item.get("title") or ""),
                "content": str(item.get("content") or ""),
                "applies_to": str(item.get("applies_to") or ""),
                "signal": str(item.get("signal") or ""),
                "recommendation": str(item.get("recommendation") or ""),
                "avoid": str(item.get("avoid") or ""),
                "source": str(item.get("source") or ""),
                "tags": tags,
            }
        if kind in {"task", "reminder"}:
            return {
                "title": str(item.get("title") or ""),
                "details": str(item.get("details") or ""),
                "status": str(item.get("status") or ""),
                "source": str(item.get("source") or ""),
                "tags": tags,
            }
        if kind == "journal":
            return {
                "event": str(item.get("event") or ""),
                "source": str(item.get("source") or ""),
                "tags": tags,
            }
        if kind == "rule":
            return {
                "title": str(item.get("title") or ""),
                "rule": str(item.get("rule") or ""),
                "scope": str(item.get("scope") or ""),
                "source": str(item.get("source") or ""),
            }
        return {key: str(value or "") for key, value in item.items()}

    def _memory_field_weight(self, field: str) -> int:
        return {
            "title": 8,
            "tags": 7,
            "category": 5,
            "rule": 5,
            "content": 4,
            "details": 4,
            "event": 4,
            "recommendation": 6,
            "avoid": 5,
            "applies_to": 5,
            "signal": 3,
            "scope": 3,
            "status": 2,
            "source": 1,
        }.get(field, 1)
