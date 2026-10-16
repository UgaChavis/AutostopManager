"""Preserve public research-result pagination for large nonnegative cursors.

Use a synthetic temporary SQLite/FTS corpus and guarded external boundaries.
Six cursors run through listing and FTS, plus missing-job and input controls.
Two native SDK calls repeat the huge-cursor listing and FTS expectations:
ten scenario families, eighteen calls, six SQL-path and two interface repeats.
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from autostop_manager import j1_research as j1


JOB_ID = "a" * 32
OTHER_JOB_ID = "b" * 32
MISSING_JOB_ID = "f" * 32
OWN_DOCUMENT_IDS = frozenset({"1" * 32, "2" * 32})
OTHER_DOCUMENT_ID = "3" * 32


@pytest.fixture
def ordinary_corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    ordinary_root = tmp_path / "ordinary"
    private_root = tmp_path / "private-vin"
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(ordinary_root))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(private_root))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "0")
    monkeypatch.delenv("AUTOSTOP_J1_SEARXNG_URL", raising=False)

    def forbidden_external(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Synthetic results pagination must not call a network or subprocess boundary")

    # Guards only; no product API, routing, SQL, result, or FTS function is mocked.
    monkeypatch.setattr(socket, "create_connection", forbidden_external)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden_external)
    monkeypatch.setattr(socket.socket, "connect", forbidden_external)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden_external)
    monkeypatch.setattr(subprocess, "run", forbidden_external)

    created = "2026-10-16T12:00:00+00:00"
    with j1._db() as connection:
        connection.executemany(
            """INSERT INTO jobs(id,objective,max_pages,status,created_at,updated_at,profile)
               VALUES(?,?,?,'completed',?,?,'general')""",
            [
                (JOB_ID, "Synthetic local corpus", 3, created, created),
                (OTHER_JOB_ID, "Separate synthetic corpus", 3, created, created),
            ],
        )
        for document_id, job_id, label in (
            ("1" * 32, JOB_ID, "first"),
            ("2" * 32, JOB_ID, "second"),
            (OTHER_DOCUMENT_ID, OTHER_JOB_ID, "foreign"),
        ):
            url = f"https://example.org/synthetic/{label}"
            title = f"Synthetic {label} source"
            body = f"needle {label} public evidence. " + "Synthetic technical context. " * 30
            connection.execute(
                """INSERT INTO documents(
                       id,job_id,url,canonical_url,title,source,kind,status,body,created_at,retrieved_at
                   ) VALUES(?,?,?,?,?,?,'html','fetched',?,?,?)""",
                (document_id, job_id, url, url, title, "synthetic", body, created, created),
            )
            connection.execute(
                "INSERT INTO documents_fts(job_id,document_id,title,body) VALUES(?,?,?,?)",
                (job_id, document_id, title, body),
            )
        connection.commit()
    assert j1._db_path() == ordinary_root / "research.sqlite3"
    assert not private_root.exists()
    return JOB_ID


@pytest.mark.parametrize("query", ["", "needle"], ids=["listing", "fts"])
@pytest.mark.parametrize(
    "cursor,expected_length,expected_next",
    [
        pytest.param(0, 1, 1, id="first-page"),
        pytest.param(1, 1, None, id="last-page"),
        pytest.param(2, 0, None, id="at-total"),
        pytest.param(2**63 - 1, 0, None, id="sqlite-int64-max"),
        pytest.param(2**63, 0, None, id="past-sqlite-int64"),
        pytest.param(10**100, 0, None, id="much-larger-positive-cursor"),
    ],
)
def test_existing_ordinary_job_preserves_page_contract(
    ordinary_corpus: str,
    query: str,
    cursor: int,
    expected_length: int,
    expected_next: int | None,
) -> None:
    result = j1.research_results(ordinary_corpus, query=query, cursor=cursor, limit=1)

    assert result["ok"] is True
    assert result["schema"] == j1.SCHEMA
    assert result["job_id"] == ordinary_corpus
    assert result["query"] == query
    assert type(result["cursor"]) is int
    assert result["cursor"] == cursor
    assert result["total"] == 2
    assert result["next_cursor"] == expected_next
    assert len(result["results"]) == expected_length
    assert "error" not in result
    for item in result["results"]:
        assert item["document_id"] in OWN_DOCUMENT_IDS
        assert item["document_id"] != OTHER_DOCUMENT_ID
        assert item["status"] == "fetched"
        assert item["duplicate_of"] is None
        assert item["duplicate"] is None
        assert item["url"].startswith("https://example.org/synthetic/")
        if query:
            assert "[needle]" in item["excerpt"]
    if not expected_length:
        assert result["results"] == []


def test_missing_job_remains_missing_even_with_a_huge_valid_cursor(ordinary_corpus: str) -> None:
    # Existing healthy DB and a syntactically valid but absent id; no routing fault.
    assert ordinary_corpus != MISSING_JOB_ID
    result = j1.research_results(MISSING_JOB_ID, query="needle", cursor=10**100, limit=1)
    assert result == {
        "ok": False,
        "schema": j1.SCHEMA,
        "job_id": MISSING_JOB_ID,
        "error": {"code": "job_not_found"},
    }


@pytest.mark.parametrize(
    "cursor,limit",
    [
        pytest.param(-1, 1, id="negative-cursor"),
        pytest.param(True, 1, id="bool-cursor"),
        pytest.param(0, True, id="bool-limit"),
    ],
)
def test_existing_pagination_validation_is_preserved(ordinary_corpus: str, cursor: Any, limit: Any) -> None:
    result = j1.research_results(ordinary_corpus, cursor=cursor, limit=limit)
    assert result == {
        "ok": False,
        "schema": j1.SCHEMA,
        "job_id": ordinary_corpus,
        "error": {"code": "pagination_invalid"},
    }


@pytest.mark.parametrize("query", ["", "needle"], ids=["listing", "fts"])
def test_native_sdk_preserves_huge_cursor_empty_page(
    ordinary_corpus: str, query: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Local imports keep the original public tests and their collection unchanged.
    import asyncio
    import json

    from mcp.server.fastmcp import FastMCP
    from mcp.types import CallToolRequest, CallToolRequestParams, TextContent

    from autostop_manager import config, mcp_tools
    from autostop_manager.storage import StoreState

    manager_db = tmp_path / "unused-manager.sqlite3"
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", str(tmp_path / "unused.env"))
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(manager_db))
    monkeypatch.setattr(config, "_ENV_LOADED", False)
    server = FastMCP("isolated-results-cursor")
    mcp_tools.register_manager_tools(server, StoreState(manager_db), include_tools={"j1_research_results"})
    cursor = 10**100

    async def invoke() -> dict[str, Any]:
        handler = server._mcp_server.request_handlers[CallToolRequest]
        response = await handler(
            CallToolRequest(
                params=CallToolRequestParams(
                    name="j1_research_results",
                    arguments={"job_id": ordinary_corpus, "query": query, "cursor": cursor, "limit": 1},
                )
            )
        )
        result = response.root
        # SDK execution failures are error results, not domain dictionaries.
        assert result.isError is False, result
        assert isinstance(result.structuredContent, dict), result
        assert len(result.content) == 1
        assert isinstance(result.content[0], TextContent)
        assert json.loads(result.content[0].text) == result.structuredContent
        return result.structuredContent

    payload = asyncio.run(invoke())
    assert type(payload["cursor"]) is int
    # The real registration adds timing metadata; the domain envelope stays exact.
    assert {key: value for key, value in payload.items() if key != "tool_execution"} == {
        "ok": True,
        "schema": j1.SCHEMA,
        "job_id": ordinary_corpus,
        "query": query,
        "total": 2,
        "cursor": cursor,
        "next_cursor": None,
        "results": [],
    }
    assert not manager_db.exists()
    assert not (tmp_path / "private-vin").exists()
