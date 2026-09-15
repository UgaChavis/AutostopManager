from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .mcp_contract import validate_manager_mcp_surface


DEFAULT_MANAGER_MCP_URL = "http://127.0.0.1:41931/mcp"
SYNTHETIC_IDENTIFIER = "SYNTHETICVIN00001"
GLOW_PLUG_QUERY = "свечи накаливания"


def _safe_url(value: str) -> str:
    """Keep diagnostics useful without retaining query parameters or credentials."""

    try:
        parsed = urlsplit(value)
        return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path or "/", "", ""))
    except ValueError:
        return "invalid_url"


def classify_transport_exception(exc: BaseException) -> str:
    """Classify the transport layer without exposing raw exception text."""

    nested = getattr(exc, "exceptions", ())
    if isinstance(nested, tuple) and nested:
        nested_classes = {classify_transport_exception(item) for item in nested}
        for failure_class in ("transport_auth_failure", "tool_not_registered", "transport_route_unavailable"):
            if failure_class in nested_classes:
                return failure_class
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code is None:
        status_code = getattr(exc, "status_code", None)
    if status_code in {401, 403}:
        return "transport_auth_failure"
    if status_code == 404:
        return "transport_route_unavailable"
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.NetworkError)):
        return "transport_route_unavailable"
    message = str(exc).casefold()
    if any(token in message for token in ("401", "403", "unauthorized", "forbidden")):
        return "transport_auth_failure"
    if "-32601" in message or "unknown tool" in message or "tool not found" in message:
        return "tool_not_registered"
    if any(token in message for token in ("404", "connection refused", "not found", "no route to host")):
        return "transport_route_unavailable"
    return "transport_failure"


def _payload_from_tool_result(result: Any) -> dict[str, Any] | None:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, Mapping):
        return dict(structured)
    for content in getattr(result, "content", []) or []:
        text = getattr(content, "text", None)
        if not isinstance(text, str):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _payload_contains(payload: Any, marker: str) -> bool:
    try:
        return marker in json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return True


async def _list_all_tools(session: ClientSession) -> list[Any]:
    tools: list[Any] = []
    cursor: str | None = None
    while True:
        page = await session.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.nextCursor
        if not cursor:
            return tools


async def _call(
    session: ClientSession, name: str, arguments: dict[str, Any], *, timeout: float
) -> tuple[bool, dict[str, Any] | None]:
    result = await session.call_tool(name, arguments, read_timeout_seconds=timedelta(seconds=timeout))
    return bool(getattr(result, "isError", False)), _payload_from_tool_result(result)


def _tool_error_check(is_error: bool, payload: dict[str, Any] | None) -> bool:
    return not is_error and isinstance(payload, dict)


