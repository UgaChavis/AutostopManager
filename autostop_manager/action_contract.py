from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from .store_owner_api import is_safe_reversible_collection_create


MUTATING_ACTIONS = {
    "create",
    "update",
    "move",
    "archive",
    "delete",
    "merge",
    "set_deadline",
    "bulk_set_deadline_if_below",
    "record_payment",
    "cash_transaction",
    "transfer",
    "adjust",
    "upload",
    "generate",
    "send",
    "forward",
    "label",
    "batch_modify",
    "bulk_label",
    "create_label",
    "create_draft",
    "update_draft",
    "send_draft",
    "assign_quote_request",
    "set_quote_request_status",
    "update_quote_request_comment",
    "set_batch_storage_location",
    "mark_order_ready",
    "add_quote_request_note",
    "replace_quote_offer_drafts",
    "execute_owner_api",
}

CREATE_ACTIONS = {
    "create",
    "record_payment",
    "cash_transaction",
    "transfer",
    "upload",
    "generate",
    "send",
    "forward",
    "create_label",
    "create_draft",
}
COLLECTION_TARGET_ACTIONS = {
    ("gmail", "label"),
    ("gmail", "archive"),
    ("gmail", "delete"),
    ("gmail", "batch_modify"),
    ("gmail", "bulk_label"),
    ("gmail", "update_draft"),
    ("gmail", "send_draft"),
    ("board", "bulk_set_deadline_if_below"),
}

GMAIL_ACTION_ALIASES = {
    "send_email": "send",
    "forward_emails": "forward",
    "apply_labels_to_emails": "label",
    "archive_emails": "archive",
    "delete_emails": "delete",
    "batch_modify_email": "batch_modify",
    "bulk_label_matching_emails": "bulk_label",
}

DOMAIN_ALIASES = {
    "cards": "card",
    "clients": "client",
    "vehicles": "vehicle",
    "orders": "repair_order",
    "repair_orders": "repair_order",
    "payments": "payment",
    "cashboxes": "cashbox",
    "inventory_items": "inventory",
    "documents": "document",
    "files": "file",
    "email": "gmail",
    "crm_board": "board",
    "store_quote_requests": "store_quote_request",
    "store_batches": "store_batch",
    "store_orders": "store_order",
}

EXECUTOR_TOOLS = {
    ("card", "update"): "update_card",
    ("card", "move"): "move_card",
    ("card", "archive"): "archive_card",
    ("card", "set_deadline"): "set_card_deadline",
    ("board", "bulk_set_deadline_if_below"): "agent_board_workflow",
    ("client", "create"): "create_client",
    ("client", "update"): "update_client",
    ("vehicle", "create"): "upsert_client_vehicle",
    ("vehicle", "update"): "upsert_client_vehicle",
    ("repair_order", "update"): "update_repair_order",
    ("payment", "record_payment"): "agent_finance_workflow",
    ("cashbox", "create"): "create_cashbox",
    ("cashbox", "delete"): "delete_cashbox",
    ("cashbox", "cash_transaction"): "create_cash_transaction",
    ("document", "generate"): "create_document_without_card_pdf",
    ("file", "upload"): "upload_shared_file",
    ("gmail", "send"): "gmail:_send_email",
    ("gmail", "forward"): "gmail:_forward_emails",
    ("gmail", "label"): "gmail:_apply_labels_to_emails",
    ("gmail", "archive"): "gmail:_archive_emails",
    ("gmail", "delete"): "gmail:_delete_emails",
    ("gmail", "batch_modify"): "gmail:_batch_modify_email",
    ("gmail", "bulk_label"): "gmail:_bulk_label_matching_emails",
    ("gmail", "create_label"): "gmail:_create_label",
    ("gmail", "create_draft"): "gmail:_create_draft",
    ("gmail", "update_draft"): "gmail:_update_draft",
    ("gmail", "send_draft"): "gmail:_send_draft",
    ("store_quote_request", "assign_quote_request"): "agent_inventory_workflow",
    ("store_quote_request", "set_quote_request_status"): "agent_inventory_workflow",
    ("store_quote_request", "update_quote_request_comment"): "agent_inventory_workflow",
    ("store_quote_request", "add_quote_request_note"): "agent_inventory_workflow",
    ("store_quote_request", "replace_quote_offer_drafts"): "agent_inventory_workflow",
    ("store_batch", "set_batch_storage_location"): "agent_inventory_workflow",
    ("store_order", "mark_order_ready"): "agent_inventory_workflow",
    ("store_owner_api", "execute_owner_api"): "store_owner_api",
}

INVENTORY_EXECUTOR_TOOLS = {
    "replenish": "replenish_inventory_item",
    "write_off": "write_off_inventory_item",
    "return": "return_inventory_movement",
}

