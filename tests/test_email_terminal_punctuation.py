"""Sentence punctuation cannot bypass the public email privacy boundary."""

import asyncio
from copy import deepcopy
import json
import socket
import sqlite3
import subprocess
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import j1_fetch, mcp_read_executor, mcp_telemetry, mcp_tools
from autostop_manager import web_research_gateway as gateway
from autostop_manager.listing_executor import BoundedListingExecutor


EMAIL = "synthetic@example.org"
PDF = "https://static.nhtsa.gov/odi/tsbs/2014/SB-10063500-2280.pdf"
VIN = "WBA" + "0" * 14


def encoded_contact(value, layers):
    for _ in range(layers):
        value = quote(value, safe="").replace(".", "%2E")
    return value


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("email privacy tests must not acquire network, process or DB data")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(subprocess, "run", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(sqlite3, "connect", denied)


@pytest.fixture
def in_memory_gateway(monkeypatch):
    calls = []
    payload = {"ok": True, "results": []}

    def invoke(name, arguments):
        calls.append((name, deepcopy(arguments)))
        return deepcopy(payload)

    monkeypatch.setattr(gateway, "_DEFAULT_GATEWAY", gateway.CapabilityWebResearchGatewayAdapter(invoke))
    return calls, payload


@pytest.fixture(scope="module")
def native_search():
    class Inert:
        def __init__(self, *args, **kwargs):
            pass

    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=1, timeout_seconds=2)
    with pytest.MonkeyPatch.context() as patch:
        for name in (
            "StoreIntegration",
            "StoreApiClient",
            "StoreOwnerApiClient",
            "StoreQuoteConductor",
            "StoreQuoteOwnerApi",
        ):
            patch.setattr(mcp_tools, name, Inert)
        for name in (
            "get_store_api_url",
            "get_store_read_token",
            "get_store_manage_token",
            "get_store_quote_token",
            "get_store_owner_token",
        ):
            patch.setattr(mcp_tools, name, lambda: "")
        patch.setattr(mcp_read_executor, "_WEB_EXECUTOR", pool)
        patch.setattr(mcp_telemetry.LOGGER, "disabled", True)
        server = FastMCP("offline-email-punctuation")
        mcp_tools.register_manager_tools(
            server, store=Inert(), store_client=Inert(), include_tools={"search_web_multi"}
        )
        tool = server._tool_manager.get_tool("search_web_multi")
        assert tool.is_async
        try:
            yield tool
        finally:
            pool.close()
            assert not any(thread.is_alive() for thread in pool._pool._threads)


@pytest.fixture(params=["public", "native"])
def search(request, native_search):
    def call(query):
        if request.param == "public":
            return gateway.search_web_multi(query=query)
        content, result = asyncio.run(native_search.run({"query": query}, convert_result=True))
        assert json.loads(content[0].text) == result
        return result

    return call


@pytest.mark.parametrize("email", [EMAIL, "synthetic.name+tag@sub.example-domain.org"])
@pytest.mark.parametrize("punctuation", ["", ",", ".", "...", ".\n"])
def test_detection_and_redaction_keep_terminal_punctuation(email, punctuation):
    text = "Contact " + email + punctuation

    assert j1_fetch.contains_sensitive(text)
    assert j1_fetch.redact_sensitive(text) == "Contact [redacted]" + punctuation


@pytest.mark.parametrize(
    "tail", ["_x", "1", "-x", ".label_", ".label1", ".label-", ".x", ".1", "._x", ".-x", "..label_"]
)
def test_incomplete_domain_continuation_is_not_redacted_as_a_partial_email(tail):
    text = EMAIL + tail

    assert not j1_fetch.contains_sensitive(text)
    assert j1_fetch.redact_sensitive(text) == text


@pytest.mark.parametrize("punctuation", [".", "..."])
@pytest.mark.parametrize("layers", [0, 1, 2])
def test_public_query_rejects_email_before_callback_without_echo(search, in_memory_gateway, punctuation, layers):
    query = "repair " + encoded_contact(EMAIL + punctuation, layers)
    calls, _payload = in_memory_gateway

    result = search(query)

    assert calls == []
    assert result["ok"] is False
    assert result["query"] == ""
    assert result["error"] == {"code": "web_research_personal_contact_rejected", "retryable": False}
    assert query not in str(result)
    assert EMAIL not in str(result)


@pytest.mark.parametrize("component", ["path", "query", "fragment"])
@pytest.mark.parametrize("layers", [0, 1, 2])
@pytest.mark.parametrize("punctuation", [".", "..."])
def test_url_contact_guard_rejects_terminal_periods_before_page_adapter(
    in_memory_gateway, component, layers, punctuation
):
    value = encoded_contact(EMAIL + punctuation, layers)
    suffix = {"path": "/" + value, "query": "/report?email=" + value, "fragment": "/report#" + value}[component]
    url = "https://example.org" + suffix
    calls, _payload = in_memory_gateway

    assert j1_fetch.public_url(url) == ""
    for method in (gateway.fetch_page_excerpt, gateway.fetch_page_browser):
        result = method(url=url)
        assert result["error"]["code"] == "web_page_url_invalid"
    assert calls == []


def test_public_results_redact_email_without_removing_sentence_punctuation(search, in_memory_gateway):
    calls, payload = in_memory_gateway
    payload["results"] = [{"url": "https://example.org/public", "title": EMAIL + ".", "snippet": EMAIL + "..."}]

    result = search("Renault 7700100008")

    assert len(calls) == 1
    assert result["ok"] is True
    assert result["results"][0]["title"] == "[redacted]."
    assert result["results"][0]["snippet"] == "[redacted]..."
    assert EMAIL not in str(result)


@pytest.mark.parametrize(
    ("query", "safe_query", "vin_redacted"),
    [
        ("Renault 7700100008", "Renault 7700100008", False),
        (PDF, PDF, False),
        ("Renault 7700100008 " + VIN, "Renault 7700100008", True),
    ],
)
def test_ordinary_parts_pdf_and_legacy_vin_query_behavior_is_preserved(
    search, in_memory_gateway, query, safe_query, vin_redacted
):
    calls, _payload = in_memory_gateway

    result = search(query)

    assert result["ok"] is True
    assert result["query"] == safe_query
    assert result["vin_redacted"] is vin_redacted
    assert calls == [("search_web_multi", {"query": safe_query, "limit": 5})]
    assert j1_fetch.public_url(PDF) == PDF
    assert j1_fetch.redact_sensitive(PDF) == PDF
