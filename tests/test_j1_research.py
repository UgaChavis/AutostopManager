"""Focused J1 queue, privacy, corpus and public-fetch safety checks."""

from __future__ import annotations

from pathlib import Path
import shutil
import socket

import pytest

from autostop_manager import j1_fetch, j1_research as j1


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("AUTOSTOP_J1_SEARXNG_URL", raising=False)
    monkeypatch.setattr(j1, "_STOP", False)
    j1_fetch._ROBOTS.clear()
    j1_fetch._HOST_NEXT.clear()


def test_private_input_rejected_before_cache_write(tmp_path: Path) -> None:
    compact = "WBA" + "0" * 14  # synthetic VIN-shaped input
    separated = "WBA " + "0" * 6 + " " + "0" * 8
    for private in (
        compact,
        separated,
        "WBA 000 000 000 000 00",
        "WBA000 0000 0000 000",
        "owner@example.org",
        "+7 999 123 45 67",
        "Bearer abcdefghijklmnop",
    ):
        assert not j1.start_research(f"Investigate {private}", ["public topic"])["ok"]
        assert not j1.start_research("public topic", [f"Investigate {private}"])["ok"]
        assert not j1_fetch.public_url("https://example.org/?q=" + private)
    assert not (tmp_path / "research.sqlite3").exists()


def test_queue_worker_search_document_incremental_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        j1,
        "search_public",
        lambda query, **_kwargs: (
            [{"url": "https://example.org/article", "title": "Useful case", "source": "searxng"}],
            "searxng",
        ),
    )
    monkeypatch.setattr(
        j1,
        "fetch_document",
        lambda url: {
            "ok": True,
            "url": url,
            "title": "Useful case",
            "kind": "html",
            "text": "Repair case: " + "diagnosis and outcome " * 80,
        },
    )
    created = j1.start_research("Investigate a public issue", ["first query"], max_pages=3)
    assert created["ok"]
    assert j1.probe()["ok"]
    assert j1.start_research("another", ["query"])["error"]["code"] == "j1_busy"
    j1.run_worker(once=True)
    job_id = created["job_id"]
    status = j1.research_status(job_id)
    assert status["status"] == "completed"
    assert (status["pages_fetched"], status["queries_done"]) == (1, 1)
    listing = j1.research_results(job_id)
    assert listing["total"] == 1
    assert listing["results"][0]["status"] == "fetched"
    assert listing["results"][0]["retrieved_at"]
    document_id = listing["results"][0]["document_id"]
    first = j1.research_document(job_id, document_id, max_chars=100)
    assert first["next_offset"] == 100
    assert j1.research_document(job_id, document_id, offset=100, max_chars=100)["text"]
    hits = j1.research_results(job_id, query="diagnosis")
    assert hits["total"] == 1
    assert hits["results"][0]["document_id"] == document_id
    assert j1.research_add_queries(job_id, ["second query"])["added"] == 1
    j1.run_worker(once=True)
    assert j1.research_status(job_id)["queries_done"] == 2
    assert j1.research_results(job_id)["total"] == 1  # URL deduplication


def test_failed_pages_visible_and_restart_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        j1,
        "search_public",
        lambda query, **_kwargs: (
            [{"url": "https://example.org/blocked", "title": "Blocked", "source": "searxng"}],
            "searxng",
        ),
    )
    monkeypatch.setattr(j1, "fetch_document", lambda _url: {"ok": False, "error": "robots_disallowed"})
    created = j1.start_research("Investigate", ["public query"])
    job_id = created["job_id"]
    with j1._db() as conn:
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        conn.execute("UPDATE queries SET status='running' WHERE job_id=?", (job_id,))
        conn.commit()
    j1.run_worker(once=True)
    status = j1.research_status(job_id)
    assert status["status"] == "completed"
    assert status["page_failures"] == [{"reason": "robots_disallowed", "count": 1}]
    listing = j1.research_results(job_id)
    assert listing["results"][0]["url"] == "https://example.org/blocked"
    assert listing["results"][0]["error"] == "robots_disallowed"
    assert not j1.research_document(job_id, listing["results"][0]["document_id"])["ok"]
    assert j1.research_cancel(job_id)["status"] == "completed"


def test_cancel_and_validation() -> None:
    created = j1.start_research("Investigate", ["public query"])
    job_id = created["job_id"]
    assert j1.research_cancel(job_id)["status"] == "cancelled"
    assert j1.research_add_queries(job_id, ["new query"])["error"]["code"] == "job_not_extendable"
    assert j1.research_results("bad")["error"]["code"] == "job_id_invalid"
    assert j1.research_results(job_id, cursor=-1)["error"]["code"] == "pagination_invalid"
    assert j1.research_document(job_id, "bad")["error"]["code"] == "identifier_invalid"
    assert j1.start_research("Investigate", ["public query"], max_pages=301)["error"]["code"] == "max_pages_invalid"
    assert not j1.start_research("Inspect http://127.0.0.1/private", ["public query"])["ok"]
    assert not j1.start_research("Investigate", ["read file:///etc/passwd"])["ok"]
    assert not j1.start_research("Investigate", ["http://localhost/private"])["ok"]


