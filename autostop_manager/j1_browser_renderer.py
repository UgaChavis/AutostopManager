"""Unprivileged, Unix-socket-only renderer for J1 dynamic public pages.

The service uses Chromium only to navigate to one supplied URL and dump the
resulting DOM.  It has no CDP endpoint, no user profile, no persistent cookies,
no download API and no general JavaScript execution interface.  Its container
has only the internal control network to the companion egress proxy.
"""

from __future__ import annotations

import argparse
from contextlib import suppress
from html.parser import HTMLParser
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import tempfile
from threading import BoundedSemaphore, Thread
from typing import Any
from urllib.parse import urlsplit

from .j1_browser import _public_http_url
from .j1_fetch import USER_AGENT, redact_sensitive


SCHEMA = "autostop.j1.browser.v1"
DEFAULT_SOCKET_PATH = "/run/autostop-j1-browser/renderer.sock"
DEFAULT_PROXY_URL = "http://127.0.0.1:18890"
DEFAULT_CONTROL_CIDR = "172.31.250.0/29"
DEFAULT_CHROMIUM_BINARY = "/usr/bin/chromium-browser"
MAX_TEXT_CHARS = 50_000
MAX_DOM_BYTES = 2_000_000
MAX_REQUEST_BYTES = 4096
MAX_TIMEOUT_SECONDS = 25
_MAX_RESPONSE_BYTES = 96_000
_MAX_CONCURRENT_RENDERS = 2


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        numeric = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(numeric, maximum))


def _redact_text(value: str, *, limit: int) -> str:
    return redact_sensitive(value, limit=limit)


def _error(code: str, *, retryable: bool) -> dict[str, Any]:
    return {"ok": False, "schema": SCHEMA, "error": code, "retryable": retryable}


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._hidden = 0
        self._in_title = False
        self.title: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        _ = attrs
        normalized = tag.casefold()
        if normalized in {"script", "style", "svg", "noscript", "nav", "footer", "header", "template"}:
            self._hidden += 1
        if normalized == "title":
            self._in_title = True
        if normalized in {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "article", "section"}:
            self.text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized in {"script", "style", "svg", "noscript", "nav", "footer", "header", "template"} and self._hidden:
            self._hidden -= 1
        if normalized == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
        if not self._hidden:
            self.text.append(data)


def _dom_to_text(dom: bytes, *, max_chars: int) -> tuple[str, str]:
    decoded = dom.decode("utf-8", "replace")
    parser = _VisibleText()
    parser.feed(decoded)
    parser.close()
    title = _redact_text(" ".join(" ".join(parser.title).split()), limit=200)
    text = re.sub(r"[ \t]+", " ", "".join(parser.text))
    text = re.sub(r"\n[\s\n]*\n", "\n", text).strip()
    return title, _redact_text(text, limit=max_chars)


