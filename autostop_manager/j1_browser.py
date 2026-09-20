"""Narrow client contract for the isolated J1 browser renderer.

This module never opens a network connection.  It can only send one bounded
``render`` request over the renderer's local Unix socket.  The renderer and
its egress proxy are intentionally separate services so J1 never reuses the
CRM browser, its credentials, or its network route.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import stat
from typing import Any

from .j1_fetch import public_url


SCHEMA = "autostop.j1.browser.v1"
DEFAULT_SOCKET_PATH = "/run/autostop-j1-browser/renderer.sock"
DEFAULT_ISOLATION_MARKER = "/run/autostop-j1-browser-attestation/isolation-ready"
DEFAULT_RELEASE_ROOT = "/opt/autostop-manager-releases/current"
_ATTESTATION_HEADER = "autostop-j1-browser-isolation-v2"
_REVISION = re.compile(r"^[0-9a-f]{40,64}$")
MAX_TEXT_CHARS = 50_000
MAX_TIMEOUT_SECONDS = 25
_MAX_MESSAGE_BYTES = 96_000


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        numeric = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(numeric, maximum))


def _public_http_url(value: Any) -> str:
    """Apply a small local boundary before a request reaches the renderer.

    The fetch worker remains the authoritative DLP and robots boundary.  This
    check deliberately does not perform DNS: only the isolated proxy resolves
    and connects to the host, pinning that answer for the connection.
    """

    # Keep browser and static fetch DLP exactly aligned.  ``public_url`` is
    # URL-context aware, so numerical bulletin identifiers remain usable.
    return public_url(str(value or ""))


def _safe_path(value: str, *, limit: int) -> str:
    if not value or not os.path.isabs(value) or len(value.encode()) >= limit:
        return ""
    return value


def _read_root_owned_file(path: str, *, maximum_bytes: int, exact_mode: int | None = None) -> bytes | None:
    """Read a sealed regular file without following a possible attacker symlink."""

    candidate = _safe_path(path, limit=240)
    if not candidate:
        return None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        mode = stat.S_IMODE(info.st_mode)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_mode & 0o022
            or (exact_mode is not None and mode != exact_mode)
        ):
            return None
        content = os.read(descriptor, maximum_bytes + 1)
        return content if len(content) <= maximum_bytes else None
    except OSError:
        return None
    finally:
        os.close(descriptor)


def active_release_revision(release_root: str | None = None) -> tuple[str, str]:
    """Return the sealed current Manager revision or a public readiness reason."""

    configured_root = release_root or os.environ.get("AUTOSTOP_J1_BROWSER_RELEASE_ROOT", DEFAULT_RELEASE_ROOT)
    root = _safe_path(configured_root, limit=220)
    if not root:
        return "", "browser_release_revision_unavailable"
    try:
        resolved_root = Path(root).resolve(strict=True)
    except OSError:
        return "", "browser_release_revision_unavailable"
    revision = _read_root_owned_file(str(resolved_root / "REVISION"), maximum_bytes=80)
    if revision is None:
        return "", "browser_release_revision_unavailable"
    value = revision.decode("ascii", "ignore").strip()
    if not _REVISION.fullmatch(value):
        return "", "browser_release_revision_unavailable"
    return value, ""


def attestation_content(revision: str) -> bytes:
    """Return the exact bounded content accepted for an active release attestation."""

    if not _REVISION.fullmatch(revision):
        return b""
    return f"{_ATTESTATION_HEADER}\nrevision={revision}\n".encode("ascii")


def _attestation_revision(marker_path: str | None = None) -> str:
    candidate = marker_path or os.environ.get("AUTOSTOP_J1_BROWSER_ISOLATION_MARKER", DEFAULT_ISOLATION_MARKER)
    payload = _read_root_owned_file(candidate, maximum_bytes=128, exact_mode=0o600)
    if payload is None:
        return ""
    try:
        header, revision_line, trailing = payload.decode("ascii").split("\n")
    except (UnicodeDecodeError, ValueError):
        return ""
    if header != _ATTESTATION_HEADER or trailing or not revision_line.startswith("revision="):
        return ""
    revision = revision_line.removeprefix("revision=")
    return revision if _REVISION.fullmatch(revision) else ""


def _socket_ready(socket_path: str | None = None) -> bool:
    candidate = socket_path or os.environ.get("AUTOSTOP_J1_BROWSER_SOCKET", DEFAULT_SOCKET_PATH)
    path = _safe_path(candidate, limit=100)
    if not path:
        return False
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISSOCK(info.st_mode) and info.st_uid == 10_001 and stat.S_IMODE(info.st_mode) == 0o600


def browser_status() -> dict[str, Any]:
    """Return a redacted, cheap browser readiness state without network access."""

    active_revision, reason = active_release_revision()
    if not active_revision:
        return {
            "browser_ready": False,
            "browser_reason": reason,
            "browser_release_revision": "",
            "browser_attestation_revision": "",
            "browser_socket_ready": False,
        }
    attested_revision = _attestation_revision()
    if not attested_revision:
        return {
            "browser_ready": False,
            "browser_reason": "browser_isolation_unverified",
            "browser_release_revision": active_revision,
            "browser_attestation_revision": "",
            "browser_socket_ready": _socket_ready(),
        }
    if attested_revision != active_revision:
        return {
            "browser_ready": False,
            "browser_reason": "browser_isolation_stale",
            "browser_release_revision": active_revision,
            "browser_attestation_revision": attested_revision,
            "browser_socket_ready": _socket_ready(),
        }
    socket_ready = _socket_ready()
    return {
        "browser_ready": socket_ready,
        "browser_reason": "" if socket_ready else "browser_socket_unavailable",
        "browser_release_revision": active_revision,
        "browser_attestation_revision": attested_revision,
        "browser_socket_ready": socket_ready,
    }


def _isolation_verified(marker_path: str | None = None) -> bool:
    """Compatibility helper for callers and focused tests with a supplied marker path."""

    if marker_path is None:
        return bool(browser_status()["browser_ready"])
    active_revision, _reason = active_release_revision()
    return bool(active_revision and _attestation_revision(marker_path) == active_revision)


def isolation_verified() -> bool:
    """Return whether the root-owned attestation belongs to the active release."""

    return _isolation_verified()


def _failure(code: str, *, retryable: bool, safe_disabled: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "schema": SCHEMA,
        "error": code,
        "retryable": retryable,
    }
    if safe_disabled:
        result["safe_disabled"] = True
    return result


def _read_message(connection: socket.socket) -> bytes:
    message = bytearray()
    while len(message) <= _MAX_MESSAGE_BYTES:
        chunk = connection.recv(min(4096, _MAX_MESSAGE_BYTES + 1 - len(message)))
        if not chunk:
            break
        message.extend(chunk)
        if b"\n" in chunk:
            line, _separator, _remainder = bytes(message).partition(b"\n")
            return line
    return b""


def _normalize_response(payload: Any, *, requested_url: str, max_chars: int) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return _failure("browser_protocol_invalid", retryable=True)
    if payload.get("ok") is not True:
        code = str(payload.get("error") or "browser_render_failed")[:80]
        return _failure(code, retryable=bool(payload.get("retryable", True)))
    url = _public_http_url(payload.get("url"))
    text = str(payload.get("text") or "")[:max_chars]
    if not url or not text:
        return _failure("browser_response_invalid", retryable=True)
    return {
        "ok": True,
        "schema": SCHEMA,
        "url": url,
        "requested_url": requested_url,
        "title": str(payload.get("title") or "")[:200],
        "text": text,
        "kind": "browser",
        "extraction_method": "browser_dom",
    }


def render_page(
    url: str,
    *,
    max_chars: int = MAX_TEXT_CHARS,
    timeout_seconds: int = 20,
    socket_path: str | None = None,
) -> dict[str, Any]:
    """Ask the isolated renderer to read exactly one public page.

    Socket and renderer failures are deliberately non-fatal: static J1 fetch
    continues and callers can record a transparent partial failure instead of
    falling back to the CRM browser.
    """

    safe_url = _public_http_url(url)
    if not safe_url:
        return _failure("browser_url_invalid", retryable=False)
    if not isolation_verified():
        return _failure("browser_isolation_unverified", retryable=True, safe_disabled=True)
    bounded_chars = _bounded_int(max_chars, default=MAX_TEXT_CHARS, minimum=80, maximum=MAX_TEXT_CHARS)
    timeout = _bounded_int(timeout_seconds, default=20, minimum=1, maximum=MAX_TIMEOUT_SECONDS)
    target_socket = socket_path or os.environ.get("AUTOSTOP_J1_BROWSER_SOCKET", DEFAULT_SOCKET_PATH)
    if not target_socket or not os.path.isabs(target_socket) or len(target_socket.encode()) >= 100:
        return _failure("browser_socket_invalid", retryable=False, safe_disabled=True)
    request = {
        "schema": SCHEMA,
        "operation": "render",
        "url": safe_url,
        "max_chars": bounded_chars,
        "timeout_seconds": timeout,
    }
    try:
        encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout + 2)
            connection.connect(target_socket)
            connection.sendall(encoded)
            raw_response = _read_message(connection)
    except (OSError, TimeoutError):
        return _failure("browser_unavailable", retryable=True, safe_disabled=True)
    try:
        payload = json.loads(raw_response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _failure("browser_protocol_invalid", retryable=True)
    return _normalize_response(payload, requested_url=safe_url, max_chars=bounded_chars)


__all__ = [
    "DEFAULT_SOCKET_PATH",
    "MAX_TEXT_CHARS",
    "SCHEMA",
    "active_release_revision",
    "attestation_content",
    "browser_status",
    "isolation_verified",
    "render_page",
]
