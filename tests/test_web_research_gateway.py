from __future__ import annotations

from autostop_manager.web_research_gateway import (
    CapabilityWebResearchGatewayAdapter,
    DuckDuckGoWebResearchGateway,
    fetch_page_browser,
    fetch_page_excerpt,
    install_web_research_gateway,
    research_part_public_evidence,
    search_web_multi,
)


def test_capability_adapter_normalizes_e8_style_payload_and_enforces_domains():
    captured = {}

    def invoke(name, arguments):
        captured["name"] = name
        captured["arguments"] = arguments
        return {
            "ok": True,
            "data": {
                "query": "FORD 1712024",
                "provider_order": ["searxng", "duckduckgo"],
                "providers": [{"provider": "searxng", "status": "success"}],
                "fallback_used": True,
                "results": [
                    {
                        "title": "Allowed result",
                        "url": "https://www.nhtsa.gov/recalls/1712024",
                        "snippet": "Reference",
                        "provider": "searxng",
                    },
                    {
                        "title": "Rejected result",
                        "url": "https://untrusted.example/1712024",
                        "snippet": "Reference",
                    },
                ],
            },
        }

    result = CapabilityWebResearchGatewayAdapter(invoke).search_web_multi(
        query="FORD 1712024",
        limit=3,
        allowed_domains=["nhtsa.gov"],
        providers=["searxng", "duckduckgo"],
    )

    assert captured == {
        "name": "search_web_multi",
        "arguments": {
            "query": "FORD 1712024",
            "limit": 3,
            "allowed_domains": ["nhtsa.gov"],
            "providers": ["searxng", "duckduckgo"],
        },
    }
    assert result["ok"] is True
    assert result["schema"] == "WebResearchGatewayV1"
    assert result["provider_order"] == ["searxng", "duckduckgo"]
    assert result["fallback_used"] is True
    assert result["results"] == [
        {
            "title": "Allowed result",
            "url": "https://www.nhtsa.gov/recalls/1712024",
            "snippet": "Reference",
            "provider": "searxng",
            "source": "www.nhtsa.gov",
        }
    ]


def test_part_evidence_adapter_calls_e8_v1_capability_with_bounded_pages():
    captured = {}

    def invoke(name, arguments):
        captured["name"] = name
        captured["arguments"] = arguments
        return {
            "contract_version": "autostop.web-research.v1",
            "operation": "part_public_evidence",
            "read_only": True,
            "ok": True,
            "status": "evidence_found",
            "results": [
                {
                    "title": "Front pads",
                    "url": "https://partsouq.com/en/catalog/genuine/parts?number=1712024",
                    "snippet": "1712024",
                    "domain": "partsouq.com",
                    "provider": "searxng",
                    "source_id": "partsouq_catalog",
                    "source_type": "oem_catalog",
                    "source_authorized": True,
                }
            ],
        }

    result = CapabilityWebResearchGatewayAdapter(invoke).research_part_public_evidence(
        query="FORD 1712024",
        limit=10,
        providers=["searxng"],
        max_pages=9,
    )

    assert captured == {
        "name": "research_part_public_evidence",
        "arguments": {"query": "FORD 1712024", "limit": 5, "providers": ["searxng"], "max_pages": 2},
    }
    assert result["ok"] is True
    assert result["capability"] == "research_part_public_evidence"
    assert result["results"][0]["source_authorized"] is True
    assert result["results"][0]["source_id"] == "partsouq_catalog"
    assert result["results"][0]["domain"] == "partsouq.com"


