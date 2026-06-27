from __future__ import annotations

import re
from typing import Any


CARD_ACTION_LEDGER_SCHEMA = [
    "pre_state_ref",
    "planned_patch",
    "write_result",
    "post_state",
    "diff",
    "verification_checks",
    "warnings",
]

CARD_ACTION_TOOL_SEQUENCE = [
    "start_manager_run",
    "agent_brief",
    "get_card_context",
    "prepare_crm_card_action",
    "update_card",
    "set_card_board_summary",
    "get_card_context",
    "record_manager_run_event",
    "finish_manager_run",
]

VEHICLE_PROFILE_META_FIELDS = {
    "manual_fields",
    "autofilled_fields",
    "tentative_fields",
    "field_sources",
    "source_summary",
    "source_confidence",
    "source_links_or_refs",
    "raw_input_text",
    "raw_image_text",
    "image_parse_status",
    "warnings",
    "data_completion_state",
}

VIN_LIKE_PATTERN = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b", re.IGNORECASE)
PHONE_LIKE_PATTERN = re.compile(r"(?<!\d)(?:\+?\d[\s().-]*){10,15}(?!\d)")


def prepare_crm_card_action(
    *,
    card_id: str,
    expected_updated_at: str | None = None,
    description: str | None = None,
    vehicle_profile: dict[str, Any] | None = None,
    board_summary: str | None = None,
    target_fields: list[str] | None = None,
    current_card: dict[str, Any] | None = None,
    intent: str = "board_cleanup",
    dry_run: bool = True,
) -> dict[str, Any]:
    card_id = str(card_id or "").strip()
    current = current_card if isinstance(current_card, dict) else {}
    current_profile = (
        current.get("vehicle_profile") if isinstance(current.get("vehicle_profile"), dict) else {}
    )
    effective_expected_updated_at = str(
        expected_updated_at or current.get("updated_at") or ""
    ).strip()

    planned_patch: dict[str, Any] = {}
    if description is not None:
        planned_patch["description"] = str(description)
    if isinstance(vehicle_profile, dict):
        planned_patch["vehicle_profile"] = _vehicle_profile_patch_with_preserved_manual_fields(
            vehicle_profile,
            current_profile,
        )

    summary_patch = str(board_summary) if board_summary is not None else None
    inferred_targets = _infer_target_fields(planned_patch, summary_patch)
    requested_targets = _normalize_target_fields(target_fields)
    target_fields_final = requested_targets or inferred_targets
    risk_flags = _risk_flags(
        card_id=card_id,
        expected_updated_at=effective_expected_updated_at,
        planned_patch=planned_patch,
        board_summary=summary_patch,
        current_profile=current_profile,
    )

    return {
        "ok": bool(card_id),
        "format": "crm_card_action_v1",
        "intent": str(intent or "board_cleanup"),
        "dry_run": bool(dry_run),
        "card_id": card_id,
        "target_fields": target_fields_final,
        "planned_patch": planned_patch,
        "board_summary": summary_patch,
        "write_contract": {
            "tool": "update_card",
            "card_id": card_id,
            "expected_updated_at": effective_expected_updated_at or None,
            "response_mode": "compact",
            "target_fields": [field for field in target_fields_final if field != "board_summary"],
            "requires_actor_name": True,
        },
        "summary_contract": {
            "tool": "set_card_board_summary",
            "card_id": card_id,
            "response_mode": "compact",
            "required": summary_patch is not None,
        },
        "verification_spec": _verification_spec(
            planned_patch=planned_patch,
            board_summary=summary_patch,
            current_profile=current_profile,
        ),
        "risk_flags": risk_flags,
        "ledger_event_schema": CARD_ACTION_LEDGER_SCHEMA,
        "tool_sequence": CARD_ACTION_TOOL_SEQUENCE,
    }


def _vehicle_profile_patch_with_preserved_manual_fields(
    patch: dict[str, Any],
    current_profile: dict[str, Any],
) -> dict[str, Any]:
    result = dict(patch)
    manual_fields = set(_string_list(current_profile.get("manual_fields")))
    manual_fields.update(_string_list(patch.get("manual_fields")))
    if manual_fields:
        result["manual_fields"] = sorted(manual_fields)

    for field_name in sorted(manual_fields):
        if field_name in result and field_name not in VEHICLE_PROFILE_META_FIELDS:
            current_value = current_profile.get(field_name)
            if current_value not in (None, "", [], {}):
                result.pop(field_name, None)

    for field_name in ("autofilled_fields", "tentative_fields"):
        if field_name in result:
            result[field_name] = sorted(set(_string_list(result.get(field_name))) - manual_fields)
    if "tentative_fields" in result and "autofilled_fields" in result:
        autofilled = set(_string_list(result.get("autofilled_fields")))
        result["tentative_fields"] = sorted(set(_string_list(result.get("tentative_fields"))) & autofilled)
    return result


