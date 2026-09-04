from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

from autostop_manager.storage import (
    STORE_QUOTE_CONDUCTOR_LEDGER_INTENT,
    STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION,
    STORE_QUOTE_CONDUCTOR_LEDGER_WORKFLOW_ID,
    ManagerMemoryStore,
)


def test_v2_workflow_is_idempotent_resumable_and_keeps_external_steps_refs_only(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    started = store.start_workflow_run(
        workflow_id="crm_gmail_workflow",
        intent="crm_gmail_workflow",
        query="ответь клиенту по карточке CRM",
        idempotency_key="crm-gmail-c1-v1",
        scope={"card_id": "C-1"},
        selected_ids=["C-1"],
    )
    duplicate = store.start_workflow_run(
        workflow_id="crm_gmail_workflow",
        intent="crm_gmail_workflow",
        query="ответь клиенту по карточке CRM",
        idempotency_key="crm-gmail-c1-v1",
    )
    assert duplicate["id"] == started["id"]
    assert duplicate["deduplicated"] is True

    assert store.transition_workflow_run(started["id"], status="executing")["ok"] is True
    checkpoint = store.checkpoint_workflow_run(
        started["id"],
        checkpoint={"phase": "crm_verified", "next_action": "send Gmail reply"},
    )
    assert checkpoint["ok"] is True

    rejected = store.register_external_step(
        started["id"],
        step_id="gmail-send-1",
        connector="gmail",
        action="send",
        request_refs={"thread_id": "thread-1", "body": "must never persist"},
    )
    assert rejected["ok"] is False
    assert rejected["error"] == "raw_external_body_not_allowed_in_manager_ledger"

    waiting = store.register_external_step(
        started["id"],
        step_id="gmail-send-1",
        connector="gmail",
        action="send",
        request_refs={"thread_id": "thread-1", "recipient_count": 1},
    )
    assert waiting["workflow_status"] == "external_wait"
    assert store.resume_workflow_run(started["id"])["error"] == "external_steps_pending"

    completed = store.complete_external_step(
        started["id"],
        step_id="gmail-send-1",
        result_refs={"message_id": "message-9", "thread_id": "thread-1", "status": "sent"},
    )
    assert completed["ok"] is True
    assert (
        store.complete_external_step(
            started["id"],
            step_id="gmail-send-1",
            result_refs={"message_id": "message-9", "thread_id": "thread-1", "status": "sent"},
        )["deduplicated"]
        is True
    )
    assert store.resume_workflow_run(started["id"])["status"] == "executing"
    assert store.transition_workflow_run(started["id"], status="verifying")["ok"] is True
    assert (
        store.transition_workflow_run(
            started["id"],
            status="completed",
            verification={"crm_readback": True, "gmail_result_ref": True},
        )["ok"]
        is True
    )

    raw_db = (tmp_path / "memory.sqlite3").read_bytes()
    assert b"must never persist" not in raw_db
    status = store.get_manager_run(started["id"], include_events=True, include_external_steps=True)
    assert status["item"]["status"] == "completed"
    assert status["item"]["external_steps"][0]["result_refs"]["message_id"] == "message-9"


def test_store_quote_conductor_uses_one_active_target_and_refs_only_telegram_steps(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    target_hash = "a" * 64
    revision_hash = "b" * 64
    scope = {
        "operation": STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION,
        "workflow_id": STORE_QUOTE_CONDUCTOR_LEDGER_WORKFLOW_ID,
        "domain": "store",
        "source": "store_quote_conductor",
        "correlation_id": "quote-conductor-correlation-001",
        "target_entity": "store_quote_request",
        "target_ref_sha256": target_hash,
        "expected_revision_sha256": revision_hash,
    }
    blocked_start = store.start_workflow_run(
        workflow_id=STORE_QUOTE_CONDUCTOR_LEDGER_WORKFLOW_ID,
        intent=STORE_QUOTE_CONDUCTOR_LEDGER_INTENT,
        idempotency_key="quote-conductor-start-001",
        correlation_id="quote-conductor-correlation-001",
        scope=scope,
        active_target_ref_sha256=target_hash,
    )
    assert blocked_start["error"] == "store_quote_conductor_ledger_owned_by_named_workflow"

    started = store.start_store_quote_conductor_run(
        idempotency_key="quote-conductor-start-001",
        correlation_id="quote-conductor-correlation-001",
        scope=scope,
        active_target_ref_sha256=target_hash,
    )
    assert started["ok"] is True

    deduplicated = store.start_store_quote_conductor_run(
        idempotency_key="quote-conductor-start-002",
        correlation_id="quote-conductor-correlation-002",
        scope={**scope, "correlation_id": "quote-conductor-correlation-002"},
        active_target_ref_sha256=target_hash,
    )
    assert deduplicated["id"] == started["id"]
    assert deduplicated["active_target_deduplicated"] is True

    assert (
        store.transition_workflow_run(
            started["id"],
            status="executing",
            message="execute store_quote_conductor",
            expected_state_version=started["state_version"],
        )["error"]
        == "store_quote_conductor_ledger_owned_by_named_workflow"
    )
    executing = store.transition_store_quote_conductor_run(
        started["id"],
        status="executing",
        message="execute store_quote_conductor",
        expected_state_version=started["state_version"],
    )
    assert executing["ok"] is True
    request_refs = {
        "expected_revision_sha256": revision_hash,
        "quote_snapshot_hash": "c" * 64,
        "telegram_context_hash": "d" * 64,
        "message_sha256": "e" * 64,
        "delivery_binding_sha256": "f" * 64,
        "route_binding_sha256": "6" * 64,
        "delivery_ref_sha256": "1" * 64,
    }
    assert (
        store.register_external_step(
            started["id"],
            step_id="telegram-quote-response-001",
            connector="telegram",
            action="quote_response",
            request_refs=request_refs,
            expected_state_version=executing["state_version"],
        )["error"]
        == "store_quote_conductor_ledger_owned_by_named_workflow"
    )
    waiting = store.register_store_quote_conductor_external_step(
        started["id"],
        step_id="telegram-quote-response-001",
        connector="telegram",
        action="quote_response",
        request_refs=request_refs,
        expected_state_version=executing["state_version"],
    )
    assert waiting["ok"] is True
    result_refs = {
        "status": "consent",
        "quote_snapshot_hash": "c" * 64,
        "telegram_context_hash": "d" * 64,
        "delivery_binding_sha256": "f" * 64,
        "reply_text_sha256": "2" * 64,
        "incoming_ref_sha256": "3" * 64,
        "inbound_binding_sha256": "4" * 64,
        "consent_context_hash": "5" * 64,
    }
    assert (
        store.complete_external_step(
            started["id"],
            step_id="telegram-quote-response-001",
            result_refs=result_refs,
            expected_state_version=waiting["state_version"],
        )["error"]
        == "store_quote_conductor_ledger_owned_by_named_workflow"
    )
    complete = store.complete_store_quote_conductor_external_step(
        started["id"],
        step_id="telegram-quote-response-001",
        result_refs=result_refs,
        expected_state_version=waiting["state_version"],
    )
    assert complete["ok"] is True

    assert (
        store.resume_workflow_run(started["id"], expected_state_version=complete["state_version"])["error"]
        == "store_quote_conductor_ledger_owned_by_named_workflow"
    )
    valid_checkpoint = {
        "operation": STORE_QUOTE_CONDUCTOR_LEDGER_OPERATION,
        "phase": "waiting_client",
        "expected_revision_sha256": revision_hash,
        "target_ref_sha256": target_hash,
        "entries_hash": "e" * 64,
    }
    assert (
        store.checkpoint_workflow_run(
            started["id"],
            checkpoint=valid_checkpoint,
            message="verify store_quote_conductor",
            expected_state_version=complete["state_version"],
        )["error"]
        == "store_quote_conductor_ledger_owned_by_named_workflow"
    )
    raw = store.checkpoint_store_quote_conductor_run(
        started["id"],
        checkpoint={
            **valid_checkpoint,
            "counts": {"customer_price": 1300},
        },
        message="verify store_quote_conductor",
        expected_state_version=complete["state_version"],
    )
    assert raw["ok"] is False
    assert raw["error"] == "store_quote_conductor_ledger_schema_invalid"
    peer = store.checkpoint_store_quote_conductor_run(
        started["id"],
        checkpoint={**valid_checkpoint, "telegram_peer_ref_sha256": "f" * 64},
        message="verify store_quote_conductor",
        expected_state_version=complete["state_version"],
    )
    assert peer["ok"] is False
    assert peer["error"] == "store_quote_conductor_ledger_schema_invalid"


def test_store_workflow_ledger_accepts_compact_refs_and_rejects_raw_business_payload(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    store = ManagerMemoryStore(db_path)
    rejected = store.start_workflow_run(
        workflow_id="store_management_workflow",
        intent="store_management",
        idempotency_key="store-raw-start-v1",
        scope={"order": {"id": "order-1", "customer": {"phone": "+79990000000"}}},
    )
    assert rejected["error"] == "raw_store_payload_not_allowed_in_manager_ledger"
    direct_private = store.start_workflow_run(
        workflow_id="store_management_workflow",
        intent="store_management",
        idempotency_key="store-direct-private-start-v1",
        scope={"domain": "store_order", "target_id": "order-1", "customer_phone": "+79990000000"},
    )
    raw_changes = store.start_workflow_run(
        workflow_id="store_management_workflow",
        intent="store_management",
        idempotency_key="store-raw-changes-start-v1",
        scope={
            "domain": "store_order",
            "target_id": "order-1",
            "planned_changes": {"status": "READY"},
        },
    )
    assert direct_private["error"] == "raw_store_payload_not_allowed_in_manager_ledger"
    assert raw_changes["error"] == "raw_store_payload_not_allowed_in_manager_ledger"

    started = store.start_workflow_run(
        workflow_id="store_management_workflow",
        intent="store_management",
        idempotency_key="store-compact-v1",
        scope={"domain": "store_order", "target_id": "order-1", "operation": "mark_order_ready"},
        selected_ids=["order-1"],
    )
    assert started["ok"] is True

    raw_checkpoint = store.checkpoint_workflow_run(
        started["id"],
        checkpoint={"phase": "read", "items": [{"customer_phone": "+79990000000"}]},
    )
    assert raw_checkpoint["error"] == "raw_store_payload_not_allowed_in_manager_ledger"
    nested_raw_checkpoint = store.checkpoint_workflow_run(
        started["id"],
        checkpoint={"phase": "read", "verification": {"customer_phone": "+79990000000"}},
    )
    assert nested_raw_checkpoint["error"] == "raw_store_payload_not_allowed_in_manager_ledger"
    retired_supplier_ref = store.checkpoint_workflow_run(
        started["id"],
        checkpoint={
            "phase": "read",
            "compact_refs": [{"entity": "store_supplier", "id": "supplier-1", "version": "v1"}],
        },
    )
    assert retired_supplier_ref["error"] == "raw_store_payload_not_allowed_in_manager_ledger"

    compact = store.checkpoint_workflow_run(
        started["id"],
        checkpoint={
            "phase": "read",
            "cursor": "opaque-cursor",
            "compact_refs": [{"entity": "store_order", "id": "order-1", "version": "v1"}],
            "counts": {"orders": 1},
            "next_action": "dry_run",
        },
    )
    assert compact["ok"] is True
    assert b"+79990000000" not in db_path.read_bytes()


def test_store_workflow_transition_rejects_raw_verification_and_oversized_summary(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    started = store.start_workflow_run(
        workflow_id="store_management_workflow",
        intent="store_management",
        idempotency_key="store-verification-v1",
        scope={"domain": "store_order", "target_id": "order-1"},
    )

    raw = store.transition_workflow_run(
        started["id"],
        status="executing",
        verification={"order": {"customer_email": "client@example.test"}},
    )
    nested_raw = store.transition_workflow_run(
        started["id"],
        status="executing",
        verification={"customer_phone": "+79990000000"},
    )
    oversized = store.transition_workflow_run(
        started["id"],
        status="executing",
        summary="x" * 4097,
    )

    assert raw["error"] == "raw_store_payload_not_allowed_in_manager_ledger"
    assert nested_raw["error"] == "raw_store_payload_not_allowed_in_manager_ledger"
    assert oversized["error"] == "store_workflow_summary_too_large"


def test_gateway_inventory_workflow_is_detected_as_store_and_rejects_free_text_channels(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    operation = "mark_order_ready"

    query_rejected = store.start_workflow_run(
        workflow_id=f"inventory:{operation}",
        intent=f"inventory_{operation}",
        query="Заказ клиента Иванова, телефон +79990000000",
        idempotency_key="inventory-ready-query-v1",
        scope={"operation": operation, "request_fingerprint": "a" * 64},
    )
    intent_rejected = store.start_workflow_run(
        workflow_id=f"inventory:{operation}",
        intent="позвони клиенту после готовности",
        idempotency_key="inventory-ready-intent-v1",
        scope={"operation": operation, "request_fingerprint": "b" * 64},
    )

    assert query_rejected["error"] == "raw_store_payload_not_allowed_in_manager_ledger"
    assert "query" in query_rejected["forbidden_keys"]
    assert intent_rejected["error"] == "raw_store_payload_not_allowed_in_manager_ledger"
    assert "intent" in intent_rejected["forbidden_keys"]


def test_gateway_inventory_workflow_accepts_machine_channels_and_blocks_raw_lifecycle_text(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    operation = "set_quote_request_status"
    started = store.start_workflow_run(
        workflow_id=f"inventory:{operation}",
        intent=f"inventory_{operation}",
        idempotency_key="inventory-status-v1",
        correlation_id="StoreStatusCorrelation123",
        scope={"operation": operation, "request_fingerprint": "c" * 64},
        selected_ids=["quote-1"],
    )
    assert started["ok"] is True

    raw_transition = store.transition_workflow_run(
        started["id"],
        status="executing",
        message="Заявка клиента Петрова переведена в работу",
        summary="Телефон клиента +79990000000",
    )
    assert raw_transition["error"] == "raw_store_payload_not_allowed_in_manager_ledger"
    assert set(raw_transition["forbidden_keys"]) == {"message", "summary"}

    executing = store.transition_workflow_run(
        started["id"],
        status="executing",
        message=f"execute {operation}",
        summary=f"inventory:{operation}",
    )
    assert executing["ok"] is True

    raw_checkpoint = store.checkpoint_workflow_run(
        started["id"],
        checkpoint={"phase": "apply", "operation": operation, "target_id": "quote-1"},
        message="Комментарий клиента: срочно",
    )
    assert raw_checkpoint["error"] == "raw_store_payload_not_allowed_in_manager_ledger"
    assert raw_checkpoint["forbidden_keys"] == ["message"]

    safe_checkpoint = store.checkpoint_workflow_run(
        started["id"],
        checkpoint={"phase": "apply", "operation": operation, "target_id": "quote-1"},
        message=f"verify {operation}",
    )
    assert safe_checkpoint["ok"] is True

    raw_event = store.record_manager_run_event(
        started["id"],
        event_type="customer_message",
        message="email client@example.test",
        target_type="store_quote_request",
        target_id="quote-1",
        payload={"operation": operation},
    )
    assert raw_event["error"] == "raw_store_payload_not_allowed_in_manager_ledger"
    assert set(raw_event["forbidden_keys"]) == {"event_type", "message"}

    safe_event = store.record_manager_run_event(
        started["id"],
        event_type="verification",
        message=f"verify {operation}",
        target_type="store_quote_request",
        target_id="quote-1",
        payload={"operation": operation, "status": "completed"},
    )
    assert safe_event["ok"] is True


def test_crm_store_workflow_envelope_is_accepted_across_verified_and_compensating_paths(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    operation = "mark_order_ready"
    scope = {
        "operation": operation,
        "mode": "apply",
        "request_fingerprint": "a" * 64,
        "domain": "store_order",
        "source": "autostop_store_api",
    }

    completed_start = store.start_workflow_run(
        workflow_id=f"inventory:{operation}",
        intent=f"inventory_{operation}",
        idempotency_key="crm-store-ready-completed-v1",
        correlation_id="StoreReadyCorrelation123",
        scope=scope,
        source="crm_gateway",
    )
    assert completed_start["ok"] is True
    executing = store.transition_workflow_run(
        completed_start["id"],
        status="executing",
        message=f"execute {operation}",
        summary=f"inventory:{operation}",
        verification={"preflight_ok": True, "operation": operation, "mode": "apply"},
        expected_state_version=completed_start["state_version"],
    )
    assert executing["ok"] is True
    verifying = store.transition_workflow_run(
        completed_start["id"],
        status="verifying",
        message=f"verify {operation}",
        summary=f"inventory:{operation}",
        verification={"executor_ok": True, "operation": operation, "mode": "apply"},
        expected_state_version=executing["state_version"],
    )
    assert verifying["ok"] is True
    completed = store.transition_workflow_run(
        completed_start["id"],
        status="completed",
        message=f"completed {operation}",
        summary=f"inventory:{operation}",
        verification={
            "executor_ok": True,
            "readback_verified": True,
            "operation": operation,
            "mode": "apply",
        },
        expected_state_version=verifying["state_version"],
    )
    assert completed["ok"] is True

    compensating_start = store.start_workflow_run(
        workflow_id=f"inventory:{operation}",
        intent=f"inventory_{operation}",
        idempotency_key="crm-store-ready-compensating-v1",
        correlation_id="StoreReadyCorrelation456",
        scope=scope,
        source="crm_gateway",
    )
    assert compensating_start["ok"] is True
    compensating_executing = store.transition_workflow_run(
        compensating_start["id"],
        status="executing",
        message=f"execute {operation}",
        summary=f"inventory:{operation}",
        verification={"preflight_ok": True, "operation": operation, "mode": "apply"},
        expected_state_version=compensating_start["state_version"],
    )
    assert compensating_executing["ok"] is True
    compensating = store.transition_workflow_run(
        compensating_start["id"],
        status="compensating",
        message=f"verification failed after executor applied {operation}",
        summary=f"inventory:{operation}",
        verification={
            "write_applied_unverified": True,
            "outcome_uncertain": True,
            "readback_verified": False,
            "operation": operation,
            "mode": "apply",
        },
        expected_state_version=compensating_executing["state_version"],
    )
    assert compensating["ok"] is True

    replay_completed = store.transition_workflow_run(
        compensating_start["id"],
        status="completed",
        message=f"completed {operation}",
        summary=f"store:{operation}",
        verification={
            "executor_ok": True,
            "readback_verified": True,
            "idempotency_replay": True,
            "operation": operation,
            "mode": "apply",
        },
        expected_state_version=compensating["state_version"],
    )
    assert replay_completed["ok"] is True


def test_raw_store_owner_ledger_accepts_refs_only_dry_run_and_requires_bound_apply_readback(
    tmp_path,
):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    operation = "store_owner_api"
    request_fingerprint = "a" * 64
    target_ref_hash = hashlib.sha256(b"target:part-1").hexdigest()
    request_sha = "c" * 64
    schema_hash = "d" * 64
    expected_revision_hash = "f" * 64
    contract_id = "ac_" + "e" * 20
    operation_id = "update_category"

    def scope(mode: str, correlation_id: str) -> dict[str, object]:
        return {
            "operation": operation,
            "mode": mode,
            "operation_id": operation_id,
            "request_fingerprint": request_fingerprint,
            "schema_hash": schema_hash,
            "target_ref_sha256": target_ref_hash,
            "correlation_id": correlation_id,
            "domain": "store",
            "expected_revision_sha256": expected_revision_hash,
            "source": "store",
            "verification_class": "exact_entity",
        }

    def checkpoint() -> dict[str, object]:
        return {
            "phase": "transport_result",
            "operation": operation,
            "operation_id": operation_id,
            "contract_id": contract_id,
            "expected_revision_sha256": expected_revision_hash,
            "request_fingerprint": request_fingerprint,
            "request_sha256": request_sha,
            "schema_hash": schema_hash,
            "target_ref_sha256": target_ref_hash,
            "verification_class": "exact_entity",
            "status": "planned",
        }

    dry_correlation = "StoreOwnerDryCorrelation123"
    dry_started = store.start_workflow_run(
        workflow_id="raw:store_owner_api",
        intent="raw_store_owner_api",
        idempotency_key="raw-owner-dry-ledger-v1",
        correlation_id=dry_correlation,
        scope=scope("dry_run", dry_correlation),
        dry_run=True,
    )
    assert dry_started["ok"] is True
    dry_executing = store.transition_workflow_run(
        dry_started["id"],
        status="executing",
        message="raw execute store_owner_api",
        expected_state_version=dry_started["state_version"],
    )
    dry_checkpoint = store.checkpoint_workflow_run(
        dry_started["id"],
        checkpoint=checkpoint(),
        message="raw verify store_owner_api",
        expected_state_version=dry_executing["state_version"],
    )
    dry_verifying = store.transition_workflow_run(
        dry_started["id"],
        status="verifying",
        message="raw verify store_owner_api",
        expected_state_version=dry_checkpoint["state_version"],
    )
    dry_completed = store.transition_workflow_run(
        dry_started["id"],
        status="completed",
        message="raw completed store_owner_api",
        summary="raw:store_owner_api",
        verification={
            "executor_ok": True,
            "passed": True,
            "check": "store_owner_server_dry_run_receipt",
            "contract_id": contract_id,
            "expected_revision_sha256": expected_revision_hash,
            "operation_id": operation_id,
            "request_fingerprint": request_fingerprint,
            "request_sha256": request_sha,
            "schema_hash": schema_hash,
            "target_ref_sha256": target_ref_hash,
            "verification_class": "exact_entity",
        },
        expected_state_version=dry_verifying["state_version"],
    )
    assert dry_completed["ok"] is True

    apply_correlation = "StoreOwnerApplyCorrelation123"
    apply_started = store.start_workflow_run(
        workflow_id="raw:store_owner_api",
        intent="raw_store_owner_api",
        idempotency_key="raw-owner-apply-ledger-v1",
        correlation_id=apply_correlation,
        scope=scope("apply", apply_correlation),
    )
    assert apply_started["ok"] is True
    apply_executing = store.transition_workflow_run(
        apply_started["id"],
        status="executing",
        message="raw execute store_owner_api",
        expected_state_version=apply_started["state_version"],
    )
    apply_checkpoint = store.checkpoint_workflow_run(
        apply_started["id"],
        checkpoint={**checkpoint(), "status": "compensating"},
        message="raw verify store_owner_api",
        expected_state_version=apply_executing["state_version"],
    )
    compensating = store.transition_workflow_run(
        apply_started["id"],
        status="compensating",
        message="raw verification failed after executor applied store_owner_api",
        verification={
            "executor_ok": True,
            "schema_hash_verified": True,
            "required": True,
            "passed": False,
            "check": "store_owner_operation_specific_exact_readback",
            "evidence": {
                "transport_status": "compensating",
                "write_applied": True,
                "readback_required": True,
                "outcome_uncertain": False,
            },
        },
        expected_state_version=apply_checkpoint["state_version"],
    )
    assert compensating["ok"] is True

    for terminal_status in ("failed", "cancelled"):
        blocked_terminal = store.transition_workflow_run(
            apply_started["id"],
            status=terminal_status,
            expected_state_version=compensating["state_version"],
        )
        assert blocked_terminal["error"] == ("store_owner_reconciliation_required_before_terminal_transition")

    false_close = store.transition_workflow_run(
        apply_started["id"],
        status="completed",
        verification={"verified": True},
        expected_state_version=compensating["state_version"],
    )
    assert false_close["error"] == "store_owner_exact_readback_required_before_completion"

    compact_ref = {
        "entity": "store_part",
        "id": "part-1",
        "version": "revision-v2",
    }
    readback_ref_hash = hashlib.sha256(
        b"store-owner-readback-ref-v1\0"
        + json.dumps(
            {
                "entity": "store_part",
                "id": "part-1",
                "target_ref_sha256": target_ref_hash,
                "version": "revision-v2",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    wrong_target_close = store.transition_workflow_run(
        apply_started["id"],
        status="completed",
        verification={
            "executor_ok": True,
            "exact_readback_verified": True,
            "contract_id": contract_id,
            "expected_revision_sha256": expected_revision_hash,
            "operation_id": operation_id,
            "request_fingerprint": request_fingerprint,
            "request_sha256": request_sha,
            "schema_hash": schema_hash,
            "target_ref_sha256": target_ref_hash,
            "verification_class": "exact_entity",
            "readback_class": "exact_entity",
            "readback_ref_sha256": readback_ref_hash,
            "compact_ref": {**compact_ref, "id": "part-other"},
        },
        expected_state_version=compensating["state_version"],
    )
    assert wrong_target_close["error"] == "store_owner_exact_readback_required_before_completion"

    wrong_version_close = store.transition_workflow_run(
        apply_started["id"],
        status="completed",
        verification={
            "executor_ok": True,
            "exact_readback_verified": True,
            "contract_id": contract_id,
            "expected_revision_sha256": expected_revision_hash,
            "operation_id": operation_id,
            "request_fingerprint": request_fingerprint,
            "request_sha256": request_sha,
            "schema_hash": schema_hash,
            "target_ref_sha256": target_ref_hash,
            "verification_class": "exact_entity",
            "readback_class": "exact_entity",
            "readback_ref_sha256": readback_ref_hash,
            "compact_ref": {**compact_ref, "version": "revision-other"},
        },
        expected_state_version=compensating["state_version"],
    )
    assert wrong_version_close["error"] == "store_owner_exact_readback_required_before_completion"

    wrong_revision_close = store.transition_workflow_run(
        apply_started["id"],
        status="completed",
        verification={
            "executor_ok": True,
            "exact_readback_verified": True,
            "contract_id": contract_id,
            "expected_revision_sha256": "0" * 64,
            "operation_id": operation_id,
            "request_fingerprint": request_fingerprint,
            "request_sha256": request_sha,
            "schema_hash": schema_hash,
            "target_ref_sha256": target_ref_hash,
            "verification_class": "exact_entity",
            "readback_class": "exact_entity",
            "readback_ref_sha256": readback_ref_hash,
            "compact_ref": compact_ref,
        },
        expected_state_version=compensating["state_version"],
    )
    assert wrong_revision_close["error"] == "store_owner_exact_readback_required_before_completion"

    valid_close = store.transition_workflow_run(
        apply_started["id"],
        status="completed",
        message="raw completed store_owner_api",
        summary="raw:store_owner_api",
        verification={
            "executor_ok": True,
            "exact_readback_verified": True,
            "contract_id": contract_id,
            "expected_revision_sha256": expected_revision_hash,
            "operation_id": operation_id,
            "request_fingerprint": request_fingerprint,
            "request_sha256": request_sha,
            "schema_hash": schema_hash,
            "target_ref_sha256": target_ref_hash,
            "verification_class": "exact_entity",
            "readback_class": "exact_entity",
            "readback_ref_sha256": readback_ref_hash,
            "compact_ref": compact_ref,
        },
        expected_state_version=compensating["state_version"],
    )
    assert valid_close["ok"] is True
    assert store.get_manager_run(apply_started["id"])["item"]["status"] == "completed"


def test_raw_store_owner_ledger_rejects_missing_binding_and_private_payload(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    rejected = store.start_workflow_run(
        workflow_id="raw:store_owner_api",
        intent="raw_store_owner_api",
        idempotency_key="raw-owner-invalid-ledger-v1",
        correlation_id="StoreOwnerInvalidCorrelation123",
        scope={
            "operation": "store_owner_api",
            "mode": "apply",
            "request_fingerprint": "a" * 64,
            "target_ref_sha256": "b" * 64,
            "correlation_id": "StoreOwnerInvalidCorrelation123",
            "domain": "store",
            "source": "store",
            "customer_phone": "+79990000000",
        },
    )
    missing_binding = store.start_workflow_run(
        workflow_id="raw:store_owner_api",
        intent="raw_store_owner_api",
        idempotency_key="raw-owner-missing-binding-v1",
        correlation_id="StoreOwnerMissingCorrelation123",
        scope={
            "operation": "store_owner_api",
            "mode": "apply",
            "domain": "store",
            "source": "store",
        },
    )

    assert rejected["error"] == "raw_store_payload_not_allowed_in_manager_ledger"
    assert "scope.customer_phone" in rejected["forbidden_keys"]
    assert missing_binding["error"] == "raw_store_payload_not_allowed_in_manager_ledger"
    assert "scope.request_fingerprint" in missing_binding["forbidden_keys"]


def test_raw_store_owner_ledger_requires_state_version_on_every_mutation(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    started = store.start_workflow_run(
        workflow_id="raw:store_owner_api",
        intent="raw_store_owner_api",
        idempotency_key="raw-owner-version-required-v1",
        correlation_id="StoreOwnerVersionRequired123",
        scope={
            "operation": "store_owner_api",
            "mode": "apply",
            "request_fingerprint": "a" * 64,
            "target_ref_sha256": hashlib.sha256(b"target:part-1").hexdigest(),
            "correlation_id": "StoreOwnerVersionRequired123",
            "domain": "store",
            "expected_revision_sha256": "b" * 64,
            "source": "store",
            "verification_class": "exact_entity",
        },
    )
    assert started["ok"] is True

    transition = store.transition_workflow_run(started["id"], status="executing", message="raw execute store_owner_api")
    checkpoint = store.checkpoint_workflow_run(
        started["id"],
        checkpoint={},
        message="raw verify store_owner_api",
    )

    assert transition["error"] == "workflow_state_version_required"
    assert checkpoint["error"] == "workflow_state_version_required"
    executing = store.transition_workflow_run(
        started["id"],
        status="executing",
        message="raw execute store_owner_api",
        expected_state_version=started["state_version"],
    )
    external_wait = store.transition_workflow_run(
        started["id"],
        status="external_wait",
        expected_state_version=executing["state_version"],
    )
    external_step = store.register_external_step(
        started["id"],
        step_id="owner-external-step",
        connector="store",
        action="write",
        expected_state_version=executing["state_version"],
    )
    external_complete = store.complete_external_step(
        started["id"],
        step_id="owner-external-step",
        result_refs={"external_ref": "owner-ref"},
        expected_state_version=executing["state_version"],
    )
    resumed = store.resume_workflow_run(started["id"], expected_state_version=executing["state_version"])
    compatibility_finish = store.finish_manager_run(started["id"])

    assert external_wait["error"] == "store_owner_external_wait_not_allowed"
    assert external_step["error"] == "store_owner_external_steps_not_allowed"
    assert external_complete["error"] == "store_owner_external_steps_not_allowed"
    assert resumed["error"] == "store_owner_resume_not_allowed"
    assert compatibility_finish["error"] == "store_owner_compatibility_finish_not_allowed"
    assert store.get_manager_run(started["id"])["item"]["state_version"] == 2


def test_rossko_owner_apply_closes_after_secret_safe_operation_state_readback(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    target_id = "path:/api/v1/warehouse/rossko-settings"
    target_hash = hashlib.sha256(f"target:{target_id}".encode()).hexdigest()
    expected_revision_hash = "b" * 64
    correlation_id = "StoreOwnerRosskoClosure123"
    common = {
        "contract_id": "ac_" + "c" * 20,
        "expected_revision_sha256": expected_revision_hash,
        "operation_id": "update_rossko_settings",
        "request_fingerprint": "d" * 64,
        "request_sha256": "e" * 64,
        "schema_hash": "f" * 64,
        "target_ref_sha256": target_hash,
        "verification_class": "operation_specific_state",
    }
    started = store.start_workflow_run(
        workflow_id="raw:store_owner_api",
        intent="raw_store_owner_api",
        idempotency_key="raw-owner-rossko-closure-v1",
        correlation_id=correlation_id,
        scope={
            "operation": "store_owner_api",
            "mode": "apply",
            "request_fingerprint": common["request_fingerprint"],
            "target_ref_sha256": target_hash,
            "correlation_id": correlation_id,
            "domain": "store",
            "expected_revision_sha256": expected_revision_hash,
            "source": "store",
            "verification_class": "operation_specific_state",
        },
    )
    executing = store.transition_workflow_run(
        started["id"],
        status="executing",
        message="raw execute store_owner_api",
        expected_state_version=started["state_version"],
    )
    checkpoint = store.checkpoint_workflow_run(
        started["id"],
        checkpoint={
            "phase": "transport_result",
            "operation": "store_owner_api",
            "status": "compensating",
            **common,
        },
        message="raw verify store_owner_api",
        expected_state_version=executing["state_version"],
    )
    compensating = store.transition_workflow_run(
        started["id"],
        status="compensating",
        message="raw verification failed after executor applied store_owner_api",
        verification={"executor_ok": True, "passed": False},
        expected_state_version=checkpoint["state_version"],
    )
    compact_ref = {
        "entity": "store_state",
        "id": "rossko-settings",
        "version": "9" * 64,
    }
    readback_hash = hashlib.sha256(
        b"store-owner-readback-ref-v1\0"
        + json.dumps(
            {
                **compact_ref,
                "target_ref_sha256": target_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    completed = store.transition_workflow_run(
        started["id"],
        status="completed",
        message="raw completed store_owner_api",
        summary="raw:store_owner_api",
        verification={
            "executor_ok": True,
            "exact_readback_verified": True,
            **common,
            "readback_class": "operation_specific_state",
            "operation_state_ref_sha256": target_hash,
            "readback_ref_sha256": readback_hash,
            "compact_ref": compact_ref,
        },
        expected_state_version=compensating["state_version"],
    )

    assert completed["ok"] is True
    assert store.get_manager_run(started["id"])["item"]["status"] == "completed"


def test_raw_store_owner_ledger_retention_is_bounded_and_cascades_events(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    def start(key: str, correlation: str):
        return store.start_workflow_run(
            workflow_id="raw:store_owner_api",
            intent="raw_store_owner_api",
            idempotency_key=key,
            correlation_id=correlation,
            scope={
                "operation": "store_owner_api",
                "mode": "apply",
                "request_fingerprint": "a" * 64,
                "target_ref_sha256": hashlib.sha256(b"target:part-1").hexdigest(),
                "correlation_id": correlation,
                "domain": "store",
                "expected_revision_sha256": "b" * 64,
                "source": "store",
                "verification_class": "exact_entity",
            },
        )

    stale = start("raw-owner-retention-stale-v1", "StoreOwnerRetentionStale123")
    unresolved = start("raw-owner-retention-unresolved-v1", "StoreOwnerRetentionUnresolved123")
    assert stale["ok"] is True
    assert unresolved["ok"] is True
    cutoff = (datetime.now(UTC) - timedelta(days=181)).isoformat()
    with store.connect() as conn:
        conn.execute(
            "UPDATE manager_runs SET updated_at = ? WHERE id = ?",
            (cutoff, stale["id"]),
        )
        conn.execute(
            """
            UPDATE manager_runs
            SET status = 'compensating', updated_at = ?,
                checkpoint_json = '{"phase":"transport_result"}'
            WHERE id = ?
            """,
            (cutoff, unresolved["id"]),
        )
        smoke_cursor = conn.execute(
            """
            INSERT INTO manager_runs
              (intent, workflow_id, status, dry_run, source, started_at, updated_at)
            VALUES ('release_smoke', 'raw:api:/api/change_feed/bootstrap',
                    'completed', 1, 'release-smoke', ?, ?)
            """,
            (cutoff, cutoff),
        )
        smoke_id = smoke_cursor.lastrowid
        assert (
            conn.execute("SELECT COUNT(*) FROM manager_run_events WHERE run_id = ?", (stale["id"],)).fetchone()[0] == 1
        )

    fresh = start("raw-owner-retention-fresh-v1", "StoreOwnerRetentionFresh123")

    assert fresh["ok"] is True
    assert store.get_manager_run(stale["id"])["ok"] is False
    assert store.get_manager_run(unresolved["id"])["ok"] is True
    assert smoke_id is not None
    assert store.get_manager_run(smoke_id)["ok"] is False
    with store.connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM manager_run_events WHERE run_id = ?", (stale["id"],)).fetchone()[0] == 0
        )


def test_store_workflow_rejects_pii_or_secret_values_in_structured_refs(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    operation = "assign_quote_request"

    private_id = store.start_workflow_run(
        workflow_id=f"inventory:{operation}",
        intent=f"inventory_{operation}",
        idempotency_key="inventory-private-id-v1",
        scope={"operation": operation, "request_fingerprint": "d" * 64},
        selected_ids=["client@example.test"],
    )
    secret_metadata = store.start_workflow_run(
        workflow_id=f"inventory:{operation}",
        intent=f"inventory_{operation}",
        idempotency_key="inventory-secret-metadata-v1",
        scope={"operation": operation, "request_fingerprint": "e" * 64},
        metadata={"access_token": "secret-value"},
    )
    secret_value = store.start_workflow_run(
        workflow_id=f"inventory:{operation}",
        intent=f"inventory_{operation}",
        idempotency_key="inventory-secret-value-v1",
        scope={"operation": operation, "request_fingerprint": "f" * 64},
        metadata={"request_fingerprint": "sk_test-sensitive-value"},
    )
    vin_ref = store.start_workflow_run(
        workflow_id=f"inventory:{operation}",
        intent=f"inventory_{operation}",
        idempotency_key="inventory-vin-ref-v1",
        scope={"operation": operation, "request_fingerprint": "0" * 64},
        selected_ids=["WDD2130421A123456"],
    )

    assert private_id["error"] == "raw_store_payload_not_allowed_in_manager_ledger"
    assert "selected_ids[0]" in private_id["forbidden_keys"]
    assert secret_metadata["error"] == "raw_store_payload_not_allowed_in_manager_ledger"
    assert "metadata.access_token" in secret_metadata["forbidden_keys"]
    assert secret_value["error"] == "raw_store_payload_not_allowed_in_manager_ledger"
    assert "metadata.request_fingerprint" in secret_value["forbidden_keys"]
    assert vin_ref["error"] == "raw_store_payload_not_allowed_in_manager_ledger"
    assert "selected_ids[0]" in vin_ref["forbidden_keys"]


def test_v2_idempotency_rejects_changed_scope_selected_ids_or_mode(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    first = store.start_workflow_run(
        workflow_id="finance:create_cash_transaction",
        intent="finance_create_cash_transaction",
        idempotency_key="finance-key-1",
        scope={"operation": "create_cash_transaction", "request_fingerprint": "aaa"},
        selected_ids=["cashbox-1"],
        dry_run=False,
    )

    changed_scope = store.start_workflow_run(
        workflow_id="finance:create_cash_transaction",
        intent="finance_create_cash_transaction",
        idempotency_key="finance-key-1",
        scope={"operation": "create_cash_transaction", "request_fingerprint": "bbb"},
        selected_ids=["cashbox-1"],
        dry_run=False,
    )
    changed_ids = store.start_workflow_run(
        workflow_id="finance:create_cash_transaction",
        intent="finance_create_cash_transaction",
        idempotency_key="finance-key-1",
        scope={"operation": "create_cash_transaction", "request_fingerprint": "aaa"},
        selected_ids=["cashbox-2"],
        dry_run=False,
    )
    changed_mode = store.start_workflow_run(
        workflow_id="finance:create_cash_transaction",
        intent="finance_create_cash_transaction",
        idempotency_key="finance-key-1",
        scope={"operation": "create_cash_transaction", "request_fingerprint": "aaa"},
        selected_ids=["cashbox-1"],
        dry_run=True,
    )

    assert first["ok"] is True
    assert changed_scope["ok"] is False
    assert changed_scope["conflict_fields"] == ["scope"]
    assert changed_ids["ok"] is False
    assert changed_ids["conflict_fields"] == ["selected_ids"]
    assert changed_mode["ok"] is False
    assert changed_mode["conflict_fields"] == ["dry_run"]


def test_v2_concurrent_idempotent_starts_deduplicate_without_integrity_errors(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    workers = 24
    barrier = Barrier(workers)

    def start(_index):
        barrier.wait()
        return store.start_workflow_run(
            workflow_id="crm_gmail_workflow",
            intent="crm_gmail_workflow",
            query="ответь клиенту",
            idempotency_key="concurrent-same-key",
            scope={"card_id": "C-1"},
            selected_ids=["C-1"],
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(start, range(workers)))

    assert all(result["ok"] is True for result in results)
    assert len({result["id"] for result in results}) == 1
    assert sum(result["deduplicated"] is False for result in results) == 1


def test_v2_rejects_invalid_state_transition(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    started = store.start_workflow_run(
        workflow_id="timer_floor_control",
        intent="timer_floor",
        query="подними таймеры",
        idempotency_key="timer-floor-v2",
    )

    result = store.transition_workflow_run(started["id"], status="completed")

    assert result["ok"] is False
    assert result["error"] == "invalid_workflow_transition"
    assert result["allowed"] == ["cancelled", "executing", "failed"]


def test_v2_completed_rejects_explicit_executor_or_verification_failure(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    failure_evidence = [
        ({"executor_ok": False, "passed": True}, ["executor_ok"]),
        ({"executor": False, "passed": True}, ["executor"]),
        ({"executor": "failed", "passed": True}, ["executor"]),
        ({"verification": False}, ["verification"]),
        ({"verification_passed": False}, ["verification_passed"]),
        ({"verification": {"passed": False}}, ["verification.passed"]),
        ({"verification": {"status": "failed"}}, ["verification.status"]),
    ]

    for index, (verification, expected_paths) in enumerate(failure_evidence):
        started = store.start_workflow_run(
            workflow_id="board",
            intent="board_write",
            idempotency_key=f"completion-failure-{index}",
        )
        executing = store.transition_workflow_run(started["id"], status="executing", expected_state_version=1)
        verifying = store.transition_workflow_run(
            started["id"],
            status="verifying",
            expected_state_version=executing["state_version"],
        )

        rejected = store.transition_workflow_run(
            started["id"],
            status="completed",
            verification=verification,
            expected_state_version=verifying["state_version"],
        )

        assert rejected["ok"] is False
        assert rejected["error"] == "verification_failed_before_completion"
        assert rejected["failure_paths"] == expected_paths
        status = store.get_manager_run(started["id"], include_events=False)
        assert status["item"]["status"] == "verifying"
        assert status["item"]["state_version"] == verifying["state_version"]


def test_v2_completed_dedup_still_rejects_new_failed_evidence(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    started = store.start_workflow_run(
        workflow_id="board",
        intent="board_write",
        idempotency_key="completed-dedup-verification",
    )
    executing = store.transition_workflow_run(started["id"], status="executing")
    verifying = store.transition_workflow_run(started["id"], status="verifying")
    completed = store.transition_workflow_run(
        started["id"],
        status="completed",
        verification={"executor_ok": True, "verification_passed": True},
        expected_state_version=verifying["state_version"],
    )
    assert completed["ok"] is True

    rejected = store.transition_workflow_run(
        started["id"],
        status="completed",
        verification={"executor_ok": False},
        expected_state_version=completed["state_version"],
    )

    assert rejected["ok"] is False
    assert rejected["error"] == "verification_failed_before_completion"
    assert rejected["failure_paths"] == ["executor_ok"]
    assert executing["state_version"] == 2


def test_v2_mutable_lifecycle_calls_enforce_expected_state_version_cas(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    started = store.start_workflow_run(
        workflow_id="crm_gmail_workflow",
        intent="crm_gmail_workflow",
        idempotency_key="workflow-cas-all-mutations",
    )

    stale_transition = store.transition_workflow_run(started["id"], status="executing", expected_state_version=0)
    assert stale_transition == {
        "ok": False,
        "error": "workflow_state_conflict",
        "run_id": started["id"],
        "expected_state_version": 0,
        "current_state_version": 1,
    }
    executing = store.transition_workflow_run(started["id"], status="executing", expected_state_version=1)
    assert executing["state_version"] == 2

    stale_checkpoint = store.checkpoint_workflow_run(
        started["id"], checkpoint={"phase": "stale"}, expected_state_version=1
    )
    assert stale_checkpoint["error"] == "workflow_state_conflict"
    assert stale_checkpoint["current_state_version"] == 2
    checkpoint = store.checkpoint_workflow_run(started["id"], checkpoint={"phase": "ready"}, expected_state_version=2)
    assert checkpoint["state_version"] == 3

    stale_wait = store.register_external_step(
        started["id"],
        step_id="gmail-send-cas",
        connector="gmail",
        action="send",
        request_refs={"thread_id": "thread-cas"},
        expected_state_version=2,
    )
    assert stale_wait["error"] == "workflow_state_conflict"
    waiting = store.register_external_step(
        started["id"],
        step_id="gmail-send-cas",
        connector="gmail",
        action="send",
        request_refs={"thread_id": "thread-cas"},
        expected_state_version=3,
    )
    assert waiting["state_version"] == 4

    stale_complete = store.complete_external_step(
        started["id"],
        step_id="gmail-send-cas",
        result_refs={"message_id": "message-cas"},
        expected_state_version=3,
    )
    assert stale_complete["error"] == "workflow_state_conflict"
    completed_step = store.complete_external_step(
        started["id"],
        step_id="gmail-send-cas",
        result_refs={"message_id": "message-cas"},
        expected_state_version=4,
    )
    assert completed_step["state_version"] == 5

    stale_resume = store.resume_workflow_run(started["id"], expected_state_version=4)
    assert stale_resume["error"] == "workflow_state_conflict"
    resumed = store.resume_workflow_run(started["id"], expected_state_version=5)
    assert resumed["status"] == "executing"
    assert resumed["state_version"] == 6

    stale_cancel = store.cancel_workflow_run(started["id"], reason="stale", expected_state_version=5)
    assert stale_cancel["error"] == "workflow_state_conflict"
    cancelled = store.cancel_workflow_run(started["id"], reason="done", expected_state_version=6)
    assert cancelled["status"] == "cancelled"
    assert cancelled["state_version"] == 7


def test_initialize_migrates_current_manager_run_schema_without_losing_legacy_rows(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE manager_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent TEXT NOT NULL DEFAULT '',
                query TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'running',
                dry_run INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'codex',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                summary TEXT NOT NULL DEFAULT '',
                verification_json TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL,
                finished_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE manager_run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                target_type TEXT NOT NULL DEFAULT '',
                target_id TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            INSERT INTO manager_runs (
                intent, query, status, dry_run, source, metadata_json,
                summary, verification_json, started_at, updated_at
            ) VALUES (
                'legacy', 'old run', 'running', 0, 'codex', '{}', '', '{}',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
            );
            """
        )

    store = ManagerMemoryStore(db_path)
    store.initialize()

    with sqlite3.connect(db_path) as conn:
        run_columns = {row[1] for row in conn.execute("PRAGMA table_info(manager_runs)")}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "workflow_id",
        "request_id",
        "idempotency_key",
        "correlation_id",
        "actor",
        "scope_json",
        "selected_ids_json",
        "checkpoint_json",
        "compensation_json",
        "state_version",
    }.issubset(run_columns)
    assert "manager_run_external_steps" in tables

    legacy = store.get_manager_run(1, include_events=False, include_external_steps=True)
    assert legacy["item"]["status"] == "running"
    assert legacy["item"]["checkpoint"] == {}
    assert legacy["item"]["external_steps"] == []
