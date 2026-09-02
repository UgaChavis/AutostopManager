from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, get_db_path


MANAGER_RULES_PATH = PROJECT_ROOT / "docs" / "agent" / "manager_rules.json"
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


def load_manager_rules() -> list[dict[str, Any]]:
    """Read the canonical runtime rules without copying them into SQLite."""

    try:
        payload = json.loads(MANAGER_RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    raw_rules = payload.get("rules") if isinstance(payload, dict) else None
    if not isinstance(raw_rules, list):
        return []
    rules: list[dict[str, Any]] = []
    for index, raw_rule in enumerate(raw_rules):
        if not isinstance(raw_rule, dict):
            continue
        title = str(raw_rule.get("id") or "").strip()
        rule = str(raw_rule.get("rule") or "").strip()
        if not title or not rule:
            continue
        priority = raw_rule.get("priority")
        rules.append(
            {
                "id": index + 1,
                "title": title,
                "rule": rule,
                "scope": str(raw_rule.get("scope") or "general"),
                "priority": priority if isinstance(priority, int) else 100,
                "source": "docs/agent/manager_rules.json",
                "created_at": "",
                "updated_at": "",
                "kind": "rule",
                "tags": [],
            }
        )
    return sorted(rules, key=lambda item: (int(item["priority"]), str(item["title"])))


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
    "expected_revision_sha256",
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
AGENT_EXECUTION_MODES = frozenset({"work", "learning"})
AGENT_REVIEW_OUTCOMES = frozenset({"confirmed", "partial", "failed", "deferred"})
AGENT_IMPROVEMENT_KINDS = frozenset(
    {
        "runtime_lesson",
        "instruction",
        "route",
        "tool_bug",
        "provider",
        "code",
        "integration",
    }
)
AGENT_IMPROVEMENT_RISKS = frozenset({"low", "medium", "high"})
AGENT_IMPROVEMENT_STATUSES = frozenset({"pending", "repairing", "verified", "promoted", "deferred", "rolled_back"})
AGENT_IMPROVEMENT_TERMINAL_STATUSES = frozenset({"promoted", "deferred", "rolled_back"})
AGENT_TOOL_EVENT_STATUSES = frozenset({"started", "succeeded", "failed", "skipped"})
GENERIC_JOURNAL_MAX_ENTRIES = 500
GENERIC_JOURNAL_MAX_EVENT_LENGTH = 768
AGENT_LEARNING_METADATA_KEYS = frozenset(
    {
        "action_kind",
        "assertions",
        "cache_hit",
        "candidate_id",
        "checks",
        "commit_ref",
        "contract_fingerprint",
        "contract_id",
        "counts",
        "deploy_ref",
        "error_code",
        "failure_class",
        "fallback_used",
        "latency_ms",
        "mode",
        "phase",
        "provider",
        "quality_score",
        "request_fingerprint",
        "response_schema",
        "response_shape",
        "review_status",
        "rollback_ref",
        "route_id",
        "safe_ref",
        "schema_hash",
        "source_kind",
        "status",
        "test_ref",
        "tool_class",
        "tool_use_id_hash",
        "tool_version",
        "verification_state",
        "workflow_id",
    }
)
# These fields are references to technical artifacts.  Allowing an arbitrary
# compact identifier here would turn a misleadingly named fingerprint into a
# covert channel for business data, so they must be one-way hashes.
AGENT_LEARNING_HASH_METADATA_KEYS = frozenset(
    {
        "contract_fingerprint",
        "request_fingerprint",
        "safe_ref",
        "schema_hash",
        "tool_use_id_hash",
    }
)
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
        "add_quote_request_note",
        "replace_quote_offer_drafts",
    }
)
STORE_OWNER_LEDGER_OPERATION = "store_owner_api"
STORE_OWNER_LEDGER_WORKFLOW_ID = "raw:store_owner_api"
STORE_OWNER_LEDGER_INTENT = "raw_store_owner_api"
STORE_OWNER_LEDGER_RETENTION = timedelta(days=180)
STORE_OWNER_LEDGER_CLEANUP_BATCH = 500
STORE_RELEASE_SMOKE_LEDGER_WORKFLOW_IDS = frozenset(
    {
        "raw:api:/api/change_feed/ack",
        "raw:api:/api/change_feed/bootstrap",
    }
)
STORE_OWNER_READBACK_CLASSES = frozenset(
    {
        "absence_plus_audit",
        "collection_membership",
        "exact_entity",
        "operation_specific_state",
    }
)
STORE_LEDGER_SAFE_CHECKPOINT_KEYS = {
    "baseline",
    "compact_refs",
    "contract_id",
    "contract_fingerprint",
    "counts",
    "cursor",
    "entity",
    "error_code",
    "expected_revision_sha256",
    "last_success_at",
    "mode",
    "next_action",
    "operation",
    "operation_id",
    "page_count",
    "pages_complete",
    "phase",
    "request_fingerprint",
    "request_sha256",
    "schema_hash",
    "snapshot_at",
    "state_version",
    "status",
    "target_id",
    "target_ref_sha256",
    "target_version",
    "verification",
    "verification_class",
}
STORE_LEDGER_SAFE_START_KEYS = {
    "compact_refs",
    "contract_id",
    "contract_fingerprint",
    "correlation_id",
    "counts",
    "domain",
    "dry_run_proof_expires_at",
    "dry_run_proof_ttl_seconds",
    "error_code",
    "expected_revision_sha256",
    "idempotency_key",
    "mode",
    "operation",
    "operation_id",
    "request_fingerprint",
    "request_sha256",
    "request_id",
    "schema_hash",
    "source",
    "state_version",
    "status",
    "target_id",
    "target_entity",
    "target_ref_sha256",
    "target_version",
    "updated_at",
    "verification",
    "verification_class",
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
_LEARNING_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-=]{0,255}$")
_LEARNING_TASK_SIGNATURE_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_LEARNING_TECHNICAL_HASH_RE = re.compile(r"^(?:sha256:)?[a-f0-9]{32,64}$", re.IGNORECASE)
_LEARNING_UUID_RE = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
    re.IGNORECASE,
)
_LEARNING_EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_LEARNING_PHONE_RE = re.compile(r"(?<!\d)\+?\d[\d\s().-]{7,}\d(?!\d)")
_LEARNING_VIN_TOKEN_RE = re.compile(
    r"(?<![A-HJ-NPR-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-HJ-NPR-Z0-9])",
    re.IGNORECASE,
)
_LEARNING_LICENSE_PLATE_RE = re.compile(
    r"(?<![A-ZА-Я0-9])[АВЕКМНОРСТУХABEKMHOPCTYX]\d{3}[АВЕКМНОРСТУХABEKMHOPCTYX]{2}\d{2,3}(?![A-ZА-Я0-9])",
    re.IGNORECASE,
)
_LEARNING_PERSON_NAME_RE = re.compile(
    r"(?<![A-Za-zА-Яа-яЁё])(?:[A-Z][a-z]{1,30}(?:[ -][A-Z][a-z]{1,30}){1,2}|[А-ЯЁ][а-яё]{1,30}(?:[ -][А-ЯЁ][а-яё]{1,30}){1,2})(?![A-Za-zА-Яа-яЁё])"
)
_LEARNING_MONEY_RE = re.compile(
    r"(?:\b(?:price|cost|amount|sum|rub|rur)\b|цена|стоимость|сумма|руб(?:\.|\b|лей\b|ля\b|ль\b)|₽)\s*[:=]?\s*\d{1,3}(?:[ _.,]\d{3})*(?:[.,]\d+)?"
    r"|\b\d{1,3}(?:[ _.,]\d{3})*(?:[.,]\d+)?\s*(?:₽|руб(?:\.|\b|лей\b|ля\b|ль\b)|rub\b|rur\b)",
    re.IGNORECASE,
)
_LEARNING_SECRET_VALUE_RE = re.compile(
    r"(?:sk[-_]|gh[opusr]_|github_pat_|xox[a-z]-|aiza|akia[0-9a-z]{8,}|ya29\.)",
    re.IGNORECASE,
)


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
    if (
        normalized_workflow_id == STORE_OWNER_LEDGER_WORKFLOW_ID
        and normalized_intent == STORE_OWNER_LEDGER_INTENT
        and candidates[0] == STORE_OWNER_LEDGER_OPERATION
        and str(scope_payload.get("domain") or "").strip().casefold() == "store"
        and str(scope_payload.get("source") or "").strip().casefold() == "store"
    ):
        return STORE_OWNER_LEDGER_OPERATION
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
    if operation == STORE_OWNER_LEDGER_OPERATION:
        allowed_workflow_ids = {STORE_OWNER_LEDGER_WORKFLOW_ID}
        allowed_intents = {STORE_OWNER_LEDGER_INTENT}
    else:
        allowed_workflow_ids = {"store_management", "store_management_workflow"}
        allowed_intents = {"store_management", "store_management_workflow"}
    if operation and operation != STORE_OWNER_LEDGER_OPERATION:
        allowed_workflow_ids.update({f"inventory:{operation}", f"store:{operation}", f"store_{operation}"})
        allowed_intents.update({f"inventory_{operation}", f"store_{operation}"})

    forbidden: list[str] = []
    if str(query or "").strip():
        forbidden.append("query")
    if str(workflow_id or "").strip().casefold() not in allowed_workflow_ids:
        forbidden.append("workflow_id")
    if str(intent or "").strip().casefold() not in allowed_intents:
        forbidden.append("intent")
    if operation == STORE_OWNER_LEDGER_OPERATION:
        owner_required = {
            "correlation_id": str(scope.get("correlation_id") or "").strip(),
            "expected_revision_sha256": str(scope.get("expected_revision_sha256") or "").strip(),
            "mode": str(scope.get("mode") or "").strip().casefold(),
            "request_fingerprint": str(scope.get("request_fingerprint") or "").strip(),
            "target_ref_sha256": str(scope.get("target_ref_sha256") or "").strip(),
            "verification_class": str(scope.get("verification_class") or "").strip(),
        }
        if owner_required["correlation_id"] != str(correlation_id or "").strip():
            forbidden.append("scope.correlation_id")
        if owner_required["mode"] not in {"dry_run", "apply"}:
            forbidden.append("scope.mode")
        for key in ("request_fingerprint", "target_ref_sha256"):
            if re.fullmatch(r"[0-9a-f]{64}", owner_required[key]) is None:
                forbidden.append(f"scope.{key}")
        if owner_required["verification_class"] not in STORE_OWNER_READBACK_CLASSES:
            forbidden.append("scope.verification_class")
        if owner_required["verification_class"] == "collection_membership":
            if owner_required["expected_revision_sha256"]:
                forbidden.append("scope.expected_revision_sha256")
        elif re.fullmatch(r"[0-9a-f]{64}", owner_required["expected_revision_sha256"]) is None:
            forbidden.append("scope.expected_revision_sha256")
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
    allowed = {
        f"execute {operation}",
        f"verify {operation}",
        f"completed {operation}",
        f"failed {operation}",
        f"verification failed after executor applied {operation}",
        f"ledger close reconciliation required for {operation}",
    }
    if operation == STORE_OWNER_LEDGER_OPERATION:
        allowed.update(
            {
                f"raw execute {operation}",
                f"raw verify {operation}",
                f"raw completed {operation}",
                f"raw failed {operation}",
                f"raw verification failed after executor applied {operation}",
                f"raw ledger close reconciliation required for {operation}",
            }
        )
    return normalized in allowed