def _infer_target_fields(planned_patch: dict[str, Any], board_summary: str | None) -> list[str]:
    fields = list(planned_patch)
    if board_summary is not None:
        fields.append("board_summary")
    return fields


def _normalize_target_fields(target_fields: list[str] | None) -> list[str]:
    if not isinstance(target_fields, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for field in target_fields:
        name = str(field or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


def _verification_spec(
    *,
    planned_patch: dict[str, Any],
    board_summary: str | None,
    current_profile: dict[str, Any],
) -> dict[str, Any]:
    checks = ["expected_updated_at_conflict", "no_unplanned_card_fields"]
    spec: dict[str, Any] = {"checks": checks}
    if "description" in planned_patch:
        checks.extend(["description_exact", "description_visible_text"])
        description = planned_patch["description"]
        spec["description_expected"] = description
        spec["description_visible_text_expected"] = _visible_description_text(description)
    if "vehicle_profile" in planned_patch:
        checks.append("vehicle_profile_field_level")
        profile_patch = planned_patch["vehicle_profile"]
        spec["vehicle_profile_expected_fields"] = profile_patch
        spec["manual_fields_to_preserve"] = _string_list(current_profile.get("manual_fields"))
    if board_summary is not None:
        checks.extend(["board_summary_exact", "board_summary_stale_false"])
        spec["board_summary_expected"] = board_summary
    return spec


def _risk_flags(
    *,
    card_id: str,
    expected_updated_at: str,
    planned_patch: dict[str, Any],
    board_summary: str | None,
    current_profile: dict[str, Any],
) -> list[str]:
    flags: list[str] = []
    if not card_id:
        flags.append("missing_card_id")
    if not expected_updated_at:
        flags.append("missing_expected_updated_at")
    if not planned_patch and board_summary is None:
        flags.append("no_target_fields")
    description = planned_patch.get("description")
    if isinstance(description, str):
        if re.search(r"<[^>]+>", description):
            flags.append("description_contains_raw_html")
        if re.search(r"(?im)^\s*(статус|следующий шаг)\s*:", description):
            flags.append("description_contains_deprecated_blocks")
    if board_summary and _has_rich_formatting(board_summary):
        flags.append("board_summary_contains_rich_formatting")
    if board_summary and _board_summary_line_count(board_summary) > 5:
        flags.append("board_summary_too_many_lines")
    if board_summary and _has_private_board_summary_data(board_summary):
        flags.append("board_summary_contains_private_identifier")
    profile_patch = planned_patch.get("vehicle_profile")
    if isinstance(profile_patch, dict):
        primary_fields = _vehicle_profile_primary_fields(profile_patch)
        manual_fields = set(_string_list(current_profile.get("manual_fields")))
        touched_manual = sorted(set(primary_fields) & manual_fields)
        if touched_manual:
            flags.append("vehicle_profile_patch_touches_manual_field")
        if primary_fields and not _has_vehicle_profile_source_metadata(profile_patch):
            flags.append("vehicle_profile_missing_source_metadata")
    return flags


def _has_rich_formatting(value: str) -> bool:
    text = str(value or "")
    return any(marker in text for marker in ("**", "++", "<", ">"))


def _board_summary_line_count(value: str) -> int:
    return sum(1 for line in str(value or "").splitlines() if line.strip())


def _has_private_board_summary_data(value: str) -> bool:
    text = str(value or "")
    return bool(VIN_LIKE_PATTERN.search(text) or PHONE_LIKE_PATTERN.search(text))


def _vehicle_profile_primary_fields(profile: dict[str, Any]) -> list[str]:
    return [
        str(key)
        for key, value in profile.items()
        if key not in VEHICLE_PROFILE_META_FIELDS and value not in (None, "", [], {})
    ]


def _has_vehicle_profile_source_metadata(profile: dict[str, Any]) -> bool:
    return any(
        key in profile and profile.get(key) not in (None, "", [], {})
        for key in ("autofilled_fields", "tentative_fields", "field_sources", "source_summary", "source_confidence")
    )


def _visible_description_text(value: Any) -> str:
    text = str(value if value is not None else "")
    for _ in range(4):
        previous = text
        text = re.sub(r"\+\+([\s\S]+?)\+\+", r"\1", text)
        text = re.sub(r"\*\*([\s\S]+?)\*\*", r"\1", text)
        text = re.sub(r"(^|[^*])\*([^*\n]+?)\*(?!\*)", r"\1\2", text)
        if text == previous:
            break
    return text


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
