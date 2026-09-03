from __future__ import annotations

import json
from pathlib import Path

import pytest

from autostop_manager.action_contract import EXECUTOR_TOOLS, INVENTORY_EXECUTOR_TOOLS, prepare_action_contract


ROOT = Path(__file__).resolve().parents[1]


def _completion_act_form(*, basis: str = "") -> dict:
    party = {
        "legal_name": "",
        "address": "",
        "inn": "",
        "kpp": "",
        "ogrn": "",
        "bank_name": "",
        "bik": "",
        "settlement_account": "",
        "correspondent_account": "",
        "signer_position": "",
        "signer_name": "",
    }
    return {
        "document_number": "18",
        "document_date": "21.08.2026",
        "basis": basis,
        "performer": dict(party),
        "customer": dict(party),
        "items": [],
        "acceptance_text": "",
    }


def test_completion_act_save_builds_named_dry_run_and_apply_contracts():
    common = {
        "domain": "document",
        "action": "save_completion_act_form",
        "target_id": "card-18",
        "owner_intent": "Сохрани только тестовое основание акта",
        "expected_revision": "4",
    }
    planned = {
        "form": _completion_act_form(basis="ТЕСТ CODEX"),
        "expected_source_fingerprint": "a" * 64,
    }
    preview = prepare_action_contract(
        **common,
        planned_changes=planned,
        idempotency_key="completion-act-preview",
        dry_run=True,
    )
    apply = prepare_action_contract(
        **common,
        planned_changes={
            **planned,
            "dry_run_proof": "b" * 64,
            "dry_run_idempotency_key": "completion-act-preview",
        },
        idempotency_key="completion-act-apply",
        dry_run=False,
    )

    assert preview["ok"] is True
    assert apply["ok"] is True
    assert preview["correlation_id"] == apply["correlation_id"]
    assert preview["execution"]["tool"] == "agent_document_workflow"
    assert preview["execution"]["operation"] == "save_completion_act_form"
    assert preview["execution"]["gateway_arguments"]["mode"] == "dry_run"
    assert apply["execution"]["gateway_arguments"]["mode"] == "apply"
    assert apply["execution"]["gateway_arguments"]["payload"]["dry_run_proof"] == "b" * 64


def test_completion_act_apply_requires_bound_proof_and_distinct_key():
    changes = {
        "form": _completion_act_form(),
        "expected_source_fingerprint": "a" * 64,
        "dry_run_proof": "b" * 64,
        "dry_run_idempotency_key": "same-key",
    }
    result = prepare_action_contract(
        domain="document",
        action="save_completion_act_form",
        target_id="card-18",
        planned_changes=changes,
        owner_intent="Сохрани черновик",
        expected_revision="4",
        idempotency_key="same-key",
        dry_run=False,
    )

    assert result["ok"] is False
    assert "apply_requires_new_idempotency_key" in result["preflight"]["blocking_reasons"]


def test_completion_act_reset_is_destructive_and_restores_verified_snapshot():
    result = prepare_action_contract(
        domain="document",
        action="reset_completion_act_form",
        target_id="card-18",
        planned_changes={
            "expected_source_fingerprint": "a" * 64,
            "verified_snapshot": {
                "form": _completion_act_form(basis="verified"),
                "version": 4,
                "source_fingerprint": "a" * 64,
            },
        },
        owner_intent="Сбрось тестовый черновик",
        expected_revision="4",
        idempotency_key="completion-act-reset-preview",
        dry_run=True,
    )

    assert result["ok"] is True
    assert result["execution"]["tool"] == "agent_document_workflow"
    assert "verified_snapshot" not in result["execution"]["gateway_arguments"]["payload"]
    assert result["compensation"]["required"] is True
    assert result["compensation"]["strategy"].startswith("restore_completion_act")


def test_payment_action_contract_requires_and_reconciles_financial_context():
    result = prepare_action_contract(
        domain="payment",
        action="record_payment",
        planned_changes={
            "amount": 12_500,
            "cashbox_id": "cashbox-main",
            "card_id": "card-42",
            "payment_method": "card",
            "outstanding_amount": 12_500,
        },
        owner_intent="Проведи оплату 12 500 по заказ-наряду ro-42 в основную кассу",
        expected_revision="ro-42@2026-07-11T10:00:00+07:00",
        idempotency_key="payment-ro-42-12500-v1",
        run_id=7,
    )

    assert result["ok"] is True
    assert result["format"] == "action_contract_v2"
    assert result["execution"]["tool"] == "agent_finance_workflow"
    assert result["execution"]["operation"] == "record_repair_order_payment"
    assert result["execution"]["gateway_arguments"]["payload"]["expected_updated_at"]
    assert result["execution"]["ready"] is True
    assert result["idempotency"]["required"] is True
    assert result["verification"]["requires_readback"] is True
    assert "repair_order_balance_reconciled" in result["verification"]["checks"]
    assert result["compensation"]["strategy"] == "compensating_transaction_never_history_delete"


def test_payment_action_contract_blocks_unapproved_overpayment():
    result = prepare_action_contract(
        domain="payment",
        action="record_payment",
        planned_changes={
            "amount": 15_000,
            "cashbox_id": "cashbox-main",
            "card_id": "card-42",
            "payment_method": "cash",
            "outstanding_amount": 12_500,
        },
        owner_intent="Проведи оплату по ro-42",
        expected_revision="ro-42@2026-07-11T10:00:00+07:00",
        idempotency_key="payment-ro-42-over-v1",
    )

    assert result["ok"] is False
    assert "overpayment_not_explicitly_allowed" in result["preflight"]["blocking_reasons"]


def test_payment_action_contract_rejects_non_finite_or_boolean_amounts():
    for amount in (True, "nan", "inf", "-inf", 10**400):
        result = prepare_action_contract(
            domain="payment",
            action="record_payment",
            planned_changes={
                "amount": amount,
                "cashbox_id": "cashbox-main",
                "card_id": "card-42",
                "payment_method": "cash",
                "outstanding_amount": 12_500,
            },
            owner_intent="Проведи оплату по ro-42",
            expected_revision="ro-42@7",
            idempotency_key=f"invalid-amount-{amount}",
        )

        assert result["ok"] is False
        assert "invalid_positive_amount" in result["preflight"]["blocking_reasons"]


