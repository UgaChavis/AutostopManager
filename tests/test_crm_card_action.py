from __future__ import annotations

from autostop_manager.crm_card_action import prepare_crm_card_action


def test_prepare_card_action_preserves_manual_vehicle_profile_values():
    result = prepare_crm_card_action(
        card_id="card-123",
        expected_updated_at="2026-06-08T10:00:00+07:00",
        vehicle_profile={
            "vin": "NEWVIN1234567890",
            "engine_model": "N63TU",
            "autofilled_fields": ["vin", "engine_model"],
            "source_summary": "VIN decode",
        },
        current_card={
            "updated_at": "2026-06-08T10:00:00+07:00",
            "vehicle_profile": {"vin": "OLDVIN1234567890", "manual_fields": ["vin"]},
        },
    )

    patch = result["planned_patch"]["vehicle_profile"]
    assert "vin" not in patch
    assert patch["engine_model"] == "N63TU"
    assert patch["manual_fields"] == ["vin"]
    assert patch["autofilled_fields"] == ["engine_model"]
    assert "vehicle_profile_patch_touches_manual_field" not in result["risk_flags"]


def test_prepare_card_action_flags_long_board_summary():
    result = prepare_crm_card_action(
        card_id="card-123",
        expected_updated_at="2026-06-08T10:00:00+07:00",
        board_summary="1\n2\n3\n4\n5\n6",
    )

    assert "board_summary_too_many_lines" in result["risk_flags"]


def test_prepare_card_action_flags_private_data_in_board_summary():
    result = prepare_crm_card_action(
        card_id="card-123",
        expected_updated_at="2026-06-08T10:00:00+07:00",
        board_summary="Клиент +7 913 123-45-67\nVIN JH4DA9350LS000000",
    )

    assert "board_summary_contains_private_identifier" in result["risk_flags"]
    assert result["ok"] is False
    assert result["ready_to_apply"] is False


def test_prepare_card_action_fails_closed_without_concurrency_token_or_patch():
    result = prepare_crm_card_action(card_id="card-123")

    assert result["ok"] is False
    assert result["ready_to_apply"] is False
    assert result["blocking_risk_flags"] == ["missing_expected_updated_at", "no_target_fields"]


def test_prepare_card_action_is_only_a_dry_run_planner():
    result = prepare_crm_card_action(
        card_id="card-123",
        expected_updated_at="2026-06-08T10:00:00+07:00",
        description="Проверить течь",
        dry_run=False,
    )

    assert result["ok"] is False
    assert "planner_apply_mode_not_supported" in result["blocking_risk_flags"]