def test_public_dns_and_redirect_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    def mixed_dns(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(j1_fetch.socket, "getaddrinfo", mixed_dns)
    with pytest.raises(ValueError, match="unsafe_dns_answer"):
        j1_fetch._public_address("example.org", 443)

    class RedirectResponse:
        status = 302

        def getheaders(self) -> list[tuple[str, str]]:
            return [("Location", "https://second.example/private")]

    class FakeConnection:
        calls = 0

        def __init__(self, *_args: object) -> None:
            FakeConnection.calls += 1

        def request(self, *_args: object, **_kwargs: object) -> None:
            pass

        def getresponse(self) -> RedirectResponse:
            return RedirectResponse()

        def close(self) -> None:
            pass

    monkeypatch.setattr(j1_fetch, "_public_address", lambda *_args: "1.1.1.1")
    monkeypatch.setattr(j1_fetch, "_PinnedHTTPS", FakeConnection)
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (False, 1.0))
    with pytest.raises(ValueError, match="redirect_robots_disallowed"):
        j1_fetch._request_public("https://first.example/page", max_bytes=100, check_redirect_robots=True)
    assert FakeConnection.calls == 1


def test_public_extract_redacts_and_pdf_failure_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"<html><title>Case</title><nav>Navigation</nav><p>Useful repair details</p></html>"
    title, text = j1_fetch._html_to_text(body, {"content-type": "text/html; charset=utf-8"})
    assert title == "Case" and "Useful repair" in text and "Navigation" not in text
    assert "[redacted]" in j1_fetch.redact_sensitive("contact owner@example.org")
    assert j1_fetch._pdf_to_text(b"not a PDF") == ""
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (True, 1.0))
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)
    monkeypatch.setattr(
        j1_fetch,
        "_request_public",
        lambda *_args, **_kwargs: (429, {"content-type": "text/html"}, b"", "https://example.org/page"),
    )
    assert j1_fetch.fetch_document("https://example.org/page")["error"] == "rate_limited"


def test_pdf_text_extracts_locally_without_retaining_pdf() -> None:
    if not shutil.which("pdftotext"):
        pytest.skip("pdftotext unavailable")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length 45 >>\nstream\nBT /F1 12 Tf 100 700 Td (J1 PUBLIC DATA) Tj ET\nendstream",
    ]
    pdf = b"%PDF-1.4\n"
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(pdf)
    pdf += f"xref\n0 {len(offsets)}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        pdf += f"{offset:010d} 00000 n \n".encode()
    pdf += f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    assert "J1 PUBLIC DATA" in j1_fetch._pdf_to_text(pdf)


def test_capacity_failure_is_visible_to_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(j1, "MAX_CORPUS_BYTES", 1000)
    monkeypatch.setattr(
        j1,
        "search_public",
        lambda _query, **_kwargs: (
            [{"url": "https://example.org/page", "title": "Title", "source": "searxng"}],
            "searxng",
        ),
    )
    monkeypatch.setattr(
        j1,
        "fetch_document",
        lambda url: {"ok": True, "url": url, "title": "Title", "kind": "html", "text": "public detail " * 100},
    )
    job_id = j1.start_research("Investigate", ["public query"])["job_id"]
    j1.run_worker(once=True)
    status = j1.research_status(job_id)
    assert status["page_failures"] == [{"reason": "cache_capacity_reached", "count": 1}]
    assert j1.research_results(job_id)["results"][0]["error"] == "cache_capacity_reached"


@pytest.mark.parametrize(
    ("status", "body", "expected_allowed", "expected_delay"),
    [
        (200, b"User-agent: *\nDisallow: /private\nCrawl-delay: 4", False, 4.0),
        (404, b"", True, 1.0),
        (503, b"", False, 1.0),
    ],
)
def test_robots_policy_obeys_rules_and_caches(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: bytes,
    expected_allowed: bool,
    expected_delay: float,
) -> None:
    requests: list[str] = []

    def robots_response(url: str, **_kwargs: object) -> tuple[int, dict[str, str], bytes, str]:
        requests.append(url)
        return status, {}, body, url

    monkeypatch.setattr(j1_fetch, "_request_public", robots_response)
    target = "https://example.org/private/report"
    assert j1_fetch._robots_policy(target) == (expected_allowed, expected_delay)
    assert j1_fetch._robots_policy(target) == (expected_allowed, expected_delay)
    assert requests == ["https://example.org/robots.txt"]