def chromium_available(binary: str | None = None) -> bool:
    """Return false for a distro wrapper with no installed Chromium runtime."""

    command = binary or os.environ.get("AUTOSTOP_J1_CHROMIUM_BINARY", DEFAULT_CHROMIUM_BINARY)
    if not command or not os.path.isabs(command) or not os.path.isfile(command) or not os.access(command, os.X_OK):
        return False
    try:
        result = subprocess.run(
            [command, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _trusted_proxy_url(value: str) -> str:
    """Accept only the fixed local/control-network proxy endpoint.

    The renderer has no public egress network.  Restricting this value as well
    keeps a compromised environment from redirecting Chromium to another local
    service or a direct public proxy.
    """

    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        control = ipaddress.ip_network(os.environ.get("AUTOSTOP_J1_BROWSER_CONTROL_CIDR", DEFAULT_CONTROL_CIDR))
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return ""
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port != 18_890
        or not (address.is_loopback or address in control)
    ):
        return ""
    return f"http://{parsed.netloc}"


def _chromium_argv(*, binary: str, url: str, profile_dir: str, proxy_url: str, wait_ms: int) -> list[str]:
    """Fixed flags only; callers cannot add Chromium switches or scripts."""

    return [
        binary,
        "--headless=new",
        "--incognito",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-pings",
        "--disable-background-networking",
        "--disable-quic",
        "--disable-component-update",
        "--disable-sync",
        "--disable-extensions",
        "--disable-default-apps",
        "--disable-translate",
        "--deny-permission-prompts",
        "--password-store=basic",
        "--use-mock-keychain",
        "--disable-breakpad",
        "--disable-crash-reporter",
        "--disable-domain-reliability",
        "--disable-features=AutofillServerCommunication,DownloadBubble,DownloadBubbleV2,MediaRouter,OptimizationHints,Translate",
        f"--proxy-server={proxy_url}",
        "--proxy-bypass-list=<-loopback>",
        f"--user-agent={USER_AGENT}",
        f"--user-data-dir={profile_dir}",
        f"--disk-cache-dir={profile_dir}/cache",
        f"--data-path={profile_dir}/data",
        f"--homedir={profile_dir}/home",
        "--window-size=1280,1800",
        f"--virtual-time-budget={wait_ms}",
        "--run-all-compositor-stages-before-draw",
        "--dump-dom",
        url,
    ]


def _write_ephemeral_preferences(profile_dir: str) -> None:
    """Disable cookies and automatic downloads in the disposable profile."""

    default = Path(profile_dir) / "Default"
    default.mkdir(mode=0o700)
    preferences = {
        "profile": {"default_content_setting_values": {"cookies": 2, "automatic_downloads": 2, "popups": 2}},
        "download": {"default_directory": "/dev/null", "prompt_for_download": False},
    }
    path = default / "Preferences"
    path.write_text(json.dumps(preferences, separators=(",", ":")), encoding="utf-8")
    os.chmod(path, 0o600)


def _child_limits() -> None:
    """Keep Chromium's dumped DOM bounded; cgroup limits the renderer itself."""

    import resource

    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_DOM_BYTES + 65_536, MAX_DOM_BYTES + 65_536))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))


def _run_chromium(url: str, *, timeout_seconds: int) -> tuple[bytes, str]:
    binary = os.environ.get("AUTOSTOP_J1_CHROMIUM_BINARY", DEFAULT_CHROMIUM_BINARY)
    proxy_url = _trusted_proxy_url(os.environ.get("AUTOSTOP_J1_BROWSER_PROXY", DEFAULT_PROXY_URL))
    if os.geteuid() == 0 or not chromium_available(binary):
        return b"", "browser_binary_unavailable"
    if not proxy_url:
        return b"", "browser_proxy_invalid"
    wait_ms = min(max((timeout_seconds - 3) * 1000, 500), 5_000)
    try:
        with tempfile.TemporaryDirectory(prefix="j1-browser-") as profile_dir, tempfile.TemporaryFile() as output:
            _write_ephemeral_preferences(profile_dir)
            process = subprocess.Popen(
                _chromium_argv(
                    binary=binary,
                    url=url,
                    profile_dir=profile_dir,
                    proxy_url=proxy_url,
                    wait_ms=wait_ms,
                ),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                preexec_fn=_child_limits,
            )
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
                return b"", "browser_timeout"
            if process.returncode != 0:
                return b"", "browser_render_failed"
            output.seek(0)
            dom = output.read(MAX_DOM_BYTES + 1)
    except (OSError, subprocess.SubprocessError):
        return b"", "browser_render_failed"
    if len(dom) > MAX_DOM_BYTES:
        return b"", "browser_document_too_large"
    return dom, ""


def render(url: str, *, max_chars: int = MAX_TEXT_CHARS, timeout_seconds: int = 20) -> dict[str, Any]:
    """Render one safe public URL and return text only, never DOM or cookies."""

    safe_url = _public_http_url(url)
    if not safe_url:
        return _error("browser_url_invalid", retryable=False)
    bounded_chars = _bounded_int(max_chars, default=MAX_TEXT_CHARS, minimum=80, maximum=MAX_TEXT_CHARS)
    timeout = _bounded_int(timeout_seconds, default=20, minimum=1, maximum=MAX_TIMEOUT_SECONDS)
    dom, error = _run_chromium(safe_url, timeout_seconds=timeout)
    if error:
        return _error(error, retryable=error not in {"browser_url_invalid", "browser_proxy_invalid"})
    title, text = _dom_to_text(dom, max_chars=bounded_chars)
    if len(text) < 80:
        return _error("browser_empty_or_dynamic", retryable=False)
    lowered = text[:3000].casefold()
    if any(marker in lowered for marker in ("captcha", "введите капчу", "sign in to continue", "please log in")):
        return _error("requires_human", retryable=False)
    return {
        "ok": True,
        "schema": SCHEMA,
        "url": safe_url,
        "title": title,
        "text": text,
        "kind": "browser",
        "extraction_method": "browser_dom",
    }


