"""Focused unit contracts for J1's isolated renderer and egress proxy."""

from __future__ import annotations

import json
import socket
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from autostop_manager import j1_browser_proxy as proxy
from autostop_manager import j1_browser_renderer as renderer


class _MemorySocket:
    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self._chunks = list(chunks or [])
        self.sent: list[bytes] = []
        self.timeouts: list[float] = []

    def __enter__(self) -> _MemorySocket:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def recv(self, _size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def sendall(self, value: bytes) -> None:
        self.sent.append(value)

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)


def test_proxy_target_parsing_keeps_only_allowed_authority_and_http() -> None:
    assert proxy._target_from_authority("Example.ORG:443", allowed_ports=frozenset({443})) == proxy.PublicTarget(
        "example.org", 443
    )
    assert proxy._target_from_http_url("http://Example.ORG/a/b?lang=ru") == (
        proxy.PublicTarget("example.org", 80),
        "/a/b?lang=ru",
    )

    for authority in (
        "user@example.org:443",
        "example.org/path",
        "example.org?x=1",
        "localhost:443",
        "local:443",
        "internal:443",
        "onion:443",
        "example.org:80",
    ):
        with pytest.raises(proxy.ProxyRequestError):
            proxy._target_from_authority(authority, allowed_ports=frozenset({443}))
    for value in (
        "https://example.org/page",
        "http://user@example.org/page",
        "http://example.org:8080/page",
        "http://example.org/page#fragment",
        "http://internal/page",
        "http://local/page",
        "http://onion/page",
    ):
        with pytest.raises(proxy.ProxyRequestError):
            proxy._target_from_http_url(value)


