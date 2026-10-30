"""A case variant of www cannot turn one observed website into independent sources."""

import asyncio
from contextlib import closing
from copy import deepcopy
from html import escape
from io import BytesIO
import json
import socket
import sqlite3
import subprocess
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import automotive_labor as labor
from autostop_manager import mcp_read_executor, mcp_tools, work_pricing as pricing, work_pricing_research as research
from autostop_manager.listing_executor import BoundedListingExecutor


OPERATION = "замена масла"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("source identity regression must not access external state")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(sqlite3, "connect", denied)
    monkeypatch.setattr(subprocess.Popen, "__init__", denied)
    monkeypatch.setattr(pricing, "_load_labor_experience", denied)
    monkeypatch.setattr(research, "urlopen", denied)


@pytest.fixture
def native_tools(monkeypatch):
    class Inert:
        def __init__(self, *_args, **_kwargs):
            pass

    for name in (
        "StoreIntegration",
        "StoreApiClient",
        "StoreOwnerApiClient",
        "StoreQuoteConductor",
        "StoreQuoteOwnerApi",
        "AutomationControlClient",
    ):
        monkeypatch.setattr(mcp_tools, name, Inert)
    for name in (
        "get_store_api_url",
        "get_store_read_token",
        "get_store_manage_token",
        "get_store_quote_token",
        "get_store_owner_token",
    ):
        monkeypatch.setattr(mcp_tools, name, lambda: "")
    server = FastMCP("synthetic-derived-source-case")
    mcp_tools.register_manager_tools(
        server,
        store=Inert(),
        store_client=Inert(),
        include_tools={"collect_work_price_evidence", "estimate_repair_work_cost"},
    )
    pool = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=5)
    monkeypatch.setattr(mcp_read_executor, "_WEB_EXECUTOR", pool)
    try:
        yield server._tool_manager._tools
    finally:
        pool.close()


@pytest.mark.parametrize("redirected", [False, True])
@pytest.mark.parametrize(
    "authority,expected",
    [
        ("www.Garage.EXAMPLE", "Garage.EXAMPLE"),
        ("WWW.Garage.EXAMPLE", "Garage.EXAMPLE"),
        ("WwW.Garage.EXAMPLE", "Garage.EXAMPLE"),
        ("wWw.Garage.EXAMPLE", "Garage.EXAMPLE"),
        ("Garage.EXAMPLE", "Garage.EXAMPLE"),
        ("www2.Garage.EXAMPLE", "www2.Garage.EXAMPLE"),
        ("mywww.Garage.EXAMPLE", "mywww.Garage.EXAMPLE"),
        ("www.Other.EXAMPLE", "Other.EXAMPLE"),
        ("WWW.Other.EXAMPLE", "Other.EXAMPLE"),
    ],
)
def test_only_www_prefix_case_changes_derived_identity(authority, expected, redirected):
    url = f"https://{authority}/Price/A?Version=Original"
    if redirected:
        url = "https://duckduckgo.com/l/?uddg=" + quote(url, safe="")
    assert research._safe_source_from_url(url) == expected
    assert research._source_identity(expected) == expected.casefold()


@pytest.mark.parametrize("label", ["Garage Alpha", "https://evidence.example/Case?Version=Original"])
def test_explicit_source_label_and_uri_are_not_rewritten(label):
    row = {
        "source": label,
        "url": "https://WWW.Garage.EXAMPLE/Price?Version=Original",
        "title": f"{OPERATION}: только работа 1000 руб.",
    }
    snapshot = deepcopy(row)
    rows, source = research.parse_public_work_evidence(
        row, "synthetic", [{"normalized_name": OPERATION}], "labor_prices"
    )
    assert row == snapshot
    assert source["primary_lineage"] == rows[0]["source"]["primary_lineage"] == label
    assert source["locator"] == row["url"]


@pytest.mark.parametrize("mode", ["direct", "native", "native-json"])
@pytest.mark.parametrize("route", ["ready", "legacy"])
@pytest.mark.parametrize(
    "hosts,source_count",
    [
        (("www.same.example", "WWW.same.example", "WwW.same.example"), 0),
        (("www.Same.example", "www.same.EXAMPLE", "www.same.example"), 0),
        (("same.example", "SAME.example", "Same.EXAMPLE"), 0),
        (("www.one.example", "WWW.two.example", "WwW.three.example"), 3),
        (("www.same.example", "www2.same.example", "mywww.same.example"), 3),
    ],
)
def test_real_search_html_to_collector_and_estimator_keeps_source_independence(
    monkeypatch, native_tools, mode, route, hosts, source_count
):
    urls = [f"https://{host}/Price/{index}?Version=Original" for index, host in enumerate(hosts)]
    markup = "".join(
        f'<div class="result"><a class="result__a" href="{escape(url, quote=True)}">{OPERATION}</a>'
        f'<div class="result__snippet">Только работа {1000 + 100 * index} руб. Красноярск.</div></div>'
        for index, url in enumerate(urls)
    )
    reads = []

    def html_response(request, **_kwargs):
        assert request.full_url.startswith("https://html.duckduckgo.com/html/?q=")
        reads.append(request.full_url)
        return closing(BytesIO(markup.encode()))

    monkeypatch.setattr(research, "urlopen", html_response)
    monkeypatch.setattr(
        research,
        "build_public_research_queries",
        lambda **_kwargs: {"labor_prices": ["synthetic price"], "labor_times": []},
    )

    def invoke(name, arguments):
        snapshot = deepcopy(arguments)
        if mode == "direct":
            function = (
                labor.collect_work_price_evidence
                if name == "collect_work_price_evidence"
                else pricing.estimate_repair_work_cost
            )
            response = function(**arguments)
        else:
            wire = {
                key: json.dumps(value) if mode == "native-json" and isinstance(value, (dict, list)) else value
                for key, value in arguments.items()
            }
            content, response = asyncio.run(native_tools[name].run(wire, convert_result=True))
            assert json.loads(content[0].text) == response
        assert arguments == snapshot
        return response

    inputs = {"make": "DEMO", "work_items": [OPERATION], "use_internal_experience": False}
    if route == "ready":
        evidence = invoke("collect_work_price_evidence", {"work_items": [OPERATION], "max_queries": 1})
        assert evidence["execution"]["network_calls"] == 1  # One synthetic transport read, never a real connection.
        assert [row["source"]["locator"] for row in evidence["data"]["observations"]] == urls
        assert [row["price_rub"] for row in evidence["data"]["observations"]] == [1000, 1100, 1200]
        inputs.update(price_evidence=evidence, auto_research=False)
    response = invoke("estimate_repair_work_cost", inputs)
    operation = response["operation_estimates"][0]
    assert len(reads) == 1
    assert operation["sample"]["matched_valid_count"] == 3
    assert operation["sample"]["source_count"] == source_count
    assert operation["market_region"] == ("krasnoyarsk" if source_count else None)
    assert response["autostop_price_rub"] == (1600 if source_count else None)
    assert response["recommended_total_works_rub"] == (1600 if source_count else None)