def _peer_allowed(connection: socket.socket) -> bool:
    if not hasattr(socket, "SO_PEERCRED"):
        return False
    try:
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", credentials)
    except OSError:
        return False
    raw_allowed = os.environ.get("AUTOSTOP_J1_BROWSER_ALLOWED_UID", "0")
    allowed = {int(item) for item in raw_allowed.split(",") if item.strip().isdigit()}
    return uid in allowed


def _read_line(connection: socket.socket) -> bytes:
    payload = bytearray()
    while len(payload) <= MAX_REQUEST_BYTES:
        chunk = connection.recv(min(2048, MAX_REQUEST_BYTES + 1 - len(payload)))
        if not chunk:
            break
        payload.extend(chunk)
        if b"\n" in chunk:
            return bytes(payload).partition(b"\n")[0]
    return b""


def _request_from_wire(raw: bytes) -> tuple[str, int, int] | None:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"schema", "operation", "url", "max_chars", "timeout_seconds"}:
        return None
    if payload.get("schema") != SCHEMA or payload.get("operation") != "render":
        return None
    url = _public_http_url(payload.get("url"))
    if not url:
        return None
    return (
        url,
        _bounded_int(payload.get("max_chars"), default=MAX_TEXT_CHARS, minimum=80, maximum=MAX_TEXT_CHARS),
        _bounded_int(payload.get("timeout_seconds"), default=20, minimum=1, maximum=MAX_TIMEOUT_SECONDS),
    )


def _write_response(connection: socket.socket, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_RESPONSE_BYTES:
        encoded = json.dumps(_error("browser_response_too_large", retryable=True)).encode("utf-8")
    connection.sendall(encoded + b"\n")


class RendererServer:
    """A small, root-client-only AF_UNIX server with two render slots."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self._slots = BoundedSemaphore(_MAX_CONCURRENT_RENDERS)
        self._listener: socket.socket | None = None

    def serve_forever(self) -> None:
        if os.geteuid() == 0:
            raise RuntimeError("renderer_must_not_run_as_root")
        path = Path(self.socket_path)
        if not path.is_absolute() or not path.parent.is_dir():
            raise RuntimeError("renderer_socket_path_invalid")
        if path.exists() or path.is_socket():
            path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener = listener
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen(8)
        try:
            while True:
                connection, _address = listener.accept()
                Thread(target=self._handle_connection, args=(connection,), daemon=True).start()
        finally:
            listener.close()
            with suppress(FileNotFoundError):
                path.unlink()

    def _handle_connection(self, connection: socket.socket) -> None:
        with connection:
            connection.settimeout(MAX_TIMEOUT_SECONDS + 3)
            if not _peer_allowed(connection):
                _write_response(connection, _error("browser_client_rejected", retryable=False))
                return
            request = _request_from_wire(_read_line(connection))
            if request is None:
                _write_response(connection, _error("browser_request_invalid", retryable=False))
                return
            if not self._slots.acquire(blocking=False):
                _write_response(connection, _error("browser_busy", retryable=True))
                return
            try:
                url, max_chars, timeout_seconds = request
                _write_response(connection, render(url, max_chars=max_chars, timeout_seconds=timeout_seconds))
            finally:
                self._slots.release()


def main() -> None:
    parser = argparse.ArgumentParser(description="AutoStop J1 isolated browser renderer")
    parser.add_argument("command", choices=("serve", "probe"))
    parser.add_argument("--socket", default=os.environ.get("AUTOSTOP_J1_BROWSER_SOCKET", DEFAULT_SOCKET_PATH))
    args = parser.parse_args()
    if args.command == "probe":
        print(json.dumps({"ok": chromium_available(), "schema": SCHEMA}, ensure_ascii=False))
        return
    RendererServer(args.socket).serve_forever()


if __name__ == "__main__":
    main()
