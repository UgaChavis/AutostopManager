"""Extra deterministic coverage for J1's isolated egress proxy.

These contracts use in-memory sockets and monkeypatched DNS/server factories;
they never create a listener or make a network request.
"""

from __future__ import annotations

import socket
import socketserver
import sys
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from autostop_manager import j1_browser_proxy as proxy


class _MemorySocket:
    def __init__(self, chunks: list[bytes | BaseException] | None = None, *, fail_send: bool = False) -> None:
        self._chunks = list(chunks or [])
        self.fail_send = fail_send
        self.sent: list[bytes] = []
        self.timeouts: list[float] = []
        self.closed = False

    def __enter__(self) -> _MemorySocket:
        return self

    def __exit__(self, *_args: object) -> None:
        self.closed = True

    def recv(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        value = self._chunks.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def sendall(self, value: bytes) -> None:
        if self.fail_send:
            raise OSError("closed")
        self.sent.append(value)

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def close(self) -> None:
        self.closed = True


def _handler(*, peer: str = "127.0.0.1", slots: object | None = None) -> proxy._J1ProxyHandler:
    handler = object.__new__(proxy._J1ProxyHandler)
    handler.request = _MemorySocket()
    handler.client_address = (peer, 4567)
    handler.server = SimpleNamespace(
        slots=slots if slots is not None else threading.BoundedSemaphore(1),
        allowed_peer_networks=(),
    )
    return handler


def test_proxy_dns_and_connect_fail_closed_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proxy.socket, "getaddrinfo", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("dns")))
    with pytest.raises(proxy.ProxyRequestError, match="dns_failed"):
        proxy.resolve_public_addresses("example.org", 443)

    monkeypatch.setattr(
        proxy.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("not-an-ip", 443))],
    )
    with pytest.raises(proxy.ProxyRequestError, match="dns_invalid"):
        proxy.resolve_public_addresses("example.org", 443)

    monkeypatch.setattr(proxy.socket, "getaddrinfo", lambda *_args, **_kwargs: [])
    with pytest.raises(proxy.ProxyRequestError, match="dns_failed"):
        proxy.resolve_public_addresses("example.org", 443)

    class _FailingConnection(_MemorySocket):
        def connect(self, _address: tuple[Any, ...]) -> None:
            raise OSError("unreachable")

    connection = _FailingConnection()
    monkeypatch.setattr(
        proxy,
        "resolve_public_addresses",
        lambda *_args: [(socket.AF_INET, socket.SOCK_STREAM, 6, ("8.8.8.8", 443))],
    )
    monkeypatch.setattr(proxy.socket, "socket", lambda *_args: connection)
    with pytest.raises(proxy.ProxyRequestError, match="connect_failed"):
        proxy.connect_public(proxy.PublicTarget("example.org", 443))
    assert connection.closed is True


def test_proxy_parsing_rejects_malformed_authorities_and_headers() -> None:
    for authority in ("", "host with-space", "x" * 301, "[broken", "example.org:bad"):
        with pytest.raises(proxy.ProxyRequestError):
            proxy._target_from_authority(authority, allowed_ports=frozenset({443}))
    for value in ("http://[broken", "http://example.org:bad", "ftp://example.org/path"):
        with pytest.raises(proxy.ProxyRequestError):
            proxy._target_from_http_url(value)

    with pytest.raises(proxy.ProxyRequestError, match="header_too_large"):
        proxy._read_headers(_MemorySocket())  # type: ignore[arg-type]
    for raw in (
        b"GET-only\r\n\r\n",
        b"GET http://example.org/ HTTP/1.1\r\nbroken\r\n\r\n",
    ):
        with pytest.raises(proxy.ProxyRequestError, match="request_invalid"):
            proxy._parse_headers(raw)


def test_proxy_relay_stops_on_idle_eof_errors_and_byte_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    left = _MemorySocket([b"part"])
    right = _MemorySocket()
    choices = iter((([left], [], []), ([], [], [])))
    monkeypatch.setattr(proxy.select, "select", lambda *_args: next(choices))
    proxy._relay(left, right, initial_to_right=b"initial")  # type: ignore[arg-type]
    assert right.sent == [b"initial", b"part"]
    assert left.timeouts == [proxy.IDLE_TIMEOUT_SECONDS]
    assert right.timeouts == [proxy.IDLE_TIMEOUT_SECONDS]

    for input_value, failing_destination in ((b"", False), (OSError("read"), False), (b"data", True)):
        source = _MemorySocket([input_value])
        destination = _MemorySocket(fail_send=failing_destination)
        monkeypatch.setattr(proxy.select, "select", lambda *_args, source=source: ([source], [], []))
        proxy._relay(source, destination)  # type: ignore[arg-type]

    source = _MemorySocket([b"too-large"])
    destination = _MemorySocket()
    monkeypatch.setattr(proxy, "MAX_TUNNEL_BYTES", 1)
    monkeypatch.setattr(proxy.select, "select", lambda *_args: ([source], [], []))
    proxy._relay(source, destination)  # type: ignore[arg-type]
    assert destination.sent == []


