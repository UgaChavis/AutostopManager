from __future__ import annotations

import hashlib
import json
import math
from typing import Any


MUTATING_ACTIONS = {
    "create",
    "update",
    "move",
    "archive",
    "delete",
    "merge",
    "set_deadline",
    "record_payment",
    "cash_transaction",
    "transfer",
    "adjust",
    "upload",
    "generate",
    "send",
    "forward",
    "label",
}

CREATE_ACTIONS = {"create", "record_payment", "cash_transaction", "transfer", "upload", "generate", "send", "forward"}

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
}

EXECUTOR_TOOLS = {
    ("card", "update"): "update_card",
    ("card", "move"): "move_card",
    ("card", "archive"): "archive_card",
    ("card", "set_deadline"): "set_card_deadline",
    ("client", "create"): "create_client",
    ("client", "update"): "update_client",
    ("vehicle", "create"): "upsert_client_vehicle",
    ("vehicle", "update"): "upsert_client_vehicle",
    ("repair_order", "update"): "update_repair_order",
    ("payment", "record_payment"): "agent_finance_workflow",
    ("cashbox", "cash_transaction"): "create_cash_transaction",
    ("document", "generate"): "create_document_without_card_pdf",
    ("file", "upload"): "upload_shared_file",
    ("gmail", "send"): "gmail:_send_email",
    ("gmail", "forward"): "gmail:_forward_emails",
    ("gmail", "label"): "gmail:_apply_labels_to_emails",
}

INVENTORY_EXECUTOR_TOOLS = {
    "replenish": "replenish_inventory_item",
    "write_off": "write_off_inventory_item",
    "return": "return_inventory_movement",
}

