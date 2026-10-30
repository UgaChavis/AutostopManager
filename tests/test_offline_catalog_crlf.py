"""Real-rg catalog excerpts retain LF/CRLF matches through native MCP."""

import asyncio
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import config, mcp_telemetry, mcp_tools
from autostop_manager import offline_catalogs as catalogs


class Inert:
    def __init__(self, *args, **kwargs):
        pass


@pytest.fixture
def cache(tmp_path, monkeypatch):
    assert shutil.which("rg"), "these regression tests require real ripgrep"
    root = tmp_path / "catalogs"
    (root / "text").mkdir(parents=True)
    monkeypatch.setenv(catalogs.CATALOG_ROOT_ENV, str(root))
    # Windows constructs a local socket pair for the loop before the I/O guard.
    loop = asyncio.new_event_loop()

    def denied(*args, **kwargs):
        raise AssertionError("offline search must not access network or application storage")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, denied)
    for name in ("getaddrinfo", "create_connection"):
        monkeypatch.setattr(socket, name, denied)
    monkeypatch.setattr(sqlite3, "connect", denied)
    monkeypatch.setattr(config, "get_db_path", denied)
    monkeypatch.setattr(catalogs, "get_db_path", denied)
    original_popen = subprocess.Popen
    processes = []

    def only_catalog_rg(command, *args, **kwargs):
        assert isinstance(command, list) and command[0] == "rg"
        assert Path(command[-1]).resolve().is_relative_to(root)
        processes.append(command)
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", only_catalog_rg)
    yield root, loop, processes
    loop.close()


@pytest.fixture(params=["direct", "native"])
def search(request, cache, monkeypatch):
    root, loop, processes = cache
    if request.param == "native":
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
        monkeypatch.setattr(mcp_telemetry.LOGGER, "disabled", True)
        server = FastMCP("isolated-offline-crlf")
        mcp_tools.register_manager_tools(
            server, store=Inert(), store_client=Inert(), include_tools={"search_offline_parts_catalogs"}
        )
        tool = server._tool_manager.get_tool("search_offline_parts_catalogs")

    def invoke(query="ABC123", **arguments):
        before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        calls_before = len(processes)
        if request.param == "direct":
            response = catalogs.search_offline_parts_catalogs(query, **arguments)
        else:
            content, response = loop.run_until_complete(tool.run({"query": query, **arguments}, convert_result=True))
            assert json.loads(content[0].text) == response
        assert len(processes) == calls_before + 1
        after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        assert after == before, "search must never rewrite the supplied cache"
        return response

    return invoke


def install(root, lines, endings, *, format="pdf", suffix=".txt", **metadata):
    entry = {
        "catalog_id": "demo",
        "text_path": "text/demo" + suffix,
        "format": format,
        "text_extract_status": "ok",
        **metadata,
    }
    (root / "catalog_index.json").write_bytes(json.dumps({"entries": [entry]}).encode())
    content = b"".join(line.encode("utf-8") + endings[index % len(endings)] for index, line in enumerate(lines))
    (root / entry["text_path"]).write_bytes(content)


@pytest.mark.parametrize("endings", [(b"\n",), (b"\r\n",), (b"\r\n", b"\n")], ids=["lf", "crlf", "mixed"])
@pytest.mark.parametrize("prefix", ["", "x" * 21000], ids=["short-row", "long-row"])
@pytest.mark.parametrize("layout", ["pdf", "legacy-pdf", "tsv", "legacy-xlsx"])
def test_native_and_direct_search_retain_crlf_matches(cache, search, endings, prefix, layout):
    root, _loop, _processes = cache
    body = prefix + " ABC123 fits listed model"
    if layout == "pdf":
        install(root, ["\f[PAGE 7]", "Heading", body], endings, ocr_pages=[7])
        locator = {"page": 7}
    elif layout == "legacy-pdf":
        install(root, ["First page", "\fHeading", body], endings)
        locator = {"page": 2}
    elif layout == "tsv":
        install(
            root, ["Sheet\t1\tHeading", "Sheet\t2\tOther", "Sheet\t3\t" + body], endings, format="xlsx", suffix=".tsv"
        )
        locator = {"sheet": "Sheet", "row": 3}
    else:
        install(root, ["### Sheet", "Heading", body], endings, format="xlsx")
        locator = {"sheet": "Sheet", "row": 2}

    response = search()

    assert response["ok"] is True
    assert response["warnings"] == [] and response["search_incomplete"] is False
    assert response["scanned_catalogs"] == 1 and len(response["results"]) == 1
    hit = response["results"][0]
    assert hit["locator"] == locator
    assert "ABC123 fits listed model" in hit["excerpt"]
    assert len(hit["excerpt"]) <= catalogs.MAX_EXCERPT_CHARS
    assert hit["candidate_only"] is True and hit["fitment_status"] == "unverified_catalog_candidate"
    if layout in {"pdf", "legacy-pdf"}:
        assert hit["extraction_method"] == ("ocr" if layout == "pdf" else "text")
        assert hit["ocr_unverified"] is (layout == "pdf")


def test_crlf_keeps_line_numbers_redaction_and_result_limit(cache, search):
    root, _loop, _processes = cache
    vin = "A" * 17
    install(root, ["\f[PAGE 7]", f"ABC123 {vin}", "\f[PAGE 8]", "ABC123 next page"], (b"\r\n",))

    response = search(limit=1)

    assert len(response["results"]) == 1 and response["results"][0]["locator"] == {"page": 7}
    assert "[VIN REDACTED]" in response["results"][0]["excerpt"]
    assert vin not in json.dumps(response)
    assert response["result_limit_reached"] is True and response["search_incomplete"] is True
    assert response["warnings"] == []


def test_crlf_page_markers_are_excluded_and_query_stays_literal(cache, search):
    root, _loop, _processes = cache
    install(root, ["\f[PAGE 123]", "A.B123 actual part", "AXB123 different part"], (b"\r\n",))

    response = search("A.B123")

    assert len(response["results"]) == 1
    assert response["results"][0]["locator"] == {"page": 123}
    assert response["results"][0]["excerpt"] == "A.B123 actual part"
    assert response["warnings"] == []
    assert search("PAGE")["results"] == []
