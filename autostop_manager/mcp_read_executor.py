"""Bound synchronous MCP backends without blocking the native MCP event loop."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import copy_context
from functools import partial, wraps
import inspect
import time
from typing import Any

from .listing_executor import BoundedListingExecutor, ListingExecutionError, read_execution_deadline

# Process-wide budgets retain cancelled admissions until real workers finish.
# Web/provider work must not consume the local automation controller's capacity.
BLOCKING_READ_TOOLS = frozenset(
    {
        "partsapi_catalog_lookup",
        "decode_vin_vpic",
        "decode_wmi_vpic",
        "benchmark_vin_parts_lookup",
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
BLOCKING_WEB_TOOLS = frozenset(
    {
        "search_web_multi",
        "fetch_page_excerpt",
        "fetch_page_browser",
        "collect_work_price_evidence",
        "estimate_repair_work_cost",
        "verify_oem_candidates_web",
    }
)
BLOCKING_CONTROL_TOOLS = frozenset({"manager_automations", "manager_automation_control"})
_WEB_EXECUTOR = BoundedListingExecutor(max_workers=2, max_waiting_calls=2, timeout_seconds=90)
_CONTROL_EXECUTOR = BoundedListingExecutor(max_workers=2, max_waiting_calls=2, timeout_seconds=30)
BLOCKING_J1_STATUS_TOOLS = frozenset({"j1_research_status"})
_J1_STATUS_EXECUTOR = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=30)
BLOCKING_J1_REPORT_TOOLS = frozenset({"j1_research_report"})
_J1_REPORT_EXECUTOR = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=30)


def _execution_failure(tool_name: str, error: ListingExecutionError) -> dict[str, Any]:
    result: dict[str, Any]
    if tool_name in BLOCKING_J1_STATUS_TOOLS | BLOCKING_J1_REPORT_TOOLS:
        # Routing to a general/VIN job itself requires state access. A transport
        # refusal must not route, echo inputs or claim that housekeeping rolled back.
        reason = {
            "provider_busy": "busy",
            "provider_wait_timeout": "wait_timeout",
            "provider_execution_failed": "execution_failed",
            "provider_executor_unavailable": "executor_unavailable",
            "provider_executor_draining": "executor_draining",
        }.get(error.code, "executor_unavailable")
        result = {
            "ok": False,
            "schema": "autostop.j1.report.v1" if tool_name in BLOCKING_J1_REPORT_TOOLS else "autostop.j1.research.v1",
            "error": {
                "code": "j1_report_executor_unavailable"
                if tool_name in BLOCKING_J1_REPORT_TOOLS
                else "j1_status_executor_unavailable",
                "reason": reason,
            },
            "outcome_uncertain": error.adapter_started,
        }
    elif tool_name in BLOCKING_CONTROL_TOOLS:
        result = {"ok": False, "error": "automation_control_unavailable"}
    elif tool_name in {"collect_work_price_evidence", "estimate_repair_work_cost", "verify_oem_candidates_web"}:
        # Once a worker starts, its still-running network/attempt counts are unknown.
        # These failures are constructed before domain validation: no input echo.
        execution: dict[str, Any] = {
            "network_calls": None if error.adapter_started else 0,
            "attempts": None if error.adapter_started else [],
        }
        if tool_name == "collect_work_price_evidence":
            from .automotive_contracts import result as automotive_result

            result = automotive_result(
                tool_name, "provider_error", {}, missing_fields=["work_price_evidence"], warnings=[error.code]
            )
            result.update(error=error.code, execution=execution)
        elif tool_name == "verify_oem_candidates_web":
            result = {
                "ok": False,
                "schema": "OemCandidatePublicEvidenceV1",
                "mode": "read_only_public_web_verification",
                "status": "public_search_provider_failed",
                "error": error.code,
                "input_context": {},
                "source_routes": [],
                "candidate_evidence": [],
                "conflicts": [],
                "manual_actions": [],
                "warnings": [error.code],
                "privacy": {"vin_input_supported": False, "writes_crm": False, "creates_orders": False},
                "execution": execution,
            }
        else:
            result = {
                "ok": False,
                "outcome": "provider_error",
                "error": error.code,
                "read_only": True,
                "crm_write_allowed": False,
                "execution": execution,
            }
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
        deadline_token = None
        if tool_name == "collect_work_price_evidence":
            from .automotive_contracts import finite_number

            arguments = inspect.signature(function).bind(*args, **kwargs)
            arguments.apply_defaults()
            budget = finite_number(arguments.arguments.get("deadline_seconds", 20), minimum=0.1)
            # Invalid budgets remain domain validation failures, without acquisition.
            if budget is not None and budget <= 60:
                deadline_token = read_execution_deadline.set(time.monotonic() + budget)
        context = copy_context()
        pool = (
            _J1_REPORT_EXECUTOR
            if tool_name in BLOCKING_J1_REPORT_TOOLS
            else _J1_STATUS_EXECUTOR
            if tool_name in BLOCKING_J1_STATUS_TOOLS
            else _CONTROL_EXECUTOR
            if tool_name in BLOCKING_CONTROL_TOOLS
            else _WEB_EXECUTOR
            if tool_name in BLOCKING_WEB_TOOLS
            else _EXECUTOR
        )
        try:
            return await pool.run(partial(context.run, function, *args, **kwargs))
        except ListingExecutionError as error:
            return _execution_failure(tool_name, error)
        finally:
            if deadline_token is not None:
                read_execution_deadline.reset(deadline_token)

    return wrapped
