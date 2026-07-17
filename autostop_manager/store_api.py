from __future__ import annotations

import json
import math
import re
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


STORE_AGENT_FORMAT = "store_agent_v1"
STORE_ENTITIES = frozenset(
    {
        "store_part",
        "store_order",
        "store_quote_request",
        "store_supplier",
        "store_batch",
        "store_warehouse_operation",
        "store_marketplace_listing",
        "store_state",
    }
)
STORE_MANAGEMENT_OPERATIONS = frozenset(
    {
        "assign_quote_request",
        "set_quote_request_status",
        "update_quote_request_comment",
        "set_batch_storage_location",
        "mark_order_ready",
    }
)
STORE_DETAIL_LEVELS = frozenset({"summary", "full"})
DEFAULT_STORE_LIMIT = 25
MAX_STORE_LIMIT = 100
MAX_CURSOR_CHARS = 4096
MAX_QUERY_CHARS = 200
MAX_FILTER_BYTES = 2000
MAX_ENTITY_ID_CHARS = 120
DEFAULT_RESPONSE_BUDGET_BYTES = 1_000_000
MAX_JSON_DEPTH = 12
MAX_JSON_CONTAINER_ITEMS = 500

_SNAKE_CASE_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_CORRELATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,159}$")
_ACTION_CHANGE_LIMITS = {
    "assign_quote_request": 1,
    "set_quote_request_status": 1,
    "update_quote_request_comment": 2,
    "set_batch_storage_location": 1,
    "mark_order_ready": 2,
}
_ENVELOPE_FIELDS = frozenset(
    {
        "ok",
        "format",
        "status",
        "summary",
        "items",
        "changes",
        "page",
        "warnings",
        "meta",
        "generated_at",
    }
)
_PAGE_FIELDS = frozenset({"has_more", "next_cursor", "replay_cursor", "limit"})
_READ_META_FIELDS = frozenset({"principal", "credential_key_id", "business_timezone", "snapshot_at"})
_ERROR_META_FIELDS = frozenset({"http_status"})
_ERROR_SUMMARY_FIELDS = frozenset({"error_code", "message", "details"})
_ERROR_DETAIL_FIELDS = frozenset(
    {
        "allowed",
        "current_generation",
        "current_updated_at",
        "issues",
        "min_available_position",
        "reset_required",
    }
)
_VALIDATION_ISSUE_FIELDS = frozenset({"location", "type", "message"})
_ENTITY_BASE_FIELDS = frozenset({"entity", "id", "entity_type", "entity_id", "updated_at", "deleted"})
_ENTITY_SUMMARY_FIELDS: dict[str, frozenset[str]] = {
    "store_part": frozenset(
        {
            "sku",
            "name",
            "manufacturer_name",
            "is_active",
            "physical_qty",
            "reserved_qty",
            "available_qty",
            "low_stock_threshold",
            "low_stock",
            "location_count",
            "data_quality_warnings",
        }
    ),
    "store_order": frozenset({"order_number", "status", "ready_at", "items_count", "total", "has_external_items"}),
    "store_quote_request": frozenset(
        {
            "request_number",
            "status",
            "assigned_user_id",
            "assigned_user_name",
            "items_count",
            "has_internal_comment",
            "internal_comment_sha256",
            "created_at",
        }
    ),
    "store_supplier": frozenset({"name", "is_active"}),
    "store_batch": frozenset(
        {
            "part_id",
            "part_sku",
            "part_name",
            "status",
            "qty_received",
            "qty_remaining",
            "storage_location",
            "received_at",
        }
    ),
    "store_warehouse_operation": frozenset(
        {"kind", "status", "order_id", "supplier_id", "items_count", "completed_at"}
    ),
    "store_marketplace_listing": frozenset(
        {
            "marketplace",
            "account_name",
            "part_id",
            "part_sku",
            "part_name",
            "status",
            "has_error",
            "failed_export_jobs",
            "last_synced_at",
        }
    ),
    "store_state": frozenset({"status", "low_stock_threshold", "counts"}),
}
_ENTITY_FULL_FIELDS: dict[str, frozenset[str]] = {
    "store_part": frozenset(
        {
            "category_id",
            "manufacturer_id",
            "active_batch_qty",
            "locations",
            "locations_count",
            "locations_has_more",
            "nested_limit",
        }
    ),
    "store_order": frozenset({"items", "items_has_more", "nested_limit"}),
    "store_quote_request": frozenset({"items", "items_has_more", "nested_limit"}),
    "store_supplier": frozenset(),
    "store_batch": frozenset({"supplier_id", "supplier_name"}),
    "store_warehouse_operation": frozenset({"items", "items_has_more", "nested_limit"}),
    "store_marketplace_listing": frozenset(
        {"has_last_error", "last_error_sha256", "error_code", "external_listing_id"}
    ),
    "store_state": frozenset({"allowed_entities"}),
}
_STORE_STATE_COUNT_FIELDS = frozenset(
    {
        "parts",
        "active_orders",
        "open_quote_requests",
        "failed_marketplace_listings",
        "failed_marketplace_export_jobs",
    }
)
_PART_LOCATION_FIELDS = frozenset(
    {
        "storage_location",
        "physical_qty",
        "reserved_qty",
        "available_qty",
        "batch_ids",
        "batch_ids_count",
        "batch_ids_has_more",
    }
)
_ORDER_ITEM_FIELDS = frozenset({"item_id", "source", "part_id", "sku", "name", "qty", "local_stock"})
_LOCAL_STOCK_FIELDS = frozenset({"physical_qty", "reserved_qty", "available_qty"})
_QUOTE_ITEM_FIELDS = frozenset({"item_id", "has_part_description", "part_description_sha256", "quantity"})
_WAREHOUSE_ITEM_FIELDS = frozenset({"item_id", "part_id", "part_sku", "part_name", "qty", "storage_location"})
_RUNTIME_SUMMARY_FIELDS = frozenset(
    {
        "service",
        "status",
        "api_version",
        "format",
        "read_contract",
        "cursor_version",
        "cursor_versions",
        "max_page_limit",
        "change_feed",
        "state",
    }
)
_CURSOR_VERSION_FIELDS = frozenset({"digest", "search", "legacy_timestamp"})
_CHANGE_FEED_FIELDS = frozenset(
    {
        "generation",
        "current_sequence",
        "min_available_position",
        "event_count",
        "oldest_changed_at",
        "newest_changed_at",
        "retention_mode",
    }
)
_DIGEST_SUMMARY_FIELDS = frozenset(
    {
        "snapshot_at",
        "business_timezone",
        "checkpoint_cursor",
        "changes_count",
        "changed_orders_count",
        "changed_quote_requests_count",
        "changed_quote_requests",
        "changed_orders",
        "order_status_distribution",
        "inventory",
        "latest_receipts",
        "latest_shipments",
        "marketplace_errors",
        "baseline",
    }
)
_STATUS_DISTRIBUTION_FIELDS = frozenset({"status", "count"})
_INVENTORY_FIELDS = frozenset(
    {
        "position_count",
        "physical_qty",
        "reserved_qty",
        "available_qty",
        "purchase_stock_value",
        "retail_stock_value",
        "low_stock_threshold",
        "low_stock_count",
        "low_stock_items",
    }
)
_LOW_STOCK_ITEM_FIELDS = frozenset({"entity_id", "sku", "name", "physical_qty", "reserved_qty", "available_qty"})
_COMPACT_OPERATION_FIELDS = frozenset(
    {
        "entity_id",
        "kind",
        "status",
        "order_id",
        "supplier_id",
        "items_count",
        "created_at",
        "completed_at",
    }
)
_MARKETPLACE_ERRORS_FIELDS = frozenset({"failed_listings", "failed_export_jobs", "latest"})
_MARKETPLACE_ERROR_ITEM_FIELDS = frozenset(
    {
        "job_id",
        "part_id",
        "account_id",
        "error_code",
        "has_error_message",
        "error_message_sha256",
        "created_at",
    }
)
_CHANGED_ORDER_FIELDS = frozenset({"entity_id", "order_number", "status", "items"})
_CHANGED_ORDER_ITEM_FIELDS = frozenset({"item_id", "source", "part_id", "sku", "name", "qty"})
_SEARCH_SUMMARY_FIELDS = frozenset({"entity", "matches"})
_ENTITY_CONTEXT_SUMMARY_FIELDS = frozenset({"entity", "entity_id", "detail"})
_ACTION_SUMMARY_FIELDS = frozenset({"operation", "mode", "target_id", "changed", "result"})
_ACTION_CHANGE_FIELDS = frozenset({"field", "before", "after"})
_ACTION_ALLOWED_CHANGE_NAMES = {
    "assign_quote_request": frozenset({"assigned_user_id"}),
    "set_quote_request_status": frozenset({"status"}),
    "update_quote_request_comment": frozenset({"has_internal_comment", "internal_comment_sha256"}),
    "set_batch_storage_location": frozenset({"storage_location"}),
    "mark_order_ready": frozenset({"status", "ready_at"}),
}
_ACTION_META_FIELDS = frozenset(
    {
        "correlation_id",
        "idempotency_key",
        "idempotency_replay",
        "owner_intent_present",
        "owner_intent_sha256",
        "effects",
        "external_effect_state",
        "dry_run_proof_ttl_seconds",
        "external_effect_manual_reconciliation_required",
        "external_effect_reconciliation_reason",
        "external_effect_reconciled_at",
    }
)
_ACTION_RESULT_FIELDS: dict[str, frozenset[str]] = {
    "assign_quote_request": frozenset(
        {
            "entity_type",
            "entity_id",
            "request_number",
            "status",
            "assigned_user_id",
            "has_internal_comment",
            "internal_comment_sha256",
            "updated_at",
        }
    ),
    "set_quote_request_status": frozenset(
        {
            "entity_type",
            "entity_id",
            "request_number",
            "status",
            "assigned_user_id",
            "has_internal_comment",
            "internal_comment_sha256",
            "updated_at",
        }
    ),
    "update_quote_request_comment": frozenset(
        {
            "entity_type",
            "entity_id",
            "request_number",
            "status",
            "assigned_user_id",
            "has_internal_comment",
            "internal_comment_sha256",
            "updated_at",
        }
    ),
    "set_batch_storage_location": frozenset({"entity_type", "entity_id", "part_id", "storage_location", "updated_at"}),
    "mark_order_ready": frozenset({"entity_type", "entity_id", "order_number", "status", "ready_at", "updated_at"}),
}
_ACTION_EFFECT_FIELDS: dict[str, frozenset[str]] = {
    "set_order_status": frozenset({"effect", "status"}),
    "set_ready_at": frozenset({"effect"}),
    "sync_shipment_draft": frozenset({"effect", "applies", "local_items"}),
    "create_internal_order_ready_notification": frozenset({"effect", "applies"}),
    "attempt_external_customer_notifier_after_commit": frozenset(
        {
            "effect",
            "applies",
            "best_effort",
            "configured",
            "customer_linked",
            "cached_chat_available",
            "deliverability",
        }
    ),
}
_WARNING_VALUES = frozenset(
    {
        "since_replayed_from_change_feed_origin_for_no_skip",
        "external_notifier_is_best_effort",
        "dry_run_proof_expires_in_30_minutes",
        "external_notifier_delivery_outcome_uncertain_manual_reconciliation_required",
    }
)
_ALWAYS_REDACT_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "manage_token",
    "password",
    "read_token",
    "refresh_token",
    "secret",
    "token",
}
_CONTACT_REDACT_KEYS = {
    "address",
    "buyer",
    "buyer_name",
    "client",
    "client_email",
    "client_name",
    "client_phone",
    "customer_email",
    "customer_name",
    "customer_phone",
    "contact",
    "contacts",
    "customer",
    "delivery_address",
    "email",
    "full_name",
    "phone",
    "phone_number",
    "recipient",
    "recipient_name",
}
_IDENTIFIER_REDACT_KEYS = {"license_plate", "vin"}


