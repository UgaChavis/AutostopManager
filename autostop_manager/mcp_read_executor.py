"""Bound blocking automotive reads without blocking the native MCP event loop."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import copy_context
from functools import partial, wraps
from typing import Any

from .listing_executor import BoundedListingExecutor, ListingExecutionError

# One process-wide admission budget for these tools, not a pool per request.
# The executor retains cancelled admissions until their real workers finish.
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


def offload_read(function: Callable[..., Any]) -> Callable[..., Any]:
    """Preserve the callable signature; SDK validation still happens before admission."""

    @wraps(function)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        context = copy_context()
        try:
            return await _EXECUTOR.run(partial(context.run, function, *args, **kwargs))
        except ListingExecutionError as error:
            return {
                "ok": False,
                "outcome": "provider_error",
                "error": error.code,
                "stage": error.stage,
                "adapter_started": error.adapter_started,
                "adapter_may_continue": error.adapter_may_continue,
                "retry_attempted": False,
                "retryable": False,
            }

    return wrapped
