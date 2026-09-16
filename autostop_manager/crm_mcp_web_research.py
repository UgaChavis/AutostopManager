"""Safe synchronous E8 calls through the local CRM MCP raw-capability route.

The Manager process owns the optional endpoint and bearer in its own runtime
environment.  This module never reads CRM configuration files, never follows
redirects, and exposes only the four read-only E8 capabilities Manager needs.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .config import CrmMcpConnectionConfig, get_crm_mcp_connection_config
from .web_research_gateway import (
    FETCH_PAGE_BROWSER_CAPABILITY,
    FETCH_PAGE_EXCERPT_CAPABILITY,
    RESEARCH_PART_PUBLIC_EVIDENCE_CAPABILITY,
    SEARCH_WEB_MULTI_CAPABILITY,
    CapabilityWebResearchGatewayAdapter,
    WebResearchGateway,
)


CRM_MCP_E8_TIMEOUT_SECONDS = 8.0
_MAX_TIMEOUT_SECONDS = 10.0
_SYNC_GRACE_SECONDS = 1.0
_SCHEMA_HASH = re.compile(r"[0-9a-f]{16}")
_SUPPORTED_CAPABILITIES = frozenset(
    {
        RESEARCH_PART_PUBLIC_EVIDENCE_CAPABILITY,
        SEARCH_WEB_MULTI_CAPABILITY,
        FETCH_PAGE_EXCERPT_CAPABILITY,
        FETCH_PAGE_BROWSER_CAPABILITY,
    }
)
_GENERIC_WEB_TIMEOUT_SECONDS = {
    SEARCH_WEB_MULTI_CAPABILITY: 30.0,
    FETCH_PAGE_EXCERPT_CAPABILITY: 20.0,
    FETCH_PAGE_BROWSER_CAPABILITY: 35.0,
}


def _bounded_timeout(value: Any) -> float:
    if isinstance(value, bool):
        return CRM_MCP_E8_TIMEOUT_SECONDS
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return CRM_MCP_E8_TIMEOUT_SECONDS
    if timeout <= 0:
        return CRM_MCP_E8_TIMEOUT_SECONDS
    return min(timeout, _MAX_TIMEOUT_SECONDS)


def _failure(code: str, *, retryable: bool) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "retryable": retryable}}


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


def _schema_hash_for_capability(payload: Mapping[str, Any], capability: str) -> str | None:
    summary = payload.get("summary")
    data = payload.get("data")
    if payload.get("ok") is not True or not isinstance(summary, Mapping) or not isinstance(data, Mapping):
        return None
    schema_hash = str(summary.get("schema_hash") or "")
    input_schema = data.get("input_schema")
    if (
        summary.get("name") != capability
        or summary.get("risk") != "read"
        or not _SCHEMA_HASH.fullmatch(schema_hash)
        or not isinstance(input_schema, Mapping)
    ):
        return None
    return schema_hash


def _run_sync(factory: Callable[[], Any], *, timeout_seconds: float) -> Any:
    """Run a coroutine factory without nesting an event loop in a sync MCP tool."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    state: dict[str, Any] = {}
    completed = threading.Event()

    def run() -> None:
        try:
            state["result"] = asyncio.run(factory())
        except Exception as exc:  # noqa: BLE001 - re-raised to the sanitized public boundary below
            state["error"] = exc
        finally:
            completed.set()

    worker = threading.Thread(target=run, name="autostop-e8-crm-mcp", daemon=True)
    worker.start()
    if not completed.wait(timeout_seconds + _SYNC_GRACE_SECONDS):
        raise TimeoutError("crm_mcp_sync_timeout")
    if "error" in state:
        raise state["error"]
    return state.get("result")


class LoopbackCrmMcpWebResearchTransport:
    """A narrow synchronous transport for discovered read-only E8 raw capabilities."""

    def __init__(
        self,
        config: CrmMcpConnectionConfig,
        *,
        timeout_seconds: float = CRM_MCP_E8_TIMEOUT_SECONDS,
    ) -> None:
        self._config = config
        self._timeout_seconds = _bounded_timeout(timeout_seconds)

    def __call__(self, capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.invoke(capability, arguments)

    def invoke(self, capability: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._config.error_code:
            return _failure(self._config.error_code, retryable=False)
        if capability not in _SUPPORTED_CAPABILITIES or not isinstance(arguments, dict):
            return _failure("crm_mcp_capability_not_allowed", retryable=False)
        timeout_seconds = max(self._timeout_seconds, _GENERIC_WEB_TIMEOUT_SECONDS.get(capability, 0.0))
        try:
            return _run_sync(
                lambda: self._invoke_async(capability, dict(arguments), timeout_seconds),
                timeout_seconds=timeout_seconds,
            )
        except Exception:  # noqa: BLE001 - do not expose URL, bearer, or upstream exceptions.
            return _failure("crm_mcp_transport_failed", retryable=True)

    async def _invoke_async(self, capability: str, arguments: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
        return await asyncio.wait_for(
            self._invoke_session(capability, arguments, timeout_seconds),
            timeout=timeout_seconds,
        )

    async def _invoke_session(
        self, capability: str, arguments: dict[str, Any], timeout_seconds: float
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._config.bearer_token}"}
        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            headers=headers,
            trust_env=False,
        ) as http_client:
            async with streamable_http_client(self._config.url, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    schema_result = await session.call_tool(
                        "get_raw_capability_schema",
                        {"name": capability},
                        read_timeout_seconds=timedelta(seconds=timeout_seconds),
                    )
                    schema = _payload_from_tool_result(schema_result)
                    schema_hash = (
                        _schema_hash_for_capability(schema, capability)
                        if not bool(getattr(schema_result, "isError", False)) and isinstance(schema, Mapping)
                        else None
                    )
                    if schema_hash is None:
                        return _failure("crm_mcp_schema_discovery_failed", retryable=True)
                    result = await session.call_tool(
                        "call_raw_capability",
                        {
                            "name": capability,
                            "arguments": arguments,
                            "schema_hash": schema_hash,
                            "allow_large_output": False,
                        },
                        read_timeout_seconds=timedelta(seconds=timeout_seconds),
                    )
                    payload = _payload_from_tool_result(result)
                    if bool(getattr(result, "isError", False)) or not isinstance(payload, Mapping):
                        return _failure("crm_mcp_capability_failed", retryable=True)
                    data = payload.get("data")
                    if payload.get("ok") is not True or not isinstance(data, Mapping):
                        return _failure("crm_mcp_capability_failed", retryable=True)
                    return {"ok": True, "data": dict(data)}


def build_crm_mcp_web_research_gateway(
    config: CrmMcpConnectionConfig | None = None,
) -> WebResearchGateway | None:
    """Return an installed-capable E8 gateway, or ``None`` for explicit fallback.

    A partial or invalid configuration remains a configured route and therefore
    returns a structured failure rather than silently resuming local search.
    """

    connection = config if config is not None else get_crm_mcp_connection_config()
    if not connection.configured:
        return None
    return CapabilityWebResearchGatewayAdapter(LoopbackCrmMcpWebResearchTransport(connection))


__all__ = [
    "CRM_MCP_E8_TIMEOUT_SECONDS",
    "LoopbackCrmMcpWebResearchTransport",
    "build_crm_mcp_web_research_gateway",
]
