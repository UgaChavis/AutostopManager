from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any


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
}

CREATE_ACTIONS = {"create", "record_payment", "cash_transaction", "transfer", "upload", "generate", "send", "forward"}
COLLECTION_TARGET_ACTIONS = {
    ("gmail", "label"),
    ("board", "bulk_set_deadline_if_below"),
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
    exact_target_id_required = (
        normalized_action not in CREATE_ACTIONS
        and (normalized_domain, normalized_action) not in COLLECTION_TARGET_ACTIONS
    )
    concurrency_required = exact_target_id_required or normalized_domain in FINANCIAL_DOMAINS

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
    verification_checks = _verification_checks(normalized_domain, normalized_action)
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
    if action == "send":
        if not _gmail_recipients_are_exact(changes):
            blockers.append("missing_exact_recipients")
        if not _nonempty_string(changes.get("subject")):
            blockers.append("missing_subject")
        if not _nonempty_string(changes.get("body_intent")):
            blockers.append("missing_body_intent")
    elif action == "forward":
        if not (
            _nonempty_string_list(changes.get("message_ids"))
            or _nonempty_string(changes.get("message_id"))
            or _nonempty_string(changes.get("thread_id"))
        ):
            blockers.append("missing_message_or_thread_id")
        if not _gmail_recipients_are_exact(changes):
            blockers.append("missing_exact_recipients")
    elif action == "label":
        message_ids_valid = _nonempty_string_list(changes.get("message_ids"))
        label_names_valid = _nonempty_string_list(changes.get("add_label_names")) or _nonempty_string_list(
            changes.get("remove_label_names")
        )
        if not message_ids_valid or not label_names_valid:
            blockers.append("missing_message_or_label_ids")
        if "create_missing_labels" in changes and not isinstance(changes["create_missing_labels"], bool):
            blockers.append("invalid_create_missing_labels_flag")


def _gmail_recipients_are_exact(changes: dict[str, Any]) -> bool:
    return _nonempty_string_list(changes.get("recipients")) or _nonempty_string(changes.get("to"))


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _nonempty_string_list(value: Any) -> bool:
    return (
        isinstance(value, list) and bool(value) and all(isinstance(item, str) and bool(item.strip()) for item in value)
    )


def _verification_checks(domain: str, action: str) -> list[str]:
    checks = ["write_response_ok", "target_reread", "planned_diff_exact", "no_unplanned_fields"]
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
    if action in DESTRUCTIVE_ACTIONS:
        checks.append("backup_or_compensation_ref_present")
    return checks


def _executor_tool(domain: str, action: str, changes: dict[str, Any]) -> str | None:
    if domain == "inventory" and action == "adjust":
        movement_type = str(changes.get("movement_type") or "").strip().casefold().replace("-", "_")
        return INVENTORY_EXECUTOR_TOOLS.get(movement_type)
    return EXECUTOR_TOOLS.get((domain, action))


def _compensation_strategy(domain: str, action: str) -> str | None:
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