FINANCIAL_DOMAINS = {"payment", "cashbox"}
FINANCIAL_TRANSACTION_ACTIONS = {
    ("payment", "record_payment"),
    ("cashbox", "cash_transaction"),
    ("cashbox", "transfer"),
}
DESTRUCTIVE_ACTIONS = {"delete", "merge", "archive"}
TARGET_ONLY_ACTIONS = {"delete", "archive"}
EXTERNAL_DOMAINS = {"gmail"}
STORE_DOMAINS = {"store_quote_request", "store_batch", "store_order", "store_owner_api"}
STORE_ACTIONS = {
    ("store_quote_request", "assign_quote_request"),
    ("store_quote_request", "set_quote_request_status"),
    ("store_quote_request", "update_quote_request_comment"),
    ("store_quote_request", "add_quote_request_note"),
    ("store_quote_request", "replace_quote_offer_drafts"),
    ("store_batch", "set_batch_storage_location"),
    ("store_order", "mark_order_ready"),
}
STORE_OWNER_ACTIONS = {("store_owner_api", "execute_owner_api")}
STORE_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,159}$")
MAX_MONEY_MINOR = 100_000_000_000_000
MAX_MONEY_AMOUNT = MAX_MONEY_MINOR / 100
DEADLINE_PART_MAXIMUMS = {
    "days": 365,
    "hours": 23,
    "minutes": 59,
    "seconds": 59,
    "total_seconds": 31_536_000,
}


