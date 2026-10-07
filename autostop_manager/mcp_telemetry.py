"""Per-call MCP timing with a content-free stderr trace."""

from __future__ import annotations

import inspect
import json
import logging
import math
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from functools import wraps
from typing import Any
from uuid import uuid4

from mcp.types import CallToolResult, TextContent


LOGGER = logging.getLogger(__name__)
if not LOGGER.handlers:
    LOGGER.addHandler(logging.StreamHandler())  # stderr; never corrupt stdio MCP.
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    if isinstance(value, int):
        return value if value <= 2**53 - 1 else None
    if not math.isfinite(value):
        return None
    return value


def _finish(
    tool_name: str, call_id: str, start: str, tick: float, result: Any, *, failed: bool = False
) -> dict[str, Any]:
    failed = failed or (isinstance(result, CallToolResult) and result.isError)
    payload = result.structuredContent if isinstance(result, CallToolResult) else result
    payload = payload if isinstance(payload, Mapping) else {}
    execution = payload.get("execution")
    execution = execution if isinstance(execution, Mapping) else {}
    processing = payload.get("processing")
    processing = processing if isinstance(processing, Mapping) else {}
    attempts = execution.get("attempts", execution.get("attempt_count", payload.get("attempt_count")))
    trace = {
        "tool": tool_name,
        "call_id": call_id,
        "started_at": start,
        "ended_at": _utc_now(),
        "wall_ms": round(max(0, time.perf_counter() - tick) * 1000, 3),
        "backend_elapsed_ms": _number(execution.get("elapsed_ms")),
        "processing_elapsed_ms": _number(processing.get("elapsed_ms")),
        "network_calls": _number(execution.get("network_calls", execution.get("network_request_count"))),
        "provider_network_attempt_count": _number(execution.get("provider_network_attempt_count")),
        "attempt_count": len(attempts) if isinstance(attempts, list) else _number(attempts),
        "outcome": "error"
        if failed or payload.get("ok") is False
        else "ok"
        if payload.get("ok") is True
        else "unknown",
    }
    LOGGER.info(json.dumps({"event": "mcp_tool_end", **trace}, separators=(",", ":")))
    return trace


def _attach(result: Any, trace: dict[str, Any]) -> Any:
    if isinstance(result, dict):
        return {**result, "tool_execution": trace}
    if not isinstance(result, CallToolResult):
        return result
    if not isinstance(result.structuredContent, dict):
        return result.model_copy(update={"meta": {**(result.meta or {}), "tool_execution": trace}})
    payload = {**result.structuredContent, "tool_execution": trace}
    content = result.content
    # Existing compact JSON text and structured channels must remain identical.
    if len(content) == 1 and isinstance(content[0], TextContent):
        try:
            same_payload = json.loads(content[0].text) == result.structuredContent
        except (ValueError, TypeError):
            same_payload = False
        if same_payload:
            content = [
                content[0].model_copy(update={"text": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))})
            ]
    return result.model_copy(update={"structuredContent": payload, "content": content})


def traced_tool(function: Callable[..., Any], tool_name: str) -> Callable[..., Any]:
    """Wrap execution after registration, preserving all SDK input metadata."""
    if getattr(function, "_manager_execution_trace", False):
        return function

    def start() -> tuple[str, str, float]:
        call_id, started_at, tick = str(uuid4()), _utc_now(), time.perf_counter()
        LOGGER.info(
            json.dumps(
                {"event": "mcp_tool_start", "tool": tool_name, "call_id": call_id, "started_at": started_at},
                separators=(",", ":"),
            )
        )
        return call_id, started_at, tick

    if inspect.iscoroutinefunction(function):

        @wraps(function)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            call_id, started_at, tick = start()
            try:
                result = await function(*args, **kwargs)
            except BaseException:
                _finish(tool_name, call_id, started_at, tick, None, failed=True)
                raise
            return _attach(result, _finish(tool_name, call_id, started_at, tick, result))
    else:

        @wraps(function)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            call_id, started_at, tick = start()
            try:
                result = function(*args, **kwargs)
            except BaseException:
                _finish(tool_name, call_id, started_at, tick, None, failed=True)
                raise
            return _attach(result, _finish(tool_name, call_id, started_at, tick, result))

    wrapped._manager_execution_trace = True  # type: ignore[attr-defined]
    return wrapped


def instrument_manager_tools(server: Any) -> None:
    manager = getattr(server, "_tool_manager", None)
    registry = getattr(manager, "_tools", None)
    if isinstance(registry, Mapping):
        for name, tool in registry.items():
            tool.fn = traced_tool(tool.fn, str(name))
    elif isinstance(getattr(server, "tools", None), dict):
        for name, function in server.tools.items():
            server.tools[name] = traced_tool(function, str(name))
