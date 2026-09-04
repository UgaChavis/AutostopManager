"""Typed, privacy-safe Store quote transport over the work Telegram bridge.

The conductor receives this object only through its narrow
``StoreQuoteTelegramSender`` protocol. It never resolves a peer, accepts a
message id, or returns customer text. A deployment composition root must inject
the transport; tests may use an in-process RPC.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .store_quote_conductor import (
    StoreQuoteTelegramDelivery,
    StoreQuoteTelegramInboundReply,
    StoreQuoteTelegramSender,
)
from .telegram_bridge import BridgeError, send_local_request


BridgeRpc = Callable[[dict[str, Any]], dict[str, Any]]

_HASH = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT = re.compile(r"^[A-Za-z0-9_-]{24,256}$")
_ERROR = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,119}$")
_CATEGORIES = frozenset({"clarification", "addition", "selection", "consent", "decline", "ambiguous"})
_IDENTITY_CATEGORIES = frozenset({"confirmed", "declined", "ambiguous"})
_HASH_FIELDS = frozenset(
    {
        "quote_ref_sha256",
        "revision_sha256",
        "message_sha256",
        "context_hash",
        "published_snapshot_hash",
        "delivery_binding_sha256",
        "route_binding_sha256",
        "delivery_ref_sha256",
        "reply_text_sha256",
        "incoming_ref_sha256",
        "inbound_binding_sha256",
        "dry_run_proof",
    }
)
_BOOL_FIELDS = frozenset(
    {
        "recipient_bound",
        "recipient_confirmed",
        "private_target_confirmed",
        "unique_target_confirmed",
        "work_account_confirmed",
        "delivery_confirmed",
        "reply_confirmed",
        "reply_sender_matches_delivery",
        "identity_pending",
        "identity_confirmed",
    }
)


def create_work_store_quote_transport(
    *,
    socket_path: Path | None = None,
    rpc: BridgeRpc | None = None,
) -> WorkStoreQuoteTransport:
    """Build a transport from an explicit fixed socket or injected test RPC."""

    if rpc is not None and socket_path is not None:
        raise ValueError("store_quote_telegram_transport_configuration_ambiguous")
    if rpc is None:
        if socket_path is None:
            raise ValueError("store_quote_telegram_transport_socket_required")
        if not isinstance(socket_path, Path) or not socket_path.is_absolute():
            raise ValueError("store_quote_telegram_transport_socket_invalid")

        def _socket_rpc(request: dict[str, Any]) -> dict[str, Any]:
            return send_local_request(socket_path, request)

        rpc = _socket_rpc
    return WorkStoreQuoteTransport(rpc=rpc)


class WorkStoreQuoteTransport(StoreQuoteTelegramSender):
    """Adapter for the bridge's five typed Store quote RPC operations.

    Recipient and identity setup methods are explicit privileged seams.  They
    are not part of ``StoreQuoteTelegramSender`` and must never be exposed as
    generic Manager tools.  A caller cannot assert recipient confirmation:
    only an independently reread direct reply to the neutral identity prompt
    promotes the stable quote route.
    """

    def __init__(self, *, rpc: BridgeRpc) -> None:
        if not callable(rpc):
            raise ValueError("store_quote_telegram_transport_rpc_required")
        self._rpc = rpc

    def bind_work_quote_recipient(
        self,
        *,
        delivery: StoreQuoteTelegramDelivery,
        peer: str,
    ) -> dict[str, Any]:
        """Bind a normal delivery only after its quote route is confirmed.

        The bridge resolves ``peer`` transiently and requires it to match the
        recipient which a prior verified identity reply promoted.  The returned
        projection never includes it, and the conductor does not call this
        method.
        """

        request = {
            "operation": "store_quote_bind_recipient",
            **_delivery_request_projection(delivery),
            "peer": str(peer or ""),
        }
        return self._invoke(request)

    def bind_work_quote_identity_candidate(
        self,
        *,
        delivery: StoreQuoteTelegramDelivery,
        peer: str,
    ) -> dict[str, Any]:
        """Bind one private candidate to the exact built-in neutral prompt.

        This creates only a *pending* stable quote route.  It deliberately
        carries the prompt text so the bridge can verify the literal neutral
        wording before any Telegram send; no caller-supplied boolean can turn
        that candidate into a confirmed client.
        """

        return self._invoke(
            {
                "operation": "store_quote_bind_identity_candidate",
                **_delivery_request_projection(delivery, include_text=True),
                "peer": str(peer or ""),
            }
        )

    def send_work_quote_message(
        self,
        *,
        delivery: StoreQuoteTelegramDelivery,
        idempotency_key: str,
        correlation_id: str,
        mode: str,
        dry_run_proof: str | None = None,
    ) -> dict[str, Any]:
        request = {
            "operation": "store_quote_send",
            **_delivery_request_projection(delivery, include_text=True),
            "idempotency_key": str(idempotency_key or ""),
            "correlation_id": str(correlation_id or ""),
            "mode": str(mode or ""),
        }
        if dry_run_proof is not None:
            request["dry_run_proof"] = str(dry_run_proof)
        return self._invoke(request)

    def readback_work_quote_message(
        self,
        *,
        delivery: StoreQuoteTelegramDelivery,
        correlation_id: str,
    ) -> dict[str, Any]:
        return self._invoke(
            {
                "operation": "store_quote_readback",
                **_delivery_request_projection(delivery),
                "correlation_id": str(correlation_id or ""),
            }
        )

    def mint_work_quote_inbound_receipt(
        self,
        *,
        delivery: StoreQuoteTelegramDelivery,
        delivery_ref_sha256: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        """Return an opaque receipt plus a non-authoritative safe category hint."""

        return self._invoke(
            {
                "operation": "store_quote_mint_inbound_receipt",
                **_delivery_request_projection(delivery),
                "delivery_ref_sha256": str(delivery_ref_sha256 or ""),
                "correlation_id": str(correlation_id or ""),
            },
            allow_receipt=True,
        )

    def mint_work_quote_identity_receipt(
        self,
        *,
        delivery: StoreQuoteTelegramDelivery,
        delivery_ref_sha256: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        """Mint one opaque receipt for the direct reply to an identity prompt."""

        return self._invoke(
            {
                "operation": "store_quote_mint_identity_receipt",
                **_delivery_request_projection(delivery),
                "delivery_ref_sha256": str(delivery_ref_sha256 or ""),
                "correlation_id": str(correlation_id or ""),
            },
            allow_receipt=True,
        )

    def readback_work_quote_identity_reply(
        self,
        *,
        delivery: StoreQuoteTelegramDelivery,
        delivery_ref_sha256: str,
        receipt: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        """Consume/reread identity proof and atomically report route promotion."""

        return self._invoke(
            {
                "operation": "store_quote_identity_readback",
                **_delivery_request_projection(delivery),
                "delivery_ref_sha256": str(delivery_ref_sha256 or ""),
                "receipt": str(receipt or ""),
                "correlation_id": str(correlation_id or ""),
            }
        )

    def readback_work_quote_reply(
        self,
        *,
        inbound: StoreQuoteTelegramInboundReply,
        correlation_id: str,
    ) -> dict[str, Any]:
        return self._invoke(
            {
                "operation": "store_quote_reply_readback",
                "quote_ref_sha256": _quote_ref_sha256(inbound.quote_request_id),
                "revision_sha256": inbound.revision_sha256,
                "published_snapshot_hash": inbound.published_snapshot_hash,
                "context_hash": inbound.context_hash,
                "delivery_binding_sha256": inbound.delivery_binding_sha256,
                "route_binding_sha256": _route_binding_sha256(
                    inbound.quote_request_id,
                    inbound.revision_sha256,
                    inbound.published_snapshot_hash,
                    inbound.context_hash,
                ),
                "delivery_ref_sha256": inbound.delivery_ref_sha256,
                "receipt": inbound.receipt,
                "reply_classification": inbound.classification,
                "correlation_id": str(correlation_id or ""),
            }
        )

    def _invoke(self, request: dict[str, Any], *, allow_receipt: bool = False) -> dict[str, Any]:
        try:
            response = self._rpc(request)
        except (BridgeError, OSError, TypeError, ValueError):
            return _error("store_quote_telegram_bridge_unavailable")
        if not isinstance(response, Mapping):
            return _error("store_quote_telegram_bridge_response_invalid")
        if response.get("ok") is not True:
            summary = response.get("summary")
            code = "store_quote_telegram_bridge_failed"
            if isinstance(summary, Mapping) and _ERROR.fullmatch(str(summary.get("error_code") or "")):
                code = str(summary["error_code"])
            elif _ERROR.fullmatch(str(response.get("error") or "")):
                code = str(response["error"])
            failure_result = _error(code)
            meta = response.get("meta")
            if isinstance(meta, Mapping) and meta.get("outcome_uncertain") is True:
                failure_result["meta"] = {"outcome_uncertain": True}
            return failure_result
        sanitized = _sanitized_summary(response.get("summary"))
        if sanitized is None:
            return _error("store_quote_telegram_bridge_projection_invalid")
        result: dict[str, Any] = {"ok": True, "summary": sanitized}
        if response.get("mode") in {"dry_run", "apply"}:
            result["mode"] = response["mode"]
        if response.get("replayed") is True:
            result["replayed"] = True
        meta = response.get("meta")
        if isinstance(meta, Mapping) and meta.get("outcome_uncertain") is True:
            result["meta"] = {"outcome_uncertain": True}
        receipt = response.get("receipt")
        if allow_receipt and isinstance(receipt, str) and _RECEIPT.fullmatch(receipt):
            result["receipt"] = receipt
        elif allow_receipt and receipt is not None:
            return _error("store_quote_telegram_bridge_projection_invalid")
        return result


def _delivery_request_projection(
    delivery: StoreQuoteTelegramDelivery,
    *,
    include_text: bool = False,
) -> dict[str, str]:
    if not isinstance(delivery, StoreQuoteTelegramDelivery):
        raise ValueError("store_quote_telegram_delivery_invalid")
    projection = {
        "quote_ref_sha256": _quote_ref_sha256(delivery.quote_request_id),
        "revision_sha256": _sha256(delivery.estimate_revision),
        "published_snapshot_hash": delivery.published_snapshot_hash,
        "context_hash": delivery.context_hash,
        "delivery_binding_sha256": delivery.binding_sha256,
        "route_binding_sha256": delivery.route_binding_sha256,
        "message_sha256": delivery.text_sha256,
        "message_kind": delivery.kind,
    }
    if include_text:
        # This is transient transport input, never copied to a response.
        projection["text"] = delivery.text
    return projection


def _quote_ref_sha256(quote_request_id: str) -> str:
    return _sha256(f"store-quote-conductor-v1\0{quote_request_id}")


def _route_binding_sha256(
    quote_request_id: str,
    revision_sha256: str,
    published_snapshot_hash: str,
    context_hash: str,
) -> str:
    return _sha256(
        f"store-quote-work-route-v2\0{_quote_ref_sha256(quote_request_id)}\0{revision_sha256}"
        f"\0{published_snapshot_hash}\0{context_hash}"
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _sanitized_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    for key in _HASH_FIELDS:
        candidate = value.get(key)
        if candidate is None:
            continue
        normalized = str(candidate).strip().casefold()
        if _HASH.fullmatch(normalized) is None:
            return None
        result[key] = normalized
    for key in _BOOL_FIELDS:
        candidate = value.get(key)
        if candidate is not None:
            if type(candidate) is not bool:
                return None
            result[key] = candidate
    category = value.get("reply_classification")
    if category is not None:
        normalized_category = str(category).strip().casefold()
        if normalized_category not in _CATEGORIES:
            return None
        result["reply_classification"] = normalized_category
    identity_category = value.get("identity_classification")
    if identity_category is not None:
        normalized_identity_category = str(identity_category).strip().casefold()
        if normalized_identity_category not in _IDENTITY_CATEGORIES:
            return None
        result["identity_classification"] = normalized_identity_category
    error_code = value.get("error_code")
    if error_code is not None:
        normalized_error = str(error_code).strip()
        if _ERROR.fullmatch(normalized_error) is None:
            return None
        result["error_code"] = normalized_error
    return result


def _error(code: str) -> dict[str, Any]:
    return {"ok": False, "summary": {"error_code": code}}
