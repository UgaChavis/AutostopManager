"""Native MCP adapters; the autonomous CLI never imports this module."""

from __future__ import annotations

from functools import wraps
import inspect
from typing import Any, get_type_hints

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations

from .e4_tools import E4_TOOLS, call_e4_tool_async
from .vehicle_identity_request import run_identity_request


def register_e4_tools(server: Any) -> None:
    descriptions = {
        "inspect_vehicle_identifier": "Inspect VIN/frame structure and year/checksum diagnostics; no data source calls.",
        "decode_vin_vpic": "Decode only through NHTSA vPIC VIN; no WMI or local fallback. Returns bound source observation.",
        "decode_wmi_vpic": "Look up only a validated 3- or 6-character WMI through NHTSA; manufacturer scope only.",
        "decode_wmi_local": "Look up only local WMI hints; manufacturer/region, never exact vehicle configuration.",
        "vininfo_decode": "Use optional local vininfo library only; preserves year/configuration alternatives.",
        "corgi_decode": "Use optional prepared local Corgi/vPIC snapshot only; never installs or downloads data.",
        "vin_brand_details": "Match versioned local VIN family rules; candidate evidence without guessed engine or gearbox.",
        "decode_frame_local": "Match limited local Japanese frame family rules; candidate evidence only.",
        "reconcile_vehicle_identity": "Purely reconcile supplied observations/context; validates binding/origins and recomputes readiness.",
        "decode_vehicle_batch": "Run one explicitly selected source on up to 500 argument objects; ordered row errors, no other sources.",
    }
    for name, function in E4_TOOLS.items():
        wrapper = _make_wrapper(name, function)
        server.tool(
            name=name,
            description=descriptions[name],
            annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
        )(wrapper)


def _make_wrapper(name: str, function: Any) -> Any:
    @wraps(function)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        context = kwargs.pop("ctx", None)
        # FastMCP supplies keyword arguments. Direct calls remain usable in tests.
        arguments = inspect.signature(function).bind(*args, **kwargs).arguments
        return await run_identity_request(call_e4_tool_async(name, dict(arguments)), context)

    hints = get_type_hints(function)
    signature = inspect.signature(function)
    parameters = [parameter.replace(annotation=hints[parameter.name]) for parameter in signature.parameters.values()]
    parameters.append(inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Context | None))
    wrapper.__annotations__ = {**hints, "ctx": Context | None}
    wrapper.__signature__ = signature.replace(parameters=parameters, return_annotation=dict[str, Any])  # type: ignore[attr-defined]
    return wrapper
