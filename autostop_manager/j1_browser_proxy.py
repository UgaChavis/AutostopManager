"""Restricted egress proxy used only by the isolated J1 renderer.

The proxy does not proxy arbitrary protocols.  It accepts local HTTP GET/HEAD
and HTTPS CONNECT requests, resolves the requested hostname itself, rejects an
entire mixed/private DNS answer, and connects to the selected numeric address.
That pins DNS for each tunnel and prevents DNS rebinding after validation.
"""

from __future__ import annotations

import argparse
from contextlib import suppress
import ipaddress
import select
import socket
import socketserver
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .j1_fetch import USER_AGENT


DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 18_890
CONNECT_TIMEOUT_SECONDS = 10.0
IDLE_TIMEOUT_SECONDS = 15.0
MAX_HEADER_BYTES = 16_384
MAX_TUNNEL_BYTES = 16 * 1024 * 1024
MAX_CLIENTS = 24
_ALLOWED_HTTP_METHODS = frozenset({"GET", "HEAD"})
Network = ipaddress.IPv4Network | ipaddress.IPv6Network


class ProxyRequestError(ValueError):
    """A client request or target cannot be forwarded safely."""


@dataclass(frozen=True)
class PublicTarget:
    host: str
    port: int


def _is_allowed_host(host: str) -> bool:
    normalized = host.casefold().rstrip(".")
    return bool(normalized) and not (
        normalized in {"localhost", "localhost.localdomain", "local", "internal", "onion"}
        or normalized.endswith((".localhost", ".local", ".internal", ".onion"))
    )