def prepare_action_contract(
    *,
    domain: str,
    action: str,
    target_id: str = "",
    planned_changes: dict[str, Any] | None = None,
    owner_intent: str = "",
    expected_revision: str | None = None,
    idempotency_key: str = "",
    correlation_id: str = "",
    run_id: int | None = None,
    actor: str = "codex-owner-agent",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Build a write-safe, connector-neutral action contract without executing it."""

    normalized_domain = DOMAIN_ALIASES.get(str(domain or "").strip().casefold(), str(domain or "").strip().casefold())
    normalized_action = _normalize_action(normalized_domain, action)
    normalized_target = str(target_id or "").strip()
    changes = dict(planned_changes) if isinstance(planned_changes, dict) else {}
    changes = _normalize_store_planned_changes(normalized_action, changes)
    intent = str(owner_intent or "").strip()
    key = str(idempotency_key or "").strip()
    requested_correlation_id = str(correlation_id or "").strip()
    revision = str(expected_revision or "").strip() or None
    owner_collection_create = _is_store_owner_collection_create(normalized_domain, changes)
    exact_target_id_required = (
        normalized_action not in CREATE_ACTIONS
        and (normalized_domain, normalized_action) not in COLLECTION_TARGET_ACTIONS
    )
    concurrency_required = (
        exact_target_id_required and not owner_collection_create
    ) or normalized_domain in FINANCIAL_DOMAINS

    blockers: list[str] = []
    warnings: list[str] = []
    if not normalized_domain:
        blockers.append("missing_domain")
    if not normalized_action:
        blockers.append("missing_action")
    if normalized_action not in MUTATING_ACTIONS:
        blockers.append("unsupported_mutating_action")
    if exact_target_id_required and not normalized_target:
        blockers.append("missing_exact_target_id")
    if not intent:
        blockers.append("missing_task_specific_owner_intent")
    if not key:
        blockers.append("missing_idempotency_key")
    if concurrency_required and not revision:
        blockers.append("missing_expected_revision")
    if not changes and normalized_action not in TARGET_ONLY_ACTIONS:
        blockers.append("missing_planned_changes")
    blockers.extend(_store_correlation_blockers(normalized_domain, requested_correlation_id))

    _validate_domain_changes(normalized_domain, normalized_action, changes, blockers, warnings)
    if normalized_action in DESTRUCTIVE_ACTIONS:
        warnings.append("destructive_action_requires_backup_or_compensation")
    if normalized_domain in EXTERNAL_DOMAINS:
        warnings.append("execute_through_external_connector_step")

    executor_tool = _executor_tool(normalized_domain, normalized_action, changes)
    if not executor_tool:
        warnings.append("executor_tool_requires_capability_discovery")

    contract_id = _contract_id(
        normalized_domain,
        normalized_action,
        normalized_target,
        changes,
        key,
        revision,
    )
    stable_correlation_id = _action_correlation_id(
        domain=normalized_domain,
        action=normalized_action,
        target_id=normalized_target,
        changes=changes,
        revision=revision,
        requested=requested_correlation_id,
        contract_id=contract_id,
    )
    verification_checks = _verification_checks(
        normalized_domain,
        normalized_action,
        dry_run=dry_run,
        changes=changes,
    )
    preflight_checks = [
        "exact_target_resolved",
        "task_specific_owner_intent_present",
        "idempotency_key_unused_or_same_contract",
        "current_state_read",
    ]
    if revision:
        preflight_checks.append("expected_revision_matches")
    if (normalized_domain, normalized_action) in FINANCIAL_TRANSACTION_ACTIONS:
        preflight_checks.extend(["cashbox_exists", "amount_and_payment_method_valid", "debt_reconciled"])
    if normalized_domain == "gmail":
        preflight_checks.extend(["thread_or_recipients_reread", "active_connector_schema_checked"])
    preflight_checks.extend(
        _store_preflight_checks(
            domain=normalized_domain,
            action=normalized_action,
            revision=revision,
            owner_collection_create=owner_collection_create,
        )
    )

    workflow_operation = (
        normalized_action
        if normalized_domain == "board"
        else "record_repair_order_payment"
        if normalized_domain == "payment" and normalized_action == "record_payment"
        else None
    )
    gateway_arguments = None
    if normalized_domain == "board" and normalized_action == "bulk_set_deadline_if_below":
        gateway_arguments = {
            "operation": normalized_action,
            "payload": changes,
            "idempotency_key": key or None,
            "mode": "dry_run" if dry_run else "apply",
        }
    elif normalized_domain == "payment" and normalized_action == "record_payment":
        gateway_arguments = {
            "operation": "record_repair_order_payment",
            "payload": {**changes, "expected_updated_at": revision},
            "idempotency_key": key or None,
        }
    elif (normalized_domain, normalized_action) in STORE_ACTIONS:
        gateway_arguments = {
            "operation": normalized_action,
            "payload": {
                "domain": normalized_domain,
                "target_id": normalized_target,
                "expected_updated_at": revision,
                "owner_intent": intent,
                "planned_changes": changes,
                "correlation_id": stable_correlation_id,
                **changes,
            },
            "idempotency_key": key or None,
            "mode": "dry_run" if dry_run else "apply",
        }
        workflow_operation = normalized_action

    return {
        "ok": not blockers,
        "format": "action_contract_v2",
        "contract_id": contract_id,
        "correlation_id": stable_correlation_id,
        "run_id": run_id,
        "domain": normalized_domain,
        "action": normalized_action,
        "target": {"type": normalized_domain, "id": normalized_target or None},
        "actor": str(actor or "codex-owner-agent"),
        "owner_intent": intent,
        "dry_run": bool(dry_run),
        "planned_changes": changes,
        "concurrency": {
            "expected_revision": revision,
            "required": concurrency_required,
        },
        "idempotency": {"key": key or None, "required": True},
        "preflight": {"checks": preflight_checks, "blocking_reasons": blockers},
        "execution": {
            "ready": not blockers and bool(executor_tool),
            "tool": executor_tool,
            "operation": workflow_operation,
            "gateway_arguments": gateway_arguments,
            "external_connector": "gmail" if normalized_domain == "gmail" else None,
            "response_mode": "compact",
        },
        "verification": {
            "checks": verification_checks,
            "requires_readback": True,
            "record_correlation_id": True,
        },
        "compensation": {
            "required": (
                normalized_domain in FINANCIAL_DOMAINS
                or normalized_action in DESTRUCTIVE_ACTIONS
                or normalized_domain in STORE_DOMAINS
            ),
            "strategy": _compensation_strategy(normalized_domain, normalized_action),
        },
        "ledger": {
            "events": ["planned_action", "preflight", "write", "verification"],
            "store_payload": False,
            "store_refs_only": normalized_domain in EXTERNAL_DOMAINS or normalized_domain in STORE_DOMAINS,
        },
        "warnings": list(dict.fromkeys(warnings)),
    }


def _validate_domain_changes(
    domain: str,
    action: str,
    changes: dict[str, Any],
    blockers: list[str],
    warnings: list[str],
) -> None:
    if (domain, action) in FINANCIAL_TRANSACTION_ACTIONS:
        _validate_financial_changes(domain, action, changes, blockers, warnings)
    if domain == "cashbox" and action == "create" and not str(changes.get("name") or "").strip():
        blockers.append("missing_cashbox_name")
    if domain == "inventory" and action == "adjust":
        _validate_inventory_changes(changes, blockers)
    if domain == "card":
        _validate_card_changes(action, changes, blockers)
    if domain == "board":
        _validate_board_changes(action, changes, blockers)
    if domain == "gmail":
        _validate_gmail_changes(action, changes, blockers)
    if domain == "document" and action == "generate" and not str(changes.get("request_text") or "").strip():
        blockers.append("missing_request_text")
    if domain == "file" and action == "upload":
        if not str(changes.get("file_name") or "").strip():
            blockers.append("missing_file_name")
        if not str(changes.get("content_base64") or "").strip():
            blockers.append("missing_content_base64")
    if domain in STORE_DOMAINS:
        _validate_store_changes(domain, action, changes, blockers, warnings)


def _validate_store_changes(
    domain: str,
    action: str,
    changes: dict[str, Any],
    blockers: list[str],
    warnings: list[str],
) -> None:
    if (domain, action) not in STORE_ACTIONS | STORE_OWNER_ACTIONS:
        blockers.append("unsupported_store_management_operation")
        return
    if domain == "store_owner_api":
        _validate_store_owner_api_changes(changes, blockers)
        return
    allowed_fields: dict[tuple[str, str], set[str]] = {
        ("store_quote_request", "assign_quote_request"): {"assignee_id"},
        ("store_quote_request", "set_quote_request_status"): {"status"},
        ("store_quote_request", "update_quote_request_comment"): {"internal_comment"},
        ("store_quote_request", "add_quote_request_note"): {"text"},
        ("store_quote_request", "replace_quote_offer_drafts"): {"items"},
        ("store_batch", "set_batch_storage_location"): {"storage_location"},
        ("store_order", "mark_order_ready"): {"status"},
    }
    allowed = allowed_fields[(domain, action)]
    unexpected = sorted(set(changes).difference(allowed))
    if unexpected:
        blockers.append("unsupported_store_change_fields")
    if action == "assign_quote_request" and not _nonempty_string(changes.get("assignee_id")):
        blockers.append("missing_store_assignee_id")
    elif action == "set_quote_request_status":
        status = str(changes.get("status") or "").strip().upper()
        if status not in {"NEW", "IN_PROGRESS"}:
            blockers.append("unsupported_store_quote_status")
    elif action == "update_quote_request_comment":
        comment = changes.get("internal_comment")
        if comment is not None and (not isinstance(comment, str) or len(comment) > 2000):
            blockers.append("invalid_store_internal_comment")
    elif action == "add_quote_request_note":
        text = changes.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            blockers.append("invalid_store_quote_note")
    elif action == "replace_quote_offer_drafts":
        items = changes.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            blockers.append("invalid_store_quote_drafts")
        elif not _valid_store_quote_drafts(items):
            blockers.append("invalid_store_quote_drafts")
    elif action == "set_batch_storage_location":
        location = changes.get("storage_location")
        if not isinstance(location, str) or not location.strip() or len(location) > 200:
            blockers.append("invalid_store_storage_location")
    elif action == "mark_order_ready":
        if str(changes.get("status") or "").strip().upper() != "READY":
            blockers.append("store_order_ready_status_required")
        warnings.append("store_order_ready_may_notify_customer")


def _validate_store_owner_api_changes(changes: dict[str, Any], blockers: list[str]) -> None:
    allowed = {
        "operation_id",
        "method",
        "path_template",
        "plan_hash",
        "risk",
        "schema_hash",
        "concrete_path",
        "query_fields",
        "query_sha256",
        "request_sha256",
        "verification_class",
        "body_fields",
        "form_fields",
        "file_fields",
    }
    if set(changes).difference(allowed):
        blockers.append("unsupported_store_change_fields")
    if not _nonempty_string(changes.get("operation_id")):
        blockers.append("missing_store_owner_operation_id")
    if str(changes.get("method") or "").strip().upper() not in {
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
    }:
        blockers.append("invalid_store_owner_method")
    if not _nonempty_string(changes.get("path_template")):
        blockers.append("missing_store_owner_path_template")
    concrete_path = str(changes.get("concrete_path") or "").strip()
    if not _store_owner_concrete_path_matches(
        str(changes.get("path_template") or "").strip(),
        concrete_path,
    ):
        blockers.append("invalid_store_owner_concrete_path")
    if str(changes.get("risk") or "").strip() not in {"write", "high_risk_write"}:
        blockers.append("invalid_store_owner_risk")
    if re.fullmatch(r"[0-9a-f]{64}", str(changes.get("plan_hash") or "")) is None:
        blockers.append("invalid_store_owner_plan_hash")
    schema_hash = str(changes.get("schema_hash") or "").strip()
    if re.fullmatch(r"[0-9a-f]{64}", schema_hash) is None:
        blockers.append("invalid_store_owner_schema_hash")
    for hash_field in ("query_sha256", "request_sha256"):
        if re.fullmatch(r"[0-9a-f]{64}", str(changes.get(hash_field) or "").strip()) is None:
            blockers.append(f"invalid_store_owner_{hash_field}")
    if str(changes.get("verification_class") or "").strip() not in {
        "absence_plus_audit",
        "collection_membership",
        "exact_entity",
        "operation_specific_state",
    }:
        blockers.append("invalid_store_owner_verification_class")
    for field in ("body_fields", "form_fields", "file_fields", "query_fields"):
        if field in changes and not (
            isinstance(changes[field], list) and all(isinstance(item, str) and item for item in changes[field])
        ):
            blockers.append(f"invalid_store_owner_{field}")


def _store_preflight_checks(
    *,
    domain: str,
    action: str,
    revision: str | None,
    owner_collection_create: bool,
) -> list[str]:
    if domain not in STORE_DOMAINS:
        return []
    checks = ["store_target_reread", "store_scope_allowed"]
    if revision:
        checks.append("store_expected_updated_at_matches")
    elif owner_collection_create:
        checks.append("store_reviewed_collection_create_confirmed")
    if action == "mark_order_ready":
        checks.extend(["store_order_current_status_is_in_progress", "notification_effect_disclosed"])
    return checks


def _store_owner_concrete_path_matches(path_template: str, concrete_path: str) -> bool:
    if (
        not path_template.startswith("/api/v1/")
        or not concrete_path.startswith("/api/v1/")
        or len(path_template) > 500
        or len(concrete_path) > 1000
        or any(character in concrete_path for character in ("?", "#", "\\"))
    ):
        return False
    template_segments = path_template.split("/")
    concrete_segments = concrete_path.split("/")
    if len(template_segments) != len(concrete_segments):
        return False
    for template_segment, concrete_segment in zip(template_segments, concrete_segments, strict=True):
        if template_segment.startswith("{") and template_segment.endswith("}"):
            if not concrete_segment or concrete_segment in {".", ".."}:
                return False
        elif template_segment != concrete_segment:
            return False
    return True


def _is_store_owner_collection_create(domain: str, changes: dict[str, Any]) -> bool:
    if domain != "store_owner_api":
        return False
    path = str(changes.get("path_template") or "").strip()
    return (
        is_safe_reversible_collection_create(
            str(changes.get("method") or "").strip(),
            path,
        )
        and str(changes.get("risk") or "").strip() == "write"
    )


def _normalize_store_planned_changes(action: str, changes: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(changes)
    if action == "assign_quote_request" and isinstance(normalized.get("assignee_id"), str):
        normalized["assignee_id"] = normalized["assignee_id"].strip()
    elif action == "set_quote_request_status" and isinstance(normalized.get("status"), str):
        normalized["status"] = normalized["status"].strip().upper()
    elif action == "update_quote_request_comment" and isinstance(normalized.get("internal_comment"), str):
        comment = normalized["internal_comment"].strip()
        normalized["internal_comment"] = comment or None
    elif action == "add_quote_request_note" and isinstance(normalized.get("text"), str):
        normalized["text"] = normalized["text"].strip()
    elif action == "set_batch_storage_location" and isinstance(normalized.get("storage_location"), str):
        normalized["storage_location"] = normalized["storage_location"].strip()
    elif action == "mark_order_ready" and isinstance(normalized.get("status"), str):
        normalized["status"] = normalized["status"].strip().upper()
    return normalized


def _valid_store_quote_drafts(items: list[Any]) -> bool:
    item_ids: set[str] = set()
    allowed_draft_fields = {
        "candidate_key",
        "part_name",
        "part_sku",
        "brand",
        "supplier",
        "purchase_price",
        "sale_price",
        "delivery_days",
        "comment",
        "source_kind",
        "source_ref",
        "source_url",
        "availability",
        "price_basis",
        "fitment_confidence",
        "oem_reference",
        "is_recommended",
    }
    allowed_source_kinds = {"LOCAL", "ROSSKO", "CATALOG", "WEB", "MANUAL_REFERENCE"}
    allowed_price_basis = {"STORE_RETAIL", "CONFIRMED_PURCHASE", "PUBLIC_RETAIL", "ESTIMATE"}
    allowed_confidence = {"HIGH", "MEDIUM", "LOW", "UNVERIFIED"}
    for item in items:
        if not isinstance(item, dict) or set(item) != {"item_id", "drafts"}:
            return False
        item_id = str(item.get("item_id") or "").strip()
        drafts = item.get("drafts")
        if not item_id or len(item_id) > 36 or item_id in item_ids or not isinstance(drafts, list) or len(drafts) > 3:
            return False
        item_ids.add(item_id)
        candidate_keys: set[str] = set()
        recommended = 0
        for draft in drafts:
            if not isinstance(draft, dict) or set(draft).difference(allowed_draft_fields):
                return False
            candidate_key = str(draft.get("candidate_key") or "").strip()
            part_name = str(draft.get("part_name") or "").strip()
            if (
                not candidate_key
                or len(candidate_key) > 160
                or candidate_key in candidate_keys
                or not part_name
                or len(part_name) > 300
            ):
                return False
            candidate_keys.add(candidate_key)
            if (
                draft.get("source_kind") not in allowed_source_kinds
                or draft.get("price_basis") not in allowed_price_basis
            ):
                return False
            if draft.get("fitment_confidence", "UNVERIFIED") not in allowed_confidence:
                return False
            sale_price = draft.get("sale_price")
            purchase_price = draft.get("purchase_price")
            if isinstance(sale_price, bool) or not isinstance(sale_price, (int, float)) or sale_price <= 0:
                return False
            if purchase_price is not None and (
                isinstance(purchase_price, bool) or not isinstance(purchase_price, (int, float)) or purchase_price <= 0
            ):
                return False
            if draft.get("is_recommended") is True:
                recommended += 1
        if recommended > 1:
            return False
    return True


def _validate_financial_changes(
    domain: str,
    action: str,
    changes: dict[str, Any],
    blockers: list[str],
    warnings: list[str],
) -> None:
    numeric_amount: float | int | None
    raw_amount_minor = changes.get("amount_minor") if action == "cash_transaction" else None
    if action == "cash_transaction" and raw_amount_minor not in (None, ""):
        numeric_amount = (
            raw_amount_minor if isinstance(raw_amount_minor, int) and not isinstance(raw_amount_minor, bool) else None
        )
        if numeric_amount is None or not 0 < numeric_amount <= MAX_MONEY_MINOR:
            blockers.append("invalid_positive_amount_minor")
    else:
        numeric_amount = _finite_number(changes.get("amount"))
        if numeric_amount is None or not 0 < numeric_amount <= MAX_MONEY_AMOUNT:
            blockers.append("invalid_positive_amount")
    if not str(changes.get("cashbox_id") or "").strip():
        blockers.append("missing_cashbox_id")
    if action == "cash_transaction":
        direction = str(changes.get("direction") or "")
        if direction not in {"income", "expense"}:
            blockers.append("invalid_cash_transaction_direction")
        if direction == "expense" and len(str(changes.get("note") or "").strip()) < 10:
            blockers.append("expense_note_too_short")
    if action == "transfer":
        source_cashbox_id = str(changes.get("cashbox_id") or "").strip()
        target_cashbox_id = str(changes.get("target_cashbox_id") or "").strip()
        if not target_cashbox_id:
            blockers.append("missing_target_cashbox_id")
        elif source_cashbox_id == target_cashbox_id:
            blockers.append("cashbox_transfer_target_must_differ")
    if domain == "payment" or action == "record_payment":
        if not str(changes.get("card_id") or "").strip():
            blockers.append("missing_card_id")
        if not str(changes.get("payment_method") or "").strip():
            blockers.append("missing_payment_method")
        numeric_outstanding = _finite_number(changes.get("outstanding_amount"))
        if numeric_outstanding is None or numeric_outstanding < 0:
            blockers.append("missing_outstanding_amount")
        elif (
            numeric_amount is not None
            and numeric_amount > numeric_outstanding
            and changes.get("allow_overpayment") is not True
        ):
            blockers.append("overpayment_not_explicitly_allowed")
        elif numeric_amount is not None and numeric_amount > numeric_outstanding:
            warnings.append("explicit_overpayment")


def _validate_inventory_changes(changes: dict[str, Any], blockers: list[str]) -> None:
    movement_type = str(changes.get("movement_type") or "").strip().casefold().replace("-", "_")
    if movement_type not in INVENTORY_EXECUTOR_TOOLS:
        blockers.append("unsupported_inventory_movement_type")
        return
    if movement_type in {"replenish", "write_off"}:
        quantity = _finite_number(changes.get("quantity"))
        if quantity is None or quantity <= 0:
            blockers.append("invalid_positive_quantity")
    if movement_type == "write_off" and not str(changes.get("card_id") or "").strip():
        blockers.append("missing_card_id")


def _validate_card_changes(action: str, changes: dict[str, Any], blockers: list[str]) -> None:
    if action == "move" and not str(changes.get("column") or "").strip():
        blockers.append("missing_target_column_id")
    if action == "set_deadline":
        _validate_deadline(changes.get("deadline"), blockers)


def _validate_board_changes(action: str, changes: dict[str, Any], blockers: list[str]) -> None:
    if action != "bulk_set_deadline_if_below":
        return
    if changes.get("include_archived") is not False:
        blockers.append("active_cards_only_required")
    minimum = changes.get("min_total_seconds")
    target = changes.get("target_total_seconds")
    if (
        isinstance(minimum, bool)
        or not isinstance(minimum, int)
        or not 0 < minimum <= DEADLINE_PART_MAXIMUMS["total_seconds"]
    ):
        blockers.append("invalid_min_total_seconds")
    if (
        isinstance(target, bool)
        or not isinstance(target, int)
        or not 0 < target <= DEADLINE_PART_MAXIMUMS["total_seconds"]
    ):
        blockers.append("invalid_target_total_seconds")
    elif isinstance(minimum, int) and not isinstance(minimum, bool) and target <= minimum:
        blockers.append("target_total_seconds_must_exceed_minimum")


def _validate_deadline(deadline: Any, blockers: list[str]) -> None:
    if not isinstance(deadline, dict) or not deadline:
        blockers.append("missing_deadline")
        return
    if any(field not in DEADLINE_PART_MAXIMUMS for field in deadline):
        blockers.append("unsupported_deadline_field")
    total_seconds = 0
    parts_valid = True
    for field, maximum in DEADLINE_PART_MAXIMUMS.items():
        value = deadline.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            parts_valid = False
            continue
        if field == "days":
            total_seconds += value * 24 * 3600
        elif field == "hours":
            total_seconds += value * 3600
        elif field == "minutes":
            total_seconds += value * 60
        else:
            total_seconds += value
    if not parts_valid:
        blockers.append("invalid_deadline_part")
    elif total_seconds <= 0:
        blockers.append("invalid_positive_deadline")


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value or any(character.isspace() and character != " " for character in value):
            return None
        if " " in value:
            if re.fullmatch(r"[+-]?\d{1,3}(?: \d{3})+(?:[.,]\d+)?", value) is None:
                return None
            value = value.replace(" ", "")
        elif re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", value) is None:
            return None
        value = value.replace(",", ".")
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _validate_gmail_changes(action: str, changes: dict[str, Any], blockers: list[str]) -> None:
    if action in {"send", "create_draft"}:
        _validate_gmail_compose(changes, blockers)
    elif action == "forward":
        _validate_gmail_forward(changes, blockers)
    elif action == "label":
        _validate_gmail_label(changes, blockers)
    elif action in {"archive", "delete"}:
        if not _nonempty_string_list(changes.get("message_ids")):
            blockers.append("missing_exact_message_ids")
    elif action == "batch_modify":
        _validate_gmail_batch_modify(changes, blockers)
    elif action == "bulk_label":
        _validate_gmail_bulk_label(changes, blockers)
    elif action == "create_label":
        if not _nonempty_string(changes.get("name")):
            blockers.append("missing_label_name")
    elif action == "update_draft":
        _validate_gmail_update_draft(changes, blockers)
    elif action == "send_draft" and not _nonempty_string(changes.get("draft_id")):
        blockers.append("missing_exact_draft_id")


def _normalize_action(domain: str, action: str) -> str:
    normalized = str(action or "").strip().casefold()
    return GMAIL_ACTION_ALIASES.get(normalized, normalized) if domain == "gmail" else normalized


def _validate_gmail_compose(changes: dict[str, Any], blockers: list[str]) -> None:
    if not _gmail_recipients_are_exact(changes):
        blockers.append("missing_exact_recipients")
    if not _nonempty_string(changes.get("subject")):
        blockers.append("missing_subject")
    if not any(_nonempty_string(changes.get(field)) for field in ("body_intent", "body", "body_file", "html_body")):
        blockers.append("missing_body_intent")


def _validate_gmail_forward(changes: dict[str, Any], blockers: list[str]) -> None:
    if not (
        _nonempty_string_list(changes.get("message_ids"))
        or _nonempty_string(changes.get("message_id"))
        or _nonempty_string(changes.get("thread_id"))
    ):
        blockers.append("missing_message_or_thread_id")
    if not _gmail_recipients_are_exact(changes):
        blockers.append("missing_exact_recipients")


def _validate_gmail_label(changes: dict[str, Any], blockers: list[str]) -> None:
    message_ids_valid = _nonempty_string_list(changes.get("message_ids"))
    label_names_valid = _nonempty_string_list(changes.get("add_label_names")) or _nonempty_string_list(
        changes.get("remove_label_names")
    )
    if not message_ids_valid or not label_names_valid:
        blockers.append("missing_message_or_label_ids")
    if "create_missing_labels" in changes and not isinstance(changes["create_missing_labels"], bool):
        blockers.append("invalid_create_missing_labels_flag")


def _validate_gmail_batch_modify(changes: dict[str, Any], blockers: list[str]) -> None:
    if not _nonempty_string_list(changes.get("message_ids")):
        blockers.append("missing_exact_message_ids")
    if not (_nonempty_string_list(changes.get("add_labels")) or _nonempty_string_list(changes.get("remove_labels"))):
        blockers.append("missing_label_ids")


def _validate_gmail_bulk_label(changes: dict[str, Any], blockers: list[str]) -> None:
    if not _nonempty_string(changes.get("query")):
        blockers.append("missing_exact_gmail_query")
    if not _nonempty_string(changes.get("label_name")):
        blockers.append("missing_label_name")
    for field in ("archive", "create_label_if_missing"):
        if field in changes and not isinstance(changes[field], bool):
            blockers.append(f"invalid_{field}_flag")


def _validate_gmail_update_draft(changes: dict[str, Any], blockers: list[str]) -> None:
    if not _nonempty_string(changes.get("draft_id")):
        blockers.append("missing_exact_draft_id")
    mutable_fields = {"to", "cc", "bcc", "subject", "body", "body_file", "html_body", "content_type"}
    if not any(field in changes and changes.get(field) is not None for field in mutable_fields):
        blockers.append("missing_draft_changes")


def _gmail_recipients_are_exact(changes: dict[str, Any]) -> bool:
    return _nonempty_string_list(changes.get("recipients")) or _nonempty_string(changes.get("to"))


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _nonempty_string_list(value: Any) -> bool:
    return (
        isinstance(value, list) and bool(value) and all(isinstance(item, str) and bool(item.strip()) for item in value)
    )


def _verification_checks(
    domain: str,
    action: str,
    *,
    dry_run: bool,
    changes: dict[str, Any] | None = None,
) -> list[str]:
    checks = ["write_response_ok", "target_reread", "planned_diff_exact", "no_unplanned_fields"]
    if domain == "store_owner_api":
        owner_changes = changes or {}
        verification_class = str(owner_changes.get("verification_class") or "")
        class_checks = {
            "absence_plus_audit": ["store_exact_absence_confirmed", "store_audit_correlation_present"],
            "collection_membership": ["store_created_entity_or_collection_membership_reread"],
            "exact_entity": ["store_exact_entity_reread"],
            "operation_specific_state": ["store_operation_specific_state_reread"],
        }.get(verification_class, ["store_operation_specific_state_reread"])
        if dry_run:
            checks = [
                "dry_run_response_ok",
                "store_openapi_schema_validated",
                "store_server_revision_matched",
                "store_request_fingerprint_bound",
                "store_server_dry_run_receipt_recorded",
                "store_business_state_unchanged",
                *class_checks,
                "no_unplanned_fields",
            ]
        else:
            checks = [
                "write_response_ok",
                "store_response_schema_validated",
                "store_server_revision_matched",
                "store_idempotency_receipt_replayed_or_recorded",
                *class_checks,
                "store_audit_correlation_present",
                "store_result_compensating_until_verified",
                "no_unplanned_fields",
            ]
        return list(dict.fromkeys(checks))
    if domain == "board" and action == "bulk_set_deadline_if_below":
        return [
            "write_response_ok",
            "active_board_reread",
            "no_active_cards_below_minimum",
            "archived_cards_unchanged",
            "no_unplanned_fields",
        ]
    if (domain, action) in FINANCIAL_TRANSACTION_ACTIONS:
        checks.extend(["cash_journal_entry_exists", "repair_order_balance_reconciled", "amount_exact"])
    if domain == "gmail":
        checks = ["connector_result_ref_present", "message_or_thread_id_present", "external_step_completed_once"]
    if domain == "document":
        checks.extend(["file_exists", "render_gate_passed", "totals_match"])
    if domain in STORE_DOMAINS:
        if dry_run:
            checks = [
                "dry_run_response_ok",
                "store_target_reread",
                "store_revision_unchanged",
                "store_dry_run_receipt_recorded",
                "store_business_state_unchanged",
                "store_planned_diff_exact",
                "no_unplanned_fields",
            ]
        else:
            checks.extend(
                [
                    "store_target_reread",
                    "store_updated_at_advanced_or_idempotent_replay",
                    "store_planned_state_exact_or_idempotent_replay",
                    "store_audit_correlation_present",
                ]
            )
        if action == "mark_order_ready":
            checks.extend(
                [
                    "store_order_status_ready" if not dry_run else "store_order_status_unchanged",
                    "notification_effect_matches_dry_run" if not dry_run else "notification_effect_disclosed",
                ]
            )
    if action in DESTRUCTIVE_ACTIONS:
        checks.append("backup_or_compensation_ref_present")
    return checks


def _executor_tool(domain: str, action: str, changes: dict[str, Any]) -> str | None:
    if domain == "inventory" and action == "adjust":
        movement_type = str(changes.get("movement_type") or "").strip().casefold().replace("-", "_")
        return INVENTORY_EXECUTOR_TOOLS.get(movement_type)
    return EXECUTOR_TOOLS.get((domain, action))


def _compensation_strategy(domain: str, action: str) -> str | None:
    if domain == "gmail" and action == "archive":
        return "restore_inbox_label_on_exact_messages"
    if domain == "gmail" and action == "delete":
        return "restore_exact_messages_from_trash"
    if domain in STORE_DOMAINS:
        return "store_exact_target_reconciliation_preserve_audit_history"
    if domain == "cashbox" and action == "create":
        return "delete_empty_created_cashbox"
    if domain == "cashbox" and action == "delete":
        return "restore_empty_cashbox_from_verified_snapshot"
    if domain in FINANCIAL_DOMAINS:
        return "compensating_transaction_never_history_delete"
    if action == "delete":
        return "restore_from_verified_backup_or_soft_delete"
    if action == "merge":
        return "preserve_source_refs_and_restore_linkage"
    if action == "archive":
        return "unarchive_exact_target"
    return None


def _contract_id(
    domain: str,
    action: str,
    target_id: str,
    changes: dict[str, Any],
    idempotency_key: str,
    revision: str | None,
) -> str:
    canonical = json.dumps(
        {
            "domain": domain,
            "action": action,
            "target_id": target_id,
            "changes": changes,
            "idempotency_key": idempotency_key,
            "revision": revision,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"ac_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:20]}"


def _store_correlation_id(
    domain: str,
    action: str,
    target_id: str,
    changes: dict[str, Any],
    revision: str | None,
) -> str:
    """Return one phase-independent correlation for preview/apply reconciliation."""

    canonical = json.dumps(
        {
            "domain": domain,
            "action": action,
            "target_id": target_id,
            "changes": changes,
            "revision": revision,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"store_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]}"


def _store_correlation_blockers(domain: str, requested: str) -> list[str]:
    if domain not in STORE_DOMAINS or not requested or STORE_CORRELATION_ID_RE.fullmatch(requested) is not None:
        return []
    return ["invalid_store_correlation_id"]


def _action_correlation_id(
    *,
    domain: str,
    action: str,
    target_id: str,
    changes: dict[str, Any],
    revision: str | None,
    requested: str,
    contract_id: str,
) -> str:
    if domain not in STORE_DOMAINS:
        return requested or contract_id
    if requested and STORE_CORRELATION_ID_RE.fullmatch(requested) is not None:
        return requested
    return _store_correlation_id(domain, action, target_id, changes, revision)
