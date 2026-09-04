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
from collections.abc import Callable
from dataclasses import dataclass, replace
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
STORE_QUOTE_BINDING_TTL_SECONDS = 7 * 24 * 60 * 60
STORE_QUOTE_RECEIPT_TTL_SECONDS = 15 * 60
STORE_QUOTE_MAX_STATE_ITEMS = 2_000
STORE_QUOTE_TRANSPORT_STATE_FILE = "store-quote-transport.json"
STORE_QUOTE_IDENTITY_PROMPT_TEXT = "Привет! Вы оставляли заявку на запчасти в AutoStop?"
TRANSCRIPTION_MODEL_NAME = "faster-whisper-small"
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
MAX_VIDEO_DURATION_SECONDS = 2 * 60
PHONE_RESOLVE_INTERVAL_SECONDS = 3.0
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
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_STORE_QUOTE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,159}$")
_STORE_QUOTE_RECEIPT_PATTERN = re.compile(r"^[A-Za-z0-9_-]{24,256}$")
_STORE_QUOTE_MESSAGE_KINDS = frozenset(
    {
        "identity_prompt",
        "clarification",
        "offer",
        "selection_confirmation",
        "addition_clarification",
        "payment_instruction",
    }
)
_STORE_QUOTE_REPLY_CATEGORIES = frozenset({"clarification", "addition", "selection", "consent", "decline", "ambiguous"})
_STORE_QUOTE_IDENTITY_CATEGORIES = frozenset({"confirmed", "declined", "ambiguous"})
_STORE_QUOTE_DECLINE_PATTERN = re.compile(
    r"\b(?:не\s+(?:надо|нужен|нужна|нужно|буду|беру)|отмен(?:а|яем|яй(?:те)?|ить)|"
    r"отказ(?:ываюсь|ался|алась)?)\b",
    re.IGNORECASE,
)
_STORE_QUOTE_ADDITION_PATTERN = re.compile(
    r"\b(?:ещ[её]\s+(?:нуж(?:ен|на|но)|добав)|добав(?:ь|ьте|ить)|заодно|плюс)\b",
    re.IGNORECASE,
)
_STORE_QUOTE_QUESTION_PATTERN = re.compile(
    r"^(?:а\s+)?(?:какой|какая|какие|когда|сколько|почему|зачем|где|как|можно|точно|"
    r"подойд[её]т|есть\s+ли|будет\s+ли|а\s+если)\b",
    re.IGNORECASE,
)
_STORE_QUOTE_CONSENT_PATTERN = re.compile(
    r"^(?:да[,.! ]+)?(?:бер(?:у|ем)|оформ(?:ляем|ляй|ляйте)|заказ(?:ываем|ывай|ывайте)|"
    r"подтверждаю|соглас(?:ен|на))(?:\s+(?:этот|его|её|вариант))?[.! ]*$",
    re.IGNORECASE,
)
_STORE_QUOTE_SELECTION_PATTERN = re.compile(
    r"\b(?:перв(?:ый|ая|ое)|втор(?:ой|ая|ое)|трет(?:ий|ья|ье)|оригинал|аналог|вариант)\b",
    re.IGNORECASE,
)
_STORE_QUOTE_IDENTITY_DECLINE_PATTERN = re.compile(
    r"\b(?:не\s+(?:я\s+)?оставлял(?:а)?|оставлял(?:а)?\s+не\s+я|это\s+не\s+я|не\s+моя\s+заявка)\b",
    re.IGNORECASE,
)
_STORE_QUOTE_IDENTITY_CONFIRM_PATTERN = re.compile(
    r"\b(?:я\s+оставлял(?:а)?|это\s+я|моя\s+заявка|вс[её]\s+верно)\b",
    re.IGNORECASE,
)
_STORE_QUOTE_IDENTITY_CONTEXT_PATTERN = re.compile(r"\b(?:оставлял(?:а)?|верно)\b", re.IGNORECASE)
_STORE_QUOTE_IDENTITY_UNCERTAIN_PATTERN = re.compile(
    r"\b(?:не\s+уверен(?:а)?|не\s+помню|кажется|вроде|возможно|наверное|может\s+быть)\b",
    re.IGNORECASE,
)
_STORE_QUOTE_SENTENCE_PATTERN = re.compile(r"[.!?]+")
_STORE_QUOTE_SPACE_PATTERN = re.compile(r"\s+")
_MUTATION_LOCK = asyncio.Lock()
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


def account_inbox_dir(account: str) -> Path:
    """Return the only private inbox accepted for a named account."""

    paths = ACCOUNT_PATHS.get(account)
    if paths is None:
        raise BridgeError("account_invalid")
    return paths.socket_path.parent / "inbox"


def account_outbox_dir(account: str) -> Path:
    """Return the only private outbox accepted for a named account."""

    paths = ACCOUNT_PATHS.get(account)
    if paths is None:
        raise BridgeError("account_invalid")
    return paths.socket_path.parent / "outbox"


def account_model_dir(account: str) -> Path:
    """Return the account-owned local speech model path."""

    paths = ACCOUNT_PATHS.get(account)
    if paths is None:
        raise BridgeError("account_invalid")
    if account == "work":
        return WORK_TRANSCRIPTION_MODEL_DIR
    return paths.state_dir / "models" / TRANSCRIPTION_MODEL_NAME


DEFAULT_OUTBOX_DIR = account_outbox_dir("personal")
DEFAULT_INBOX_DIR = account_inbox_dir("personal")


@dataclass(frozen=True)
class TelegramConfig:
    api_id: int
    api_hash: str
    credentials_path: Path = DEFAULT_CREDENTIALS_PATH
    session_path: Path = DEFAULT_SESSION_PATH
    state_dir: Path = DEFAULT_STATE_DIR
    socket_path: Path = DEFAULT_SOCKET_PATH
    # The daemon receives a config produced by the fixed account selector.
    # Keep the selector on the config so the Store-quote RPC can fail closed
    # instead of inferring an account from a peer or a filesystem path.
    account: str = "personal"

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
            credentials_path=credentials_path,
            session_path=session_path,
            state_dir=state_dir,
            socket_path=socket_path,
        )


