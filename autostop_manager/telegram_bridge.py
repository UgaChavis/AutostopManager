from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import socket
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CREDENTIALS_PATH = Path("/etc/autostop-telegram/credentials")
DEFAULT_SESSION_PATH = Path("/var/lib/autostop-telegram/account")
DEFAULT_STATE_DIR = Path("/var/lib/autostop-telegram")
DEFAULT_SOCKET_PATH = Path("/run/autostop-telegram/bridge.sock")
DEFAULT_2FA_PASSWORD_PATH = Path("/run/autostop-telegram/2fa-password.once")
DEFAULT_ROLE_BINDINGS_PATH = Path("/var/lib/autostop-telegram/director_roles.json")
MAX_REQUEST_BYTES = 128 * 1024
MAX_MESSAGE_CHARS = 4096
MAX_CAPTION_CHARS = 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024
CONTRACT_TTL_SECONDS = 15 * 60
DEFAULT_OUTBOX_DIR = Path("/run/autostop-telegram/outbox")
DEFAULT_INBOX_DIR = Path("/run/autostop-telegram/inbox")
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
MAX_VIDEO_DURATION_SECONDS = 2 * 60
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
DIRECTOR_ROLES = frozenset(
    {
        "director_admin",
        "director_reception",
        "director_workshop",
    }
)
MAX_ROLE_BINDINGS_BYTES = 4096
_MUTATION_LOCK = asyncio.Lock()


