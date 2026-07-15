from __future__ import annotations

import json
from pathlib import Path

from autostop_manager.action_contract import EXECUTOR_TOOLS, INVENTORY_EXECUTOR_TOOLS, prepare_action_contract


ROOT = Path(__file__).resolve().parents[1]


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


def test_gmail_action_contract_uses_external_connector_without_confirmation_state():
    result = prepare_action_contract(
        domain="gmail",
        action="send",
        planned_changes={
            "recipients": ["client@example.com"],
            "subject": "Документы по автомобилю",
            "body_intent": "Отправить проверенный PDF заказ-наряда",
            "attachment_ids": ["crm-file-7"],
        },
        owner_intent="Отправь клиенту заказ-наряд из карточки C-7",
        idempotency_key="gmail-send-c7-ro-v1",
    )

    assert result["ok"] is True
    assert result["execution"]["external_connector"] == "gmail"
    assert result["execution"]["tool"] == "gmail:_send_email"
    assert "owner_confirmation" not in str(result).casefold()
    assert result["ledger"]["store_refs_only"] is True


def test_gmail_send_accepts_current_to_shape_and_rejects_non_string_values():
    current = prepare_action_contract(
        domain="gmail",
        action="send",
        planned_changes={
            "to": "client@example.com",
            "subject": "Документы по автомобилю",
            "body_intent": "Отправить проверенный PDF заказ-наряда",
        },
        owner_intent="Отправь клиенту заказ-наряд",
        idempotency_key="gmail-send-current-shape-v1",
    )
    unsafe = prepare_action_contract(
        domain="gmail",
        action="send",
        planned_changes={"recipients": [123], "subject": 456, "body_intent": 789},
        owner_intent="Отправь письмо клиенту",
        idempotency_key="gmail-send-invalid-types-v1",
    )

    assert current["ok"] is True
    assert current["execution"]["ready"] is True
    assert unsafe["ok"] is False
    assert {
        "missing_exact_recipients",
        "missing_subject",
        "missing_body_intent",
    }.issubset(unsafe["preflight"]["blocking_reasons"])


def test_gmail_forward_accepts_current_message_ids_and_to_shape():
    result = prepare_action_contract(
        domain="gmail",
        action="forward",
        planned_changes={
            "message_ids": ["message-1", "message-2"],
            "to": "manager@example.com",
            "note": "Для ознакомления",
        },
        owner_intent="Перешли два выбранных письма руководителю",
        idempotency_key="gmail-forward-current-shape-v1",
    )

    assert result["ok"] is True
    assert result["execution"]["tool"] == "gmail:_forward_emails"
    assert result["execution"]["ready"] is True


def test_gmail_label_uses_exact_message_ids_and_current_label_name_shape():
    result = prepare_action_contract(
        domain="gmail",
        action="label",
        planned_changes={
            "message_ids": ["message-1", "message-2"],
            "add_label_names": ["Заказ-наряды"],
            "remove_label_names": ["Входящие на разбор"],
            "create_missing_labels": False,
        },
        owner_intent="Переметь два выбранных письма как заказ-наряды",
        idempotency_key="gmail-label-selected-v1",
    )

    assert result["ok"] is True
    assert result["concurrency"]["required"] is False
    assert result["target"]["id"] is None
    assert result["execution"]["tool"] == "gmail:_apply_labels_to_emails"
    assert result["execution"]["ready"] is True


def test_gmail_label_rejects_legacy_ambiguous_or_malformed_targets():
    cases = (
        {"message_ids": "message-1", "add_label_names": ["Заказ-наряды"]},
        {"message_ids": [123], "add_label_names": ["Заказ-наряды"]},
        {"message_ids": ["message-1"], "add_label_names": [""]},
        {"message_ids": ["message-1"], "label_ids": ["Label_1"]},
    )
    for index, planned_changes in enumerate(cases):
        result = prepare_action_contract(
            domain="gmail",
            action="label",
            planned_changes=planned_changes,
            owner_intent="Переметь выбранное письмо",
            idempotency_key=f"gmail-label-invalid-target-{index}",
        )

        assert result["ok"] is False
        assert "missing_message_or_label_ids" in result["preflight"]["blocking_reasons"]

    invalid_flag = prepare_action_contract(
        domain="gmail",
        action="label",
        planned_changes={
            "message_ids": ["message-1"],
            "add_label_names": ["Заказ-наряды"],
            "create_missing_labels": "false",
        },
        owner_intent="Переметь выбранное письмо",
        idempotency_key="gmail-label-invalid-create-flag-v1",
    )
    assert "invalid_create_missing_labels_flag" in invalid_flag["preflight"]["blocking_reasons"]


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


def test_action_contract_executor_tools_exist_in_the_tracked_crm_catalog():
    catalog = json.loads((ROOT / "docs" / "agent" / "crm_mcp_catalog.json").read_text(encoding="utf-8"))

    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for nested in value.values():
                yield from strings(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from strings(nested)

    catalog_strings = set(strings(catalog))
    configured = {
        tool
        for tool in [*EXECUTOR_TOOLS.values(), *INVENTORY_EXECUTOR_TOOLS.values()]
        if not tool.startswith("gmail:")
    }

    assert configured.issubset(catalog_strings)
    assert "record_repair_order_payment" not in configured
    assert "transfer_between_cashboxes" not in configured
    assert "adjust_inventory" not in configured
    assert "upload_file" not in configured
    assert "merge_clients" not in configured


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
