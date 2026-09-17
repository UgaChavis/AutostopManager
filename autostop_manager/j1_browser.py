"""Narrow client contract for the isolated J1 browser renderer.

This module never opens a network connection.  It can only send one bounded
``render`` request over the renderer's local Unix socket.  The renderer and
its egress proxy are intentionally separate services so J1 never reuses the
CRM browser, its credentials, or its network route.
"""

from __future__ import annotations

import json
import os
import socket
import stat
from typing import Any

from .j1_fetch import public_url


SCHEMA = "autostop.j1.browser.v1"
DEFAULT_SOCKET_PATH = "/run/autostop-j1-browser/renderer.sock"
DEFAULT_ISOLATION_MARKER = "/run/autostop-j1-browser/isolation-ready"
_ISOLATION_MARKER_CONTENT = b"autostop-j1-browser-isolation-v1\n"
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


def _isolation_verified(marker_path: str | None = None) -> bool:
    """Require a root-owned deployment attestation before renderer use.

    The renderer/proxy code is shipped with J1, but a browser socket alone is
    not sufficient proof that its Docker networks were checked in the current
    release.  The deployment gate writes this bounded, root-owned marker only
    after its isolation probes succeed.  Missing or malformed markers keep the
    browser path fail-closed.
    """

    candidate = marker_path or os.environ.get("AUTOSTOP_J1_BROWSER_ISOLATION_MARKER", DEFAULT_ISOLATION_MARKER)
    if not candidate or not os.path.isabs(candidate) or len(candidate.encode()) >= 240:
        return False
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
    except OSError:
        return False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            return False
        return os.read(descriptor, len(_ISOLATION_MARKER_CONTENT) + 1) == _ISOLATION_MARKER_CONTENT
    except OSError:
        return False
    finally:
        os.close(descriptor)


def isolation_verified() -> bool:
    """Return whether the deployment's root-owned browser attestation is valid."""

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


__all__ = ["DEFAULT_SOCKET_PATH", "MAX_TEXT_CHARS", "SCHEMA", "isolation_verified", "render_page"]