def _store_summary_is_allowed(summary: str, *, operation: str) -> bool:
    normalized = str(summary or "").strip().casefold()
    if not normalized:
        return True
    allowed = {"store_management", "store_management_workflow"}
    if operation == STORE_OWNER_LEDGER_OPERATION:
        allowed.add(STORE_OWNER_LEDGER_WORKFLOW_ID)
    elif operation:
        allowed.update({f"inventory:{operation}", f"store:{operation}"})
    return normalized in allowed


def _store_owner_verification_is_refs_only(value: Any) -> bool:
    if not isinstance(value, dict) or len(value) > 24:
        return False
    allowed_keys = {
        "audit_correlation_present",
        "check",
        "collection_membership_verified",
        "collection_ref_sha256",
        "compact_ref",
        "contract_id",
        "evidence",
        "exact_readback_verified",
        "executor_ok",
        "expected_revision_sha256",
        "operation_id",
        "operation_state_ref_sha256",
        "passed",
        "readback_class",
        "readback_ref_sha256",
        "request_fingerprint",
        "request_sha256",
        "required",
        "schema_hash",
        "schema_hash_verified",
        "target_absent",
        "target_ref_sha256",
        "verification_class",
    }
    if not set(value).issubset(allowed_keys):
        return False
    if _find_forbidden_store_payload_keys(value) or _find_unsafe_store_machine_values(value):
        return False
    evidence = value.get("evidence")
    if evidence is not None:
        if not isinstance(evidence, dict) or not set(evidence).issubset(
            {
                "domain_handler_executed",
                "outcome_uncertain",
                "readback_required",
                "transport_status",
                "write_applied",
            }
        ):
            return False
        if any(isinstance(item, (dict, list)) for item in evidence.values()):
            return False
    compact_ref = value.get("compact_ref")
    if compact_ref is not None:
        if not isinstance(compact_ref, dict) or not set(compact_ref).issubset(STORE_CHECKPOINT_REF_KEYS):
            return False
        if str(compact_ref.get("entity") or "").strip().casefold() not in STORE_LEDGER_REF_ENTITIES:
            return False
        if not str(compact_ref.get("id") or "").strip():
            return False
        if not str(compact_ref.get("version") or "").strip():
            return False
        if _find_unsafe_store_machine_values(compact_ref):
            return False
    return True


def _store_owner_target_ref_sha256(target_id: str) -> str:
    return hashlib.sha256(f"target:{target_id}".encode()).hexdigest()


def _cleanup_store_owner_runs(
    conn: sqlite3.Connection,
    *,
    now: datetime,
) -> int:
    cutoff = (now - STORE_OWNER_LEDGER_RETENTION).isoformat()
    workflow_placeholders = ",".join("?" for _ in STORE_RELEASE_SMOKE_LEDGER_WORKFLOW_IDS)
    rows = conn.execute(
        """
        SELECT id, status, dry_run, checkpoint_json
        FROM manager_runs
        WHERE (
          (workflow_id = ? AND intent = ?)
          OR workflow_id IN ("""
        + workflow_placeholders
        + """
        )
        ) AND updated_at < ?
          AND status IN ('completed', 'planned', 'failed', 'cancelled')
        ORDER BY updated_at ASC, id ASC
        LIMIT ?
        """,
        (
            STORE_OWNER_LEDGER_WORKFLOW_ID,
            STORE_OWNER_LEDGER_INTENT,
            *sorted(STORE_RELEASE_SMOKE_LEDGER_WORKFLOW_IDS),
            cutoff,
            STORE_OWNER_LEDGER_CLEANUP_BATCH,
        ),
    ).fetchall()
    ids = []
    for row in rows:
        checkpoint = _decode_json(row["checkpoint_json"], {})
        post_dispatch = checkpoint.get("phase") == "transport_result"
        if row["status"] in {"completed", "planned"} or bool(row["dry_run"]) or not post_dispatch:
            ids.append(int(row["id"]))
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"DELETE FROM manager_runs WHERE id IN ({placeholders})", ids)
    return len(ids)


def _store_owner_transition_error(
    run_id: int,
    *,
    current_status: str,
    target_status: str,
    expected_state_version: int | None,
    scope: dict[str, Any],
    checkpoint: dict[str, Any],
) -> dict[str, Any] | None:
    if expected_state_version is None:
        return {
            "ok": False,
            "error": "workflow_state_version_required",
            "run_id": run_id,
            "status": current_status,
        }
    if target_status == "external_wait":
        return {
            "ok": False,
            "error": "store_owner_external_wait_not_allowed",
            "run_id": run_id,
            "status": current_status,
        }
    if (
        str(scope.get("mode") or "").strip().casefold() == "apply"
        and target_status in {"failed", "cancelled"}
        and (current_status == "compensating" or checkpoint.get("phase") == "transport_result")
    ):
        return {
            "ok": False,
            "error": "store_owner_reconciliation_required_before_terminal_transition",
            "run_id": run_id,
            "status": current_status,
        }
    return None


