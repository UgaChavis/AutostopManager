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

from .config import get_db_path


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
    "expected_revision_sha256",
    "message_sha256",
    "quote_snapshot_hash",
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
# These fields are references to technical artifacts.  Allowing an arbitrary
# compact identifier here would turn a misleadingly named fingerprint into a
# covert channel for business data, so they must be one-way hashes.
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
        "store_quote_conductor",
    }
)
STORE_OWNER_LEDGER_OPERATION = "store_owner_api"
STORE_OWNER_LEDGER_WORKFLOW_ID = "raw:store_owner_api"
STORE_OWNER_LEDGER_INTENT = "raw_store_owner_api"
STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION = "store_quote_conductor"
STORE_QUOTE_CONDUCTOR_LEDGER_WORKFLOW_ID = "store_quote_conductor"
STORE_QUOTE_CONDUCTOR_LEDGER_INTENT = "store_quote_conductor"
# The named conductor is the only owner of this workflow's durable state.
# Generic workflow tools never receive this object and therefore cannot mutate
# conductor runs even when they know a run id.
_STORE_QUOTE_CONDUCTOR_INTERNAL_ACCESS = object()
_STORE_QUOTE_CONDUCTOR_START_SCOPE_KEYS = frozenset(
    {
        "operation",
        "workflow_id",
        "domain",
        "source",
        "correlation_id",
        "target_entity",
        "target_ref_sha256",
        "expected_revision_sha256",
    }
)
_STORE_QUOTE_CONDUCTOR_CHECKPOINT_KEYS = frozenset(
    {
        "contract_id",
        "counts",
        "entries_hash",
        "evidence_hash",
        "error_code",
        "expected_revision_sha256",
        "operation",
        "phase",
        "published_snapshot_hash",
        "quote_snapshot_hash",
        "request_fingerprint",
        "snapshot_at",
        "target_ref_sha256",
        "verification",
    }
)
_STORE_QUOTE_CONDUCTOR_HASH_KEYS = frozenset(
    {
        "entries_hash",
        "evidence_hash",
        "expected_revision_sha256",
        "published_snapshot_hash",
        "quote_snapshot_hash",
        "request_fingerprint",
        "snapshot_at",
        "target_ref_sha256",
    }
)
_STORE_QUOTE_CONDUCTOR_COUNT_KEYS = frozenset({"offers", "entries", "coverage"})
_STORE_QUOTE_CONDUCTOR_PHASES = frozenset(
    {
        "new",
        "evidence_ready",
        "draft_saved",
        "published",
        "waiting_payment",
        "revision_needed",
        "handoff",
        "declined",
        "compensating",
    }
)
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
    "entries_hash",
    "evidence_hash",
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
    "published_snapshot_hash",
    "quote_snapshot_hash",
    "schema_hash",
    "snapshot_at",
    "state_version",
    "status",
    "target_id",
    "target_ref_sha256",
    "target_version",
    "message_sha256",
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
    "failure_class",
    "mode",
    "operation",
    "outcome",
    "phase",
    "route_version",
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
        normalized_workflow_id == STORE_QUOTE_CONDUCTOR_LEDGER_WORKFLOW_ID
        and normalized_intent == STORE_QUOTE_CONDUCTOR_LEDGER_INTENT
        and candidates[0] == STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION
        and str(scope_payload.get("domain") or "").strip().casefold() == "store"
        and str(scope_payload.get("source") or "").strip().casefold() == "store_quote_conductor"
    ):
        return STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION
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