def _config_for_account(account: str) -> TelegramConfig:
    paths = ACCOUNT_PATHS.get(account)
    if paths is None:
        raise BridgeError("account_invalid")
    return replace(
        TelegramConfig.load(
            paths.credentials_path,
            session_path=paths.session_path,
            state_dir=paths.state_dir,
            socket_path=paths.socket_path,
        ),
        account=account,
    )


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


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
    encoded = base64.urlsafe_b64encode(_canonical_json(payload)).rstrip(b"=")
    signature = hmac.new(secret, encoded, hashlib.sha256).digest()
    return f"{encoded.decode('ascii')}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii')}"


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
    try:
        encoded_raw, signature_raw = token.split(".", 1)
        encoded = encoded_raw.encode("ascii")
        signature = base64.urlsafe_b64decode(signature_raw + "=" * (-len(signature_raw) % 4))
        expected = hmac.new(secret, encoded, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise BridgeError("contract_invalid")
        decoded = base64.urlsafe_b64decode(encoded_raw + "=" * (-len(encoded_raw) % 4))
        payload = json.loads(decoded)
    except BridgeError:
        raise
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeError("contract_invalid") from exc

    current_time = int(time.time()) if now is None else int(now)
    if not isinstance(payload, dict):
        raise BridgeError("contract_invalid")
    if payload.get("peer_id") != int(peer_id):
        raise BridgeError("contract_target_changed")
    if payload.get("text_sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest():
        raise BridgeError("contract_text_changed")
    if payload.get("reply_to_message_id", 0) != int(reply_to_message_id):
        raise BridgeError("contract_reply_target_changed")
    if payload.get("reply_message_sha256", "") != reply_message_sha256:
        raise BridgeError("contract_reply_source_changed")
    issued_at = payload.get("issued_at")
    if not isinstance(issued_at, int) or issued_at > current_time + 60:
        raise BridgeError("contract_invalid")
    if current_time - issued_at > CONTRACT_TTL_SECONDS:
        raise BridgeError("contract_expired")
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
    encoded = base64.urlsafe_b64encode(_canonical_json(payload)).rstrip(b"=")
    signature = hmac.new(secret, encoded, hashlib.sha256).digest()
    return f"{encoded.decode('ascii')}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii')}"


def verify_photo_contract(
    token: str,
    secret: bytes,
    *,
    peer_id: int,
    caption: str,
    photo_sha256: str,
    now: int | None = None,
) -> dict[str, Any]:
    try:
        encoded_raw, signature_raw = token.split(".", 1)
        encoded = encoded_raw.encode("ascii")
        signature = base64.urlsafe_b64decode(signature_raw + "=" * (-len(signature_raw) % 4))
        expected = hmac.new(secret, encoded, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise BridgeError("contract_invalid")
        decoded = base64.urlsafe_b64decode(encoded_raw + "=" * (-len(encoded_raw) % 4))
        payload = json.loads(decoded)
    except BridgeError:
        raise
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeError("contract_invalid") from exc

    current_time = int(time.time()) if now is None else int(now)
    if not isinstance(payload, dict) or payload.get("type") != "photo":
        raise BridgeError("contract_invalid")
    if payload.get("peer_id") != int(peer_id):
        raise BridgeError("contract_target_changed")
    if payload.get("caption_sha256") != hashlib.sha256(caption.encode("utf-8")).hexdigest():
        raise BridgeError("contract_text_changed")
    if payload.get("photo_sha256") != photo_sha256:
        raise BridgeError("contract_photo_changed")
    issued_at = payload.get("issued_at")
    if not isinstance(issued_at, int) or issued_at > current_time + 60:
        raise BridgeError("contract_invalid")
    if current_time - issued_at > CONTRACT_TTL_SECONDS:
        raise BridgeError("contract_expired")
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
    encoded = base64.urlsafe_b64encode(_canonical_json(payload)).rstrip(b"=")
    signature = hmac.new(secret, encoded, hashlib.sha256).digest()
    return f"{encoded.decode('ascii')}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii')}"


def verify_download_contract(
    token: str,
    secret: bytes,
    *,
    peer_id: int,
    message_id: int,
    media_fingerprint: str,
    now: int | None = None,
) -> dict[str, Any]:
    try:
        encoded_raw, signature_raw = token.split(".", 1)
        encoded = encoded_raw.encode("ascii")
        signature = base64.urlsafe_b64decode(signature_raw + "=" * (-len(signature_raw) % 4))
        expected = hmac.new(secret, encoded, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise BridgeError("contract_invalid")
        decoded = base64.urlsafe_b64decode(encoded_raw + "=" * (-len(encoded_raw) % 4))
        payload = json.loads(decoded)
    except BridgeError:
        raise
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeError("contract_invalid") from exc

    current_time = int(time.time()) if now is None else int(now)
    if not isinstance(payload, dict) or payload.get("type") != "download":
        raise BridgeError("contract_invalid")
    if payload.get("peer_id") != int(peer_id):
        raise BridgeError("contract_target_changed")
    if payload.get("message_id") != int(message_id):
        raise BridgeError("contract_message_changed")
    if payload.get("media_fingerprint") != media_fingerprint:
        raise BridgeError("contract_media_changed")
    issued_at = payload.get("issued_at")
    if not isinstance(issued_at, int) or issued_at > current_time + 60:
        raise BridgeError("contract_invalid")
    if current_time - issued_at > CONTRACT_TTL_SECONDS:
        raise BridgeError("contract_expired")
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
    if mode not in {"dry_run", "apply"}:
        raise BridgeError("mode_invalid")
    if mode == "apply" and not idempotency_key:
        raise BridgeError("idempotency_key_required")
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
    if mode not in {"dry_run", "apply"}:
        raise BridgeError("mode_invalid")
    if mode == "apply" and not idempotency_key:
        raise BridgeError("idempotency_key_required")
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
    if mode not in {"dry_run", "apply"}:
        raise BridgeError("mode_invalid")
    if mode == "apply" and not idempotency_key:
        raise BridgeError("idempotency_key_required")
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


def _load_telegram_functions() -> tuple[Any, Any]:
    try:
        from telethon import functions, utils
    except ImportError as exc:
        raise BridgeError("telethon_not_installed") from exc
    return functions, utils


def _load_qrcode() -> Any:
    try:
        import qrcode
    except ImportError as exc:
        raise BridgeError("qrcode_not_installed") from exc
    return qrcode


def _read_one_time_password(path: Path) -> str:
    try:
        file_fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise BridgeError("two_factor_password_unavailable") from exc
    consumed = False
    try:
        file_stat = os.fstat(file_fd)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.geteuid()
            or stat.S_IMODE(file_stat.st_mode) != 0o600
        ):
            raise BridgeError("two_factor_password_permissions_invalid")
        if file_stat.st_size > 1024:
            raise BridgeError("two_factor_password_invalid")
        consumed = True
        raw = os.read(file_fd, 1025)
        if len(raw) > 1024:
            raise BridgeError("two_factor_password_invalid")
        password = raw.decode("utf-8").rstrip("\r\n")
    except (OSError, UnicodeError) as exc:
        raise BridgeError("two_factor_password_unreadable") from exc
    finally:
        os.close(file_fd)
        if consumed:
            path.unlink(missing_ok=True)
    if not password:
        raise BridgeError("two_factor_password_invalid")
    return password


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


def _store_quote_state_path(config: TelegramConfig) -> Path:
    return config.state_dir / STORE_QUOTE_TRANSPORT_STATE_FILE


def _require_store_quote_work_account(config: TelegramConfig) -> None:
    """The typed Store quote RPC is deliberately unavailable to personal Telegram."""

    if config.account != "work":
        raise BridgeError("store_quote_work_account_required")


def _store_quote_sha256(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _store_quote_hash(value: Any, *, code: str = "store_quote_binding_invalid") -> str:
    normalized = str(value or "").strip().casefold()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise BridgeError(code)
    return normalized


def _store_quote_identifier(value: Any, *, code: str) -> str:
    normalized = str(value or "").strip()
    if _STORE_QUOTE_IDENTIFIER_PATTERN.fullmatch(normalized) is None:
        raise BridgeError(code)
    return normalized


def _store_quote_message_text(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_MESSAGE_CHARS:
        raise BridgeError("store_quote_message_invalid")
    if any(marker in value for marker in ("\r", "\n", "\x00")):
        raise BridgeError("store_quote_message_invalid")
    if len(_STORE_QUOTE_SENTENCE_PATTERN.findall(value)) not in range(1, 4):
        raise BridgeError("store_quote_message_style_invalid")
    if value.count("?") != 1 or not value.endswith("?"):
        raise BridgeError("store_quote_message_style_invalid")
    return value


def _store_quote_delivery_binding(projection: dict[str, str]) -> str:
    """Match the conductor's canonical opaque delivery binding exactly."""

    return hashlib.sha256(
        _canonical_json(
            {
                "quote_ref_sha256": projection["quote_ref_sha256"],
                "revision_sha256": projection["revision_sha256"],
                "published_snapshot_hash": projection["published_snapshot_hash"],
                "context_hash": projection["context_hash"],
                "kind": projection["message_kind"],
                "message_sha256": projection["message_sha256"],
            }
        )
    ).hexdigest()


def _store_quote_route_binding(
    quote_ref_sha256: str,
    revision_sha256: str,
    published_snapshot_hash: str,
    context_hash: str,
) -> str:
    """Stable route binding for one exact opaque published quote context."""

    return _store_quote_sha256(
        f"store-quote-work-route-v2\0{quote_ref_sha256}\0{revision_sha256}\0{published_snapshot_hash}\0{context_hash}"
    )


def _store_quote_projection(request: dict[str, Any], *, require_text: bool) -> dict[str, str]:
    """Validate the hash-only Store quote binding passed across the local RPC."""

    kind = str(request.get("message_kind") or "").strip().casefold()
    if kind not in _STORE_QUOTE_MESSAGE_KINDS:
        raise BridgeError("store_quote_message_kind_invalid")
    projection = {
        "quote_ref_sha256": _store_quote_hash(request.get("quote_ref_sha256")),
        "revision_sha256": _store_quote_hash(request.get("revision_sha256")),
        "published_snapshot_hash": _store_quote_hash(request.get("published_snapshot_hash")),
        "context_hash": _store_quote_hash(request.get("context_hash")),
        "delivery_binding_sha256": _store_quote_hash(request.get("delivery_binding_sha256")),
        "route_binding_sha256": _store_quote_hash(request.get("route_binding_sha256")),
        "message_sha256": _store_quote_hash(request.get("message_sha256")),
        "message_kind": kind,
    }
    if projection["delivery_binding_sha256"] != _store_quote_delivery_binding(projection):
        raise BridgeError("store_quote_delivery_binding_invalid")
    if projection["route_binding_sha256"] != _store_quote_route_binding(
        projection["quote_ref_sha256"],
        projection["revision_sha256"],
        projection["published_snapshot_hash"],
        projection["context_hash"],
    ):
        raise BridgeError("store_quote_route_binding_invalid")
    if require_text:
        text = _store_quote_message_text(request.get("text"))
        if not hmac.compare_digest(_store_quote_sha256(text), projection["message_sha256"]):
            raise BridgeError("store_quote_message_binding_invalid")
    return projection


def _store_quote_projection_matches(record: dict[str, Any], projection: dict[str, str]) -> bool:
    return all(str(record.get(key) or "") == value for key, value in projection.items())


def _store_quote_is_identity_prompt(projection: dict[str, str]) -> bool:
    """Recognize only the literal neutral identity prompt by kind and hash."""

    return projection.get("message_kind") == "identity_prompt" and hmac.compare_digest(
        str(projection.get("message_sha256") or ""),
        _store_quote_sha256(STORE_QUOTE_IDENTITY_PROMPT_TEXT),
    )


def _store_quote_summary(projection: dict[str, str], **extra: Any) -> dict[str, Any]:
    """Return the only bridge projection which may leave the work account."""

    return {
        "quote_ref_sha256": projection["quote_ref_sha256"],
        "revision_sha256": projection["revision_sha256"],
        "message_sha256": projection["message_sha256"],
        "context_hash": projection["context_hash"],
        "published_snapshot_hash": projection["published_snapshot_hash"],
        "delivery_binding_sha256": projection["delivery_binding_sha256"],
        "route_binding_sha256": projection["route_binding_sha256"],
        **extra,
    }


def _load_store_quote_transport_state(config: TelegramConfig) -> dict[str, dict[str, Any]]:
    """Load private bridge state; it may contain peer/message IDs, never text."""

    state = _load_idempotency(_store_quote_state_path(config))
    if not state:
        return {"routes": {}, "bindings": {}, "deliveries": {}, "dry_runs": {}, "receipts": {}}
    # Earlier typed bridge versions kept exact-delivery bindings only.  Those
    # records did not have verified identity proof, so preserve them for safe
    # cleanup but never infer a confirmed route from them.
    if set(state) == {"bindings", "deliveries", "dry_runs", "receipts"}:
        state = {"routes": {}, **state}
    if set(state) != {"routes", "bindings", "deliveries", "dry_runs", "receipts"}:
        raise BridgeError("store_quote_state_invalid")
    if any(not isinstance(value, dict) or len(value) > STORE_QUOTE_MAX_STATE_ITEMS for value in state.values()):
        raise BridgeError("store_quote_state_invalid")
    return state


def _store_quote_prune_state(state: dict[str, dict[str, Any]], *, now: int) -> None:
    """Keep the local work-account capability cache bounded and fail closed when stale.

    A receipt's *capability* is short lived, but its opaque tombstone must live
    as long as the delivery binding.  Otherwise an already-consumed (or simply
    expired) direct reply could be scanned again and minted into a fresh
    receipt.  The bridge stores no text in that tombstone.
    """

    binding_floor = now - STORE_QUOTE_BINDING_TTL_SECONDS
    receipt_floor = now - STORE_QUOTE_RECEIPT_TTL_SECONDS
    for key, value in list(state["bindings"].items()):
        if not isinstance(value, dict) or int(value.get("created_at") or 0) < binding_floor:
            state["bindings"].pop(key, None)
    for key, value in list(state["routes"].items()):
        if not isinstance(value, dict) or int(value.get("created_at") or 0) < binding_floor:
            state["routes"].pop(key, None)
    for key, value in list(state["deliveries"].items()):
        if not isinstance(value, dict) or int(value.get("created_at") or 0) < binding_floor:
            state["deliveries"].pop(key, None)
    for key, value in list(state["dry_runs"].items()):
        if not isinstance(value, dict) or int(value.get("issued_at") or 0) < receipt_floor:
            state["dry_runs"].pop(key, None)
    for key, value in list(state["receipts"].items()):
        if not isinstance(value, dict) or int(value.get("issued_at") or 0) < binding_floor:
            state["receipts"].pop(key, None)


def _save_store_quote_transport_state(config: TelegramConfig, state: dict[str, dict[str, Any]]) -> None:
    _save_idempotency(_store_quote_state_path(config), state)


def _store_quote_receipt_expired(record: dict[str, Any], *, now: int) -> bool:
    issued_at = record.get("issued_at")
    if isinstance(issued_at, bool) or not isinstance(issued_at, int) or issued_at <= 0:
        raise BridgeError("store_quote_inbound_receipt_invalid")
    return issued_at < now - STORE_QUOTE_RECEIPT_TTL_SECONDS


def _store_quote_delivery_ref(projection: dict[str, str], *, message_id: int, nonce: str) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "kind": "store_quote_work_delivery_v1",
                "delivery_binding_sha256": projection["delivery_binding_sha256"],
                "message_id": int(message_id),
                "nonce": nonce,
            }
        )
    ).hexdigest()


def _store_quote_incoming_ref(*, delivery_ref_sha256: str, message_id: int, text_sha256: str) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "kind": "store_quote_work_incoming_v1",
                "delivery_ref_sha256": delivery_ref_sha256,
                "message_id": int(message_id),
                "reply_text_sha256": text_sha256,
            }
        )
    ).hexdigest()


