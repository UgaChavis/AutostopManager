"""Deterministic CI coverage for J1's isolated renderer boundaries."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from autostop_manager import j1_browser, j1_browser_renderer as renderer


class _Connection:
    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self.chunks = list(chunks or [])
        self.sent: list[bytes] = []
        self.timeout: float | None = None

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def recv(self, _size: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    def sendall(self, payload: bytes) -> None:
        self.sent.append(payload)

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout


class _SuccessfulChromium:
    returncode = 0
    pid = 101

    def __init__(self, _argv: list[str], *, stdout: Any, **_kwargs: Any) -> None:
        stdout.write(b"<html><title>Rendered</title><p>public material</p></html>")

    def wait(self, timeout: float) -> None:
        assert timeout > 0


class _FailedChromium:
    returncode = 1
    pid = 102

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def wait(self, timeout: float) -> None:
        assert timeout > 0


class _TimeoutChromium:
    returncode = 0
    pid = 103

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.wait_calls = 0

    def wait(self, timeout: float) -> None:
        self.wait_calls += 1
        if self.wait_calls == 1:
            raise subprocess.TimeoutExpired("chromium", timeout)


def _allow_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renderer.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(renderer, "chromium_available", lambda _binary: True)
    monkeypatch.setenv("AUTOSTOP_J1_BROWSER_PROXY", "http://127.0.0.1:18890")


def test_chromium_availability_validates_binary_and_subprocess(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = tmp_path / "chromium"
    binary.write_text("stub", encoding="utf-8")
    binary.chmod(0o700)

    monkeypatch.setattr(renderer.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    assert renderer.chromium_available(str(binary)) is True

    monkeypatch.setattr(renderer.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1))
    assert renderer.chromium_available(str(binary)) is False

    def unavailable(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("missing runtime")

    monkeypatch.setattr(renderer.subprocess, "run", unavailable)
    assert renderer.chromium_available(str(binary)) is False
    assert renderer.chromium_available("relative-browser") is False


def test_trusted_proxy_rejects_malformed_control_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOSTOP_J1_BROWSER_CONTROL_CIDR", "not-a-network")
    assert renderer._trusted_proxy_url("http://127.0.0.1:18890") == ""

    monkeypatch.setenv("AUTOSTOP_J1_BROWSER_CONTROL_CIDR", renderer.DEFAULT_CONTROL_CIDR)
    assert renderer._trusted_proxy_url("http://127.0.0.1:bad-port") == ""
    assert renderer._trusted_proxy_url("http://[::1]:18890") == "http://[::1]:18890"


def test_chromium_arguments_and_child_limits_are_fixed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    argv = renderer._chromium_argv(
        binary="/safe/chromium",
        url="https://example.org/article",
        profile_dir=str(tmp_path),
        proxy_url="http://127.0.0.1:18890",
        wait_ms=500,
    )
    assert argv[0] == "/safe/chromium"
    assert argv[-1] == "https://example.org/article"
    assert "--dump-dom" in argv
    assert "--disable-extensions" in argv
    assert "--proxy-bypass-list=<-loopback>" in argv
    assert all("remote-debugging" not in value for value in argv)

    calls: list[tuple[int, tuple[int, int]]] = []
    fake_resource = SimpleNamespace(
        RLIMIT_FSIZE=1,
        RLIMIT_NOFILE=2,
        setrlimit=lambda kind, values: calls.append((kind, values)),
    )
    monkeypatch.setitem(sys.modules, "resource", fake_resource)
    renderer._child_limits()
    assert calls == [
        (1, (renderer.MAX_DOM_BYTES + 65_536, renderer.MAX_DOM_BYTES + 65_536)),
        (2, (256, 256)),
    ]


def test_run_chromium_collects_bounded_dom_without_binary_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_runtime(monkeypatch)
    monkeypatch.setattr(renderer.subprocess, "Popen", _SuccessfulChromium)

    dom, error = renderer._run_chromium("https://example.org/article", timeout_seconds=7)
    assert error == ""
    assert b"public material" in dom


def test_run_chromium_reports_timeout_nonzero_and_process_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_runtime(monkeypatch)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(renderer.os, "killpg", lambda pid, signal: killed.append((pid, signal)))
    monkeypatch.setattr(renderer.subprocess, "Popen", _TimeoutChromium)
    assert renderer._run_chromium("https://example.org/article", timeout_seconds=4) == (b"", "browser_timeout")
    assert killed == [(103, renderer.signal.SIGKILL)]

    monkeypatch.setattr(renderer.subprocess, "Popen", _FailedChromium)
    assert renderer._run_chromium("https://example.org/article", timeout_seconds=4) == (b"", "browser_render_failed")

    def broken_popen(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("not executable")

    monkeypatch.setattr(renderer.subprocess, "Popen", broken_popen)
    assert renderer._run_chromium("https://example.org/article", timeout_seconds=4) == (b"", "browser_render_failed")


def test_run_chromium_rejects_missing_binary_and_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renderer.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(renderer, "chromium_available", lambda _binary: False)
    assert renderer._run_chromium("https://example.org/article", timeout_seconds=4) == (
        b"",
        "browser_binary_unavailable",
    )

    monkeypatch.setattr(renderer, "chromium_available", lambda _binary: True)
    monkeypatch.setenv("AUTOSTOP_J1_BROWSER_PROXY", "http://8.8.8.8:18890")
    assert renderer._run_chromium("https://example.org/article", timeout_seconds=4) == (b"", "browser_proxy_invalid")


def test_renderer_render_reports_input_and_runtime_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    assert renderer.render("http://127.0.0.1/private") == {
        "ok": False,
        "schema": renderer.SCHEMA,
        "error": "browser_url_invalid",
        "retryable": False,
    }

    monkeypatch.setattr(renderer, "_run_chromium", lambda *_args, **_kwargs: (b"", "browser_proxy_invalid"))
    proxy_error = renderer.render("https://example.org/article")
    assert proxy_error["error"] == "browser_proxy_invalid"
    assert proxy_error["retryable"] is False

    monkeypatch.setattr(renderer, "_run_chromium", lambda *_args, **_kwargs: (b"", "browser_timeout"))
    assert renderer.render("https://example.org/article")["retryable"] is True


def test_renderer_server_handles_invalid_and_busy_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    server = renderer.RendererServer("/tmp/j1-renderer-ci.sock")
    monkeypatch.setattr(renderer, "_peer_allowed", lambda _connection: True)

    invalid = _Connection([b"not-json\n"])
    server._handle_connection(invalid)  # type: ignore[arg-type]
    assert json.loads(b"".join(invalid.sent).decode())["error"] == "browser_request_invalid"

    assert server._slots.acquire(blocking=False)
    assert server._slots.acquire(blocking=False)
    try:
        request = {
            "schema": renderer.SCHEMA,
            "operation": "render",
            "url": "https://example.org/article",
            "max_chars": 100,
            "timeout_seconds": 3,
        }
        busy = _Connection([json.dumps(request).encode() + b"\n"])
        server._handle_connection(busy)  # type: ignore[arg-type]
        assert json.loads(b"".join(busy.sent).decode())["error"] == "browser_busy"
    finally:
        server._slots.release()
        server._slots.release()


def test_renderer_server_and_main_fail_closed_before_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    server = renderer.RendererServer(str(tmp_path / "renderer.sock"))
    monkeypatch.setattr(renderer.os, "geteuid", lambda: 0)
    with pytest.raises(RuntimeError, match="renderer_must_not_run_as_root"):
        server.serve_forever()

    monkeypatch.setattr(renderer.os, "geteuid", lambda: 1000)
    server = renderer.RendererServer("relative.sock")
    with pytest.raises(RuntimeError, match="renderer_socket_path_invalid"):
        server.serve_forever()

    monkeypatch.setattr(renderer, "chromium_available", lambda: True)
    monkeypatch.setattr(sys, "argv", ["renderer", "probe"])
    renderer.main()
    assert json.loads(capsys.readouterr().out) == {"ok": True, "schema": renderer.SCHEMA}

    called: list[str] = []
    monkeypatch.setattr(renderer.RendererServer, "serve_forever", lambda self: called.append(self.socket_path))
    monkeypatch.setattr(sys, "argv", ["renderer", "serve", "--socket", "/tmp/renderer.sock"])
    renderer.main()
    assert called == ["/tmp/renderer.sock"]


def test_browser_client_rejects_bad_socket_and_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(j1_browser, "isolation_verified", lambda: True)
    result = j1_browser.render_page("https://example.org/article", socket_path="relative.sock")
    assert result["error"] == "browser_socket_invalid"

    class BrokenSocket:
        def __enter__(self) -> BrokenSocket:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def settimeout(self, _timeout: float) -> None:
            return None

        def connect(self, _path: str) -> None:
            return None

        def sendall(self, _payload: bytes) -> None:
            return None

        def recv(self, _size: int) -> bytes:
            return b"not-json\n"

    monkeypatch.setattr(j1_browser.socket, "socket", lambda *_args: BrokenSocket())
    malformed = j1_browser.render_page("https://example.org/article", socket_path="/tmp/renderer.sock")
    assert malformed["error"] == "browser_protocol_invalid"