FINANCIAL_DOMAINS = {"payment", "cashbox"}
DESTRUCTIVE_ACTIONS = {"delete", "merge", "archive"}
EXTERNAL_DOMAINS = {"gmail"}


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def prepare_action_contract(
    *,
    domain: str,
    action: str,
    target_id: str = "",
    planned_changes: dict[str, Any] | None = None,
    owner_intent: str = "",
    expected_revision: str | None = None,
    idempotency_key: str = "",
    run_id: int | None = None,
    actor: str = "codex-owner-agent",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Build a write-safe, connector-neutral action contract without executing it."""

    normalized_domain = DOMAIN_ALIASES.get(str(domain or "").strip().casefold(), str(domain or "").strip().casefold())
    normalized_action = str(action or "").strip().casefold()
    normalized_target = str(target_id or "").strip()
    changes = dict(planned_changes) if isinstance(planned_changes, dict) else {}
    intent = str(owner_intent or "").strip()
    key = str(idempotency_key or "").strip()
    revision = str(expected_revision or "").strip() or None
    concurrency_required = normalized_action not in CREATE_ACTIONS or normalized_domain in FINANCIAL_DOMAINS

    blockers: list[str] = []
    warnings: list[str] = []
    if not normalized_domain:
        blockers.append("missing_domain")
    if not normalized_action:
        blockers.append("missing_action")
    if normalized_action not in MUTATING_ACTIONS:
        blockers.append("unsupported_mutating_action")
    if normalized_action not in CREATE_ACTIONS and not normalized_target:
        blockers.append("missing_exact_target_id")
    if not intent:
        blockers.append("missing_task_specific_owner_intent")
    if not key:
        blockers.append("missing_idempotency_key")
    if concurrency_required and not revision:
        blockers.append("missing_expected_revision")
    if not changes:
        blockers.append("missing_planned_changes")

    if normalized_domain in FINANCIAL_DOMAINS:
        _validate_financial_changes(normalized_domain, normalized_action, changes, blockers, warnings)
    if normalized_domain == "gmail":
        _validate_gmail_changes(normalized_action, changes, blockers)
    if normalized_domain == "document" and not str(changes.get("document_type") or "").strip():
        blockers.append("missing_document_type")
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
    verification_checks = _verification_checks(normalized_domain, normalized_action)
    preflight_checks = [
        "exact_target_resolved",
        "task_specific_owner_intent_present",
        "idempotency_key_unused_or_same_contract",
        "current_state_read",
    ]
    if revision:
        preflight_checks.append("expected_revision_matches")
    if normalized_domain in FINANCIAL_DOMAINS:
        preflight_checks.extend(["cashbox_exists", "amount_and_payment_method_valid", "debt_reconciled"])
    if normalized_domain == "gmail":
        preflight_checks.extend(["thread_or_recipients_reread", "active_connector_schema_checked"])

    return {
        "ok": not blockers,
        "format": "action_contract_v2",
        "contract_id": contract_id,
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
            "operation": (
                "record_repair_order_payment"
                if normalized_domain == "payment" and normalized_action == "record_payment"
                else None
            ),
            "gateway_arguments": (
                {
                    "operation": "record_repair_order_payment",
                    "payload": {**changes, "expected_updated_at": revision},
                    "idempotency_key": key or None,
                }
                if normalized_domain == "payment" and normalized_action == "record_payment"
                else None
            ),
            "external_connector": "gmail" if normalized_domain == "gmail" else None,
            "response_mode": "compact",
        },
        "verification": {
            "checks": verification_checks,
            "requires_readback": True,
            "record_correlation_id": True,
        },
        "compensation": {
            "required": normalized_domain in FINANCIAL_DOMAINS or normalized_action in DESTRUCTIVE_ACTIONS,
            "strategy": _compensation_strategy(normalized_domain, normalized_action),
        },
        "ledger": {
            "events": ["planned_action", "preflight", "write", "verification"],
            "store_payload": False,
            "store_refs_only": normalized_domain in EXTERNAL_DOMAINS,
        },
        "warnings": list(dict.fromkeys(warnings)),
    }


def _validate_financial_changes(
    domain: str,
    action: str,
    changes: dict[str, Any],
    blockers: list[str],
    warnings: list[str],
) -> None:
    numeric_amount = _finite_number(changes.get("amount"))
    if numeric_amount is None or numeric_amount <= 0:
        blockers.append("invalid_positive_amount")
    if not str(changes.get("cashbox_id") or "").strip():
        blockers.append("missing_cashbox_id")
    if action == "transfer" and not str(changes.get("target_cashbox_id") or "").strip():
        blockers.append("missing_target_cashbox_id")
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
            and not bool(changes.get("allow_overpayment"))
        ):
            blockers.append("overpayment_not_explicitly_allowed")
        elif numeric_amount is not None and numeric_amount > numeric_outstanding:
            warnings.append("explicit_overpayment")


def _validate_gmail_changes(action: str, changes: dict[str, Any], blockers: list[str]) -> None:
    if action == "send":
        recipients = changes.get("recipients")
        if not isinstance(recipients, list) or not any(str(item or "").strip() for item in recipients):
            blockers.append("missing_exact_recipients")
        if not str(changes.get("subject") or "").strip():
            blockers.append("missing_subject")
        if not str(changes.get("body_intent") or "").strip():
            blockers.append("missing_body_intent")
    elif action == "forward":
        if not str(changes.get("message_id") or changes.get("thread_id") or "").strip():
            blockers.append("missing_message_or_thread_id")
        recipients = changes.get("recipients")
        if not isinstance(recipients, list) or not any(str(item or "").strip() for item in recipients):
            blockers.append("missing_exact_recipients")
    elif action == "label":
        if not changes.get("message_ids") or not changes.get("label_ids"):
            blockers.append("missing_message_or_label_ids")


def _verification_checks(domain: str, action: str) -> list[str]:
    checks = ["write_response_ok", "target_reread", "planned_diff_exact", "no_unplanned_fields"]
    if domain in FINANCIAL_DOMAINS:
        checks.extend(["cash_journal_entry_exists", "repair_order_balance_reconciled", "amount_exact"])
    if domain == "gmail":
        checks = ["connector_result_ref_present", "message_or_thread_id_present", "external_step_completed_once"]
    if domain == "document":
        checks.extend(["file_exists", "render_gate_passed", "totals_match"])
    if action in DESTRUCTIVE_ACTIONS:
        checks.append("backup_or_compensation_ref_present")
    return checks


def _executor_tool(domain: str, action: str, changes: dict[str, Any]) -> str | None:
    if domain == "inventory" and action == "adjust":
        movement_type = str(changes.get("movement_type") or "").strip().casefold().replace("-", "_")
        return INVENTORY_EXECUTOR_TOOLS.get(movement_type)
    return EXECUTOR_TOOLS.get((domain, action))


def _compensation_strategy(domain: str, action: str) -> str | None:
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