def _target_from_authority(authority: str, *, allowed_ports: frozenset[int]) -> PublicTarget:
    raw = authority.strip()
    if not raw or len(raw) > 300 or any(char.isspace() for char in raw):
        raise ProxyRequestError("invalid_authority")
    try:
        parsed = urlsplit(f"https://{raw}")
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ProxyRequestError("invalid_authority") from exc
    if (
        not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ProxyRequestError("invalid_authority")
    target_port = port or 443
    if target_port not in allowed_ports or not _is_allowed_host(host):
        raise ProxyRequestError("target_not_allowed")
    return PublicTarget(host=host.rstrip("."), port=target_port)


def _target_from_http_url(value: str) -> tuple[PublicTarget, str]:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ProxyRequestError("invalid_url") from exc
    if (
        parsed.scheme != "http"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not _is_allowed_host(host)
    ):
        raise ProxyRequestError("target_not_allowed")
    target_port = port or 80
    if target_port != 80:
        raise ProxyRequestError("target_not_allowed")
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    return PublicTarget(host=host.rstrip("."), port=target_port), path


def resolve_public_addresses(host: str, port: int) -> list[tuple[int, int, int, tuple[Any, ...]]]:
    """Resolve once and reject *all* non-global answers before connecting."""

    try:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ProxyRequestError("dns_failed") from exc
    candidates: list[tuple[int, int, int, tuple[Any, ...]]] = []
    seen: set[tuple[int, str, int]] = set()
    for family, socktype, protocol, _canonname, sockaddr in rows:
        address = str(sockaddr[0]).split("%", 1)[0]
        try:
            public = ipaddress.ip_address(address).is_global
        except ValueError as exc:
            raise ProxyRequestError("dns_invalid") from exc
        if not public:
            raise ProxyRequestError("unsafe_dns_answer")
        marker = (family, address, int(sockaddr[1]))
        if marker not in seen:
            seen.add(marker)
            candidates.append((family, socktype, protocol, sockaddr))
    if not candidates:
        raise ProxyRequestError("dns_failed")
    return candidates


def connect_public(target: PublicTarget, *, timeout: float = CONNECT_TIMEOUT_SECONDS) -> socket.socket:
    """Connect to a checked numeric DNS answer without resolving a second time."""

    candidates = resolve_public_addresses(target.host, target.port)
    last_error: OSError | None = None
    for family, socktype, protocol, sockaddr in candidates:
        connection = socket.socket(family, socktype, protocol)
        try:
            connection.settimeout(timeout)
            connection.connect(sockaddr)
            return connection
        except OSError as exc:
            last_error = exc
            connection.close()
    raise ProxyRequestError("connect_failed") from last_error


def _read_headers(connection: socket.socket) -> tuple[bytes, bytes]:
    buffer = bytearray()
    while len(buffer) <= MAX_HEADER_BYTES:
        chunk = connection.recv(min(4096, MAX_HEADER_BYTES + 1 - len(buffer)))
        if not chunk:
            break
        buffer.extend(chunk)
        marker = buffer.find(b"\r\n\r\n")
        if marker >= 0:
            return bytes(buffer[: marker + 4]), bytes(buffer[marker + 4 :])
    raise ProxyRequestError("header_too_large")


def _parse_headers(raw: bytes) -> tuple[str, str, dict[str, str]]:
    try:
        lines = raw.decode("iso-8859-1").split("\r\n")
        method, target, version = lines[0].split(" ")
    except (UnicodeDecodeError, IndexError, ValueError) as exc:
        raise ProxyRequestError("request_invalid") from exc
    if not version.startswith("HTTP/1."):
        raise ProxyRequestError("request_invalid")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line:
            raise ProxyRequestError("request_invalid")
        key, value = line.split(":", 1)
        normalized = key.strip().casefold()
        if not normalized or normalized in headers:
            raise ProxyRequestError("request_invalid")
        headers[normalized] = value.strip()
    if headers.get("proxy-authorization") or headers.get("transfer-encoding"):
        raise ProxyRequestError("request_not_allowed")
    content_length = headers.get("content-length", "0")
    if not content_length.isdigit() or int(content_length) != 0:
        raise ProxyRequestError("request_not_allowed")
    return method.upper(), target, headers


def _relay(left: socket.socket, right: socket.socket, *, initial_to_right: bytes = b"") -> None:
    if initial_to_right:
        right.sendall(initial_to_right)
    transferred = len(initial_to_right)
    left.settimeout(IDLE_TIMEOUT_SECONDS)
    right.settimeout(IDLE_TIMEOUT_SECONDS)
    sockets = (left, right)
    while transferred <= MAX_TUNNEL_BYTES:
        readable, _writable, _errors = select.select(sockets, [], [], IDLE_TIMEOUT_SECONDS)
        if not readable:
            return
        for source in readable:
            destination = right if source is left else left
            try:
                payload = source.recv(65_536)
            except (OSError, TimeoutError):
                return
            if not payload:
                return
            transferred += len(payload)
            if transferred > MAX_TUNNEL_BYTES:
                return
            try:
                destination.sendall(payload)
            except (OSError, TimeoutError):
                return


def _safe_http_headers(headers: dict[str, str], target: PublicTarget) -> bytes:
    accept = headers.get("accept", "text/html,application/xhtml+xml")[:512]
    language = headers.get("accept-language", "")[:200]
    lines = [
        f"Host: {target.host}",
        f"User-Agent: {USER_AGENT}",
        f"Accept: {accept}",
        "Connection: close",
    ]
    if language:
        lines.append(f"Accept-Language: {language}")
    return "\r\n".join(lines).encode("iso-8859-1", "replace")


def _allowed_peer(peer: str, networks: tuple[Network, ...]) -> bool:
    """Allow only loopback or the renderer's exact internal-control network."""

    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return address.is_loopback or any(address in network for network in networks)


class _J1ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        slots = getattr(server, "slots", None)
        if not isinstance(slots, threading.BoundedSemaphore) or not slots.acquire(blocking=False):
            return
        try:
            peer = str(self.client_address[0])
            networks = getattr(server, "allowed_peer_networks", ())
            if not _allowed_peer(peer, networks):
                return
            self.request.settimeout(CONNECT_TIMEOUT_SECONDS)
            raw, remainder = _read_headers(self.request)
            method, requested, headers = _parse_headers(raw)
            if method == "CONNECT":
                self._connect_tunnel(requested, remainder)
            elif method in _ALLOWED_HTTP_METHODS:
                if remainder:
                    raise ProxyRequestError("request_not_allowed")
                self._forward_http(method, requested, headers)
            else:
                raise ProxyRequestError("request_not_allowed")
        except (OSError, ProxyRequestError, ValueError):
            with suppress(OSError):
                self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
        finally:
            slots.release()

    def _connect_tunnel(self, authority: str, remainder: bytes) -> None:
        target = _target_from_authority(authority, allowed_ports=frozenset({443}))
        with connect_public(target) as upstream:
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\nConnection: close\r\n\r\n")
            _relay(self.request, upstream, initial_to_right=remainder)

    def _forward_http(self, method: str, requested: str, headers: dict[str, str]) -> None:
        target, path = _target_from_http_url(requested)
        request = f"{method} {path} HTTP/1.1\r\n".encode("ascii", "strict")
        request += _safe_http_headers(headers, target) + b"\r\n\r\n"
        with connect_public(target) as upstream:
            upstream.sendall(request)
            _relay(upstream, self.request)


class _J1ProxyServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        allowed_peer_networks: tuple[Network, ...],
    ) -> None:
        super().__init__(address, _J1ProxyHandler)
        self.slots = threading.BoundedSemaphore(MAX_CLIENTS)
        self.allowed_peer_networks = allowed_peer_networks


def _parse_allowed_peers(values: list[str] | tuple[str, ...]) -> tuple[Network, ...]:
    networks: list[Network] = []
    for value in values:
        try:
            networks.append(ipaddress.ip_network(value, strict=True))
        except ValueError as exc:
            raise ValueError("allowed_peer_invalid") from exc
    return tuple(networks)


def serve(
    *,
    host: str = DEFAULT_LISTEN_HOST,
    port: int = DEFAULT_LISTEN_PORT,
    allowed_peers: list[str] | tuple[str, ...] = (),
) -> None:
    if host not in {"127.0.0.1", "::1", "0.0.0.0", "::"} or not isinstance(port, int) or not 1024 <= port <= 65_535:
        raise ValueError("listen_address_invalid")
    peer_networks = _parse_allowed_peers(allowed_peers)
    if host in {"0.0.0.0", "::"} and not peer_networks:
        raise ValueError("allowed_peer_required")
    with _J1ProxyServer((host, port), allowed_peer_networks=peer_networks) as server:
        server.serve_forever(poll_interval=0.5)


def main() -> None:
    parser = argparse.ArgumentParser(description="AutoStop J1 isolated egress proxy")
    parser.add_argument("command", choices=("serve",))
    parser.add_argument("--host", default=DEFAULT_LISTEN_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_LISTEN_PORT)
    parser.add_argument("--allowed-peer", action="append", default=[])
    args = parser.parse_args()
    serve(host=args.host, port=args.port, allowed_peers=args.allowed_peer)


if __name__ == "__main__":
    main()