def test_payment_action_contract_requires_boolean_true_for_overpayment():
    changes = {
        "amount": 15_000,
        "cashbox_id": "cashbox-main",
        "card_id": "card-42",
        "payment_method": "cash",
        "outstanding_amount": 12_500,
    }
    blocked = prepare_action_contract(
        domain="payment",
        action="record_payment",
        planned_changes={**changes, "allow_overpayment": "false"},
        owner_intent="Проведи оплату по ro-42",
        expected_revision="ro-42@7",
        idempotency_key="overpayment-string-false",
    )
    allowed = prepare_action_contract(
        domain="payment",
        action="record_payment",
        planned_changes={**changes, "allow_overpayment": True},
        owner_intent="Проведи оплату по ro-42 с подтвержденной переплатой",
        expected_revision="ro-42@7",
        idempotency_key="overpayment-explicit-true",
    )

    assert "overpayment_not_explicitly_allowed" in blocked["preflight"]["blocking_reasons"]
    assert allowed["ok"] is True
    assert "explicit_overpayment" in allowed["warnings"]


@pytest.mark.parametrize("outstanding", ["NaN", "Infinity", "-Infinity", True])
def test_payment_action_contract_rejects_non_finite_or_boolean_outstanding_amount(outstanding):
    result = prepare_action_contract(
        domain="payment",
        action="record_payment",
        planned_changes={
            "amount": 1_000,
            "cashbox_id": "cashbox-main",
            "card_id": "card-42",
            "payment_method": "cash",
            "outstanding_amount": outstanding,
        },
        owner_intent="Проведи оплату по ro-42",
        expected_revision="ro-42@2026-07-11T10:00:00+07:00",
        idempotency_key=f"payment-invalid-outstanding-{outstanding!s}",
    )

    assert result["ok"] is False
    assert "missing_outstanding_amount" in result["preflight"]["blocking_reasons"]


def test_gmail_invoice_requires_document_guard_and_mismatch_confirmation():
    common = {
        "domain": "gmail",
        "action": "send",
        "owner_intent": "Отправь счёт клиенту",
        "idempotency_key": "gmail-invoice-send-v1",
    }
    unsafe = prepare_action_contract(
        **common,
        planned_changes={"to": "client@example.com", "subject": "Счёт", "body_intent": "Счёт во вложении"},
    )
    safe = prepare_action_contract(
        **common,
        planned_changes={
            "to": "client@example.com",
            "sender": "service@example.com",
            "sender_verified": True,
            "subject": "Счёт",
            "body_intent": "Счёт во вложении",
            "document_guard": {
                "attachment_sha256": "b" * 64,
                "money_basis": "repair_order_current",
                "rendered_total": "12500.00",
                "repair_order_total": "12500.00",
                "tax_status": "without_vat",
                "financial_mismatch": False,
                "tax_mismatch": False,
                "financial_or_tax_mismatch": False,
                "mismatch_with_current_repair_order": False,
                "qa_passed": True,
            },
        },
    )
    mismatch_changes = {
        **safe["planned_changes"],
        "document_guard": {
            **safe["planned_changes"]["document_guard"],
            "repair_order_total": "12499.00",
            "financial_mismatch": True,
            "financial_or_tax_mismatch": True,
            "mismatch_with_current_repair_order": True,
        },
    }
    mismatch = prepare_action_contract(**common, planned_changes=mismatch_changes)
    invalid_guard = {**safe["planned_changes"]["document_guard"], "financial_mismatch": True}
    invalid_guard.pop("tax_status")
    invalid = prepare_action_contract(
        **common, planned_changes={**safe["planned_changes"], "document_guard": invalid_guard}
    )
    total_cases = (
        ({"repair_order_total": None}, "document_guard_repair_order_total_required"),
        ({"rendered_total": "NaN"}, "document_guard_rendered_total_required"),
        ({"rendered_total": "-1"}, "document_guard_rendered_total_required"),
        (
            {"rendered_total": "9007199254740992.00", "repair_order_total": "9007199254740993.00"},
            "inconsistent_document_guard_mismatch_flags",
        ),
    )

    assert unsafe["ok"] is False
    assert "verified_sender_required" in unsafe["preflight"]["blocking_reasons"]
    assert safe["ok"] is True
    assert "financial_or_tax_mismatch_confirmation_required" in mismatch["preflight"]["blocking_reasons"]
    assert "attachment_sha256_matches_document_guard" in safe["verification"]["checks"]
    assert "document_guard_tax_status_required" in invalid["preflight"]["blocking_reasons"]
    assert "inconsistent_document_guard_mismatch_flags" in invalid["preflight"]["blocking_reasons"]
    for guard_patch, blocker in total_cases:
        blocked = prepare_action_contract(
            **common,
            planned_changes={
                **safe["planned_changes"],
                "document_guard": {**safe["planned_changes"]["document_guard"], **guard_patch},
            },
        )
        assert blocker in blocked["preflight"]["blocking_reasons"]


@pytest.mark.parametrize(
    ("action", "planned_changes", "tool"),
    [
        (
            "send",
            {
                "to": "client@example.com",
                "subject": "Документы по автомобилю",
                "body_intent": "Отправить проверенный PDF заказ-наряда",
                "attachment_ids": ["crm-file-7"],
            },
            "gmail:_send_email",
        ),
        (
            "forward",
            {"message_ids": ["message-1", "message-2"], "to": "manager@example.com", "note": "Для ознакомления"},
            "gmail:_forward_emails",
        ),
        (
            "label",
            {
                "message_ids": ["message-1", "message-2"],
                "add_label_names": ["Заказ-наряды"],
                "remove_label_names": ["Входящие на разбор"],
                "create_missing_labels": False,
            },
            "gmail:_apply_labels_to_emails",
        ),
        ("archive", {"message_ids": ["message-1"]}, "gmail:_archive_emails"),
        ("delete", {"message_ids": ["message-1"]}, "gmail:_delete_emails"),
        (
            "batch_modify",
            {"message_ids": ["message-1"], "add_labels": ["Label_1"]},
            "gmail:_batch_modify_email",
        ),
        (
            "bulk_label",
            {
                "query": "from:supplier@example.com older_than:1y",
                "label_name": "Архив поставщика",
                "archive": True,
                "create_label_if_missing": False,
            },
            "gmail:_bulk_label_matching_emails",
        ),
        ("create_label", {"name": "Заказ-наряды"}, "gmail:_create_label"),
        (
            "create_draft",
            {"to": "me", "subject": "Проверка", "body": "Тестовый черновик"},
            "gmail:_create_draft",
        ),
        (
            "update_draft",
            {"draft_id": "draft-1", "subject": "Уточнённая тема"},
            "gmail:_update_draft",
        ),
        ("send_draft", {"draft_id": "draft-1"}, "gmail:_send_draft"),
    ],
)
def test_gmail_current_mutation_surface(action, planned_changes, tool):
    result = prepare_action_contract(
        domain="gmail",
        action=action,
        planned_changes=planned_changes,
        owner_intent=f"Выполни точное тестовое действие {action}",
        idempotency_key=f"gmail-{action}-contract-v1",
    )

    assert result["ok"] is True
    assert result["execution"]["external_connector"] == "gmail"
    assert result["execution"]["tool"] == tool
    assert result["execution"]["ready"] is True
    assert result["concurrency"]["required"] is False
    assert result["target"]["id"] is None
    assert "owner_confirmation" not in str(result).casefold()
    assert result["ledger"]["store_refs_only"] is True