def test_proxy_handler_routes_allowed_requests_and_returns_403_for_invalid_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = _handler(slots=object())
    blocked.handle()
    assert blocked.request.sent == []

    forwarded: list[tuple[str, str, dict[str, str]]] = []
    http = _handler()
    monkeypatch.setattr(proxy, "_read_headers", lambda _request: (b"raw", b""))
    monkeypatch.setattr(proxy, "_parse_headers", lambda _raw: ("GET", "http://example.org/", {"accept": "text/plain"}))
    http._forward_http = lambda method, requested, headers: forwarded.append((method, requested, headers))  # type: ignore[method-assign]
    http.handle()
    assert forwarded == [("GET", "http://example.org/", {"accept": "text/plain"})]
    assert http.request.timeouts == [proxy.CONNECT_TIMEOUT_SECONDS]

    tunneled: list[tuple[str, bytes]] = []
    connect = _handler()
    monkeypatch.setattr(proxy, "_parse_headers", lambda _raw: ("CONNECT", "example.org:443", {}))
    monkeypatch.setattr(proxy, "_read_headers", lambda _request: (b"raw", b"hello"))
    connect._connect_tunnel = lambda authority, remainder: tunneled.append((authority, remainder))  # type: ignore[method-assign]
    connect.handle()
    assert tunneled == [("example.org:443", b"hello")]

    for method, remainder in (("GET", b"body"), ("POST", b"")):
        rejected = _handler()
        monkeypatch.setattr(proxy, "_read_headers", lambda _request, remainder=remainder: (b"raw", remainder))
        monkeypatch.setattr(proxy, "_parse_headers", lambda _raw, method=method: (method, "http://example.org/", {}))
        rejected.handle()
        assert b"403 Forbidden" in b"".join(rejected.request.sent)

    errored = _handler()
    monkeypatch.setattr(proxy, "_read_headers", lambda _request: (_ for _ in ()).throw(proxy.ProxyRequestError("bad")))
    errored.handle()
    assert b"403 Forbidden" in b"".join(errored.request.sent)


def test_proxy_helpers_cover_empty_headers_invalid_peers_and_server_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    target = proxy.PublicTarget("example.org", 80)
    no_language = proxy._safe_http_headers({}, target).decode("iso-8859-1")
    assert "Accept-Language" not in no_language
    assert proxy._allowed_peer("not-an-address", ()) is False
    with pytest.raises(ValueError, match="allowed_peer_invalid"):
        proxy._parse_allowed_peers(["not-a-cidr"])

    initialized: dict[str, object] = {}

    def fake_tcp_init(instance: object, address: tuple[str, int], handler_cls: object) -> None:
        initialized.update(instance=instance, address=address, handler_cls=handler_cls)

    monkeypatch.setattr(socketserver.TCPServer, "__init__", fake_tcp_init)
    server = proxy._J1ProxyServer(("127.0.0.1", 18890), allowed_peer_networks=())
    assert initialized["address"] == ("127.0.0.1", 18890)
    assert isinstance(server.slots, threading.BoundedSemaphore)

    calls: dict[str, object] = {}

    class _Server:
        def __init__(self, address: tuple[str, int], *, allowed_peer_networks: tuple[proxy.Network, ...]) -> None:
            calls.update(address=address, allowed_peer_networks=allowed_peer_networks)

        def __enter__(self) -> _Server:
            return self

        def __exit__(self, *_args: object) -> None:
            calls["closed"] = True

        def serve_forever(self, *, poll_interval: float) -> None:
            calls["poll_interval"] = poll_interval

    monkeypatch.setattr(proxy, "_J1ProxyServer", _Server)
    proxy.serve(host="127.0.0.1", port=18890, allowed_peers=["172.31.250.2/32"])
    assert calls["address"] == ("127.0.0.1", 18890)
    assert calls["poll_interval"] == 0.5 and calls["closed"] is True

    for host, port in (("invalid", 18890), ("127.0.0.1", 80)):
        with pytest.raises(ValueError, match="listen_address_invalid"):
            proxy.serve(host=host, port=port)


def test_proxy_main_dispatches_only_parsed_local_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatched: dict[str, object] = {}
    monkeypatch.setattr(proxy, "serve", lambda **kwargs: dispatched.update(kwargs))
    monkeypatch.setattr(
        sys,
        "argv",
        ["autostop-j1-browser-proxy", "serve", "--host", "::1", "--port", "18891", "--allowed-peer", "172.31.250.2/32"],
    )
    proxy.main()
    assert dispatched == {"host": "::1", "port": 18891, "allowed_peers": ["172.31.250.2/32"]}
