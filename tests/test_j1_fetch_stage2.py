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


def test_forced_browser_uses_no_parser_preflight_then_renders_and_redacts(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://example.org/dynamic"
    final_url = "https://example.org/final"
    vin = "WBA00000000000000"
    email = "owner@example.com"
    phone = "+7 (999) 123-45-67"
    secret = "token=super-secret-value"
    robots_calls = []
    rate_calls = []
    request_calls = []
    render_calls = []

    monkeypatch.setattr(j1_browser, "isolation_verified", lambda: True)
    monkeypatch.setattr(
        j1_fetch,
        "_robots_policy",
        lambda target: robots_calls.append(target) or (True, 1.0),
    )
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda origin, delay: rate_calls.append((origin, delay)))
    monkeypatch.setattr(
        j1_fetch,
        "_extract_static_content",
        lambda *_args, **_kwargs: pytest.fail("Manager browser preflight must not parse page content"),
    )

    def request(target, **kwargs):
        request_calls.append((target, kwargs))
        return (
            200,
            {"content-type": "text/html"},
            b"untrusted body must be ignored",
            final_url,
        )

    def render(target, **kwargs):
        render_calls.append((target, kwargs))
        return {
            "ok": True,
            "url": target,
            "title": f"Rendered {email}",
            "text": f"Rendered {vin} {email} {phone} {secret} " + "x" * 120,
        }

    monkeypatch.setattr(j1_fetch, "_request_public", request)
    monkeypatch.setattr(j1_browser, "render_page", render)

    result = j1_fetch.fetch_browser_document(url, max_chars=300)

    assert result["ok"] is True
    assert result["url"] == final_url
    assert result["extraction_method"] == "browser_dom"
    assert request_calls == [
        (
            url,
            {
                "max_bytes": j1_fetch.MAX_HTML_BYTES,
                "check_redirect_robots": True,
                "read_body": False,
            },
        )
    ]
    assert render_calls == [(final_url, {"max_chars": 300, "timeout_seconds": 20})]
    assert robots_calls == [url, final_url]
    assert rate_calls == [("https://example.org", 1.0), ("https://example.org", 1.0)]
    assert all(value not in str(result) for value in (vin, email, phone, secret))
    assert "[redacted]" in str(result)


@pytest.mark.parametrize("content_type", ["application/pdf", "application/json", "image/png", ""])
def test_forced_browser_rejects_non_html_without_parsing(monkeypatch: pytest.MonkeyPatch, content_type: str) -> None:
    renderer_calls = []
    monkeypatch.setattr(j1_browser, "isolation_verified", lambda: True)
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (True, 1.0))
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)
    monkeypatch.setattr(
        j1_fetch,
        "_extract_static_content",
        lambda *_args, **_kwargs: pytest.fail("Manager browser preflight must not parse page content"),
    )
    monkeypatch.setattr(
        j1_fetch,
        "_request_public",
        lambda *_args, **_kwargs: (
            200,
            {"content-type": content_type},
            b"%PDF or other attacker-controlled body",
            "https://example.org/file",
        ),
    )
    monkeypatch.setattr(
        j1_browser,
        "render_page",
        lambda *args, **kwargs: renderer_calls.append((args, kwargs)),
    )

    result = j1_fetch.fetch_browser_document("https://example.org/file")

    assert result == {"ok": False, "error": "unsupported_media"}
    assert renderer_calls == []


def test_forced_browser_unattested_state_makes_no_external_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(j1_browser, "isolation_verified", lambda: False)
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda *_args: pytest.fail("must not read robots"))
    monkeypatch.setattr(j1_fetch, "_request_public", lambda *_args, **_kwargs: pytest.fail("must not fetch"))
    monkeypatch.setattr(j1_browser, "render_page", lambda *_args, **_kwargs: pytest.fail("must not render"))

    assert j1_fetch.fetch_browser_document("https://example.org/page") == {
        "ok": False,
        "error": "browser_isolation_unverified",
    }


@pytest.mark.parametrize(
    ("redirect_failure", "expected"),
    [(False, "robots_disallowed"), (True, "redirect_robots_disallowed")],
)
def test_forced_browser_policy_denial_never_reaches_renderer(
    monkeypatch: pytest.MonkeyPatch, redirect_failure: bool, expected: str
) -> None:
    renderer_calls = []
    monkeypatch.setattr(j1_browser, "isolation_verified", lambda: True)
    monkeypatch.setattr(j1_browser, "render_page", lambda *args, **kwargs: renderer_calls.append((args, kwargs)))
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)
    if redirect_failure:
        monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (True, 1.0))

        def reject_redirect(*_args, **_kwargs):
            raise ValueError("redirect_robots_disallowed")

        monkeypatch.setattr(j1_fetch, "_request_public", reject_redirect)
    else:
        monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (False, 1.0))
        monkeypatch.setattr(j1_fetch, "_request_public", lambda *_args, **_kwargs: pytest.fail("must not fetch"))

    result = j1_fetch.fetch_browser_document("https://example.org/page")

    assert result["ok"] is False
    assert result["error"] == expected
    assert renderer_calls == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "access_restricted"),
        (403, "access_restricted"),
        (429, "rate_limited"),
        (503, "http_server_error"),
        (404, "http_error"),
    ],
)
def test_forced_browser_rejects_non_success_preflight_without_rendering(
    monkeypatch: pytest.MonkeyPatch, status: int, expected: str
) -> None:
    renderer_calls = []
    monkeypatch.setattr(j1_browser, "isolation_verified", lambda: True)
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (True, 1.0))
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)
    monkeypatch.setattr(j1_fetch.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(
        j1_fetch,
        "_request_public",
        lambda *_args, **_kwargs: (status, {"content-type": "text/html"}, b"", "https://example.org/page"),
    )
    monkeypatch.setattr(j1_browser, "render_page", lambda *args, **kwargs: renderer_calls.append((args, kwargs)))

    result = j1_fetch.fetch_browser_document("https://example.org/page")

    assert result["ok"] is False
    assert result["error"] == expected
    assert renderer_calls == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [("short", "browser_response_invalid"), ("CAPTCHA " + "x" * 100, "requires_human")],
)
def test_forced_browser_rejects_short_or_human_gated_render(
    monkeypatch: pytest.MonkeyPatch, text: str, expected: str
) -> None:
    monkeypatch.setattr(j1_browser, "isolation_verified", lambda: True)
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (True, 1.0))
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)
    monkeypatch.setattr(
        j1_fetch,
        "_request_public",
        lambda *_args, **_kwargs: (
            200,
            {"content-type": "text/html"},
            b"<p>Rich static preflight " + b"x" * 100 + b"</p>",
            "https://example.org/page",
        ),
    )
    monkeypatch.setattr(
        j1_browser,
        "render_page",
        lambda url, **_kwargs: {"ok": True, "url": url, "title": "Rendered", "text": text},
    )

    result = j1_fetch.fetch_browser_document("https://example.org/page")

    assert result["ok"] is False
    assert result["error"] == expected
    assert result["browser_attempted"] is True
