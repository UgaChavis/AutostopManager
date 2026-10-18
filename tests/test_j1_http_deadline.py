"""Real HTTP parsing over isolated sockets; no DNS or provider traffic."""

from __future__ import annotations

from collections.abc import Callable, Iterator
import http.client
import io
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from autostop_manager import j1_fetch


@pytest.mark.parametrize("connection_class", [j1_fetch._PinnedHTTP, j1_fetch._PinnedHTTPS])
def test_pinned_connection_retains_mutable_standard_timeout(monkeypatch, connection_class) -> None:
    calls = []
    sock = SimpleNamespace(close=lambda: None)

    def connect(address, timeout):
        calls.append((address, timeout))
        return sock

    monkeypatch.setattr(j1_fetch.socket, "create_connection", connect)
    monkeypatch.setattr(j1_fetch.ssl.SSLContext, "wrap_socket", lambda _self, sock, **_kwargs: sock)
    connection = connection_class("example.org", "1.1.1.1", 443, 10)
    connection.timeout = 0.02
    try:
        connection.connect()
        assert calls == [(("1.1.1.1", 443), 0.02)]
    finally:
        connection.close()


@pytest.fixture
def exchange(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., dict[str, Any]]]:
    clients: list[socket.socket] = []
    peers: list[socket.socket] = []
    threads: list[threading.Thread] = []
    original_connection = j1_fetch._PinnedHTTP
    monkeypatch.setattr(j1_fetch, "_public_address", lambda *_args: "1.1.1.1")

    def install(segments: list[tuple[bytes, float]], *, io_timeout: float | None = None) -> dict[str, Any]:
        client, peer = socket.socketpair()
        clients.append(client)
        peers.append(peer)
        state: dict[str, Any] = {}

        def connect(address: tuple[str, int], timeout: float) -> socket.socket:
            assert address == ("1.1.1.1", 80)
            client.settimeout(min(timeout, io_timeout) if io_timeout is not None else timeout)
            return client

        class Connection(original_connection):
            def getresponse(self) -> http.client.HTTPResponse:
                response = super().getresponse()
                state["response"] = response
                return response

        def serve() -> None:
            try:
                peer.settimeout(1)
                request = b""
                while b"\r\n\r\n" not in request:
                    part = peer.recv(4096)
                    if not part:
                        return
                    request += part
                for data, delay in segments:
                    if delay:
                        time.sleep(delay)
                    peer.sendall(data)
            except OSError:
                pass
            finally:
                peer.close()

        monkeypatch.setattr(j1_fetch.socket, "create_connection", connect)
        monkeypatch.setattr(j1_fetch, "_PinnedHTTP", Connection)
        thread = threading.Thread(target=serve)
        threads.append(thread)
        thread.start()
        return state

    yield install
    for client in clients:
        client.close()
    for peer in peers:
        peer.close()
    for thread in threads:
        thread.join(1)
        assert not thread.is_alive()


def _drip_segments(phase: str) -> list[tuple[bytes, float]]:
    header = b"HTTP/1.1 200 OK\r\nContent-Length: 24\r\nConnection: close\r\n\r\n"
    chunked = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
    if phase == "headers":
        segments = [(bytes([byte]), 0.004) for byte in header] + [(b"a" * 24, 0)]
    elif phase == "body":
        segments = [(header, 0)] + [(b"a", 0.007)] * 24
    elif phase == "chunk_size":
        segments = (
            [(chunked, 0)]
            + [(bytes([byte]), 0.007) for byte in b"18;extension=abcdefghijklmnop\r\n"]
            + [(b"a" * 24 + b"\r\n0\r\n\r\n", 0)]
        )
    else:
        segments = [(chunked + b"3\r\nabc\r\n0\r\n", 0)] + [
            (bytes([byte]), 0.007) for byte in b"X-Synthetic-Trailer: abcdefghijklmnop\r\n\r\n"
        ]
    return segments


@pytest.mark.parametrize("phase", ["headers", "body", "chunk_size", "trailers"])
def test_http_exchange_deadline_bounds_continuous_drip(exchange, phase: str) -> None:
    exchange(_drip_segments(phase))
    # A finite drip would finish successfully with only an idle timeout. Check
    # interruption here; process scheduling/GC cannot guarantee a wall bound.
    with pytest.raises(TimeoutError):
        j1_fetch._request_public("http://example.org/report", max_bytes=100, timeout=0.06)