class BridgeError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TelegramConfig:
    api_id: int
    api_hash: str
    credentials_path: Path = DEFAULT_CREDENTIALS_PATH
    session_path: Path = DEFAULT_SESSION_PATH
    state_dir: Path = DEFAULT_STATE_DIR
    socket_path: Path = DEFAULT_SOCKET_PATH
    role_bindings_path: Path = DEFAULT_ROLE_BINDINGS_PATH

    @classmethod
    def load(
        cls,
        credentials_path: Path = DEFAULT_CREDENTIALS_PATH,
        *,
        session_path: Path = DEFAULT_SESSION_PATH,
        state_dir: Path = DEFAULT_STATE_DIR,
        socket_path: Path = DEFAULT_SOCKET_PATH,
        role_bindings_path: Path = DEFAULT_ROLE_BINDINGS_PATH,
    ) -> TelegramConfig:
        runtime_paths = (credentials_path, session_path, state_dir, socket_path, role_bindings_path)
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
            role_bindings_path=role_bindings_path,
        )


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def issue_send_contract(
    secret: bytes,
    *,
    peer_id: int,
    text: str,
    last_message_id: int,
    role: str | None = None,
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
    if role is not None:
        payload["role"] = _validate_role(role)
    encoded = base64.urlsafe_b64encode(_canonical_json(payload)).rstrip(b"=")
    signature = hmac.new(secret, encoded, hashlib.sha256).digest()
    return f"{encoded.decode('ascii')}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii')}"


def verify_send_contract(
    token: str,
    secret: bytes,
    *,
    peer_id: int,
    text: str,
    role: str | None = None,
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
    if role is None and "role" in payload:
        raise BridgeError("contract_role_required")
    if role is not None and payload.get("role") != _validate_role(role):
        raise BridgeError("contract_role_changed")
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


def issue_role_binding_contract(
    secret: bytes,
    *,
    role: str,
    peer_id: int,
    replace: bool,
    now: int | None = None,
) -> str:
    issued_at = int(time.time()) if now is None else int(now)
    payload = {
        "issued_at": issued_at,
        "peer_id": int(peer_id),
        "replace": bool(replace),
        "role": _validate_role(role),
        "type": "role_binding",
    }
    encoded = base64.urlsafe_b64encode(_canonical_json(payload)).rstrip(b"=")
    signature = hmac.new(secret, encoded, hashlib.sha256).digest()
    return f"{encoded.decode('ascii')}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii')}"


def verify_role_binding_contract(
    token: str,
    secret: bytes,
    *,
    role: str,
    peer_id: int,
    replace: bool,
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
    if not isinstance(payload, dict) or payload.get("type") != "role_binding":
        raise BridgeError("contract_invalid")
    if payload.get("role") != _validate_role(role):
        raise BridgeError("contract_role_changed")
    if payload.get("peer_id") != int(peer_id):
        raise BridgeError("contract_target_changed")
    if payload.get("replace") is not bool(replace):
        raise BridgeError("contract_replace_changed")
    issued_at = payload.get("issued_at")
    if not isinstance(issued_at, int) or issued_at > current_time + 60:
        raise BridgeError("contract_invalid")
    if current_time - issued_at > CONTRACT_TTL_SECONDS:
        raise BridgeError("contract_expired")
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


def _validate_role(value: str) -> str:
    role = str(value or "").strip()
    if role not in DIRECTOR_ROLES:
        raise BridgeError("role_invalid")
    return role


def validate_role_request(request: dict[str, Any]) -> str:
    return _validate_role(str(request.get("role") or ""))


def validate_bind_role_request(request: dict[str, Any]) -> tuple[str, str, str, str, bool]:
    role = validate_role_request(request)
    peer = str(request.get("peer") or "").strip()
    mode = str(request.get("mode") or "dry_run")
    idempotency_key = str(request.get("idempotency_key") or "").strip()
    raw_replace = request.get("replace", False)
    if not isinstance(raw_replace, bool):
        raise BridgeError("replace_invalid")
    replace = raw_replace
    if not peer:
        raise BridgeError("peer_required")
    if mode not in {"dry_run", "apply"}:
        raise BridgeError("mode_invalid")
    if mode == "apply" and not idempotency_key:
        raise BridgeError("idempotency_key_required")
    return role, peer, mode, idempotency_key, replace


def validate_send_role_request(request: dict[str, Any]) -> tuple[str, str, str, str]:
    role = validate_role_request(request)
    text = str(request.get("text") or "")
    mode = str(request.get("mode") or "dry_run")
    idempotency_key = str(request.get("idempotency_key") or "").strip()
    if not text or len(text) > MAX_MESSAGE_CHARS:
        raise BridgeError("message_length_invalid")
    if mode not in {"dry_run", "apply"}:
        raise BridgeError("mode_invalid")
    if mode == "apply" and not idempotency_key:
        raise BridgeError("idempotency_key_required")
    return role, text, mode, idempotency_key


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


def _validate_role_bindings_payload(payload: Any) -> dict[str, int]:
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise BridgeError("role_bindings_invalid")
    raw_roles = payload.get("roles")
    if not isinstance(raw_roles, dict) or any(role not in DIRECTOR_ROLES for role in raw_roles):
        raise BridgeError("role_bindings_invalid")
    bindings: dict[str, int] = {}
    for raw_role, raw_peer_id in raw_roles.items():
        role = _validate_role(str(raw_role))
        if isinstance(raw_peer_id, bool) or not isinstance(raw_peer_id, int) or raw_peer_id == 0:
            raise BridgeError("role_bindings_invalid")
        bindings[role] = raw_peer_id
    if len(set(bindings.values())) != len(bindings):
        raise BridgeError("role_bindings_invalid")
    return bindings


def _load_role_bindings(path: Path) -> dict[str, int]:
    if not path.is_absolute():
        raise BridgeError("role_bindings_path_invalid")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise BridgeError("role_bindings_unavailable") from exc
    try:
        file_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.geteuid()
            or stat.S_IMODE(file_stat.st_mode) != 0o600
            or not 0 < file_stat.st_size <= MAX_ROLE_BINDINGS_BYTES
        ):
            raise BridgeError("role_bindings_invalid")
        raw = os.read(descriptor, MAX_ROLE_BINDINGS_BYTES + 1)
        if len(raw) > MAX_ROLE_BINDINGS_BYTES:
            raise BridgeError("role_bindings_invalid")
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeError("role_bindings_unreadable") from exc
    finally:
        os.close(descriptor)
    return _validate_role_bindings_payload(payload)


def _save_role_bindings(path: Path, bindings: dict[str, int]) -> None:
    normalized = _validate_role_bindings_payload({"version": 1, "roles": bindings})
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_stat = path.parent.stat()
    if parent_stat.st_uid != os.geteuid() or stat.S_IMODE(parent_stat.st_mode) & 0o077:
        raise BridgeError("role_bindings_directory_invalid")
    temp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        descriptor = os.open(
            temp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                {"version": 1, "roles": normalized},
                stream,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
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
    try:
        from telethon import functions, utils
    except ImportError as exc:
        raise BridgeError("telethon_not_installed") from exc

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


def _safe_role_target(role: str, target: dict[str, Any]) -> dict[str, Any]:
    return {
        "bound": True,
        "is_contact": bool(target.get("is_contact")),
        "kind": str(target.get("kind") or "unknown"),
        "role": _validate_role(role),
    }


async def _resolve_role(
    client: Any,
    config: TelegramConfig,
    role: str,
) -> tuple[Any, dict[str, Any]]:
    normalized_role = _validate_role(role)
    bindings = _load_role_bindings(config.role_bindings_path)
    peer_id = bindings.get(normalized_role)
    if peer_id is None:
        raise BridgeError("role_not_bound")
    entity, target = await _resolve_peer(client, str(peer_id))
    if target.get("id") != peer_id or target.get("kind") != "private" or target.get("is_contact") is not True:
        raise BridgeError("role_binding_invalid")
    return entity, target


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
    role: str | None = None,
    reply_to_message_id: int = 0,
) -> dict[str, Any]:
    reply_source = await _load_reply_source(client, entity, reply_to_message_id)
    reply_message_sha256 = reply_source["text_sha256"] if reply_source is not None else ""
    last_message_id = await _last_message_id(client, entity)
    contract_key = _ensure_private_key(config.state_dir / "contract.key")
    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    response_target = _safe_role_target(role, target) if role is not None else target
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
                role=role,
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
        role=role,
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
            or previous.get("role") != role
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
        "role": role,
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


async def _handle_bind_role(
    client: Any,
    config: TelegramConfig,
    request: dict[str, Any],
) -> dict[str, Any]:
    role, peer, mode, idempotency_key, replace = validate_bind_role_request(request)
    _entity, target = await _resolve_peer(client, peer)
    if target.get("kind") != "private" or target.get("is_contact") is not True:
        raise BridgeError("role_candidate_invalid")
    peer_id = int(target["id"])
    bindings = _load_role_bindings(config.role_bindings_path)
    existing_peer_id = bindings.get(role)
    if existing_peer_id is not None and existing_peer_id != peer_id and not replace:
        raise BridgeError("role_already_bound")
    if any(bound_role != role and bound_peer_id == peer_id for bound_role, bound_peer_id in bindings.items()):
        raise BridgeError("peer_already_bound")
    contract_key = _ensure_private_key(config.state_dir / "contract.key")
    if mode == "dry_run":
        return {
            "ok": True,
            "mode": "dry_run",
            "role": role,
            "target": _safe_role_target(role, target),
            "replace": replace,
            "contract_token": issue_role_binding_contract(
                contract_key,
                role=role,
                peer_id=peer_id,
                replace=replace,
            ),
        }

    contract_token = str(request.get("contract_token") or "")
    if not contract_token:
        raise BridgeError("contract_required")
    verify_role_binding_contract(
        contract_token,
        contract_key,
        role=role,
        peer_id=peer_id,
        replace=replace,
    )
    idempotency_path = config.state_dir / "idempotency.json"
    idempotency = _load_idempotency(idempotency_path)
    previous = idempotency.get(idempotency_key)
    if previous is not None:
        if (
            previous.get("operation") != "bind_role"
            or previous.get("role") != role
            or previous.get("peer_id") != peer_id
        ):
            raise BridgeError("idempotency_key_conflict")
        return {"ok": True, "mode": "apply", "replayed": True, "role": role, "verified": True}
    bindings[role] = peer_id
    _save_role_bindings(config.role_bindings_path, bindings)
    idempotency[idempotency_key] = {
        "operation": "bind_role",
        "peer_id": peer_id,
        "role": role,
    }
    _save_idempotency(idempotency_path, idempotency)
    rebound = _load_role_bindings(config.role_bindings_path)
    if rebound.get(role) != peer_id:
        raise BridgeError("role_binding_readback_failed")
    return {"ok": True, "mode": "apply", "replayed": False, "role": role, "verified": True}


async def _handle_roles(client: Any, config: TelegramConfig) -> dict[str, Any]:
    bindings = _load_role_bindings(config.role_bindings_path)
    rows = []
    for role in sorted(DIRECTOR_ROLES):
        row = {"role": role, "bound": role in bindings, "verified": False}
        if row["bound"]:
            try:
                _entity, target = await _resolve_role(client, config, role)
            except BridgeError:
                row["error"] = "role_binding_invalid"
            else:
                row["verified"] = True
                row["kind"] = target["kind"]
        rows.append(row)
    return {"ok": True, "roles": rows}


async def _handle_operation(client: Any, config: TelegramConfig, request: dict[str, Any]) -> dict[str, Any]:
    operation = str(request.get("operation") or "")
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

    if operation == "roles":
        return await _handle_roles(client, config)

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

    if operation == "read_role":
        role = validate_role_request(request)
        limit = max(1, min(int(request.get("limit") or 20), 100))
        entity, target = await _resolve_role(client, config, role)
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
        return {"ok": True, "target": _safe_role_target(role, target), "messages": rows}

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

    if operation == "send_role":
        role, text, mode, idempotency_key = validate_send_role_request(request)
        entity, target = await _resolve_role(client, config, role)
        return await _handle_send_text_to_entity(
            client,
            config,
            request,
            entity=entity,
            target=target,
            text=text,
            mode=mode,
            idempotency_key=idempotency_key,
            role=role,
        )

    if operation == "bind_role":
        return await _handle_bind_role(client, config, request)

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
        request.get("operation") in {"bind_role", "send", "send_photo", "send_role", "download"}
        and request.get("mode") == "apply"
    ) or request.get("operation") == "discard_download"


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autostop-telegram")
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS_PATH)
    parser.add_argument("--session", type=Path, default=DEFAULT_SESSION_PATH)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET_PATH)
    parser.add_argument("--role-bindings", type=Path, default=DEFAULT_ROLE_BINDINGS_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("daemon")
    qr_login = subparsers.add_parser("qr-login")
    qr_login.add_argument("--output", type=Path)
    qr_login.add_argument("--password-file", type=Path)
    subparsers.add_parser("status")
    dialogs = subparsers.add_parser("dialogs")
    dialogs.add_argument("--limit", type=int, default=20)
    search = subparsers.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=20)
    subparsers.add_parser("roles")
    read = subparsers.add_parser("read")
    read.add_argument("--peer", required=True)
    read.add_argument("--limit", type=int, default=20)
    read_role = subparsers.add_parser("read-role")
    read_role.add_argument("--role", required=True, choices=sorted(DIRECTOR_ROLES))
    read_role.add_argument("--limit", type=int, default=20)
    send = subparsers.add_parser("send")
    send.add_argument("--peer", required=True)
    send.add_argument("--text", required=True)
    send.add_argument("--mode", choices=["dry_run", "apply"], default="dry_run")
    send.add_argument("--contract-token", default="")
    send.add_argument("--idempotency-key", default="")
    send.add_argument("--reply-to-message-id", type=int, default=0)
    send_role = subparsers.add_parser("send-role")
    send_role.add_argument("--role", required=True, choices=sorted(DIRECTOR_ROLES))
    send_role.add_argument("--text", required=True)
    send_role.add_argument("--mode", choices=["dry_run", "apply"], default="dry_run")
    send_role.add_argument("--contract-token", default="")
    send_role.add_argument("--idempotency-key", default="")
    bind_role = subparsers.add_parser("bind-role")
    bind_role.add_argument("--role", required=True, choices=sorted(DIRECTOR_ROLES))
    bind_role.add_argument("--peer", required=True)
    bind_role.add_argument("--mode", choices=["dry_run", "apply"], default="dry_run")
    bind_role.add_argument("--contract-token", default="")
    bind_role.add_argument("--idempotency-key", default="")
    bind_role.add_argument("--replace", action="store_true")
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
            config = TelegramConfig.load(
                args.credentials,
                session_path=args.session,
                state_dir=args.state_dir,
                socket_path=args.socket,
                role_bindings_path=args.role_bindings,
            )
            asyncio.run(run_daemon(config))
            return 0
        if args.command == "qr-login":
            config = TelegramConfig.load(
                args.credentials,
                session_path=args.session,
                state_dir=args.state_dir,
                socket_path=args.socket,
                role_bindings_path=args.role_bindings,
            )
            two_factor_password = _read_one_time_password(args.password_file) if args.password_file else ""
            output_path = args.output or config.state_dir / "login-qr.png"
            payload = asyncio.run(run_qr_login(config, output_path, two_factor_password=two_factor_password))
        else:
            request: dict[str, Any] = {"operation": args.command}
            if args.command == "dialogs":
                request["limit"] = args.limit
            elif args.command == "search":
                request.update({"query": args.query, "limit": args.limit})
            elif args.command == "roles":
                pass
            elif args.command == "read":
                request.update({"peer": args.peer, "limit": args.limit})
            elif args.command == "read-role":
                request.update({"operation": "read_role", "role": args.role, "limit": args.limit})
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
            elif args.command == "send-role":
                request.update(
                    {
                        "operation": "send_role",
                        "role": args.role,
                        "text": args.text,
                        "mode": args.mode,
                        "contract_token": args.contract_token,
                        "idempotency_key": args.idempotency_key,
                    }
                )
            elif args.command == "bind-role":
                request.update(
                    {
                        "operation": "bind_role",
                        "role": args.role,
                        "peer": args.peer,
                        "mode": args.mode,
                        "contract_token": args.contract_token,
                        "idempotency_key": args.idempotency_key,
                        "replace": args.replace,
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
            payload = send_local_request(args.socket, request)
    except BridgeError as exc:
        payload = {"ok": False, "error": exc.code}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