@pytest.mark.parametrize(
    ("action", "planned_changes", "blockers"),
    [
        (
            "send",
            {"to": 123, "subject": 456, "body_intent": 789},
            {"missing_exact_recipients", "missing_subject", "missing_body_intent"},
        ),
        (
            "send",
            {"recipients": ["client@example.com"], "subject": "Проверка", "body": "Текст"},
            {"missing_exact_recipients"},
        ),
        ("send_email", {"legacy": True}, {"unsupported_mutating_action"}),
        ("forward_emails", {"legacy": True}, {"unsupported_mutating_action"}),
        ("apply_labels_to_emails", {"legacy": True}, {"unsupported_mutating_action"}),
        ("archive_emails", {"legacy": True}, {"unsupported_mutating_action"}),
        ("delete_emails", {"legacy": True}, {"unsupported_mutating_action"}),
        ("batch_modify_email", {"legacy": True}, {"unsupported_mutating_action"}),
        ("bulk_label_matching_emails", {"legacy": True}, {"unsupported_mutating_action"}),
        ("label", {"message_ids": "message-1", "add_label_names": ["Заказ-наряды"]}, {"missing_message_or_label_ids"}),
        ("label", {"message_ids": [123], "add_label_names": ["Заказ-наряды"]}, {"missing_message_or_label_ids"}),
        ("label", {"message_ids": ["message-1"], "add_label_names": [""]}, {"missing_message_or_label_ids"}),
        ("label", {"message_ids": ["message-1"], "label_ids": ["Label_1"]}, {"missing_message_or_label_ids"}),
        (
            "label",
            {"message_ids": ["message-1"], "add_label_names": ["Заказ-наряды"], "create_missing_labels": "false"},
            {"invalid_create_missing_labels_flag"},
        ),
        ("archive", {"message_ids": []}, {"missing_exact_message_ids"}),
        ("delete", {"message_ids": "message-1"}, {"missing_exact_message_ids"}),
        ("batch_modify", {"message_ids": ["message-1"]}, {"missing_label_ids"}),
        ("bulk_label", {"query": "in:inbox"}, {"missing_label_name"}),
        ("create_label", {"name": ""}, {"missing_label_name"}),
        ("create_draft", {"to": "me", "subject": "x"}, {"missing_body_intent"}),
        ("update_draft", {"draft_id": "draft-1"}, {"missing_draft_changes"}),
        ("send_draft", {"draft_id": ""}, {"missing_exact_draft_id"}),
    ],
)
def test_gmail_mutations_fail_closed_on_invalid_inputs(action, planned_changes, blockers):
    result = prepare_action_contract(
        domain="gmail",
        action=action,
        planned_changes=planned_changes,
        owner_intent=f"Выполни тестовое действие {action}",
        idempotency_key=f"gmail-{action}-invalid-v1",
    )

    assert result["ok"] is False
    assert blockers.issubset(result["preflight"]["blocking_reasons"])


def test_document_contract_accepts_request_text_for_crm_type_inference():
    result = prepare_action_contract(
        domain="document",
        action="generate",
        planned_changes={"request_text": "Сформируй акт выполненных работ без карточки"},
        owner_intent="Создай акт выполненных работ в CRM",
        idempotency_key="document-completion-act-v1",
    )

    assert result["ok"] is True
    assert result["execution"]["tool"] == "create_document_without_card_pdf"
    assert result["execution"]["ready"] is True


def test_card_document_uses_repair_order_print_pdf():
    result = prepare_action_contract(
        domain="document",
        action="generate",
        target_id="card-42",
        planned_changes={"request_text": "Сформируй счёт из заказ-наряда"},
        owner_intent="Сформируй счёт для карточки card-42",
        idempotency_key="document-card-42-v1",
    )

    assert result["ok"] is True
    assert result["execution"]["tool"] == "download_repair_order_print_pdf"
    assert result["execution"]["gateway_arguments"] == {"card_id": "card-42"}


def test_repair_order_update_uses_proof_bound_finance_workflow():
    common = {
        "domain": "repair_order",
        "action": "update",
        "target_id": "card-42",
        "owner_intent": "Поставь компанию Horizon клиентом в заказ-наряде карточки card-42",
        "expected_revision": "2026-08-25T10:00:00Z",
    }
    preview = prepare_action_contract(
        **common,
        planned_changes={"client_id": "client-company"},
        idempotency_key="ro-42-preview",
    )
    apply = prepare_action_contract(
        **common,
        planned_changes={
            "client_id": "client-company",
            "dry_run_proof": "a" * 64,
            "dry_run_idempotency_key": "ro-42-preview",
        },
        idempotency_key="ro-42-apply",
        dry_run=False,
    )

    assert preview["execution"]["tool"] == "agent_finance_workflow"
    assert preview["execution"]["gateway_arguments"]["mode"] == "dry_run"
    assert preview["execution"]["gateway_arguments"]["payload"] == {
        "card_id": "card-42",
        "repair_order": {"client_id": "client-company"},
        "expected_updated_at": "2026-08-25T10:00:00Z",
    }
    assert apply["ok"] is True
    assert apply["execution"]["operation"] == "update_repair_order"
    assert apply["execution"]["gateway_arguments"]["dry_run_proof"] == "a" * 64


def test_repair_order_update_target_cannot_be_overridden_by_changes():
    result = prepare_action_contract(
        domain="repair_order",
        action="update",
        target_id="card-42",
        planned_changes={"card_id": "card-other", "repair_order": {"client_id": "client-company"}},
        owner_intent="Обнови клиента заказ-наряда карточки card-42",
        expected_revision="2026-08-25T10:00:00Z",
        idempotency_key="ro-42-target-guard-preview",
    )

    assert result["execution"]["gateway_arguments"]["payload"]["card_id"] == "card-42"
    assert result["execution"]["gateway_arguments"]["payload"]["repair_order"] == {"client_id": "client-company"}


def test_document_contract_requires_type_or_request_text():
    result = prepare_action_contract(
        domain="document",
        action="generate",
        planned_changes={"manual_document": {"rows": []}},
        owner_intent="Создай документ в CRM",
        idempotency_key="document-missing-kind-v1",
    )

    assert result["ok"] is False
    assert "missing_request_text" in result["preflight"]["blocking_reasons"]


def test_document_contract_requires_request_text_even_with_explicit_type():
    result = prepare_action_contract(
        domain="document",
        action="generate",
        planned_changes={"document_type": "completion_act"},
        owner_intent="Создай акт выполненных работ в CRM",
        idempotency_key="document-type-without-request-v1",
    )

    assert result["ok"] is False
    assert "missing_request_text" in result["preflight"]["blocking_reasons"]


