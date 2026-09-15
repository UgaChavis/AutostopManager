from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import socket
import stat
import sys
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_CREDENTIALS_PATH = Path("/etc/autostop-telegram/credentials")
DEFAULT_SESSION_PATH = Path("/var/lib/autostop-telegram/account")
DEFAULT_STATE_DIR = Path("/var/lib/autostop-telegram")
DEFAULT_SOCKET_PATH = Path("/run/autostop-telegram/bridge.sock")
WORK_CREDENTIALS_PATH = Path("/etc/autostop-work-telegram/credentials")
WORK_SESSION_PATH = Path("/var/lib/autostop-work-telegram/account")
WORK_STATE_DIR = Path("/var/lib/autostop-work-telegram")
WORK_SOCKET_PATH = Path("/run/autostop-work-telegram/bridge.sock")
WORK_TRANSCRIPTION_MODEL_DIR = Path("/opt/autostop-work-telegram-models/faster-whisper-small")
MAX_REQUEST_BYTES = 128 * 1024
MAX_MESSAGE_CHARS = 4096
MAX_CAPTION_CHARS = 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024
CONTRACT_TTL_SECONDS = 15 * 60
TRANSCRIPTION_MODEL_NAME = "faster-whisper-small"
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
MAX_VIDEO_DURATION_SECONDS = 2 * 60
PHONE_RESOLVE_INTERVAL_SECONDS = 3.0
MAX_INBOUND_MONITOR_EVENTS = 32
MAX_INBOUND_MONITOR_CONTEXT_MESSAGES = 5
INBOUND_MONITOR_CONTEXT_WINDOW_SECONDS = 5 * 60
MAX_INBOUND_MONITOR_FALLBACK_DIALOGS = 20
MONITOR_VOICE_STAGE_TIMEOUT_SECONDS = 120
MONITOR_VOICE_STAGE_TTL_SECONDS = 10 * 60
MONITOR_VOICE_STAGE_CLEANUP_RETRIES = 3
MONITOR_VOICE_STAGE_CLEANUP_RETRY_SECONDS = 15
DEFAULT_LOCAL_REQUEST_TIMEOUT_SECONDS = 30.0
MAX_LOCAL_REQUEST_TIMEOUT_SECONDS = 180.0
WORK_MONITOR_ENVIRONMENT = "AUTOSTOP_WORK_TELEGRAM_MONITOR_INCOMING"
WORK_WAKE_ENVIRONMENT = "AUTOSTOP_WORK_TELEGRAM_WAKE_SOCKET"
INBOUND_EVENT_OPEN = "open"
INBOUND_EVENT_REPLY_VERIFIED = "reply_verified"
INBOUND_EVENT_NO_REPLY_NEEDED = "no_reply_needed"
INBOUND_EVENT_ID_PATTERN = re.compile(r"inbound-([1-9][0-9]{0,38})")
DOWNLOAD_MIME_SUFFIXES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "text/csv": ".csv",
    "text/plain": ".txt",
    "video/mp4": ".mp4",
}
SENSITIVE_URI_PATTERN = re.compile(r"(?i)\b(?:tg|vpn)://[^\s]+")
_MUTATION_LOCK = asyncio.Lock()
_MONITOR_VOICE_LOCK = asyncio.Lock()
_MONITOR_VOICE_EXPIRY_TASKS: set[asyncio.Task[None]] = set()
_PHONE_RESOLVE_LAST_AT = 0.0


class BridgeError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TelegramAccountPaths:
    credentials_path: Path
    session_path: Path
    state_dir: Path
    socket_path: Path


ACCOUNT_PATHS = {
    "personal": TelegramAccountPaths(
        credentials_path=DEFAULT_CREDENTIALS_PATH,
        session_path=DEFAULT_SESSION_PATH,
        state_dir=DEFAULT_STATE_DIR,
        socket_path=DEFAULT_SOCKET_PATH,
    ),
    "work": TelegramAccountPaths(
        credentials_path=WORK_CREDENTIALS_PATH,
        session_path=WORK_SESSION_PATH,
        state_dir=WORK_STATE_DIR,
        socket_path=WORK_SOCKET_PATH,
    ),
}


def _account_paths(account: str) -> TelegramAccountPaths:
    paths = ACCOUNT_PATHS.get(account)
    if paths is None:
        raise BridgeError("account_invalid")
    return paths


def account_inbox_dir(account: str) -> Path:
    """Return the only private inbox accepted for a named account."""

    return _account_paths(account).socket_path.parent / "inbox"


def account_outbox_dir(account: str) -> Path:
    """Return the only private outbox accepted for a named account."""

    return _account_paths(account).socket_path.parent / "outbox"


def account_model_dir(account: str) -> Path:
    """Return the account-owned local speech model path."""

    paths = _account_paths(account)
    if account == "work":
        return WORK_TRANSCRIPTION_MODEL_DIR
    return paths.state_dir / "models" / TRANSCRIPTION_MODEL_NAME


DEFAULT_OUTBOX_DIR = account_outbox_dir("personal")