def _store_quote_inbound_binding(
    projection: dict[str, str],
    *,
    delivery_ref_sha256: str,
    reply_classification: str,
    reply_text_sha256: str,
    incoming_ref_sha256: str,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "quote_ref_sha256": projection["quote_ref_sha256"],
                "revision_sha256": projection["revision_sha256"],
                "published_snapshot_hash": projection["published_snapshot_hash"],
                "telegram_context_hash": projection["context_hash"],
                "delivery_binding_sha256": projection["delivery_binding_sha256"],
                "delivery_ref_sha256": delivery_ref_sha256,
                "reply_classification": reply_classification,
                "reply_text_sha256": reply_text_sha256,
                "incoming_ref_sha256": incoming_ref_sha256,
            }
        )
    ).hexdigest()


def _classify_store_quote_reply(value: str) -> str:
    """Conservative, transient-only classifier for one direct reply to a quote turn."""

    if not isinstance(value, str) or not value or len(value) > MAX_MESSAGE_CHARS or "\x00" in value:
        raise BridgeError("store_quote_reply_invalid")
    normalized = _STORE_QUOTE_SPACE_PATTERN.sub(" ", value).strip().casefold()
    if not normalized:
        raise BridgeError("store_quote_reply_invalid")
    if _STORE_QUOTE_DECLINE_PATTERN.search(normalized):
        return "decline"
    if _STORE_QUOTE_ADDITION_PATTERN.search(normalized):
        return "addition"
    if "?" in normalized or _STORE_QUOTE_QUESTION_PATTERN.search(normalized):
        return "clarification"
    if _STORE_QUOTE_CONSENT_PATTERN.fullmatch(normalized):
        return "consent"
    if _STORE_QUOTE_SELECTION_PATTERN.search(normalized):
        return "selection"
    return "ambiguous"


def _classify_store_quote_identity_reply(value: str) -> str:
    """Recognize a clear natural reply to the neutral identity prompt."""

    if not isinstance(value, str) or not value or len(value) > MAX_MESSAGE_CHARS or "\x00" in value:
        raise BridgeError("store_quote_identity_reply_invalid")
    normalized = _STORE_QUOTE_SPACE_PATTERN.sub(" ", value).strip().casefold()
    if not normalized or "?" in normalized or _STORE_QUOTE_IDENTITY_UNCERTAIN_PATTERN.search(normalized):
        return "ambiguous"
    bare = normalized.rstrip(".! ")
    confirmed = (
        bare in {"да", "оставлял", "оставляла", "верно"}
        or _STORE_QUOTE_IDENTITY_CONFIRM_PATTERN.search(normalized) is not None
        or (
            re.search(r"\bда\b", normalized) is not None
            and _STORE_QUOTE_IDENTITY_CONTEXT_PATTERN.search(normalized) is not None
        )
    )
    declined = _STORE_QUOTE_IDENTITY_DECLINE_PATTERN.search(normalized) is not None
    leading_no = re.match(r"^нет(?:\b|[,.!])", normalized) is not None
    if confirmed and (declined or leading_no):
        return "ambiguous"
    if declined or leading_no:
        return "declined"
    if confirmed:
        return "confirmed"
    return "ambiguous"


