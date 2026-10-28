"""Focused isolated unit coverage for J1 OCR and browser client contracts."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from pathlib import Path
import stat
import socket
from typing import cast
import subprocess

import pytest

from autostop_manager import j1_browser, j1_browser_renderer, j1_fetch, j1_ocr


class _ChunkSocket:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)

    def recv(self, _size: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""


class _WireSocket:
    def __init__(self, response: bytes) -> None:
        self.response = [response]
        self.connected_to = ""
        self.timeout: float | None = None
        self.sent = b""

    def __enter__(self) -> _WireSocket:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def connect(self, value: str) -> None:
        self.connected_to = value

    def sendall(self, value: bytes) -> None:
        self.sent += value

    def recv(self, _size: int) -> bytes:
        return self.response.pop(0) if self.response else b""


def test_bounded_integer_helpers_reject_bool_and_clamp() -> None:
    for helper in (j1_browser._bounded_int, j1_ocr._bounded_int):
        assert helper(True, default=7, minimum=2, maximum=9) == 7
        assert helper("4", default=7, minimum=2, maximum=9) == 4
        assert helper(-10, default=7, minimum=2, maximum=9) == 2
        assert helper(99, default=7, minimum=2, maximum=9) == 9
        assert helper("not-a-number", default=7, minimum=2, maximum=9) == 7


def test_browser_reads_a_single_line_and_normalizes_safe_response() -> None:
    assert j1_browser._read_message(_ChunkSocket([b'{"ok":true}', b"\nignored"])) == b'{"ok":true}'

    result = j1_browser._normalize_response(
        {
            "schema": j1_browser.SCHEMA,
            "ok": True,
            "url": "https://example.org/final",
            "title": "T" * 250,
            "text": "public evidence " * 20,
        },
        requested_url="https://example.org/request",
        max_chars=100,
    )

    assert result["ok"] is True
    assert result["url"] == "https://example.org/final"
    assert result["requested_url"] == "https://example.org/request"
    assert len(result["title"]) == 200
    assert len(result["text"]) == 100
    assert result["extraction_method"] == "browser_dom"


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"schema": "wrong", "ok": True}, "browser_protocol_invalid"),
        (
            {"schema": j1_browser.SCHEMA, "ok": True, "url": "http://127.0.0.1/", "text": "x"},
            "browser_response_invalid",
        ),
        ({"schema": j1_browser.SCHEMA, "ok": False, "error": "blocked", "retryable": False}, "blocked"),
    ],
)
def test_browser_normalization_fails_closed(payload: object, error: str) -> None:
    result = j1_browser._normalize_response(
        payload,
        requested_url="https://example.org/request",
        max_chars=100,
    )
    assert result["ok"] is False
    assert result["error"] == error


def test_browser_marker_requires_root_owned_private_regular_file(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[int] = []
    revision = "a" * 40
    real_os_functions = (os.open, os.close, os.read, os.fstat)
    monkeypatch.setattr(j1_browser, "os", SimpleNamespace(**vars(j1_browser.os)))
    monkeypatch.setattr(j1_browser.os, "open", lambda *_args: 42)
    monkeypatch.setattr(j1_browser.os, "read", lambda *_args: j1_browser.attestation_content(revision))
    monkeypatch.setattr(j1_browser.os, "close", lambda descriptor: closed.append(descriptor))
    monkeypatch.setattr(j1_browser, "active_release_revision", lambda *_args: (revision, ""))
    monkeypatch.setattr(
        j1_browser.os,
        "fstat",
        lambda _descriptor: SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0),
    )
    assert (os.open, os.close, os.read, os.fstat) == real_os_functions
    assert j1_browser._isolation_verified("/run/j1-ready") is True
    assert closed == [42]

    monkeypatch.setattr(
        j1_browser.os,
        "fstat",
        lambda _descriptor: SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0),
    )
    assert j1_browser._isolation_verified("/run/j1-ready") is False


def test_browser_client_sends_bounded_ipc_request_and_clips_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    response = {
        "schema": j1_browser.SCHEMA,
        "ok": True,
        "url": "https://example.org/rendered",
        "title": "Rendered page",
        "text": "evidence " * 30,
    }
    wire = _WireSocket(json.dumps(response).encode("utf-8") + b"\n")
    monkeypatch.setattr(j1_browser, "isolation_verified", lambda: True)
    monkeypatch.setattr(j1_browser.socket, "socket", lambda *_args: wire)

    result = j1_browser.render_page(
        "https://example.org/request",
        max_chars=90,
        timeout_seconds=999,
        socket_path=str(tmp_path / "renderer.sock"),
    )

    request = json.loads(wire.sent.decode("utf-8"))
    assert wire.connected_to == str(tmp_path / "renderer.sock")
    assert wire.timeout == 27
    assert request == {
        "schema": j1_browser.SCHEMA,
        "operation": "render",
        "url": "https://example.org/request",
        "max_chars": 90,
        "timeout_seconds": j1_browser.MAX_TIMEOUT_SECONDS,
    }
    assert result["ok"] is True
    assert len(result["text"]) == 90


class _SuccessfulProcess:
    returncode = 0

    def __init__(self) -> None:
        self.wait_calls: list[float] = []

    def wait(self, timeout: float) -> None:
        self.wait_calls.append(timeout)


class _TimeoutProcess:
    returncode = 0

    def __init__(self) -> None:
        self.killed = False
        self.calls = 0

    def wait(self, timeout: float) -> None:
        self.calls += 1
        if self.calls == 1:
            raise subprocess.TimeoutExpired("ocr", timeout)

    def kill(self) -> None:
        self.killed = True


def test_ocr_runner_handles_success_and_timeout_without_running_a_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    successful = _SuccessfulProcess()
    monkeypatch.setattr(j1_ocr.subprocess, "Popen", lambda *_args, **_kwargs: successful)
    assert j1_ocr._run(["safe-command"], timeout=0) is True
    assert successful.wait_calls == [1.0]

    timed_out = _TimeoutProcess()
    monkeypatch.setattr(j1_ocr.subprocess, "Popen", lambda *_args, **_kwargs: timed_out)
    assert j1_ocr._run(["safe-command"], timeout=4) is False
    assert timed_out.killed is True
    assert timed_out.calls == 2


def test_ocr_pipeline_reports_render_and_extract_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(j1_ocr, "_ocr_languages_available", lambda: True)
    monkeypatch.setattr(j1_ocr, "_run", lambda *_args, **_kwargs: False)
    render_failure = j1_ocr.extract_scanned_pdf(b"%PDF-1.4\nscan")
    assert render_failure["error"] == "ocr_render_failed"

    def render_then_fail_extract(argv: list[str], **_kwargs: object) -> bool:
        if argv[0] == "pdftoppm":
            Path(argv[-1]).parent.joinpath("page-01.jpg").write_bytes(b"\xff\xd8" + b"x" * 100)
            return True
        return False

    monkeypatch.setattr(j1_ocr, "_run", render_then_fail_extract)
    extract_failure = j1_ocr.extract_scanned_pdf(b"%PDF-1.4\nscan", max_pages=1)
    assert extract_failure == {
        "ok": False,
        "error": "ocr_extract_failed",
        "retryable": True,
        "extraction_method": "pdf_ocr",
    }


def test_ocr_pipeline_redacts_sensitive_text_and_rejects_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(j1_ocr, "_ocr_languages_available", lambda: True)

    def fake_run(argv: list[str], *, stdout: object = None, **_kwargs: object) -> bool:
        if argv[0] == "pdftoppm":
            Path(argv[-1]).parent.joinpath("page-01.jpg").write_bytes(b"\xff\xd8" + b"x" * 100)
        else:
            assert stdout is not None
            stdout.write(b"ZZZ00000000000000 public technical evidence " + b"x" * 100)  # type: ignore[union-attr]
        return True

    monkeypatch.setattr(j1_ocr, "_run", fake_run)
    extracted = j1_ocr.extract_scanned_pdf(b"%PDF-1.4\nscan", max_pages=1)
    assert extracted["ok"] is True
    assert "ZZZ00000000000000" not in extracted["text"]

    def short_text(argv: list[str], *, stdout: object = None, **_kwargs: object) -> bool:
        if argv[0] == "pdftoppm":
            Path(argv[-1]).parent.joinpath("page-01.jpg").write_bytes(b"\xff\xd8" + b"x" * 100)
        else:
            assert stdout is not None
            stdout.write(b"too short")  # type: ignore[union-attr]
        return True

    monkeypatch.setattr(j1_ocr, "_run", short_text)
    empty = j1_ocr.extract_scanned_pdf(b"%PDF-1.4\nscan", max_pages=1)
    assert empty == {
        "ok": False,
        "error": "ocr_empty",
        "retryable": False,
        "extraction_method": "pdf_ocr",
    }


@pytest.mark.parametrize(
    "char_limit,page_text,expected_pages",
    [(80, b"Technical text. " * 6, 1), (80, b"Technical text. " * 2, 3), (1000, b"Technical text. " * 2, 10)],
)
def test_ocr_reports_only_pages_read_before_the_character_budget(monkeypatch, char_limit, page_text, expected_pages):
    monkeypatch.setattr(j1_ocr, "_ocr_languages_available", lambda: True)
    monkeypatch.setattr(j1_ocr, "_prepare_workdir", lambda *_args: None)
    extracted = []

    def run(argv, *, timeout, stdout=None):
        if argv[0] == "pdftoppm":
            for page in range(1, 11):
                Path(f"{argv[-1]}-{page:02}.jpg").write_bytes(b"synthetic image")
        else:
            extracted.append(argv[1])
            stdout.write(page_text)
        return True

    monkeypatch.setattr(j1_ocr, "_run", run)
    result = j1_ocr.extract_scanned_pdf(b"%PDF-1.4 synthetic", max_pages=10, max_chars=char_limit)
    assert result["ok"] is True
    assert len(extracted) == expected_pages
    assert result["pages_processed"] == expected_pages


class _BudgetWire:
    def __init__(self):
        self.payload = b""

    def sendall(self, value):
        self.payload += value

    def recv(self, size):
        result, self.payload = self.payload[:size], self.payload[size:]
        return result


@pytest.mark.parametrize("symbol", ["x", "я", "😀", "\x01", '"', "\\"])
def test_browser_character_budget_survives_utf8_and_json_wire_expansion(symbol):
    text = symbol * j1_browser.MAX_TEXT_CHARS
    prefix = "https://example.org/"
    url = prefix + "😀" * (2048 - len(prefix))
    title = "😀" * 200
    rendered = {
        "ok": True,
        "schema": j1_browser_renderer.SCHEMA,
        "url": url,
        "title": title,
        "text": text,
        "kind": "browser",
        "extraction_method": "browser_dom",
    }
    wire = _BudgetWire()
    j1_browser_renderer._write_response(cast(socket.socket, wire), rendered)
    received = json.loads(j1_browser._read_message(cast(socket.socket, wire)))
    assert received == rendered
    normalized = j1_browser._normalize_response(received, requested_url=url, max_chars=j1_browser.MAX_TEXT_CHARS)
    assert normalized["ok"] is True
    assert normalized["text"] == text
    assert normalized["title"] == title
    assert normalized["url"] == url


def test_browser_response_still_rejects_unbounded_payload():
    wire = _BudgetWire()
    j1_browser_renderer._write_response(
        cast(socket.socket, wire), {"ok": True, "text": "x" * (j1_browser_renderer._MAX_RESPONSE_BYTES + 1)}
    )
    result = json.loads(j1_browser._read_message(cast(socket.socket, wire)))
    assert result["ok"] is False
    assert result["error"] == "browser_response_too_large"


@pytest.mark.parametrize("operation", ["revision", "status"])
def test_browser_release_symlink_loop_returns_structured_unavailable(tmp_path, monkeypatch, operation):
    release = tmp_path / "release"
    release.symlink_to("release")
    if operation == "revision":
        assert j1_browser.active_release_revision(str(release)) == ("", "browser_release_revision_unavailable")
    else:
        monkeypatch.setenv("AUTOSTOP_J1_BROWSER_RELEASE_ROOT", str(release))
        result = j1_browser.browser_status()
        assert result["browser_ready"] is False
        assert result["browser_reason"] == "browser_release_revision_unavailable"


class _RequestPeer:
    def __init__(self, request):
        self.request = request
        self.response = b""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def settimeout(self, _timeout):
        pass

    def recv(self, size):
        result, self.request = self.request[:size], self.request[size:]
        return result

    def sendall(self, value):
        self.response += value


@pytest.mark.parametrize("symbol", ["x", "я", "😀"])
@pytest.mark.parametrize("component", ["path", "query"])
def test_native_browser_accepts_same_size_unicode_url_over_bounded_request_wire(monkeypatch, symbol, component):
    prefix = "https://example.org/" if component == "path" else "https://example.org/?term="
    url = prefix + symbol * (2048 - len(prefix))
    assert len(url) == 2048
    assert j1_fetch.public_url(url) == url
    rendered_urls = []
    request_sizes = []
    server = j1_browser_renderer.RendererServer("/tmp/synthetic-j1-wire.sock")

    def chromium(target, **_kwargs):
        rendered_urls.append(target)
        return b"<p>Public technical evidence with sufficient words. " + b"x" * 200 + b"</p>", ""

    class NativeSocket:
        def __init__(self):
            self.response = b""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, _timeout):
            pass

        def connect(self, target):
            assert target == "/tmp/synthetic-j1-wire.sock"

        def sendall(self, value):
            request_sizes.append(len(value))
            peer = _RequestPeer(value)
            server._handle_connection(cast(socket.socket, peer))
            self.response = peer.response

        def recv(self, size):
            result, self.response = self.response[:size], self.response[size:]
            return result

    monkeypatch.setattr(j1_browser, "isolation_verified", lambda: True)
    monkeypatch.setattr(j1_browser_renderer, "_peer_allowed", lambda _connection: True)
    monkeypatch.setattr(j1_browser_renderer, "_run_chromium", chromium)
    monkeypatch.setattr(j1_browser.socket, "socket", lambda *_args, **_kwargs: NativeSocket())
    result = j1_browser.render_page(url, max_chars=200, timeout_seconds=3, socket_path="/tmp/synthetic-j1-wire.sock")
    assert result["ok"] is True, {"result": result, "wire_bytes": request_sizes[0]}
    assert result["url"] == url
    assert rendered_urls == [url]


def test_browser_request_still_rejects_unbounded_wire():
    peer = _RequestPeer(b"x" * (j1_browser_renderer.MAX_REQUEST_BYTES + 1) + b"\n")
    assert j1_browser_renderer._read_line(cast(socket.socket, peer)) == b""
