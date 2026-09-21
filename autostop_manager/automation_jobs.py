"""Production adapters for allowlisted Manager automation jobs.

Business records remain in CRM and Telegram.  This module keeps only opaque
checkpoint/delivery references in Manager SQLite and never logs payloads.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, Protocol
from urllib.parse import quote, unquote

import httpx

from .automation_registry import AutomationError, AutomationStore, canonical_json
from .config import AutomationCrmConnectionConfig, get_work_telegram_socket_path
from .telegram_bridge import BridgeError, send_local_request


CRM_DIGEST_CONSUMER_ID = "manager.crm_digest_v1"
CRM_DIGEST_PAGE_LIMIT = 25
MAX_CRM_RESPONSE_BYTES = 256 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DIGEST_ID = re.compile(r"digest-[0-9a-f]{32}\Z")
_ACTION = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_TECHNICAL_REF = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_CARD_PATH = re.compile(r"/\?card_id=([A-Za-z0-9._~%-]{1,384})\Z")
_CATEGORIES = frozenset({"movement", "repair_order", "finance", "inventory", "other"})


class AutomationJobError(RuntimeError):
    def __init__(self, code: str, *, outcome_uncertain: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.outcome_uncertain = outcome_uncertain


class CrmDigestSource(Protocol):
    def register(self) -> dict[str, Any]: ...

    def read(self, *, cursor: str | None = None) -> dict[str, Any]: ...

    def summarize(self, *, ack: str) -> dict[str, Any]: ...

    def acknowledge(self, *, ack: str) -> dict[str, Any]: ...


class OwnerNotifier(Protocol):
    def status(self) -> dict[str, Any]: ...

    def preview(self, text: str) -> dict[str, Any]: ...

    def apply(self, text: str, *, contract_token: str, idempotency_key: str) -> dict[str, Any]: ...

    def readback(self, *, message_id: int, text_sha256: str) -> dict[str, Any]: ...

    def lookup(self, *, idempotency_key: str, text_sha256: str) -> dict[str, Any]: ...


class HttpCrmDigestSource:
    """Narrow direct HTTP client for five durable CRM change-feed routes."""

    _FORMATS: ClassVar[dict[str, str]] = {
        "register": "crm_change_feed_registration_v1",
        "read": "crm_change_feed_page_v1",
        "summarize": "crm_change_digest_v1",
        "ack": "crm_change_feed_ack_v1",
    }

    def __init__(self, config: AutomationCrmConnectionConfig, *, timeout_seconds: float = 10) -> None:
        if not config.configured or config.error_code or not config.api_url or not config.bearer_token:
            raise AutomationJobError(config.error_code or "automation_crm_not_configured")
        self.config = config
        self.timeout_seconds = timeout_seconds

    def _post(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        path = {
            "register": "/api/change_feed/register",
            "read": "/api/change_feed/read",
            "summarize": "/api/change_feed/summarize",
            "ack": "/api/change_feed/ack",
        }[operation]
        try:
            with httpx.Client(
                base_url=self.config.api_url,
                headers={
                    "Authorization": f"Bearer {self.config.bearer_token}",
                    "X-Autostop-Automation-Protocol": "crm_digest_v1",
                },
                follow_redirects=False,
                trust_env=False,
                timeout=self.timeout_seconds,
            ) as client:
                with client.stream("POST", path, json=dict(payload)) as response:
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > MAX_CRM_RESPONSE_BYTES:
                            raise AutomationJobError("automation_crm_response_too_large")
                    status_code = response.status_code
        except AutomationJobError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise AutomationJobError(
                "automation_crm_transport_failed",
                outcome_uncertain=operation in {"register", "ack"},
            ) from exc
        try:
            envelope = json.loads(content)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AutomationJobError("automation_crm_response_invalid") from exc
        if status_code < 200 or status_code >= 300 or not isinstance(envelope, Mapping):
            raise AutomationJobError("automation_crm_request_failed")
        data = envelope.get("data")
        if envelope.get("ok") is not True or not isinstance(data, Mapping):
            raise AutomationJobError("automation_crm_request_failed")
        result = dict(data)
        if result.get("format") != self._FORMATS[operation]:
            raise AutomationJobError("automation_crm_contract_invalid")
        return result

    def register(self) -> dict[str, Any]:
        return self._post("register", {"consumer_id": CRM_DIGEST_CONSUMER_ID, "start_at": "latest"})

    def read(self, *, cursor: str | None = None) -> dict[str, Any]:
        return self._post(
            "read",
            {"consumer_id": CRM_DIGEST_CONSUMER_ID, "cursor": cursor, "limit": CRM_DIGEST_PAGE_LIMIT},
        )

    def summarize(self, *, ack: str) -> dict[str, Any]:
        return self._post("summarize", {"consumer_id": CRM_DIGEST_CONSUMER_ID, "ack": ack})

    def acknowledge(self, *, ack: str) -> dict[str, Any]:
        return self._post("ack", {"consumer_id": CRM_DIGEST_CONSUMER_ID, "ack": ack})


class TelegramOwnerNotifier:
    """Use the work bridge's fixed owner target; callers cannot supply a peer."""

    def __init__(self, socket_path: Path | None = None, *, timeout_seconds: float = 15) -> None:
        self.socket_path = socket_path or get_work_telegram_socket_path()
        self.timeout_seconds = timeout_seconds

    def _request(
        self,
        payload: dict[str, Any],
        *,
        apply_started: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        try:
            response = send_local_request(
                self.socket_path,
                payload,
                timeout_seconds=self.timeout_seconds if timeout_seconds is None else timeout_seconds,
            )
        except (BridgeError, OSError, TimeoutError) as exc:
            raise AutomationJobError(
                "automation_telegram_outcome_unknown" if apply_started else "automation_telegram_unavailable",
                outcome_uncertain=apply_started,
            ) from exc
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise AutomationJobError(
                "automation_telegram_outcome_unknown" if apply_started else "automation_telegram_rejected",
                outcome_uncertain=apply_started,
            )
        return response

    def status(self) -> dict[str, Any]:
        response = self._request(
            {"operation": "status"},
            timeout_seconds=min(self.timeout_seconds, 2.0),
        )
        # The bridge's full status contains account metadata.  Scheduler
        # readiness needs only these two booleans and must never expose a peer.
        return {
            "transport_ready": response.get("transport_ready") is True,
            "owner_notification_configured": response.get("owner_notification_configured") is True,
        }

    def preview(self, text: str) -> dict[str, Any]:
        return self._request({"operation": "send_owner_notification", "text": text, "mode": "dry_run"})

    def apply(self, text: str, *, contract_token: str, idempotency_key: str) -> dict[str, Any]:
        return self._request(
            {
                "operation": "send_owner_notification",
                "text": text,
                "mode": "apply",
                "contract_token": contract_token,
                "idempotency_key": idempotency_key,
            },
            apply_started=True,
        )

    def readback(self, *, message_id: int, text_sha256: str) -> dict[str, Any]:
        return self._request(
            {
                "operation": "owner_notification_readback",
                "message_id": message_id,
                "expected_text_sha256": text_sha256,
            },
            apply_started=True,
        )

    def lookup(self, *, idempotency_key: str, text_sha256: str) -> dict[str, Any]:
        return self._request(
            {
                "operation": "owner_notification_idempotency_readback",
                "idempotency_key": idempotency_key,
                "expected_text_sha256": text_sha256,
            },
            apply_started=True,
        )


def _validated_card_path(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AutomationJobError("automation_crm_digest_invalid")
    match = _CARD_PATH.fullmatch(value)
    if match is None:
        raise AutomationJobError("automation_crm_digest_invalid")
    try:
        decoded = unquote(match.group(1), errors="strict")
    except (UnicodeError, ValueError) as exc:
        raise AutomationJobError("automation_crm_digest_invalid") from exc
    if (
        not 1 <= len(decoded.encode()) <= 128
        or any(char.isspace() or ord(char) < 33 for char in decoded)
        or any(char in "/?&#" for char in decoded)
        or quote(decoded, safe="") != match.group(1)
    ):
        raise AutomationJobError("automation_crm_digest_invalid")
    return value


def _validate_digest_items(payload: Mapping[str, Any], *, total_events: int) -> list[dict[str, Any]]:
    items = payload.get("items")
    omitted = payload.get("omitted_groups")
    if not isinstance(items, list) or len(items) > 12 or type(omitted) is not int or omitted < 0:
        raise AutomationJobError("automation_crm_digest_invalid")
    if len(items) + omitted != total_events:
        raise AutomationJobError("automation_crm_digest_invalid")
    validated = []
    allowed_keys = {"category", "count", "actions", "crm_path", "direction", "movement", "from_ref", "to_ref"}
    for item in items:
        if not isinstance(item, Mapping) or set(item).difference(allowed_keys):
            raise AutomationJobError("automation_crm_digest_invalid")
        category = item.get("category")
        count = item.get("count")
        actions = item.get("actions")
        if (
            category not in _CATEGORIES
            or type(count) is not int
            or not 1 <= count <= 100_000
            or not isinstance(actions, list)
            or not 1 <= len(actions) <= 4
            or len(set(actions)) != len(actions)
            or any(not isinstance(action, str) or _ACTION.fullmatch(action) is None for action in actions)
        ):
            raise AutomationJobError("automation_crm_digest_invalid")
        for key in ("direction", "movement", "from_ref", "to_ref"):
            value = item.get(key)
            if value is not None and (not isinstance(value, str) or _TECHNICAL_REF.fullmatch(value) is None):
                raise AutomationJobError("automation_crm_digest_invalid")
        copied = dict(item)
        copied["crm_path"] = _validated_card_path(item.get("crm_path"))
        validated.append(copied)
    return validated


def validate_digest(payload: Mapping[str, Any]) -> dict[str, Any]:
    digest_id = str(payload.get("digest_id") or "")
    content_hash = str(payload.get("content_hash") or "")
    if (
        payload.get("format") != "crm_change_digest_v1"
        or payload.get("consumer_id") != CRM_DIGEST_CONSUMER_ID
        or _DIGEST_ID.fullmatch(digest_id) is None
        or _SHA256.fullmatch(content_hash) is None
    ):
        raise AutomationJobError("automation_crm_digest_invalid")
    snapshot_keys = (
        "format",
        "digest_id",
        "generation",
        "consumer_id",
        "from_sequence",
        "through_sequence",
        "delivery_high_water",
        "total_events",
        "raw_event_count",
        "category_counts",
        "financial_totals",
        "items",
        "omitted_groups",
        "created_at",
    )
    snapshot = {key: payload.get(key) for key in snapshot_keys}
    calculated = hashlib.sha256(canonical_json(snapshot).encode()).hexdigest()
    total_events = payload.get("total_events")
    raw_event_count = payload.get("raw_event_count")
    through_sequence = payload.get("through_sequence")
    if (
        calculated != content_hash
        or type(total_events) is not int
        or not 1 <= total_events <= 100_000
        or type(raw_event_count) is not int
        or not total_events <= raw_event_count <= 1_000_000
        or type(through_sequence) is not int
        or through_sequence < 1
    ):
        raise AutomationJobError("automation_crm_digest_invalid")
    counts = payload.get("category_counts")
    totals = payload.get("financial_totals")
    if not isinstance(counts, Mapping) or set(counts) != _CATEGORIES:
        raise AutomationJobError("automation_crm_digest_invalid")
    if any(type(value) is not int or value < 0 for value in counts.values()) or sum(counts.values()) != total_events:
        raise AutomationJobError("automation_crm_digest_invalid")
    expected_total_keys = {"income_minor", "expense_minor", "refund_minor", "cancel_minor", "unknown_amount_events"}
    if not isinstance(totals, Mapping) or set(totals) != expected_total_keys:
        raise AutomationJobError("automation_crm_digest_invalid")
    if any(type(value) is not int for value in totals.values()) or int(totals["unknown_amount_events"]) < 0:
        raise AutomationJobError("automation_crm_digest_invalid")
    validated = dict(payload)
    validated["items"] = _validate_digest_items(payload, total_events=total_events)
    return validated


def _money(minor: int) -> str:
    sign = "−" if minor < 0 else ""
    value = abs(minor)
    return f"{sign}{value // 100:,}".replace(",", " ") + f",{value % 100:02d} ₽"


def render_crm_digest(payload: Mapping[str, Any]) -> str:
    digest = validate_digest(payload)
    total = int(digest["total_events"])
    counts = digest.get("category_counts")
    totals = digest.get("financial_totals")
    if not isinstance(counts, Mapping) or not isinstance(totals, Mapping):
        raise AutomationJobError("automation_crm_digest_invalid")
    labels = (
        ("movement", "перемещения"),
        ("repair_order", "заказ-наряды"),
        ("finance", "финансы"),
        ("inventory", "склад"),
        ("other", "прочее"),
    )
    count_parts = []
    for key, label in labels:
        value = counts.get(key, 0)
        if type(value) is not int or value < 0:
            raise AutomationJobError("automation_crm_digest_invalid")
        if value:
            count_parts.append(f"{label}: {value}")
    lines = [f"CRM: {total} изм."]
    if count_parts:
        lines.append(" · ".join(count_parts))
    money_labels = (
        ("income_minor", "приход"),
        ("expense_minor", "расход"),
        ("refund_minor", "возврат"),
        ("cancel_minor", "отмена"),
    )
    money_parts = []
    for key, label in money_labels:
        value = totals.get(key, 0)
        if type(value) is not int:
            raise AutomationJobError("automation_crm_digest_invalid")
        if value:
            money_parts.append(f"{label}: {_money(value)}")
    unknown = totals.get("unknown_amount_events", 0)
    if type(unknown) is not int or unknown < 0:
        raise AutomationJobError("automation_crm_digest_invalid")
    if money_parts:
        lines.append("Деньги — " + " · ".join(money_parts))
    if unknown:
        lines.append(f"Сумма не указана: {unknown}")
    omitted = digest["omitted_groups"]
    if omitted:
        lines.append(f"Ещё групп изменений: {omitted}")
    links = []
    for item in digest["items"]:
        path = item.get("crm_path")
        if isinstance(path, str) and path not in links:
            links.append(path)
        if len(links) == 3:
            break
    if links:
        lines.append("Карточки: " + " · ".join(f"https://crm.autostopcrm.ru{path}" for path in links))
    rendered = "\n".join(lines)
    if len(rendered) > 1500:
        raise AutomationJobError("automation_crm_digest_render_too_large")
    return rendered


class CrmDigestExecutor:
    """One bounded, restart-safe CRM digest run."""

    _CURSOR_NAMES = (
        "delivery_state",
        "pending_ack",
        "pending_digest_id",
        "pending_content_hash",
        "pending_through_sequence",
        "notification_message_id",
        "notification_text_hash",
    )

    def __init__(
        self,
        *,
        store: AutomationStore,
        source: CrmDigestSource,
        notifier: OwnerNotifier,
    ) -> None:
        self.store = store
        self.source = source
        self.notifier = notifier

    @staticmethod
    def _claim_fields(claim: Mapping[str, Any]) -> tuple[str, str, str, int]:
        job_id = str(claim.get("job_id") or "")
        run_id = str(claim.get("run_id") or "")
        owner = str(claim.get("lease_owner") or "")
        fencing_token = claim.get("fencing_token")
        if not job_id or not run_id or not owner or type(fencing_token) is not int:
            raise AutomationJobError("automation_claim_invalid")
        return job_id, run_id, owner, fencing_token

    def _heartbeat(self, claim: Mapping[str, Any]) -> None:
        job_id, run_id, owner, fencing_token = self._claim_fields(claim)
        if not self.store.heartbeat_run(
            job_id=job_id,
            run_id=run_id,
            owner=owner,
            fencing_token=fencing_token,
        ):
            raise AutomationJobError("automation_fencing_conflict")

    def _require_current_claim(self, claim: Mapping[str, Any]) -> None:
        job_id, run_id, owner, fencing_token = self._claim_fields(claim)
        claimed_revision = claim.get("claimed_revision")
        if type(claimed_revision) is not int or not self.store.run_claim_is_current(
            job_id=job_id,
            run_id=run_id,
            owner=owner,
            fencing_token=fencing_token,
            claimed_revision=claimed_revision,
        ):
            raise AutomationJobError("automation_claim_superseded")

    def _cursor(self, job_id: str, name: str) -> tuple[str | None, int]:
        item = self.store.get_cursors(job_id=job_id).get(name)
        return (item.get("value"), int(item["revision"])) if item else (None, 0)

    def _set_cursor(self, claim: Mapping[str, Any], name: str, value: str | None) -> None:
        job_id, _run_id, owner, fencing_token = self._claim_fields(claim)
        _old_value, revision = self._cursor(job_id, name)
        self.store.update_cursor(
            job_id=job_id,
            cursor_name=name,
            cursor_value=value,
            expected_revision=revision,
            owner=owner,
            fencing_token=fencing_token,
        )

    def _clear_pending(self, claim: Mapping[str, Any]) -> None:
        for name in self._CURSOR_NAMES:
            self._set_cursor(claim, name, None)

    def _resume_pending(self, claim: Mapping[str, Any], registration: Mapping[str, Any]) -> None:
        job_id = str(claim["job_id"])
        cursors = self.store.get_cursors(job_id=job_id)
        value = lambda name: (cursors.get(name) or {}).get("value")  # noqa: E731
        state = str(value("delivery_state") or "")
        if not state:
            return
        pending_ack = str(value("pending_ack") or "")
        raw_through = str(value("pending_through_sequence") or "")
        try:
            through = int(raw_through)
        except ValueError as exc:
            raise AutomationJobError("automation_pending_delivery_invalid") from exc
        acked_sequence = registration.get("acked_sequence")
        if type(acked_sequence) is int and acked_sequence >= through:
            self._clear_pending(claim)
            return
        if state == "prepared":
            self._clear_pending(claim)
            return
        recovered_message_id: int | None = None
        if state == "sending":
            digest_id = str(value("pending_digest_id") or "")
            text_hash = str(value("notification_text_hash") or "")
            if _DIGEST_ID.fullmatch(digest_id) is None or _SHA256.fullmatch(text_hash) is None:
                raise AutomationJobError("automation_pending_delivery_invalid")
            try:
                lookup = self.notifier.lookup(
                    idempotency_key=f"crm-digest:{digest_id}",
                    text_sha256=text_hash,
                )
            except AutomationJobError as exc:
                raise AutomationJobError(
                    "automation_delivery_manual_reconciliation_required",
                    outcome_uncertain=True,
                ) from exc
            if (
                lookup.get("outcome") != "verified"
                or lookup.get("found") is not True
                or lookup.get("verified") is not True
                or lookup.get("text_sha256") != text_hash
                or type(lookup.get("message_id")) is not int
                or int(lookup["message_id"]) <= 0
            ):
                # not_found is not proof of non-delivery: the bridge may have
                # crashed after Telegram accepted the send but before saving
                # its idempotency record. Never resend or ACK here.
                raise AutomationJobError(
                    "automation_delivery_manual_reconciliation_required",
                    outcome_uncertain=True,
                )
            self._set_cursor(claim, "notification_message_id", str(lookup["message_id"]))
            self._set_cursor(claim, "delivery_state", "sent_unverified")
            recovered_message_id = int(lookup["message_id"])
        message_id_raw = str(recovered_message_id or value("notification_message_id") or "")
        text_hash = str(value("notification_text_hash") or "")
        try:
            message_id = int(message_id_raw)
        except ValueError as exc:
            raise AutomationJobError("automation_pending_delivery_invalid") from exc
        if message_id <= 0 or _SHA256.fullmatch(text_hash) is None or not pending_ack:
            raise AutomationJobError("automation_pending_delivery_invalid")
        readback = self.notifier.readback(message_id=message_id, text_sha256=text_hash)
        if readback.get("verified") is not True or int(readback.get("message_id") or 0) != message_id:
            raise AutomationJobError("automation_delivery_readback_failed", outcome_uncertain=True)
        self._set_cursor(claim, "delivery_state", "delivered_unacked")
        acknowledged = self.source.acknowledge(ack=pending_ack)
        if type(acknowledged.get("acked_sequence")) is not int or int(acknowledged["acked_sequence"]) < through:
            raise AutomationJobError("automation_crm_ack_invalid", outcome_uncertain=True)
        self._clear_pending(claim)

    def _execute(self, claim: Mapping[str, Any]) -> dict[str, Any]:
        self._claim_fields(claim)
        self._heartbeat(claim)
        registration = self.source.register()
        if registration.get("consumer_id") != CRM_DIGEST_CONSUMER_ID:
            raise AutomationJobError("automation_crm_registration_invalid")
        self._resume_pending(claim, registration)
        self._heartbeat(claim)
        page = self.source.read(cursor=None)
        if page.get("consumer_id") != CRM_DIGEST_CONSUMER_ID or not isinstance(page.get("events"), list):
            raise AutomationJobError("automation_crm_page_invalid")
        if not page["events"]:
            return {"ok": True, "result_code": "no_changes", "pages": 0, "events": 0}
        page_ack = page.get("ack")
        if not isinstance(page_ack, str) or not page_ack or len(page_ack) > 4096:
            raise AutomationJobError("automation_crm_page_invalid")
        digest = validate_digest(self.source.summarize(ack=page_ack))
        ack = digest.get("ack")
        if not isinstance(ack, str) or not ack or len(ack) > 4096:
            raise AutomationJobError("automation_crm_digest_invalid")
        text = render_crm_digest(digest)
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        digest_id = str(digest["digest_id"])
        through = int(digest["through_sequence"])
        idempotency_key = f"crm-digest:{digest_id}"
        for name, value in (
            ("pending_ack", ack),
            ("pending_digest_id", digest_id),
            ("pending_content_hash", str(digest["content_hash"])),
            ("pending_through_sequence", str(through)),
            ("notification_text_hash", text_hash),
            ("notification_message_id", None),
            ("delivery_state", "prepared"),
        ):
            self._set_cursor(claim, name, value)
        self._heartbeat(claim)
        self._require_current_claim(claim)
        preview = self.notifier.preview(text)
        contract_token = preview.get("contract_token")
        if (
            preview.get("mode") != "dry_run"
            or preview.get("text_sha256") != text_hash
            or not isinstance(contract_token, str)
            or not contract_token
        ):
            raise AutomationJobError("automation_notification_preview_invalid")
        self._heartbeat(claim)
        self._require_current_claim(claim)
        self._set_cursor(claim, "delivery_state", "sending")
        applied = self.notifier.apply(
            text,
            contract_token=contract_token,
            idempotency_key=idempotency_key,
        )
        message_id = applied.get("message_id")
        if applied.get("verified") is not True or type(message_id) is not int or message_id <= 0:
            raise AutomationJobError("automation_telegram_outcome_unknown", outcome_uncertain=True)
        self._heartbeat(claim)
        self._set_cursor(claim, "notification_message_id", str(message_id))
        self._set_cursor(claim, "delivery_state", "sent_unverified")
        readback = self.notifier.readback(message_id=message_id, text_sha256=text_hash)
        if readback.get("verified") is not True or readback.get("message_id") != message_id:
            raise AutomationJobError("automation_delivery_readback_failed", outcome_uncertain=True)
        self._heartbeat(claim)
        self._set_cursor(claim, "delivery_state", "delivered_unacked")
        acknowledged = self.source.acknowledge(ack=ack)
        if type(acknowledged.get("acked_sequence")) is not int or int(acknowledged["acked_sequence"]) < through:
            raise AutomationJobError("automation_crm_ack_invalid", outcome_uncertain=True)
        self._clear_pending(claim)
        return {
            "ok": True,
            "result_code": "ok",
            "pages": 1,
            "events": int(digest["total_events"]),
        }

    def __call__(self, claim: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return self._execute(claim)
        except (AutomationJobError, AutomationError) as exc:
            code = exc.code if isinstance(exc, (AutomationJobError, AutomationError)) else "automation_job_failed"
            return {
                "ok": False,
                "result_code": code,
                "outcome_uncertain": bool(getattr(exc, "outcome_uncertain", False)),
            }
