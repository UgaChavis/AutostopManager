"""Exercise the research-only download budget over synthetic HTTP responses."""

from __future__ import annotations

from collections.abc import Callable
import http.client
import io
import socket
from typing import cast

import pytest

from autostop_manager import j1_fetch, j1_research, web_research_gateway

_PDF_URL = "https://example.org/workshop.pdf"
_PDF_PREFIX = b"%PDF-"
_PDF = _PDF_PREFIX + b"x" * 75
_LARGE_HEADERS = (("Content-Length", "80"), ("Content-Type", "application/pdf"))
_TEXT = "P1A27. Inspect the connector and verify reference voltage at the specified terminal."


@pytest.fixture
def wire(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[tuple[tuple[str, str], ...], bytes, int], list[int | None]]:
    monkeypatch.setattr(j1_fetch, "MAX_PDF_BYTES", 64)
    monkeypatch.setattr(j1_fetch, "MAX_RESEARCH_PDF_BYTES", 96)
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (True, 0))
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)
    monkeypatch.setattr(j1_fetch, "_public_address", lambda *_args: "1.1.1.1")

    def install(headers: tuple[tuple[str, str], ...], body: bytes, status: int = 200) -> list[int | None]:
        requested: list[int | None] = []
        response_bytes = (
            f"HTTP/1.1 {status} Synthetic\r\n"
            + "Connection: close\r\n"
            + "".join(f"{key}: {value}\r\n" for key, value in headers)
            + "\r\n"
        ).encode("ascii") + body

        class SyntheticSocket:
            def makefile(self, _mode: str) -> io.BytesIO:
                return io.BytesIO(response_bytes)

        class Response(http.client.HTTPResponse):
            def read(self, amt: int | None = None) -> bytes:
                requested.append(amt)
                return super().read(amt)

        class Connection:
            def __init__(self, *_args: object) -> None:
                self.response = Response(cast(socket.socket, SyntheticSocket()))

            def request(self, *_args: object, **_kwargs: object) -> None:
                pass

            def getresponse(self) -> Response:
                self.response.begin()
                return self.response

            def close(self) -> None:
                pass

        monkeypatch.setattr(j1_fetch, "_PinnedHTTPS", Connection)
        monkeypatch.setattr(j1_fetch, "_PinnedHTTP", Connection)
        return requested

    return install


def test_public_worker_can_store_a_larger_pdf_but_standalone_excerpt_cannot(wire, monkeypatch, tmp_path):
    requested = wire(_LARGE_HEADERS, _PDF)
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "public"))
    monkeypatch.setattr(j1_research, "_STOP", False)

    def native_pdf_text(body: bytes) -> str:
        assert body == _PDF
        return _TEXT + "\n\f"

    monkeypatch.setattr(j1_fetch, "_pdf_to_text", native_pdf_text)
    adapter = web_research_gateway.CapabilityWebResearchGatewayAdapter(
        lambda *_args: {
            "ok": False,
            "error": {"code": "unsupported_media", "retryable": False},
            "status_code": 200,
            "content_type": "application/pdf",
        }
    )
    standalone = adapter.fetch_page_excerpt(url=_PDF_URL)
    assert standalone["error"]["code"] == "document_too_large"
    assert requested == []

    created = j1_research.research_sources(
        objective="Find technical diagnostic instructions",
        source_urls=[_PDF_URL],
        idempotency_key="pdf-byte-budget-001",
    )
    assert created["ok"], created
    job = created["job_id"]
    with j1_research._db() as conn:
        assert j1_research._claim_job(conn) == job
    j1_research._work_job(job)
    result = j1_research.research_results(job, query="P1A27")
    assert result["total"] == 1
    row = result["results"][0]
    assert row["page"] == 1 and row["extraction_method"] == "pdf_text"
    assert j1_research.research_document(job, row["document_id"], page=1)["text"] == _TEXT
    assert requested == [len(_PDF_PREFIX), len(_PDF) + 1 - len(_PDF_PREFIX)]


@pytest.mark.parametrize(
    ("url", "headers", "status", "body", "expected_reads"),
    [
        (_PDF_URL, (("Content-Type", "application/pdf"),), 200, _PDF, [65]),
        (_PDF_URL, (("Content-Length", "97"), ("Content-Type", "application/pdf")), 200, _PDF, []),
        (_PDF_URL, (("Content-Length", "80"), ("Content-Type", "text/html")), 200, _PDF, []),
        (_PDF_URL, (*_LARGE_HEADERS, ("Transfer-Encoding", "chunked")), 200, _PDF, []),
        (_PDF_URL, (*_LARGE_HEADERS, ("Content-Encoding", "gzip")), 200, _PDF, []),
        (_PDF_URL, (*_LARGE_HEADERS, ("Content-Length", "80")), 200, _PDF, []),
        (_PDF_URL, (*_LARGE_HEADERS, ("Content-Type", "text/html")), 200, _PDF, []),
        (_PDF_URL, _LARGE_HEADERS, 403, _PDF, []),
        (_PDF_URL, _LARGE_HEADERS, 200, b"<html>" + _PDF[6:], [5]),
        (_PDF_URL, _LARGE_HEADERS, 200, _PDF[:-5], [5, 76]),
        ("http://example.org/workshop.pdf", _LARGE_HEADERS, 200, _PDF, []),
        ("https://example.org/workshop", _LARGE_HEADERS, 200, _PDF, []),
    ],
)
def test_large_pdf_budget_requires_confirmed_headers_signature_and_actual_size(
    wire, url, headers, status, body, expected_reads
):
    requested = wire(headers, body, status)
    assert j1_fetch.fetch_document(url, allow_large_pdf=True) == {"ok": False, "error": "document_too_large"}
    assert requested == expected_reads


def test_large_pdf_body_does_not_include_bytes_outside_the_http_frame(wire):
    requested = wire(_LARGE_HEADERS, _PDF + b"x" * 200)
    _, _, body, _ = j1_fetch._request_public(
        _PDF_URL, max_bytes=j1_fetch.MAX_PDF_BYTES, check_redirect_robots=True, allow_large_pdf=True
    )
    assert body == _PDF
    assert requested == [5, 76]