def test_existing_entity_update_requires_target_revision_and_idempotency():
    result = prepare_action_contract(
        domain="client",
        action="update",
        target_id="client-1",
        planned_changes={"name": "Иван"},
        owner_intent="Исправь имя клиента client-1",
    )

    assert result["ok"] is False
    assert result["preflight"]["blocking_reasons"] == [
        "missing_idempotency_key",
        "missing_expected_revision",
    ]


@pytest.mark.parametrize(
    ("domain", "action", "changes"),
    [
        ("store_quote_request", "assign_quote_request", {"assignee_id": "employee-7"}),
        ("store_quote_request", "set_quote_request_status", {"status": "WAITING_FOR_APPROVAL"}),
        ("store_quote_request", "update_quote_request_comment", {"internal_comment": "Проверить VIN"}),
        ("store_quote_request", "add_quote_request_note", {"text": "Нужно уточнить сторону"}),
        (
            "store_quote_request",
            "replace_quote_offer_drafts",
            {
                "items": [
                    {
                        "item_id": "item-1",
                        "drafts": [
                            {
                                "candidate_key": "rossko:abc",
                                "part_name": "Фильтр",
                                "sale_price": 1300,
                                "source_kind": "ROSSKO",
                                "price_basis": "CONFIRMED_PURCHASE",
                            }
                        ],
                    }
                ]
            },
        ),
        ("store_batch", "set_batch_storage_location", {"storage_location": "A-17"}),
        ("store_order", "mark_order_ready", {"status": "READY"}),
    ],
)
def test_store_action_contract_allowlist_uses_inventory_workflow_and_safe_transport(domain, action, changes):
    result = prepare_action_contract(
        domain=domain,
        action=action,
        target_id="exact-store-id",
        planned_changes=changes,
        owner_intent="Выполни точное разрешенное изменение объекта exact-store-id",
        expected_revision="2026-07-16T10:00:00+07:00",
        idempotency_key=f"{action}-exact-store-id-v1",
    )

    assert result["ok"] is True
    assert result["execution"]["tool"] == "agent_inventory_workflow"
    assert result["execution"]["operation"] == action
    payload = result["execution"]["gateway_arguments"]["payload"]
    assert payload["domain"] == domain
    assert payload["target_id"] == "exact-store-id"
    assert payload["expected_updated_at"] == "2026-07-16T10:00:00+07:00"
    assert payload["owner_intent"] == result["owner_intent"]
    assert payload["planned_changes"] == changes
    assert payload["correlation_id"] == result["correlation_id"]
    assert result["correlation_id"] != result["contract_id"]
    assert result["compensation"]["required"] is True
    assert result["ledger"]["store_refs_only"] is True
    assert "store_target_reread" in result["preflight"]["checks"]
    assert "store_scope_allowed" not in result["preflight"]["checks"]
    assert "store_revision_unchanged" in result["verification"]["checks"]
    assert "store_dry_run_receipt_recorded" in result["verification"]["checks"]
    assert "store_business_state_unchanged" in result["verification"]["checks"]


def test_store_contract_uses_stable_correlation_across_dry_run_and_apply_phases():
    common = {
        "domain": "store_quote_request",
        "action": "set_quote_request_status",
        "target_id": "quote-1",
        "planned_changes": {"status": "WAITING_FOR_APPROVAL"},
        "expected_revision": "version-1",
    }
    preview = prepare_action_contract(
        **common,
        owner_intent="Проверь перевод quote-1 в работу",
        idempotency_key="quote-1-progress-preview-v1",
        dry_run=True,
    )
    apply = prepare_action_contract(
        **common,
        owner_intent="Выполни перевод точной заявки quote-1 в работу",
        idempotency_key="quote-1-progress-apply-v1",
        dry_run=False,
    )

    assert preview["ok"] is True
    assert apply["ok"] is True
    assert preview["contract_id"] != apply["contract_id"]
    assert preview["correlation_id"] == apply["correlation_id"]
    assert 8 <= len(preview["correlation_id"]) <= 160
    assert preview["correlation_id"][0].isalnum()
    assert preview["execution"]["gateway_arguments"]["payload"]["correlation_id"] == preview["correlation_id"]
    assert apply["execution"]["gateway_arguments"]["payload"]["correlation_id"] == apply["correlation_id"]


def test_store_contract_accepts_only_alnum_first_correlation_between_8_and_160_chars():
    common = {
        "domain": "store_batch",
        "action": "set_batch_storage_location",
        "target_id": "batch-1",
        "planned_changes": {"storage_location": "A-17"},
        "owner_intent": "Поставь точную партию batch-1 на A-17",
        "expected_revision": "version-1",
        "idempotency_key": "batch-1-location-v1",
    }
    explicit = "S" + "a" * 159
    allowed = prepare_action_contract(**common, correlation_id=explicit)
    too_short = prepare_action_contract(**common, correlation_id="Store-1")
    bad_prefix = prepare_action_contract(**common, correlation_id=":store-123")
    too_long = prepare_action_contract(**common, correlation_id="S" + "a" * 160)

    assert allowed["ok"] is True
    assert allowed["correlation_id"] == explicit
    assert allowed["execution"]["gateway_arguments"]["payload"]["correlation_id"] == explicit
    assert "invalid_store_correlation_id" in too_short["preflight"]["blocking_reasons"]
    assert "invalid_store_correlation_id" in bad_prefix["preflight"]["blocking_reasons"]
    assert "invalid_store_correlation_id" in too_long["preflight"]["blocking_reasons"]


def test_store_ready_contract_discloses_notification_and_requires_explicit_ready_status():
    blocked = prepare_action_contract(
        domain="store_order",
        action="mark_order_ready",
        target_id="order-1",
        planned_changes={"status": "IN_PROGRESS"},
        owner_intent="Переведи заказ order-1 в READY",
        expected_revision="version-1",
        idempotency_key="order-1-ready-v1",
    )
    allowed = prepare_action_contract(
        domain="store_order",
        action="mark_order_ready",
        target_id="order-1",
        planned_changes={"status": "READY"},
        owner_intent="Переведи точный заказ order-1 в READY",
        expected_revision="version-1",
        idempotency_key="order-1-ready-v2",
    )

    assert "store_order_ready_status_required" in blocked["preflight"]["blocking_reasons"]
    assert "notification_effect_disclosed" in allowed["preflight"]["checks"]
    assert "notification_effect_disclosed" in allowed["verification"]["checks"]
    assert "store_order_ready_may_notify_customer" in allowed["warnings"]

    apply_contract = prepare_action_contract(
        domain="store_order",
        action="mark_order_ready",
        target_id="order-1",
        planned_changes={"status": "READY"},
        owner_intent="Переведи точный заказ order-1 в READY",
        expected_revision="version-1",
        idempotency_key="order-1-ready-v3",
        dry_run=False,
    )
    assert "store_updated_at_advanced_or_idempotent_replay" in apply_contract["verification"]["checks"]
    assert "store_audit_correlation_present" in apply_contract["verification"]["checks"]