def _store_owner_readback_ref_sha256(
    *,
    target_ref_sha256: str,
    compact_ref: dict[str, Any],
) -> str:
    payload = {
        "entity": str(compact_ref.get("entity") or "").strip().casefold(),
        "id": str(compact_ref.get("id") or "").strip(),
        "target_ref_sha256": target_ref_sha256,
        "version": str(compact_ref.get("version") or "").strip(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"store-owner-readback-ref-v1\0" + encoded).hexdigest()


def _store_owner_completion_error(
    run_id: int,
    *,
    current_status: str,
    dry_run: bool,
    scope: dict[str, Any],
    checkpoint: dict[str, Any],
    verification: dict[str, Any],
) -> dict[str, Any] | None:
    mode = str(scope.get("mode") or "").strip().casefold()
    if not _store_owner_verification_is_refs_only(verification):
        return {
            "ok": False,
            "error": "store_owner_exact_readback_required_before_completion",
            "run_id": run_id,
            "status": current_status,
        }
    required_checkpoint = {
        "contract_id": str(checkpoint.get("contract_id") or "").strip(),
        "expected_revision_sha256": str(checkpoint.get("expected_revision_sha256") or "").strip(),
        "operation_id": str(checkpoint.get("operation_id") or "").strip(),
        "request_fingerprint": str(checkpoint.get("request_fingerprint") or "").strip(),
        "request_sha256": str(checkpoint.get("request_sha256") or "").strip(),
        "schema_hash": str(checkpoint.get("schema_hash") or "").strip(),
        "target_ref_sha256": str(checkpoint.get("target_ref_sha256") or "").strip(),
        "verification_class": str(checkpoint.get("verification_class") or "").strip(),
    }
    scope_request_fingerprint = str(scope.get("request_fingerprint") or "").strip()
    scope_target_hash = str(scope.get("target_ref_sha256") or "").strip()
    checkpoint_valid = (
        checkpoint.get("phase") == "transport_result"
        and re.fullmatch(r"ac_[0-9a-f]{20}", required_checkpoint["contract_id"]) is not None
        and bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,199}", required_checkpoint["operation_id"]))
        and all(
            re.fullmatch(r"[0-9a-f]{64}", required_checkpoint[key]) is not None
            for key in ("request_fingerprint", "request_sha256", "schema_hash", "target_ref_sha256")
        )
        and required_checkpoint["request_fingerprint"] == scope_request_fingerprint
        and required_checkpoint["target_ref_sha256"] == scope_target_hash
        and required_checkpoint["expected_revision_sha256"] == str(scope.get("expected_revision_sha256") or "").strip()
        and required_checkpoint["verification_class"] == str(scope.get("verification_class") or "").strip()
        and (
            (
                required_checkpoint["verification_class"] == "collection_membership"
                and not required_checkpoint["expected_revision_sha256"]
            )
            or re.fullmatch(r"[0-9a-f]{64}", required_checkpoint["expected_revision_sha256"]) is not None
        )
    )
    binding_matches = all(
        str(verification.get(key) or "").strip() == required_checkpoint[key]
        for key in (
            "contract_id",
            "expected_revision_sha256",
            "operation_id",
            "request_fingerprint",
            "request_sha256",
            "schema_hash",
            "target_ref_sha256",
            "verification_class",
        )
    )
    if not checkpoint_valid or not binding_matches:
        return {
            "ok": False,
            "error": "store_owner_exact_readback_required_before_completion",
            "run_id": run_id,
            "status": current_status,
        }
    if mode in {"dry_run", "revision"} or dry_run:
        if (
            verification.get("executor_ok") is True
            and verification.get("passed") is True
            and str(verification.get("check") or "")
            in {"store_owner_server_dry_run_receipt", "store_owner_read_response_contract"}
        ):
            return None
        return {
            "ok": False,
            "error": "store_owner_exact_readback_required_before_completion",
            "run_id": run_id,
            "status": current_status,
        }
    compact_ref = verification.get("compact_ref")
    readback_ref_matches = isinstance(compact_ref, dict) and str(
        verification.get("readback_ref_sha256") or ""
    ).strip() == _store_owner_readback_ref_sha256(
        target_ref_sha256=required_checkpoint["target_ref_sha256"],
        compact_ref=compact_ref,
    )
    close_valid = (
        mode == "apply"
        and current_status == "compensating"
        and verification.get("executor_ok") is True
        and verification.get("exact_readback_verified") is True
        and required_checkpoint["verification_class"] in STORE_OWNER_READBACK_CLASSES
        and str(verification.get("readback_class") or "").strip() == required_checkpoint["verification_class"]
        and isinstance(compact_ref, dict)
        and bool(str(compact_ref.get("id") or "").strip())
        and bool(str(compact_ref.get("version") or "").strip())
        and readback_ref_matches
    )
    readback_class = required_checkpoint["verification_class"]
    compact_id = str(compact_ref.get("id") or "").strip() if isinstance(compact_ref, dict) else ""
    if readback_class in {"exact_entity", "absence_plus_audit"}:
        close_valid = close_valid and (
            _store_owner_target_ref_sha256(compact_id) == required_checkpoint["target_ref_sha256"]
        )
    if readback_class == "collection_membership":
        close_valid = (
            close_valid
            and verification.get("collection_membership_verified") is True
            and str(verification.get("collection_ref_sha256") or "").strip() == required_checkpoint["target_ref_sha256"]
        )
    if readback_class == "operation_specific_state":
        close_valid = (
            close_valid
            and str(verification.get("operation_state_ref_sha256") or "").strip()
            == required_checkpoint["target_ref_sha256"]
        )
    if readback_class == "absence_plus_audit":
        close_valid = (
            close_valid
            and verification.get("target_absent") is True
            and verification.get("audit_correlation_present") is True
        )
    if close_valid:
        return None
    return {
        "ok": False,
        "error": "store_owner_exact_readback_required_before_completion",
        "run_id": run_id,
        "status": current_status,
    }


def _store_owner_checkpoint_forbidden(
    *,
    current_status: str,
    scope: dict[str, Any],
    existing: dict[str, Any],
    checkpoint: dict[str, Any],
) -> list[str]:
    forbidden: list[str] = []
    required_hashes = (
        "expected_revision_sha256",
        "request_fingerprint",
        "request_sha256",
        "schema_hash",
        "target_ref_sha256",
    )
    if checkpoint.get("phase") != "transport_result":
        forbidden.append("phase")
    if current_status != "executing":
        forbidden.append("status")
    if existing.get("phase") == "transport_result" and existing != checkpoint:
        forbidden.append("checkpoint_immutable")
    if re.fullmatch(r"ac_[0-9a-f]{20}", str(checkpoint.get("contract_id") or "")) is None:
        forbidden.append("contract_id")
    if not str(checkpoint.get("operation_id") or "").strip():
        forbidden.append("operation_id")
    verification_class = str(checkpoint.get("verification_class") or "")
    for key in required_hashes:
        if key == "expected_revision_sha256" and verification_class == "collection_membership":
            if checkpoint.get(key) not in {None, ""}:
                forbidden.append(key)
            continue
        if re.fullmatch(r"[0-9a-f]{64}", str(checkpoint.get(key) or "")) is None:
            forbidden.append(key)
    if checkpoint.get("request_fingerprint") != scope.get("request_fingerprint"):
        forbidden.append("request_fingerprint")
    if checkpoint.get("target_ref_sha256") != scope.get("target_ref_sha256"):
        forbidden.append("target_ref_sha256")
    if checkpoint.get("expected_revision_sha256") != scope.get("expected_revision_sha256"):
        forbidden.append("expected_revision_sha256")
    if verification_class not in STORE_OWNER_READBACK_CLASSES:
        forbidden.append("verification_class")
    if verification_class != scope.get("verification_class"):
        forbidden.append("verification_class")
    return forbidden


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


def _normalize_agent_execution_mode(value: str | None) -> str | None:
    normalized = str(value or "").strip().casefold()
    return normalized if normalized in AGENT_EXECUTION_MODES else None


def _normalize_learning_identifier(value: Any, *, field: str, allow_empty: bool) -> str | None:
    text = str(value or "").strip()
    if not text:
        return "" if allow_empty else None
    if not _LEARNING_IDENTIFIER_RE.fullmatch(text):
        return None
    if _looks_like_sensitive_learning_value(text):
        return None
    return text


def _task_signature_hash(value: Any) -> str:
    text = str(value or "").strip()
    if _LEARNING_TASK_SIGNATURE_RE.fullmatch(text):
        return text
    # Do not persist task text: the hash is intentionally one-way and scoped
    # only to route-quality statistics, not owner/business memory.
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _looks_like_sensitive_learning_value(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    # Opaque UUIDs and hashes are the technical references used by the
    # learning ledger. Their digit/hyphen shape must not be mistaken for a
    # phone number by the conservative detector below.
    if _LEARNING_UUID_RE.fullmatch(text) or _LEARNING_TECHNICAL_HASH_RE.fullmatch(text):
        return False
    return bool(
        _STORE_VIN_VALUE_RE.fullmatch(text)
        or _LEARNING_VIN_TOKEN_RE.search(text)
        or _LEARNING_LICENSE_PLATE_RE.search(text)
        or _LEARNING_PERSON_NAME_RE.search(text)
        or _LEARNING_EMAIL_RE.search(text)
        or _LEARNING_PHONE_RE.search(text)
        or _LEARNING_MONEY_RE.search(text)
        or _STORE_SECRET_VALUE_RE.search(text)
        or _STORE_JWT_VALUE_RE.fullmatch(text)
        or _LEARNING_SECRET_VALUE_RE.search(text)
    )


def _sanitize_agent_learning_metadata(value: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """Keep only compact technical status evidence for the learning ledger."""

    if value is None:
        return {}, []
    if not isinstance(value, dict):
        return {}, ["metadata"]
    sanitized: dict[str, Any] = {}
    forbidden: list[str] = []
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip().casefold()
        if not key:
            continue
        if key not in AGENT_LEARNING_METADATA_KEYS:
            forbidden.append(key)
            continue
        normalized = _sanitize_agent_learning_metadata_value(key, raw_value)
        if normalized is None:
            forbidden.append(key)
            continue
        sanitized[key] = normalized
    return sanitized, list(dict.fromkeys(forbidden))


def _sanitize_agent_learning_metadata_value(key: str, value: Any) -> Any | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value if -1_000_000_000 <= value <= 1_000_000_000 else None
    if isinstance(value, float):
        return value if math.isfinite(value) and -1_000_000_000 <= value <= 1_000_000_000 else None
    if isinstance(value, str):
        normalized = _normalize_learning_identifier(value, field=key, allow_empty=False)
        if not normalized:
            return None
        if key in AGENT_LEARNING_HASH_METADATA_KEYS and not _LEARNING_TECHNICAL_HASH_RE.fullmatch(normalized):
            return None
        return normalized
    if key in {"checks", "assertions"} and isinstance(value, list):
        result: list[str] = []
        for item in value[:30]:
            normalized = _normalize_learning_identifier(item, field=key, allow_empty=False)
            if not normalized:
                return None
            result.append(normalized)
        return list(dict.fromkeys(result))
    if key == "counts" and isinstance(value, dict):
        result_counts: dict[str, int | float] = {}
        for raw_count_key, raw_count in list(value.items())[:30]:
            count_key = _normalize_learning_identifier(raw_count_key, field="counts", allow_empty=False)
            if not count_key or not isinstance(raw_count, (int, float)) or isinstance(raw_count, bool):
                return None
            if not math.isfinite(float(raw_count)) or abs(float(raw_count)) > 1_000_000_000:
                return None
            result_counts[count_key] = raw_count
        return result_counts
    return None


def _sanitize_learning_checks(values: list[str] | None) -> tuple[list[str], list[str]]:
    if values is None:
        return [], []
    if not isinstance(values, list):
        return [], ["completion_checks"]
    result: list[str] = []
    invalid: list[str] = []
    for raw_value in values[:50]:
        normalized = _normalize_learning_identifier(raw_value, field="completion_checks", allow_empty=False)
        if not normalized:
            invalid.append("completion_checks")
            continue
        result.append(normalized)
    return list(dict.fromkeys(result)), invalid


def _sanitize_agent_tool_assessment(value: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]], list[str]]:
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], ["tool_assessment"]
    allowed_keys = {
        "tool_name",
        "status",
        "calls",
        "successes",
        "failures",
        "fallback_used",
        "latency_ms",
        "error_code",
    }
    result: list[dict[str, Any]] = []
    invalid: list[str] = []
    for entry in value[:50]:
        if not isinstance(entry, dict):
            invalid.append("tool_assessment")
            continue
        unknown = [str(key) for key in entry if str(key) not in allowed_keys]
        if unknown:
            invalid.extend(f"tool_assessment.{key}" for key in unknown)
            continue
        tool_name = _normalize_learning_identifier(entry.get("tool_name"), field="tool_name", allow_empty=False)
        status = str(entry.get("status") or "").strip().casefold()
        if not tool_name or status not in AGENT_TOOL_EVENT_STATUSES:
            invalid.append("tool_assessment")
            continue
        item: dict[str, Any] = {"tool_name": tool_name, "status": status}
        valid = True
        for key in ("calls", "successes", "failures", "latency_ms"):
            if key not in entry:
                continue
            raw_value = entry[key]
            if not isinstance(raw_value, int) or isinstance(raw_value, bool) or not 0 <= raw_value <= 1_000_000_000:
                valid = False
                break
            item[key] = raw_value
        if "fallback_used" in entry:
            if not isinstance(entry["fallback_used"], bool):
                valid = False
            else:
                item["fallback_used"] = entry["fallback_used"]
        if "error_code" in entry:
            error_code = _normalize_learning_identifier(entry["error_code"], field="error_code", allow_empty=True)
            if error_code is None:
                valid = False
            elif error_code:
                item["error_code"] = error_code
        if not valid:
            invalid.append("tool_assessment")
            continue
        result.append(item)
    return result, invalid


