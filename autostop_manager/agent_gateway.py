from __future__ import annotations

from typing import Any

from .config import PROJECT_ROOT


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