class _RejectRedirectHandler(HTTPRedirectHandler):
    """Turn every redirect into HTTPError before urllib can forward auth."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = build_opener(_RejectRedirectHandler())


def urlopen(request: Request, *, timeout: float):
    """Compatibility seam for tests backed by an opener that never redirects."""

    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


def clamp_store_limit(limit: int, *, default: int = DEFAULT_STORE_LIMIT) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, MAX_STORE_LIMIT))


class StoreApiClient:
    """Bounded internal AutoStop App client with runtime-injected credentials.

    The client intentionally has no filesystem, database, or environment access.
    Configuration is injected by the Manager composition root.
    """

    def __init__(
        self,
        *,
        api_url: str,
        read_token: str,
        manage_token: str,
        timeout: float = 8.0,
        max_read_attempts: int = 2,
        max_response_bytes: int = DEFAULT_RESPONSE_BUDGET_BYTES,
        failure_threshold: int = 3,
        circuit_cooldown_seconds: float = 30.0,
    ) -> None:
        self.api_url = str(api_url or "").strip().rstrip("/")
        self._read_token = str(read_token or "").strip()
        self._manage_token = str(manage_token or "").strip()
        self.timeout = max(0.1, float(timeout))
        self.max_read_attempts = max(1, min(int(max_read_attempts), 3))
        self.max_response_bytes = max(1024, int(max_response_bytes))
        self.failure_threshold = max(1, int(failure_threshold))
        self.circuit_cooldown_seconds = max(0.0, float(circuit_cooldown_seconds))
        self._failure_count = 0
        self._circuit_opened_at: float | None = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return (
            "StoreApiClient("
            f"configured={bool(self.api_url and self._read_token)}, "
            f"manage_configured={bool(self.api_url and self._manage_token)}, "
            f"timeout={self.timeout!r})"
        )

    def runtime_status(self, *, live: bool = False) -> dict[str, Any]:
        local = self.local_status()
        if not live:
            return {
                "ok": local["read_configured"],
                "format": STORE_AGENT_FORMAT,
                "status": "ready" if local["read_configured"] and not local["circuit_open"] else "degraded",
                "summary": local,
                "items": [],
                "page": {},
                "warnings": [] if local["read_configured"] else ["store_read_not_configured"],
                "meta": {"source": "autostop_store_adapter", "live_check": False},
            }
        result = self._request(
            "GET",
            "/runtime-status",
            expected_item_limit=1,
            expected_change_limit=0,
            response_contract="runtime",
        )
        summary = result.get("summary")
        if isinstance(summary, dict):
            result["summary"] = {**summary, "adapter": self.local_status()}
        meta = result.get("meta")
        if isinstance(meta, dict):
            meta["live_check"] = True
        return result

    def local_status(self) -> dict[str, Any]:
        circuit_open = self._circuit_is_open()
        with self._lock:
            failure_count = self._failure_count
        return {
            "read_configured": bool(self.api_url and self._read_token),
            "manage_configured": bool(self.api_url and self._manage_token),
            "circuit_open": circuit_open,
            "consecutive_failures": failure_count,
        }

    def digest(
        self,
        *,
        baseline: bool = False,
        since: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_STORE_LIMIT,
    ) -> dict[str, Any]:
        page_limit = clamp_store_limit(limit)
        query: dict[str, Any] = {"baseline": "true" if baseline else "false", "limit": page_limit}
        if since:
            query["since"] = str(since)
        if cursor:
            query["cursor"] = self._validate_cursor(cursor)
        return self._request(
            "GET",
            "/digest",
            query=query,
            expected_item_limit=page_limit,
            expected_change_limit=page_limit,
            response_contract="digest",
        )

    def search(
        self,
        *,
        entity: str,
        query_text: str = "",
        filters: dict[str, Any] | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_STORE_LIMIT,
    ) -> dict[str, Any]:
        normalized_entity = self._validate_entity(entity)
        page_limit = clamp_store_limit(limit)
        normalized_query = str(query_text or "").strip()
        if len(normalized_query) > MAX_QUERY_CHARS:
            raise ValueError("store search query is too large")
        query: dict[str, Any] = {
            "entity": normalized_entity,
            "query": normalized_query,
            "limit": page_limit,
        }
        if filters is not None:
            if not isinstance(filters, dict):
                raise ValueError("store search filters must be an object")
            encoded_filters = json.dumps(filters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if len(encoded_filters.encode("utf-8")) > MAX_FILTER_BYTES:
                raise ValueError("store search filters are too large")
            if filters:
                query["filters"] = encoded_filters
        if cursor:
            query["cursor"] = self._validate_cursor(cursor)
        return self._request(
            "GET",
            "/search",
            query=query,
            expected_item_limit=page_limit,
            expected_change_limit=0,
            response_contract="search",
            expected_entity=normalized_entity,
        )

    def entity_context(
        self,
        *,
        entity: str,
        entity_id: str,
        detail: str = "summary",
    ) -> dict[str, Any]:
        normalized_entity = self._validate_entity(entity)
        normalized_id = str(entity_id or "").strip()
        if not normalized_id:
            raise ValueError("entity_id is required")
        if len(normalized_id) > MAX_ENTITY_ID_CHARS:
            raise ValueError("entity_id is too large")
        normalized_detail = str(detail or "summary").strip().casefold()
        if normalized_detail not in STORE_DETAIL_LEVELS:
            raise ValueError("unsupported store detail level")
        path = f"/entities/{quote(normalized_entity, safe='')}/{quote(normalized_id, safe='')}"
        return self._request(
            "GET",
            path,
            query={"detail": normalized_detail},
            expected_item_limit=1,
            expected_change_limit=0,
            response_contract="entity",
            expected_entity=normalized_entity,
            expected_detail=normalized_detail,
        )

    def management_action(
        self,
        *,
        operation: str,
        target_id: str,
        expected_updated_at: str,
        owner_intent: str,
        idempotency_key: str,
        correlation_id: str,
        mode: str,
        planned_changes: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_operation = str(operation or "").strip().casefold()
        if normalized_operation not in STORE_MANAGEMENT_OPERATIONS:
            raise ValueError("unsupported store management operation")
        normalized_mode = str(mode or "").strip().casefold()
        if normalized_mode not in {"dry_run", "apply"}:
            raise ValueError("mode must be dry_run or apply")
        normalized_correlation_id = str(correlation_id or "").strip()
        if _CORRELATION_ID.fullmatch(normalized_correlation_id) is None:
            raise ValueError("correlation_id has an invalid format")
        body = {
            "target_id": str(target_id or "").strip(),
            "expected_updated_at": str(expected_updated_at or "").strip(),
            "owner_intent": str(owner_intent or "").strip(),
            "idempotency_key": str(idempotency_key or "").strip(),
            "correlation_id": normalized_correlation_id,
            "mode": normalized_mode,
            "planned_changes": dict(planned_changes or {}),
        }
        return self._request(
            "POST",
            f"/actions/{quote(normalized_operation, safe='')}",
            json_body=body,
            manage=True,
            expected_item_limit=0,
            expected_change_limit=_ACTION_CHANGE_LIMITS[normalized_operation],
            response_contract="action",
            expected_operation=normalized_operation,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        manage: bool = False,
        expected_item_limit: int,
        expected_change_limit: int,
        response_contract: str,
        expected_entity: str | None = None,
        expected_detail: str | None = None,
        expected_operation: str | None = None,
    ) -> dict[str, Any]:
        if not self.api_url:
            return self._error("store_api_url_missing", attempt_count=0)
        token = self._manage_token if manage else self._read_token
        if not token:
            return self._error("store_manage_token_missing" if manage else "store_read_token_missing", attempt_count=0)
        if self._circuit_is_open():
            return self._error("store_circuit_open", attempt_count=0)

        try:
            url = f"{self.api_url}{path}"
            if query:
                url = f"{url}?{urlencode(query)}"
            data = None
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "AutostopManager/0.1",
            }
            if json_body is not None:
                data = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                headers["Content-Type"] = "application/json"
        except (TypeError, UnicodeError, ValueError):
            return self._error("store_request_invalid", attempt_count=0)

        attempts = self.max_read_attempts if method == "GET" else 1
        last_error = "store_request_failed"
        attempt_count = 0
        http_status: int | None = None
        request_dispatched = False
        for attempt in range(1, attempts + 1):
            try:
                request = Request(url, data=data, headers=headers, method=method)
            except (TypeError, ValueError):
                return self._error("store_request_invalid", attempt_count=0)
            attempt_count = attempt
            request_dispatched = True
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    raw = response.read(self.max_response_bytes + 1)
                if len(raw) > self.max_response_bytes:
                    last_error = "store_response_too_large"
                    break
                payload = json.loads(raw.decode("utf-8"))
                self._validate_envelope(
                    payload,
                    expected_item_limit=expected_item_limit,
                    expected_change_limit=expected_change_limit,
                    response_contract=response_contract,
                    expected_entity=expected_entity,
                    expected_detail=expected_detail,
                    expected_operation=expected_operation,
                )
                self._record_success()
                redacted = _redact_payload(payload)
                meta = redacted.setdefault("meta", {})
                if isinstance(meta, dict):
                    meta.update(
                        {
                            "source": "autostop_store_api",
                            "attempt_count": attempt,
                            "request_dispatched": True,
                            "outcome_uncertain": False,
                        }
                    )
                return redacted
            except HTTPError as exc:
                status_code = int(exc.code)
                http_status = status_code
                last_error = f"store_http_{status_code}"
                if 300 <= status_code < 400:
                    last_error = "store_redirect_rejected"
                    break
                if 400 <= status_code < 500:
                    self._record_success()
                    parsed = self._decode_http_error(
                        exc,
                        expected_item_limit=expected_item_limit,
                        expected_change_limit=expected_change_limit,
                        attempt_count=attempt,
                        response_contract=response_contract,
                        expected_entity=expected_entity,
                        expected_detail=expected_detail,
                        expected_operation=expected_operation,
                    )
                    if parsed is not None:
                        return parsed
                    return self._error(
                        last_error,
                        attempt_count=attempt,
                        status="conflict" if status_code == 409 else "blocked",
                        request_dispatched=True,
                        outcome_uncertain=False,
                        http_status=status_code,
                    )
            except (TimeoutError, URLError):
                last_error = "store_timeout_or_network_error"
            except UnicodeDecodeError:
                last_error = "store_response_encoding_invalid"
                break
            except json.JSONDecodeError:
                last_error = "store_response_json_invalid"
                break
            except (OSError, RecursionError, TypeError, ValueError):
                last_error = "store_response_schema_invalid"
                break

        self._record_failure()
        return self._error(
            last_error,
            attempt_count=attempt_count,
            request_dispatched=request_dispatched,
            outcome_uncertain=method == "POST" and request_dispatched,
            http_status=http_status,
        )

    def _decode_http_error(
        self,
        error: HTTPError,
        *,
        expected_item_limit: int,
        expected_change_limit: int,
        attempt_count: int,
        response_contract: str,
        expected_entity: str | None,
        expected_detail: str | None,
        expected_operation: str | None,
    ) -> dict[str, Any] | None:
        try:
            raw = error.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                return None
            payload = json.loads(raw.decode("utf-8"))
            self._validate_envelope(
                payload,
                expected_item_limit=expected_item_limit,
                expected_change_limit=expected_change_limit,
                response_contract=response_contract,
                expected_entity=expected_entity,
                expected_detail=expected_detail,
                expected_operation=expected_operation,
            )
        except (OSError, RecursionError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None
        redacted = _redact_payload(payload)
        meta = redacted.setdefault("meta", {})
        if isinstance(meta, dict):
            meta.update(
                {
                    "source": "autostop_store_api",
                    "attempt_count": attempt_count,
                    "http_status": int(error.code),
                    "request_dispatched": True,
                    "outcome_uncertain": False,
                }
            )
        return redacted

    def _validate_envelope(
        self,
        payload: Any,
        *,
        expected_item_limit: int,
        expected_change_limit: int,
        response_contract: str,
        expected_entity: str | None,
        expected_detail: str | None,
        expected_operation: str | None,
    ) -> None:
        if not isinstance(payload, dict):
            raise ValueError("store response must be an object")
        if payload.get("format") != STORE_AGENT_FORMAT:
            raise ValueError("unexpected store response format")
        if not isinstance(payload.get("ok"), bool):
            raise ValueError("store response ok must be boolean")
        if not isinstance(payload.get("status"), str):
            raise ValueError("store response status must be a string")
        _validate_snake_case_keys(payload)
        _require_allowed_keys(payload, _ENVELOPE_FIELDS, path="<root>")
        for object_key in ("summary", "page", "meta"):
            if not isinstance(payload.get(object_key), dict):
                raise ValueError(f"{object_key} must be an object")
        warnings = payload.get("warnings")
        if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
            raise ValueError("warnings must be a string list")
        collection_limits = {
            "items": expected_item_limit,
            "changes": expected_change_limit,
        }
        for collection_key, collection_limit in collection_limits.items():
            collection = payload.get(collection_key, [])
            if not isinstance(collection, list):
                raise ValueError(f"{collection_key} must be a list")
            if len(collection) > collection_limit:
                raise ValueError(f"{collection_key} exceeds the bounded limit")
            if any(not isinstance(item, dict) for item in collection):
                raise ValueError(f"{collection_key} must contain objects")
        page = payload["page"]
        _require_allowed_keys(page, _PAGE_FIELDS, path="page")
        if page:
            if "has_more" in page and not isinstance(page["has_more"], bool):
                raise ValueError("page.has_more must be boolean")
            next_cursor = page.get("next_cursor")
            if next_cursor is not None and not isinstance(next_cursor, str):
                raise ValueError("page.next_cursor must be a string or null")
            if isinstance(next_cursor, str) and len(next_cursor) > MAX_CURSOR_CHARS:
                raise ValueError("page.next_cursor is too large")
            if page.get("has_more") is True and not next_cursor:
                raise ValueError("page.next_cursor is required when has_more is true")
            replay_cursor = page.get("replay_cursor")
            if replay_cursor is not None and not isinstance(replay_cursor, str):
                raise ValueError("page.replay_cursor must be a string or null")
            if isinstance(replay_cursor, str) and len(replay_cursor) > MAX_CURSOR_CHARS:
                raise ValueError("page.replay_cursor is too large")
        _validate_contract_payload(
            payload,
            response_contract=response_contract,
            expected_entity=expected_entity,
            expected_detail=expected_detail,
            expected_operation=expected_operation,
        )

    def _validate_entity(self, entity: str) -> str:
        normalized = str(entity or "").strip().casefold()
        if normalized not in STORE_ENTITIES:
            raise ValueError("unsupported store entity")
        return normalized

    def _validate_cursor(self, cursor: str) -> str:
        normalized = str(cursor or "").strip()
        if not normalized:
            raise ValueError("cursor cannot be empty")
        if len(normalized) > MAX_CURSOR_CHARS:
            raise ValueError("cursor is too large")
        return normalized

    def _circuit_is_open(self) -> bool:
        with self._lock:
            if self._circuit_opened_at is None:
                return False
            if time.monotonic() - self._circuit_opened_at < self.circuit_cooldown_seconds:
                return True
            self._circuit_opened_at = None
            self._failure_count = 0
            return False

    def _record_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            self._circuit_opened_at = None

    def _record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            if self._failure_count >= self.failure_threshold:
                self._circuit_opened_at = time.monotonic()

    def _error(
        self,
        code: str,
        *,
        attempt_count: int,
        status: str = "degraded",
        request_dispatched: bool = False,
        outcome_uncertain: bool = False,
        http_status: int | None = None,
    ) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "source": "autostop_store_adapter",
            "attempt_count": int(attempt_count),
            "read_configured": bool(self.api_url and self._read_token),
            "manage_configured": bool(self.api_url and self._manage_token),
            "request_dispatched": bool(request_dispatched),
            "outcome_uncertain": bool(outcome_uncertain),
        }
        if http_status is not None:
            meta["http_status"] = int(http_status)
        return {
            "ok": False,
            "format": STORE_AGENT_FORMAT,
            "status": status,
            "summary": {"error_code": str(code)},
            "items": [],
            "changes": [],
            "page": {},
            "warnings": [str(code)],
            "meta": meta,
        }


def _require_allowed_keys(value: Any, allowed: frozenset[str], *, path: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    unknown = sorted(str(key) for key in value if str(key) not in allowed)
    if unknown:
        raise ValueError(f"unknown Store field at {path}: {unknown[0]}")


def _require_object_list(value: Any, *, path: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{path} must be an object list")
    return value


def _validate_entity_projection(
    item: dict[str, Any],
    *,
    expected_entity: str | None = None,
    detail: str = "summary",
    path: str,
) -> None:
    entity = str(item.get("entity_type") or item.get("entity") or expected_entity or "")
    if entity not in STORE_ENTITIES:
        raise ValueError(f"{path} has an unsupported Store entity")
    for alias in ("entity", "entity_type"):
        if item.get(alias) not in (None, "", entity):
            raise ValueError(f"{path}.{alias} does not match the Store entity")
    if expected_entity is not None and entity != expected_entity:
        raise ValueError(f"{path} does not match the requested Store entity")
    allowed = _ENTITY_BASE_FIELDS | _ENTITY_SUMMARY_FIELDS[entity]
    if detail == "full":
        allowed |= _ENTITY_FULL_FIELDS[entity]
    _require_allowed_keys(item, allowed, path=path)

    if "counts" in item:
        _require_allowed_keys(item["counts"], _STORE_STATE_COUNT_FIELDS, path=f"{path}.counts")
    if "locations" in item:
        for index, location in enumerate(_require_object_list(item["locations"], path=f"{path}.locations")):
            _require_allowed_keys(
                location,
                _PART_LOCATION_FIELDS,
                path=f"{path}.locations[{index}]",
            )
    if "items" in item:
        nested_fields = {
            "store_order": _ORDER_ITEM_FIELDS,
            "store_quote_request": _QUOTE_ITEM_FIELDS,
            "store_warehouse_operation": _WAREHOUSE_ITEM_FIELDS,
        }.get(entity)
        if nested_fields is None:
            raise ValueError(f"{path}.items is not allowed for {entity}")
        for index, nested in enumerate(_require_object_list(item["items"], path=f"{path}.items")):
            _require_allowed_keys(nested, nested_fields, path=f"{path}.items[{index}]")
            if entity == "store_order" and "local_stock" in nested:
                _require_allowed_keys(
                    nested["local_stock"],
                    _LOCAL_STOCK_FIELDS,
                    path=f"{path}.items[{index}].local_stock",
                )
    if "data_quality_warnings" in item:
        warnings = item["data_quality_warnings"]
        if not isinstance(warnings, list) or any(
            not isinstance(value, str) or re.fullmatch(r"[a-z0-9_]{1,120}", value) is None for value in warnings
        ):
            raise ValueError(f"{path}.data_quality_warnings must contain fixed codes")
    if "allowed_entities" in item:
        entities = item["allowed_entities"]
        if not isinstance(entities, list) or any(value not in STORE_ENTITIES for value in entities):
            raise ValueError(f"{path}.allowed_entities contains an unsupported entity")


def _validate_digest_summary(summary: dict[str, Any]) -> None:
    _require_allowed_keys(summary, _DIGEST_SUMMARY_FIELDS, path="summary")
    for key in ("changed_quote_requests",):
        if key in summary:
            for index, item in enumerate(_require_object_list(summary[key], path=f"summary.{key}")):
                _validate_entity_projection(
                    item,
                    expected_entity="store_quote_request",
                    path=f"summary.{key}[{index}]",
                )
    if "changed_orders" in summary:
        for index, order in enumerate(_require_object_list(summary["changed_orders"], path="summary.changed_orders")):
            _require_allowed_keys(
                order,
                _CHANGED_ORDER_FIELDS,
                path=f"summary.changed_orders[{index}]",
            )
            if "items" in order:
                for item_index, item in enumerate(
                    _require_object_list(order["items"], path=f"summary.changed_orders[{index}].items")
                ):
                    _require_allowed_keys(
                        item,
                        _CHANGED_ORDER_ITEM_FIELDS,
                        path=f"summary.changed_orders[{index}].items[{item_index}]",
                    )
    if "order_status_distribution" in summary:
        for index, item in enumerate(
            _require_object_list(
                summary["order_status_distribution"],
                path="summary.order_status_distribution",
            )
        ):
            _require_allowed_keys(
                item,
                _STATUS_DISTRIBUTION_FIELDS,
                path=f"summary.order_status_distribution[{index}]",
            )
    if "inventory" in summary:
        inventory = summary["inventory"]
        _require_allowed_keys(inventory, _INVENTORY_FIELDS, path="summary.inventory")
        if "low_stock_items" in inventory:
            for index, item in enumerate(
                _require_object_list(inventory["low_stock_items"], path="summary.inventory.low_stock_items")
            ):
                _require_allowed_keys(
                    item,
                    _LOW_STOCK_ITEM_FIELDS,
                    path=f"summary.inventory.low_stock_items[{index}]",
                )
    for key in ("latest_receipts", "latest_shipments"):
        if key in summary:
            for index, item in enumerate(_require_object_list(summary[key], path=f"summary.{key}")):
                _require_allowed_keys(
                    item,
                    _COMPACT_OPERATION_FIELDS,
                    path=f"summary.{key}[{index}]",
                )
    if "marketplace_errors" in summary:
        errors = summary["marketplace_errors"]
        _require_allowed_keys(errors, _MARKETPLACE_ERRORS_FIELDS, path="summary.marketplace_errors")
        if "latest" in errors:
            for index, item in enumerate(
                _require_object_list(errors["latest"], path="summary.marketplace_errors.latest")
            ):
                _require_allowed_keys(
                    item,
                    _MARKETPLACE_ERROR_ITEM_FIELDS,
                    path=f"summary.marketplace_errors.latest[{index}]",
                )


def _validate_runtime_summary(summary: dict[str, Any]) -> None:
    _require_allowed_keys(summary, _RUNTIME_SUMMARY_FIELDS, path="summary")
    if "cursor_versions" in summary:
        _require_allowed_keys(summary["cursor_versions"], _CURSOR_VERSION_FIELDS, path="summary.cursor_versions")
    if "change_feed" in summary:
        _require_allowed_keys(summary["change_feed"], _CHANGE_FEED_FIELDS, path="summary.change_feed")
    if "state" in summary:
        _validate_entity_projection(
            summary["state"],
            expected_entity="store_state",
            detail="full",
            path="summary.state",
        )


def _validate_error_payload(payload: dict[str, Any]) -> None:
    _require_allowed_keys(payload["summary"], _ERROR_SUMMARY_FIELDS, path="summary")
    if "details" in payload["summary"]:
        details = payload["summary"]["details"]
        _require_allowed_keys(details, _ERROR_DETAIL_FIELDS, path="summary.details")
        if "issues" in details:
            for index, issue in enumerate(_require_object_list(details["issues"], path="summary.details.issues")):
                _require_allowed_keys(
                    issue,
                    _VALIDATION_ISSUE_FIELDS,
                    path=f"summary.details.issues[{index}]",
                )
    _require_allowed_keys(payload["meta"], _ERROR_META_FIELDS | {"snapshot_at"}, path="meta")
    if payload.get("items") or payload.get("changes"):
        raise ValueError("Store error envelopes cannot contain entity payloads")


def _validate_action_payload(payload: dict[str, Any], operation: str) -> None:
    summary = payload["summary"]
    _require_allowed_keys(summary, _ACTION_SUMMARY_FIELDS, path="summary")
    if summary.get("operation") not in (None, "", operation):
        raise ValueError("Store action response operation mismatch")
    result = summary.get("result")
    if result is not None:
        _require_allowed_keys(result, _ACTION_RESULT_FIELDS[operation], path="summary.result")
    for index, change in enumerate(payload.get("changes") or []):
        _require_allowed_keys(change, _ACTION_CHANGE_FIELDS, path=f"changes[{index}]")
        if change.get("field") not in _ACTION_ALLOWED_CHANGE_NAMES[operation]:
            raise ValueError("Store action returned a non-allowlisted change field")
    if payload.get("items"):
        raise ValueError("Store action envelopes cannot contain read items")
    _require_allowed_keys(payload["meta"], _ACTION_META_FIELDS | {"snapshot_at"}, path="meta")
    effects = payload["meta"].get("effects", [])
    if not isinstance(effects, list) or any(not isinstance(effect, dict) for effect in effects):
        raise ValueError("meta.effects must be an object list")
    for index, effect in enumerate(effects):
        effect_name = str(effect.get("effect") or "")
        allowed = _ACTION_EFFECT_FIELDS.get(effect_name)
        if allowed is None:
            raise ValueError("Store action returned a non-allowlisted external effect")
        _require_allowed_keys(effect, allowed, path=f"meta.effects[{index}]")


def _validate_contract_payload(
    payload: dict[str, Any],
    *,
    response_contract: str,
    expected_entity: str | None,
    expected_detail: str | None,
    expected_operation: str | None,
) -> None:
    if not payload["ok"]:
        _validate_error_payload(payload)
        return
    if any(warning not in _WARNING_VALUES for warning in payload.get("warnings") or []):
        raise ValueError("Store response contains a non-allowlisted warning")
    if response_contract == "action" and expected_operation in STORE_MANAGEMENT_OPERATIONS:
        _validate_action_payload(payload, expected_operation)
        return
    _require_allowed_keys(payload["meta"], _READ_META_FIELDS, path="meta")
    if response_contract == "runtime":
        _validate_runtime_summary(payload["summary"])
        if payload.get("items") or payload.get("changes"):
            raise ValueError("runtime-status cannot contain entity payloads")
        return
    if response_contract == "digest":
        _validate_digest_summary(payload["summary"])
        for collection_name in ("items", "changes"):
            for index, item in enumerate(payload.get(collection_name) or []):
                _validate_entity_projection(item, path=f"{collection_name}[{index}]")
        return
    if response_contract == "search":
        _require_allowed_keys(payload["summary"], _SEARCH_SUMMARY_FIELDS, path="summary")
        if payload["summary"].get("entity") not in (None, "", expected_entity):
            raise ValueError("Store search response entity mismatch")
        for index, item in enumerate(payload.get("items") or []):
            _validate_entity_projection(
                item,
                expected_entity=expected_entity,
                path=f"items[{index}]",
            )
        if payload.get("changes"):
            raise ValueError("Store search cannot contain change payloads")
        return
    if response_contract == "entity":
        _require_allowed_keys(payload["summary"], _ENTITY_CONTEXT_SUMMARY_FIELDS, path="summary")
        for index, item in enumerate(payload.get("items") or []):
            _validate_entity_projection(
                item,
                expected_entity=expected_entity,
                detail=expected_detail or "summary",
                path=f"items[{index}]",
            )
        if payload.get("changes"):
            raise ValueError("Store entity context cannot contain change payloads")
        return
    raise ValueError("unsupported Store response contract")


def _validate_snake_case_keys(value: Any, *, path: str = "", depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ValueError("store response exceeds maximum nesting depth")
    if isinstance(value, dict):
        if len(value) > MAX_JSON_CONTAINER_ITEMS:
            raise ValueError("store response object is too wide")
        for raw_key, nested in value.items():
            key = str(raw_key)
            if _SNAKE_CASE_KEY.fullmatch(key) is None:
                raise ValueError(f"non-snake-case key at {path or '<root>'}")
            _validate_snake_case_keys(
                nested,
                path=f"{path}.{key}" if path else key,
                depth=depth + 1,
            )
    elif isinstance(value, list):
        if len(value) > MAX_JSON_CONTAINER_ITEMS:
            raise ValueError("store response list is too wide")
        for index, nested in enumerate(value):
            _validate_snake_case_keys(nested, path=f"{path}[{index}]", depth=depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("store response contains a non-finite number")


def _redact_payload(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if depth > MAX_JSON_DEPTH:
        raise ValueError("store response exceeds maximum nesting depth")
    normalized_key = str(key or "").casefold()
    if _key_requires_redaction(normalized_key):
        return "[redacted]"
    if isinstance(value, dict):
        if len(value) > MAX_JSON_CONTAINER_ITEMS:
            raise ValueError("store response object is too wide")
        return {
            str(nested_key): _redact_payload(
                nested_value,
                key=str(nested_key),
                depth=depth + 1,
            )
            for nested_key, nested_value in value.items()
        }
    if isinstance(value, list):
        if len(value) > MAX_JSON_CONTAINER_ITEMS:
            raise ValueError("store response list is too wide")
        return [_redact_payload(item, key=key, depth=depth + 1) for item in value]
    return value


def _key_requires_redaction(key: str) -> bool:
    normalized = str(key or "").strip().casefold()
    if normalized in _ALWAYS_REDACT_KEYS or normalized in _IDENTIFIER_REDACT_KEYS:
        return True
    if normalized in _CONTACT_REDACT_KEYS:
        return True
    key_tokens = set(normalized.split("_"))
    if key_tokens & {"password", "secret", "token"}:
        return True
    return normalized.endswith(
        (
            "_address",
            "_api_key",
            "_email",
            "_license_plate",
            "_phone",
            "_vin",
        )
    )