def test_local_ddg_fallback_is_explicit_and_filters_domains():
    captured = []

    def search(query, *, timeout_seconds):
        captured.append((query, timeout_seconds))
        return {
            "results": [
                {
                    "title": "Allowed via redirect",
                    "url": "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.nhtsa.gov%2Frecalls%2F1712024",
                    "snippet": "Reference",
                },
                {
                    "title": "Rejected",
                    "url": "https://untrusted.example/1712024",
                    "snippet": "Reference",
                },
            ]
        }

    result = DuckDuckGoWebResearchGateway(search).search_web_multi(
        query="FORD 1712024",
        limit=3,
        allowed_domains=["nhtsa.gov"],
        timeout_seconds=7,
    )

    assert captured == [("FORD 1712024", 7)]
    assert result["ok"] is True
    assert result["adapter"] == "local_duckduckgo_fallback"
    assert result["fallback_used"] is True
    assert result["provider_order"] == ["duckduckgo"]
    assert result["results"][0]["url"].startswith("https://duckduckgo.com/l/")
    assert len(result["results"]) == 1


def test_local_ddg_fallback_normalizes_provider_failure_without_error_text():
    def failed_search(*_args, **_kwargs):
        raise TimeoutError("temporary upstream error")

    result = DuckDuckGoWebResearchGateway(failed_search).search_web_multi(query="FORD 1712024")

    assert result["ok"] is False
    assert result["fallback_used"] is True
    assert result["error"] == {"code": "web_search_provider_failed", "retryable": True}


def test_part_evidence_adapter_removes_separator_formatted_vin_before_invocation():
    captured = []
    vin = "WBA/000000/00000000"

    def invoke(name, arguments):
        captured.append((name, arguments))
        return {"ok": True, "results": []}

    result = CapabilityWebResearchGatewayAdapter(invoke).research_part_public_evidence(
        query=f"front brake pads {vin}",
        max_pages=1,
    )

    assert captured == [
        (
            "research_part_public_evidence",
            {"query": "front brake pads", "limit": 5, "max_pages": 1},
        )
    ]
    assert result["query"] == "front brake pads"
    assert result["vin_redacted"] is True


def test_part_evidence_adapter_removes_mixed_separator_vin_before_invocation():
    captured = []
    vin = "WBA 000000/00000000"

    def invoke(name, arguments):
        captured.append((name, arguments))
        return {"ok": True, "results": []}

    result = CapabilityWebResearchGatewayAdapter(invoke).research_part_public_evidence(
        query=f"front brake pads {vin}",
        max_pages=1,
    )

    assert captured == [
        (
            "research_part_public_evidence",
            {"query": "front brake pads", "limit": 5, "max_pages": 1},
        )
    ]
    assert result["vin_redacted"] is True


def test_installed_gateway_exception_becomes_safe_structured_failure():
    class FailingGateway:
        def search_web_multi(self, **_kwargs):
            raise KeyError("private upstream details")

        def research_part_public_evidence(self, **_kwargs):
            raise KeyError("private upstream details")

    install_web_research_gateway(FailingGateway())
    try:
        generic = search_web_multi(query="FORD 1712024")
        evidence = research_part_public_evidence(query="FORD 1712024")
    finally:
        install_web_research_gateway(None)

    for result in (generic, evidence):
        assert result["ok"] is False
        assert result["error"] == {"code": "web_research_gateway_failed", "retryable": True}
        assert "private upstream details" not in str(result)


def test_vin_only_query_is_rejected_without_invoking_gateway():
    invoked = []

    def invoke(*args):
        invoked.append(args)
        return {"ok": True, "results": []}

    result = CapabilityWebResearchGatewayAdapter(invoke).research_part_public_evidence(query="WBA00000000000000")

    assert invoked == []
    assert result["ok"] is False
    assert result["query"] == ""
    assert result["vin_redacted"] is True
    assert result["error"] == {"code": "vin_like_query_rejected", "retryable": False}


def test_brand_and_ten_digit_article_are_not_redacted_as_vin():
    calls = []

    def invoke(name, arguments):
        calls.append((name, arguments))
        return {"ok": True, "data": {"results": []}}

    adapter = CapabilityWebResearchGatewayAdapter(invoke)
    part = adapter.search_web_multi(query="Renault 7700100008 цена Красноярск")
    vin = adapter.search_web_multi(query="WBA 000000/00000000")

    assert part["ok"] is True
    assert part["query"] == "Renault 7700100008 цена Красноярск"
    assert part["vin_redacted"] is False
    assert calls == [("search_web_multi", {"query": "Renault 7700100008 цена Красноярск", "limit": 5})]
    assert vin["ok"] is False
    assert vin["vin_redacted"] is True


