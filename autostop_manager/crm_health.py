from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .storage import _now


DEFAULT_OVERLOAD_THRESHOLD = 8
DONE_STATUSES = {"done", "closed", "completed", "archived", "cancelled", "canceled", "готово", "закрыто"}


def build_crm_health_plan(
    *,
    board_context: dict[str, Any] | None = None,
    board_review: dict[str, Any] | None = None,
    today_context: dict[str, Any] | None = None,
    now: str | datetime | None = None,
    overload_threshold: int = DEFAULT_OVERLOAD_THRESHOLD,
) -> dict[str, Any]:
    """Build a read-only CRM health plan from already fetched payloads."""

    generated_at = _now()
    now_dt = _parse_datetime(now) or datetime.now(timezone.utc)
    review = _unwrap_payload(board_review)
    context = _unwrap_payload(board_context)
    today = _unwrap_payload(today_context)

    overloaded_columns = _overloaded_columns(
        _extract_columns(review, context),
        threshold=max(1, overload_threshold),
    )
    event_noise = _event_noise(_extract_events(review, context, today))
    stale_tasks = _stale_tasks(_extract_tasks(today), now_dt)
    suggested_actions = _suggested_actions(overloaded_columns, stale_tasks, event_noise)

    return {
        "ok": True,
        "mode": "read_only",
        "generated_at": generated_at,
        "overload_threshold": max(1, overload_threshold),
        "overloaded_columns": overloaded_columns,
        "stale_tasks": stale_tasks,
        "event_noise": event_noise,
        "suggested_actions": suggested_actions,
        "verification": {
            "cards_moved": 0,
            "cards_archived": 0,
            "crm_writes": 0,
            "live_connector_called": False,
        },
    }


def _unwrap_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    for key in ("data", "result", "payload"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return payload


def _extract_columns(*payloads: dict[str, Any]) -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for payload in payloads:
        for raw_column in _column_sources(payload):
            if not isinstance(raw_column, dict):
                continue
            column_id = str(raw_column.get("column_id") or raw_column.get("id") or raw_column.get("columnId") or "")
            label = str(raw_column.get("label") or raw_column.get("name") or raw_column.get("title") or column_id)
            count = _column_count(raw_column)
            key = (column_id, label)
            if key in seen:
                continue
            seen.add(key)
            columns.append({"column_id": column_id, "label": label, "count": count})
    return columns


def _column_sources(payload: dict[str, Any]) -> list[Any]:
    if isinstance(payload.get("by_column"), list):
        return list(payload["by_column"])
    if isinstance(payload.get("columns"), list):
        return list(payload["columns"])
    if isinstance(payload.get("column_counts"), list):
        return list(payload["column_counts"])
    if isinstance(payload.get("column_counts"), dict):
        return [
            {"column_id": key, "label": key, "count": value}
            for key, value in payload["column_counts"].items()
        ]
    return []


def _column_count(column: dict[str, Any]) -> int:
    for key in ("count", "card_count", "cards_count", "total"):
        value = column.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    cards = column.get("cards")
    return len(cards) if isinstance(cards, list) else 0


def _overloaded_columns(columns: list[dict[str, Any]], *, threshold: int) -> list[dict[str, Any]]:
    overloaded = []
    for column in columns:
        count = int(column.get("count") or 0)
        if count < threshold:
            continue
        overloaded.append(
            {
                **column,
                "risk": "high" if count >= threshold * 2 else "medium",
                "matched_by": f"card_count >= {threshold}",
            }
        )
    return overloaded


def _extract_events(*payloads: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for payload in payloads:
        for key in ("recent_events", "events", "board_events"):
            value = payload.get(key)
            if isinstance(value, list):
                events.extend(item for item in value if isinstance(item, dict))
    return events


def _event_noise(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    noise = []
    for event in events:
        haystack = " ".join(
            str(event.get(key) or "")
            for key in ("actor_name", "actor", "type", "text", "message", "title", "card_short_id")
        ).lower()
        if "codex mcp qa" not in haystack and "test mcp qa" not in haystack:
            continue
        noise.append(
            {
                "timestamp": event.get("timestamp") or event.get("created_at"),
                "actor_name": event.get("actor_name") or event.get("actor"),
                "type": event.get("type"),
                "card_short_id": event.get("card_short_id"),
                "text": event.get("text") or event.get("message") or event.get("title"),
                "reason": "qa_event_noise",
            }
        )
    return noise


def _extract_tasks(today: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = []
    for key in ("tasks", "live_tasks", "items"):
        value = today.get(key)
        if isinstance(value, list):
            tasks.extend(item for item in value if isinstance(item, dict))
    return tasks


def _stale_tasks(tasks: list[dict[str, Any]], now_dt: datetime) -> list[dict[str, Any]]:
    stale = []
    for task in tasks:
        status = str(task.get("status") or "open").lower()
        if status in DONE_STATUSES:
            continue
        due_at = task.get("due_at") or task.get("due") or task.get("remind_at")
        due_dt = _parse_datetime(due_at)
        if due_dt is None or due_dt >= now_dt:
            continue
        days_overdue = max(0, (now_dt - due_dt).days)
        stale.append(
            {
                "id": task.get("id"),
                "title": task.get("title") or task.get("content") or task.get("event") or "",
                "status": task.get("status") or "open",
                "due_at": due_at,
                "days_overdue": days_overdue,
                "reason": "open_task_due_in_past",
            }
        )
    return stale


def _parse_datetime(value: str | datetime | Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _suggested_actions(
    overloaded_columns: list[dict[str, Any]],
    stale_tasks: list[dict[str, Any]],
    event_noise: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for column in overloaded_columns:
        actions.append(
            {
                "category": "overloaded_column",
                "target": column["label"],
                "action": "review_cards_for_missing_next_step_deadline_or_board_summary",
                "requires_owner_approval": False,
            }
        )
    for task in stale_tasks:
        actions.append(
            {
                "category": "stale_task",
                "target": task["title"],
                "action": "refresh_or_close_manager_task_after_owner_confirmation",
                "requires_owner_approval": True,
            }
        )
    if event_noise:
        actions.append(
            {
                "category": "event_noise",
                "target": "recent_events",
                "action": "discount_codex_mcp_qa_events_when_interpreting_live_board_activity",
                "requires_owner_approval": False,
            }
        )
    if not actions:
        actions.append(
            {
                "category": "routine",
                "target": "crm_board",
                "action": "keep_read_only_monitoring_until_owner_confirms_write_scope",
                "requires_owner_approval": False,
            }
        )
    return actions
