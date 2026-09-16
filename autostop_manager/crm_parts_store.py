"""Narrow CRM Gateway access to the AutoStop parts-store board column.

Customer and vehicle content stays in CRM. This adapter keeps no local copy and
cannot change cards in another column, place orders, or publish Store offers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Protocol

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .config import CrmMcpConnectionConfig, get_crm_mcp_connection_config
from .crm_mcp_web_research import _payload_from_tool_result, _run_sync

PARTS_STORE_COLUMN = "parts_store"
_RISKS = {"search_cards": "read", "get_card": "read", "create_card": "write", "update_card": "write"}
_SCHEMA_HASH = re.compile(r"[0-9a-f]{16}")
_TIMEOUT_SECONDS = 12.0
_GATEWAY_WARNINGS = frozenset(
    {
        "agent_gateway_disabled",
        "agent_gateway_raw_disabled",
        "agent_gateway_writes_disabled",
        "maintenance_mode_raw_write_blocked",
        "capability_not_found",
        "schema_hash_mismatch_rediscover_capability",
        "idempotency_key_required_for_raw_write",
        "expected_updated_at_required_reread_exact_card_first",
        "raw_capability_failed",
        "verification_failed_compensation_required",
        "workflow_ledger_close_failed",
    }
)


def _failure(code: str, *, uncertain: bool = False) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "blocked" if not uncertain else "uncertain",
        "error": code,
        "column": PARTS_STORE_COLUMN,
        "outcome_uncertain": uncertain,
    }


def _write_failure(code: str, response: Mapping[str, Any]) -> dict[str, Any]:
    uncertain = response.get("outcome_uncertain") is True or response.get("status") not in {"blocked"}
    result = _failure(code, uncertain=uncertain)
    warnings = response.get("warnings")
    warning = warnings[0] if isinstance(warnings, list) and warnings else response.get("error")
    if isinstance(warning, str) and warning in _GATEWAY_WARNINGS:
        result["gateway_warning"] = warning
    return result


class CrmPartsStoreTransport(Protocol):
    def invoke(self, name: str, arguments: dict[str, Any], *, idempotency_key: str = "") -> dict[str, Any]: ...


class LoopbackCrmPartsStoreTransport:
    """Discover the live CRM schema before each allowlisted raw-capability call."""

    def __init__(self, config: CrmMcpConnectionConfig) -> None:
        self._config = config

    def invoke(self, name: str, arguments: dict[str, Any], *, idempotency_key: str = "") -> dict[str, Any]:
        if not self._config.configured:
            return _failure("crm_mcp_not_configured")
        if self._config.error_code:
            return _failure(self._config.error_code)
        if name not in _RISKS or not isinstance(arguments, dict):
            return _failure("crm_capability_not_allowed")
        if _RISKS[name] == "write" and not str(idempotency_key or "").strip():
            return _failure("idempotency_key_required")
        try:
            return _run_sync(
                lambda: self._invoke_async(name, arguments, idempotency_key),
                timeout_seconds=_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 - never expose endpoint, bearer or CRM content
            return _failure("crm_mcp_transport_failed", uncertain=_RISKS[name] == "write")

    async def _invoke_async(self, name: str, arguments: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_SECONDS,
            follow_redirects=False,
            headers={"Authorization": f"Bearer {self._config.bearer_token}"},
            trust_env=False,
        ) as http_client:
            async with streamable_http_client(self._config.url, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    schema_result = await session.call_tool(
                        "get_raw_capability_schema",
                        {"name": name},
                        read_timeout_seconds=timedelta(seconds=_TIMEOUT_SECONDS),
                    )
                    schema = _payload_from_tool_result(schema_result)
                    summary = schema.get("summary") if isinstance(schema, dict) else None
                    data = schema.get("data") if isinstance(schema, dict) else None
                    schema_hash = str(summary.get("schema_hash") or "") if isinstance(summary, Mapping) else ""
                    if (
                        getattr(schema_result, "isError", False)
                        or not isinstance(schema, Mapping)
                        or schema.get("ok") is not True
                        or not isinstance(summary, Mapping)
                        or summary.get("name") != name
                        or summary.get("risk") != _RISKS[name]
                        or not _SCHEMA_HASH.fullmatch(schema_hash)
                        or not isinstance(data, Mapping)
                        or not isinstance(data.get("input_schema"), Mapping)
                    ):
                        return _failure("crm_schema_discovery_failed")
                    call_args: dict[str, Any] = {
                        "name": name,
                        "arguments": arguments,
                        "schema_hash": schema_hash,
                        # Full description is required to append without truncating history.
                        "allow_large_output": True,
                    }
                    if _RISKS[name] == "write":
                        call_args["idempotency_key"] = idempotency_key
                    result = await session.call_tool(
                        "call_raw_capability",
                        call_args,
                        read_timeout_seconds=timedelta(seconds=_TIMEOUT_SECONDS),
                    )
                    payload = _payload_from_tool_result(result)
                    if getattr(result, "isError", False) or not isinstance(payload, dict):
                        return _failure("crm_capability_failed", uncertain=_RISKS[name] == "write")
                    return payload


def _source_data(response: Mapping[str, Any]) -> dict[str, Any]:
    outer = response.get("data")
    inner = outer.get("data") if isinstance(outer, Mapping) else None
    return dict(inner) if isinstance(inner, Mapping) else {}


def _card(response: Mapping[str, Any]) -> dict[str, Any] | None:
    card = _source_data(response).get("card")
    return dict(card) if isinstance(card, Mapping) else None


def _card_view(card: Mapping[str, Any], *, full: bool) -> dict[str, Any]:
    fields = (
        ("id", "column", "title", "vehicle", "updated_at", "description")
        if full
        else ("id", "column", "title", "vehicle", "updated_at")
    )
    return {field: card[field] for field in fields if field in card}


def parts_store_cards(  # noqa: C901 - one bounded CRUD action router
    operation: str,
    *,
    transport: CrmPartsStoreTransport | None = None,
    query: str = "",
    limit: int = 30,
    card_id: str = "",
    title: str = "",
    vehicle: str = "",
    description: str = "",
    note: str = "",
    expected_updated_at: str = "",
    idempotency_key: str = "",
) -> dict[str, Any]:
    """List/get/create cards or append one note to an exact parts-store card."""

    gateway = transport or LoopbackCrmPartsStoreTransport(get_crm_mcp_connection_config())
    action = str(operation or "").strip().lower()
    if action == "list":
        if isinstance(limit, bool) or not isinstance(limit, int):
            return _failure("invalid_limit")
        response = gateway.invoke(
            "search_cards",
            {
                "query": query[:240],
                "column": PARTS_STORE_COLUMN,
                "include_archived": False,
                "limit": max(1, min(limit, 50)),
            },
        )
        if response.get("ok") is not True:
            return _failure("crm_list_failed")
        data = _source_data(response)
        cards = data.get("cards")
        if not isinstance(cards, list):
            return _failure("crm_list_invalid")
        items = [
            _card_view(item, full=False)
            for item in cards
            if isinstance(item, Mapping) and item.get("column") == PARTS_STORE_COLUMN
        ]
        return {
            "ok": True,
            "status": "completed",
            "column": PARTS_STORE_COLUMN,
            "items": items,
            "has_more": bool((data.get("meta") or {}).get("has_more")),
        }

    if action not in {"get", "create", "append_note"}:
        return _failure("unsupported_operation")
    if action == "create":
        if not str(title or "").strip() or not str(idempotency_key or "").strip():
            return _failure("title_and_idempotency_required")
        arguments = {
            "title": title.strip(),
            "vehicle": vehicle.strip(),
            "description": description.strip(),
            "column": PARTS_STORE_COLUMN,
        }
        response = gateway.invoke("create_card", arguments, idempotency_key=idempotency_key)
        if response.get("ok") is not True:
            return _write_failure("crm_create_failed", response)
        created = _card(response)
        created_id = str((created or {}).get("id") or "")
        if not created_id:
            return _failure("crm_create_unverified", uncertain=True)
        readback = gateway.invoke("get_card", {"card_id": created_id})
        actual = _card(readback) if readback.get("ok") is True else None
        if (
            actual is None
            or actual.get("column") != PARTS_STORE_COLUMN
            or actual.get("title") != arguments["title"]
            or actual.get("description") != arguments["description"]
        ):
            return _failure("crm_create_unverified", uncertain=True)
        return {"ok": True, "status": "completed", "column": PARTS_STORE_COLUMN, "card": _card_view(actual, full=True)}

    exact_id = str(card_id or "").strip()
    if not exact_id:
        return _failure("card_id_required")
    read = gateway.invoke("get_card", {"card_id": exact_id})
    current = _card(read) if read.get("ok") is True else None
    if current is None:
        return _failure("card_not_found")
    if current.get("id") != exact_id or current.get("column") != PARTS_STORE_COLUMN:
        return _failure("card_outside_parts_store")
    if action == "get":
        return {"ok": True, "status": "completed", "column": PARTS_STORE_COLUMN, "card": _card_view(current, full=True)}

    clean_note = str(note or "").strip()
    revision = str(current.get("updated_at") or "")
    history = current.get("description")
    if not clean_note or len(clean_note) > 4000 or not revision or not isinstance(history, str):
        return _failure("note_or_card_revision_invalid")
    if expected_updated_at and expected_updated_at != revision:
        return _failure("card_revision_changed")
    if not str(idempotency_key or "").strip():
        return _failure("idempotency_key_required")
    if history.rstrip().endswith(clean_note):
        return {
            "ok": True,
            "status": "already_present",
            "column": PARTS_STORE_COLUMN,
            "card": _card_view(current, full=True),
        }
    appended = f"{history.rstrip()}\n\n{clean_note}" if history.strip() else clean_note
    response = gateway.invoke(
        "update_card",
        {"card_id": exact_id, "description": appended, "expected_updated_at": revision},
        idempotency_key=idempotency_key,
    )
    if response.get("ok") is not True:
        return _write_failure("crm_append_failed", response)
    readback = gateway.invoke("get_card", {"card_id": exact_id})
    actual = _card(readback) if readback.get("ok") is True else None
    if actual is None or actual.get("column") != PARTS_STORE_COLUMN or actual.get("description") != appended:
        return _failure("crm_append_unverified", uncertain=True)
    return {"ok": True, "status": "completed", "column": PARTS_STORE_COLUMN, "card": _card_view(actual, full=True)}