@pytest.mark.parametrize(
    ("domain", "action", "raw_changes", "canonical_changes"),
    [
        ("store_quote_request", "assign_quote_request", {"assignee_id": " employee-7 "}, {"assignee_id": "employee-7"}),
        (
            "store_quote_request",
            "set_quote_request_status",
            {"status": " waiting_for_approval "},
            {"status": "WAITING_FOR_APPROVAL"},
        ),
        (
            "store_quote_request",
            "update_quote_request_comment",
            {"internal_comment": "  Проверить VIN  "},
            {"internal_comment": "Проверить VIN"},
        ),
        (
            "store_quote_request",
            "update_quote_request_comment",
            {"internal_comment": "   "},
            {"internal_comment": None},
        ),
        ("store_batch", "set_batch_storage_location", {"storage_location": " A-17 "}, {"storage_location": "A-17"}),
        ("store_order", "mark_order_ready", {"status": " ready "}, {"status": "READY"}),
    ],
)
def test_store_contract_normalizes_planned_changes_once_for_transport_and_readback(
    domain,
    action,
    raw_changes,
    canonical_changes,
):
    result = prepare_action_contract(
        domain=domain,
        action=action,
        target_id="store-id",
        planned_changes=raw_changes,
        owner_intent="Выполни точное изменение store-id",
        expected_revision="version-1",
        idempotency_key=f"canonical-{action}-{raw_changes!s}",
    )

    assert result["ok"] is True
    assert result["planned_changes"] == canonical_changes
    assert result["execution"]["gateway_arguments"]["payload"]["planned_changes"] == canonical_changes
    for field, value in canonical_changes.items():
        assert result["execution"]["gateway_arguments"]["payload"][field] == value


@pytest.mark.parametrize(
    ("domain", "action", "changes", "blocker"),
    [
        ("store_order", "delete", {"status": "READY"}, "unsupported_store_management_operation"),
        ("store_order", "mark_order_ready", {"price": 1}, "unsupported_store_change_fields"),
        ("store_quote_request", "set_quote_request_status", {"status": "NEW"}, "unsupported_store_quote_status"),
        ("store_quote_request", "set_quote_request_status", {"status": "COMPLETE"}, "unsupported_store_quote_status"),
        ("store_batch", "set_batch_storage_location", {"storage_location": ""}, "invalid_store_storage_location"),
    ],
)
def test_store_action_contract_blocks_non_allowlisted_or_malformed_changes(domain, action, changes, blocker):
    result = prepare_action_contract(
        domain=domain,
        action=action,
        target_id="store-id",
        planned_changes=changes,
        owner_intent="Измени объект store-id",
        expected_revision="version-1",
        idempotency_key=f"blocked-{domain}-{action}",
    )

    assert result["ok"] is False
    assert blocker in result["preflight"]["blocking_reasons"]


def test_action_contract_executor_tools_exist_in_the_tracked_crm_catalog():
    catalog = json.loads((ROOT / "docs" / "agent" / "crm_mcp_catalog.json").read_text(encoding="utf-8"))
    gateway_tools = {
        tool
        for tool in [*EXECUTOR_TOOLS.values(), *INVENTORY_EXECUTOR_TOOLS.values()]
        if tool.startswith("agent_") or tool in {"call_raw_capability"}
    }

    assert gateway_tools.issubset(set(catalog["expected_tool_names"]))
    assert EXECUTOR_TOOLS[("repair_order", "update")] == "agent_finance_workflow"


def test_inventory_executor_is_selected_by_movement_type_and_unknown_transfer_requires_discovery():
    inventory = prepare_action_contract(
        domain="inventory",
        action="adjust",
        target_id="inventory-1",
        planned_changes={"movement_type": "write_off", "quantity": 2, "card_id": "card-1"},
        owner_intent="Спиши две единицы inventory-1",
        expected_revision="inventory-1@7",
        idempotency_key="inventory-writeoff-1-v1",
    )
    transfer = prepare_action_contract(
        domain="cashbox",
        action="transfer",
        planned_changes={"amount": 1000, "cashbox_id": "cashbox-1", "target_cashbox_id": "cashbox-2"},
        owner_intent="Переведи 1000 между кассами",
        expected_revision="cashbox-1@9",
        idempotency_key="cash-transfer-1-2-v1",
    )

    assert inventory["execution"]["tool"] == "write_off_inventory_item"
    assert inventory["execution"]["ready"] is True
    assert transfer["execution"]["tool"] is None
    assert transfer["execution"]["ready"] is False
    assert "executor_tool_requires_capability_discovery" in transfer["warnings"]


def test_cashbox_lifecycle_contracts_do_not_require_transaction_fields():
    created = prepare_action_contract(
        domain="cashbox",
        action="create",
        planned_changes={"name": "Резервная касса"},
        owner_intent="Создай резервную кассу",
        expected_revision="board@9",
        idempotency_key="cashbox-create-reserve-v1",
    )
    deleted = prepare_action_contract(
        domain="cashbox",
        action="delete",
        target_id="cashbox-empty",
        owner_intent="Удали пустую кассу cashbox-empty",
        expected_revision="cashbox-empty@2",
        idempotency_key="cashbox-delete-empty-v1",
    )

    assert created["ok"] is True
    assert created["execution"]["tool"] == "create_cashbox"
    assert created["execution"]["ready"] is True
    assert created["compensation"]["strategy"] == "delete_empty_created_cashbox"
    assert deleted["ok"] is True
    assert deleted["execution"]["tool"] == "delete_cashbox"
    assert deleted["execution"]["ready"] is True
    assert deleted["compensation"]["strategy"] == "restore_empty_cashbox_from_verified_snapshot"
    for contract in (created, deleted):
        assert "invalid_positive_amount" not in contract["preflight"]["blocking_reasons"]
        assert "missing_cashbox_id" not in contract["preflight"]["blocking_reasons"]
        assert "amount_and_payment_method_valid" not in contract["preflight"]["checks"]
        assert "cash_journal_entry_exists" not in contract["verification"]["checks"]
        assert "repair_order_balance_reconciled" not in contract["verification"]["checks"]


