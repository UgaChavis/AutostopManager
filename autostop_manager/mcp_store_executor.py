"""Keep Store I/O off the MCP loop without abandoning stateful operations."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import copy_context
from functools import partial, wraps
from typing import Any

from .agent_gateway import agent_envelope
from .listing_executor import BoundedListingExecutor, ListingExecutionError


STORE_READ_TOOLS = frozenset(
    {
        "store_runtime_status",
        "store_search",
        "store_entity_context",
        "download_store_quote_vin_photo",
        "get_store_analytics_report",
        "store_owner_capabilities",
    }
)
# These entry points can mutate Store or Manager state even in a nominally
# read/dry-run invocation. Keep their former single-loop serialization, while
# leaving independent reads available to inspect an uncertain write's outcome.
STORE_STATEFUL_TOOLS = frozenset(
    {"store_digest", "store_management_action", "store_owner_api", "store_quote_conductor"}
)
STORE_BLOCKING_TOOLS = STORE_READ_TOOLS | STORE_STATEFUL_TOOLS

_READ_EXECUTOR = BoundedListingExecutor(max_workers=2, max_waiting_calls=2, timeout_seconds=60)
_STATEFUL_EXECUTOR = BoundedListingExecutor(max_workers=1, max_waiting_calls=2, timeout_seconds=90)
_ERROR_CODES = {
    "provider_busy": "store_executor_busy",
    "provider_wait_timeout": "store_executor_wait_timeout",
    "provider_executor_unavailable": "store_executor_unavailable",
    "provider_execution_failed": "store_executor_failed",
}


def _execution_failure(tool_name: str, error: ListingExecutionError) -> dict[str, Any]:
    code = _ERROR_CODES[error.code]
    uncertain = tool_name in STORE_STATEFUL_TOOLS and error.adapter_started
    status = "compensating" if uncertain else "degraded"
    flags = {
        "stage": error.stage,
        "adapter_started": error.adapter_started,
        "adapter_may_continue": error.adapter_may_continue,
        "retry_attempted": False,
        "retryable": False,
    }
    meta: dict[str, Any] = {
        "source": "manager_mcp_executor",
        **flags,
        # Starting a workflow does not prove it reached HTTP dispatch. Neither
        # a timeout nor a completed worker proves a stateful effect did not run.
        "request_dispatched": None if error.adapter_started else False,
        "outcome_uncertain": uncertain,
    }
    if uncertain:
        meta.update(readback_required=True, readback_verified=False)
    if tool_name == "store_quote_conductor":
        return {
            **agent_envelope(
                ok=False,
                status=status,
                summary={"error_code": code},
                warnings=[code],
                next_actions=["verify_store_outcome_before_retry"] if uncertain else [],
                meta={"conductor_format": "store_quote_conductor_v1", **meta},
            ),
            **flags,
        }
    result: dict[str, Any] = {
        "ok": False,
        "format": "store_agent_v1",
        "status": status,
        "summary": {"error_code": code},
        "warnings": [code],
        "meta": meta,
        **flags,
    }
    if tool_name in {"store_owner_api", "store_owner_capabilities"}:
        result.update(format="autostop_store_owner_api_v1", error={"code": code}, data_included=False)
    elif tool_name == "get_store_analytics_report":
        result.update(format="store_analytics_report_v1", error={"code": code})
        meta.update(aggregatedOnly=True, rawEventsIncluded=False)
    else:
        result.update(items=[], changes=[], page={})
    return result


def offload_store(function: Callable[..., Any], tool_name: str) -> Callable[..., Any]:
    """Keep SDK validation/signatures and return completed domain results intact.

    BoundedListingExecutor retains admissions until the real worker finishes,
    including after caller cancellation. A cancelled queued job never invokes
    the function; an admitted workflow can finish its one write and readback.
    """

    @wraps(function)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        context = copy_context()
        pool = _STATEFUL_EXECUTOR if tool_name in STORE_STATEFUL_TOOLS else _READ_EXECUTOR
        try:
            return await pool.run(partial(context.run, function, *args, **kwargs))
        except ListingExecutionError as error:
            return _execution_failure(tool_name, error)

    return wrapped
