from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .action_contract import prepare_action_contract
from .storage import ManagerMemoryStore, _now
from .store_api import MAX_STORE_LIMIT, STORE_AGENT_FORMAT, StoreApiClient, clamp_store_limit


STORE_DOMAIN_ACTIONS = {
    ("store_quote_request", "assign_quote_request"),
    ("store_quote_request", "set_quote_request_status"),
    ("store_quote_request", "update_quote_request_comment"),
    ("store_quote_request", "add_quote_request_note"),
    ("store_batch", "set_batch_storage_location"),
    ("store_order", "mark_order_ready"),
}
STORE_DIGEST_STREAMS = frozenset({"store_digest", "store_bootstrap"})


@dataclass(frozen=True)
class _DigestRequest:
    page_limit: int
    expected_state_version: int
    request_cursor: str | None
    effective_since: str | None
    effective_baseline: bool
    base_refs: Any
    replaying_unacknowledged: bool
    prior_delivery_acknowledged: bool = False
    acknowledged_cursor: str | None = None


class StoreIntegration:
    """Store API orchestration, compact cursor persistence, and safe writes."""

    def __init__(self, *, client: StoreApiClient, store: ManagerMemoryStore) -> None:
        self.client = client
        self.store = store

    def runtime_status(
        self,
        *,
        live: bool = False,
        bootstrap_snapshot: bool = False,
    ) -> dict[str, Any]:
        if bootstrap_snapshot:
            return self.client.bootstrap_snapshot()
        return self.client.runtime_status(live=live)

    def digest(
        self,
        *,
        baseline: bool = False,
        since: str | None = None,
        cursor: str | None = None,
        ack_token: str | None = None,
        limit: int = 25,
        stream: str = "store_digest",
    ) -> dict[str, Any]:
        normalized_stream = str(stream or "").strip().casefold()
        if normalized_stream not in STORE_DIGEST_STREAMS:
            return _error_envelope("invalid_store_digest_stream", status="blocked")
        page_limit = clamp_store_limit(limit)
        checkpoint = self.store.get_store_checkpoint(normalized_stream)
        if not checkpoint.get("ok"):
            return _error_envelope(str(checkpoint.get("error") or "store_checkpoint_read_failed"))
        prepared = self._prepare_digest_request(
            checkpoint=checkpoint,
            baseline=baseline,
            since=since,
            cursor=cursor,
            ack_token=ack_token,
            page_limit=page_limit,
            stream=normalized_stream,
        )
        if isinstance(prepared, dict):
            return prepared
        page_limit = prepared.page_limit
        expected_state_version = prepared.expected_state_version
        request_cursor = prepared.request_cursor
        effective_since = prepared.effective_since
        effective_baseline = prepared.effective_baseline
        base_refs = prepared.base_refs
        replaying_unacknowledged = prepared.replaying_unacknowledged
        prior_delivery_acknowledged = prepared.prior_delivery_acknowledged
        acknowledged_cursor = prepared.acknowledged_cursor

        page_result = self.client.digest(
            baseline=effective_baseline,
            since=effective_since,
            cursor=request_cursor,
            limit=page_limit,
        )
        if not page_result.get("ok"):
            error_code = _error_code(page_result)
            self.store.record_store_checkpoint_failure(
                stream=normalized_stream,
                error_code=error_code,
                expected_state_version=expected_state_version,
            )
            return {
                **page_result,
                "meta": {
                    **_dict(page_result.get("meta")),
                    "stream": normalized_stream,
                    "baseline": effective_baseline,
                    "checkpoint_advanced": False,
                    "delivery_acknowledged": prior_delivery_acknowledged,
                    "acknowledged_cursor": acknowledged_cursor,
                    "traversal_advanced": prior_delivery_acknowledged,
                },
            }

        summary = _dict(page_result.get("summary"))
        meta = _dict(page_result.get("meta"))
        page_items = _item_list(page_result)
        page_refs = [ref for item in page_items if (ref := _compact_ref(item))]
        compact_refs = _merge_compact_refs(base_refs, page_refs)
        page = _dict(page_result.get("page"))
        next_cursor = str(
            page.get("next_cursor") or meta.get("checkpoint_cursor") or summary.get("checkpoint_cursor") or ""
        ).strip()
        source_has_more = bool(page.get("has_more"))
        if not next_cursor:
            self.store.record_store_checkpoint_failure(
                stream=normalized_stream,
                error_code=(
                    "store_digest_missing_next_cursor" if source_has_more else "store_digest_missing_checkpoint_cursor"
                ),
                expected_state_version=expected_state_version,
            )
            return _error_envelope(
                "store_digest_missing_next_cursor" if source_has_more else "store_digest_missing_checkpoint_cursor",
                meta={
                    "stream": normalized_stream,
                    "baseline": effective_baseline,
                    "checkpoint_advanced": False,
                    "traversal_advanced": prior_delivery_acknowledged,
                },
            )

        snapshot_at = str(meta.get("snapshot_at") or summary.get("snapshot_at") or _now())
        visible_items = [] if effective_baseline else page_items
        delivery_ack_required = bool(visible_items) or source_has_more
        if delivery_ack_required:
            replay_cursor = str(page.get("replay_cursor") or "").strip()
            if not replay_cursor:
                self.store.record_store_checkpoint_failure(
                    stream=normalized_stream,
                    error_code="store_digest_missing_replay_cursor",
                    expected_state_version=expected_state_version,
                )
                return _error_envelope(
                    "store_digest_missing_replay_cursor",
                    meta={
                        "stream": normalized_stream,
                        "baseline": effective_baseline,
                        "checkpoint_advanced": False,
                    },
                )
            if replaying_unacknowledged:
                expected_page_refs = _page_ref_membership(checkpoint.get("pending_page_refs"))
                observed_page_refs = _page_ref_membership(page_refs)
                replay_matches = (
                    replay_cursor == str(checkpoint.get("pending_request_cursor") or "").strip()
                    and next_cursor == str(checkpoint.get("pending_cursor") or "").strip()
                    and source_has_more == bool(checkpoint.get("pending_page_has_more"))
                    and len(page_items) == len(page_refs)
                    and observed_page_refs == expected_page_refs
                )
                if not replay_matches:
                    self.store.record_store_checkpoint_failure(
                        stream=normalized_stream,
                        error_code="store_digest_replay_mismatch",
                        expected_state_version=expected_state_version,
                    )
                    return _error_envelope(
                        "store_digest_replay_mismatch",
                        status="conflict",
                        meta={
                            "stream": normalized_stream,
                            "baseline": effective_baseline,
                            "checkpoint_advanced": False,
                        },
                    )
                existing_delivery_token = str(checkpoint.get("pending_delivery_token") or "").strip()
                existing_pending_cursor = str(checkpoint.get("pending_cursor") or "").strip()
                delivery_cursor = _encode_delivery_cursor(
                    stream=normalized_stream,
                    next_cursor=existing_pending_cursor,
                    delivery_token=existing_delivery_token,
                )
                return _digest_envelope(
                    summary=summary,
                    page_items=page_items,
                    warnings=page_result.get("warnings"),
                    page_limit=page_limit,
                    baseline=effective_baseline,
                    has_more=True,
                    next_cursor=delivery_cursor,
                    stream=normalized_stream,
                    checkpoint_advanced=False,
                    checkpoint_state_version=expected_state_version,
                    snapshot_at=str(checkpoint.get("pending_snapshot_at") or snapshot_at),
                    source_has_more=source_has_more,
                    delivery_ack_required=True,
                    delivery_ack_token=existing_delivery_token,
                    delivery_acknowledged=prior_delivery_acknowledged,
                    acknowledged_cursor=acknowledged_cursor,
                    replayed_delivery=True,
                    traversal_advanced=prior_delivery_acknowledged,
                )
            delivery_token = _store_delivery_token(
                stream=normalized_stream,
                request_cursor=replay_cursor,
                request_since=None,
                next_cursor=next_cursor,
                snapshot_at=snapshot_at,
                baseline=effective_baseline,
                source_has_more=source_has_more,
                page_limit=page_limit,
                page_refs=page_refs,
            )
            delivery_cursor = _encode_delivery_cursor(
                stream=normalized_stream,
                next_cursor=next_cursor,
                delivery_token=delivery_token,
            )
            pending = self.store.record_store_checkpoint_pending(
                stream=normalized_stream,
                next_cursor=next_cursor,
                compact_refs=compact_refs,
                baseline=effective_baseline,
                expected_state_version=expected_state_version,
                request_cursor=replay_cursor,
                request_since=None,
                page_has_more=source_has_more,
                page_limit=page_limit,
                page_refs=_page_refs_without_versions(page_refs),
                snapshot_at=snapshot_at,
                delivery_token=delivery_token,
            )
            if not pending.get("ok"):
                return _error_envelope(
                    str(pending.get("error") or "store_checkpoint_pending_failed"),
                    status="conflict",
                    meta={
                        "stream": normalized_stream,
                        "baseline": effective_baseline,
                        "checkpoint_advanced": False,
                        "traversal_advanced": prior_delivery_acknowledged,
                    },
                )
            return _digest_envelope(
                summary=summary,
                page_items=page_items,
                warnings=page_result.get("warnings"),
                page_limit=page_limit,
                baseline=effective_baseline,
                has_more=True,
                next_cursor=delivery_cursor,
                stream=normalized_stream,
                checkpoint_advanced=False,
                checkpoint_state_version=pending.get("state_version"),
                snapshot_at=snapshot_at,
                source_has_more=source_has_more,
                delivery_ack_required=True,
                delivery_ack_token=delivery_token,
                delivery_acknowledged=prior_delivery_acknowledged,
                acknowledged_cursor=acknowledged_cursor,
                replayed_delivery=replaying_unacknowledged,
                traversal_advanced=prior_delivery_acknowledged,
            )

        committed = self.store.commit_store_checkpoint(
            stream=normalized_stream,
            cursor=next_cursor,
            last_success_at=snapshot_at,
            compact_refs=compact_refs,
            expected_state_version=expected_state_version,
        )
        if not committed.get("ok"):
            return _error_envelope(
                str(committed.get("error") or "store_checkpoint_commit_failed"),
                status="conflict",
                meta={
                    "stream": normalized_stream,
                    "baseline": effective_baseline,
                    "checkpoint_advanced": False,
                },
            )
        return _digest_envelope(
            summary=summary,
            page_items=page_items,
            warnings=page_result.get("warnings"),
            page_limit=page_limit,
            baseline=effective_baseline,
            has_more=False,
            next_cursor=next_cursor,
            stream=normalized_stream,
            checkpoint_advanced=True,
            checkpoint_state_version=committed.get("state_version"),
            snapshot_at=snapshot_at,
            source_has_more=False,
            delivery_ack_required=False,
            delivery_acknowledged=prior_delivery_acknowledged,
            acknowledged_cursor=acknowledged_cursor,
            replayed_delivery=replaying_unacknowledged,
            traversal_advanced=prior_delivery_acknowledged,
        )

    def _prepare_digest_request(
        self,
        *,
        checkpoint: dict[str, Any],
        baseline: bool,
        since: str | None,
        cursor: str | None,
        ack_token: str | None,
        page_limit: int,
        stream: str,
    ) -> _DigestRequest | dict[str, Any]:
        explicit_cursor = str(cursor or "").strip() or None
        explicit_ack_token = str(ack_token or "").strip() or None
        pending_cursor = str(checkpoint.get("pending_cursor") or "").strip() or None
        pending_delivery_token = str(checkpoint.get("pending_delivery_token") or "").strip() or None
        traversal_cursor = str(checkpoint.get("traversal_cursor") or "").strip() or None
        last_ack_cursor = str(checkpoint.get("last_ack_cursor") or "").strip() or None
        last_ack_delivery_token = str(checkpoint.get("last_ack_delivery_token") or "").strip() or None
        expected_state_version = int(checkpoint.get("state_version") or 0)
        if explicit_ack_token and not explicit_cursor:
            return _error_envelope(
                "store_digest_ack_fields_required",
                status="conflict",
                meta={"stream": stream, "checkpoint_advanced": False},
            )

        if explicit_cursor is not None:
            decoded_ack = _decode_delivery_cursor(explicit_cursor)
            decoded_stream = str(_dict(decoded_ack).get("stream") or "")
            decoded_cursor = str(_dict(decoded_ack).get("next_cursor") or "")
            decoded_token = str(_dict(decoded_ack).get("delivery_token") or "")
            matches_pending = bool(
                decoded_ack is not None
                and pending_cursor
                and pending_delivery_token
                and decoded_stream == stream
                and decoded_cursor == pending_cursor
                and hmac.compare_digest(decoded_token, pending_delivery_token)
                and (explicit_ack_token is None or hmac.compare_digest(explicit_ack_token, pending_delivery_token))
            )
            matches_last_ack = bool(
                decoded_ack is not None
                and last_ack_cursor
                and last_ack_delivery_token
                and decoded_stream == stream
                and decoded_cursor == last_ack_cursor
                and hmac.compare_digest(decoded_token, last_ack_delivery_token)
                and (explicit_ack_token is None or hmac.compare_digest(explicit_ack_token, last_ack_delivery_token))
            )
            if not matches_pending and not matches_last_ack:
                return _digest_ack_error(stream)
            if matches_last_ack and bool(checkpoint.get("last_ack_was_final")):
                return _digest_ack_envelope(
                    stream=stream,
                    cursor=last_ack_cursor or decoded_cursor,
                    checkpoint_state_version=expected_state_version,
                    snapshot_at=str(checkpoint.get("last_ack_snapshot_at") or _now()),
                    page_limit=page_limit,
                )
            if matches_pending:
                pending_snapshot_at = str(checkpoint.get("pending_snapshot_at") or _now())
                if bool(checkpoint.get("pending_page_has_more")):
                    acknowledged = self.store.acknowledge_store_checkpoint_page(
                        stream=stream,
                        cursor=pending_cursor or "",
                        delivery_token=pending_delivery_token or "",
                        expected_state_version=expected_state_version,
                    )
                    if not acknowledged.get("ok"):
                        return _error_envelope(
                            str(acknowledged.get("error") or "store_digest_ack_failed"),
                            status="conflict",
                            meta={"stream": stream, "checkpoint_advanced": False},
                        )
                    return _DigestRequest(
                        page_limit=page_limit,
                        expected_state_version=int(acknowledged.get("state_version") or 0),
                        request_cursor=pending_cursor,
                        effective_since=None,
                        effective_baseline=False,
                        base_refs=acknowledged.get("traversal_refs"),
                        replaying_unacknowledged=False,
                        prior_delivery_acknowledged=True,
                        acknowledged_cursor=pending_cursor,
                    )
                committed = self.store.commit_store_checkpoint(
                    stream=stream,
                    cursor=pending_cursor or "",
                    last_success_at=pending_snapshot_at,
                    compact_refs=checkpoint.get("pending_refs"),
                    expected_state_version=expected_state_version,
                    acknowledged_delivery_token=pending_delivery_token,
                )
                if not committed.get("ok"):
                    return _error_envelope(
                        str(committed.get("error") or "store_digest_ack_failed"),
                        status="conflict",
                        meta={"stream": stream, "checkpoint_advanced": False},
                    )
                return _digest_ack_envelope(
                    stream=stream,
                    cursor=pending_cursor or "",
                    checkpoint_state_version=int(committed.get("state_version") or 0),
                    snapshot_at=pending_snapshot_at,
                    page_limit=page_limit,
                )
            if pending_cursor is not None:
                return _DigestRequest(
                    page_limit=clamp_store_limit(int(checkpoint.get("pending_page_limit") or page_limit)),
                    expected_state_version=expected_state_version,
                    request_cursor=str(checkpoint.get("pending_request_cursor") or "").strip() or None,
                    effective_since=str(checkpoint.get("pending_request_since") or "").strip() or None,
                    effective_baseline=bool(checkpoint.get("pending_baseline")),
                    base_refs=(
                        checkpoint.get("traversal_refs") if traversal_cursor else checkpoint.get("compact_refs")
                    ),
                    replaying_unacknowledged=True,
                    prior_delivery_acknowledged=True,
                    acknowledged_cursor=last_ack_cursor,
                )
            if traversal_cursor is not None:
                return _DigestRequest(
                    page_limit=page_limit,
                    expected_state_version=expected_state_version,
                    request_cursor=traversal_cursor,
                    effective_since=None,
                    effective_baseline=bool(checkpoint.get("traversal_baseline")),
                    base_refs=checkpoint.get("traversal_refs"),
                    replaying_unacknowledged=False,
                    prior_delivery_acknowledged=True,
                    acknowledged_cursor=last_ack_cursor,
                )
            return _digest_ack_error(stream)

        if pending_cursor is not None:
            return _DigestRequest(
                page_limit=clamp_store_limit(int(checkpoint.get("pending_page_limit") or page_limit)),
                expected_state_version=expected_state_version,
                request_cursor=str(checkpoint.get("pending_request_cursor") or "").strip() or None,
                effective_since=str(checkpoint.get("pending_request_since") or "").strip() or None,
                effective_baseline=bool(checkpoint.get("pending_baseline")),
                base_refs=(checkpoint.get("traversal_refs") if traversal_cursor else checkpoint.get("compact_refs")),
                replaying_unacknowledged=True,
            )
        if traversal_cursor is not None:
            return _DigestRequest(
                page_limit=page_limit,
                expected_state_version=expected_state_version,
                request_cursor=traversal_cursor,
                effective_since=None,
                effective_baseline=bool(checkpoint.get("traversal_baseline")),
                base_refs=checkpoint.get("traversal_refs"),
                replaying_unacknowledged=False,
            )

        explicit_since = str(since or "").strip() or None
        committed_cursor = str(checkpoint.get("cursor") or "").strip() or None
        effective_baseline = bool(baseline or (not committed_cursor and not explicit_since))
        request_cursor = None if baseline or explicit_since else committed_cursor
        effective_since = None
        if not request_cursor:
            effective_since = explicit_since or (str(checkpoint.get("last_success_at") or "").strip() or None)
        return _DigestRequest(
            page_limit=page_limit,
            expected_state_version=expected_state_version,
            request_cursor=request_cursor,
            effective_since=effective_since,
            effective_baseline=effective_baseline,
            base_refs=checkpoint.get("compact_refs"),
            replaying_unacknowledged=False,
        )

    def search(
        self,
        *,
        entity: str,
        query: str = "",
        filters: dict[str, Any] | None = None,
        cursor: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        try:
            return self.client.search(
                entity=entity,
                query_text=query,
                filters=filters,
                cursor=cursor,
                limit=limit,
            )
        except ValueError as exc:
            return _error_envelope(_validation_error_code(exc))

    def entity_context(self, *, entity: str, entity_id: str, detail: str = "summary") -> dict[str, Any]:
        try:
            return self.client.entity_context(entity=entity, entity_id=entity_id, detail=detail)
        except ValueError as exc:
            return _error_envelope(_validation_error_code(exc))

    def quote_vin_photo_preview(
        self,
        *,
        quote_request_id: str,
        expected_photo_sha256: str,
    ) -> dict[str, Any]:
        try:
            return self.client.quote_vin_photo_preview(
                quote_request_id=quote_request_id,
                expected_photo_sha256=expected_photo_sha256,
            )
        except ValueError as exc:
            return _error_envelope(_validation_error_code(exc))

    def management_action(
        self,
        *,
        domain: str,
        action: str,
        target_id: str,
        planned_changes: dict[str, Any] | None,
        owner_intent: str,
        expected_updated_at: str,
        idempotency_key: str,
        correlation_id: str,
        mode: str = "dry_run",
    ) -> dict[str, Any]:
        normalized_domain = str(domain or "").strip().casefold()
        normalized_action = str(action or "").strip().casefold()
        normalized_mode = str(mode or "").strip().casefold()
        if (normalized_domain, normalized_action) not in STORE_DOMAIN_ACTIONS:
            return _error_envelope("unsupported_store_management_operation", status="blocked")
        if normalized_mode not in {"dry_run", "apply"}:
            return _error_envelope("invalid_store_management_mode", status="blocked")

        contract = prepare_action_contract(
            domain=normalized_domain,
            action=normalized_action,
            target_id=target_id,
            planned_changes=planned_changes,
            owner_intent=owner_intent,
            expected_revision=expected_updated_at,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            dry_run=normalized_mode == "dry_run",
        )
        if not contract.get("ok") or not _dict(contract.get("execution")).get("ready"):
            return _error_envelope(
                "store_action_contract_blocked",
                status="blocked",
                summary={
                    "contract_id": contract.get("contract_id"),
                    "blocking_reasons": _dict(contract.get("preflight")).get("blocking_reasons", []),
                },
            )

        canonical_changes = _dict(contract.get("planned_changes"))
        canonical_target_id = str(_dict(contract.get("target")).get("id") or target_id)
        canonical_owner_intent = str(contract.get("owner_intent") or owner_intent)
        canonical_expected_updated_at = str(
            _dict(contract.get("concurrency")).get("expected_revision") or expected_updated_at
        )
        canonical_idempotency_key = str(_dict(contract.get("idempotency")).get("key") or idempotency_key)
        canonical_correlation_id = str(contract.get("correlation_id") or correlation_id)

        pre_state = self.entity_context(
            entity=normalized_domain,
            entity_id=canonical_target_id,
            detail="summary",
        )
        if not pre_state.get("ok"):
            return _error_envelope(
                "store_preflight_reread_failed",
                status="blocked",
                summary={"contract_id": contract.get("contract_id")},
            )
        if not _entity_matches(pre_state, entity=normalized_domain, entity_id=canonical_target_id):
            return _error_envelope(
                "store_preflight_target_mismatch",
                status="blocked",
                summary={"contract_id": contract.get("contract_id")},
            )
        observed_updated_at = _entity_field(pre_state, "updated_at")
        if not str(observed_updated_at or "").strip():
            return _error_envelope(
                "store_preflight_version_missing",
                status="blocked",
                summary={"contract_id": contract.get("contract_id")},
            )
        pre_read_revision_mismatch = str(observed_updated_at) != canonical_expected_updated_at
        if pre_read_revision_mismatch and normalized_mode != "apply":
            return _error_envelope(
                "store_expected_updated_at_mismatch",
                status="conflict",
                summary={"contract_id": contract.get("contract_id")},
            )

        try:
            result = self.client.management_action(
                operation=normalized_action,
                target_id=canonical_target_id,
                expected_updated_at=canonical_expected_updated_at,
                owner_intent=canonical_owner_intent,
                idempotency_key=canonical_idempotency_key,
                correlation_id=canonical_correlation_id,
                mode=normalized_mode,
                planned_changes=canonical_changes,
            )
        except ValueError as exc:
            return _error_envelope(_validation_error_code(exc), status="blocked")
        if not result.get("ok"):
            result_meta = _dict(result.get("meta"))
            if normalized_mode == "apply" and result_meta.get("outcome_uncertain") is True:
                reconcile_state = self.entity_context(
                    entity=normalized_domain,
                    entity_id=canonical_target_id,
                    detail="full" if normalized_action == "add_quote_request_note" else "summary",
                )
                post_state_read = reconcile_state.get("ok") is True
                target_matches = post_state_read and _entity_matches(
                    reconcile_state,
                    entity=normalized_domain,
                    entity_id=canonical_target_id,
                )
                failed_fields = (
                    _failed_readback_fields(reconcile_state, canonical_changes, operation=normalized_action)
                    if target_matches
                    else sorted(canonical_changes)
                )
                return _error_envelope(
                    "store_apply_outcome_uncertain",
                    status="compensating",
                    summary={
                        "contract_id": contract.get("contract_id"),
                        "write_applied_unverified": True,
                        "transport_error_code": _error_code(result),
                        "readback_target_matches": bool(target_matches),
                        "failed_fields": failed_fields,
                    },
                    meta={
                        **result_meta,
                        "domain": normalized_domain,
                        "action": normalized_action,
                        "mode": normalized_mode,
                        "pre_state_read": True,
                        "pre_read_revision_mismatch": pre_read_revision_mismatch,
                        "post_state_read": post_state_read,
                        "readback_verified": False,
                    },
                )
            return {
                **result,
                "meta": {
                    **result_meta,
                    "contract_id": contract.get("contract_id"),
                    "domain": normalized_domain,
                    "action": normalized_action,
                    "mode": normalized_mode,
                    "pre_state_read": True,
                    "pre_read_revision_mismatch": pre_read_revision_mismatch,
                },
            }

        post_state: dict[str, Any] | None = None
        readback_verified = False
        if normalized_mode == "apply":
            post_state = self.entity_context(
                entity=normalized_domain,
                entity_id=canonical_target_id,
                detail="full" if normalized_action == "add_quote_request_note" else "summary",
            )
            if not post_state.get("ok"):
                return _error_envelope(
                    "store_apply_readback_failed",
                    status="compensating",
                    summary={
                        "contract_id": contract.get("contract_id"),
                        "write_applied_unverified": True,
                    },
                    meta={
                        "domain": normalized_domain,
                        "action": normalized_action,
                        "mode": normalized_mode,
                        "pre_state_read": True,
                        "pre_read_revision_mismatch": pre_read_revision_mismatch,
                        "post_state_read": False,
                        "readback_verified": False,
                    },
                )
            if not _entity_matches(post_state, entity=normalized_domain, entity_id=canonical_target_id):
                return _error_envelope(
                    "store_apply_readback_target_mismatch",
                    status="compensating",
                    summary={
                        "contract_id": contract.get("contract_id"),
                        "write_applied_unverified": True,
                    },
                    meta={
                        "domain": normalized_domain,
                        "action": normalized_action,
                        "mode": normalized_mode,
                        "pre_state_read": True,
                        "pre_read_revision_mismatch": pre_read_revision_mismatch,
                        "post_state_read": True,
                        "readback_verified": False,
                    },
                )
            failed_fields = _failed_readback_fields(post_state, canonical_changes, operation=normalized_action)
            if failed_fields:
                return _error_envelope(
                    "store_apply_readback_mismatch",
                    status="compensating",
                    summary={
                        "contract_id": contract.get("contract_id"),
                        "write_applied_unverified": True,
                        "failed_fields": failed_fields,
                    },
                    meta={
                        "domain": normalized_domain,
                        "action": normalized_action,
                        "mode": normalized_mode,
                        "pre_state_read": True,
                        "pre_read_revision_mismatch": pre_read_revision_mismatch,
                        "post_state_read": True,
                        "readback_verified": False,
                    },
                )
            post_updated_at = _entity_field(post_state, "updated_at")
            idempotency_replay = _dict(result.get("meta")).get("idempotency_replay") is True
            if not str(post_updated_at or "").strip() or (
                not idempotency_replay
                and not _version_advanced(
                    str(observed_updated_at),
                    str(post_updated_at),
                )
            ):
                return _error_envelope(
                    "store_apply_readback_version_not_advanced",
                    status="compensating",
                    summary={
                        "contract_id": contract.get("contract_id"),
                        "write_applied_unverified": True,
                    },
                    meta={
                        "domain": normalized_domain,
                        "action": normalized_action,
                        "mode": normalized_mode,
                        "pre_state_read": True,
                        "pre_read_revision_mismatch": pre_read_revision_mismatch,
                        "post_state_read": True,
                        "idempotency_replay": idempotency_replay,
                        "readback_verified": False,
                    },
                )
            readback_verified = True

        result_warnings = [str(item) for item in result.get("warnings", []) if str(item).strip()]
        if normalized_action == "mark_order_ready" and normalized_mode == "dry_run":
            result_warnings.append("store_order_ready_may_notify_customer")
        return {
            **result,
            "warnings": list(dict.fromkeys(result_warnings)),
            "meta": {
                **_dict(result.get("meta")),
                "contract_id": contract.get("contract_id"),
                "domain": normalized_domain,
                "action": normalized_action,
                "mode": normalized_mode,
                "pre_state_read": True,
                "pre_read_revision_mismatch": pre_read_revision_mismatch,
                "post_state_read": post_state is not None,
                "readback_verified": readback_verified,
            },
        }


def _digest_envelope(
    *,
    summary: dict[str, Any],
    page_items: list[dict[str, Any]],
    warnings: Any,
    page_limit: int,
    baseline: bool,
    has_more: bool,
    next_cursor: str,
    stream: str,
    checkpoint_advanced: bool,
    checkpoint_state_version: Any,
    snapshot_at: str,
    source_has_more: bool,
    delivery_ack_required: bool,
    delivery_ack_token: str | None = None,
    delivery_acknowledged: bool = False,
    acknowledged_cursor: str | None = None,
    replayed_delivery: bool = False,
    traversal_advanced: bool = False,
) -> dict[str, Any]:
    visible_items = [] if baseline else page_items
    public_summary = {
        key: value
        for key, value in summary.items()
        if key not in {"checkpoint_cursor", "next_cursor", "replay_cursor", "cursor"}
    }
    return {
        "ok": True,
        "format": STORE_AGENT_FORMAT,
        "status": ("baseline" if baseline and checkpoint_advanced else "baseline_pending" if baseline else "completed"),
        "summary": {
            **public_summary,
            "baseline_created": bool(baseline and checkpoint_advanced),
        },
        "items": visible_items,
        "page": {
            "limit": page_limit,
            "returned": len(visible_items),
            "has_more": has_more,
            "next_cursor": next_cursor if has_more else None,
            "source_has_more": source_has_more,
            "ack_required": delivery_ack_required,
            "ack_token": delivery_ack_token,
        },
        "warnings": list(dict.fromkeys(str(item) for item in warnings or [] if str(item).strip()))
        if isinstance(warnings, list)
        else [],
        "meta": {
            "source": "autostop_store_api",
            "stream": stream,
            "baseline": baseline,
            "checkpoint_advanced": checkpoint_advanced,
            "checkpoint_state_version": checkpoint_state_version,
            "last_success_at": snapshot_at if checkpoint_advanced else None,
            "delivery_ack_required": delivery_ack_required,
            "delivery_acknowledged": delivery_acknowledged,
            "acknowledged_cursor": acknowledged_cursor,
            "replayed_delivery": replayed_delivery,
            "traversal_advanced": traversal_advanced,
        },
    }


def _digest_ack_envelope(
    *,
    stream: str,
    cursor: str,
    checkpoint_state_version: int,
    snapshot_at: str,
    page_limit: int,
) -> dict[str, Any]:
    return {
        "ok": True,
        "format": STORE_AGENT_FORMAT,
        "status": "completed",
        "summary": {"delivery_acknowledged": True},
        "items": [],
        "page": {
            "limit": page_limit,
            "returned": 0,
            "has_more": False,
            "next_cursor": None,
            "source_has_more": False,
            "ack_required": False,
            "ack_token": None,
        },
        "warnings": [],
        "meta": {
            "source": "autostop_store_api",
            "stream": stream,
            "baseline": False,
            "checkpoint_advanced": True,
            "checkpoint_state_version": checkpoint_state_version,
            "last_success_at": snapshot_at,
            "delivery_ack_required": False,
            "delivery_acknowledged": True,
            "acknowledged_cursor": cursor,
            "replayed_delivery": False,
            "traversal_advanced": False,
        },
    }


def _store_delivery_token(
    *,
    stream: str,
    request_cursor: str | None,
    request_since: str | None,
    next_cursor: str,
    snapshot_at: str,
    baseline: bool,
    source_has_more: bool,
    page_limit: int,
    page_refs: list[dict[str, str]],
) -> str:
    payload = {
        "v": 1,
        "stream": stream,
        "request_cursor": request_cursor,
        "request_since": request_since,
        "next_cursor": next_cursor,
        "snapshot_at": snapshot_at,
        "baseline": baseline,
        "source_has_more": source_has_more,
        "page_limit": page_limit,
        "page_refs": page_refs,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _encode_delivery_cursor(*, stream: str, next_cursor: str, delivery_token: str) -> str:
    payload = {
        "v": 1,
        "kind": "store_delivery_ack",
        "stream": stream,
        "next_cursor": next_cursor,
        "delivery_token": delivery_token,
    }
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_delivery_cursor(value: str) -> dict[str, str] | None:
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode((value + padding).encode("ascii")))
        if (
            not isinstance(payload, dict)
            or payload.get("v") != 1
            or payload.get("kind") != "store_delivery_ack"
            or str(payload.get("stream") or "") not in STORE_DIGEST_STREAMS
            or not str(payload.get("next_cursor") or "")
            or re.fullmatch(r"[0-9a-f]{64}", str(payload.get("delivery_token") or "")) is None
        ):
            return None
        return {
            "stream": str(payload["stream"]),
            "next_cursor": str(payload["next_cursor"]),
            "delivery_token": str(payload["delivery_token"]),
        }
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None


def _merge_compact_refs(*values: Any) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for value in values:
        if not isinstance(value, list):
            continue
        for raw in value:
            if not isinstance(raw, dict):
                continue
            ref = _compact_ref(raw)
            if not ref:
                continue
            key = (ref["entity"], ref["id"])
            refs = [item for item in refs if (item["entity"], item["id"]) != key]
            refs.append(ref)
            if len(refs) > 500:
                refs = refs[-500:]
            while refs and len(json.dumps(refs, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 16_384:
                refs.pop(0)
    return refs


def _page_refs_without_versions(values: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{"entity": ref["entity"], "id": ref["id"]} for ref in values if ref.get("entity") and ref.get("id")]


def _page_ref_membership(value: Any) -> set[tuple[str, str]]:
    if not isinstance(value, list):
        return set()
    return {
        (str(ref.get("entity") or ""), str(ref.get("id") or ""))
        for ref in value
        if isinstance(ref, dict) and ref.get("entity") and ref.get("id")
    }


def _entity_payload(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload.get("items")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return dict(items[0])
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        return {}
    entity = summary.get("entity")
    return dict(entity) if isinstance(entity, dict) else dict(summary)


def _entity_field(payload: dict[str, Any], field: str) -> Any:
    entity = _entity_payload(payload)
    return entity.get(field)


def _entity_matches(payload: dict[str, Any], *, entity: str, entity_id: str) -> bool:
    item = _entity_payload(payload)
    observed_entity = str(item.get("entity") or item.get("entity_type") or "").strip().casefold()
    observed_id = str(item.get("id") or item.get("entity_id") or "").strip()
    return observed_entity == entity and observed_id == entity_id


def _version_advanced(before: str, after: str) -> bool:
    normalized_before = str(before or "").strip()
    normalized_after = str(after or "").strip()
    if not normalized_before or not normalized_after or normalized_before == normalized_after:
        return False
    try:
        before_timestamp = datetime.fromisoformat(normalized_before.replace("Z", "+00:00"))
        after_timestamp = datetime.fromisoformat(normalized_after.replace("Z", "+00:00"))
    except ValueError:
        return True
    try:
        return after_timestamp > before_timestamp
    except TypeError:
        return False


def _failed_readback_fields(
    payload: dict[str, Any],
    planned_changes: dict[str, Any],
    *,
    operation: str,
) -> list[str]:
    entity = _entity_payload(payload)
    if operation == "add_quote_request_note":
        expected_text = str(planned_changes.get("text") or "").strip()
        notes_value = entity.get("notes")
        notes: list[Any] = notes_value if isinstance(notes_value, list) else []
        return (
            []
            if any(
                isinstance(note, dict)
                and note.get("origin") == "AUTOSTOP_MANAGER"
                and str(note.get("text") or "").strip() == expected_text
                for note in notes
            )
            else ["text"]
        )
    failed: list[str] = []
    for field, expected in planned_changes.items():
        readback_field = "assigned_user_id" if operation == "assign_quote_request" and field == "assignee_id" else field
        if operation == "update_quote_request_comment" and field == "internal_comment":
            readback_field = "internal_comment_sha256"
            expected = _internal_comment_sha256(expected)
        if readback_field not in entity:
            failed.append(str(field))
            continue
        observed = entity[readback_field]
        if field == "status":
            matches = str(observed).strip().upper() == str(expected).strip().upper()
        elif isinstance(expected, str):
            matches = str(observed).strip() == expected.strip()
        else:
            matches = observed == expected
        if not matches:
            failed.append(str(field))
    return sorted(set(failed))


def _internal_comment_sha256(value: Any) -> str:
    normalized = str(value).strip() if value is not None else ""
    canonical = "none:" if not normalized else f"comment:{normalized}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _item_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("items")
    if not isinstance(raw, list):
        raw = payload.get("changes")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)][:MAX_STORE_LIMIT]


def _compact_ref(item: dict[str, Any]) -> dict[str, str] | None:
    entity = str(item.get("entity") or item.get("type") or "").strip().casefold()
    entity_id = str(item.get("id") or item.get("entity_id") or "").strip()
    if not entity.startswith("store_") or not entity_id:
        return None
    ref = {"entity": entity, "id": entity_id}
    version = str(item.get("version") or item.get("updated_at") or "").strip()
    if version:
        ref["version"] = version
    updated_at = str(item.get("updated_at") or "").strip()
    if updated_at:
        ref["updated_at"] = updated_at
    return ref


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _error_code(payload: dict[str, Any]) -> str:
    summary = _dict(payload.get("summary"))
    return str(summary.get("error_code") or "store_read_failed")


def _validation_error_code(exc: ValueError) -> str:
    text = str(exc).casefold()
    if "entity" in text:
        return "invalid_store_entity"
    if "cursor" in text:
        return "invalid_store_cursor"
    if "detail" in text:
        return "invalid_store_detail"
    if "operation" in text:
        return "invalid_store_operation"
    if "mode" in text:
        return "invalid_store_mode"
    return "invalid_store_request"


def _digest_ack_error(stream: str) -> dict[str, Any]:
    return _error_envelope(
        "store_digest_ack_stale_or_foreign",
        status="conflict",
        meta={"stream": stream, "checkpoint_advanced": False},
    )


def _error_envelope(
    error_code: str,
    *,
    status: str = "degraded",
    summary: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "format": STORE_AGENT_FORMAT,
        "status": status,
        "summary": {"error_code": str(error_code), **(summary or {})},
        "items": [],
        "page": {},
        "warnings": [str(error_code)],
        "meta": {"source": "autostop_store_adapter", **(meta or {})},
    }