def _normalize_learning_duration(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if 0 <= normalized <= 86_400_000 else None


def _normalize_learning_improvement_kind(value: str | None) -> str | None:
    text = str(value or "").strip().casefold()
    if not text:
        return ""
    return text if text in AGENT_IMPROVEMENT_KINDS else None


def _validate_agent_learning_lesson(value: str, *, allow_empty: bool = False) -> str | None:
    text = str(value or "").strip()
    if not text:
        return "" if allow_empty else None
    if len(text) > 768 or _looks_like_sensitive_learning_value(text):
        return None
    # A lesson is generic operating guidance, never a copied body or an entity
    # record. Detect direct secret/body-like labels even when no value pattern
    # happens to match.
    normalized_tokens = {token.casefold() for token in re.findall(r"[A-Za-zА-Яа-яЁё]+", text)}
    if {"vin", "вин", "телефон", "почта", "email", "паспорт"} & normalized_tokens:
        return None
    return text


def _validate_durable_memory_text(value: Any, *, allow_empty: bool = False) -> str | None:
    """Keep user-facing memory free of direct identifiers and credentials.

    Unlike the learning ledger, normal memory may contain ordinary natural
    language and durable routing guidance.  It must nevertheless never become
    a store for copied CRM, email, financial, or credential values.
    """

    text = str(value or "").strip()
    if not text:
        return "" if allow_empty else None
    if len(text) > 4096 or _looks_like_sensitive_learning_value(text):
        return None
    return text


def _validate_durable_memory_tags(values: list[str] | None) -> list[str] | None:
    if values is None:
        return []
    if not isinstance(values, list):
        return None
    result: list[str] = []
    for value in values[:100]:
        normalized = _validate_durable_memory_text(value)
        if normalized is None:
            return None
        result.append(normalized)
    return list(dict.fromkeys(result))


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
        queries.append("knowledge-map knowledge-sync knowledge-audit")
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

                CREATE TABLE IF NOT EXISTS journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'codex',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    archived_at TEXT
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

                CREATE INDEX IF NOT EXISTS idx_knowledge_documents_domain
                    ON knowledge_documents(domain);

                CREATE INDEX IF NOT EXISTS idx_knowledge_sections_domain
                    ON knowledge_sections(domain);

                CREATE INDEX IF NOT EXISTS idx_knowledge_sections_search
                    ON knowledge_sections(search_text);

                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_sections_fts
                USING fts5(domain, path, heading, search_text);

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

                -- Learning telemetry deliberately contains only technical hashes,
                -- status codes, and compact references. It never stores prompts,
                -- tool arguments/results, CRM/Store rows, or Gmail content.
                CREATE TABLE IF NOT EXISTS agent_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    state_version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_turns (
                    id TEXT PRIMARY KEY,
                    external_turn_id TEXT NOT NULL DEFAULT '',
                    task_signature TEXT NOT NULL,
                    workflow_id TEXT NOT NULL DEFAULT '',
                    mode_override TEXT NOT NULL DEFAULT '',
                    effective_mode TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'codex',
                    status TEXT NOT NULL DEFAULT 'active',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    started_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_tool_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    turn_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration_ms INTEGER,
                    error_code TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(turn_id) REFERENCES agent_turns(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS agent_experience_reviews (
                    id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL UNIQUE,
                    outcome TEXT NOT NULL,
                    completion_checks_json TEXT NOT NULL DEFAULT '[]',
                    tool_assessment_json TEXT NOT NULL DEFAULT '[]',
                    failure_class TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'completed',
                    created_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    FOREIGN KEY(turn_id) REFERENCES agent_turns(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS agent_improvements (
                    id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL,
                    review_id TEXT,
                    kind TEXT NOT NULL,
                    risk TEXT NOT NULL DEFAULT 'low',
                    status TEXT NOT NULL DEFAULT 'pending',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    resolution_json TEXT NOT NULL DEFAULT '{}',
                    promoted_lesson_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(turn_id) REFERENCES agent_turns(id) ON DELETE CASCADE,
                    FOREIGN KEY(review_id) REFERENCES agent_experience_reviews(id) ON DELETE SET NULL,
                    FOREIGN KEY(promoted_lesson_id) REFERENCES lessons(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_manager_runs_status
                    ON manager_runs(status, started_at);

                CREATE INDEX IF NOT EXISTS idx_manager_run_events_run_id
                    ON manager_run_events(run_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_manager_run_external_steps_run_id
                    ON manager_run_external_steps(run_id, status, created_at);

                CREATE INDEX IF NOT EXISTS idx_store_checkpoints_status
                    ON store_checkpoints(last_attempt_status, updated_at);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_turns_external_turn_id
                    ON agent_turns(external_turn_id) WHERE external_turn_id <> '';

                CREATE INDEX IF NOT EXISTS idx_agent_turns_mode_status
                    ON agent_turns(effective_mode, status, updated_at);

                CREATE INDEX IF NOT EXISTS idx_agent_tool_events_turn_id
                    ON agent_tool_events(turn_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_agent_improvements_status
                    ON agent_improvements(status, risk, updated_at);

                """
            )
            self._ensure_columns(conn)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_manager_runs_idempotency "
                "ON manager_runs(idempotency_key) WHERE idempotency_key <> ''"
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO agent_settings (key, value, state_version, updated_at)
                VALUES ('global_execution_mode', 'work', 1, ?)
                """,
                (_now(),),
            )

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
        normalized_content = _validate_durable_memory_text(content)
        normalized_title = _validate_durable_memory_text(title, allow_empty=True)
        normalized_category = _validate_durable_memory_text(category)
        normalized_source = _validate_durable_memory_text(source)
        normalized_tags = _validate_durable_memory_tags(tags)
        if (
            None in {normalized_content, normalized_title, normalized_category, normalized_source}
            or normalized_tags is None
        ):
            return {"ok": False, "error": "unsafe_durable_memory_value"}
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
                        normalized_content,
                        normalized_category,
                        normalized_source,
                        float(confidence),
                        float(importance),
                        expires_at,
                        supersedes_id,
                        sensitivity,
                        _json_list(normalized_tags),
                        now,
                        now,
                    ),
                )
                row_id = _required_lastrowid(cursor)
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO notes
                        (title, content, category, source, importance, expires_at, supersedes_id, sensitivity, tags_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_title,
                        normalized_content,
                        normalized_category,
                        normalized_source,
                        float(importance),
                        expires_at,
                        supersedes_id,
                        sensitivity,
                        _json_list(normalized_tags),
                        now,
                        now,
                    ),
                )
                row_id = _required_lastrowid(cursor)
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
        normalized_content = _validate_durable_memory_text(content)
        normalized_title = _validate_durable_memory_text(title, allow_empty=True)
        normalized_applies_to = _validate_durable_memory_text(applies_to)
        normalized_signal = _validate_durable_memory_text(signal)
        normalized_recommendation = _validate_durable_memory_text(recommendation, allow_empty=True)
        normalized_avoid = _validate_durable_memory_text(avoid, allow_empty=True)
        normalized_source = _validate_durable_memory_text(source)
        normalized_tags = _validate_durable_memory_tags(tags)
        if (
            None
            in {
                normalized_content,
                normalized_title,
                normalized_applies_to,
                normalized_signal,
                normalized_recommendation,
                normalized_avoid,
                normalized_source,
            }
            or normalized_tags is None
        ):
            return {"ok": False, "error": "unsafe_durable_memory_value"}
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
                    normalized_title,
                    normalized_content,
                    normalized_applies_to,
                    normalized_signal,
                    normalized_recommendation,
                    normalized_avoid,
                    importance,
                    confidence,
                    normalized_source,
                    _json_list(normalized_tags),
                    now,
                    now,
                ),
            )
        return {
            "ok": True,
            "kind": "lesson",
            "id": cursor.lastrowid,
            "created_at": now,
            "applies_to": normalized_applies_to,
            "signal": normalized_signal,
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

    def journal(self, event: str, *, source: str = "codex", tags: list[str] | None = None) -> dict[str, Any]:
        self.initialize()
        normalized_event = _validate_durable_memory_text(event)
        normalized_source = _validate_durable_memory_text(source)
        normalized_tags = _validate_durable_memory_tags(tags)
        if (
            normalized_event is None
            or len(normalized_event) > GENERIC_JOURNAL_MAX_EVENT_LENGTH
            or normalized_source is None
            or normalized_tags is None
        ):
            return {"ok": False, "error": "unsafe_durable_memory_value"}
        now = _now()
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO journal (event, source, tags_json, created_at) VALUES (?, ?, ?, ?)",
                (normalized_event, normalized_source, _json_list(normalized_tags), now),
            )
            conn.execute(
                """
                DELETE FROM journal
                WHERE id IN (
                    SELECT id FROM journal
                    ORDER BY created_at DESC, id DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (GENERIC_JOURNAL_MAX_ENTRIES,),
            )
        return {"ok": True, "id": cursor.lastrowid, "created_at": now}

    def recall(  # noqa: C901
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
        if not kind or kind == "rule":
            for item in load_manager_rules():
                if not _matches_filter(item.get("scope"), category):
                    continue
                score, matched_fields = self._score_memory_item(item, query, query_tokens)
                if query_tokens and score <= 0:
                    continue
                item["score"] = score + max(0, 30 - int(item.get("priority") or 100)) // 2
                item["matched_fields"] = matched_fields
                results.append(item)
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
                ("journal", "journal", "created_at"),
                ("lesson", "lessons", "updated_at"),
            ]
            for row_kind, table, order_column in searches:
                if kind and row_kind != kind:
                    continue
                row_limit = max(limit * 10, 100)
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
        rules = load_manager_rules()
        sections = {
            "notes": self._section_summary("notes", "updated_at"),
            "facts": self._section_summary("facts", "updated_at"),
            "lessons": self._section_summary("lessons", "updated_at", where="archived_at IS NULL"),
            "tasks": self._section_summary("tasks", "updated_at", where="status = 'open'"),
            "journal": self._section_summary("journal", "created_at", where="archived_at IS NULL"),
            "rules": {"count": len(rules), "last_updated": None},
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
                ("journal", "journal"),
                ("lessons", "lesson"),
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
            rows.extend(load_manager_rules())
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
            "journal": self._count_rows("journal", where="archived_at IS NULL"),
            "rules": len(load_manager_rules()),
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

    # Agent learning is intentionally a separate technical ledger.  It is not
    # a second CRM, Store, or Gmail cache: turns use a one-way task hash and
    # events/reviews accept only allowlisted status metadata.
    def get_agent_mode(self) -> dict[str, Any]:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value, state_version, updated_at FROM agent_settings WHERE key = 'global_execution_mode'"
            ).fetchone()
        mode = _normalize_agent_execution_mode(row["value"] if row else "work") or "work"
        return {
            "ok": True,
            "schema": "AgentModeV1",
            "global_mode": mode,
            "supported_modes": sorted(AGENT_EXECUTION_MODES),
            "state_version": int(row["state_version"] or 1) if row else 1,
            "updated_at": row["updated_at"] if row else None,
        }

    def set_agent_mode(
        self,
        mode: str,
        *,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        normalized_mode = _normalize_agent_execution_mode(mode)
        if not normalized_mode:
            return {
                "ok": False,
                "error": "invalid_agent_execution_mode",
                "supported_modes": sorted(AGENT_EXECUTION_MODES),
            }
        self.initialize()
        now = _now()
        with self.connect() as conn:
            # The read and compare-and-swap must share a write reservation.
            # Otherwise two callers can both read the old version and one of
            # them may report success after its conditional UPDATE matched no
            # row.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT value, state_version FROM agent_settings WHERE key = 'global_execution_mode'"
            ).fetchone()
            current_mode = _normalize_agent_execution_mode(row["value"] if row else "work") or "work"
            current_version = int(row["state_version"] or 1) if row else 1
            if expected_state_version is not None and int(expected_state_version) != current_version:
                return {
                    "ok": False,
                    "error": "agent_mode_state_conflict",
                    "expected_state_version": int(expected_state_version),
                    "current_state_version": current_version,
                }
            if current_mode == normalized_mode:
                return {
                    "ok": True,
                    "schema": "AgentModeV1",
                    "global_mode": current_mode,
                    "state_version": current_version,
                    "updated_at": None,
                    "deduplicated": True,
                }
            next_version = current_version + 1
            cursor = conn.execute(
                """
                UPDATE agent_settings
                SET value = ?, state_version = ?, updated_at = ?
                WHERE key = 'global_execution_mode' AND state_version = ?
                """,
                (normalized_mode, next_version, now, current_version),
            )
            if cursor.rowcount != 1:
                latest = conn.execute(
                    "SELECT state_version FROM agent_settings WHERE key = 'global_execution_mode'"
                ).fetchone()
                return {
                    "ok": False,
                    "error": "agent_mode_state_conflict",
                    "expected_state_version": current_version,
                    "current_state_version": int(latest["state_version"] or 1) if latest else None,
                }
        return {
            "ok": True,
            "schema": "AgentModeV1",
            "global_mode": normalized_mode,
            "state_version": next_version,
            "updated_at": now,
            "deduplicated": False,
        }

    def resolve_agent_mode(self, mode_override: str | None = None) -> dict[str, Any]:
        override = _normalize_agent_execution_mode(mode_override)
        if mode_override not in (None, "") and not override:
            return {
                "ok": False,
                "error": "invalid_agent_execution_mode",
                "supported_modes": sorted(AGENT_EXECUTION_MODES),
            }
        current = self.get_agent_mode()
        if not current.get("ok"):
            return current
        return {
            "ok": True,
            "schema": "AgentModeResolutionV1",
            "global_mode": current["global_mode"],
            "mode_override": override,
            "effective_mode": override or current["global_mode"],
            "state_version": current["state_version"],
        }

    def start_agent_turn(
        self,
        task_signature: str = "",
        *,
        mode_override: str | None = None,
        workflow_id: str = "",
        source: str = "codex",
        external_turn_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create an idempotent technical learning turn without persisting task text."""

        resolved = self.resolve_agent_mode(mode_override)
        if not resolved.get("ok"):
            return resolved
        # Work mode deliberately has no learning ledger.  Returning a compact
        # skipped result prevents mode transitions from leaving reusable work
        # turns that could later be mistaken for a learning turn.
        if resolved["effective_mode"] != "learning":
            return {
                "ok": True,
                "turn_id": None,
                "external_turn_id": None,
                "task_signature": _task_signature_hash(task_signature),
                "effective_mode": "work",
                "mode_override": resolved["mode_override"],
                "status": "skipped",
                "learning_enabled": False,
                "deduplicated": True,
                "skipped": True,
            }
        normalized_workflow = _normalize_learning_identifier(workflow_id, field="workflow_id", allow_empty=True)
        normalized_external = _normalize_learning_identifier(
            external_turn_id,
            field="external_turn_id",
            allow_empty=True,
        )
        normalized_source = _normalize_learning_identifier(source, field="source", allow_empty=False)
        if normalized_workflow is None or normalized_external is None or not normalized_source:
            return {"ok": False, "error": "unsafe_agent_learning_identifier"}
        sanitized_metadata, forbidden = _sanitize_agent_learning_metadata(metadata)
        if forbidden:
            return {
                "ok": False,
                "error": "raw_agent_learning_payload_not_allowed",
                "forbidden_keys": forbidden,
            }
        signature = _task_signature_hash(task_signature)
        now = _now()
        turn_id = str(uuid.uuid4())
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if normalized_external:
                existing = conn.execute(
                    "SELECT * FROM agent_turns WHERE external_turn_id = ? LIMIT 1",
                    (normalized_external,),
                ).fetchone()
                if existing:
                    item = self._row_to_dict(existing)
                    if item["effective_mode"] == resolved["effective_mode"]:
                        return {
                            "ok": True,
                            "turn_id": item["id"],
                            "external_turn_id": item["external_turn_id"],
                            "task_signature": item["task_signature"],
                            "effective_mode": item["effective_mode"],
                            "mode_override": item["mode_override"] or None,
                            "status": item["status"],
                            "deduplicated": True,
                        }
                    # A legacy work-mode turn may have the same external Codex
                    # turn id.  Retire that technical row before creating the
                    # learning turn; never reuse a row across execution modes.
                    conn.execute(
                        """
                        UPDATE agent_turns
                        SET external_turn_id = '', status = 'deferred', updated_at = ?
                        WHERE id = ?
                        """,
                        (now, item["id"]),
                    )
            else:
                existing = conn.execute(
                    """
                    SELECT * FROM agent_turns
                    WHERE task_signature = ? AND effective_mode = ? AND status = 'active'
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (signature, resolved["effective_mode"]),
                ).fetchone()
                if existing:
                    item = self._row_to_dict(existing)
                    return {
                        "ok": True,
                        "turn_id": item["id"],
                        "external_turn_id": item["external_turn_id"] or None,
                        "task_signature": item["task_signature"],
                        "effective_mode": item["effective_mode"],
                        "mode_override": item["mode_override"] or None,
                        "status": item["status"],
                        "deduplicated": True,
                    }
            conn.execute(
                """
                INSERT INTO agent_turns (
                    id, external_turn_id, task_signature, workflow_id, mode_override,
                    effective_mode, source, status, metadata_json, started_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    turn_id,
                    normalized_external or "",
                    signature,
                    normalized_workflow or "",
                    resolved["mode_override"] or "",
                    resolved["effective_mode"],
                    normalized_source,
                    json.dumps(sanitized_metadata, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return {
            "ok": True,
            "turn_id": turn_id,
            "external_turn_id": normalized_external or None,
            "task_signature": signature,
            "effective_mode": resolved["effective_mode"],
            "mode_override": resolved["mode_override"],
            "status": "active",
            "started_at": now,
            "deduplicated": False,
        }

    def record_agent_tool_event(
        self,
        turn_id: str,
        *,
        tool_name: str,
        status: str,
        duration_ms: int | None = None,
        error_code: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record one refs-only tool event for a learning turn."""

        normalized_tool = _normalize_learning_identifier(tool_name, field="tool_name", allow_empty=False)
        normalized_status = str(status or "").strip().casefold()
        normalized_error = _normalize_learning_identifier(error_code, field="error_code", allow_empty=True)
        if not normalized_tool or normalized_status not in AGENT_TOOL_EVENT_STATUSES or normalized_error is None:
            return {"ok": False, "error": "invalid_agent_tool_event"}
        sanitized_metadata, forbidden = _sanitize_agent_learning_metadata(metadata)
        if forbidden:
            return {
                "ok": False,
                "error": "raw_agent_learning_payload_not_allowed",
                "forbidden_keys": forbidden,
            }
        normalized_duration = _normalize_learning_duration(duration_ms)
        if duration_ms is not None and normalized_duration is None:
            return {"ok": False, "error": "invalid_agent_tool_duration"}
        self.initialize()
        now = _now()
        with self.connect() as conn:
            turn = self._find_agent_turn_row(conn, turn_id)
            if not turn:
                return {"ok": False, "error": "agent_turn_not_found", "turn_id": turn_id}
            if str(turn["effective_mode"] or "") != "learning":
                return {"ok": False, "error": "agent_tool_events_require_learning_mode", "turn_id": turn["id"]}
            if normalized_duration is None and normalized_status in {"succeeded", "failed", "skipped"}:
                normalized_duration = self._derived_tool_event_duration(
                    conn,
                    turn_id=str(turn["id"]),
                    tool_name=normalized_tool,
                    tool_use_id_hash=str(sanitized_metadata.get("tool_use_id_hash") or ""),
                    now=now,
                )
            cursor = conn.execute(
                """
                INSERT INTO agent_tool_events
                    (turn_id, tool_name, status, duration_ms, error_code, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(turn["id"]),
                    normalized_tool,
                    normalized_status,
                    normalized_duration,
                    normalized_error or "",
                    json.dumps(sanitized_metadata, ensure_ascii=False),
                    now,
                ),
            )
            conn.execute("UPDATE agent_turns SET updated_at = ? WHERE id = ?", (now, turn["id"]))
        return {
            "ok": True,
            "id": _required_lastrowid(cursor),
            "turn_id": str(turn["id"]),
            "tool_name": normalized_tool,
            "status": normalized_status,
            "duration_ms": normalized_duration,
            "created_at": now,
        }

    def get_active_agent_turn(
        self,
        task_signature: str = "",
        *,
        external_turn_id: str = "",
        effective_mode: str | None = None,
    ) -> dict[str, Any]:
        """Return a compact active learning turn without exposing task text."""

        normalized_external = _normalize_learning_identifier(
            external_turn_id,
            field="external_turn_id",
            allow_empty=True,
        )
        if normalized_external is None:
            return {"ok": False, "error": "unsafe_agent_learning_identifier"}
        normalized_mode = _normalize_agent_execution_mode(effective_mode)
        if effective_mode not in (None, "") and not normalized_mode:
            return {"ok": False, "error": "invalid_agent_execution_mode"}
        signature = _task_signature_hash(task_signature)
        self.initialize()
        with self.connect() as conn:
            if normalized_external:
                query = """
                    SELECT id, external_turn_id, task_signature, workflow_id, mode_override,
                           effective_mode, status, started_at, updated_at
                    FROM agent_turns
                    WHERE external_turn_id = ? AND status = 'active'
                """
                params: list[Any] = [normalized_external]
                if normalized_mode:
                    query += " AND effective_mode = ?"
                    params.append(normalized_mode)
                row = conn.execute(f"{query} LIMIT 1", params).fetchone()
            else:
                query = """
                    SELECT id, external_turn_id, task_signature, workflow_id, mode_override,
                           effective_mode, status, started_at, updated_at
                    FROM agent_turns
                    WHERE task_signature = ? AND status = 'active'
                """
                params = [signature]
                if normalized_mode:
                    query += " AND effective_mode = ?"
                    params.append(normalized_mode)
                row = conn.execute(f"{query} ORDER BY updated_at DESC LIMIT 1", params).fetchone()
        if not row:
            return {"ok": True, "active_turn": None}
        item = dict(row)
        item["turn_id"] = item.pop("id")
        item["mode_override"] = item["mode_override"] or None
        item["external_turn_id"] = item["external_turn_id"] or None
        return {"ok": True, "active_turn": item}

    def post_run_review(
        self,
        turn_id: str,
        *,
        outcome: str = "confirmed",
        completion_checks: list[str] | None = None,
        tool_assessment: list[dict[str, Any]] | None = None,
        failure_class: str = "",
        improvement_kind: str = "",
        risk: str = "low",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Close a learning turn with a compact objective experience review."""

        normalized_outcome = str(outcome or "").strip().casefold()
        normalized_failure = _normalize_learning_identifier(failure_class, field="failure_class", allow_empty=True)
        normalized_kind = _normalize_learning_improvement_kind(improvement_kind)
        normalized_risk = str(risk or "low").strip().casefold()
        checks, invalid_checks = _sanitize_learning_checks(completion_checks)
        assessment, invalid_assessment = _sanitize_agent_tool_assessment(tool_assessment)
        sanitized_metadata, forbidden = _sanitize_agent_learning_metadata(metadata)
        if (
            normalized_outcome not in AGENT_REVIEW_OUTCOMES
            or normalized_failure is None
            or normalized_kind is None
            or normalized_risk not in AGENT_IMPROVEMENT_RISKS
            or invalid_checks
            or invalid_assessment
            or forbidden
        ):
            return {
                "ok": False,
                "error": "invalid_agent_experience_review",
                "forbidden_keys": list(dict.fromkeys(forbidden + invalid_checks + invalid_assessment)),
            }
        self.initialize()
        now = _now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            turn = self._find_agent_turn_row(conn, turn_id)
            if not turn:
                return {"ok": False, "error": "agent_turn_not_found", "turn_id": turn_id}
            if str(turn["effective_mode"] or "") != "learning":
                return {"ok": False, "error": "experience_review_requires_learning_mode", "turn_id": turn["id"]}
            existing = conn.execute(
                "SELECT * FROM agent_experience_reviews WHERE turn_id = ? LIMIT 1",
                (turn["id"],),
            ).fetchone()
            if existing:
                candidate = conn.execute(
                    "SELECT * FROM agent_improvements WHERE review_id = ? ORDER BY created_at ASC LIMIT 1",
                    (existing["id"],),
                ).fetchone()
                return self._experience_review_result(existing, candidate, deduplicated=True)
            review_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO agent_experience_reviews (
                    id, turn_id, outcome, completion_checks_json, tool_assessment_json,
                    failure_class, metadata_json, status, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?)
                """,
                (
                    review_id,
                    turn["id"],
                    normalized_outcome,
                    json.dumps(checks, ensure_ascii=False),
                    json.dumps(assessment, ensure_ascii=False),
                    normalized_failure or "",
                    json.dumps(sanitized_metadata, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            candidate = None
            if normalized_kind:
                candidate_id = str(uuid.uuid4())
                evidence = {
                    "outcome": normalized_outcome,
                    "failure_class": normalized_failure or "",
                    "completion_checks": checks,
                    "tool_assessment": assessment,
                }
                conn.execute(
                    """
                    INSERT INTO agent_improvements (
                        id, turn_id, review_id, kind, risk, status, evidence_json, resolution_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, '{}', ?, ?)
                    """,
                    (
                        candidate_id,
                        turn["id"],
                        review_id,
                        normalized_kind,
                        normalized_risk,
                        json.dumps(evidence, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                candidate = conn.execute("SELECT * FROM agent_improvements WHERE id = ?", (candidate_id,)).fetchone()
            conn.execute(
                "UPDATE agent_turns SET status = 'reviewed', reviewed_at = ?, updated_at = ? WHERE id = ?",
                (now, now, turn["id"]),
            )
            review = conn.execute("SELECT * FROM agent_experience_reviews WHERE id = ?", (review_id,)).fetchone()
        return self._experience_review_result(review, candidate, deduplicated=False)

    def has_completed_experience_review(self, turn_id: str) -> dict[str, Any]:
        self.initialize()
        with self.connect() as conn:
            turn = self._find_agent_turn_row(conn, turn_id)
            if not turn:
                return {"ok": False, "error": "agent_turn_not_found", "turn_id": turn_id}
            review = conn.execute(
                "SELECT id, outcome, status, completed_at FROM agent_experience_reviews WHERE turn_id = ? LIMIT 1",
                (turn["id"],),
            ).fetchone()
            improvement_rows = conn.execute(
                "SELECT id, status FROM agent_improvements WHERE turn_id = ? ORDER BY created_at ASC",
                (turn["id"],),
            ).fetchall()
        review_completed = bool(review and review["status"] == "completed")
        unresolved_improvements = [
            str(row["id"])
            for row in improvement_rows
            if str(row["status"] or "") not in AGENT_IMPROVEMENT_TERMINAL_STATUSES
        ]
        return {
            "ok": True,
            "turn_id": str(turn["id"]),
            "external_turn_id": str(turn["external_turn_id"] or "") or None,
            "effective_mode": str(turn["effective_mode"]),
            "review_completed": review_completed,
            # A completed reflection alone is not enough in learning mode: an
            # improvement candidate must be promoted, deferred, or rolled
            # back before the answer can be released.
            "learning_cycle_closed": review_completed and not unresolved_improvements,
            "unresolved_improvement_ids": unresolved_improvements,
            "review_id": review["id"] if review else None,
            "outcome": review["outcome"] if review else None,
            "completed_at": review["completed_at"] if review else None,
        }

    def has_completed_experience_review_by_external_id(self, external_turn_id: str) -> dict[str, Any]:
        return self.has_completed_experience_review(external_turn_id)

    def get_agent_turn(self, turn_id: str, *, include_events: bool = True) -> dict[str, Any]:
        self.initialize()
        with self.connect() as conn:
            turn = self._find_agent_turn_row(conn, turn_id)
            if not turn:
                return {"ok": False, "error": "agent_turn_not_found", "turn_id": turn_id}
            item = self._row_to_dict(turn)
            review = conn.execute(
                "SELECT * FROM agent_experience_reviews WHERE turn_id = ? LIMIT 1",
                (turn["id"],),
            ).fetchone()
            if review:
                item["experience_review"] = self._row_to_dict(review)
            if include_events:
                rows = conn.execute(
                    "SELECT * FROM agent_tool_events WHERE turn_id = ? ORDER BY created_at ASC",
                    (turn["id"],),
                ).fetchall()
                item["tool_events"] = [self._row_to_dict(row) for row in rows]
            improvements = conn.execute(
                "SELECT * FROM agent_improvements WHERE turn_id = ? ORDER BY created_at ASC",
                (turn["id"],),
            ).fetchall()
            item["improvements"] = [self._row_to_dict(row) for row in improvements]
        return {"ok": True, "item": item}

    def get_agent_turn_by_external_id(self, external_turn_id: str, *, include_events: bool = True) -> dict[str, Any]:
        return self.get_agent_turn(external_turn_id, include_events=include_events)

    def get_agent_learning_turn_by_external_id_fast(self, external_turn_id: str) -> dict[str, Any]:
        """Read one learning turn without schema initialization or telemetry writes.

        Codex starts a separate hook process for each tool event.  In normal
        work mode there is no turn, so this small read-only path avoids running
        the full migration/DDL initializer for every tool invocation.  A
        missing or pre-migration database simply means no learning turn.
        """

        normalized_external = _normalize_learning_identifier(
            external_turn_id,
            field="external_turn_id",
            allow_empty=False,
        )
        if not normalized_external:
            return {"ok": False, "error": "unsafe_agent_learning_identifier"}
        if not self.path.exists():
            return {"ok": True, "item": None}
        try:
            with self.connect() as conn:
                row = conn.execute(
                    """
                    SELECT id, external_turn_id, effective_mode, status
                    FROM agent_turns
                    WHERE external_turn_id = ? AND effective_mode = 'learning'
                    LIMIT 1
                    """,
                    (normalized_external,),
                ).fetchone()
        except sqlite3.OperationalError:
            return {"ok": True, "item": None}
        if not row:
            return {"ok": True, "item": None}
        return {
            "ok": True,
            "item": {
                "turn_id": str(row["id"]),
                "external_turn_id": str(row["external_turn_id"]),
                "effective_mode": str(row["effective_mode"]),
                "status": str(row["status"]),
            },
        }

    def agent_learning_workflow(  # noqa: C901
        self,
        operation: str,
        *,
        candidate_id: str = "",
        turn_id: str = "",
        verification: dict[str, Any] | None = None,
        reason_code: str = "",
        lesson_content: str = "",
        lesson_title: str = "",
        applies_to: str = "general",
    ) -> dict[str, Any]:
        """Advance a reviewed improvement candidate; actual repairs remain external actions."""

        normalized_operation = str(operation or "").strip().casefold()
        if normalized_operation == "summary":
            return self.get_agent_learning_summary()
        if normalized_operation not in {"repair", "verify", "promote", "defer", "rollback"}:
            return {"ok": False, "error": "invalid_agent_learning_operation"}
        normalized_candidate = _normalize_learning_identifier(candidate_id, field="candidate_id", allow_empty=False)
        if not normalized_candidate:
            return {"ok": False, "error": "candidate_id is required"}
        sanitized_verification, forbidden = _sanitize_agent_learning_metadata(verification)
        normalized_reason = _normalize_learning_identifier(reason_code, field="reason_code", allow_empty=True)
        if forbidden or normalized_reason is None:
            return {
                "ok": False,
                "error": "raw_agent_learning_payload_not_allowed",
                "forbidden_keys": forbidden,
            }
        self.initialize()
        now = _now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            candidate = conn.execute(
                "SELECT * FROM agent_improvements WHERE id = ? LIMIT 1", (normalized_candidate,)
            ).fetchone()
            if not candidate:
                return {"ok": False, "error": "agent_improvement_not_found", "candidate_id": normalized_candidate}
            if turn_id:
                turn = self._find_agent_turn_row(conn, turn_id)
                if not turn or str(turn["id"]) != str(candidate["turn_id"]):
                    return {
                        "ok": False,
                        "error": "agent_improvement_turn_mismatch",
                        "candidate_id": normalized_candidate,
                    }
            current_status = str(candidate["status"] or "")
            next_status = current_status
            resolution = _decode_json(candidate["resolution_json"], {})
            if normalized_operation == "repair":
                if current_status in {"promoted", "rolled_back"}:
                    return {"ok": False, "error": "agent_improvement_is_terminal", "status": current_status}
                if str(candidate["risk"] or "low") != "low":
                    return {
                        "ok": False,
                        "error": "agent_improvement_repair_requires_low_risk",
                        "risk": candidate["risk"],
                    }
                if str(candidate["kind"] or "") == "provider":
                    return {
                        "ok": False,
                        "error": "agent_provider_improvement_must_be_deferred",
                        "kind": candidate["kind"],
                    }
                next_status = "repairing"
                resolution["repair_started"] = True
            elif normalized_operation == "verify":
                if current_status not in {"pending", "repairing", "deferred", "verified"}:
                    return {"ok": False, "error": "agent_improvement_not_verifiable", "status": current_status}
                if sanitized_verification.get("verification_state") not in {"passed", "verified"}:
                    return {"ok": False, "error": "verification_state_passed_required"}
                next_status = "verified"
                resolution["verification"] = sanitized_verification
            elif normalized_operation == "defer":
                if current_status in {"promoted", "rolled_back"}:
                    return {"ok": False, "error": "agent_improvement_is_terminal", "status": current_status}
                if not normalized_reason:
                    return {"ok": False, "error": "reason_code is required"}
                next_status = "deferred"
                resolution["defer_reason_code"] = normalized_reason
            elif normalized_operation == "rollback":
                if current_status not in {"repairing", "verified", "deferred", "rolled_back"}:
                    return {"ok": False, "error": "agent_improvement_not_rollbackable", "status": current_status}
                next_status = "rolled_back"
                if sanitized_verification:
                    resolution["rollback"] = sanitized_verification
            elif normalized_operation == "promote":
                if current_status == "promoted":
                    return self._agent_improvement_result(candidate, deduplicated=True)
                if current_status != "verified":
                    return {
                        "ok": False,
                        "error": "agent_improvement_must_be_verified_before_promotion",
                        "status": current_status,
                    }
                content = _validate_agent_learning_lesson(lesson_content)
                title = _validate_agent_learning_lesson(lesson_title, allow_empty=True)
                normalized_applies_to = _normalize_learning_identifier(
                    applies_to, field="applies_to", allow_empty=False
                )
                if content is None or title is None or not normalized_applies_to:
                    return {"ok": False, "error": "unsafe_agent_learning_lesson"}
                cursor = conn.execute(
                    """
                    INSERT INTO lessons (
                        title, content, applies_to, signal, recommendation, avoid,
                        importance, confidence, source, tags_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 'verified_learning_candidate', '', '', 0.7, 0.8, 'agent_learning', '[]', ?, ?)
                    """,
                    (title or "", content, normalized_applies_to, now, now),
                )
                lesson_id = _required_lastrowid(cursor)
                next_status = "promoted"
                resolution["promoted_lesson_id"] = lesson_id
                conn.execute(
                    """
                    UPDATE agent_improvements
                    SET status = ?, resolution_json = ?, promoted_lesson_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (next_status, json.dumps(resolution, ensure_ascii=False), lesson_id, now, normalized_candidate),
                )
                updated = conn.execute(
                    "SELECT * FROM agent_improvements WHERE id = ?", (normalized_candidate,)
                ).fetchone()
                return self._agent_improvement_result(updated, deduplicated=False)
            if next_status not in AGENT_IMPROVEMENT_STATUSES:
                return {"ok": False, "error": "invalid_agent_improvement_status"}
            conn.execute(
                "UPDATE agent_improvements SET status = ?, resolution_json = ?, updated_at = ? WHERE id = ?",
                (next_status, json.dumps(resolution, ensure_ascii=False), now, normalized_candidate),
            )
            updated = conn.execute("SELECT * FROM agent_improvements WHERE id = ?", (normalized_candidate,)).fetchone()
        return self._agent_improvement_result(updated, deduplicated=current_status == next_status)

    def get_agent_learning_summary(self, *, limit: int = 20) -> dict[str, Any]:
        self.initialize()
        limit = max(1, min(int(limit), 100))
        mode = self.get_agent_mode()
        with self.connect() as conn:
            turn_rows = conn.execute(
                "SELECT effective_mode, status, COUNT(*) AS count FROM agent_turns GROUP BY effective_mode, status"
            ).fetchall()
            improvement_rows = conn.execute(
                "SELECT status, risk, COUNT(*) AS count FROM agent_improvements GROUP BY status, risk"
            ).fetchall()
            recent_rows = conn.execute(
                "SELECT * FROM agent_improvements ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return {
            "ok": True,
            "schema": "AgentLearningSummaryV1",
            "mode": mode,
            "turn_counts": [dict(row) for row in turn_rows],
            "improvement_counts": [dict(row) for row in improvement_rows],
            "recent_improvements": [self._row_to_dict(row) for row in recent_rows],
        }

    def _find_agent_turn_row(self, conn: sqlite3.Connection, turn_ref: str) -> sqlite3.Row | None:
        normalized = _normalize_learning_identifier(turn_ref, field="turn_id", allow_empty=False)
        if not normalized:
            return None
        return conn.execute(
            """
            SELECT * FROM agent_turns
            WHERE id = ? OR external_turn_id = ?
            ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (normalized, normalized, normalized),
        ).fetchone()

    def _derived_tool_event_duration(
        self,
        conn: sqlite3.Connection,
        *,
        turn_id: str,
        tool_name: str,
        tool_use_id_hash: str,
        now: str,
    ) -> int | None:
        if not tool_use_id_hash:
            return None
        rows = conn.execute(
            """
            SELECT metadata_json, created_at FROM agent_tool_events
            WHERE turn_id = ? AND tool_name = ? AND status = 'started'
            ORDER BY id DESC LIMIT 20
            """,
            (turn_id, tool_name),
        ).fetchall()
        for row in rows:
            metadata = _decode_json(row["metadata_json"], {})
            if str(metadata.get("tool_use_id_hash") or "") != tool_use_id_hash:
                continue
            try:
                elapsed = datetime.fromisoformat(now) - datetime.fromisoformat(str(row["created_at"]))
            except ValueError:
                return None
            return _normalize_learning_duration(int(max(elapsed.total_seconds(), 0) * 1000))
        return None

    def _experience_review_result(
        self,
        review: sqlite3.Row,
        candidate: sqlite3.Row | None,
        *,
        deduplicated: bool,
    ) -> dict[str, Any]:
        item = self._row_to_dict(review)
        result = {
            "ok": True,
            "schema": "ExperienceReviewV1",
            "review": item,
            "deduplicated": deduplicated,
        }
        if candidate:
            result["improvement"] = self._row_to_dict(candidate)
        return result

    def _agent_improvement_result(self, row: sqlite3.Row, *, deduplicated: bool) -> dict[str, Any]:
        return {
            "ok": True,
            "schema": "AgentLearningImprovementV1",
            "improvement": self._row_to_dict(row),
            "deduplicated": deduplicated,
        }

    def today_context(self, *, limit: int = 20) -> dict[str, Any]:
        self.initialize()
        rules = load_manager_rules()
        warnings = [] if rules else ["manager_rules_unavailable"]
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
        return {
            "ok": True,
            "generated_at": now,
            "tasks": tasks,
            "recent_journal": journal_rows,
            "manager_rules": rules[:limit],
            "crm_read_order": [
                "agent_bootstrap",
                "agent_board_digest",
                "agent_search",
                "agent_entity_context",
                "for AutoStop App use Store entities; bootstrap is one stateless snapshot and owner digest uses store_digest",
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
        owner_workflow = (
            _store_workflow_operation(
                workflow_id=workflow_id,
                intent=intent,
                scope=scope_payload,
            )
            == STORE_OWNER_LEDGER_OPERATION
        )
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
            if owner_workflow or workflow_id in STORE_RELEASE_SMOKE_LEDGER_WORKFLOW_IDS:
                _cleanup_store_owner_runs(conn, now=datetime.now(UTC))
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
                scope_payload = _decode_json(row["scope_json"], {})
                operation = _store_workflow_operation(
                    workflow_id=row["workflow_id"],
                    intent=row["intent"],
                    scope=scope_payload,
                )
                owner_transition_error = (
                    _store_owner_transition_error(
                        run_id,
                        current_status=current,
                        target_status=target_status,
                        expected_state_version=expected_state_version,
                        scope=scope_payload,
                        checkpoint=_decode_json(row["checkpoint_json"], {}),
                    )
                    if operation == STORE_OWNER_LEDGER_OPERATION
                    else None
                )
                if owner_transition_error is not None or len(str(summary or "").encode("utf-8")) > 4096:
                    return owner_transition_error or {
                        "ok": False,
                        "error": "store_workflow_summary_too_large",
                        "run_id": run_id,
                    }
                store_forbidden = _find_forbidden_store_payload_keys(verification or {})
                if verification and (
                    not (
                        operation == STORE_OWNER_LEDGER_OPERATION
                        and _store_owner_verification_is_refs_only(verification)
                    )
                    and _safe_store_scalar_map(verification, kind="verification") is None
                ):
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
                scope_payload = _decode_json(row["scope_json"], {})
                operation = _store_workflow_operation(
                    workflow_id=row["workflow_id"],
                    intent=row["intent"],
                    scope=scope_payload,
                )
                if operation == STORE_OWNER_LEDGER_OPERATION:
                    owner_completion_error = _store_owner_completion_error(
                        run_id,
                        current_status=current,
                        dry_run=bool(row["dry_run"]),
                        scope=scope_payload,
                        checkpoint=_decode_json(row["checkpoint_json"], {}),
                        verification=verification_payload,
                    )
                    if owner_completion_error:
                        return owner_completion_error
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
                scope_payload = _decode_json(row["scope_json"], {})
                store_forbidden = _find_forbidden_store_payload_keys(checkpoint_payload)
                store_forbidden.extend(_find_unsafe_store_machine_values(checkpoint_payload))
                unknown_keys = sorted(set(checkpoint_payload).difference(STORE_LEDGER_SAFE_CHECKPOINT_KEYS))
                operation = _store_workflow_operation(
                    workflow_id=row["workflow_id"],
                    intent=row["intent"],
                    scope=scope_payload,
                )
                if operation == STORE_OWNER_LEDGER_OPERATION and expected_state_version is None:
                    return {
                        "ok": False,
                        "error": "workflow_state_version_required",
                        "run_id": run_id,
                        "status": row["status"],
                    }
                if operation == STORE_OWNER_LEDGER_OPERATION:
                    store_forbidden.extend(
                        _store_owner_checkpoint_forbidden(
                            current_status=str(row["status"] or ""),
                            scope=scope_payload,
                            existing=_decode_json(row["checkpoint_json"], {}),
                            checkpoint=checkpoint_payload,
                        )
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
            if (
                _store_workflow_operation(
                    workflow_id=run["workflow_id"],
                    intent=run["intent"],
                    scope=_decode_json(run["scope_json"], {}),
                )
                == STORE_OWNER_LEDGER_OPERATION
            ):
                return {
                    "ok": False,
                    "error": "store_owner_external_steps_not_allowed",
                    "run_id": run_id,
                }
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
            if (
                _store_workflow_operation(
                    workflow_id=run["workflow_id"],
                    intent=run["intent"],
                    scope=_decode_json(run["scope_json"], {}),
                )
                == STORE_OWNER_LEDGER_OPERATION
            ):
                return {
                    "ok": False,
                    "error": "store_owner_external_steps_not_allowed",
                    "run_id": run_id,
                }
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
        if (
            _store_workflow_operation(
                workflow_id=str(item.get("workflow_id") or ""),
                intent=str(item.get("intent") or ""),
                scope=item.get("scope") if isinstance(item.get("scope"), dict) else {},
            )
            == STORE_OWNER_LEDGER_OPERATION
        ):
            return {
                "ok": False,
                "error": "store_owner_resume_not_allowed",
                "run_id": run_id,
            }
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
                "SELECT workflow_id, intent, scope_json FROM manager_runs WHERE id = ? LIMIT 1",
                (run_id,),
            ).fetchone()
        if not existing:
            return {"ok": False, "error": "manager run not found", "run_id": run_id}
        if (
            _store_workflow_operation(
                workflow_id=existing["workflow_id"],
                intent=existing["intent"],
                scope=_decode_json(existing["scope_json"], {}),
            )
            == STORE_OWNER_LEDGER_OPERATION
        ):
            return {
                "ok": False,
                "error": "store_owner_compatibility_finish_not_allowed",
                "run_id": run_id,
            }
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
        if "completion_checks_json" in item:
            item["completion_checks"] = _decode_json(item.pop("completion_checks_json"), [])
        if "tool_assessment_json" in item:
            item["tool_assessment"] = _decode_json(item.pop("tool_assessment_json"), [])
        if "evidence_json" in item:
            item["evidence"] = _decode_json(item.pop("evidence_json"), {})
        if "resolution_json" in item:
            item["resolution"] = _decode_json(item.pop("resolution_json"), {})
        if "dry_run" in item:
            item["dry_run"] = bool(item["dry_run"])
        return item

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
        if kind == "task":
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