def _store_quote_identity_inbound_binding(
    projection: dict[str, str],
    *,
    delivery_ref_sha256: str,
    identity_classification: str,
    reply_text_sha256: str,
    incoming_ref_sha256: str,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "kind": "store_quote_work_identity_inbound_v1",
                "quote_ref_sha256": projection["quote_ref_sha256"],
                "route_binding_sha256": projection["route_binding_sha256"],
                "delivery_binding_sha256": projection["delivery_binding_sha256"],
                "delivery_ref_sha256": delivery_ref_sha256,
                "identity_classification": identity_classification,
                "reply_text_sha256": reply_text_sha256,
                "incoming_ref_sha256": incoming_ref_sha256,
            }
        )
    ).hexdigest()


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
    if contract["last_message_id"] != last_message_id:
        raise BridgeError("conversation_changed_since_dry_run")

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
        return {
            "ok": True,
            "mode": "apply",
            "replayed": True,
            "target": target,
            "message_id": previous.get("message_id"),
        }

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
    readback = await client.get_messages(entity, ids=int(sent.id))
    if readback is None or str(readback.message or "") != caption or readback.media is None:
        raise BridgeError("send_readback_failed")
    return {
        "ok": True,
        "mode": "apply",
        "replayed": False,
        "target": target,
        "message_id": int(sent.id),
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
) -> dict[str, Any]:
    reply_source = await _load_reply_source(client, entity, reply_to_message_id)
    reply_message_sha256 = reply_source["text_sha256"] if reply_source is not None else ""
    last_message_id = await _last_message_id(client, entity)
    contract_key = _ensure_private_key(config.state_dir / "contract.key")
    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    response_target = target
    if mode == "dry_run":
        return {
            "ok": True,
            "mode": "dry_run",
            "target": response_target,
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
    if contract["last_message_id"] != last_message_id:
        raise BridgeError("conversation_changed_since_dry_run")

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
        return {
            "ok": True,
            "mode": "apply",
            "replayed": True,
            "target": response_target,
            "message_id": previous.get("message_id"),
            "reply_to_message_id": reply_to_message_id or None,
            "reply_verified": bool(reply_to_message_id),
        }

    if reply_to_message_id:
        sent = await client.send_message(entity, text, reply_to=reply_to_message_id)
    else:
        sent = await client.send_message(entity, text)
    idempotency[idempotency_key] = {
        "operation": "send_text",
        "message_id": int(sent.id),
        "peer_id": target["id"],
        "reply_message_sha256": reply_message_sha256,
        "reply_to_message_id": reply_to_message_id,
        "text_sha256": text_sha256,
    }
    _save_idempotency(idempotency_path, idempotency)
    readback = await client.get_messages(entity, ids=int(sent.id))
    if readback is None or str(readback.message or "") != text or _message_reply_to_id(readback) != reply_to_message_id:
        raise BridgeError("send_readback_failed")
    return {
        "ok": True,
        "mode": "apply",
        "replayed": False,
        "target": response_target,
        "message_id": int(sent.id),
        "reply_to_message_id": reply_to_message_id or None,
        "reply_verified": bool(reply_to_message_id),
        "verified": True,
    }


async def _store_quote_bound_target(
    client: Any,
    state: dict[str, dict[str, Any]],
    projection: dict[str, str],
    *,
    allow_pending_identity: bool = False,
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    binding = state["bindings"].get(projection["delivery_binding_sha256"])
    if not isinstance(binding, dict) or not _store_quote_projection_matches(binding, projection):
        raise BridgeError("store_quote_recipient_binding_missing")
    route = state["routes"].get(projection["route_binding_sha256"])
    if not isinstance(route, dict) or (
        route.get("route_binding_sha256") != projection["route_binding_sha256"]
        or route.get("quote_ref_sha256") != projection["quote_ref_sha256"]
    ):
        raise BridgeError("store_quote_route_binding_missing")
    if binding.get("route_binding_sha256") != projection["route_binding_sha256"]:
        raise BridgeError("store_quote_recipient_binding_invalid")
    peer_id = binding.get("peer_id")
    if isinstance(peer_id, bool) or not isinstance(peer_id, int) or peer_id == 0:
        raise BridgeError("store_quote_recipient_binding_invalid")
    if route.get("peer_id") != peer_id:
        raise BridgeError("store_quote_recipient_binding_invalid")
    confirmed = route.get("recipient_confirmed") is True
    pending_identity = (
        allow_pending_identity
        and binding.get("identity_candidate") is True
        and _store_quote_is_identity_prompt(projection)
    )
    if not confirmed and not pending_identity:
        raise BridgeError("store_quote_recipient_not_confirmed")
    entity, target = await _resolve_peer(client, str(peer_id))
    if target.get("kind") != "private" or target.get("id") != peer_id:
        raise BridgeError("store_quote_recipient_binding_changed")
    return entity, target, binding


async def _store_quote_verified_delivery_message(
    client: Any,
    entity: Any,
    *,
    projection: dict[str, str],
    delivery: dict[str, Any],
) -> Any:
    message_id = delivery.get("message_id")
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        raise BridgeError("store_quote_delivery_record_invalid")
    message = await client.get_messages(entity, ids=message_id)
    if (
        message is None
        or int(getattr(message, "id", 0) or 0) != message_id
        or not bool(getattr(message, "out", False))
        or _message_reply_to_id(message) != 0
    ):
        raise BridgeError("store_quote_delivery_readback_failed")
    text = str(getattr(message, "message", None) or "")
    if not hmac.compare_digest(_store_quote_sha256(text), projection["message_sha256"]):
        raise BridgeError("store_quote_delivery_readback_failed")
    return message


def _store_quote_mode(value: Any) -> str:
    mode = str(value or "").strip().casefold()
    if mode not in {"dry_run", "apply"}:
        raise BridgeError("store_quote_mode_invalid")
    return mode


def _store_quote_apply_identifiers(request: dict[str, Any], *, mode: str) -> tuple[str, str]:
    idempotency_key = _store_quote_identifier(
        request.get("idempotency_key"), code="store_quote_idempotency_key_invalid"
    )
    correlation_id = _store_quote_identifier(request.get("correlation_id"), code="store_quote_correlation_id_invalid")
    if mode == "apply" and not idempotency_key:
        raise BridgeError("store_quote_idempotency_key_invalid")
    return idempotency_key, correlation_id


async def _handle_store_quote_bind_identity_candidate(
    client: Any,
    config: TelegramConfig,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Record one pending private candidate for the literal neutral prompt."""

    _require_store_quote_work_account(config)
    projection = _store_quote_projection(request, require_text=True)
    if (
        not _store_quote_is_identity_prompt(projection)
        or str(request.get("text") or "") != STORE_QUOTE_IDENTITY_PROMPT_TEXT
    ):
        raise BridgeError("store_quote_identity_prompt_required")
    peer = str(request.get("peer") or "").strip()
    if not peer:
        raise BridgeError("store_quote_peer_required")
    _entity, target = await _resolve_peer(client, peer)
    if target.get("kind") != "private" or not isinstance(target.get("id"), int) or int(target["id"]) == 0:
        raise BridgeError("store_quote_private_peer_required")
    now = int(time.time())
    state = _load_store_quote_transport_state(config)
    _store_quote_prune_state(state, now=now)
    route_key = projection["route_binding_sha256"]
    route = state["routes"].get(route_key)
    if route is not None:
        if (
            not isinstance(route, dict)
            or route.get("route_binding_sha256") != route_key
            or route.get("quote_ref_sha256") != projection["quote_ref_sha256"]
            or route.get("peer_id") != target["id"]
        ):
            raise BridgeError("store_quote_identity_route_conflict")
        if route.get("recipient_confirmed") is True:
            raise BridgeError("store_quote_identity_already_confirmed")
        if route.get("identity_delivery_binding_sha256") != projection["delivery_binding_sha256"]:
            raise BridgeError("store_quote_identity_pending")
    else:
        state["routes"][route_key] = {
            "route_binding_sha256": route_key,
            "quote_ref_sha256": projection["quote_ref_sha256"],
            "peer_id": int(target["id"]),
            "recipient_confirmed": False,
            "identity_delivery_binding_sha256": projection["delivery_binding_sha256"],
            "created_at": now,
        }
    binding_key = projection["delivery_binding_sha256"]
    existing = state["bindings"].get(binding_key)
    if existing is not None:
        if (
            not isinstance(existing, dict)
            or not _store_quote_projection_matches(existing, projection)
            or existing.get("peer_id") != target["id"]
            or existing.get("identity_candidate") is not True
        ):
            raise BridgeError("store_quote_recipient_binding_conflict")
    else:
        state["bindings"][binding_key] = {
            **projection,
            "peer_id": int(target["id"]),
            "recipient_confirmed": False,
            "identity_candidate": True,
            "created_at": now,
        }
    _save_store_quote_transport_state(config, state)
    return {
        "ok": True,
        "summary": _store_quote_summary(
            projection,
            recipient_bound=True,
            recipient_confirmed=False,
            private_target_confirmed=True,
            unique_target_confirmed=True,
            work_account_confirmed=True,
            identity_pending=True,
        ),
    }


async def _handle_store_quote_bind_recipient(
    client: Any,
    config: TelegramConfig,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Bind a normal quote turn only to an identity-confirmed route."""

    _require_store_quote_work_account(config)
    projection = _store_quote_projection(request, require_text=False)
    if projection["message_kind"] == "identity_prompt":
        raise BridgeError("store_quote_identity_candidate_required")
    peer = str(request.get("peer") or "").strip()
    if not peer:
        raise BridgeError("store_quote_peer_required")
    _entity, target = await _resolve_peer(client, peer)
    if target.get("kind") != "private" or not isinstance(target.get("id"), int) or int(target["id"]) == 0:
        raise BridgeError("store_quote_private_peer_required")
    now = int(time.time())
    state = _load_store_quote_transport_state(config)
    _store_quote_prune_state(state, now=now)
    route = state["routes"].get(projection["route_binding_sha256"])
    if not isinstance(route, dict) or (
        route.get("route_binding_sha256") != projection["route_binding_sha256"]
        or route.get("quote_ref_sha256") != projection["quote_ref_sha256"]
    ):
        raise BridgeError("store_quote_route_binding_missing")
    if route.get("recipient_confirmed") is not True:
        raise BridgeError("store_quote_recipient_not_confirmed")
    if route.get("peer_id") != target["id"]:
        raise BridgeError("store_quote_recipient_binding_changed")
    binding_key = projection["delivery_binding_sha256"]
    existing = state["bindings"].get(binding_key)
    if existing is not None:
        if (
            not isinstance(existing, dict)
            or not _store_quote_projection_matches(existing, projection)
            or existing.get("peer_id") != target["id"]
            or existing.get("identity_candidate") is True
        ):
            raise BridgeError("store_quote_recipient_binding_conflict")
    else:
        state["bindings"][binding_key] = {
            **projection,
            "peer_id": int(target["id"]),
            "recipient_confirmed": True,
            "created_at": now,
        }
    _save_store_quote_transport_state(config, state)
    return {
        "ok": True,
        "summary": _store_quote_summary(
            projection,
            recipient_bound=True,
            recipient_confirmed=True,
            private_target_confirmed=True,
            unique_target_confirmed=True,
            work_account_confirmed=True,
        ),
    }


async def _handle_store_quote_send(
    client: Any,
    config: TelegramConfig,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Send a bound quote message without returning a peer, message id or text."""

    _require_store_quote_work_account(config)
    mode = _store_quote_mode(request.get("mode"))
    projection = _store_quote_projection(request, require_text=True)
    text = str(request["text"])
    idempotency_key, _correlation_id = _store_quote_apply_identifiers(request, mode=mode)
    idempotency_key_sha256 = _store_quote_sha256(idempotency_key)
    now = int(time.time())
    state = _load_store_quote_transport_state(config)
    _store_quote_prune_state(state, now=now)
    entity, _target, binding = await _store_quote_bound_target(
        client,
        state,
        projection,
        allow_pending_identity=_store_quote_is_identity_prompt(projection),
    )
    recipient_confirmed = state["routes"][projection["route_binding_sha256"]].get("recipient_confirmed") is True
    binding_key = projection["delivery_binding_sha256"]
    existing = state["deliveries"].get(binding_key)
    if existing is not None:
        if (
            not isinstance(existing, dict)
            or not _store_quote_projection_matches(existing, projection)
            or (mode == "apply" and existing.get("idempotency_key_sha256") != idempotency_key_sha256)
        ):
            raise BridgeError("store_quote_delivery_idempotency_conflict")
        delivery_ref_sha256 = _store_quote_hash(
            existing.get("delivery_ref_sha256"), code="store_quote_delivery_record_invalid"
        )
        return {
            "ok": True,
            "mode": mode,
            "replayed": True,
            "summary": _store_quote_summary(
                projection,
                **({"dry_run_proof": _store_quote_sha256(secrets.token_bytes(32))} if mode == "dry_run" else {}),
                delivery_ref_sha256=delivery_ref_sha256,
                recipient_confirmed=recipient_confirmed,
                private_target_confirmed=True,
                unique_target_confirmed=True,
                work_account_confirmed=True,
                delivery_confirmed=True,
            ),
        }
    if mode == "dry_run":
        proof = _store_quote_sha256(secrets.token_bytes(32))
        while proof in state["dry_runs"]:
            proof = _store_quote_sha256(secrets.token_bytes(32))
        state["dry_runs"][proof] = {
            **projection,
            "last_message_id": await _last_message_id(client, entity),
            "idempotency_key_sha256": idempotency_key_sha256,
            "issued_at": now,
        }
        _save_store_quote_transport_state(config, state)
        return {
            "ok": True,
            "mode": "dry_run",
            "summary": _store_quote_summary(
                projection,
                dry_run_proof=proof,
                recipient_confirmed=recipient_confirmed,
                private_target_confirmed=True,
                unique_target_confirmed=True,
                work_account_confirmed=True,
            ),
        }

    proof = _store_quote_hash(request.get("dry_run_proof"), code="store_quote_dry_run_proof_invalid")
    dry_run = state["dry_runs"].get(proof)
    # The conductor deliberately uses a different idempotency key for apply.
    # The opaque dry-run proof, not that key, binds this exact delivery.
    if not isinstance(dry_run, dict) or not _store_quote_projection_matches(dry_run, projection):
        raise BridgeError("store_quote_dry_run_proof_invalid")
    last_message_id = dry_run.get("last_message_id")
    if isinstance(last_message_id, bool) or not isinstance(last_message_id, int):
        raise BridgeError("store_quote_dry_run_proof_invalid")
    if await _last_message_id(client, entity) != last_message_id:
        raise BridgeError("store_quote_conversation_changed_since_dry_run")
    sent = await client.send_message(entity, text)
    message_id = int(getattr(sent, "id", 0) or 0)
    if message_id <= 0:
        raise BridgeError("store_quote_send_failed")
    delivery_ref_sha256 = _store_quote_delivery_ref(
        projection,
        message_id=message_id,
        nonce=secrets.token_hex(16),
    )
    state["deliveries"][binding_key] = {
        **projection,
        "peer_id": binding["peer_id"],
        "message_id": message_id,
        "idempotency_key_sha256": idempotency_key_sha256,
        "delivery_ref_sha256": delivery_ref_sha256,
        "created_at": now,
    }
    state["dry_runs"].pop(proof, None)
    _save_store_quote_transport_state(config, state)
    try:
        await _store_quote_verified_delivery_message(
            client,
            entity,
            projection=projection,
            delivery=state["deliveries"][binding_key],
        )
    except BridgeError:
        # The send may have happened.  Preserve the private idempotency record
        # so an independent typed readback can reconcile it without a resend.
        return {
            "ok": False,
            "mode": "apply",
            "meta": {"outcome_uncertain": True},
            "summary": _store_quote_summary(projection, error_code="store_quote_send_readback_failed"),
        }
    return {
        "ok": True,
        "mode": "apply",
        "replayed": False,
        "summary": _store_quote_summary(
            projection,
            delivery_ref_sha256=delivery_ref_sha256,
            recipient_confirmed=recipient_confirmed,
            private_target_confirmed=True,
            unique_target_confirmed=True,
            work_account_confirmed=True,
            delivery_confirmed=True,
        ),
    }


async def _handle_store_quote_readback(
    client: Any,
    config: TelegramConfig,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Independently reread a sent quote and expose only a verified projection."""

    _require_store_quote_work_account(config)
    projection = _store_quote_projection(request, require_text=False)
    _store_quote_identifier(request.get("correlation_id"), code="store_quote_correlation_id_invalid")
    state = _load_store_quote_transport_state(config)
    _store_quote_prune_state(state, now=int(time.time()))
    entity, _target, _binding = await _store_quote_bound_target(
        client,
        state,
        projection,
        allow_pending_identity=_store_quote_is_identity_prompt(projection),
    )
    delivery = state["deliveries"].get(projection["delivery_binding_sha256"])
    if not isinstance(delivery, dict) or not _store_quote_projection_matches(delivery, projection):
        raise BridgeError("store_quote_delivery_missing")
    delivery_ref_sha256 = _store_quote_hash(
        delivery.get("delivery_ref_sha256"), code="store_quote_delivery_record_invalid"
    )
    await _store_quote_verified_delivery_message(client, entity, projection=projection, delivery=delivery)
    _save_store_quote_transport_state(config, state)
    return {
        "ok": True,
        "summary": _store_quote_summary(
            projection,
            delivery_ref_sha256=delivery_ref_sha256,
            recipient_confirmed=state["routes"][projection["route_binding_sha256"]].get("recipient_confirmed") is True,
            private_target_confirmed=True,
            unique_target_confirmed=True,
            work_account_confirmed=True,
            delivery_confirmed=True,
        ),
    }


def _store_quote_inbound_request(request: dict[str, Any]) -> tuple[dict[str, str], str, str, str]:
    """Validate only fields available to an inbound receipt readback."""

    projection = {
        "quote_ref_sha256": _store_quote_hash(request.get("quote_ref_sha256")),
        "revision_sha256": _store_quote_hash(request.get("revision_sha256")),
        "published_snapshot_hash": _store_quote_hash(request.get("published_snapshot_hash")),
        "context_hash": _store_quote_hash(request.get("context_hash")),
        "delivery_binding_sha256": _store_quote_hash(request.get("delivery_binding_sha256")),
        "route_binding_sha256": _store_quote_hash(request.get("route_binding_sha256")),
    }
    delivery_ref_sha256 = _store_quote_hash(request.get("delivery_ref_sha256"), code="store_quote_delivery_ref_invalid")
    receipt = str(request.get("receipt") or "").strip()
    if _STORE_QUOTE_RECEIPT_PATTERN.fullmatch(receipt) is None:
        raise BridgeError("store_quote_inbound_receipt_invalid")
    classification = str(request.get("reply_classification") or "").strip().casefold()
    if classification not in _STORE_QUOTE_REPLY_CATEGORIES:
        raise BridgeError("store_quote_reply_classification_invalid")
    return projection, delivery_ref_sha256, receipt, classification


async def _handle_store_quote_mint_inbound_receipt(
    client: Any,
    config: TelegramConfig,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Mint one opaque capability for the sole direct reply to a delivered quote."""

    _require_store_quote_work_account(config)
    projection = _store_quote_projection(request, require_text=False)
    if _store_quote_is_identity_prompt(projection):
        raise BridgeError("store_quote_identity_requires_identity_readback")
    _store_quote_identifier(request.get("correlation_id"), code="store_quote_correlation_id_invalid")
    supplied_ref = _store_quote_hash(request.get("delivery_ref_sha256"), code="store_quote_delivery_ref_invalid")
    now = int(time.time())
    state = _load_store_quote_transport_state(config)
    _store_quote_prune_state(state, now=now)
    entity, target, binding = await _store_quote_bound_target(client, state, projection)
    delivery = state["deliveries"].get(projection["delivery_binding_sha256"])
    if (
        not isinstance(delivery, dict)
        or not _store_quote_projection_matches(delivery, projection)
        or delivery.get("delivery_ref_sha256") != supplied_ref
    ):
        raise BridgeError("store_quote_delivery_missing")
    await _store_quote_verified_delivery_message(client, entity, projection=projection, delivery=delivery)
    messages = await client.get_messages(entity, limit=20)
    candidates = []
    for message in messages or []:
        sender_id = getattr(message, "sender_id", None)
        if (
            not bool(getattr(message, "out", False))
            and _message_reply_to_id(message) == delivery["message_id"]
            and isinstance(sender_id, int)
            and sender_id == target["id"]
        ):
            candidates.append(message)
    if len(candidates) != 1:
        raise BridgeError("store_quote_inbound_reply_not_unique")
    incoming = candidates[0]
    incoming_id = int(getattr(incoming, "id", 0) or 0)
    if incoming_id <= 0:
        raise BridgeError("store_quote_inbound_reply_not_unique")
    for existing_receipt, record in state["receipts"].items():
        if (
            isinstance(record, dict)
            and record.get("delivery_ref_sha256") == supplied_ref
            and record.get("incoming_message_id") == incoming_id
        ):
            if record.get("consumed") is True:
                raise BridgeError("store_quote_inbound_reply_already_consumed")
            if _store_quote_receipt_expired(record, now=now):
                raise BridgeError("store_quote_inbound_reply_expired")
            return {
                "ok": True,
                "receipt": existing_receipt,
                "summary": _store_quote_summary(
                    projection,
                    delivery_ref_sha256=supplied_ref,
                    reply_classification=record.get("reply_classification"),
                    reply_text_sha256=record.get("reply_text_sha256"),
                    incoming_ref_sha256=record.get("incoming_ref_sha256"),
                    recipient_confirmed=binding.get("recipient_confirmed") is True,
                    private_target_confirmed=True,
                    unique_target_confirmed=True,
                    work_account_confirmed=True,
                    delivery_confirmed=True,
                    reply_confirmed=True,
                    reply_sender_matches_delivery=True,
                ),
            }
    reply_text = str(getattr(incoming, "message", None) or "")
    reply_text_sha256 = _store_quote_sha256(reply_text)
    classification = _classify_store_quote_reply(reply_text)
    incoming_ref_sha256 = _store_quote_incoming_ref(
        delivery_ref_sha256=supplied_ref,
        message_id=incoming_id,
        text_sha256=reply_text_sha256,
    )
    receipt = secrets.token_urlsafe(32)
    while receipt in state["receipts"]:
        receipt = secrets.token_urlsafe(32)
    state["receipts"][receipt] = {
        **projection,
        "receipt_kind": "quote",
        "delivery_ref_sha256": supplied_ref,
        "peer_id": target["id"],
        "incoming_message_id": incoming_id,
        "reply_classification": classification,
        "reply_text_sha256": reply_text_sha256,
        "incoming_ref_sha256": incoming_ref_sha256,
        "issued_at": now,
        "consumed": False,
    }
    _save_store_quote_transport_state(config, state)
    return {
        "ok": True,
        "receipt": receipt,
        "summary": _store_quote_summary(
            projection,
            delivery_ref_sha256=supplied_ref,
            reply_classification=classification,
            reply_text_sha256=reply_text_sha256,
            incoming_ref_sha256=incoming_ref_sha256,
            recipient_confirmed=binding.get("recipient_confirmed") is True,
            private_target_confirmed=True,
            unique_target_confirmed=True,
            work_account_confirmed=True,
            delivery_confirmed=True,
            reply_confirmed=True,
            reply_sender_matches_delivery=True,
        ),
    }


async def _handle_store_quote_mint_identity_receipt(
    client: Any,
    config: TelegramConfig,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Mint one opaque receipt for the sole direct reply to an identity prompt."""

    _require_store_quote_work_account(config)
    projection = _store_quote_projection(request, require_text=False)
    if not _store_quote_is_identity_prompt(projection):
        raise BridgeError("store_quote_identity_prompt_required")
    _store_quote_identifier(request.get("correlation_id"), code="store_quote_correlation_id_invalid")
    supplied_ref = _store_quote_hash(request.get("delivery_ref_sha256"), code="store_quote_delivery_ref_invalid")
    now = int(time.time())
    state = _load_store_quote_transport_state(config)
    _store_quote_prune_state(state, now=now)
    route = state["routes"].get(projection["route_binding_sha256"])
    if not isinstance(route, dict) or (
        route.get("route_binding_sha256") != projection["route_binding_sha256"]
        or route.get("quote_ref_sha256") != projection["quote_ref_sha256"]
        or route.get("identity_delivery_binding_sha256") != projection["delivery_binding_sha256"]
    ):
        raise BridgeError("store_quote_identity_binding_invalid")
    entity, target, binding = await _store_quote_bound_target(
        client,
        state,
        projection,
        allow_pending_identity=True,
    )
    if binding.get("identity_candidate") is not True:
        raise BridgeError("store_quote_identity_binding_invalid")
    delivery = state["deliveries"].get(projection["delivery_binding_sha256"])
    if (
        not isinstance(delivery, dict)
        or not _store_quote_projection_matches(delivery, projection)
        or delivery.get("delivery_ref_sha256") != supplied_ref
    ):
        raise BridgeError("store_quote_delivery_missing")
    await _store_quote_verified_delivery_message(client, entity, projection=projection, delivery=delivery)
    messages = await client.get_messages(entity, limit=20)
    candidates = []
    for message in messages or []:
        sender_id = getattr(message, "sender_id", None)
        if (
            not bool(getattr(message, "out", False))
            and _message_reply_to_id(message) == delivery["message_id"]
            and isinstance(sender_id, int)
            and sender_id == target["id"]
        ):
            candidates.append(message)
    if len(candidates) != 1:
        raise BridgeError("store_quote_identity_reply_not_unique")
    incoming = candidates[0]
    incoming_id = int(getattr(incoming, "id", 0) or 0)
    if incoming_id <= 0:
        raise BridgeError("store_quote_identity_reply_not_unique")
    for existing_receipt, record in state["receipts"].items():
        if (
            isinstance(record, dict)
            and record.get("receipt_kind") == "identity"
            and record.get("delivery_ref_sha256") == supplied_ref
            and record.get("incoming_message_id") == incoming_id
        ):
            if record.get("consumed") is True:
                raise BridgeError("store_quote_identity_reply_already_consumed")
            if _store_quote_receipt_expired(record, now=now):
                raise BridgeError("store_quote_identity_reply_expired")
            return {
                "ok": True,
                "receipt": existing_receipt,
                "summary": _store_quote_summary(
                    projection,
                    delivery_ref_sha256=supplied_ref,
                    identity_classification=record.get("identity_classification"),
                    reply_text_sha256=record.get("reply_text_sha256"),
                    incoming_ref_sha256=record.get("incoming_ref_sha256"),
                    recipient_confirmed=False,
                    private_target_confirmed=True,
                    unique_target_confirmed=True,
                    work_account_confirmed=True,
                    delivery_confirmed=True,
                    reply_confirmed=True,
                    reply_sender_matches_delivery=True,
                    identity_pending=True,
                ),
            }
    reply_text = str(getattr(incoming, "message", None) or "")
    reply_text_sha256 = _store_quote_sha256(reply_text)
    identity_classification = _classify_store_quote_identity_reply(reply_text)
    incoming_ref_sha256 = _store_quote_incoming_ref(
        delivery_ref_sha256=supplied_ref,
        message_id=incoming_id,
        text_sha256=reply_text_sha256,
    )
    receipt = secrets.token_urlsafe(32)
    while receipt in state["receipts"]:
        receipt = secrets.token_urlsafe(32)
    state["receipts"][receipt] = {
        **projection,
        "receipt_kind": "identity",
        "delivery_ref_sha256": supplied_ref,
        "peer_id": target["id"],
        "incoming_message_id": incoming_id,
        "identity_classification": identity_classification,
        "reply_text_sha256": reply_text_sha256,
        "incoming_ref_sha256": incoming_ref_sha256,
        "issued_at": now,
        "consumed": False,
    }
    _save_store_quote_transport_state(config, state)
    return {
        "ok": True,
        "receipt": receipt,
        "summary": _store_quote_summary(
            projection,
            delivery_ref_sha256=supplied_ref,
            identity_classification=identity_classification,
            reply_text_sha256=reply_text_sha256,
            incoming_ref_sha256=incoming_ref_sha256,
            recipient_confirmed=False,
            private_target_confirmed=True,
            unique_target_confirmed=True,
            work_account_confirmed=True,
            delivery_confirmed=True,
            reply_confirmed=True,
            reply_sender_matches_delivery=True,
            identity_pending=True,
        ),
    }


async def _handle_store_quote_identity_readback(
    client: Any,
    config: TelegramConfig,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Consume/reread an identity receipt and atomically promote only bare yes."""

    _require_store_quote_work_account(config)
    projection = _store_quote_projection(request, require_text=False)
    if not _store_quote_is_identity_prompt(projection):
        raise BridgeError("store_quote_identity_prompt_required")
    _store_quote_identifier(request.get("correlation_id"), code="store_quote_correlation_id_invalid")
    delivery_ref_sha256 = _store_quote_hash(request.get("delivery_ref_sha256"), code="store_quote_delivery_ref_invalid")
    receipt = str(request.get("receipt") or "").strip()
    if _STORE_QUOTE_RECEIPT_PATTERN.fullmatch(receipt) is None:
        raise BridgeError("store_quote_inbound_receipt_invalid")
    now = int(time.time())
    state = _load_store_quote_transport_state(config)
    _store_quote_prune_state(state, now=now)
    route = state["routes"].get(projection["route_binding_sha256"])
    if not isinstance(route, dict) or (
        route.get("route_binding_sha256") != projection["route_binding_sha256"]
        or route.get("quote_ref_sha256") != projection["quote_ref_sha256"]
        or route.get("identity_delivery_binding_sha256") != projection["delivery_binding_sha256"]
    ):
        raise BridgeError("store_quote_identity_binding_invalid")
    entity, target, binding = await _store_quote_bound_target(
        client,
        state,
        projection,
        allow_pending_identity=True,
    )
    if binding.get("identity_candidate") is not True:
        raise BridgeError("store_quote_identity_binding_invalid")
    delivery = state["deliveries"].get(projection["delivery_binding_sha256"])
    if (
        not isinstance(delivery, dict)
        or not _store_quote_projection_matches(delivery, projection)
        or delivery.get("delivery_ref_sha256") != delivery_ref_sha256
    ):
        raise BridgeError("store_quote_delivery_missing")
    await _store_quote_verified_delivery_message(client, entity, projection=projection, delivery=delivery)
    record = state["receipts"].get(receipt)
    if not isinstance(record, dict):
        raise BridgeError("store_quote_inbound_receipt_unknown")
    if record.get("receipt_kind") != "identity":
        raise BridgeError("store_quote_identity_binding_invalid")
    if record.get("consumed") is True:
        raise BridgeError("store_quote_identity_reply_consumed")
    if _store_quote_receipt_expired(record, now=now):
        raise BridgeError("store_quote_identity_reply_expired")
    if (
        not _store_quote_projection_matches(record, projection)
        or record.get("delivery_ref_sha256") != delivery_ref_sha256
        or record.get("peer_id") != target["id"]
    ):
        raise BridgeError("store_quote_identity_binding_invalid")
    if route.get("recipient_confirmed") is True:
        raise BridgeError("store_quote_identity_already_confirmed")
    incoming_id = record.get("incoming_message_id")
    if isinstance(incoming_id, bool) or not isinstance(incoming_id, int) or incoming_id <= 0:
        raise BridgeError("store_quote_inbound_receipt_invalid")
    incoming = await client.get_messages(entity, ids=incoming_id)
    if (
        incoming is None
        or int(getattr(incoming, "id", 0) or 0) != incoming_id
        or bool(getattr(incoming, "out", False))
        or _message_reply_to_id(incoming) != delivery["message_id"]
        or getattr(incoming, "sender_id", None) != target["id"]
    ):
        raise BridgeError("store_quote_identity_reply_readback_failed")
    reply_text = str(getattr(incoming, "message", None) or "")
    reply_text_sha256 = _store_quote_sha256(reply_text)
    identity_classification = _classify_store_quote_identity_reply(reply_text)
    if not hmac.compare_digest(reply_text_sha256, str(record.get("reply_text_sha256") or "")) or (
        identity_classification != record.get("identity_classification")
    ):
        raise BridgeError("store_quote_identity_reply_changed")
    incoming_ref_sha256 = _store_quote_hash(
        record.get("incoming_ref_sha256"), code="store_quote_inbound_receipt_invalid"
    )
    identity_binding_sha256 = _store_quote_identity_inbound_binding(
        projection,
        delivery_ref_sha256=delivery_ref_sha256,
        identity_classification=identity_classification,
        reply_text_sha256=reply_text_sha256,
        incoming_ref_sha256=incoming_ref_sha256,
    )
    record["consumed"] = True
    confirmed = identity_classification == "confirmed"
    if confirmed:
        route["recipient_confirmed"] = True
        route["confirmed_at"] = now
    _save_store_quote_transport_state(config, state)
    return {
        "ok": True,
        "summary": _store_quote_summary(
            projection,
            delivery_ref_sha256=delivery_ref_sha256,
            identity_classification=identity_classification,
            reply_text_sha256=reply_text_sha256,
            incoming_ref_sha256=incoming_ref_sha256,
            inbound_binding_sha256=identity_binding_sha256,
            recipient_confirmed=confirmed,
            private_target_confirmed=True,
            unique_target_confirmed=True,
            work_account_confirmed=True,
            delivery_confirmed=True,
            reply_confirmed=True,
            reply_sender_matches_delivery=True,
            identity_pending=not confirmed,
            identity_confirmed=confirmed,
        ),
    }


async def _handle_store_quote_reply_readback(
    client: Any,
    config: TelegramConfig,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Consume and reread a receipt; a claimed category never substitutes this proof."""

    _require_store_quote_work_account(config)
    inbound_projection, delivery_ref_sha256, receipt, supplied_classification = _store_quote_inbound_request(request)
    _store_quote_identifier(request.get("correlation_id"), code="store_quote_correlation_id_invalid")
    now = int(time.time())
    state = _load_store_quote_transport_state(config)
    _store_quote_prune_state(state, now=now)
    binding = state["bindings"].get(inbound_projection["delivery_binding_sha256"])
    if not isinstance(binding, dict):
        raise BridgeError("store_quote_recipient_binding_missing")
    projection = {key: str(binding.get(key) or "") for key in (*inbound_projection, "message_sha256", "message_kind")}
    if not _store_quote_projection_matches(binding, projection) or any(
        projection[key] != value for key, value in inbound_projection.items()
    ):
        raise BridgeError("store_quote_inbound_binding_invalid")
    if _store_quote_is_identity_prompt(projection):
        raise BridgeError("store_quote_identity_requires_identity_readback")
    entity, target, binding = await _store_quote_bound_target(client, state, projection)
    delivery = state["deliveries"].get(projection["delivery_binding_sha256"])
    if (
        not isinstance(delivery, dict)
        or not _store_quote_projection_matches(delivery, projection)
        or delivery.get("delivery_ref_sha256") != delivery_ref_sha256
    ):
        raise BridgeError("store_quote_delivery_missing")
    await _store_quote_verified_delivery_message(client, entity, projection=projection, delivery=delivery)
    record = state["receipts"].get(receipt)
    if not isinstance(record, dict):
        raise BridgeError("store_quote_inbound_receipt_unknown")
    if record.get("receipt_kind") not in {None, "quote"}:
        raise BridgeError("store_quote_inbound_binding_invalid")
    if record.get("consumed") is True:
        raise BridgeError("store_quote_inbound_receipt_consumed")
    if _store_quote_receipt_expired(record, now=now):
        raise BridgeError("store_quote_inbound_receipt_expired")
    if (
        not _store_quote_projection_matches(record, projection)
        or record.get("delivery_ref_sha256") != delivery_ref_sha256
        or record.get("reply_classification") != supplied_classification
        or record.get("peer_id") != target["id"]
    ):
        raise BridgeError("store_quote_inbound_binding_invalid")
    incoming_id = record.get("incoming_message_id")
    if isinstance(incoming_id, bool) or not isinstance(incoming_id, int) or incoming_id <= 0:
        raise BridgeError("store_quote_inbound_receipt_invalid")
    incoming = await client.get_messages(entity, ids=incoming_id)
    if (
        incoming is None
        or int(getattr(incoming, "id", 0) or 0) != incoming_id
        or bool(getattr(incoming, "out", False))
        or _message_reply_to_id(incoming) != delivery["message_id"]
        or getattr(incoming, "sender_id", None) != target["id"]
    ):
        raise BridgeError("store_quote_inbound_reply_readback_failed")
    reply_text = str(getattr(incoming, "message", None) or "")
    reply_text_sha256 = _store_quote_sha256(reply_text)
    classification = _classify_store_quote_reply(reply_text)
    if not hmac.compare_digest(
        reply_text_sha256, str(record.get("reply_text_sha256") or "")
    ) or classification != record.get("reply_classification"):
        raise BridgeError("store_quote_inbound_reply_changed")
    incoming_ref_sha256 = _store_quote_hash(
        record.get("incoming_ref_sha256"), code="store_quote_inbound_receipt_invalid"
    )
    inbound_binding_sha256 = _store_quote_inbound_binding(
        projection,
        delivery_ref_sha256=delivery_ref_sha256,
        reply_classification=classification,
        reply_text_sha256=reply_text_sha256,
        incoming_ref_sha256=incoming_ref_sha256,
    )
    record["consumed"] = True
    _save_store_quote_transport_state(config, state)
    return {
        "ok": True,
        "summary": _store_quote_summary(
            projection,
            delivery_ref_sha256=delivery_ref_sha256,
            reply_classification=classification,
            reply_text_sha256=reply_text_sha256,
            incoming_ref_sha256=incoming_ref_sha256,
            inbound_binding_sha256=inbound_binding_sha256,
            recipient_confirmed=binding.get("recipient_confirmed") is True,
            private_target_confirmed=True,
            unique_target_confirmed=True,
            work_account_confirmed=True,
            delivery_confirmed=True,
            reply_confirmed=True,
            reply_sender_matches_delivery=True,
        ),
    }


async def _handle_operation(  # noqa: C901
    client: Any, config: TelegramConfig, request: dict[str, Any]
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

    # These are intentionally daemon-only RPC operations.  They do not have
    # CLI switches and never return Telegram identities, message ids or text.
    if operation == "store_quote_bind_identity_candidate":
        return await _handle_store_quote_bind_identity_candidate(client, config, request)

    if operation == "store_quote_bind_recipient":
        return await _handle_store_quote_bind_recipient(client, config, request)

    if operation == "store_quote_send":
        return await _handle_store_quote_send(client, config, request)

    if operation == "store_quote_readback":
        return await _handle_store_quote_readback(client, config, request)

    if operation == "store_quote_mint_inbound_receipt":
        return await _handle_store_quote_mint_inbound_receipt(client, config, request)

    if operation == "store_quote_mint_identity_receipt":
        return await _handle_store_quote_mint_identity_receipt(client, config, request)

    if operation == "store_quote_reply_readback":
        return await _handle_store_quote_reply_readback(client, config, request)

    if operation == "store_quote_identity_readback":
        return await _handle_store_quote_identity_readback(client, config, request)

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
    return (
        request.get("operation") in {"send", "send_photo", "download"} and request.get("mode") == "apply"
    ) or request.get("operation") in {
        "discard_download",
        "store_quote_bind_identity_candidate",
        "store_quote_bind_recipient",
        # Every typed Store quote operation may prune or persist the private
        # bridge state.  Serialize all of them so a stale readback/dry-run
        # cannot overwrite a receipt which another request has consumed.
        "store_quote_send",
        "store_quote_readback",
        "store_quote_mint_inbound_receipt",
        "store_quote_mint_identity_receipt",
        "store_quote_reply_readback",
        "store_quote_identity_readback",
    }


async def _serve_client(
    client: Any, config: TelegramConfig, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
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
                response = await _handle_operation(client, config, request)
        else:
            response = await _handle_operation(client, config, request)
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


async def run_daemon(config: TelegramConfig) -> None:
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
    client = TelegramClient(str(config.session_path), config.api_id, config.api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise BridgeError("account_not_authorized")
    server = await asyncio.start_unix_server(
        lambda reader, writer: _serve_client(client, config, reader, writer),
        path=str(config.socket_path),
    )
    os.chmod(config.socket_path, 0o600)
    try:
        async with server:
            await server.serve_forever()
    finally:
        await client.disconnect()
        config.socket_path.unlink(missing_ok=True)


def _save_qr(url: str, output_path: Path) -> None:
    qrcode = _load_qrcode()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image = qrcode.make(url)
    temp_path = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    try:
        image.save(temp_path, format="PNG")
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, output_path)
    finally:
        temp_path.unlink(missing_ok=True)


async def run_qr_login(config: TelegramConfig, output_path: Path, *, two_factor_password: str = "") -> dict[str, Any]:
    TelegramClient, _, SessionPasswordNeededError = _load_telethon()
    client = TelegramClient(str(config.session_path), config.api_id, config.api_hash)
    await client.connect()
    try:
        if await client.is_user_authorized():
            return {"ok": True, "authorized": True, "already_authorized": True}
        qr_login = await client.qr_login()
        for _ in range(24):
            _save_qr(qr_login.url, output_path)
            try:
                # Telegram supplies the token expiry. Waiting for that exact
                # lifetime avoids invalidating a QR which is still being shown
                # to the user.
                user = await qr_login.wait()
                return {
                    "ok": True,
                    "authorized": True,
                    "already_authorized": False,
                    "account_id": int(getattr(user, "id", 0) or 0),
                }
            except TimeoutError:
                await qr_login.recreate()
            except SessionPasswordNeededError as exc:
                if not two_factor_password:
                    raise BridgeError("two_factor_password_required") from exc
                try:
                    user = await client.sign_in(password=two_factor_password)
                except Exception as password_exc:
                    raise BridgeError("two_factor_password_invalid") from password_exc
                return {
                    "ok": True,
                    "authorized": True,
                    "already_authorized": False,
                    "account_id": int(getattr(user, "id", 0) or 0),
                }
        raise BridgeError("qr_login_timeout")
    finally:
        await client.disconnect()


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


def send_local_request(socket_path: Path, request: dict[str, Any]) -> dict[str, Any]:
    encoded = _canonical_json(request) + b"\n"
    if len(encoded) > MAX_REQUEST_BYTES:
        raise BridgeError("request_size_invalid")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(30)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autostop-telegram")
    parser.add_argument("--account", choices=tuple(ACCOUNT_PATHS), required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("daemon")
    qr_login = subparsers.add_parser("qr-login")
    qr_login.add_argument("--output", type=Path)
    qr_login.add_argument("--password-file", type=Path)
    subparsers.add_parser("code-login")
    subparsers.add_parser("probe")
    subparsers.add_parser("status")
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
            asyncio.run(run_daemon(config))
            return 0
        if args.command == "qr-login":
            config = _config_from_args(args)
            two_factor_password = _read_one_time_password(args.password_file) if args.password_file else ""
            output_path = args.output or config.state_dir / "login-qr.png"
            payload = asyncio.run(run_qr_login(config, output_path, two_factor_password=two_factor_password))
        elif args.command == "code-login":
            if args.account != "work":
                raise BridgeError("code_login_requires_work_account")
            payload = asyncio.run(run_code_login(_config_from_args(args)))
        else:
            request: dict[str, Any] = {"operation": args.command}
            if args.command == "dialogs":
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
