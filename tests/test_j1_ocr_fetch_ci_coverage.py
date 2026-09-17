"""Deterministic failure-path coverage for bounded J1 collection helpers."""

from __future__ import annotations

from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest

from autostop_manager import j1_fetch, j1_ocr


class _Response:
    def __init__(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        self.status = status
        self._headers = headers
        self._body = body

    def getheaders(self) -> list[tuple[str, str]]:
        return self._headers

    def read(self, _limit: int) -> bytes:
        return self._body


class _Connection:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.requests: list[tuple[object, ...]] = []
        self.closed = False

    def request(self, *args: object, **kwargs: object) -> None:
        self.requests.append((*args, kwargs))

    def getresponse(self) -> _Response:
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def test_dns_pinning_rejects_private_and_returns_deterministic_global_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        j1_fetch.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
        ],
    )
    assert j1_fetch._public_address("example.org", 443) == "1.1.1.1"

    monkeypatch.setattr(
        j1_fetch.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(ValueError, match="unsafe_dns_answer"):
        j1_fetch._public_address("example.org", 443)


def test_robots_cache_honors_disallow_and_reuses_a_safe_404_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    j1_fetch._ROBOTS.clear()
    calls: list[str] = []

    def not_found(url: str, **_kwargs: object) -> tuple[int, dict[str, str], bytes, str]:
        calls.append(url)
        return 404, {}, b"", url

    monkeypatch.setattr(j1_fetch, "_request_public", not_found)
    url = "https://example.org/public"
    assert j1_fetch._robots_policy(url) == (True, 1.0)
    assert j1_fetch._robots_policy(url) == (True, 1.0)
    assert calls == ["https://example.org/robots.txt"]

    j1_fetch._ROBOTS.clear()
    monkeypatch.setattr(
        j1_fetch,
        "_request_public",
        lambda url, **_kwargs: (200, {}, b"User-agent: *\nDisallow: /private\nCrawl-delay: 7\n", url),
    )
    assert j1_fetch._robots_policy("https://example.org/private/report") == (False, 7.0)


def test_public_request_handles_checked_redirect_and_rejects_encoded_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    connections = [
        _Connection([_Response(302, [("Location", "/next")], b"")]),
        _Connection([_Response(200, [("Content-Type", "text/plain")], b"technical evidence")]),
    ]
    monkeypatch.setattr(j1_fetch, "_public_address", lambda *_args: "1.1.1.1")
    monkeypatch.setattr(j1_fetch, "_PinnedHTTPS", lambda *_args: connections.pop(0))
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (True, 1.0))
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)
    status, headers, body, final_url = j1_fetch._request_public(
        "https://example.org/start", max_bytes=100, check_redirect_robots=True
    )
    assert (status, headers["content-type"], body, final_url) == (
        200,
        "text/plain",
        b"technical evidence",
        "https://example.org/next",
    )

    encoded = _Connection([_Response(200, [("Content-Encoding", "gzip")], b"x")])
    monkeypatch.setattr(j1_fetch, "_PinnedHTTPS", lambda *_args: encoded)
    with pytest.raises(ValueError, match="unsupported_content_encoding"):
        j1_fetch._request_public("https://example.org/encoded", max_bytes=100)


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "access_restricted"), (429, "rate_limited"), (500, "http_server_error")],
)
def test_fetch_document_returns_explicit_safe_http_failures(
    monkeypatch: pytest.MonkeyPatch, status: int, expected: str
) -> None:
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (True, 1.0))
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)
    monkeypatch.setattr(
        j1_fetch,
        "_request_public",
        lambda *_args, **_kwargs: (status, {"content-type": "text/html"}, b"", "https://example.org/page"),
    )
    assert j1_fetch.fetch_document("https://example.org/page") == {"ok": False, "error": expected}


def test_searx_parser_discards_private_rows_and_redacts_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = (
        b'{"results":[{"url":"https://example.org/guide","title":"Technical guide"},'
        b'{"url":"http://127.0.0.1/private","title":"No"}]}'
    )

    class SearchConnection:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.closed = False

        def request(self, *_args: object, **_kwargs: object) -> None:
            return None

        def getresponse(self) -> _Response:
            return _Response(200, [], payload)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(j1_fetch.http.client, "HTTPConnection", SearchConnection)
    rows = j1_fetch._search_searxng("technical guide", "http://127.0.0.1:8890")
    assert rows == [
        {"url": "https://example.org/guide", "title": "Technical guide", "snippet": "", "source": "searxng"}
    ]


def test_ocr_runtime_helpers_fail_closed_without_real_binaries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    limits: list[tuple[object, tuple[int, int]]] = []
    monkeypatch.setattr(j1_ocr.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(j1_ocr.resource, "setrlimit", lambda kind, values: limits.append((kind, values)))
    j1_ocr._restrict_child()
    assert len(limits) == 4

    monkeypatch.setattr(j1_ocr.shutil, "which", lambda _name: None)
    assert not j1_ocr._binaries_available()
    j1_ocr._ocr_languages_available.cache_clear()
    assert not j1_ocr._ocr_languages_available()

    workdir = tmp_path / "work"
    workdir.mkdir()
    source = workdir / "source.pdf"
    source.write_bytes(b"%PDF")
    monkeypatch.setattr(j1_ocr.os, "geteuid", lambda: 1000)
    j1_ocr._prepare_workdir(workdir, source)
    assert source.read_bytes() == b"%PDF"


def test_ocr_language_probe_and_process_errors_are_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    j1_ocr._ocr_languages_available.cache_clear()
    monkeypatch.setattr(j1_ocr, "_binaries_available", lambda: True)
    monkeypatch.setattr(
        j1_ocr.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=b"eng\nrus\n"),
    )
    assert j1_ocr._ocr_languages_available()

    j1_ocr._ocr_languages_available.cache_clear()
    monkeypatch.setattr(j1_ocr.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing")))
    assert not j1_ocr._ocr_languages_available()

    monkeypatch.setattr(j1_ocr.subprocess, "Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing")))
    assert not j1_ocr._run(["tesseract"], timeout=3)


def test_ocr_rejects_invalid_input_and_tempfile_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    assert j1_ocr.extract_scanned_pdf(b"not a pdf")["error"] == "ocr_invalid_pdf"
    assert j1_ocr.extract_scanned_pdf(b"%PDF" + b"x" * j1_ocr.MAX_PDF_BYTES)["error"] == "ocr_invalid_pdf"
    monkeypatch.setattr(j1_ocr, "_ocr_languages_available", lambda: True)
    monkeypatch.setattr(
        j1_ocr.tempfile,
        "TemporaryDirectory",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("temporary unavailable")),
    )
    assert j1_ocr.extract_scanned_pdf(b"%PDF-1.4") == {
        "ok": False,
        "error": "ocr_extract_failed",
        "retryable": True,
        "extraction_method": "pdf_ocr",
    }


def test_ocr_run_timeout_kills_child(monkeypatch: pytest.MonkeyPatch) -> None:
    class TimedOut:
        returncode = 0

        def __init__(self) -> None:
            self.calls = 0
            self.killed = False

        def wait(self, *, timeout: float) -> None:
            _ = timeout
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("tesseract", 1)

        def kill(self) -> None:
            self.killed = True

    process = TimedOut()
    monkeypatch.setattr(j1_ocr.subprocess, "Popen", lambda *_args, **_kwargs: process)
    assert not j1_ocr._run(["tesseract"], timeout=3)
    assert process.killed and process.calls == 2
