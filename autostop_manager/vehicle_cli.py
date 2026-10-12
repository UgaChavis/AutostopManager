"""Bounded JSON CLI for the current independent vehicle functions; no MCP registration."""

from __future__ import annotations

import importlib
from collections.abc import Callable
import inspect
import json
import math
import sys
from typing import Any, BinaryIO, TextIO, get_type_hints


MAX_REQUEST_BYTES = 5 * 1024 * 1024
MAX_JSON_DEPTH = 64
VEHICLE_TOOLS = {
    "inspect_vehicle_identifier": "automotive_identity",
    "decode_vin_vpic": "automotive_identity",
    "decode_wmi_vpic": "automotive_identity",
    "decode_wmi_local": "automotive_offline",
    "vininfo_decode": "automotive_offline",
    "corgi_decode": "automotive_offline",
    "vin_brand_details": "automotive_offline",
    "decode_frame_local": "automotive_offline",
    "reconcile_vehicle_identity": "automotive_identity",
    "decode_vehicle_batch": "automotive_identity",
}


def _failure(code: str) -> dict[str, Any]:
    return {"ok": False, "outcome": code, "error": code, "data": {}}


def _load_tool(name: str) -> Callable[..., dict[str, Any]]:
    module = importlib.import_module("." + VEHICLE_TOOLS[name], package="autostop_manager")
    return getattr(module, name)  # type: ignore[no-any-return]


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> Any:
    raise ValueError("nonfinite_json")


def _bounded_json(value: Any) -> bool:
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_JSON_DEPTH or (isinstance(item, float) and not math.isfinite(item)):
            return False
        if isinstance(item, str):
            item.encode("utf-8")
        if isinstance(item, dict):
            pending.extend((key, depth + 1) for key in item)
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return True


def call_vehicle_tool(name: str, arguments: Any) -> dict[str, Any]:
    """Validate one current signature and call only its selected implementation."""
    if name not in VEHICLE_TOOLS:
        return _failure("unknown_tool")
    if not isinstance(arguments, dict):
        return _failure("invalid_input")
    from pydantic import TypeAdapter, ValidationError

    try:
        function = _load_tool(name)
    except (ImportError, AttributeError, RuntimeError):
        return _failure("tool_failed")
    try:
        parameters = inspect.signature(function).bind(**arguments)
        hints = get_type_hints(function)
        for parameter, value in parameters.arguments.items():
            TypeAdapter(hints[parameter]).validate_python(value, strict=True)
    except (TypeError, ValueError, KeyError, ValidationError):
        return _failure("invalid_input")
    try:
        response = function(**arguments)
        if not isinstance(response, dict):
            return _failure("tool_failed")
        json.dumps(response, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except Exception:  # noqa: BLE001 - CLI boundary never exposes arguments or provider exception text.
        return _failure("tool_failed")
    return response


def run_vehicle_tool(name: str, stream: BinaryIO | TextIO | None = None) -> dict[str, Any]:
    """Read at most the request limit plus one byte, then dispatch a single tool."""
    if name not in VEHICLE_TOOLS:
        return _failure("unknown_tool")
    if stream is None:
        stream = getattr(sys.stdin, "buffer", sys.stdin)
    try:
        raw = stream.read(MAX_REQUEST_BYTES + 1)
        size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
        if size > MAX_REQUEST_BYTES:
            return _failure("request_too_large")
        text = raw if isinstance(raw, str) else raw.decode("utf-8")
        arguments = json.loads(text, object_pairs_hook=_json_object, parse_constant=_reject_constant)
        if not _bounded_json(arguments):
            return _failure("invalid_json")
    except (OSError, ValueError, UnicodeError, RecursionError):
        return _failure("invalid_json")
    return call_vehicle_tool(name, arguments)
