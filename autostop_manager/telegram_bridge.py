from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
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
MAX_REQUEST_BYTES = 128 * 1024
MAX_MESSAGE_CHARS = 4096
CONTRACT_TTL_SECONDS = 15 * 60
SENSITIVE_URI_PATTERN = re.compile(r"(?i)\b(?:tg|vpn)://[^\s]+")


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

    @classmethod
    def load(cls, credentials_path: Path = DEFAULT_CREDENTIALS_PATH) -> TelegramConfig:
        try:
            file_stat = credentials_path.stat()
        except OSError as exc:
            raise BridgeError("credentials_unavailable") from exc
        if stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise BridgeError("credentials_permissions_too_open")

        values: dict[str, str] = {}
        try:
            lines = credentials_path.read_text(encoding="ascii").splitlines()
        except (OSError, UnicodeError) as exc:
            raise BridgeError("credentials_unreadable") from exc
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
        return cls(api_id=int(api_id_raw), api_hash=api_hash, credentials_path=credentials_path)


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def issue_send_contract(
    secret: bytes,
    *,
    peer_id: int,
    text: str,
    last_message_id: int,
    now: int | None = None,
) -> str:
    issued_at = int(time.time()) if now is None else int(now)
    payload = {
        "issued_at": issued_at,
        "last_message_id": int(last_message_id),
        "peer_id": int(peer_id),
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
    issued_at = payload.get("issued_at")
    if not isinstance(issued_at, int) or issued_at > current_time + 60:
        raise BridgeError("contract_invalid")
    if current_time - issued_at > CONTRACT_TTL_SECONDS:
        raise BridgeError("contract_expired")
    if not isinstance(payload.get("last_message_id"), int):
        raise BridgeError("contract_invalid")
    return payload


def validate_send_request(request: dict[str, Any]) -> tuple[str, str, str, str]:
    peer = str(request.get("peer") or "").strip()
    text = str(request.get("text") or "")
    mode = str(request.get("mode") or "dry_run")
    idempotency_key = str(request.get("idempotency_key") or "").strip()
    if not peer:
        raise BridgeError("peer_required")
    if not text or len(text) > MAX_MESSAGE_CHARS:
        raise BridgeError("message_length_invalid")
    if mode not in {"dry_run", "apply"}:
        raise BridgeError("mode_invalid")
    if mode == "apply" and not idempotency_key:
        raise BridgeError("idempotency_key_required")
    return peer, text, mode, idempotency_key


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
        file_stat = path.stat()
    except OSError as exc:
        raise BridgeError("two_factor_password_unavailable") from exc
    if not stat.S_ISREG(file_stat.st_mode) or stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise BridgeError("two_factor_password_permissions_invalid")
    if file_stat.st_size > 1024:
        raise BridgeError("two_factor_password_invalid")
    try:
        password = path.read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError) as exc:
        raise BridgeError("two_factor_password_unreadable") from exc
    finally:
        path.unlink(missing_ok=True)
    if not password:
        raise BridgeError("two_factor_password_invalid")
    return password


def _ensure_private_key(path: Path) -> bytes:
    if path.exists():
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise BridgeError("contract_key_permissions_too_open")
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(key)
    return key


def _load_idempotency(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeError("idempotency_state_unreadable") from exc
    if not isinstance(payload, dict):
        raise BridgeError("idempotency_state_invalid")
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


def _save_idempotency(path: Path, payload: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp_path, path)


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
        target["is_contact"] = bool(getattr(entity, "contact", False))
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


async def _last_message_id(client: Any, entity: Any) -> int:
    messages = await client.get_messages(entity, limit=1)
    return int(messages[0].id) if messages else 0


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
                    "text": text,
                    "sensitive_content_redacted": sensitive_content_redacted,
                    "media_type": type(message.media).__name__ if message.media is not None else None,
                }
            )
        return {"ok": True, "target": target, "messages": rows}

    if operation == "send":
        peer, text, mode, idempotency_key = validate_send_request(request)
        entity, target = await _resolve_peer(client, peer)
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
                "contract_token": issue_send_contract(
                    contract_key,
                    peer_id=target["id"],
                    text=text,
                    last_message_id=last_message_id,
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
        )
        if contract["last_message_id"] != last_message_id:
            raise BridgeError("conversation_changed_since_dry_run")

        idempotency_path = config.state_dir / "idempotency.json"
        idempotency = _load_idempotency(idempotency_path)
        previous = idempotency.get(idempotency_key)
        if previous is not None:
            if previous.get("peer_id") != target["id"] or previous.get("text_sha256") != text_sha256:
                raise BridgeError("idempotency_key_conflict")
            return {
                "ok": True,
                "mode": "apply",
                "replayed": True,
                "target": target,
                "message_id": previous.get("message_id"),
            }

        sent = await client.send_message(entity, text)
        idempotency[idempotency_key] = {
            "message_id": int(sent.id),
            "peer_id": target["id"],
            "text_sha256": text_sha256,
        }
        _save_idempotency(idempotency_path, idempotency)
        readback = await client.get_messages(entity, ids=int(sent.id))
        if readback is None or str(readback.message or "") != text:
            raise BridgeError("send_readback_failed")
        return {
            "ok": True,
            "mode": "apply",
            "replayed": False,
            "target": target,
            "message_id": int(sent.id),
            "verified": True,
        }

    raise BridgeError("operation_not_supported")


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
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("daemon")
    qr_login = subparsers.add_parser("qr-login")
    qr_login.add_argument("--output", type=Path, default=DEFAULT_STATE_DIR / "login-qr.png")
    qr_login.add_argument("--password-file", type=Path)
    subparsers.add_parser("status")
    dialogs = subparsers.add_parser("dialogs")
    dialogs.add_argument("--limit", type=int, default=20)
    search = subparsers.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=20)
    read = subparsers.add_parser("read")
    read.add_argument("--peer", required=True)
    read.add_argument("--limit", type=int, default=20)
    send = subparsers.add_parser("send")
    send.add_argument("--peer", required=True)
    send.add_argument("--text", required=True)
    send.add_argument("--mode", choices=["dry_run", "apply"], default="dry_run")
    send.add_argument("--contract-token", default="")
    send.add_argument("--idempotency-key", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "daemon":
            config = TelegramConfig.load(args.credentials)
            config = TelegramConfig(
                api_id=config.api_id,
                api_hash=config.api_hash,
                credentials_path=config.credentials_path,
                socket_path=args.socket,
            )
            asyncio.run(run_daemon(config))
            return 0
        if args.command == "qr-login":
            config = TelegramConfig.load(args.credentials)
            two_factor_password = _read_one_time_password(args.password_file) if args.password_file else ""
            payload = asyncio.run(run_qr_login(config, args.output, two_factor_password=two_factor_password))
        else:
            request: dict[str, Any] = {"operation": args.command}
            if args.command == "dialogs":
                request["limit"] = args.limit
            elif args.command == "search":
                request.update({"query": args.query, "limit": args.limit})
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
                    }
                )
            payload = send_local_request(args.socket, request)
    except BridgeError as exc:
        payload = {"ok": False, "error": exc.code}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
