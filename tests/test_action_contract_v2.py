from __future__ import annotations

import json
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-Infinity", True])
def test_payment_action_contract_rejects_non_finite_or_boolean_amounts(amount):
    result = prepare_action_contract(
        domain="payment",
        action="record_payment",
        planned_changes={
            "amount": amount,
            "cashbox_id": "cashbox-main",
            "card_id": "card-42",
            "payment_method": "cash",
            "outstanding_amount": 12_500,
            "allow_overpayment": True,
        },
        owner_intent="Проведи оплату по ro-42",
        expected_revision="ro-42@2026-07-11T10:00:00+07:00",
        idempotency_key=f"payment-invalid-{amount!s}",
    )

    assert result["ok"] is False
    assert "invalid_positive_amount" in result["preflight"]["blocking_reasons"]


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
        tool for tool in [*EXECUTOR_TOOLS.values(), *INVENTORY_EXECUTOR_TOOLS.values()] if not tool.startswith("gmail:")
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
        planned_changes={"movement_type": "write_off", "quantity": 2},
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
