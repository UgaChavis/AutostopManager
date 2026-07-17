from __future__ import annotations

import json
from typing import Any

from .config import PROJECT_ROOT
from .context import build_agent_brief
from .knowledge_base import find_command_route
from .storage import ManagerMemoryStore


COMMAND_ROUTES_PATH = PROJECT_ROOT / "docs" / "agent" / "command_routes.json"


def agent_envelope(
    *,
    ok: bool,
    status: str,
    summary: dict[str, Any] | None = None,
    run_id: int | None = None,
    changes: list[dict[str, Any]] | None = None,
    verification: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    next_actions: list[str] | None = None,
    page: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the compact, stable envelope used by manager-side Agent Gateway tools."""

    return {
        "ok": bool(ok),
        "format": "agent_envelope_v2",
        "run_id": run_id,
        "status": str(status or ("completed" if ok else "failed")),
        "summary": summary or {},
        "changes": changes or [],
        "verification": verification or {},
        "warnings": list(dict.fromkeys(warnings or [])),
        "next_actions": next_actions or [],
        "page": page or {},
        "meta": {"response_mode": "compact", **(meta or {})},
    }


def list_agent_workflows(*, query: str = "", intent: str | None = None, limit: int = 50) -> dict[str, Any]:
    workflows = _load_workflows()
    selected = find_command_route(query, intent=intent) if query or intent else None
    limit = max(1, min(int(limit), 100))
    items = workflows[:limit]
    return agent_envelope(
        ok=True,
        status="completed",
        summary={
            "selected_workflow_id": (selected or {}).get("workflow_id") or (selected or {}).get("command_id"),
            "workflow_count": len(workflows),
            "items": items,
        },
        page={"limit": limit, "returned": len(items), "has_more": len(workflows) > len(items)},
        meta={"registry": "docs/agent/command_routes.json"},
    )


def build_agent_bootstrap(
    store: ManagerMemoryStore | None,
    *,
    query: str = "",
    intent: str | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    memory = store or ManagerMemoryStore()
    brief = build_agent_brief(memory, query, intent=intent, limit=limit)
    route = find_command_route(query, intent=intent)
    active = memory.list_active_manager_runs(limit=500).get("items", [])
    unfinished = [_compact_run(item) for item in active]
    selected = _compact_workflow(route) if route else None
    warnings: list[str] = []
    if not selected:
        warnings.append("workflow_not_resolved_use_list_agent_workflows_or_focused_reads")

    return agent_envelope(
        ok=True,
        status="ready",
        summary={
            "role": brief.get("role"),
            "intent": brief.get("intent"),
            "selected_workflow": selected,
            "source_boundaries": brief.get("source_boundaries", {}),
            "required_context": brief.get("required_context", []),
            "missing_context": brief.get("missing_context", []),
            "unfinished_runs": unfinished,
            "policy": {
                "full_owner_agent_capability": True,
                "owner_confirmation_state": False,
                "preflight_required": True,
                "idempotency_required": True,
                "optimistic_concurrency_required": True,
                "readback_verification_required": True,
                "external_email_bodies_in_ledger": False,
            },
        },
        warnings=warnings,
        next_actions=list(brief.get("next_actions") or []),
        meta={
            "workflow_registry_count": len(_load_workflows()),
            "workflow_registry_tool": "list_agent_workflows",
            "action_contract_tool": "prepare_action_contract",
        },
    )


def _load_workflows() -> list[dict[str, Any]]:
    try:
        payload = json.loads(COMMAND_ROUTES_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    routes = payload.get("routes") if isinstance(payload, dict) else []
    if not isinstance(routes, list):
        return []
    workflows = [_compact_workflow(item) for item in routes if isinstance(item, dict)]
    workflows.sort(key=lambda item: (-int(item.get("priority") or 0), str(item.get("workflow_id") or "")))
    return workflows


def _compact_workflow(route: dict[str, Any]) -> dict[str, Any]:
    workflow_id = str(route.get("workflow_id") or route.get("command_id") or "")
    return {
        "workflow_id": workflow_id,
        "intent": route.get("intent"),
        "domain": route.get("domain"),
        "priority": int(route.get("priority") or 0),
        "open_first": route.get("open_first"),
        "required_reads": _string_list(route.get("required_reads")),
        "write_domains": _string_list(route.get("write_domains")),
        "external_connectors": _string_list(route.get("external_connectors")),
        "completion_checks": _string_list(route.get("completion_checks")),
    }


def _compact_run(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": item.get("id"),
        "workflow_id": item.get("workflow_id") or item.get("intent"),
        "status": item.get("status"),
        "checkpoint": item.get("checkpoint", {}),
        "updated_at": item.get("updated_at"),
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]