@pytest.mark.parametrize("phase", ["headers", "body", "chunk_size", "trailers"])
@pytest.mark.parametrize("idle_timeout", [None, 0.06, 0.002])
def test_http_parser_enforces_exact_deadline_without_scheduler_timing(
    monkeypatch: pytest.MonkeyPatch, phase: str, idle_timeout: float | None
) -> None:
    segments = _drip_segments(phase)
    elapsed = 0.0
    deadline = 0.06
    state = SimpleNamespace(timeout=idle_timeout)
    reads: list[tuple[float, float | None]] = []
    streams: list[io.RawIOBase] = []

    class DripReader(io.RawIOBase):
        def readable(self) -> bool:
            return True

        def readinto(self, buffer: Any) -> int:
            nonlocal elapsed
            reads.append((elapsed, state.timeout))
            if not segments:
                return 0
            data, delay = segments.pop(0)
            if state.timeout is not None and delay > state.timeout:
                elapsed += state.timeout
                raise TimeoutError("scripted_socket_timeout")
            elapsed += delay
            assert len(data) <= len(buffer)
            buffer[: len(data)] = data
            return len(data)

    def settimeout(timeout: float | None) -> None:
        state.timeout = timeout

    def makefile(*_args, **_kwargs) -> DripReader:
        stream = DripReader()
        streams.append(stream)
        return stream

    sock = SimpleNamespace(
        makefile=makefile,
        gettimeout=lambda: state.timeout,
        settimeout=settimeout,
    )
    # Keep the real HTTP parser and buffered/raw reader wiring. Only transport
    # delivery and its monotonic clock are controlled, without a sender thread.
    monkeypatch.setattr(j1_fetch, "time", SimpleNamespace(monotonic=lambda: elapsed))
    with j1_fetch._deadline_response_class(deadline)(sock) as response, pytest.raises(TimeoutError):
        response.begin()
        response.read(101)
    assert elapsed == pytest.approx(min(deadline, idle_timeout) if idle_timeout is not None else deadline)
    assert segments  # The response has not completed when the budget expires.
    assert all(stream.closed for stream in streams)
    assert [timeout for _, timeout in reads] == pytest.approx(
        [
            min(idle_timeout, deadline - started) if idle_timeout is not None else deadline - started
            for started, _ in reads
        ]
    )


@pytest.mark.parametrize(
    "payload, expected",
    [
        (b"Content-Length: 3\r\n\r\nabc", b"abc"),
        (b"Transfer-Encoding: chunked\r\n\r\n3;x=y\r\nabc\r\n0\r\nX-Trailer: value\r\n\r\n", b"abc"),
    ],
)
def test_http_deadline_preserves_framing_and_closes_response(exchange, payload: bytes, expected: bytes) -> None:
    state = exchange([(b"HTTP/1.1 200 OK\r\nConnection: close\r\n" + payload, 0)])
    result = j1_fetch._request_public("http://example.org/report", max_bytes=100, timeout=0.5)
    assert result[0] == 200 and result[2] == expected
    assert state["response"].isclosed()


def test_http_response_is_closed_when_body_is_not_read(exchange) -> None:
    state = exchange([(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nabc", 0)])
    result = j1_fetch._request_public("http://example.org/report", max_bytes=100, read_body=False)
    assert result[2] == b""
    assert state["response"].isclosed()


def test_http_deadline_preserves_a_shorter_socket_idle_timeout(exchange) -> None:
    exchange(
        [(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\n", 0), (b"abc", 0.07)],
        io_timeout=0.025,
    )
    with pytest.raises(TimeoutError):
        j1_fetch._request_public("http://example.org/report", max_bytes=100, timeout=0.5)


@pytest.mark.parametrize(
    "payload, error",
    [
        (b"NOT HTTP\r\n\r\n", http.client.BadStatusLine),
        (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nwrong\r\n", http.client.IncompleteRead),
    ],
)
def test_http_deadline_keeps_parser_failures(exchange, payload: bytes, error: type[Exception]) -> None:
    exchange([(payload, 0)])
    with pytest.raises(error):
        j1_fetch._request_public("http://example.org/report", max_bytes=100, timeout=0.5)


def test_https_deadline_bounds_decrypted_body_with_verified_tls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl needed to generate an isolated TLS test certificate")
    certificate, key = tmp_path / "certificate.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=example.org",
            "-addext",
            "subjectAltName=DNS:example.org",
        ],
        check=True,
        capture_output=True,
        timeout=5,
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate, key)
    client_context = ssl.create_default_context(cafile=str(certificate))
    client, peer = socket.socketpair()
    server_errors: list[Exception] = []

    def connect(address: tuple[str, int], timeout: float) -> socket.socket:
        assert address == ("1.1.1.1", 443)
        client.settimeout(timeout)
        return client

    def serve() -> None:
        try:
            peer.settimeout(1)
            with server_context.wrap_socket(peer, server_side=True) as connection:
                request = b""
                while b"\r\n\r\n" not in request:
                    part = connection.recv(4096)
                    if not part:
                        return
                    request += part
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 40\r\nConnection: close\r\n\r\n")
                for _ in range(40):
                    connection.sendall(b"a")
                    time.sleep(0.006)
        except OSError:
            pass
        except Exception as exc:  # noqa: BLE001 - report deterministic TLS fixture failures.
            server_errors.append(exc)
        finally:
            peer.close()

    monkeypatch.setattr(j1_fetch, "_public_address", lambda *_args: "1.1.1.1")
    monkeypatch.setattr(j1_fetch.socket, "create_connection", connect)
    monkeypatch.setattr(j1_fetch.ssl, "create_default_context", lambda: client_context)
    thread = threading.Thread(target=serve)
    thread.start()
    try:
        with pytest.raises(TimeoutError):
            j1_fetch._request_public("https://example.org/report", max_bytes=100, timeout=0.09)
    finally:
        client.close()
        peer.close()
        thread.join(1)
        assert not thread.is_alive()
    assert not server_errors
