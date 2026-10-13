"""Bound synchronous MCP backends without blocking the native MCP event loop."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import copy_context
from functools import partial, wraps
from typing import Any

from .listing_executor import BoundedListingExecutor, ListingExecutionError

# Process-wide budgets retain cancelled admissions until real workers finish.
# Web/provider work must not consume the local automation controller's capacity.
BLOCKING_READ_TOOLS = frozenset(
    {
        "partsapi_catalog_lookup",
        "decode_vin_vpic",
        "decode_wmi_vpic",
        "vininfo_decode",
        "corgi_decode",
        "public_aftermarket_catalog_lookup",
        "exist_price_lookup",
        "lookup_original_parts",
        "resolve_vin_oem_parts",
        "search_offline_parts_catalogs",
        "lookup_public_automotive_evidence",
        "elcats_catalog_query",
    }
)
_EXECUTOR = BoundedListingExecutor(max_workers=2, max_waiting_calls=2, timeout_seconds=60)
BLOCKING_WEB_TOOLS = frozenset({"search_web_multi", "fetch_page_excerpt", "fetch_page_browser"})
BLOCKING_CONTROL_TOOLS = frozenset({"manager_automations", "manager_automation_control"})
_WEB_EXECUTOR = BoundedListingExecutor(max_workers=2, max_waiting_calls=2, timeout_seconds=90)
_CONTROL_EXECUTOR = BoundedListingExecutor(max_workers=2, max_waiting_calls=2, timeout_seconds=30)


def _execution_failure(tool_name: str, error: ListingExecutionError) -> dict[str, Any]:
    if tool_name in BLOCKING_CONTROL_TOOLS:
        result: dict[str, Any] = {"ok": False, "error": "automation_control_unavailable"}
    elif tool_name in BLOCKING_WEB_TOOLS:
        from .web_research_gateway import _gateway_failure_response, _page_failure

        if tool_name == "search_web_multi":
            # The backend has not validated the input: never echo it on refusal.
            result = _gateway_failure_response(
                query="", vin_redacted=False, capability=tool_name, allowed_domains=[], provider_order=[]
            )
            result["adapter"] = "manager_mcp_executor"
            result["error"] = {"code": error.code, "retryable": False}
        else:
            result = _page_failure(tool_name, code=error.code, retryable=False)
    else:
        result = {"ok": False, "outcome": "provider_error", "error": error.code}
    return {
        **result,
        "stage": error.stage,
        "adapter_started": error.adapter_started,
        "adapter_may_continue": error.adapter_may_continue,
        "retry_attempted": False,
        "retryable": False,
    }


def offload_read(function: Callable[..., Any], tool_name: str = "") -> Callable[..., Any]:
    """Preserve the callable signature; SDK validation still happens before admission."""

    @wraps(function)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        context = copy_context()
        pool = (
            _CONTROL_EXECUTOR
            if tool_name in BLOCKING_CONTROL_TOOLS
            else _WEB_EXECUTOR
            if tool_name in BLOCKING_WEB_TOOLS
            else _EXECUTOR
        )
        try:
            return await pool.run(partial(context.run, function, *args, **kwargs))
        except ListingExecutionError as error:
            return _execution_failure(tool_name, error)

    return wrapped