@pytest.mark.parametrize(
    ("status", "headers", "body", "expected"),
    [
        (401, {"content-type": "text/html"}, b"", "access_restricted"),
        (503, {"content-type": "text/html"}, b"", "http_error"),
        (200, {"content-type": "image/png"}, b"image", "unsupported_media"),
        (200, {"content-type": "text/html"}, b"<p>short</p>", "empty_or_dynamic"),
        (200, {"content-type": "text/plain"}, b"captcha " * 20, "requires_human"),
        (200, {"content-type": "application/pdf"}, b"bad pdf", "pdf_extract_failed"),
    ],
)
def test_fetch_document_reports_specific_unavailable_reason(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    headers: dict[str, str],
    body: bytes,
    expected: str,
) -> None:
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (True, 1.0))
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)
    monkeypatch.setattr(
        j1_fetch,
        "_request_public",
        lambda *_args, **_kwargs: (status, headers, body, "https://example.org/report"),
    )
    if expected == "pdf_extract_failed":
        monkeypatch.setattr(j1_fetch, "_pdf_to_text", lambda _body: "")
    assert j1_fetch.fetch_document("https://example.org/report")["error"] == expected


def test_fetch_document_html_plain_text_and_pdf_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (True, 1.0))
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)
    responses = [
        (
            200,
            {"content-type": "text/html"},
            b"<title>Case</title><p>Public details " + b"x" * 100 + b"</p>",
            "https://example.org/report",
        ),
        (200, {"content-type": "text/plain"}, b"Public plain report " + b"x" * 100, "https://example.org/report.txt"),
        (200, {"content-type": "application/pdf"}, b"%PDF-1.4", "https://example.org/report.pdf"),
    ]
    monkeypatch.setattr(j1_fetch, "_request_public", lambda *_args, **_kwargs: responses.pop(0))
    monkeypatch.setattr(j1_fetch, "_pdf_to_text", lambda _body: "Public PDF report " + "x" * 100)
    html = j1_fetch.fetch_document("https://example.org/report")
    plain = j1_fetch.fetch_document("https://example.org/report.txt")
    pdf = j1_fetch.fetch_document("https://example.org/report.pdf")
    assert (html["kind"], html["title"]) == ("html", "Case")
    assert (plain["kind"], plain["title"]) == ("text", "")
    assert (pdf["kind"], pdf["title"]) == ("pdf", "report.pdf")
    assert all(result["ok"] and len(result["text"]) >= 80 for result in (html, plain, pdf))


def test_public_request_rejects_oversize_and_compressed_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status = 200

        def __init__(self, headers: dict[str, str], body: bytes) -> None:
            self.headers = headers
            self.body = body

        def getheaders(self) -> list[tuple[str, str]]:
            return list(self.headers.items())

        def read(self, count: int) -> bytes:
            return self.body[:count]

    class FakeConnection:
        def __init__(self, *_args: object) -> None:
            pass

        def request(self, *_args: object, **_kwargs: object) -> None:
            pass

        def getresponse(self) -> Response:
            return responses.pop(0)

        def close(self) -> None:
            pass

    monkeypatch.setattr(j1_fetch, "_public_address", lambda *_args: "1.1.1.1")
    monkeypatch.setattr(j1_fetch, "_PinnedHTTPS", FakeConnection)
    responses = [
        Response({"content-length": "101"}, b""),
        Response({}, b"x" * 101),
        Response({"content-encoding": "gzip"}, b"ok"),
        Response({"content-type": "text/plain"}, b"public"),
    ]
    for reason in ("document_too_large", "document_too_large", "unsupported_content_encoding"):
        with pytest.raises(ValueError, match=reason):
            j1_fetch._request_public("https://example.org/data", max_bytes=100)
    assert j1_fetch._request_public("https://example.org/data", max_bytes=100) == (
        200,
        {"content-type": "text/plain"},
        b"public",
        "https://example.org/data",
    )


def test_search_searxng_filters_private_results_then_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status = 200

        def read(self, _count: int) -> bytes:
            return b'{"results":[{"url":"https://example.org/article","title":"Public report"},{"url":"http://127.0.0.1/private","title":"Private"}]}'

    class FakeConnection:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def request(self, *_args: object, **_kwargs: object) -> None:
            pass

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            pass

    monkeypatch.setattr(j1_fetch.http.client, "HTTPConnection", FakeConnection)
    assert j1_fetch.search_public("public topic", searxng_url="http://127.0.0.1:8080")[0] == [
        {"url": "https://example.org/article", "title": "Public report", "source": "searxng"}
    ]
    with pytest.raises(ValueError, match="searxng_url_invalid"):
        j1_fetch._search_searxng("public topic", "http://example.org/search")

    monkeypatch.setattr(j1_fetch, "_search_searxng", lambda *_args: [])
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (True, 1.0))
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)
    monkeypatch.setattr(
        j1_fetch,
        "_request_public",
        lambda *_args, **_kwargs: (
            200,
            {"content-type": "text/html"},
            b'<a class="result__a" href="https://example.org/second">Another public report</a>',
            "https://html.duckduckgo.com/html/",
        ),
    )
    rows, source = j1_fetch.search_public("public topic", searxng_url="http://127.0.0.1:8080")
    assert source == "duckduckgo"
    assert rows == [{"url": "https://example.org/second", "title": "Another public report", "source": "duckduckgo"}]