def _store_start_channel_forbidden(  # noqa: C901
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
    if operation == STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION:
        allowed_workflow_ids = {STORE_QUOTE_CONDUCTOR_LEDGER_WORKFLOW_ID}
        allowed_intents = {STORE_QUOTE_CONDUCTOR_LEDGER_INTENT}
    elif operation == STORE_OWNER_LEDGER_OPERATION:
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
    if operation == STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION:
        required = {
            "correlation_id": str(scope.get("correlation_id") or "").strip(),
            "expected_revision_sha256": str(scope.get("expected_revision_sha256") or "").strip(),
            "target_ref_sha256": str(scope.get("target_ref_sha256") or "").strip(),
        }
        if required["correlation_id"] != str(correlation_id or "").strip():
            forbidden.append("scope.correlation_id")
        if str(scope.get("domain") or "").strip().casefold() != "store":
            forbidden.append("scope.domain")
        if str(scope.get("source") or "").strip().casefold() != "store_quote_conductor":
            forbidden.append("scope.source")
        for key in ("expected_revision_sha256", "target_ref_sha256"):
            if re.fullmatch(r"[0-9a-f]{64}", required[key]) is None:
                forbidden.append(f"scope.{key}")
    elif operation == STORE_OWNER_LEDGER_OPERATION:
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
    if operation == STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION:
        allowed.add(STORE_QUOTE_CONDUCTOR_LEDGER_WORKFLOW_ID)
    elif operation == STORE_OWNER_LEDGER_OPERATION:
        allowed.add(STORE_OWNER_LEDGER_WORKFLOW_ID)
    elif operation:
        allowed.update({f"inventory:{operation}", f"store:{operation}"})
    return normalized in allowed


def _store_quote_conductor_hash(value: Any) -> bool:
    return re.fullmatch(r"[0-9a-f]{64}", str(value or "").strip()) is not None


def _store_quote_conductor_code(value: Any) -> bool:
    normalized = str(value or "").strip().casefold()
    return re.fullmatch(r"[a-z][a-z0-9_]{0,159}", normalized) is not None


def _store_quote_conductor_start_forbidden(
    *,
    workflow_id: str,
    intent: str,
    query: str,
    correlation_id: str,
    source: str,
    scope: dict[str, Any],
    metadata: dict[str, Any],
    selected_ids: list[str],
    dry_run: bool,
    active_target_ref_sha256: str,
) -> list[str]:
    """Validate the conductor's deliberately tiny durable start record."""

    forbidden: list[str] = []
    if workflow_id != STORE_QUOTE_CONDUCTOR_LEDGER_WORKFLOW_ID:
        forbidden.append("workflow_id")
    if intent != STORE_QUOTE_CONDUCTOR_LEDGER_INTENT:
        forbidden.append("intent")
    if query:
        forbidden.append("query")
    if metadata:
        forbidden.append("metadata")
    if selected_ids:
        forbidden.append("selected_ids")
    if dry_run:
        forbidden.append("dry_run")
    if source != "store_quote_conductor":
        forbidden.append("source")
    if set(scope) != _STORE_QUOTE_CONDUCTOR_START_SCOPE_KEYS:
        forbidden.append("scope_keys")
    expected_values = {
        "operation": STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION,
        "workflow_id": STORE_QUOTE_CONDUCTOR_LEDGER_WORKFLOW_ID,
        "domain": "store",
        "source": "store_quote_conductor",
        "target_entity": "store_quote_request",
    }
    for key, expected in expected_values.items():
        if str(scope.get(key) or "").strip().casefold() != expected:
            forbidden.append(f"scope.{key}")
    scope_correlation = str(scope.get("correlation_id") or "").strip()
    if (
        not scope_correlation
        or scope_correlation != correlation_id
        or not _store_machine_value_is_safe(scope_correlation)
    ):
        forbidden.append("scope.correlation_id")
    for key in ("target_ref_sha256", "expected_revision_sha256"):
        if not _store_quote_conductor_hash(scope.get(key)):
            forbidden.append(f"scope.{key}")
    if active_target_ref_sha256 != str(scope.get("target_ref_sha256") or "").strip():
        forbidden.append("active_target_ref_sha256")
    return list(dict.fromkeys(forbidden))


def _store_quote_conductor_checkpoint_forbidden(  # noqa: C901
    *,
    scope: dict[str, Any],
    checkpoint: dict[str, Any],
    selected_ids: list[str] | None,
    message: str,
    expected_state_version: int | None,
) -> list[str]:
    """Keep quote-conductor checkpoints strictly hash/code/count based.

    This is intentionally narrower than the generic Store ledger.  In
    particular, it leaves no extensible numeric map that could carry client
    prices and no free-form value that could carry Telegram text or a peer.
    """

    forbidden: list[str] = []
    if expected_state_version is None:
        forbidden.append("expected_state_version")
    if selected_ids not in (None, []):
        forbidden.append("selected_ids")
    if message != f"verify {STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION}":
        forbidden.append("message")
    unknown_keys = set(checkpoint).difference(_STORE_QUOTE_CONDUCTOR_CHECKPOINT_KEYS)
    forbidden.extend(sorted(unknown_keys))
    if checkpoint.get("operation") != STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION:
        forbidden.append("operation")
    phase = str(checkpoint.get("phase") or "").strip()
    if phase not in _STORE_QUOTE_CONDUCTOR_PHASES:
        forbidden.append("phase")
    if not _store_quote_conductor_hash(checkpoint.get("expected_revision_sha256")):
        forbidden.append("expected_revision_sha256")
    if checkpoint.get("target_ref_sha256") != scope.get("target_ref_sha256"):
        forbidden.append("target_ref_sha256")
    for key in _STORE_QUOTE_CONDUCTOR_HASH_KEYS:
        if key in checkpoint and not _store_quote_conductor_hash(checkpoint.get(key)):
            forbidden.append(key)
    contract_id = checkpoint.get("contract_id")
    if contract_id is not None and re.fullmatch(r"ac_[0-9a-f]{20}", str(contract_id or "")) is None:
        forbidden.append("contract_id")
    error_code = checkpoint.get("error_code")
    if error_code is not None and not _store_quote_conductor_code(error_code):
        forbidden.append("error_code")
    counts = checkpoint.get("counts")
    if counts is not None:
        if not isinstance(counts, dict) or not set(counts).issubset(_STORE_QUOTE_CONDUCTOR_COUNT_KEYS):
            forbidden.append("counts")
        else:
            for key, value in counts.items():
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 50:
                    forbidden.append(f"counts.{key}")

    verification = checkpoint.get("verification")
    if verification is not None:
        required_verification = {"route_version", "outcome", "failure_class", "offers", "entries", "coverage"}
        if not isinstance(verification, dict) or set(verification) != required_verification:
            forbidden.append("verification")
        else:
            if verification.get("route_version") != "store_quote_conductor_v1":
                forbidden.append("verification.route_version")
            if str(verification.get("outcome") or "") not in _STORE_QUOTE_CONDUCTOR_PHASES:
                forbidden.append("verification.outcome")
            if not _store_quote_conductor_code(verification.get("failure_class")):
                forbidden.append("verification.failure_class")
            for key in _STORE_QUOTE_CONDUCTOR_COUNT_KEYS:
                value = verification.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 50:
                    forbidden.append(f"verification.{key}")
    return list(dict.fromkeys(forbidden))


def _store_quote_conductor_transition_forbidden(
    *,
    status: str,
    message: str,
    verification: dict[str, Any] | None,
    summary: str,
    expected_state_version: int | None,
) -> list[str]:
    forbidden: list[str] = []
    if expected_state_version is None:
        forbidden.append("expected_state_version")
    if not _store_message_is_allowed(message, operation=STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION):
        forbidden.append("message")
    if summary not in {"", STORE_QUOTE_CONDUCTOR_LEDGER_WORKFLOW_ID}:
        forbidden.append("summary")
    proof = verification if isinstance(verification, dict) else {}
    if status == "completed":
        if proof != {"workflow_completed": True}:
            forbidden.append("verification")
    elif status == "compensating":
        if proof != {"executor_ok": True, "passed": False}:
            forbidden.append("verification")
    elif verification not in (None, {}):
        forbidden.append("verification")
    return list(dict.fromkeys(forbidden))


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


@dataclass(frozen=True)
class StoreState:
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

                CREATE INDEX IF NOT EXISTS idx_manager_runs_status
                    ON manager_runs(status, started_at);

                CREATE INDEX IF NOT EXISTS idx_manager_run_events_run_id
                    ON manager_run_events(run_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_manager_run_external_steps_run_id
                    ON manager_run_external_steps(run_id, status, created_at);

                CREATE INDEX IF NOT EXISTS idx_store_checkpoints_status
                    ON store_checkpoints(last_attempt_status, updated_at);

                """
            )
            self._ensure_columns(conn)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_manager_runs_idempotency "
                "ON manager_runs(idempotency_key) WHERE idempotency_key <> ''"
            )

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        desired = {
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

    def store_quote_conductor_release_readiness(self) -> dict[str, Any]:
        """Read aggregate legacy state without opening the persistent DB for writing."""

        path = self.path
        if not path.is_file():
            return {
                "ok": False,
                "read_only": True,
                "error": "store_quote_conductor_release_database_unavailable",
                "blocking_reasons": ["store_quote_conductor_release_database_unavailable"],
            }

        placeholders = ",".join("?" for _ in WORKFLOW_TERMINAL_STATES)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            connection.execute("PRAGMA query_only = ON")
            rows = connection.execute(
                f"""
                SELECT
                    runs.status,
                    runs.checkpoint_json,
                    COUNT(steps.id) AS external_steps,
                    COALESCE(SUM(CASE WHEN steps.status != 'completed' THEN 1 ELSE 0 END), 0)
                        AS pending_external_steps
                FROM manager_runs AS runs
                LEFT JOIN manager_run_external_steps AS steps ON steps.run_id = runs.id
                WHERE runs.workflow_id = ? AND runs.status NOT IN ({placeholders})
                GROUP BY runs.id, runs.status, runs.checkpoint_json
                """,
                [STORE_QUOTE_CONDUCTOR_LEDGER_WORKFLOW_ID, *sorted(WORKFLOW_TERMINAL_STATES)],
            ).fetchall()
        except (OSError, sqlite3.Error):
            return {
                "ok": False,
                "read_only": True,
                "error": "store_quote_conductor_release_state_unavailable",
                "blocking_reasons": ["store_quote_conductor_release_state_unavailable"],
            }
        finally:
            if connection is not None:
                connection.close()

        active_by_status: dict[str, int] = {}
        active_by_phase: dict[str, int] = {}
        legacy_by_phase: dict[str, int] = {}
        legacy_active_total = 0
        legacy_checkpoint_count = 0
        runs_with_external_steps = 0
        pending_external_steps = 0
        external_wait_runs = 0

        for status, checkpoint_json, external_steps, pending_steps in rows:
            normalized_status = str(status or "") or "missing"
            active_by_status[normalized_status] = active_by_status.get(normalized_status, 0) + 1
            checkpoint = _decode_json(checkpoint_json, None)
            phase = str(checkpoint.get("phase") or "").strip() if isinstance(checkpoint, dict) else ""
            normalized_phase = phase or "missing"
            active_by_phase[normalized_phase] = active_by_phase.get(normalized_phase, 0) + 1
            external_steps = int(external_steps or 0)
            pending_steps = int(pending_steps or 0)
            runs_with_external_steps += int(external_steps > 0)
            pending_external_steps += pending_steps

            legacy_checkpoint = not isinstance(checkpoint, dict) or bool(
                set(checkpoint).difference(_STORE_QUOTE_CONDUCTOR_CHECKPOINT_KEYS)
            )
            legacy = (
                normalized_status == "external_wait"
                or phase not in _STORE_QUOTE_CONDUCTOR_PHASES
                or legacy_checkpoint
                or external_steps > 0
            )
            if not legacy:
                continue
            legacy_active_total += 1
            legacy_by_phase[normalized_phase] = legacy_by_phase.get(normalized_phase, 0) + 1
            legacy_checkpoint_count += int(legacy_checkpoint)
            external_wait_runs += int(normalized_status == "external_wait")

        blocking_reasons: list[str] = []
        if legacy_active_total:
            blocking_reasons.append("legacy_store_quote_conductor_active_runs")
        if external_wait_runs:
            blocking_reasons.append("legacy_store_quote_conductor_external_wait_runs")
        if legacy_checkpoint_count:
            blocking_reasons.append("legacy_store_quote_conductor_checkpoints")
        if runs_with_external_steps:
            blocking_reasons.append("legacy_store_quote_conductor_external_steps")
        return {
            "ok": not blocking_reasons,
            "format": "store_quote_conductor_release_readiness_v1",
            "read_only": True,
            "active_total": len(rows),
            "legacy_active_total": legacy_active_total,
            "active_by_status": dict(sorted(active_by_status.items())),
            "active_by_phase": dict(sorted(active_by_phase.items())),
            "legacy_by_phase": dict(sorted(legacy_by_phase.items())),
            "runs_with_external_steps": runs_with_external_steps,
            "pending_external_steps": pending_external_steps,
            "legacy_checkpoint_count": legacy_checkpoint_count,
            "blocking_reasons": blocking_reasons,
        }

    def start_store_quote_conductor_run(
        self,
        *,
        idempotency_key: str,
        correlation_id: str,
        scope: dict[str, Any],
        active_target_ref_sha256: str,
    ) -> dict[str, Any]:
        """Start the named Store quote conductor with its closed ledger schema.

        This method is intentionally not registered as a generic Manager MCP
        capability.  The conductor is its only caller; public workflow tools
        must not be able to manufacture or take over a quote run.
        """

        return self.start_workflow_run(
            workflow_id=STORE_QUOTE_CONDUCTOR_LEDGER_WORKFLOW_ID,
            intent=STORE_QUOTE_CONDUCTOR_LEDGER_INTENT,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            scope=scope,
            source="store_quote_conductor",
            active_target_ref_sha256=active_target_ref_sha256,
            _conductor_access=_STORE_QUOTE_CONDUCTOR_INTERNAL_ACCESS,
        )

    def start_workflow_run(  # noqa: C901
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
        active_target_ref_sha256: str | None = None,
        _conductor_access: object | None = None,
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
        normalized_active_target_ref = str(active_target_ref_sha256 or "").strip()
        owner_workflow = (
            _store_workflow_operation(
                workflow_id=workflow_id,
                intent=intent,
                scope=scope_payload,
            )
            == STORE_OWNER_LEDGER_OPERATION
        )
        conductor_workflow = (
            _store_workflow_operation(
                workflow_id=workflow_id,
                intent=intent,
                scope=scope_payload,
            )
            == STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION
        )
        if conductor_workflow and _conductor_access is not _STORE_QUOTE_CONDUCTOR_INTERNAL_ACCESS:
            return {
                "ok": False,
                "error": "store_quote_conductor_ledger_owned_by_named_workflow",
            }
        if normalized_active_target_ref and (
            not conductor_workflow
            or re.fullmatch(r"[0-9a-f]{64}", normalized_active_target_ref) is None
            or normalized_active_target_ref != str(scope_payload.get("target_ref_sha256") or "").strip()
        ):
            return {"ok": False, "error": "store_quote_conductor_active_target_invalid"}
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
            if conductor_workflow:
                conductor_forbidden = _store_quote_conductor_start_forbidden(
                    workflow_id=workflow_id,
                    intent=intent,
                    query=query,
                    correlation_id=effective_correlation_id,
                    source=str(source or "").strip().casefold(),
                    scope=scope_payload,
                    metadata=metadata_payload,
                    selected_ids=normalized_ids,
                    dry_run=dry_run,
                    active_target_ref_sha256=normalized_active_target_ref,
                )
                if conductor_forbidden:
                    return {
                        "ok": False,
                        "error": "store_quote_conductor_ledger_schema_invalid",
                        "forbidden_keys": conductor_forbidden,
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

            if normalized_active_target_ref:
                placeholders = ",".join("?" for _ in ACTIVE_WORKFLOW_STATES)
                active_rows = conn.execute(
                    f"""
                    SELECT * FROM manager_runs
                    WHERE workflow_id = ? AND intent = ? AND status IN ({placeholders})
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (
                        STORE_QUOTE_CONDUCTOR_LEDGER_WORKFLOW_ID,
                        STORE_QUOTE_CONDUCTOR_LEDGER_INTENT,
                        *sorted(ACTIVE_WORKFLOW_STATES),
                    ),
                ).fetchall()
                for active in active_rows:
                    active_scope = _decode_json(active["scope_json"], {})
                    if str(active_scope.get("target_ref_sha256") or "").strip() != normalized_active_target_ref:
                        continue
                    item = self._row_to_dict(active)
                    return {
                        "ok": True,
                        **item,
                        "deduplicated": True,
                        "active_target_deduplicated": True,
                    }

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

    def transition_store_quote_conductor_run(
        self,
        run_id: int,
        *,
        status: str,
        message: str,
        verification: dict[str, Any] | None = None,
        summary: str = "",
        expected_state_version: int,
    ) -> dict[str, Any]:
        """Advance a conductor run through its fixed, refs-only state machine."""

        return self.transition_workflow_run(
            run_id,
            status=status,
            message=message,
            verification=verification,
            summary=summary,
            expected_state_version=expected_state_version,
            _conductor_access=_STORE_QUOTE_CONDUCTOR_INTERNAL_ACCESS,
        )

    def transition_workflow_run(  # noqa: C901
        self,
        run_id: int,
        *,
        status: str,
        message: str = "",
        verification: dict[str, Any] | None = None,
        summary: str = "",
        expected_state_version: int | None = None,
        _conductor_access: object | None = None,
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
                if operation == STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION:
                    if _conductor_access is not _STORE_QUOTE_CONDUCTOR_INTERNAL_ACCESS:
                        return {
                            "ok": False,
                            "error": "store_quote_conductor_ledger_owned_by_named_workflow",
                            "run_id": run_id,
                        }
                    conductor_forbidden = _store_quote_conductor_transition_forbidden(
                        status=target_status,
                        message=message,
                        verification=verification,
                        summary=summary,
                        expected_state_version=expected_state_version,
                    )
                    if conductor_forbidden:
                        return {
                            "ok": False,
                            "error": "store_quote_conductor_ledger_schema_invalid",
                            "run_id": run_id,
                            "forbidden_keys": conductor_forbidden,
                        }
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

    def checkpoint_store_quote_conductor_run(
        self,
        run_id: int,
        *,
        checkpoint: dict[str, Any],
        message: str,
        expected_state_version: int,
    ) -> dict[str, Any]:
        """Persist the conductor's closed checkpoint projection only."""

        return self.checkpoint_workflow_run(
            run_id,
            checkpoint=checkpoint,
            message=message,
            expected_state_version=expected_state_version,
            _conductor_access=_STORE_QUOTE_CONDUCTOR_INTERNAL_ACCESS,
        )

    def checkpoint_workflow_run(
        self,
        run_id: int,
        *,
        checkpoint: dict[str, Any],
        selected_ids: list[str] | None = None,
        message: str = "",
        expected_state_version: int | None = None,
        _conductor_access: object | None = None,
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
                if operation == STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION:
                    if _conductor_access is not _STORE_QUOTE_CONDUCTOR_INTERNAL_ACCESS:
                        return {
                            "ok": False,
                            "error": "store_quote_conductor_ledger_owned_by_named_workflow",
                            "run_id": run_id,
                        }
                    conductor_forbidden = _store_quote_conductor_checkpoint_forbidden(
                        scope=scope_payload,
                        checkpoint=checkpoint_payload,
                        selected_ids=selected_ids,
                        message=message,
                        expected_state_version=expected_state_version,
                    )
                    if conductor_forbidden:
                        return {
                            "ok": False,
                            "error": "store_quote_conductor_ledger_schema_invalid",
                            "run_id": run_id,
                            "forbidden_keys": conductor_forbidden,
                        }
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
            operation = _store_workflow_operation(
                workflow_id=run["workflow_id"],
                intent=run["intent"],
                scope=_decode_json(run["scope_json"], {}),
            )
            if operation == STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION:
                return {
                    "ok": False,
                    "error": "store_quote_conductor_ledger_owned_by_named_workflow",
                    "run_id": run_id,
                }
            if operation == STORE_OWNER_LEDGER_OPERATION:
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
            operation = _store_workflow_operation(
                workflow_id=run["workflow_id"],
                intent=run["intent"],
                scope=_decode_json(run["scope_json"], {}),
            )
            if operation == STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION:
                return {
                    "ok": False,
                    "error": "store_quote_conductor_ledger_owned_by_named_workflow",
                    "run_id": run_id,
                }
            if operation == STORE_OWNER_LEDGER_OPERATION:
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

    def resume_workflow_run(
        self,
        run_id: int,
        *,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        run = self.get_manager_run(run_id, include_events=False, include_external_steps=True)
        if not run.get("ok"):
            return run
        item = run["item"]
        operation = _store_workflow_operation(
            workflow_id=str(item.get("workflow_id") or ""),
            intent=str(item.get("intent") or ""),
            scope=item.get("scope") if isinstance(item.get("scope"), dict) else {},
        )
        if operation == STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION:
            return {
                "ok": False,
                "error": "store_quote_conductor_ledger_owned_by_named_workflow",
                "run_id": run_id,
            }
        if operation == STORE_OWNER_LEDGER_OPERATION:
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