def test_page_adapter_forwards_bounded_browser_request_and_sanitizes_evidence():
    calls = []

    def invoke(name, arguments):
        calls.append((name, arguments))
        return {
            "ok": True,
            "data": {
                "ok": True,
                "final_url": "https://example.com/part",
                "title": "Part WBA00000000000000",
                "excerpt": "Price 4200 RUB; VIN WBA00000000000000",
                "status_code": 200,
                "access_flags": ["login_required"],
                "requires_human": True,
                "links": [
                    {"url": "https://example.com/next", "text": "Next"},
                    {"url": "https://example.com/WBA00000000000000", "text": "Private"},
                ],
            },
        }

    result = CapabilityWebResearchGatewayAdapter(invoke).fetch_page_browser(
        url="https://example.com/part", max_chars=99999, wait_ms=99999
    )

    assert calls == [("fetch_page_browser", {"url": "https://example.com/part", "max_chars": 8000, "wait_ms": 5000})]
    assert result["ok"] is True
    assert result["vin_redacted"] is True
    assert result["requires_human"] is True
    assert result["links"] == [{"url": "https://example.com/next", "text": "Next", "domain": "example.com"}]
    assert "WBA00000000000000" not in str(result)


def test_page_adapter_rejects_vin_url_and_unconfigured_page_fails_closed():
    calls = []
    adapter = CapabilityWebResearchGatewayAdapter(lambda *args: calls.append(args))
    rejected = adapter.fetch_page_excerpt(url="https://example.com/WBA00000000000000")
    assert rejected["error"]["code"] == "web_page_url_invalid"
    assert calls == []

    install_web_research_gateway(None)
    result = fetch_page_excerpt(url="https://example.com/part")
    assert result["error"]["code"] == "web_page_gateway_unavailable"


def test_generic_web_research_rejects_contact_data_before_any_external_call():
    calls = []

    def invoke(name, arguments):
        calls.append((name, arguments))
        return {"ok": True, "data": {"results": []}}

    install_web_research_gateway(CapabilityWebResearchGatewayAdapter(invoke))
    try:
        for query in ("pads owner@example.com", "pads +7 (999) 123-45-67"):
            result = search_web_multi(query=query)
            assert result["ok"] is False
            assert result["error"]["code"] == "web_research_personal_contact_rejected"
            assert query not in str(result)
        page = fetch_page_excerpt(url="https://example.com/part?email=owner%40example.com")
        phone_page = fetch_page_browser(url="https://example.com/part?phone=%2B79991234567")
        valid_part = search_web_multi(query="7700100008")
    finally:
        install_web_research_gateway(None)

    assert page["error"]["code"] == "web_page_url_invalid"
    assert phone_page["error"]["code"] == "web_page_url_invalid"
    assert valid_part["ok"] is True
    assert calls == [("search_web_multi", {"query": "7700100008", "limit": 5})]


def test_installed_page_gateway_returns_excerpt_and_browser_without_private_errors():
    calls = []

    def invoke(name, arguments):
        calls.append((name, arguments))
        return {
            "ok": True,
            "data": {
                "ok": True,
                "url": arguments["url"],
                "final_url": arguments["url"],
                "excerpt": "Public price 4200 RUB",
            },
        }

    install_web_research_gateway(CapabilityWebResearchGatewayAdapter(invoke))
    try:
        excerpt = fetch_page_excerpt(url="https://example.com/part")
        browser = fetch_page_browser(url="https://example.com/part", wait_ms=0)
    finally:
        install_web_research_gateway(None)

    assert excerpt["ok"] is True
    assert browser["ok"] is True
    assert excerpt["excerpt"] == "Public price 4200 RUB"
    assert [name for name, _ in calls] == ["fetch_page_excerpt", "fetch_page_browser"]