def test_inventory_contract_rejects_non_positive_quantity_and_unknown_movement():
    invalid_quantity = prepare_action_contract(
        domain="inventory",
        action="adjust",
        target_id="inventory-1",
        planned_changes={"movement_type": "write_off", "quantity": -2, "card_id": "card-1"},
        owner_intent="Спиши остаток inventory-1",
        expected_revision="inventory-1@7",
        idempotency_key="inventory-negative-v1",
    )
    invalid_movement = prepare_action_contract(
        domain="inventory",
        action="adjust",
        target_id="inventory-1",
        planned_changes={"movement_type": "transfer", "quantity": 2},
        owner_intent="Перемести остаток inventory-1",
        expected_revision="inventory-1@7",
        idempotency_key="inventory-unknown-v1",
    )

    assert "invalid_positive_quantity" in invalid_quantity["preflight"]["blocking_reasons"]
    assert invalid_quantity["execution"]["ready"] is False
    assert "unsupported_inventory_movement_type" in invalid_movement["preflight"]["blocking_reasons"]
    assert invalid_movement["execution"]["ready"] is False


def test_inventory_contract_uses_operation_specific_requirements():
    missing_card = prepare_action_contract(
        domain="inventory",
        action="adjust",
        target_id="inventory-1",
        planned_changes={"movement_type": "write_off", "quantity": 2},
        owner_intent="Спиши две единицы inventory-1 в заказ-наряд",
        expected_revision="inventory-1@7",
        idempotency_key="inventory-writeoff-no-card-v1",
    )
    returned = prepare_action_contract(
        domain="inventory",
        action="adjust",
        target_id="movement-1",
        planned_changes={"movement_type": "return"},
        owner_intent="Верни складское движение movement-1",
        expected_revision="movement-1@7",
        idempotency_key="inventory-return-v1",
    )

    assert "missing_card_id" in missing_card["preflight"]["blocking_reasons"]
    assert missing_card["execution"]["ready"] is False
    assert returned["ok"] is True
    assert returned["execution"]["tool"] == "return_inventory_movement"
    assert returned["execution"]["ready"] is True


def test_cash_transaction_accepts_minor_units_and_localized_decimal_amount():
    minor = prepare_action_contract(
        domain="cashbox",
        action="cash_transaction",
        planned_changes={
            "cashbox_id": "cashbox-1",
            "direction": "expense",
            "amount_minor": 5_050,
            "note": "Расходник цеха",
        },
        owner_intent="Проведи расход 50,50 рубля из кассы cashbox-1",
        expected_revision="cashbox-1@9",
        idempotency_key="cashbox-expense-minor-v1",
    )
    localized = prepare_action_contract(
        domain="cashbox",
        action="cash_transaction",
        planned_changes={
            "cashbox_id": "cashbox-1",
            "direction": "income",
            "amount": "1 500,50",
        },
        owner_intent="Проведи приход 1500,50 в кассу cashbox-1",
        expected_revision="cashbox-1@9",
        idempotency_key="cashbox-income-localized-v1",
    )

    assert minor["ok"] is True
    assert minor["execution"]["ready"] is True
    assert localized["ok"] is True
    assert localized["execution"]["ready"] is True


def test_cash_transaction_rejects_number_strings_the_live_executor_cannot_parse_safely():
    for index, amount in enumerate(("1\u00a0500,50", "1 50,00", "1e3", "1_000")):
        result = prepare_action_contract(
            domain="cashbox",
            action="cash_transaction",
            planned_changes={"cashbox_id": "cashbox-1", "direction": "income", "amount": amount},
            owner_intent="Проведи приход в кассу cashbox-1",
            expected_revision="cashbox-1@9",
            idempotency_key=f"cashbox-unsafe-number-{index}",
        )

        assert result["ok"] is False
        assert "invalid_positive_amount" in result["preflight"]["blocking_reasons"]


def test_cash_transaction_rejects_invalid_direction_minor_units_and_short_expense_note():
    result = prepare_action_contract(
        domain="cashbox",
        action="cash_transaction",
        planned_changes={
            "cashbox_id": "cashbox-1",
            "direction": "out",
            "amount_minor": 50.5,
            "note": "коротко",
        },
        owner_intent="Проведи расход из кассы cashbox-1",
        expected_revision="cashbox-1@9",
        idempotency_key="cashbox-invalid-v1",
    )

    assert result["ok"] is False
    assert "invalid_positive_amount_minor" in result["preflight"]["blocking_reasons"]
    assert "invalid_cash_transaction_direction" in result["preflight"]["blocking_reasons"]

    for index, direction in enumerate(("INCOME", " income ")):
        invalid_literal = prepare_action_contract(
            domain="cashbox",
            action="cash_transaction",
            planned_changes={"cashbox_id": "cashbox-1", "direction": direction, "amount": 100},
            owner_intent="Проведи приход в кассу cashbox-1",
            expected_revision="cashbox-1@9",
            idempotency_key=f"cashbox-direction-literal-{index}",
        )
        assert "invalid_cash_transaction_direction" in invalid_literal["preflight"]["blocking_reasons"]

    short_expense = prepare_action_contract(
        domain="cashbox",
        action="cash_transaction",
        planned_changes={
            "cashbox_id": "cashbox-1",
            "direction": "expense",
            "amount": 100,
            "note": "коротко",
        },
        owner_intent="Проведи расход из кассы cashbox-1",
        expected_revision="cashbox-1@9",
        idempotency_key="cashbox-short-note-v1",
    )
    assert "expense_note_too_short" in short_expense["preflight"]["blocking_reasons"]

    for index, amount_minor in enumerate((50.0, "5050", True, 10**20)):
        unsafe_minor = prepare_action_contract(
            domain="cashbox",
            action="cash_transaction",
            planned_changes={
                "cashbox_id": "cashbox-1",
                "direction": "income",
                "amount_minor": amount_minor,
            },
            owner_intent="Проведи приход в копейках в кассу cashbox-1",
            expected_revision="cashbox-1@9",
            idempotency_key=f"cashbox-unsafe-minor-type-{index}",
        )
        assert "invalid_positive_amount_minor" in unsafe_minor["preflight"]["blocking_reasons"]


def test_direct_executor_contracts_require_their_mandatory_payload_fields():
    move = prepare_action_contract(
        domain="card",
        action="move",
        target_id="card-1",
        planned_changes={"before_card_id": "card-2"},
        owner_intent="Перемести card-1",
        expected_revision="card-1@4",
        idempotency_key="move-card-missing-column-v1",
    )
    deadline = prepare_action_contract(
        domain="card",
        action="set_deadline",
        target_id="card-1",
        planned_changes={"deadline": {}},
        owner_intent="Поставь дедлайн card-1",
        expected_revision="card-1@4",
        idempotency_key="deadline-card-missing-value-v1",
    )
    upload = prepare_action_contract(
        domain="file",
        action="upload",
        planned_changes={"mime_type": "application/pdf"},
        owner_intent="Загрузи PDF в CRM",
        idempotency_key="upload-file-missing-data-v1",
    )

    assert "missing_target_column_id" in move["preflight"]["blocking_reasons"]
    assert "missing_deadline" in deadline["preflight"]["blocking_reasons"]
    assert {"missing_file_name", "missing_content_base64"}.issubset(upload["preflight"]["blocking_reasons"])