async def async_probe_manager_mcp(
    url: str = DEFAULT_MANAGER_MCP_URL,
    *,
    timeout: float = 10.0,
    provider_failure_check: bool = False,
    store_check: bool = False,
) -> dict[str, Any]:
    """Probe a native Manager MCP endpoint using only synthetic, read-only data.

    Results are deliberately summarized: endpoint shape, tool names, hashes,
    statuses and failure classes are retained; input identifiers, response
    bodies, provider URLs, credentials and exception strings are not.
    """

    safe_url = _safe_url(url)
    report: dict[str, Any] = {
        "ok": False,
        "endpoint": safe_url,
        "diagnostic": "transport_failure",
        "checks": {},
        "privacy": {
            "synthetic_inputs_only": True,
            "raw_identifier_returned": False,
            "secret_exposed": False,
            "raw_provider_response_retained": False,
        },
    }
    if not (0 < timeout <= 90):
        return {**report, "diagnostic": "invalid_timeout"}

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    await session.send_ping()
                    server_info = getattr(initialized, "serverInfo", None)
                    report["checks"]["native_ping"] = {
                        "ok": True,
                        "server_name": str(getattr(server_info, "name", "")) or None,
                    }

                    listed_tools = await _list_all_tools(session)
                    tool_schemas = {
                        str(tool.name): getattr(tool, "inputSchema", None)
                        for tool in listed_tools
                        if getattr(tool, "name", None)
                    }
                    contract = validate_manager_mcp_surface(tool_schemas)
                    duplicate_tools = len(tool_schemas) != len(listed_tools)
                    if duplicate_tools and contract.get("ok"):
                        contract = {
                            **contract,
                            "ok": False,
                            "diagnostic": "tool_not_registered",
                            "warnings": ["manager_mcp_transport_duplicate_tool_name"],
                        }
                    report["checks"]["tools_list"] = {
                        "ok": bool(contract.get("ok")),
                        "diagnostic": contract.get("diagnostic"),
                        "tool_count": len(listed_tools),
                        "expected_tool_count": contract.get("expected_tool_count"),
                        "schema_fingerprint": contract.get("registered_schema_fingerprint"),
                        "missing_registered_tools": contract.get("missing_registered_tools", []),
                        "unexpected_registered_tools": contract.get("unexpected_registered_tools", []),
                        "warnings": contract.get("warnings", []),
                    }
                    if not contract.get("ok"):
                        report["diagnostic"] = str(contract.get("diagnostic") or "tool_not_registered")
                        return report

                    status_error, status = await _call(
                        session, "catalog_provider_status", {"stage": "oem_catalog"}, timeout=timeout
                    )
                    status_payload = status or {}
                    status_ok = _tool_error_check(status_error, status) and status_payload.get("ok") is True
                    report["checks"]["catalog_provider_status"] = {
                        "ok": status_ok,
                        "provider_count": len(status_payload.get("providers") or []),
                        "stage": status_payload.get("stage"),
                    }
                    if not status_ok:
                        report["diagnostic"] = "tool_invocation_failure"
                        return report

                    category_error, category = await _call(
                        session,
                        "search_partsapi_category_index",
                        {"query": GLOW_PLUG_QUERY, "intent_id": "glow_plug", "limit": 3},
                        timeout=timeout,
                    )
                    category_payload = category or {}
                    category_ok = _tool_error_check(category_error, category) and category_payload.get("ok") is True
                    category_unresolved = category_ok and not bool(category_payload.get("matches"))
                    report["checks"]["partsapi_category_index"] = {
                        "ok": category_ok,
                        "diagnostic": "category_unresolved" if category_unresolved else "ok",
                        "match_count": int(category_payload.get("count") or 0),
                        "schema": category_payload.get("schema"),
                    }
                    if not category_ok:
                        report["diagnostic"] = "tool_invocation_failure"
                        return report

                    resolver_error, resolver = await _call(
                        session,
                        "resolve_vin_oem_parts",
                        {
                            "identifier": SYNTHETIC_IDENTIFIER,
                            "requested_part": GLOW_PLUG_QUERY,
                            "dry_run": True,
                            "live_vpic": False,
                            "live_partsapi_identity": False,
                            "live_partsapi_oem": False,
                        },
                        timeout=timeout,
                    )
                    resolver_payload = resolver or {}
                    readiness = resolver_payload.get("readiness")
                    resolver_status = resolver_payload.get("status")
                    resolver_ok = (
                        _tool_error_check(resolver_error, resolver)
                        and resolver_status == "needs_partsapi_category_mapping"
                        and isinstance(readiness, Mapping)
                        and readiness.get("needs_partsapi_category_mapping") is True
                        and not bool(resolver_payload.get("oem_candidates"))
                        and int(resolver_payload.get("live_call_count") or 0) == 0
                    )
                    raw_identifier_returned = _payload_contains(resolver, SYNTHETIC_IDENTIFIER)
                    report["checks"]["synthetic_resolver"] = {
                        "ok": resolver_ok and not raw_identifier_returned,
                        "diagnostic": "category_unresolved"
                        if resolver_status == "needs_partsapi_category_mapping"
                        else "resolver_failed",
                        "status": resolver_status,
                        "live_call_count": int(resolver_payload.get("live_call_count") or 0) if resolver else None,
                        "oem_candidate_count": len(resolver_payload.get("oem_candidates") or []) if resolver else 0,
                    }
                    if raw_identifier_returned:
                        report["privacy"]["raw_identifier_returned"] = True
                    if not resolver_ok or raw_identifier_returned:
                        report["diagnostic"] = (
                            "category_unresolved"
                            if resolver_status == "needs_partsapi_category_mapping"
                            else "tool_invocation_failure"
                        )
                        return report

                    if provider_failure_check:
                        provider_error, provider = await _call(
                            session,
                            "partsapi_catalog_lookup",
                            {
                                "operation": "parts_by_vin",
                                "identifier": SYNTHETIC_IDENTIFIER,
                                "category": "0",
                                "timeout": 0,
                                "max_attempts": 1,
                                "dry_run": False,
                            },
                            timeout=timeout,
                        )
                        provider_payload = provider or {}
                        provider_failure_class = provider_payload.get("failure_class")
                        provider_ok = (
                            _tool_error_check(provider_error, provider)
                            and provider_payload.get("ok") is False
                            and bool(provider_failure_class)
                            and provider_payload.get("requires_fallback") is True
                        )
                        raw_provider_identifier = _payload_contains(provider, SYNTHETIC_IDENTIFIER)
                        report["checks"]["provider_failure"] = {
                            "ok": provider_ok and not raw_provider_identifier,
                            "diagnostic": "provider_failure" if provider_ok else "tool_invocation_failure",
                            "failure_class": provider_failure_class,
                            "outcome": provider_payload.get("outcome"),
                            "attempt_count": int(provider_payload.get("attempt_count") or 0) if provider else None,
                        }
                        if raw_provider_identifier:
                            report["privacy"]["raw_identifier_returned"] = True
                        if not provider_ok or raw_provider_identifier:
                            report["diagnostic"] = "provider_failure" if provider_ok else "tool_invocation_failure"
                            return report
                    if store_check:
                        for name, arguments in (
                            ("store_runtime_status", {"live": True}),
                            ("store_owner_capabilities", {"limit": 1}),
                        ):
                            tool_error, payload = await _call(session, name, arguments, timeout=timeout)
                            report["checks"][name] = {
                                "ok": _tool_error_check(tool_error, payload) and (payload or {}).get("ok") is True
                            }
                        if not all(
                            report["checks"][name]["ok"]
                            for name in ("store_runtime_status", "store_owner_capabilities")
                        ):
                            report["diagnostic"] = "store_connection_unavailable"
                            return report
    except Exception as exc:  # noqa: BLE001 - error text can contain transport details; report only its safe class.
        report["diagnostic"] = classify_transport_exception(exc)
        report["checks"]["transport"] = {"ok": False, "exception_type": type(exc).__name__}
        return report

    report["ok"] = True
    report["diagnostic"] = "ok"
    return report


def probe_manager_mcp(
    url: str = DEFAULT_MANAGER_MCP_URL,
    *,
    timeout: float = 10.0,
    provider_failure_check: bool = False,
    store_check: bool = False,
) -> dict[str, Any]:
    """Synchronous CLI entry point for the safe native endpoint probe."""

    return asyncio.run(
        async_probe_manager_mcp(
            url, timeout=timeout, provider_failure_check=provider_failure_check, store_check=store_check
        )
    )