@dataclass(frozen=True)
class TelegramConfig:
    api_id: int
    api_hash: str
    session_path: Path = DEFAULT_SESSION_PATH
    state_dir: Path = DEFAULT_STATE_DIR
    socket_path: Path = DEFAULT_SOCKET_PATH

    @classmethod
    def load(
        cls,
        credentials_path: Path = DEFAULT_CREDENTIALS_PATH,
        *,
        session_path: Path = DEFAULT_SESSION_PATH,
        state_dir: Path = DEFAULT_STATE_DIR,
        socket_path: Path = DEFAULT_SOCKET_PATH,
    ) -> TelegramConfig:
        runtime_paths = (credentials_path, session_path, state_dir, socket_path)
        if any(not path.is_absolute() for path in runtime_paths):
            raise BridgeError("runtime_path_not_absolute")
        if session_path.parent != state_dir:
            raise BridgeError("session_state_mismatch")
        try:
            credentials_fd = os.open(credentials_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except OSError as exc:
            raise BridgeError("credentials_unavailable") from exc
        try:
            file_stat = os.fstat(credentials_fd)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != os.geteuid():
                raise BridgeError("credentials_invalid")
            if stat.S_IMODE(file_stat.st_mode) & 0o077:
                raise BridgeError("credentials_permissions_too_open")
            raw_credentials = os.read(credentials_fd, 16 * 1024 + 1)
            if len(raw_credentials) > 16 * 1024:
                raise BridgeError("credentials_invalid")
            lines = raw_credentials.decode("ascii").splitlines()
        except (OSError, UnicodeError) as exc:
            raise BridgeError("credentials_unreadable") from exc
        finally:
            os.close(credentials_fd)

        values: dict[str, str] = {}
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()

        api_id_raw = values.get("TELEGRAM_API_ID", "")
        api_hash = values.get("TELEGRAM_API_HASH", "")
        if not api_id_raw.isdigit() or not (6 <= len(api_id_raw) <= 12):
            raise BridgeError("api_id_invalid")
        if len(api_hash) != 32 or any(char not in "0123456789abcdefABCDEF" for char in api_hash):
            raise BridgeError("api_hash_invalid")
        return cls(
            api_id=int(api_id_raw),
            api_hash=api_hash,
            session_path=session_path,
            state_dir=state_dir,
            socket_path=socket_path,
        )


@dataclass(frozen=True)
class InboundMonitorEvent:
    """One private incoming message reference kept only in daemon memory."""

    sequence: int
    peer_id: int
    message_id: int
    received_at: str


@dataclass(frozen=True)
class InboundVoiceStage:
    """One transient, opaque work-inbox file awaiting local transcription."""

    event_sequence: int
    path: Path
    duration_seconds: int
    suffix: str


class InboundMonitor:
    """Bounded, content-free monitor for explicitly enabled work Telegram."""

    def __init__(self, *, max_events: int = MAX_INBOUND_MONITOR_EVENTS) -> None:
        if max_events < 1:
            raise ValueError("inbound_monitor_limit_invalid")
        self.started_at = datetime.now(UTC).isoformat()
        self._max_events = max_events
        self._events: deque[InboundMonitorEvent] = deque()
        self._seen: set[tuple[int, int]] = set()
        self._states: dict[int, str] = {}
        self._voice_stages: dict[str, InboundVoiceStage] = {}
        self._voice_stage_by_event: dict[int, str] = {}
        self._voice_stage_reservations: set[int] = set()
        # A fresh in-memory epoch must never rebind a caller's old event ref.
        self._next_sequence = secrets.randbits(128) or 1
        self._dropped_events = 0
        self._dropped_open_events = 0

    @staticmethod
    def _event_id(event: InboundMonitorEvent) -> str:
        return f"inbound-{event.sequence}"

    def _state(self, event: InboundMonitorEvent) -> str:
        return self._states.get(event.sequence, INBOUND_EVENT_OPEN)

    def _evict_for_new_event(self) -> None:
        """Keep unresolved opaque refs ahead of already closed refs when bounded."""

        removed = next((event for event in self._events if self._state(event) != INBOUND_EVENT_OPEN), self._events[0])
        if self._state(removed) == INBOUND_EVENT_OPEN:
            self._dropped_open_events += 1
        self._events.remove(removed)
        self._seen.discard((removed.peer_id, removed.message_id))
        self._states.pop(removed.sequence, None)
        self._voice_stage_reservations.discard(removed.sequence)
        self._voice_stage_by_event.pop(removed.sequence, None)
        self._dropped_events += 1

    def record(self, event: Any) -> str | None:
        """Remember only an incoming private message locator; never its body or media."""

        if not bool(getattr(event, "is_private", False)) or bool(getattr(event, "out", False)):
            return None
        try:
            peer_id = int(getattr(event, "chat_id", 0) or 0)
            message_id = int(getattr(event, "id", 0) or 0)
        except (TypeError, ValueError):
            return None
        if peer_id <= 0 or message_id <= 0 or (peer_id, message_id) in self._seen:
            return None
        if len(self._events) == self._max_events:
            self._evict_for_new_event()
        event_ref = InboundMonitorEvent(
            sequence=self._next_sequence,
            peer_id=peer_id,
            message_id=message_id,
            received_at=datetime.now(UTC).isoformat(),
        )
        self._events.append(event_ref)
        self._seen.add((peer_id, message_id))
        self._states[event_ref.sequence] = INBOUND_EVENT_OPEN
        self._next_sequence += 1
        return self._event_id(event_ref)

    def status(self) -> dict[str, Any]:
        state_counts = {
            state: sum(1 for event in self._events if self._state(event) == state)
            for state in (INBOUND_EVENT_OPEN, INBOUND_EVENT_REPLY_VERIFIED, INBOUND_EVENT_NO_REPLY_NEEDED)
        }
        return {
            "enabled": True,
            "started_at": self.started_at,
            "pending_events": state_counts[INBOUND_EVENT_OPEN],
            "retained_events": len(self._events),
            "open_events": state_counts[INBOUND_EVENT_OPEN],
            "reply_verified_events": state_counts[INBOUND_EVENT_REPLY_VERIFIED],
            "no_reply_needed_events": state_counts[INBOUND_EVENT_NO_REPLY_NEEDED],
            "dropped_events": self._dropped_events,
            "dropped_open_events": self._dropped_open_events,
            "retention": "memory_only",
        }

    def events(self) -> list[dict[str, Any]]:
        return [
            {
                "event_id": self._event_id(event),
                "received_at": event.received_at,
                "state": self._state(event),
            }
            for event in self._events
        ]

    def resolve(self, event_id: str) -> InboundMonitorEvent:
        match = INBOUND_EVENT_ID_PATTERN.fullmatch(event_id)
        if match is None:
            raise BridgeError("inbound_event_invalid")
        sequence = int(match.group(1))
        for event in self._events:
            if event.sequence == sequence:
                return event
        raise BridgeError("inbound_event_unavailable")

    def require_open(self, event_id: str) -> InboundMonitorEvent:
        """Resolve one event only when it has not been deliberately closed."""

        event = self.resolve(event_id)
        if self._state(event) != INBOUND_EVENT_OPEN:
            raise BridgeError("inbound_event_not_open")
        return event

    def voice_stage_for(self, event: InboundMonitorEvent) -> tuple[str, InboundVoiceStage] | None:
        """Return the one active opaque stage for an event, if it has one."""

        handle = self._voice_stage_by_event.get(event.sequence)
        if handle is None:
            return None
        stage = self._voice_stages.get(handle)
        if stage is None:
            self._voice_stage_by_event.pop(event.sequence, None)
            return None
        return handle, stage

    def reserve_voice_stage(self, event: InboundMonitorEvent) -> None:
        """Claim an open event before an awaited Telegram media download."""

        if self._state(event) != INBOUND_EVENT_OPEN:
            raise BridgeError("inbound_event_not_open")
        if self.voice_stage_for(event) is not None or event.sequence in self._voice_stage_reservations:
            raise BridgeError("inbound_voice_stage_active")
        self._voice_stage_reservations.add(event.sequence)

    def release_voice_stage_reservation(self, event: InboundMonitorEvent) -> None:
        """Forget an unfinished stage claim after a failed or cancelled download."""

        self._voice_stage_reservations.discard(event.sequence)

    def stage_voice(
        self,
        event: InboundMonitorEvent,
        handle: str,
        path: Path,
        *,
        duration_seconds: int,
        suffix: str,
    ) -> InboundVoiceStage:
        """Keep an opaque handle for one transient voice file, never its source IDs."""

        if (
            re.fullmatch(r"[A-Za-z0-9_-]{24,128}", handle) is None
            or not isinstance(duration_seconds, int)
            or duration_seconds <= 0
            or suffix not in {".m4a", ".mp3", ".ogg", ".opus"}
        ):
            raise BridgeError("inbound_voice_stage_invalid")
        if self._state(event) != INBOUND_EVENT_OPEN:
            self.release_voice_stage_reservation(event)
            raise BridgeError("inbound_event_not_open")
        if self.voice_stage_for(event) is not None:
            raise BridgeError("inbound_voice_stage_active")
        if event.sequence not in self._voice_stage_reservations:
            raise BridgeError("inbound_voice_stage_reservation_lost")
        stage = InboundVoiceStage(
            event_sequence=event.sequence,
            path=path,
            duration_seconds=duration_seconds,
            suffix=suffix,
        )
        self._voice_stage_reservations.discard(event.sequence)
        self._voice_stages[handle] = stage
        self._voice_stage_by_event[event.sequence] = handle
        return stage

    def consume_voice_stage(self, handle: str) -> InboundVoiceStage | None:
        """Forget a staged file handle after deletion has been verified."""

        stage = self._voice_stages.pop(handle, None)
        if stage is not None:
            self._voice_stage_by_event.pop(stage.event_sequence, None)
        return stage

    def voice_stage(self, handle: str) -> InboundVoiceStage | None:
        """Find one opaque stage without relinquishing its cleanup retry handle."""

        return self._voice_stages.get(handle)

    def mark_reply_verified(self, *, peer_id: int, message_id: int) -> bool:
        """Close an active opaque ref only after a verified direct reply."""

        for event in self._events:
            if event.peer_id == peer_id and event.message_id == message_id:
                state = self._state(event)
                if state == INBOUND_EVENT_REPLY_VERIFIED:
                    return True
                if state != INBOUND_EVENT_OPEN:
                    return False
                self._voice_stage_reservations.discard(event.sequence)
                self._states[event.sequence] = INBOUND_EVENT_REPLY_VERIFIED
                return True
        return False

    def mark_no_reply_needed(self, event_id: str) -> dict[str, str]:
        """Allow an explicit operator decision without recording its rationale."""

        event = self.resolve(event_id)
        state = self._state(event)
        if state == INBOUND_EVENT_NO_REPLY_NEEDED:
            return {"event_id": self._event_id(event), "state": state}
        if state != INBOUND_EVENT_OPEN:
            raise BridgeError("inbound_event_already_closed")
        self._voice_stage_reservations.discard(event.sequence)
        self._states[event.sequence] = INBOUND_EVENT_NO_REPLY_NEEDED
        return {"event_id": self._event_id(event), "state": self._state(event)}

    def context(self, event_id: str, *, limit: int) -> list[InboundMonitorEvent]:
        """Return a small, anchor-centred window of one private chat's pending refs."""

        anchor = self.resolve(event_id)
        anchor_time = datetime.fromisoformat(anchor.received_at)
        peer_events = [
            event
            for event in self._events
            if event.peer_id == anchor.peer_id
            and abs((datetime.fromisoformat(event.received_at) - anchor_time).total_seconds())
            <= INBOUND_MONITOR_CONTEXT_WINDOW_SECONDS
        ]
        anchor_index = peer_events.index(anchor)
        start = max(0, anchor_index - limit // 2)
        end = min(len(peer_events), start + limit)
        start = max(0, end - limit)
        return peer_events[start:end]


def _config_for_account(account: str) -> TelegramConfig:
    paths = _account_paths(account)
    return TelegramConfig.load(
        paths.credentials_path,
        session_path=paths.session_path,
        state_dir=paths.state_dir,
        socket_path=paths.socket_path,
    )


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _issue_contract(secret: bytes, payload: dict[str, Any]) -> str:
    encoded = base64.urlsafe_b64encode(_canonical_json(payload)).rstrip(b"=")
    signature = hmac.new(secret, encoded, hashlib.sha256).digest()
    return f"{encoded.decode('ascii')}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii')}"


def _decode_contract(token: str, secret: bytes) -> dict[str, Any]:
    try:
        encoded_raw, signature_raw = token.split(".", 1)
        encoded = encoded_raw.encode("ascii")
        signature = base64.urlsafe_b64decode(signature_raw + "=" * (-len(signature_raw) % 4))
        if not hmac.compare_digest(signature, hmac.new(secret, encoded, hashlib.sha256).digest()):
            raise BridgeError("contract_invalid")
        payload = json.loads(base64.urlsafe_b64decode(encoded_raw + "=" * (-len(encoded_raw) % 4)))
    except BridgeError:
        raise
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeError("contract_invalid") from exc
    if not isinstance(payload, dict):
        raise BridgeError("contract_invalid")
    return payload


def _verify_contract_age(payload: dict[str, Any], now: int | None) -> None:
    current_time = int(time.time()) if now is None else int(now)
    issued_at = payload.get("issued_at")
    if not isinstance(issued_at, int) or issued_at > current_time + 60:
        raise BridgeError("contract_invalid")
    if current_time - issued_at > CONTRACT_TTL_SECONDS:
        raise BridgeError("contract_expired")


def issue_send_contract(
    secret: bytes,
    *,
    peer_id: int,
    text: str,
    last_message_id: int,
    reply_to_message_id: int = 0,
    reply_message_sha256: str = "",
    now: int | None = None,
) -> str:
    issued_at = int(time.time()) if now is None else int(now)
    payload = {
        "issued_at": issued_at,
        "last_message_id": int(last_message_id),
        "peer_id": int(peer_id),
        "reply_message_sha256": reply_message_sha256,
        "reply_to_message_id": int(reply_to_message_id),
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    return _issue_contract(secret, payload)


def verify_send_contract(
    token: str,
    secret: bytes,
    *,
    peer_id: int,
    text: str,
    reply_to_message_id: int = 0,
    reply_message_sha256: str = "",
    now: int | None = None,
) -> dict[str, Any]:
    payload = _decode_contract(token, secret)
    if payload.get("peer_id") != int(peer_id):
        raise BridgeError("contract_target_changed")
    if payload.get("text_sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest():
        raise BridgeError("contract_text_changed")
    if payload.get("reply_to_message_id", 0) != int(reply_to_message_id):
        raise BridgeError("contract_reply_target_changed")
    if payload.get("reply_message_sha256", "") != reply_message_sha256:
        raise BridgeError("contract_reply_source_changed")
    _verify_contract_age(payload, now)
    if not isinstance(payload.get("last_message_id"), int):
        raise BridgeError("contract_invalid")
    return payload


def issue_photo_contract(
    secret: bytes,
    *,
    peer_id: int,
    caption: str,
    photo_sha256: str,
    last_message_id: int,
    now: int | None = None,
) -> str:
    issued_at = int(time.time()) if now is None else int(now)
    payload = {
        "caption_sha256": hashlib.sha256(caption.encode("utf-8")).hexdigest(),
        "issued_at": issued_at,
        "last_message_id": int(last_message_id),
        "peer_id": int(peer_id),
        "photo_sha256": photo_sha256,
        "type": "photo",
    }
    return _issue_contract(secret, payload)


def verify_photo_contract(
    token: str,
    secret: bytes,
    *,
    peer_id: int,
    caption: str,
    photo_sha256: str,
    now: int | None = None,
) -> dict[str, Any]:
    payload = _decode_contract(token, secret)
    if payload.get("type") != "photo":
        raise BridgeError("contract_invalid")
    if payload.get("peer_id") != int(peer_id):
        raise BridgeError("contract_target_changed")
    if payload.get("caption_sha256") != hashlib.sha256(caption.encode("utf-8")).hexdigest():
        raise BridgeError("contract_text_changed")
    if payload.get("photo_sha256") != photo_sha256:
        raise BridgeError("contract_photo_changed")
    _verify_contract_age(payload, now)
    if not isinstance(payload.get("last_message_id"), int):
        raise BridgeError("contract_invalid")
    return payload


def issue_download_contract(
    secret: bytes,
    *,
    peer_id: int,
    message_id: int,
    media_fingerprint: str,
    now: int | None = None,
) -> str:
    issued_at = int(time.time()) if now is None else int(now)
    payload = {
        "issued_at": issued_at,
        "media_fingerprint": media_fingerprint,
        "message_id": int(message_id),
        "peer_id": int(peer_id),
        "type": "download",
    }
    return _issue_contract(secret, payload)


def verify_download_contract(
    token: str,
    secret: bytes,
    *,
    peer_id: int,
    message_id: int,
    media_fingerprint: str,
    now: int | None = None,
) -> dict[str, Any]:
    payload = _decode_contract(token, secret)
    if payload.get("type") != "download":
        raise BridgeError("contract_invalid")
    if payload.get("peer_id") != int(peer_id):
        raise BridgeError("contract_target_changed")
    if payload.get("message_id") != int(message_id):
        raise BridgeError("contract_message_changed")
    if payload.get("media_fingerprint") != media_fingerprint:
        raise BridgeError("contract_media_changed")
    _verify_contract_age(payload, now)
    return payload


def normalize_phone(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 32 or re.fullmatch(r"[+\d\s().-]+", raw) is None:
        raise BridgeError("phone_invalid")
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if not 8 <= len(digits) <= 15 or digits.startswith("0"):
        raise BridgeError("phone_invalid")
    return f"+{digits}"


def _validate_mode(mode: str, idempotency_key: str) -> None:
    if mode not in {"dry_run", "apply"}:
        raise BridgeError("mode_invalid")
    if mode == "apply" and not idempotency_key:
        raise BridgeError("idempotency_key_required")


def validate_send_request(request: dict[str, Any]) -> tuple[str, str, str, str, int]:
    peer = str(request.get("peer") or "").strip()
    text = str(request.get("text") or "")
    mode = str(request.get("mode") or "dry_run")
    idempotency_key = str(request.get("idempotency_key") or "").strip()
    raw_reply_to_message_id = request.get("reply_to_message_id", 0)
    if isinstance(raw_reply_to_message_id, bool):
        raise BridgeError("reply_to_message_id_invalid")
    try:
        reply_to_message_id = int(raw_reply_to_message_id or 0)
    except (TypeError, ValueError) as exc:
        raise BridgeError("reply_to_message_id_invalid") from exc
    if not peer:
        raise BridgeError("peer_required")
    if not text or len(text) > MAX_MESSAGE_CHARS:
        raise BridgeError("message_length_invalid")
    _validate_mode(mode, idempotency_key)
    if reply_to_message_id < 0:
        raise BridgeError("reply_to_message_id_invalid")
    return peer, text, mode, idempotency_key, reply_to_message_id


def validate_photo_request(request: dict[str, Any]) -> tuple[str, Path, str, str, str]:
    peer = str(request.get("peer") or "").strip()
    photo = Path(str(request.get("photo") or ""))
    caption = str(request.get("caption") or "")
    mode = str(request.get("mode") or "dry_run")
    idempotency_key = str(request.get("idempotency_key") or "").strip()
    if not peer:
        raise BridgeError("peer_required")
    if not caption or len(caption) > MAX_CAPTION_CHARS:
        raise BridgeError("caption_length_invalid")
    _validate_mode(mode, idempotency_key)
    return peer, photo, caption, mode, idempotency_key


def validate_download_request(request: dict[str, Any]) -> tuple[str, int, str, str]:
    peer = str(request.get("peer") or "").strip()
    mode = str(request.get("mode") or "dry_run")
    idempotency_key = str(request.get("idempotency_key") or "").strip()
    try:
        message_id = int(request.get("message_id") or 0)
    except (TypeError, ValueError) as exc:
        raise BridgeError("message_id_invalid") from exc
    if not peer:
        raise BridgeError("peer_required")
    if message_id <= 0:
        raise BridgeError("message_id_invalid")
    _validate_mode(mode, idempotency_key)
    return peer, message_id, mode, idempotency_key


def _message_media_metadata(message: Any) -> dict[str, Any]:
    media = getattr(message, "media", None)
    file = getattr(message, "file", None)
    mime_type = str(getattr(file, "mime_type", None) or "").casefold()
    file_name = str(getattr(file, "name", None) or "")
    try:
        size_bytes = int(getattr(file, "size", None) or 0)
    except (TypeError, ValueError):
        size_bytes = 0
    suffix = DOWNLOAD_MIME_SUFFIXES.get(mime_type, "")
    if file_name:
        supplied_suffix = Path(file_name).suffix.casefold()
        if supplied_suffix == suffix:
            suffix = supplied_suffix
    duration_seconds = 0
    voice = False
    video = False
    width = 0
    height = 0
    document = getattr(message, "document", None)
    for attribute in getattr(document, "attributes", None) or []:
        attribute_type = type(attribute).__name__
        if attribute_type == "DocumentAttributeAudio":
            try:
                duration_seconds = int(getattr(attribute, "duration", None) or 0)
            except (TypeError, ValueError):
                duration_seconds = 0
            voice = bool(getattr(attribute, "voice", False))
        elif attribute_type == "DocumentAttributeVideo":
            try:
                duration_seconds = int(getattr(attribute, "duration", None) or 0)
                width = int(getattr(attribute, "w", None) or 0)
                height = int(getattr(attribute, "h", None) or 0)
            except (TypeError, ValueError):
                duration_seconds = 0
                width = 0
                height = 0
            video = True
    if mime_type.startswith("audio/") and not 0 < duration_seconds <= 10 * 60:
        suffix = ""
    if mime_type == "video/mp4" and (
        not video or not 0 < duration_seconds <= MAX_VIDEO_DURATION_SECONDS or width <= 0 or height <= 0
    ):
        suffix = ""
    downloadable = media is not None and bool(suffix) and 0 < size_bytes <= MAX_DOWNLOAD_BYTES
    fingerprint_payload = {
        "file_name": Path(file_name).name if file_name else "",
        "media_type": type(media).__name__ if media is not None else None,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "suffix": suffix,
        "duration_seconds": duration_seconds or None,
        "voice": voice,
        "video": video,
        "width": width or None,
        "height": height or None,
    }
    return {
        **fingerprint_payload,
        "downloadable": downloadable,
        "fingerprint": hashlib.sha256(_canonical_json(fingerprint_payload)).hexdigest(),
    }


def _validate_download_content(content: bytes, *, mime_type: str, suffix: str) -> None:
    if not content or len(content) > MAX_DOWNLOAD_BYTES:
        raise BridgeError("download_size_invalid")
    valid = False
    if mime_type == "image/jpeg":
        valid = content.startswith(b"\xff\xd8\xff") and content.endswith(b"\xff\xd9")
    elif mime_type == "image/png":
        valid = content.startswith(b"\x89PNG\r\n\x1a\n")
    elif mime_type == "image/webp":
        valid = len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    elif mime_type == "application/pdf":
        valid = content.startswith(b"%PDF-")
    elif mime_type in {"audio/ogg", "audio/opus"}:
        valid = content.startswith(b"OggS")
    elif mime_type == "audio/mpeg":
        valid = content.startswith(b"ID3") or (len(content) >= 2 and content[0] == 0xFF and content[1] & 0xE0 == 0xE0)
    elif mime_type == "audio/mp4":
        valid = len(content) >= 12 and content[4:8] == b"ftyp"
    elif mime_type == "video/mp4":
        valid = len(content) >= 12 and content[4:8] == b"ftyp"
    elif suffix in {".docx", ".xlsx"}:
        valid = content.startswith(b"PK\x03\x04")
    elif mime_type in {"text/plain", "text/csv"}:
        try:
            content.decode("utf-8")
            valid = True
        except UnicodeDecodeError:
            valid = False
    if not valid:
        raise BridgeError("download_content_invalid")


def _save_private_download(content: bytes, *, message_id: int, suffix: str, inbox_dir: Path) -> tuple[Path, str]:
    inbox_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(inbox_dir, 0o700)
    inbox_stat = inbox_dir.stat()
    if inbox_stat.st_uid != os.geteuid() or stat.S_IMODE(inbox_stat.st_mode) != 0o700:
        raise BridgeError("download_inbox_permissions_invalid")
    digest = hashlib.sha256(content).hexdigest()
    output_path = inbox_dir / f"{message_id}-{digest[:16]}{suffix}"
    if output_path.exists():
        existing = _read_private_download(output_path, inbox_dir=inbox_dir)
        if not hmac.compare_digest(hashlib.sha256(existing).hexdigest(), digest):
            raise BridgeError("download_existing_conflict")
        return output_path, digest
    temp_path = inbox_dir / f".{message_id}-{secrets.token_hex(8)}.tmp"
    try:
        descriptor = os.open(
            temp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, output_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return output_path, digest


def _monitor_voice_stage_path(handle: str, *, suffix: str, inbox_dir: Path) -> Path:
    if re.fullmatch(r"[A-Za-z0-9_-]{24,128}", handle) is None:
        raise BridgeError("inbound_voice_handle_invalid")
    if suffix not in {".m4a", ".mp3", ".ogg", ".opus"}:
        raise BridgeError("inbound_voice_media_invalid")
    return inbox_dir / f"monitor-{handle}{suffix}"


def _save_monitor_voice_stage(content: bytes, *, handle: str, suffix: str, inbox_dir: Path) -> Path:
    """Save one monitored voice under an opaque runtime-only name."""

    inbox_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(inbox_dir, 0o700)
    inbox_stat = inbox_dir.stat()
    if inbox_stat.st_uid != os.geteuid() or stat.S_IMODE(inbox_stat.st_mode) != 0o700:
        raise BridgeError("download_inbox_permissions_invalid")
    output_path = _monitor_voice_stage_path(handle, suffix=suffix, inbox_dir=inbox_dir)
    if output_path.exists() or output_path.is_symlink():
        raise BridgeError("inbound_voice_stage_conflict")
    temp_path = inbox_dir / f".monitor-{secrets.token_urlsafe(18)}.tmp"
    try:
        descriptor = os.open(
            temp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, output_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return output_path


def _read_private_download(path: Path, *, inbox_dir: Path) -> bytes:
    if not path.is_absolute() or path.parent != inbox_dir or path.name in {"", ".", ".."}:
        raise BridgeError("download_path_invalid")
    try:
        inbox_fd = os.open(inbox_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise BridgeError("download_inbox_unavailable") from exc
    try:
        inbox_stat = os.fstat(inbox_fd)
        if inbox_stat.st_uid != os.geteuid() or stat.S_IMODE(inbox_stat.st_mode) != 0o700:
            raise BridgeError("download_inbox_permissions_invalid")
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=inbox_fd)
        except OSError as exc:
            raise BridgeError("download_unavailable") from exc
        try:
            file_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != os.geteuid()
                or stat.S_IMODE(file_stat.st_mode) != 0o600
                or not 0 < file_stat.st_size <= MAX_DOWNLOAD_BYTES
            ):
                raise BridgeError("download_permissions_invalid")
            chunks: list[bytes] = []
            remaining = file_stat.st_size
            while remaining > 0:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise BridgeError("download_read_incomplete")
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(inbox_fd)
    if len(content) > MAX_DOWNLOAD_BYTES:
        raise BridgeError("download_size_invalid")
    return content


def _discard_private_download(path: Path, *, inbox_dir: Path) -> bool:
    if not path.is_absolute() or path.parent != inbox_dir or path.name in {"", ".", ".."}:
        raise BridgeError("download_path_invalid")
    if not path.exists() and not path.is_symlink():
        return False
    _read_private_download(path, inbox_dir=inbox_dir)
    path.unlink()
    return True


def _load_validated_photo_file(
    path: Path,
    *,
    outbox_dir: Path = DEFAULT_OUTBOX_DIR,
) -> tuple[Path, str, int, bytes]:
    if not path.is_absolute() or not outbox_dir.is_absolute() or path.parent != outbox_dir:
        raise BridgeError("photo_path_invalid")
    if path.name in {"", ".", ".."} or path.suffix.casefold() not in {".jpg", ".jpeg"}:
        raise BridgeError("photo_path_invalid")
    try:
        outbox_fd = os.open(
            outbox_dir,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise BridgeError("photo_unavailable") from exc
    try:
        outbox_stat = os.fstat(outbox_fd)
        if outbox_stat.st_uid != os.geteuid() or stat.S_IMODE(outbox_stat.st_mode) != 0o700:
            raise BridgeError("photo_outbox_permissions_invalid")
        try:
            file_fd = os.open(
                path.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=outbox_fd,
            )
        except OSError as exc:
            raise BridgeError("photo_unavailable") from exc
        try:
            file_stat = os.fstat(file_fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise BridgeError("photo_invalid")
            if file_stat.st_uid != os.geteuid() or stat.S_IMODE(file_stat.st_mode) != 0o600:
                raise BridgeError("photo_permissions_invalid")
            if not 4 <= file_stat.st_size <= MAX_PHOTO_BYTES:
                raise BridgeError("photo_size_invalid")
            content = bytearray()
            while len(content) <= MAX_PHOTO_BYTES:
                chunk = os.read(file_fd, min(1024 * 1024, MAX_PHOTO_BYTES + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
            if len(content) > MAX_PHOTO_BYTES:
                raise BridgeError("photo_size_invalid")
        except OSError as exc:
            raise BridgeError("photo_unreadable") from exc
        finally:
            os.close(file_fd)
    finally:
        os.close(outbox_fd)
    if not content.startswith(b"\xff\xd8") or not content.endswith(b"\xff\xd9"):
        raise BridgeError("photo_invalid")
    immutable_content = bytes(content)
    return path, hashlib.sha256(immutable_content).hexdigest(), len(immutable_content), immutable_content


def validate_photo_file(path: Path, *, outbox_dir: Path = DEFAULT_OUTBOX_DIR) -> tuple[Path, str, int]:
    """Validate one private JPEG without allowing a later pathname race."""
    validated_path, digest, size, _content = _load_validated_photo_file(path, outbox_dir=outbox_dir)
    return validated_path, digest, size


def redact_sensitive_message_text(text: str) -> tuple[str, bool]:
    redacted, replacements = SENSITIVE_URI_PATTERN.subn("[redacted_sensitive_uri]", text)
    return redacted, replacements > 0


def _load_telethon() -> tuple[Any, Any, Any]:
    try:
        from telethon import TelegramClient, utils
        from telethon.errors import SessionPasswordNeededError
    except ImportError as exc:
        raise BridgeError("telethon_not_installed") from exc
    return TelegramClient, utils, SessionPasswordNeededError


def _load_telegram_events() -> Any:
    try:
        from telethon import events
    except ImportError as exc:
        raise BridgeError("telethon_not_installed") from exc
    return events


def _load_telegram_functions() -> tuple[Any, Any]:
    try:
        from telethon import functions, utils
    except ImportError as exc:
        raise BridgeError("telethon_not_installed") from exc
    return functions, utils


def _read_hidden_terminal_value(prompt: str) -> str:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise BridgeError("interactive_terminal_required")
    try:
        value = getpass.getpass(prompt, stream=sys.stderr)
    except (EOFError, KeyboardInterrupt) as exc:
        raise BridgeError("interactive_input_unavailable") from exc
    if not value:
        raise BridgeError("interactive_input_empty")
    return value


def _ensure_private_key(path: Path) -> bytes:
    for _ in range(2):
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            key = secrets.token_bytes(32)
            try:
                descriptor = os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                )
            except FileExistsError:
                continue
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
            return key
        except OSError as exc:
            raise BridgeError("contract_key_unavailable") from exc
        try:
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != os.geteuid():
                raise BridgeError("contract_key_invalid")
            if stat.S_IMODE(file_stat.st_mode) != 0o600:
                raise BridgeError("contract_key_permissions_too_open")
            key = os.read(descriptor, 33)
        finally:
            os.close(descriptor)
        if len(key) != 32:
            raise BridgeError("contract_key_invalid")
        return key
    raise BridgeError("contract_key_unavailable")


def _load_idempotency(path: Path) -> dict[str, dict[str, Any]]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise BridgeError("idempotency_state_unreadable") from exc
    try:
        file_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.geteuid()
            or stat.S_IMODE(file_stat.st_mode) != 0o600
            or file_stat.st_size > 5 * 1024 * 1024
        ):
            raise BridgeError("idempotency_state_invalid")
        raw = os.read(descriptor, 5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            raise BridgeError("idempotency_state_invalid")
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeError("idempotency_state_unreadable") from exc
    finally:
        os.close(descriptor)
    if not isinstance(payload, dict):
        raise BridgeError("idempotency_state_invalid")
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


def _save_idempotency(path: Path, payload: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        descriptor = os.open(
            temp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _target_from_entity(entity: Any, utils: Any) -> dict[str, Any]:
    title = (
        getattr(entity, "title", None)
        or " ".join(part for part in (getattr(entity, "first_name", None), getattr(entity, "last_name", None)) if part)
        or getattr(entity, "username", None)
        or "Unknown"
    )
    entity_type = type(entity).__name__.casefold()
    if getattr(entity, "broadcast", False):
        kind = "channel"
    elif getattr(entity, "megagroup", False):
        kind = "supergroup"
    elif entity_type == "chat":
        kind = "group"
    elif entity_type == "user":
        kind = "bot" if getattr(entity, "bot", False) else "private"
    else:
        kind = "unknown"
    return {
        "id": int(utils.get_peer_id(entity)),
        "title": str(title),
        "username": getattr(entity, "username", None),
        "kind": kind,
        "is_contact": bool(getattr(entity, "contact", False)),
    }


async def _search_entities(client: Any, query: str, limit: int) -> list[tuple[Any, dict[str, Any]]]:
    functions, utils = _load_telegram_functions()

    result = await client(functions.contacts.SearchRequest(q=query, limit=limit))
    normalized = query.casefold()
    matches: list[tuple[Any, dict[str, Any]]] = []
    seen: set[int] = set()
    for entity in [*getattr(result, "users", []), *getattr(result, "chats", [])]:
        target = _target_from_entity(entity, utils)
        if target["id"] in seen:
            continue
        title = target["title"].casefold()
        username = str(target.get("username") or "").casefold()
        if normalized not in title and normalized.lstrip("@") not in username:
            continue
        seen.add(target["id"])
        target["exact_title"] = title == normalized
        target["exact_username"] = username == normalized.lstrip("@")
        matches.append((entity, target))
    matches.sort(
        key=lambda item: (
            not item[1]["exact_title"],
            not item[1]["exact_username"],
            not item[1]["is_contact"],
            item[1]["kind"] != "private",
            item[1]["title"].casefold(),
        )
    )
    return matches[:limit]


async def _resolve_phone(client: Any, raw_phone: str) -> tuple[Any, dict[str, Any]]:
    global _PHONE_RESOLVE_LAST_AT

    phone = normalize_phone(raw_phone)
    current_time = time.monotonic()
    if current_time - _PHONE_RESOLVE_LAST_AT < PHONE_RESOLVE_INTERVAL_SECONDS:
        raise BridgeError("phone_resolve_rate_limited")
    _PHONE_RESOLVE_LAST_AT = current_time
    functions, utils = _load_telegram_functions()
    try:
        result = await client(functions.contacts.ResolvePhoneRequest(phone=phone))
    except Exception as exc:
        if type(exc).__name__ == "PhoneNotOccupiedError":
            raise BridgeError("phone_not_resolved") from exc
        raise
    user_id = int(getattr(getattr(result, "peer", None), "user_id", 0) or 0)
    users = [user for user in getattr(result, "users", []) if int(getattr(user, "id", 0) or 0) == user_id]
    if user_id <= 0 or len(users) != 1:
        raise BridgeError("phone_not_resolved")
    entity = users[0]
    target = _target_from_entity(entity, utils)
    if target["kind"] != "private":
        raise BridgeError("phone_not_resolved")
    return entity, target


async def _resolve_peer(client: Any, raw_peer: str) -> tuple[Any, dict[str, Any]]:
    _, utils, _ = _load_telethon()
    candidate: Any = raw_peer
    if raw_peer.lstrip("-").isdigit():
        candidate = int(raw_peer)
    if raw_peer.startswith("@") or isinstance(candidate, int):
        try:
            entity = await client.get_entity(candidate)
        except Exception as exc:
            raise BridgeError("peer_not_found") from exc
    else:
        search_matches = await _search_entities(client, raw_peer, 20)
        matches = [entity for entity, target in search_matches if target["exact_title"] or target["exact_username"]]
        if not matches:
            raise BridgeError("peer_not_found")
        if len(matches) != 1:
            raise BridgeError("peer_ambiguous")
        entity = matches[0]
    return entity, _target_from_entity(entity, utils)


async def _last_message_id(client: Any, entity: Any) -> int:
    messages = await client.get_messages(entity, limit=1)
    return int(messages[0].id) if messages else 0


def _message_reply_to_id(message: Any) -> int:
    direct = getattr(message, "reply_to_msg_id", None)
    nested = getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)
    try:
        return int(direct or nested or 0)
    except (TypeError, ValueError):
        return 0


async def _load_reply_source(client: Any, entity: Any, message_id: int) -> dict[str, Any] | None:
    if message_id == 0:
        return None
    message = await client.get_messages(entity, ids=message_id)
    if message is None or int(getattr(message, "id", 0) or 0) != message_id:
        raise BridgeError("reply_source_not_found")
    raw_text = str(getattr(message, "message", None) or "")
    return {
        "message_id": message_id,
        "text_sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        "out": bool(getattr(message, "out", False)),
        "date": message.date.isoformat() if getattr(message, "date", None) else None,
    }


async def _handle_send_photo(client: Any, config: TelegramConfig, request: dict[str, Any]) -> dict[str, Any]:
    peer, photo, caption, mode, idempotency_key = validate_photo_request(request)
    photo_path, photo_sha256, photo_bytes, photo_content = _load_validated_photo_file(
        photo,
        outbox_dir=config.socket_path.parent / "outbox",
    )
    entity, target = await _resolve_peer(client, peer)
    if target["kind"] != "private":
        raise BridgeError("private_peer_required")
    last_message_id = await _last_message_id(client, entity)
    contract_key = _ensure_private_key(config.state_dir / "contract.key")
    caption_sha256 = hashlib.sha256(caption.encode("utf-8")).hexdigest()
    if mode == "dry_run":
        return {
            "ok": True,
            "mode": "dry_run",
            "target": target,
            "caption_chars": len(caption),
            "caption_sha256": caption_sha256,
            "photo_bytes": photo_bytes,
            "photo_sha256": photo_sha256,
            "last_message_id": last_message_id,
            "contract_token": issue_photo_contract(
                contract_key,
                peer_id=target["id"],
                caption=caption,
                photo_sha256=photo_sha256,
                last_message_id=last_message_id,
            ),
        }

    contract_token = str(request.get("contract_token") or "")
    if not contract_token:
        raise BridgeError("contract_required")
    contract = verify_photo_contract(
        contract_token,
        contract_key,
        peer_id=target["id"],
        caption=caption,
        photo_sha256=photo_sha256,
    )
    idempotency_path = config.state_dir / "idempotency.json"
    idempotency = _load_idempotency(idempotency_path)
    previous = idempotency.get(idempotency_key)
    if previous is not None:
        if (
            previous.get("operation") != "send_photo"
            or previous.get("peer_id") != target["id"]
            or previous.get("caption_sha256") != caption_sha256
            or previous.get("photo_sha256") != photo_sha256
        ):
            raise BridgeError("idempotency_key_conflict")
    else:
        if contract["last_message_id"] != last_message_id:
            raise BridgeError("conversation_changed_since_dry_run")
        upload = io.BytesIO(photo_content)
        upload.name = photo_path.name
        sent = await client.send_file(entity, upload, caption=caption, force_document=False)
        idempotency[idempotency_key] = {
            "operation": "send_photo",
            "message_id": int(sent.id),
            "peer_id": target["id"],
            "caption_sha256": caption_sha256,
            "photo_sha256": photo_sha256,
        }
        _save_idempotency(idempotency_path, idempotency)
    message_id = int(idempotency[idempotency_key]["message_id"])
    readback = await client.get_messages(entity, ids=message_id)
    if readback is None or str(readback.message or "") != caption or readback.media is None:
        raise BridgeError("send_readback_failed")
    return {
        "ok": True,
        "mode": "apply",
        "replayed": previous is not None,
        "target": target,
        "message_id": message_id,
        "verified": True,
    }


async def _handle_download(client: Any, config: TelegramConfig, request: dict[str, Any]) -> dict[str, Any]:
    peer, message_id, mode, idempotency_key = validate_download_request(request)
    entity, target = await _resolve_peer(client, peer)
    message = await client.get_messages(entity, ids=message_id)
    if message is None or int(getattr(message, "id", 0) or 0) != message_id:
        raise BridgeError("message_not_found")
    metadata = _message_media_metadata(message)
    if not metadata["downloadable"]:
        raise BridgeError("media_not_downloadable")
    contract_key = _ensure_private_key(config.state_dir / "contract.key")
    if mode == "dry_run":
        return {
            "ok": True,
            "mode": "dry_run",
            "target": target,
            "message_id": message_id,
            "media": {key: value for key, value in metadata.items() if key != "fingerprint"},
            "contract_token": issue_download_contract(
                contract_key,
                peer_id=target["id"],
                message_id=message_id,
                media_fingerprint=metadata["fingerprint"],
            ),
        }

    contract_token = str(request.get("contract_token") or "")
    if not contract_token:
        raise BridgeError("contract_required")
    verify_download_contract(
        contract_token,
        contract_key,
        peer_id=target["id"],
        message_id=message_id,
        media_fingerprint=metadata["fingerprint"],
    )
    idempotency_path = config.state_dir / "idempotency.json"
    idempotency = _load_idempotency(idempotency_path)
    previous = idempotency.get(idempotency_key)
    if previous is not None:
        if (
            previous.get("operation") != "download_media"
            or previous.get("peer_id") != target["id"]
            or previous.get("message_id") != message_id
            or previous.get("media_fingerprint") != metadata["fingerprint"]
        ):
            raise BridgeError("idempotency_key_conflict")
        saved_path = Path(str(previous.get("saved_path") or ""))
        try:
            replay_content = _read_private_download(
                saved_path,
                inbox_dir=config.socket_path.parent / "inbox",
            )
        except BridgeError as exc:
            raise BridgeError("download_replay_missing") from exc
        if len(replay_content) != previous.get("size_bytes") or hashlib.sha256(
            replay_content
        ).hexdigest() != previous.get("sha256"):
            raise BridgeError("download_replay_mismatch")
        return {
            "ok": True,
            "mode": "apply",
            "replayed": True,
            "target": target,
            "message_id": message_id,
            "saved_path": str(saved_path),
            "sha256": previous.get("sha256"),
            "size_bytes": previous.get("size_bytes"),
            "mime_type": metadata["mime_type"],
        }

    content = await client.download_media(message, file=bytes)
    if not isinstance(content, bytes):
        raise BridgeError("download_failed")
    if metadata["size_bytes"] != len(content):
        raise BridgeError("download_size_mismatch")
    _validate_download_content(content, mime_type=metadata["mime_type"], suffix=metadata["suffix"])
    saved_path, digest = _save_private_download(
        content,
        message_id=message_id,
        suffix=metadata["suffix"],
        inbox_dir=config.socket_path.parent / "inbox",
    )
    idempotency[idempotency_key] = {
        "operation": "download_media",
        "peer_id": target["id"],
        "message_id": message_id,
        "media_fingerprint": metadata["fingerprint"],
        "saved_path": str(saved_path),
        "sha256": digest,
        "size_bytes": len(content),
    }
    _save_idempotency(idempotency_path, idempotency)
    return {
        "ok": True,
        "mode": "apply",
        "replayed": False,
        "target": target,
        "message_id": message_id,
        "saved_path": str(saved_path),
        "sha256": digest,
        "size_bytes": len(content),
        "mime_type": metadata["mime_type"],
        "verified": True,
    }


async def _handle_send_text_to_entity(
    client: Any,
    config: TelegramConfig,
    request: dict[str, Any],
    *,
    entity: Any,
    target: dict[str, Any],
    text: str,
    mode: str,
    idempotency_key: str,
    reply_to_message_id: int = 0,
    inbound_monitor: InboundMonitor | None = None,
) -> dict[str, Any]:
    if target["kind"] != "private":
        raise BridgeError("private_peer_required")
    reply_source = await _load_reply_source(client, entity, reply_to_message_id)
    reply_message_sha256 = reply_source["text_sha256"] if reply_source is not None else ""
    last_message_id = await _last_message_id(client, entity)
    contract_key = _ensure_private_key(config.state_dir / "contract.key")
    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if mode == "dry_run":
        return {
            "ok": True,
            "mode": "dry_run",
            "target": target,
            "message_chars": len(text),
            "text_sha256": text_sha256,
            "last_message_id": last_message_id,
            "reply_source": reply_source,
            "contract_token": issue_send_contract(
                contract_key,
                peer_id=target["id"],
                text=text,
                last_message_id=last_message_id,
                reply_to_message_id=reply_to_message_id,
                reply_message_sha256=reply_message_sha256,
            ),
        }

    contract_token = str(request.get("contract_token") or "")
    if not contract_token:
        raise BridgeError("contract_required")
    contract = verify_send_contract(
        contract_token,
        contract_key,
        peer_id=target["id"],
        text=text,
        reply_to_message_id=reply_to_message_id,
        reply_message_sha256=reply_message_sha256,
    )
    idempotency_path = config.state_dir / "idempotency.json"
    idempotency = _load_idempotency(idempotency_path)
    previous = idempotency.get(idempotency_key)
    if previous is not None:
        if (
            previous.get("operation") not in {None, "send_text"}
            or previous.get("peer_id") != target["id"]
            or previous.get("text_sha256") != text_sha256
            or previous.get("reply_to_message_id", 0) != reply_to_message_id
            or previous.get("reply_message_sha256", "") != reply_message_sha256
        ):
            raise BridgeError("idempotency_key_conflict")
    else:
        if contract["last_message_id"] != last_message_id:
            raise BridgeError("conversation_changed_since_dry_run")
        sent = await client.send_message(
            entity, text, **({"reply_to": reply_to_message_id} if reply_to_message_id else {})
        )
        idempotency[idempotency_key] = {
            "operation": "send_text",
            "message_id": int(sent.id),
            "peer_id": target["id"],
            "reply_message_sha256": reply_message_sha256,
            "reply_to_message_id": reply_to_message_id,
            "text_sha256": text_sha256,
            "readback_verified": False,
        }
        _save_idempotency(idempotency_path, idempotency)
    message_id = int(idempotency[idempotency_key]["message_id"])
    readback = await client.get_messages(entity, ids=message_id)
    if readback is None or str(readback.message or "") != text or _message_reply_to_id(readback) != reply_to_message_id:
        raise BridgeError("send_readback_failed")
    idempotency[idempotency_key]["readback_verified"] = True
    _save_idempotency(idempotency_path, idempotency)
    monitor_event_closed: bool | None = None
    if reply_to_message_id and inbound_monitor is not None:
        monitor_event_closed = inbound_monitor.mark_reply_verified(
            peer_id=int(target["id"]), message_id=reply_to_message_id
        )
    response = {
        "ok": True,
        "mode": "apply",
        "replayed": previous is not None,
        "target": target,
        "message_id": message_id,
        "reply_to_message_id": reply_to_message_id or None,
        "reply_verified": bool(reply_to_message_id),
        "verified": True,
    }
    if monitor_event_closed is not None:
        response["monitor_event_closed"] = monitor_event_closed
    return response


def _monitor_context_limit(value: Any) -> int:
    if isinstance(value, bool):
        raise BridgeError("inbound_context_limit_invalid")
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise BridgeError("inbound_context_limit_invalid") from exc
    if not 1 <= limit <= MAX_INBOUND_MONITOR_CONTEXT_MESSAGES:
        raise BridgeError("inbound_context_limit_invalid")
    return limit


def _validate_inbound_monitor_messages(
    events: list[InboundMonitorEvent],
    result: Any,
) -> list[Any]:
    if not events:
        return []
    messages = [result] if len(events) == 1 else list(result or [])
    if len(messages) != len(events):
        raise BridgeError("inbound_message_unavailable")
    for event, message in zip(events, messages, strict=True):
        if (
            message is None
            or int(getattr(message, "id", 0) or 0) != event.message_id
            or bool(getattr(message, "out", False))
            or not bool(getattr(message, "is_private", False))
            or int(getattr(message, "chat_id", 0) or 0) != event.peer_id
        ):
            raise BridgeError("inbound_message_unavailable")
    return messages


async def _load_global_inbound_monitor_messages(
    client: Any,
    events: list[InboundMonitorEvent],
    requested_ids: int | list[int],
) -> list[Any]:
    try:
        result = await client.get_messages(None, ids=requested_ids)
    except Exception as exc:  # avoid exposing live Telegram transport details.
        raise BridgeError("inbound_message_unavailable") from exc
    return _validate_inbound_monitor_messages(events, result)


async def _load_inbound_monitor_context_messages(client: Any, events: list[InboundMonitorEvent]) -> list[Any]:
    if not events:
        return []
    message_ids = [event.message_id for event in events]
    requested_ids: int | list[int] = message_ids[0] if len(message_ids) == 1 else message_ids
    try:
        # A first private update may not have an access hash.  Requesting a
        # bounded set of non-channel message IDs avoids resolving or persisting
        # a contact/entity; the checks below bind each result back to its ref.
        return await _load_global_inbound_monitor_messages(client, events, requested_ids)
    except BridgeError:  # Try the bounded exact-peer fallback below.
        pass

    peer_id = events[0].peer_id
    if any(event.peer_id != peer_id for event in events):
        raise BridgeError("inbound_message_unavailable")
    try:
        # The work monitor has entity persistence disabled.  This fallback does
        # not serialize dialog details and touches at most 20 recent dialogs to
        # find the exact peer already held by the content-free monitor ref.
        entity = None
        async for dialog in client.iter_dialogs(limit=MAX_INBOUND_MONITOR_FALLBACK_DIALOGS):
            try:
                dialog_id = int(getattr(dialog, "id", 0) or 0)
            except (TypeError, ValueError):
                continue
            if dialog_id == peer_id:
                entity = getattr(dialog, "entity", None)
                break
        if entity is None:
            raise BridgeError("inbound_message_unavailable")
        result = await client.get_messages(entity, ids=requested_ids)
        return _validate_inbound_monitor_messages(events, result)
    except BridgeError:
        raise
    except Exception as exc:  # avoid exposing live Telegram transport details.
        raise BridgeError("inbound_message_unavailable") from exc


def _monitor_context_media_kind(message: Any) -> str | None:
    if getattr(message, "media", None) is None:
        return None
    if getattr(message, "photo", None) is not None:
        return "photo"
    if _message_media_metadata(message)["voice"]:
        return "voice"
    return "media"


def _monitor_context_payload(message: Any) -> dict[str, Any]:
    text, sensitive_content_redacted = redact_sensitive_message_text(str(getattr(message, "message", "") or ""))
    return {
        "date": message.date.isoformat() if getattr(message, "date", None) else None,
        "text": text,
        "sensitive_content_redacted": sensitive_content_redacted,
        "has_media": bool(getattr(message, "media", None)),
        "media_kind": _monitor_context_media_kind(message),
    }


def _monitor_context_message(event: InboundMonitorEvent, message: Any) -> dict[str, Any]:
    return {
        "event_id": f"inbound-{event.sequence}",
        "message": _monitor_context_payload(message),
    }


async def _handle_monitor_read(client: Any, monitor: InboundMonitor, event_id: str) -> dict[str, Any]:
    event = monitor.resolve(event_id)
    message = (await _load_inbound_monitor_context_messages(client, [event]))[0]
    return {
        "ok": True,
        "event_id": event_id,
        "message": _monitor_context_payload(message),
    }


async def _handle_monitor_context(
    client: Any,
    monitor: InboundMonitor,
    event_id: str,
    *,
    limit: int,
) -> dict[str, Any]:
    events = monitor.context(event_id, limit=limit)
    messages = [
        _monitor_context_message(event, message)
        for event, message in zip(events, await _load_inbound_monitor_context_messages(client, events), strict=True)
    ]
    return {"ok": True, "event_id": event_id, "messages": messages}


def _monitor_voice_stage_response(event_id: str, handle: str, stage: InboundVoiceStage) -> dict[str, Any]:
    return {
        "ok": True,
        "event_id": event_id,
        "media": {
            "kind": "voice",
            "duration_seconds": stage.duration_seconds,
            "suffix": stage.suffix,
        },
        "media_handle": handle,
    }


def _require_monitor_voice_metadata(message: Any) -> dict[str, Any]:
    metadata = _message_media_metadata(message)
    if not metadata["voice"] or not metadata["downloadable"]:
        raise BridgeError("inbound_voice_media_invalid")
    if metadata["suffix"] not in {".m4a", ".mp3", ".ogg", ".opus"}:
        raise BridgeError("inbound_voice_media_invalid")
    return metadata


async def _expire_monitor_voice_stage(
    config: TelegramConfig,
    monitor: InboundMonitor,
    handle: str,
) -> None:
    """Bound an abandoned local voice hand-off without retaining its content."""

    try:
        await asyncio.sleep(MONITOR_VOICE_STAGE_TTL_SECONDS)
        for cleanup_attempt in range(MONITOR_VOICE_STAGE_CLEANUP_RETRIES):
            async with _MONITOR_VOICE_LOCK:
                staged = monitor.voice_stage(handle)
                if staged is None:
                    return
                try:
                    removed = _discard_private_download(staged.path, inbox_dir=config.socket_path.parent / "inbox")
                except BridgeError:
                    pass
                else:
                    if removed:
                        monitor.consume_voice_stage(handle)
                        return
            if cleanup_attempt + 1 < MONITOR_VOICE_STAGE_CLEANUP_RETRIES:
                await asyncio.sleep(MONITOR_VOICE_STAGE_CLEANUP_RETRY_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - an expiry task must not leak an unhandled exception.
        # Expiry is only a best-effort orphan cleanup.  The service shutdown
        # also removes its RuntimeDirectory, and no dialogue data is logged.
        return


def _schedule_monitor_voice_stage_expiry(config: TelegramConfig, monitor: InboundMonitor, handle: str) -> None:
    task = asyncio.create_task(
        _expire_monitor_voice_stage(config, monitor, handle),
        name="autostop-monitor-voice-expiry",
    )
    _MONITOR_VOICE_EXPIRY_TASKS.add(task)
    task.add_done_callback(_MONITOR_VOICE_EXPIRY_TASKS.discard)


async def _handle_monitor_voice_stage(
    client: Any,
    config: TelegramConfig,
    monitor: InboundMonitor,
    *,
    event_id: str,
) -> dict[str, Any]:
    event = monitor.require_open(event_id)
    monitor.reserve_voice_stage(event)
    path: Path | None = None
    try:
        async with asyncio.timeout(MONITOR_VOICE_STAGE_TIMEOUT_SECONDS):
            message = (await _load_inbound_monitor_context_messages(client, [event]))[0]
            metadata = _require_monitor_voice_metadata(message)
            content = await client.download_media(message, file=bytes)
        if not isinstance(content, bytes):
            raise BridgeError("download_failed")
        if metadata["size_bytes"] != len(content):
            raise BridgeError("download_size_mismatch")
        _validate_download_content(content, mime_type=metadata["mime_type"], suffix=metadata["suffix"])
        handle = secrets.token_urlsafe(24)
        path = _save_monitor_voice_stage(
            content,
            handle=handle,
            suffix=metadata["suffix"],
            inbox_dir=config.socket_path.parent / "inbox",
        )
        stage = monitor.stage_voice(
            event,
            handle,
            path,
            duration_seconds=metadata["duration_seconds"],
            suffix=metadata["suffix"],
        )
    except TimeoutError as exc:
        monitor.release_voice_stage_reservation(event)
        raise BridgeError("inbound_voice_download_timeout") from exc
    except BaseException:
        monitor.release_voice_stage_reservation(event)
        if path is not None:
            with suppress(BridgeError):
                _discard_private_download(path, inbox_dir=config.socket_path.parent / "inbox")
        raise
    _schedule_monitor_voice_stage_expiry(config, monitor, handle)
    return _monitor_voice_stage_response(event_id, handle, stage)


async def _handle_monitor_voice_discard(
    config: TelegramConfig,
    monitor: InboundMonitor,
    *,
    handle: str,
    runner_cleanup_verified: bool = False,
) -> dict[str, Any]:
    staged = monitor.voice_stage(handle)
    if staged is None:
        raise BridgeError("inbound_voice_stage_unavailable")
    removed = _discard_private_download(staged.path, inbox_dir=config.socket_path.parent / "inbox")
    if not removed and runner_cleanup_verified is not True:
        raise BridgeError("inbound_voice_cleanup_unverified")
    monitor.consume_voice_stage(handle)
    return {"ok": True, "cleanup_verified": True}


async def _handle_monitor_operation(
    client: Any,
    config: TelegramConfig,
    request: dict[str, Any],
    inbound_monitor: InboundMonitor | None,
) -> dict[str, Any] | None:
    operation = str(request.get("operation") or "")
    if operation not in {
        "monitor_status",
        "monitor_events",
        "monitor_read",
        "monitor_context",
        "monitor_mark",
        "monitor_voice_stage",
        "monitor_voice_discard",
    }:
        return None
    if inbound_monitor is None:
        raise BridgeError("inbound_monitor_disabled")
    if operation == "monitor_status":
        return {"ok": True, "monitor": inbound_monitor.status()}
    if operation == "monitor_events":
        return {"ok": True, "monitor": inbound_monitor.status(), "events": inbound_monitor.events()}
    if operation == "monitor_mark":
        if str(request.get("disposition") or "") != INBOUND_EVENT_NO_REPLY_NEEDED:
            raise BridgeError("inbound_disposition_invalid")
        return {"ok": True, "event": inbound_monitor.mark_no_reply_needed(str(request.get("event_id") or ""))}
    if operation == "monitor_voice_stage":
        return await _handle_monitor_voice_stage(
            client,
            config,
            inbound_monitor,
            event_id=str(request.get("event_id") or ""),
        )
    if operation == "monitor_voice_discard":
        return await _handle_monitor_voice_discard(
            config,
            inbound_monitor,
            handle=str(request.get("media_handle") or ""),
            runner_cleanup_verified=request.get("runner_cleanup_verified") is True,
        )
    if operation == "monitor_context":
        return await _handle_monitor_context(
            client,
            inbound_monitor,
            str(request.get("event_id") or ""),
            limit=_monitor_context_limit(request.get("limit", MAX_INBOUND_MONITOR_CONTEXT_MESSAGES)),
        )
    return await _handle_monitor_read(client, inbound_monitor, str(request.get("event_id") or ""))


async def _handle_operation(
    client: Any,
    config: TelegramConfig,
    request: dict[str, Any],
    *,
    inbound_monitor: InboundMonitor | None = None,
) -> dict[str, Any]:
    operation = str(request.get("operation") or "")
    if operation == "probe":
        return {"ok": True, "authorized": bool(await client.get_me())}

    if operation == "status":
        me = await client.get_me()
        return {
            "ok": True,
            "authorized": bool(me),
            "account": {
                "id": int(getattr(me, "id", 0) or 0),
                "name": " ".join(
                    part for part in (getattr(me, "first_name", None), getattr(me, "last_name", None)) if part
                ),
                "username": getattr(me, "username", None),
            }
            if me
            else None,
        }

    monitor_response = await _handle_monitor_operation(client, config, request, inbound_monitor)
    if monitor_response is not None:
        return monitor_response

    if operation == "dialogs":
        limit = max(1, min(int(request.get("limit") or 20), 100))
        _, utils, _ = _load_telethon()
        dialogs = []
        async for dialog in client.iter_dialogs(limit=limit):
            target = _target_from_entity(dialog.entity, utils)
            dialogs.append({**target, "unread_count": int(dialog.unread_count or 0)})
        return {"ok": True, "dialogs": dialogs}

    if operation == "search":
        query = str(request.get("query") or "").strip()
        if not query:
            raise BridgeError("query_required")
        limit = max(1, min(int(request.get("limit") or 20), 50))
        matches = await _search_entities(client, query, limit)
        return {"ok": True, "query": query, "matches": [target for _, target in matches]}

    if operation == "resolve_phone":
        _entity, target = await _resolve_phone(client, str(request.get("phone") or ""))
        return {
            "ok": True,
            "resolved": True,
            "target": {
                "id": target["id"],
                "kind": target["kind"],
                "is_contact": target["is_contact"],
            },
        }

    if operation == "read":
        peer = str(request.get("peer") or "").strip()
        if not peer:
            raise BridgeError("peer_required")
        limit = max(1, min(int(request.get("limit") or 20), 100))
        entity, target = await _resolve_peer(client, peer)
        messages = await client.get_messages(entity, limit=limit)
        rows = []
        for message in reversed(messages):
            text, sensitive_content_redacted = redact_sensitive_message_text(str(message.message or ""))
            rows.append(
                {
                    "id": int(message.id),
                    "date": message.date.isoformat() if message.date else None,
                    "out": bool(message.out),
                    "reply_to_message_id": _message_reply_to_id(message) or None,
                    "text": text,
                    "sensitive_content_redacted": sensitive_content_redacted,
                    "media_type": type(message.media).__name__ if message.media is not None else None,
                    "media": {
                        key: value for key, value in _message_media_metadata(message).items() if key != "fingerprint"
                    }
                    if message.media is not None
                    else None,
                }
            )
        return {"ok": True, "target": target, "messages": rows}

    if operation == "send":
        peer, text, mode, idempotency_key, reply_to_message_id = validate_send_request(request)
        entity, target = await _resolve_peer(client, peer)
        return await _handle_send_text_to_entity(
            client,
            config,
            request,
            entity=entity,
            target=target,
            text=text,
            mode=mode,
            idempotency_key=idempotency_key,
            reply_to_message_id=reply_to_message_id,
            inbound_monitor=inbound_monitor,
        )

    if operation == "send_photo":
        return await _handle_send_photo(client, config, request)

    if operation == "download":
        return await _handle_download(client, config, request)

    if operation == "discard_download":
        path = Path(str(request.get("path") or ""))
        removed = _discard_private_download(path, inbox_dir=config.socket_path.parent / "inbox")
        return {"ok": True, "removed": removed, "path": str(path)}

    raise BridgeError("operation_not_supported")


def _requires_mutation_lock(request: dict[str, Any]) -> bool:
    operation = request.get("operation")
    return (operation in {"send", "send_photo", "download"} and request.get("mode") == "apply") or operation in {
        "discard_download",
        "monitor_mark",
    }


def _requires_monitor_voice_lock(request: dict[str, Any]) -> bool:
    # Staging reserves its event before the first await, so different voice
    # downloads need not queue behind one another.  Serializing the full
    # download here would let a second local caller outlive its RPC deadline.
    return request.get("operation") == "monitor_voice_discard"


async def _serve_client(
    client: Any,
    config: TelegramConfig,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    inbound_monitor: InboundMonitor | None = None,
) -> None:
    response: dict[str, Any]
    try:
        raw = await reader.readline()
        if not raw or len(raw) > MAX_REQUEST_BYTES:
            raise BridgeError("request_size_invalid")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise BridgeError("request_invalid")
        if _requires_mutation_lock(request):
            async with _MUTATION_LOCK:
                response = await _handle_operation(client, config, request, inbound_monitor=inbound_monitor)
        elif _requires_monitor_voice_lock(request):
            async with _MONITOR_VOICE_LOCK:
                response = await _handle_operation(client, config, request, inbound_monitor=inbound_monitor)
        else:
            response = await _handle_operation(client, config, request, inbound_monitor=inbound_monitor)
    except BridgeError as exc:
        response = {"ok": False, "error": exc.code}
    except (json.JSONDecodeError, UnicodeError, ValueError):
        response = {"ok": False, "error": "request_invalid"}
    except Exception:  # noqa: BLE001 - RPC boundary must not leak Telegram or transport details.
        response = {"ok": False, "error": "telegram_operation_failed"}
    writer.write(_canonical_json(response) + b"\n")
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def _capture_incoming_event(monitor: InboundMonitor, event: Any) -> None:
    try:
        event_id = monitor.record(event)
    except Exception:  # noqa: BLE001 - one malformed provider update must not stop the bridge.
        return
    wake_socket = os.environ.get(WORK_WAKE_ENVIRONMENT)
    if event_id is not None and wake_socket:
        try:
            from autostop_manager.telegram_wake import local_request

            result = await asyncio.wait_for(
                local_request({"operation": "event", "event_id": event_id}, Path(wake_socket)), 3
            )
            if not result.get("ok"):
                print("work_telegram_wake_delivery_failed=true", file=sys.stderr, flush=True)
        except (OSError, ValueError, TimeoutError):
            print("work_telegram_wake_delivery_failed=true", file=sys.stderr, flush=True)


async def run_daemon(config: TelegramConfig, *, monitor_incoming: bool = False) -> None:
    TelegramClient, _, _ = _load_telethon()
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.socket_path.parent.mkdir(parents=True, exist_ok=True)
    outbox_dir = config.socket_path.parent / "outbox"
    outbox_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(outbox_dir, 0o700)
    inbox_dir = config.socket_path.parent / "inbox"
    inbox_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(inbox_dir, 0o700)
    config.socket_path.unlink(missing_ok=True)
    client = TelegramClient(str(config.session_path), config.api_id, config.api_hash, catch_up=False)
    inbound_monitor = InboundMonitor() if monitor_incoming else None
    if inbound_monitor is not None:
        # Telethon otherwise persists entity names, phones and usernames while processing updates.
        client.session.save_entities = False
        events = _load_telegram_events()

        async def capture(event: Any) -> None:
            await _capture_incoming_event(inbound_monitor, event)

        client.add_event_handler(capture, events.NewMessage(incoming=True))
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise BridgeError("account_not_authorized")
    server = await asyncio.start_unix_server(
        lambda reader, writer: _serve_client(client, config, reader, writer, inbound_monitor=inbound_monitor),
        path=str(config.socket_path),
    )
    os.chmod(config.socket_path, 0o600)
    try:
        async with server:
            await server.serve_forever()
    finally:
        await client.disconnect()
        config.socket_path.unlink(missing_ok=True)


async def run_code_login(
    config: TelegramConfig,
    *,
    read_secret: Callable[[str], str] = _read_hidden_terminal_value,
) -> dict[str, Any]:
    """Authorize one new account through a local hidden terminal prompt.

    The phone, login code, and optional cloud password remain in process memory
    only. This deliberately takes no secret-bearing CLI arguments or files.
    """

    TelegramClient, utils, SessionPasswordNeededError = _load_telethon()
    previous_umask = os.umask(0o077)
    client: Any | None = None
    phone = ""
    code = ""
    password = ""
    try:
        client = TelegramClient(str(config.session_path), config.api_id, config.api_hash)
        await client.connect()
        if await client.is_user_authorized():
            raise BridgeError("account_already_authorized")

        raw_phone = read_secret("Telegram phone: ")
        normalized_phone = utils.parse_phone(raw_phone)
        if not normalized_phone:
            raise BridgeError("phone_invalid")
        phone = str(normalized_phone)
        await client.send_code_request(phone)

        code = read_secret("Telegram login code: ").strip()
        if not code:
            raise BridgeError("login_code_invalid")
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            password = read_secret("Telegram cloud password: ")
            try:
                await client.sign_in(password=password)
            except Exception as exc:  # never leak provider errors or secret-bearing context.
                raise BridgeError("two_factor_password_invalid") from exc

        if not await client.get_me():
            raise BridgeError("account_not_authorized")
        return {"ok": True, "authorized": True, "already_authorized": False, "verified": True}
    except BridgeError:
        raise
    except Exception as exc:  # never leak provider errors or secret-bearing context.
        raise BridgeError("code_login_failed") from exc
    finally:
        phone = ""
        code = ""
        password = ""
        try:
            if client is not None:
                await client.disconnect()
        finally:
            os.umask(previous_umask)


def send_local_request(
    socket_path: Path,
    request: dict[str, Any],
    *,
    timeout_seconds: float = DEFAULT_LOCAL_REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if isinstance(timeout_seconds, bool):
        raise BridgeError("bridge_timeout_invalid")
    try:
        timeout_value = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise BridgeError("bridge_timeout_invalid") from exc
    if not 1 <= timeout_value <= MAX_LOCAL_REQUEST_TIMEOUT_SECONDS:
        raise BridgeError("bridge_timeout_invalid")
    encoded = _canonical_json(request) + b"\n"
    if len(encoded) > MAX_REQUEST_BYTES:
        raise BridgeError("request_size_invalid")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout_value)
            connection.connect(str(socket_path))
            connection.sendall(encoded)
            chunks = bytearray()
            while b"\n" not in chunks and len(chunks) <= MAX_REQUEST_BYTES:
                part = connection.recv(65536)
                if not part:
                    break
                chunks.extend(part)
    except OSError as exc:
        raise BridgeError("bridge_unavailable") from exc
    if not chunks or len(chunks) > MAX_REQUEST_BYTES:
        raise BridgeError("response_invalid")
    try:
        response = json.loads(bytes(chunks).split(b"\n", 1)[0])
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeError("response_invalid") from exc
    if not isinstance(response, dict):
        raise BridgeError("response_invalid")
    return response


def _config_from_args(args: Any) -> TelegramConfig:
    return _config_for_account(args.account)


def _socket_from_args(args: Any) -> Path:
    return ACCOUNT_PATHS[args.account].socket_path


def _monitor_incoming_from_args(args: Any) -> bool:
    """Keep work-message intake explicitly opt-in, including on service restart."""

    return args.account == "work" and (
        bool(getattr(args, "monitor_incoming", False)) or os.environ.get(WORK_MONITOR_ENVIRONMENT, "").strip() == "1"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autostop-telegram")
    parser.add_argument("--account", choices=tuple(ACCOUNT_PATHS), required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    daemon = subparsers.add_parser("daemon")
    daemon.add_argument("--monitor-incoming", action="store_true")
    subparsers.add_parser("code-login")
    subparsers.add_parser("probe")
    subparsers.add_parser("status")
    subparsers.add_parser("monitor-status")
    subparsers.add_parser("monitor-events")
    monitor_read = subparsers.add_parser("monitor-read")
    monitor_read.add_argument("--event-id", required=True)
    monitor_context = subparsers.add_parser("monitor-context")
    monitor_context.add_argument("--event-id", required=True)
    monitor_context.add_argument("--limit", type=int, default=MAX_INBOUND_MONITOR_CONTEXT_MESSAGES)
    monitor_mark = subparsers.add_parser("monitor-mark")
    monitor_mark.add_argument("--event-id", required=True)
    monitor_mark.add_argument("--disposition", choices=[INBOUND_EVENT_NO_REPLY_NEEDED], required=True)
    dialogs = subparsers.add_parser("dialogs")
    dialogs.add_argument("--limit", type=int, default=20)
    search = subparsers.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=20)
    resolve_phone = subparsers.add_parser("resolve-phone")
    resolve_phone.add_argument("--phone", required=True)
    read = subparsers.add_parser("read")
    read.add_argument("--peer", required=True)
    read.add_argument("--limit", type=int, default=20)
    send = subparsers.add_parser("send")
    send.add_argument("--peer", required=True)
    send.add_argument("--text", required=True)
    send.add_argument("--mode", choices=["dry_run", "apply"], default="dry_run")
    send.add_argument("--contract-token", default="")
    send.add_argument("--idempotency-key", default="")
    send.add_argument("--reply-to-message-id", type=int, default=0)
    send_photo = subparsers.add_parser("send-photo")
    send_photo.add_argument("--peer", required=True)
    send_photo.add_argument("--file", required=True, type=Path)
    send_photo.add_argument("--caption", required=True)
    send_photo.add_argument("--mode", choices=["dry_run", "apply"], default="dry_run")
    send_photo.add_argument("--contract-token", default="")
    send_photo.add_argument("--idempotency-key", default="")
    download = subparsers.add_parser("download")
    download.add_argument("--peer", required=True)
    download.add_argument("--message-id", required=True, type=int)
    download.add_argument("--mode", choices=["dry_run", "apply"], default="dry_run")
    download.add_argument("--contract-token", default="")
    download.add_argument("--idempotency-key", default="")
    discard_download = subparsers.add_parser("discard-download")
    discard_download.add_argument("--file", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "daemon":
            config = _config_from_args(args)
            asyncio.run(run_daemon(config, monitor_incoming=_monitor_incoming_from_args(args)))
            return 0
        if args.command == "code-login":
            payload = asyncio.run(run_code_login(_config_from_args(args)))
        else:
            request: dict[str, Any] = {"operation": args.command}
            if args.command == "monitor-status":
                request["operation"] = "monitor_status"
            elif args.command == "monitor-events":
                request["operation"] = "monitor_events"
            elif args.command == "monitor-read":
                request.update({"operation": "monitor_read", "event_id": args.event_id})
            elif args.command == "monitor-context":
                request.update({"operation": "monitor_context", "event_id": args.event_id, "limit": args.limit})
            elif args.command == "monitor-mark":
                request.update(
                    {
                        "operation": "monitor_mark",
                        "event_id": args.event_id,
                        "disposition": args.disposition,
                    }
                )
            elif args.command == "dialogs":
                request["limit"] = args.limit
            elif args.command == "search":
                request.update({"query": args.query, "limit": args.limit})
            elif args.command == "resolve-phone":
                request.update({"operation": "resolve_phone", "phone": args.phone})
            elif args.command == "read":
                request.update({"peer": args.peer, "limit": args.limit})
            elif args.command == "send":
                request.update(
                    {
                        "peer": args.peer,
                        "text": args.text,
                        "mode": args.mode,
                        "contract_token": args.contract_token,
                        "idempotency_key": args.idempotency_key,
                        "reply_to_message_id": args.reply_to_message_id,
                    }
                )
            elif args.command == "send-photo":
                request.update(
                    {
                        "operation": "send_photo",
                        "peer": args.peer,
                        "photo": str(args.file),
                        "caption": args.caption,
                        "mode": args.mode,
                        "contract_token": args.contract_token,
                        "idempotency_key": args.idempotency_key,
                    }
                )
            elif args.command == "download":
                request.update(
                    {
                        "peer": args.peer,
                        "message_id": args.message_id,
                        "mode": args.mode,
                        "contract_token": args.contract_token,
                        "idempotency_key": args.idempotency_key,
                    }
                )
            elif args.command == "discard-download":
                request.update({"operation": "discard_download", "path": str(args.file)})
            payload = send_local_request(_socket_from_args(args), request)
    except BridgeError as exc:
        payload = {"ok": False, "error": exc.code}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    succeeded = payload.get("ok") is True and (args.command != "probe" or payload.get("authorized") is True)
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