def test_card_move_requires_live_connector_column_argument_name():
    result = prepare_action_contract(
        domain="card",
        action="move",
        target_id="card-1",
        planned_changes={"column_id": "column-2"},
        owner_intent="Перемести card-1 в column-2",
        expected_revision="card-1@4",
        idempotency_key="move-card-column-alias-v1",
    )

    assert result["ok"] is False
    assert "missing_target_column_id" in result["preflight"]["blocking_reasons"]
    assert result["execution"]["ready"] is False


def test_card_deadline_matches_live_connector_ranges_and_requires_positive_duration():
    def contract(deadline):
        return prepare_action_contract(
            domain="card",
            action="set_deadline",
            target_id="card-1",
            planned_changes={"deadline": deadline},
            owner_intent="Поставь дедлайн card-1",
            expected_revision="card-1@4",
            idempotency_key=f"deadline-card-{deadline!r}",
        )

    valid = contract({"days": 1, "minutes": 30})
    zero = contract({"total_seconds": 0})
    wrong_type = contract({"minutes": "30"})
    out_of_range = contract({"hours": 24})
    unknown = contract({"weeks": 1})

    assert valid["ok"] is True
    assert valid["execution"]["ready"] is True
    assert "invalid_positive_deadline" in zero["preflight"]["blocking_reasons"]
    assert "invalid_deadline_part" in wrong_type["preflight"]["blocking_reasons"]
    assert "invalid_deadline_part" in out_of_range["preflight"]["blocking_reasons"]
    assert "unsupported_deadline_field" in unknown["preflight"]["blocking_reasons"]
    assert "invalid_positive_deadline" in unknown["preflight"]["blocking_reasons"]


def test_raw_crm_create_contract_uses_schema_bound_raw_route_without_revision():
    result = prepare_action_contract(
        domain="crm",
        action="create_card",
        planned_changes={"title": "Запись на замену масла", "deadline": {"total_seconds": 160_982}},
        owner_intent="Создай карточку записи клиента на четверг",
        idempotency_key="create-card-appointment-v1",
    )

    assert result["ok"] is True
    assert result["concurrency"] == {"expected_revision": None, "required": False}
    assert result["execution"] == {
        "ready": True,
        "tool": "call_raw_capability",
        "operation": None,
        "gateway_arguments": {
            "raw_capability": "create_card",
            "arguments": {"title": "Запись на замену масла", "deadline": {"total_seconds": 160_982}},
            "idempotency_key": "create-card-appointment-v1",
            "requires_schema_discovery": True,
        },
        "external_connector": None,
        "response_mode": "compact",
    }
    assert "raw_capability_schema_hash_checked" in result["preflight"]["checks"]


def test_raw_crm_client_create_contract_is_a_collection_create():
    result = prepare_action_contract(
        domain="crm",
        action="create_client",
        planned_changes={"last_name": "Хондошко", "first_name": "Иван"},
        owner_intent="Создай найденного клиента без дубля",
        idempotency_key="create-client-khondoshko-v1",
    )

    assert result["ok"] is True
    assert result["concurrency"] == {"expected_revision": None, "required": False}
    assert result["execution"]["tool"] == "call_raw_capability"
    assert result["execution"]["gateway_arguments"]["raw_capability"] == "create_client"


def test_raw_crm_link_contract_requires_target_revision_and_uses_raw_route():
    result = prepare_action_contract(
        domain="crm",
        action="link_card_to_client",
        target_id="card-42",
        planned_changes={
            "card_id": "card-42",
            "client_id": "client-7",
            "expected_card_updated_at": "2026-07-28T10:00:00+00:00",
            "expected_client_updated_at": "2026-07-28T09:00:00+00:00",
        },
        owner_intent="Свяжи карточку с точным клиентом",
        expected_revision="2026-07-28T10:00:00+00:00",
        idempotency_key="link-card-42-client-7-v1",
    )

    assert result["ok"] is True
    assert result["concurrency"]["required"] is True
    assert result["execution"]["tool"] == "call_raw_capability"
    assert result["execution"]["gateway_arguments"]["raw_capability"] == "link_card_to_client"


def test_card_cleanup_contract_routes_exact_revision_to_named_workflow():
    result = prepare_action_contract(
        domain="board",
        action="cleanup_card",
        target_id="card-42",
        planned_changes={"description": "Обновлённая запись"},
        owner_intent="Обнови описание точной карточки",
        expected_revision="2026-07-28T10:00:00+00:00",
        idempotency_key="cleanup-card-42-v1",
    )

    assert result["ok"] is True
    assert result["execution"]["tool"] == "agent_board_workflow"
    assert result["execution"]["gateway_arguments"]["payload"] == {
        "description": "Обновлённая запись",
        "card_id": "card-42",
        "expected_updated_at": "2026-07-28T10:00:00+00:00",
    }


def test_active_board_timer_floor_contract_routes_to_named_workflow_without_revision():
    result = prepare_action_contract(
        domain="crm_board",
        action="bulk_set_deadline_if_below",
        target_id="active_cards",
        planned_changes={
            "include_archived": False,
            "min_total_seconds": 172800,
            "target_total_seconds": 173700,
        },
        owner_intent="Сделай всем активным карточкам таймер более двух суток",
        idempotency_key="active-board-timer-floor-v1",
    )

    assert result["ok"] is True
    assert result["domain"] == "board"
    assert result["concurrency"] == {"expected_revision": None, "required": False}
    assert result["execution"]["ready"] is True
    assert result["execution"]["tool"] == "agent_board_workflow"
    assert result["execution"]["operation"] == "bulk_set_deadline_if_below"
    assert result["execution"]["gateway_arguments"] == {
        "operation": "bulk_set_deadline_if_below",
        "payload": {
            "include_archived": False,
            "min_total_seconds": 172800,
            "target_total_seconds": 173700,
        },
        "idempotency_key": "active-board-timer-floor-v1",
        "mode": "dry_run",
    }


def test_active_board_timer_floor_contract_rejects_archive_scope_and_missing_buffer():
    result = prepare_action_contract(
        domain="board",
        action="bulk_set_deadline_if_below",
        planned_changes={
            "include_archived": True,
            "min_total_seconds": 172800,
            "target_total_seconds": 172800,
        },
        owner_intent="Подними таймеры активных карточек",
        idempotency_key="unsafe-board-timer-floor-v1",
    )

    assert result["ok"] is False
    assert "active_cards_only_required" in result["preflight"]["blocking_reasons"]
    assert "target_total_seconds_must_exceed_minimum" in result["preflight"]["blocking_reasons"]


