"""Focused branch checks for J1 OCR and browser fallback integration."""

from __future__ import annotations

import pytest

from autostop_manager import j1_browser, j1_fetch, j1_ocr


def test_pdf_extraction_prefers_text_then_reports_ocr_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(j1_fetch, "_pdf_to_text", lambda _body: "Static PDF text")
    assert j1_fetch._extract_pdf(b"%PDF") == {
        "ok": True,
        "text": "Static PDF text",
        "extraction_method": "pdf_text",
    }

    monkeypatch.setattr(j1_fetch, "_pdf_to_text", lambda _body: "")
    monkeypatch.setattr(
        j1_ocr,
        "extract_scanned_pdf",
        lambda _body, **_kwargs: {"ok": True, "text": "OCR text", "extraction_method": "pdf_ocr"},
    )
    assert j1_fetch._extract_pdf(b"%PDF")["extraction_method"] == "pdf_ocr"

    monkeypatch.setattr(j1_ocr, "extract_scanned_pdf", lambda _body, **_kwargs: {"ok": False, "error": "ocr_timeout"})
    assert j1_fetch._extract_pdf(b"%PDF") == {
        "ok": False,
        "error": "ocr_timeout",
        "extraction_method": "pdf_ocr",
    }


def test_static_extraction_metadata_and_media_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        j1_fetch, "_extract_pdf", lambda _body: {"ok": True, "text": "PDF text", "extraction_method": "pdf_text"}
    )
    pdf = j1_fetch._extract_static_content("application/pdf", b"%PDF", "https://example.org/a.pdf", {})
    assert (pdf["kind"], pdf["title"], pdf["extraction_method"]) == ("pdf", "a.pdf", "pdf_text")

    html = j1_fetch._extract_static_content(
        "text/html", b"<title>Case</title><p>Public body</p>", "https://example.org/a", {"content-type": "text/html"}
    )
    assert (html["kind"], html["extraction_method"]) == ("html", "html_text")
    assert j1_fetch._extract_static_content("image/png", b"image", "https://example.org/a", {}) == {
        "ok": False,
        "error": "unsupported_media",
    }
    assert j1_fetch._extract_static_content(
        "text/plain", b"x" * (j1_fetch.MAX_HTML_BYTES + 1), "https://example.org/a", {}
    ) == {"ok": False, "error": "document_too_large"}


def test_browser_fallback_stays_gated_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://example.org/dynamic"
    assert j1_fetch._browser_fallback(url, "Title", allow_browser=False) == {
        "ok": False,
        "error": "browser_limit_reached",
    }
    monkeypatch.setattr(j1_browser, "isolation_verified", lambda: False)
    assert j1_fetch._browser_fallback(url, "Title", allow_browser=True) == {
        "ok": False,
        "error": "browser_isolation_unverified",
    }

    monkeypatch.setattr(j1_browser, "isolation_verified", lambda: True)
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (False, 1.0))
    assert j1_fetch._browser_fallback(url, "Title", allow_browser=True) == {
        "ok": False,
        "error": "robots_disallowed",
    }


def test_browser_fallback_records_renderer_failures_and_success(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://example.org/dynamic"
    monkeypatch.setattr(j1_browser, "isolation_verified", lambda: True)
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (True, 1.0))
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)
    monkeypatch.setattr(j1_browser, "render_page", lambda *_args, **_kwargs: {"ok": False, "error": "requires_human"})
    assert j1_fetch._browser_fallback(url, "Title", allow_browser=True) == {
        "ok": False,
        "error": "requires_human",
        "browser_attempted": True,
    }

    monkeypatch.setattr(j1_browser, "render_page", lambda *_args, **_kwargs: {"ok": True, "url": url, "text": "short"})
    assert j1_fetch._browser_fallback(url, "Title", allow_browser=True)["error"] == "browser_response_invalid"

    monkeypatch.setattr(
        j1_browser,
        "render_page",
        lambda *_args, **_kwargs: {
            "ok": True,
            "url": url,
            "title": "Rendered",
            "text": "Public rendered text " + "x" * 100,
        },
    )
    result = j1_fetch._browser_fallback(url, "Title", allow_browser=True)
    assert result["ok"] is True
    assert (result["title"], result["extraction_method"], result["browser_attempted"]) == (
        "Rendered",
        "browser_dom",
        True,
    )
