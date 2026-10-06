"""Independent E4 tools: one callable registry for CLI and native MCP.

Imports of optional decoders are lazy. No tool silently chooses another source.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import inspect
import time
import threading
from typing import Any, Literal

SourceTool = Literal[
    "decode_vin_vpic",
    "decode_wmi_vpic",
    "decode_wmi_local",
    "vininfo_decode",
    "corgi_decode",
    "vin_brand_details",
    "decode_frame_local",
]
IdentifierType = Literal["auto", "vin", "vin_partial", "frame_number", "market_code"]


def inspect_vehicle_identifier(identifier: str, identifier_type: IdentifierType = "auto") -> dict[str, Any]:
    from .e4_identity import inspect_vehicle_identifier as inspect_identifier

    return inspect_identifier(identifier, identifier_type=identifier_type)


def decode_vin_vpic(
    identifier: str, model_year: int | None = None, identifier_type: IdentifierType = "auto"
) -> dict[str, Any]:
    from .e4_identity import decode_vin_vpic as decode

    return decode(identifier, model_year=model_year, identifier_type=identifier_type)


def decode_wmi_vpic(identifier: str) -> dict[str, Any]:
    from .e4_identity import decode_wmi_vpic as decode

    return decode(identifier)


def decode_wmi_local(identifier: str) -> dict[str, Any]:
    from .e4_identity import decode_wmi_local as decode

    return decode(identifier)


def vininfo_decode(identifier: str, model_year: int | None = None) -> dict[str, Any]:
    from .e4_optional import vininfo_decode as decode

    return decode(identifier, model_year=model_year)


def corgi_decode(identifier: str, model_year: int | None = None) -> dict[str, Any]:
    from .e4_optional import corgi_decode as decode

    return decode(identifier, model_year=model_year)


def vin_brand_details(
    identifier: str, model_year: int | None = None, market: str | None = None, identifier_type: IdentifierType = "auto"
) -> dict[str, Any]:
    from .e4_identity import vin_brand_details as decode

    return decode(identifier, model_year=model_year, market=market, identifier_type=identifier_type)


def decode_frame_local(identifier: str, identifier_type: IdentifierType = "auto") -> dict[str, Any]:
    from .e4_identity import decode_frame_local as decode

    return decode(identifier, identifier_type=identifier_type)


def reconcile_vehicle_identity(
    identifier: str,
    results: list[dict[str, Any]],
    crm_context: dict[str, Any] | None = None,
    identifier_type: IdentifierType = "auto",
) -> dict[str, Any]:
    from .e4_identity import reconcile_vehicle_identity as reconcile

    return reconcile(identifier, results, crm_context=crm_context, identifier_type=identifier_type)


def _failure(code: str) -> dict[str, Any]:
    return {"ok": False, "outcome": code, "errors": [{"code": code}]}


def _validated_args(tool: str, arguments: Any) -> dict[str, Any] | None:
    if not isinstance(arguments, dict):
        return None
    function = E4_TOOLS.get(tool)
    if function is None:
        return None
    try:
        inspect.signature(function).bind(**arguments)
    except TypeError:
        return None
    if not isinstance(arguments.get("identifier"), str):
        return None
    year = arguments.get("model_year")
    if year is not None and (type(year) is not int or not 1886 <= year <= 2100):
        return None
    kind = arguments.get("identifier_type", "auto")
    if not isinstance(kind, str) or kind not in {"auto", "vin", "vin_partial", "frame_number", "market_code"}:
        return None
    if arguments.get("market") is not None and not isinstance(arguments["market"], str):
        return None
    return arguments


def call_e4_tool(tool: str, arguments: Any) -> dict[str, Any]:
    if tool not in E4_TOOLS:
        return _failure("unknown_tool")
    if tool == "decode_vehicle_batch":
        if not isinstance(arguments, dict):
            return _failure("invalid_input")
        try:
            inspect.signature(decode_vehicle_batch).bind(**arguments)
        except TypeError:
            return _failure("invalid_input")
    elif _validated_args(tool, arguments) is None:
        return _failure("invalid_input")
    try:
        return E4_TOOLS[tool](**arguments)
    except (TypeError, ValueError, AttributeError, OverflowError):
        # Never interpolate caller arguments or exception text into diagnostics.
        return _failure("invalid_input")


def _batch_inputs(tool: str, items: Any) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    if not isinstance(tool, str) or tool not in SOURCE_TOOLS:
        return None, _failure("unsupported_batch_tool")
    if not isinstance(items, list) or len(items) > 500:
        return None, _failure("identity_batch_too_large" if isinstance(items, list) else "invalid_input")
    rows = []
    for item in items:
        arguments = _validated_args(tool, item)
        rows.append({"ok": arguments is not None, "arguments": arguments})
    return rows, None


def _finish_batch(tool: str, results: list[dict[str, Any]], processing: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": all(row.get("ok") for row in results),
        "tool": tool,
        "count": len(results),
        "results": [{**row, "item_index": index} for index, row in enumerate(results)],
        "processing": processing,
    }


def _provider_inputs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "ok": row["ok"],
            "identifier": (row["arguments"] or {}).get("identifier", ""),
            "context": {"model_year": (row["arguments"] or {}).get("model_year")},
            "identifier_type": (row["arguments"] or {}).get("identifier_type", "auto"),
        }
        for row in rows
    ]


def _provider_rows(tool: str, rows: list[dict[str, Any]], collected: dict[str, Any]) -> list[dict[str, Any]]:
    from .e4_identity import vpic_observation

    results = []
    for row, provider in zip(rows, collected["results"], strict=True):
        if not row["ok"] or provider is None:
            results.append(_failure("invalid_input"))
            continue
        arguments = row["arguments"]
        results.append(
            vpic_observation(
                arguments["identifier"],
                provider,
                model_year=arguments.get("model_year"),
                wmi=tool == "decode_wmi_vpic",
                identifier_type=arguments.get("identifier_type", "auto"),
            )
        )
    return results


def decode_vehicle_batch(tool: SourceTool, items: list[dict[str, Any]]) -> dict[str, Any]:
    from .vehicle_identity_transport import IdentityBudget, collect_source_provider_results

    rows, error = _batch_inputs(tool, items)
    if error is not None:
        return error
    assert rows is not None
    if tool in {"decode_vin_vpic", "decode_wmi_vpic"}:
        collected = collect_source_provider_results(
            _provider_inputs(rows), source="vin" if tool == "decode_vin_vpic" else "wmi", budget=IdentityBudget()
        )
        return _finish_batch(tool, _provider_rows(tool, rows, collected), collected["processing"])
    return _local_batch(tool, rows)


def _local_batch(tool: str, rows: list[dict[str, Any]], cancel_event: threading.Event | None = None) -> dict[str, Any]:
    started = time.monotonic()
    results = []
    for row in rows:
        remaining = max(0.0, 30.0 - (time.monotonic() - started))
        if not row["ok"]:
            result = _failure("invalid_input")
        elif cancel_event is not None and cancel_event.is_set():
            result = _failure("decoder_cancelled")
        elif remaining <= 0:
            result = _failure("deadline_exceeded")
        elif tool == "corgi_decode":
            from .e4_optional import corgi_decode as decode

            result = decode(**row["arguments"], timeout_seconds=remaining, _cancel_event=cancel_event)
        else:
            result = call_e4_tool(tool, row["arguments"])
        results.append(result)
    return _finish_batch(tool, results, {"deadline_seconds": 30, "elapsed_ms": (time.monotonic() - started) * 1000})


async def _corgi_call_async(arguments: dict[str, Any], rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    from .e4_optional import corgi_decode as decode

    if rows is None and _validated_args("corgi_decode", arguments) is None:
        return _failure("invalid_input")
    cancelled = threading.Event()
    operation = (
        asyncio.to_thread(_local_batch, "corgi_decode", rows, cancelled)
        if rows is not None
        else asyncio.to_thread(decode, **arguments, _cancel_event=cancelled)
    )
    work = asyncio.create_task(operation)
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        cancelled.set()
        # The child loop observes this flag, kills and reaps the process before
        # the owning HTTP request finishes cancellation.
        await asyncio.shield(work)
        raise


async def call_e4_tool_async(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Native HTTP uses cancellable transport for the selected online sources."""
    from .vehicle_identity_transport import IdentityBudget, collect_source_provider_results_async

    if tool == "corgi_decode":
        return await _corgi_call_async(arguments)
    if tool == "decode_vehicle_batch":
        selected = arguments.get("tool", "")
        rows, error = _batch_inputs(selected, arguments.get("items"))
    elif tool in {"decode_vin_vpic", "decode_wmi_vpic"}:
        selected = tool
        rows, error = _batch_inputs(selected, [arguments])
    else:
        return await asyncio.to_thread(call_e4_tool, tool, arguments)
    if error is not None:
        return error
    assert rows is not None
    if selected == "corgi_decode":
        return await _corgi_call_async(arguments, rows)
    if selected not in {"decode_vin_vpic", "decode_wmi_vpic"}:
        return await asyncio.to_thread(call_e4_tool, tool, arguments)
    collected = await collect_source_provider_results_async(
        _provider_inputs(rows),
        source="vin" if selected == "decode_vin_vpic" else "wmi",
        budget=IdentityBudget(max_attempts=20 if tool == "decode_vehicle_batch" else 2),
        use_vpic_batch=tool == "decode_vehicle_batch",
    )
    results = _provider_rows(selected, rows, collected)
    if tool == "decode_vehicle_batch":
        return _finish_batch(selected, results, collected["processing"])
    return {**results[0], "processing": collected["processing"]}


E4_TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    "inspect_vehicle_identifier": inspect_vehicle_identifier,
    "decode_vin_vpic": decode_vin_vpic,
    "decode_wmi_vpic": decode_wmi_vpic,
    "decode_wmi_local": decode_wmi_local,
    "vininfo_decode": vininfo_decode,
    "corgi_decode": corgi_decode,
    "vin_brand_details": vin_brand_details,
    "decode_frame_local": decode_frame_local,
    "reconcile_vehicle_identity": reconcile_vehicle_identity,
    "decode_vehicle_batch": decode_vehicle_batch,
}
SOURCE_TOOLS = frozenset(E4_TOOLS) - {
    "inspect_vehicle_identifier",
    "reconcile_vehicle_identity",
    "decode_vehicle_batch",
}
