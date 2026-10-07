from __future__ import annotations

import pytest

from autostop_manager import j1_fetch
from autostop_manager.crm_mcp_web_research import _capability_failure
from autostop_manager.web_research_gateway import (
    CapabilityWebResearchGatewayAdapter,
    normalize_web_page_response,
    normalize_web_research_response,
)


@pytest.mark.parametrize(
    "body",
    [
        {
            "ok": False,
            "error": {"code": "http_not_found", "retryable": False},
            "status_code": 404,
            "content_type": "application/pdf",
        },
        {
            "ok": True,
            "data": {
                "ok": False,
                "error": {"code": "http_not_found", "retryable": False},
                "status_code": 404,
                "content_type": "application/pdf",
            },
        },
    ],
)
def test_transport_preserves_safe_error_metadata(body):
    failure = _capability_failure(body)
    assert failure["error"] == {"code": "http_not_found", "retryable": False}
    assert failure["status_code"] == 404
    assert failure["content_type"] == "application/pdf"
    assert failure["cause_unknown"] is False


@pytest.mark.parametrize(
    "code,status,media",
    [
        ("http_not_found", 404, "application/pdf"),
        ("crm_mcp_capability_failed", 0, ""),
        ("unsupported_media", 0, "application/pdf"),
        ("unsupported_media", 200, "image/png"),
    ],
)
def test_no_pdf_fallback_for_404_unknown_status_or_other_media(monkeypatch, code, status, media):
    monkeypatch.setattr(j1_fetch, "fetch_document", lambda *_args, **_kwargs: pytest.fail("forbidden fallback"))
    adapter = CapabilityWebResearchGatewayAdapter(
        lambda *_: {
            "ok": False,
            "error": {"code": code, "retryable": False},
            "status_code": status,
            "content_type": media,
        }
    )
    result = adapter.fetch_page_excerpt(url="https://example.com/disc.pdf")
    assert result["ok"] is False
    assert result["error"]["retryable"] is False
    assert result["cause_unknown"] is (code == "crm_mcp_capability_failed")


def test_pdf_uses_existing_guarded_static_reader_without_browser(monkeypatch):
    calls = []

    def fetch(url, *, allow_browser):
        calls.append((url, allow_browser))
        return {
            "ok": True,
            "kind": "pdf",
            "url": url,
            "title": "Brake catalogue",
            "text": "Brake OE " * 30,
            "extraction_method": "pdf_text",
        }

    monkeypatch.setattr(j1_fetch, "fetch_document", fetch)
    adapter = CapabilityWebResearchGatewayAdapter(
        lambda *_: {
            "ok": False,
            "error": {"code": "unsupported_media", "retryable": False},
            "status_code": 200,
            "content_type": "application/pdf",
        }
    )
    result = adapter.fetch_page_excerpt(url="https://example.com/disc", max_chars=40)
    assert calls == [("https://example.com/disc", False)]
    assert result["ok"] is True
    assert result["status_code"] == 200
    assert len(result["excerpt"]) == 40
    assert result["truncated"] is True
    assert result["requested_chars"] == result["effective_chars"] == 40
    assert result["acquisition_method"] == "guarded_static_pdf"
    assert result["extraction_method"] == "pdf_text"


def test_search_zero_with_success_is_empty_and_all_failures_are_unavailable():
    base = {
        "results": [],
        "providers": [
            {"provider": "searxng", "status": "success", "result_count": 0},
            {"provider": "duckduckgo", "status": "error", "error_code": "search_challenge", "retryable": False},
        ],
    }
    empty = normalize_web_research_response(base, query="brake disc", limit=3)
    assert empty["ok"] is True
    assert empty["outcome"] == "empty_result"
    assert empty["providers"][1]["error_code"] == "search_challenge"
    assert empty["acquisition_method"] == "search_index"
    base["providers"] = base["providers"][1:]
    assert normalize_web_research_response(base, query="brake disc", limit=3)["outcome"] == "search_unavailable"


def test_legacy_page_does_not_fabricate_live_acquisition_or_status():
    result = normalize_web_page_response(
        {"ok": True, "data": {"ok": True, "excerpt": "Brake disc"}},
        capability="fetch_page_excerpt",
        url="https://example.com/disc",
        max_chars=80,
    )
    assert result["status_code"] == 0
    assert result["acquisition_method"] == "unknown"
