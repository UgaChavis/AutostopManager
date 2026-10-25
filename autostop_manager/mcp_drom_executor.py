"""Bound native Drom callback waits without repeating vendor operations."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import copy_context
from functools import partial, wraps
from typing import Any

from .listing_executor import BoundedListingExecutor, ListingExecutionError


DROM_BLOCKING_TOOLS = frozenset({"drom_start_parts_search", "drom_get_parts_search"})
_START_EXECUTOR = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=90)
_GET_EXECUTOR = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=50)


def _execution_failure(tool_name: str, error: ListingExecutionError) -> dict[str, Any]:
    start = tool_name == "drom_start_parts_search"
    prefix = "drom_start_executor_" if start else "drom_get_executor_"
    suffix = {
        "provider_busy": "busy",
        "provider_wait_timeout": "wait_timeout",
        "provider_execution_failed": "failed",
        "provider_executor_unavailable": "unavailable",
        "provider_executor_draining": "draining",
    }.get(error.code, "unavailable")
    return {
        "ok": False,
        "source": "drom",
        "status": "uncertain" if start and error.adapter_started else "failed",
        "error": prefix + suffix,
        "task_id": None,
        "uid": None,
        "outcome_uncertain": error.adapter_started,
    }


def offload_drom(function: Callable[..., Any], tool_name: str) -> Callable[..., Any]:
    """Offload the original callback, including configuration and dry-run guards."""

    @wraps(function)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        context = copy_context()
        pool = _START_EXECUTOR if tool_name == "drom_start_parts_search" else _GET_EXECUTOR
        try:
            return await pool.run(partial(context.run, function, *args, **kwargs))
        except ListingExecutionError as error:
            return _execution_failure(tool_name, error)

    return wrapped