def test_proxy_dns_rejects_mixed_answers_and_deduplicates_checked_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 443, 0, 0)),
    ]
    monkeypatch.setattr(proxy.socket, "getaddrinfo", lambda *_args, **_kwargs: rows)
    resolved = proxy.resolve_public_addresses("example.org", 443)
    assert [item[3][0] for item in resolved] == ["8.8.8.8", "2606:4700:4700::1111"]

    monkeypatch.setattr(
        proxy.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [*rows, (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(proxy.ProxyRequestError, match="unsafe_dns_answer"):
        proxy.resolve_public_addresses("example.org", 443)


def test_proxy_connect_pins_checked_numeric_answer_and_closes_failed_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    candidates = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, ("8.8.8.8", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, ("1.1.1.1", 443)),
    ]
    monkeypatch.setattr(proxy, "resolve_public_addresses", lambda *_args: candidates)

    class Connection:
        def __init__(self, should_fail: bool) -> None:
            self.should_fail = should_fail
            self.connected: tuple[Any, ...] | None = None
            self.timeout: float | None = None
            self.closed = False

        def settimeout(self, value: float) -> None:
            self.timeout = value

        def connect(self, address: tuple[Any, ...]) -> None:
            self.connected = address
            if self.should_fail:
                raise OSError("first address unreachable")

        def close(self) -> None:
            self.closed = True

    created = [Connection(True), Connection(False)]
    pending = list(created)
    monkeypatch.setattr(proxy.socket, "socket", lambda *_args: pending.pop(0))

    connected = proxy.connect_public(proxy.PublicTarget("example.org", 443), timeout=2.5)
    assert connected is created[1]
    assert connected.connected == ("1.1.1.1", 443)
    assert connected.timeout == 2.5
    assert created[0].closed is True
    assert pending == []


def test_proxy_header_reader_and_parser_reject_request_smuggling_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    inbound = _MemorySocket([b"GET http://example.org/a HTTP/1.1\r\nHost: example.org\r\n", b"\r\nbody"])
    headers, remainder = proxy._read_headers(inbound)  # type: ignore[arg-type]
    assert remainder == b"body"
    assert proxy._parse_headers(headers) == ("GET", "http://example.org/a", {"host": "example.org"})

    for raw, expected in (
        (b"GET http://example.org/ HTTP/2.0\r\n\r\n", "request_invalid"),
        (b"GET http://example.org/ HTTP/1.1\r\nHost: a\r\nhost: b\r\n\r\n", "request_invalid"),
        (b"GET http://example.org/ HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n", "request_not_allowed"),
        (b"GET http://example.org/ HTTP/1.1\r\nContent-Length: 1\r\n\r\n", "request_not_allowed"),
    ):
        with pytest.raises(proxy.ProxyRequestError, match=expected):
            proxy._parse_headers(raw)

    monkeypatch.setattr(proxy, "MAX_HEADER_BYTES", 8)
    with pytest.raises(proxy.ProxyRequestError, match="header_too_large"):
        proxy._read_headers(_MemorySocket([b"012345678"]))  # type: ignore[arg-type]


def test_proxy_forwards_only_sanitized_http_and_pinned_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    class Upstream(_MemorySocket):
        def __init__(self) -> None:
            super().__init__()
            self.closed = False

        def __exit__(self, *_args: object) -> None:
            self.closed = True

    upstream = Upstream()
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(proxy, "connect_public", lambda target: calls.append(("connect", target)) or upstream)
    monkeypatch.setattr(proxy, "_relay", lambda *args, **kwargs: calls.append(("relay", *args, kwargs)))

    handler = object.__new__(proxy._J1ProxyHandler)
    handler.request = _MemorySocket()
    handler._forward_http(
        "GET",
        "http://example.org/technical?page=2",
        {"accept": "text/plain", "accept-language": "ru", "cookie": "must-not-leak"},
    )
    outgoing = b"".join(upstream.sent).decode("iso-8859-1")
    assert outgoing.startswith("GET /technical?page=2 HTTP/1.1\r\n")
    assert "Host: example.org" in outgoing
    assert "Accept-Language: ru" in outgoing
    assert "cookie" not in outgoing.casefold()

    handler._connect_tunnel("example.org:443", b"client-hello")
    assert b"HTTP/1.1 200 Connection Established" in b"".join(handler.request.sent)
    assert calls[-1][-1] == {"initial_to_right": b"client-hello"}


def test_proxy_handler_drops_untrusted_peer_before_reading_request() -> None:
    handler = object.__new__(proxy._J1ProxyHandler)
    request = _MemorySocket([b"GET http://example.org/ HTTP/1.1\r\n\r\n"])
    handler.request = request
    handler.client_address = ("10.0.0.4", 12345)
    handler.server = SimpleNamespace(slots=threading.BoundedSemaphore(1), allowed_peer_networks=())

    handler.handle()
    assert request.sent == []
    assert request.timeouts == []


def test_renderer_proxy_boundary_and_wire_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOSTOP_J1_BROWSER_CONTROL_CIDR", "172.31.250.0/29")
    assert renderer._trusted_proxy_url("http://172.31.250.3:18890/") == "http://172.31.250.3:18890"
    assert renderer._trusted_proxy_url("http://127.0.0.1:18890") == "http://127.0.0.1:18890"
    for value in (
        "http://8.8.8.8:18890",
        "https://172.31.250.3:18890",
        "http://user@172.31.250.3:18890",
        "http://172.31.250.3:8080",
        "http://172.31.250.3:18890/path",
    ):
        assert renderer._trusted_proxy_url(value) == ""

    request = {
        "schema": renderer.SCHEMA,
        "operation": "render",
        "url": "https://example.org/technical",
        "max_chars": 1,
        "timeout_seconds": 999,
    }
    assert renderer._request_from_wire(json.dumps(request).encode()) == ("https://example.org/technical", 80, 25)
    assert renderer._request_from_wire(b"not-json") is None
    assert renderer._request_from_wire(json.dumps({**request, "extra": True}).encode()) is None
    assert (
        renderer._request_from_wire(json.dumps({**request, "url": "https://example.org/ZZZ00000000000000"}).encode())
        is None
    )


def test_renderer_filters_dom_and_returns_explicit_dynamic_or_captcha_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    title, text = renderer._dom_to_text(
        b"<title>Useful title</title><nav>navigation</nav><script>secret()</script><p>Public technical text</p>",
        max_chars=200,
    )
    assert title == "Useful title"
    assert "Public technical text" in text
    assert "navigation" not in text and "secret" not in text

    monkeypatch.setattr(renderer, "_run_chromium", lambda *_args, **_kwargs: (b"<p>short</p>", ""))
    assert renderer.render("https://example.org/page")["error"] == "browser_empty_or_dynamic"

    captcha = b"<p>CAPTCHA please verify you are human " + b"x" * 100 + b"</p>"
    monkeypatch.setattr(renderer, "_run_chromium", lambda *_args, **_kwargs: (captcha, ""))
    result = renderer.render("https://example.org/page")
    assert result == {"ok": False, "schema": renderer.SCHEMA, "error": "requires_human", "retryable": False}


def test_renderer_ipc_rejects_invalid_client_and_emits_bounded_valid_response(monkeypatch: pytest.MonkeyPatch) -> None:
    server = renderer.RendererServer("/tmp/j1-renderer-test.sock")

    rejected = _MemorySocket()
    monkeypatch.setattr(renderer, "_peer_allowed", lambda _connection: False)
    server._handle_connection(rejected)  # type: ignore[arg-type]
    assert json.loads(b"".join(rejected.sent).decode())["error"] == "browser_client_rejected"

    valid_request = (
        json.dumps(
            {
                "schema": renderer.SCHEMA,
                "operation": "render",
                "url": "https://example.org/page",
                "max_chars": 200,
                "timeout_seconds": 3,
            }
        ).encode()
        + b"\n"
    )
    accepted = _MemorySocket([valid_request])
    monkeypatch.setattr(renderer, "_peer_allowed", lambda _connection: True)
    monkeypatch.setattr(
        renderer,
        "render",
        lambda *_args, **_kwargs: {
            "ok": True,
            "schema": renderer.SCHEMA,
            "url": "https://example.org/page",
            "title": "Page",
            "text": "public text",
        },
    )
    server._handle_connection(accepted)  # type: ignore[arg-type]
    response = json.loads(b"".join(accepted.sent).decode())
    assert response["ok"] is True and response["url"] == "https://example.org/page"

    oversized = _MemorySocket()
    renderer._write_response(oversized, {"ok": True, "text": "x" * (renderer._MAX_RESPONSE_BYTES + 1)})  # type: ignore[arg-type]
    assert json.loads(b"".join(oversized.sent).decode())["error"] == "browser_response_too_large"


def test_renderer_peer_credentials_and_binary_gate_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    class CredentialsSocket:
        def __init__(self, uid: int) -> None:
            self.uid = uid

        def getsockopt(self, *_args: object) -> bytes:
            import struct

            return struct.pack("3i", 123, self.uid, 456)

    monkeypatch.setenv("AUTOSTOP_J1_BROWSER_ALLOWED_UID", "1001,1002")
    assert renderer._peer_allowed(CredentialsSocket(1002))  # type: ignore[arg-type]
    assert not renderer._peer_allowed(CredentialsSocket(0))  # type: ignore[arg-type]

    monkeypatch.setattr(renderer.os, "geteuid", lambda: 0)
    assert renderer._run_chromium("https://example.org/page", timeout_seconds=2) == (
        b"",
        "browser_binary_unavailable",
    )


def test_renderer_line_reader_discards_overlong_and_extra_wire_data() -> None:
    assert renderer._read_line(_MemorySocket([b'{"ok":true}\nignored'])) == b'{"ok":true}'  # type: ignore[arg-type]
    assert renderer._read_line(_MemorySocket([b"x" * (renderer.MAX_REQUEST_BYTES + 1)])) == b""  # type: ignore[arg-type]