def test_store_owner_api_contract_is_refs_only_and_routes_to_guarded_transport():
    result = prepare_action_contract(
        domain="store_owner_api",
        action="execute_owner_api",
        target_id="part-1",
        planned_changes={
            "operation_id": "update_part",
            "method": "PATCH",
            "path_template": "/api/v1/parts/{id}",
            "plan_hash": "f" * 64,
            "risk": "write",
            "schema_hash": "a" * 64,
            "concrete_path": "/api/v1/parts/part-1",
            "query_fields": [],
            "query_sha256": "b" * 64,
            "request_sha256": "c" * 64,
            "verification_class": "exact_entity",
            "body_fields": ["name", "salePrice"],
            "file_fields": [],
        },
        owner_intent="Обновить точную карточку товара part-1",
        expected_revision="2026-07-21T00:00:00Z",
        idempotency_key="store-owner-update-part-001",
        correlation_id="store-owner-update-part-001",
    )

    assert result["ok"] is True
    assert result["execution"]["ready"] is True
    assert result["execution"]["tool"] == "store_owner_api"
    assert "store_server_revision_matched" in result["verification"]["checks"]
    assert result["ledger"]["store_payload"] is False
    assert result["ledger"]["store_refs_only"] is True
    assert "store_exact_entity_reread" in result["verification"]["checks"]


def test_store_owner_contract_rejects_request_fingerprint_or_concrete_path_mismatch():
    result = prepare_action_contract(
        domain="store_owner_api",
        action="execute_owner_api",
        target_id="part-1",
        planned_changes={
            "operation_id": "update_part",
            "method": "PATCH",
            "path_template": "/api/v1/parts/{id}",
            "concrete_path": "/api/v1/customers/customer-1",
            "risk": "write",
            "schema_hash": "a" * 64,
            "query_fields": [],
            "query_sha256": "not-a-hash",
            "request_sha256": "b" * 64,
            "verification_class": "exact_entity",
            "body_fields": ["name"],
            "form_fields": [],
            "file_fields": [],
        },
        owner_intent="Обновить точный товар",
        expected_revision="2026-07-21T00:00:00Z",
        idempotency_key="store-owner-invalid-binding-001",
    )

    assert result["ok"] is False
    assert "invalid_store_owner_concrete_path" in result["preflight"]["blocking_reasons"]
    assert "invalid_store_owner_query_sha256" in result["preflight"]["blocking_reasons"]


def test_store_owner_api_contract_rejects_untyped_or_unversioned_request():
    result = prepare_action_contract(
        domain="store_owner_api",
        action="execute_owner_api",
        target_id="part-1",
        planned_changes={
            "operation_id": "update_part",
            "method": "TRACE",
            "path_template": "/api/v1/parts/{id}",
            "risk": "unknown",
            "schema_hash": "invalid",
            "raw_body": {"name": "forbidden"},
        },
        owner_intent="Обновить товар",
        idempotency_key="store-owner-invalid-001",
        correlation_id="store-owner-invalid-001",
    )

    assert result["ok"] is False
    assert "missing_expected_revision" in result["preflight"]["blocking_reasons"]
    assert "unsupported_store_change_fields" in result["preflight"]["blocking_reasons"]
    assert "invalid_store_owner_method" in result["preflight"]["blocking_reasons"]
    assert "invalid_store_owner_risk" in result["preflight"]["blocking_reasons"]
    assert "invalid_store_owner_schema_hash" in result["preflight"]["blocking_reasons"]


def test_reversible_store_owner_collection_create_does_not_require_fake_revision():
    result = prepare_action_contract(
        domain="store_owner_api",
        action="execute_owner_api",
        target_id="collection:/api/v1/categories",
        planned_changes={
            "operation_id": "create_category",
            "method": "POST",
            "path_template": "/api/v1/categories",
            "plan_hash": "e" * 64,
            "risk": "high_risk_write",
            "schema_hash": "b" * 64,
            "concrete_path": "/api/v1/categories",
            "query_fields": [],
            "query_sha256": "c" * 64,
            "request_sha256": "d" * 64,
            "verification_class": "collection_membership",
            "body_fields": ["name"],
            "file_fields": [],
        },
        owner_intent="Создать категорию по точной команде владельца",
        idempotency_key="store-owner-create-category-001",
        correlation_id="store-owner-create-category-001",
    )

    assert result["ok"] is True
    assert result["concurrency"] == {"expected_revision": None, "required": False}


def test_high_risk_store_owner_post_still_requires_current_revision():
    result = prepare_action_contract(
        domain="store_owner_api",
        action="execute_owner_api",
        target_id="warehouse-stock",
        planned_changes={
            "operation_id": "receive_batch",
            "method": "POST",
            "path_template": "/api/v1/warehouse/receipts/batch",
            "risk": "high_risk_write",
            "schema_hash": "c" * 64,
            "concrete_path": "/api/v1/warehouse/receipts/batch",
            "query_fields": [],
            "query_sha256": "d" * 64,
            "request_sha256": "e" * 64,
            "verification_class": "operation_specific_state",
            "body_fields": ["items"],
            "file_fields": [],
        },
        owner_intent="Принять точно проверенную партию",
        idempotency_key="store-owner-receive-batch-001",
        correlation_id="store-owner-receive-batch-001",
    )

    assert result["ok"] is False
    assert "missing_expected_revision" in result["preflight"]["blocking_reasons"]


@pytest.mark.parametrize(
    ("operation_id", "path_template"),
    [
        ("receive_batch", "/api/v1/warehouse/receipts/batch"),
        ("create_blocked_buyer", "/api/v1/customers/blocked-buyers"),
        ("export_marketplace_items", "/api/v1/marketplaces/exports"),
        ("future_unreviewed_create", "/api/v1/future-unreviewed-collection"),
    ],
)
def test_unreviewed_collection_post_cannot_bypass_revision_with_write_risk(
    operation_id: str,
    path_template: str,
):
    result = prepare_action_contract(
        domain="store_owner_api",
        action="execute_owner_api",
        target_id=f"collection:{path_template}",
        planned_changes={
            "operation_id": operation_id,
            "method": "POST",
            "path_template": path_template,
            # A stale or compromised classifier must not relax concurrency.
            "risk": "write",
            "schema_hash": "d" * 64,
            "concrete_path": path_template,
            "query_fields": [],
            "query_sha256": "e" * 64,
            "request_sha256": "f" * 64,
            "verification_class": "operation_specific_state",
            "body_fields": ["items"],
            "file_fields": [],
        },
        owner_intent="Выполнить проверенную операцию с точным состоянием",
        idempotency_key=f"store-owner-{operation_id}-001",
        correlation_id=f"store-owner-{operation_id}-001",
    )

    assert result["ok"] is False
    assert result["concurrency"] == {"expected_revision": None, "required": True}
    assert "missing_expected_revision" in result["preflight"]["blocking_reasons"]
