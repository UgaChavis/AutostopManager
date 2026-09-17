"""Focused isolated unit coverage for J1 OCR and browser client contracts."""

from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path
import stat
import subprocess

import pytest

from autostop_manager import j1_browser, j1_ocr


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
    monkeypatch.setattr(j1_browser.os, "open", lambda *_args: 42)
    monkeypatch.setattr(j1_browser.os, "read", lambda *_args: j1_browser._ISOLATION_MARKER_CONTENT)
    monkeypatch.setattr(j1_browser.os, "close", lambda descriptor: closed.append(descriptor))
    monkeypatch.setattr(
        j1_browser.os,
        "fstat",
        lambda _descriptor: SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0),
    )
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
