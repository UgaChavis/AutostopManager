"""Keep J1 browser usage separate from the final text extraction method.

Seventeen distinct scenarios plus one native SDK repeat (18 pytest cases).
Real fetch wrappers, parsing, worker, SQLite, public APIs, and one registered SDK
path. Fake boundaries: HTTP, renderer, robots/attestation, wait, and search. SQL
writes are confined to explicit legacy/recovery fixtures using pre-change columns.
No new column name or storage design is imposed. Browser use and final extraction
method are independent facts. Two renders for one document consume one page.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
import json
import signal
from types import SimpleNamespace
from uuid import uuid4

from mcp.server.fastmcp import FastMCP
import pytest

from autostop_manager import config, j1_browser, j1_fetch, j1_research as j1, mcp_telemetry, mcp_tools
from autostop_manager.storage import StoreState


URL = "https://example.org/public-page"
STATIC_TEXT = (
    "Synthetic static public technical evidence. "
    "This body is long enough for static extraction without another browser attempt. "
)
RENDER_TEXT = "Synthetic public rendered evidence. " * 5
_API = {
    "start": j1.start_research,
    "status": j1.research_status,
    "results": j1.research_results,
    "document": j1.research_document,
    "add_queries": j1.research_add_queries,
    "cancel": j1.research_cancel,
}


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "ordinary"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "private-vin"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "0")
    monkeypatch.delenv("AUTOSTOP_J1_SEARXNG_URL", raising=False)
    monkeypatch.setattr(j1, "_STOP", False)
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    yield
    for sig, handler in previous.items():
        signal.signal(sig, handler)


@pytest.fixture
def direct_api():
    def call(operation, **arguments):
        snapshot = deepcopy(arguments)
        result = _API[operation](**arguments)
        assert arguments == snapshot
        return result

    return call


@pytest.fixture
def native_api(tmp_path, monkeypatch):
    class InertAdapter:
        def __init__(self, *_args, **_kwargs):
            pass

    for name in (
        "StoreIntegration",
        "StoreApiClient",
        "StoreOwnerApiClient",
        "StoreQuoteConductor",
        "StoreQuoteOwnerApi",
    ):
        monkeypatch.setattr(mcp_tools, name, InertAdapter)
    monkeypatch.setattr(config, "_ENV_LOADED", False)
    monkeypatch.setattr(mcp_telemetry.LOGGER, "disabled", True)
    names = {f"j1_research_{operation}" for operation in ("start", "status", "results", "document")}
    server = FastMCP("isolated-browser-accounting")
    mcp_tools.register_manager_tools(server, StoreState(tmp_path / "native.sqlite3"), include_tools=names)
    assert set(server._tool_manager._tools) == names

    def call(operation, **arguments):
        snapshot = deepcopy(arguments)
        wire = deepcopy(arguments)
        if operation == "start":
            # Exercise the SDK's supported JSON string -> list input conversion.
            wire["queries"] = json.dumps(wire["queries"])
        tool = server._tool_manager.get_tool(f"j1_research_{operation}")
        content, response = asyncio.run(tool.run(wire, convert_result=True))
        assert json.loads(content[0].text) == response
        assert arguments == snapshot
        return response

    return call


def boundaries(monkeypatch, http_steps, render_steps=("timeout",), *, stop_after_pages=None):
    seen = SimpleNamespace(http=Counter(), renders=Counter(), waits=[], stopped=False)
    monkeypatch.setattr(j1_fetch, "_robots_policy", lambda _url: (True, 1.0))
    monkeypatch.setattr(j1_fetch, "_rate_limit", lambda *_args: None)
    monkeypatch.setattr(j1_fetch.time, "sleep", seen.waits.append)
    monkeypatch.setattr(j1_browser, "isolation_verified", lambda: True)

    def request(url, *, max_bytes, check_redirect_robots):
        assert max_bytes == j1_fetch.MAX_PDF_BYTES and check_redirect_robots is True
        index = seen.http[url]
        seen.http[url] += 1
        assert index < len(http_steps), "unscripted HTTP attempt"
        if index == 1 and stop_after_pages == len(seen.renders) and not seen.stopped:
            # A cooperative signal arrives while the final response is in flight.
            # The real worker persists this unit before observing its stop flag.
            j1._on_stop(signal.SIGTERM, None)
            seen.stopped = True
        step = http_steps[index]
        if step == "short":
            return 200, {"content-type": "text/html"}, b"<p>short</p>", url
        if step == "static":
            return 200, {"content-type": "text/html"}, f"<p>{STATIC_TEXT}</p>".encode(), url
        if step in {"503", "429"}:
            headers = {"content-type": "text/html"}
            if step == "429":
                headers["retry-after"] = "999"
            return int(step), headers, b"", url
        raise AssertionError("unknown HTTP fixture")

    def render(url, *, max_chars, timeout_seconds):
        assert max_chars == j1_fetch.MAX_TEXT_CHARS and timeout_seconds == 20
        index = seen.renders[url]
        seen.renders[url] += 1
        assert index < len(render_steps), "unscripted renderer attempt"
        if render_steps[index] == "timeout":
            return {"ok": False, "error": "browser_timeout", "retryable": True}
        assert render_steps[index] == "success"
        return {"ok": True, "url": url, "title": "Synthetic render", "text": RENDER_TEXT}

    monkeypatch.setattr(j1_fetch, "_request_public", request)
    monkeypatch.setattr(j1_browser, "render_page", render)
    return seen


def queue_job(monkeypatch, api, urls, *, max_pages=None):
    groups = [urls[index : index + 10] for index in range(0, len(urls), 10)]
    answers = {f"Synthetic public source group {index}": group for index, group in enumerate(groups)}

    def search(query, **_kwargs):
        assert query in answers, query
        return [{"url": url, "title": "Synthetic source", "source": "synthetic"} for url in answers[query]], "synthetic"

    monkeypatch.setattr(j1, "search_public", search)
    assert j1.fetch_document is j1_fetch.fetch_document
    created = api(
        "start",
        objective="Synthetic public technical evidence",
        queries=list(answers),
        max_pages=max_pages if max_pages is not None else len(urls),
    )
    assert created["ok"] is True, created
    return created["job_id"], answers


def budget(api, job_id, attempted, *, state="completed"):
    result = api("status", job_id=job_id)
    assert result["ok"] is True, result
    assert result["status"] == state, result
    assert result["browser_pages_attempted"] == attempted, result
    assert result["browser_pages_remaining"] == max(0, j1.MAX_BROWSER_PAGES - attempted), result
    return result


def items(api, job_id):
    result = api("results", job_id=job_id, limit=50)
    assert result["ok"] is True and result["next_cursor"] is None, result
    return result["results"]


@pytest.mark.parametrize("second", ["503", "429", "static"])
def test_fetch_retains_browser_fact_when_retry_finishes_without_browser(monkeypatch, second):
    seen = boundaries(monkeypatch, ("short", second))
    result = j1_fetch.fetch_document(URL)
    assert (seen.http[URL], seen.renders[URL], seen.waits) == (2, 1, [])
    assert "retry_after_seconds" not in result
    if second == "static":
        assert result["ok"] is True and result["extraction_method"] == "html_text"
    else:
        assert result["error"] == {"503": "http_server_error", "429": "rate_limited"}[second]
        assert result.get("extraction_method") != "browser_dom"
    assert result.get("browser_attempted") is True, result


def test_fetch_static_success_does_not_create_browser_usage(monkeypatch):
    seen = boundaries(monkeypatch, ("static",), ())
    result = j1_fetch.fetch_document(URL)
    assert result["ok"] is True and result["extraction_method"] == "html_text"
    assert result["browser_attempted"] is False
    assert (seen.http[URL], sum(seen.renders.values()), seen.waits) == (1, 0, [])


@pytest.mark.parametrize("second_render", ["timeout", "success"])
def test_fetch_keeps_two_render_attempts_bounded_to_one_page(monkeypatch, second_render):
    seen = boundaries(monkeypatch, ("short", "short"), ("timeout", second_render))
    result = j1_fetch.fetch_document(URL)
    assert (seen.http[URL], seen.renders[URL], seen.waits) == (2, 2, [])
    assert result["browser_attempted"] is True and result["extraction_method"] == "browser_dom"
    assert result["ok"] is (second_render == "success")


def test_fetch_browser_false_never_calls_renderer(monkeypatch):
    seen = boundaries(monkeypatch, ("short",), ())
    result = j1_fetch.fetch_document(URL, allow_browser=False)
    assert result["error"] == "browser_limit_reached" and result["browser_attempted"] is False
    assert (seen.http[URL], sum(seen.renders.values()), seen.waits) == (1, 0, [])


def terminal_case(monkeypatch, api, outcome):
    seen = boundaries(monkeypatch, ("short", "503" if outcome == "failed" else "static"))
    urls = [URL, "https://example.net/identical-public-page"] if outcome == "duplicate" else [URL]
    job_id, _answers = queue_job(monkeypatch, api, urls)
    if outcome == "capacity":
        monkeypatch.setattr(j1, "MAX_CORPUS_BYTES", 1000)
    j1.run_worker(once=True)
    rows = items(api, job_id)
    expected_states = {
        "fetched": ["fetched"],
        "failed": ["failed"],
        "duplicate": ["duplicate", "fetched"],
        "capacity": ["failed"],
    }[outcome]
    assert sorted(row["status"] for row in rows) == expected_states
    assert all(seen.http[url] == 2 and seen.renders[url] == 1 for url in urls)
    for row in rows:
        assert row["extraction_method"] == ("unknown" if outcome == "failed" else "html_text")
        document = api("document", job_id=job_id, document_id=row["document_id"])
        assert document["ok"] is (row["status"] == "fetched")
        if row["status"] == "fetched":
            assert document["extraction_method"] == "html_text"
            assert STATIC_TEXT.strip() in document["text"]
    if outcome == "capacity":
        assert rows[0]["error"] == "cache_capacity_reached"
    before_reads = (seen.http.copy(), seen.renders.copy())
    status = budget(api, job_id, len(urls))
    assert budget(api, job_id, len(urls))["coverage"] == status["coverage"]
    assert (seen.http, seen.renders) == before_reads


@pytest.mark.parametrize("outcome", ["fetched", "failed", "duplicate", "capacity"])
def test_browser_usage_survives_each_document_terminal_path(monkeypatch, direct_api, outcome):
    terminal_case(monkeypatch, direct_api, outcome)


def test_native_encoded_start_and_status_preserve_browser_usage_and_static_metadata(monkeypatch, native_api):
    # One meaningful interface repeat: SDK conversion of queries, serialized
    # status budget, and results/document extraction metadata stay consistent.
    terminal_case(monkeypatch, native_api, "fetched")


def test_consumer_counts_two_renders_once_and_static_page_zero(monkeypatch, direct_api):
    # These are two distinct public jobs, so a global/shared counter also fails.
    seen = boundaries(monkeypatch, ("short", "short"), ("timeout", "timeout"))
    job_id, _answers = queue_job(monkeypatch, direct_api, [URL])
    j1.run_worker(once=True)
    assert seen.renders[URL] == 2
    assert items(direct_api, job_id)[0]["extraction_method"] == "browser_dom"
    budget(direct_api, job_id, 1)

    seen = boundaries(monkeypatch, ("static",), ())
    next_id, _answers = queue_job(monkeypatch, direct_api, [URL])
    j1.run_worker(once=True)
    assert not seen.renders and seen.http[URL] == 1
    assert items(direct_api, next_id)[0]["extraction_method"] == "html_text"
    budget(direct_api, next_id, 0)
    budget(direct_api, job_id, 1)


@pytest.mark.parametrize("resume", ["add_queries", "worker_restart"])
def test_page_budget_survives_resume_without_more_renderer_pages(monkeypatch, direct_api, resume):
    total = j1.MAX_BROWSER_PAGES + 1
    urls = [f"https://example.org/public-page-{index}" for index in range(total)]
    seen = boundaries(
        monkeypatch,
        ("short", "503"),
        stop_after_pages=j1.MAX_BROWSER_PAGES if resume == "worker_restart" else None,
    )
    initial = urls if resume == "worker_restart" else urls[:-1]
    job_id, answers = queue_job(monkeypatch, direct_api, initial, max_pages=total)
    j1.run_worker(once=True)
    budget(direct_api, job_id, j1.MAX_BROWSER_PAGES, state="running" if resume == "worker_restart" else "completed")
    assert len(seen.renders) == j1.MAX_BROWSER_PAGES
    if resume == "add_queries":
        query = "Synthetic additional public evidence"
        answers[query] = urls[-1:]
        assert direct_api("add_queries", job_id=job_id, queries=[query])["added"] == 1
        budget(direct_api, job_id, j1.MAX_BROWSER_PAGES, state="queued")
    else:
        assert seen.stopped is True
        monkeypatch.setattr(j1, "_STOP", False)
    j1.run_worker(once=True)
    status = budget(direct_api, job_id, j1.MAX_BROWSER_PAGES)
    assert status["pages_total"] == total
    assert len(seen.renders) == j1.MAX_BROWSER_PAGES
    assert all(count == 1 for count in seen.renders.values())
    assert sum(row["error"] == "browser_limit_reached" for row in items(direct_api, job_id)) == 1


def seed_legacy(job_id, urls, *, recovering=False):
    # Old columns only: a newly added usage flag would have its default value.
    # This represents retained records, not a mocked SQL result or a new schema.
    with j1._db() as connection:
        connection.executemany(
            """INSERT INTO documents(id,job_id,url,status,extraction_method,error,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            [
                (
                    uuid4().hex,
                    job_id,
                    url,
                    "running" if recovering else "failed",
                    "browser_dom",
                    "" if recovering else "browser_timeout",
                    j1._utcnow(),
                )
                for url in urls
            ],
        )
        connection.execute("UPDATE queries SET status='done' WHERE job_id=?", (job_id,))
        connection.execute("UPDATE jobs SET status=? WHERE id=?", ("running" if recovering else "completed", job_id))
        connection.commit()


def test_legacy_browser_dom_rows_still_consume_budget_on_extension(monkeypatch, direct_api):
    urls = [f"https://example.org/legacy-page-{index}" for index in range(j1.MAX_BROWSER_PAGES)]
    job_id, answers = queue_job(monkeypatch, direct_api, urls, max_pages=len(urls) + 1)
    seed_legacy(job_id, urls)
    budget(direct_api, job_id, len(urls))
    seen = boundaries(monkeypatch, ("short",), ())
    query = "Synthetic legacy continuation"
    answers[query] = [URL]
    assert direct_api("add_queries", job_id=job_id, queries=[query])["added"] == 1
    j1.run_worker(once=True)
    budget(direct_api, job_id, len(urls))
    assert seen.http[URL] == 1 and not seen.renders
    assert next(row for row in items(direct_api, job_id) if row["url"] == URL)["error"] == "browser_limit_reached"


def test_legacy_recovery_static_overwrite_preserves_prior_usage(monkeypatch, direct_api):
    job_id, _answers = queue_job(monkeypatch, direct_api, [URL])
    seed_legacy(job_id, [URL], recovering=True)
    budget(direct_api, job_id, 1, state="running")
    seen = boundaries(monkeypatch, ("static", "static"), ())
    for recovery in range(2):
        if recovery:
            # A second retained running record now has html_text metadata, so
            # preservation must use its stored usage fact rather than relabeling.
            with j1._db() as connection:
                connection.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
                connection.execute("UPDATE documents SET status='running' WHERE job_id=?", (job_id,))
                connection.commit()
        j1.run_worker(once=True)
        row = items(direct_api, job_id)[0]
        assert row["status"] == "fetched" and row["extraction_method"] == "html_text"
        budget(direct_api, job_id, 1)
        assert not seen.renders and seen.http[URL] == recovery + 1


def test_completed_and_cancelled_status_keep_already_persisted_budget(monkeypatch, direct_api):
    seen = boundaries(monkeypatch, ("short", "static"))
    job_id, _answers = queue_job(monkeypatch, direct_api, [URL], max_pages=2)
    j1.run_worker(once=True)
    budget(direct_api, job_id, 1)
    assert direct_api("cancel", job_id=job_id)["status"] == "completed"
    budget(direct_api, job_id, 1)
    query = "Synthetic unstarted followup"
    assert direct_api("add_queries", job_id=job_id, queries=[query])["added"] == 1
    assert direct_api("cancel", job_id=job_id)["status"] == "cancelled"
    before = (seen.http.copy(), seen.renders.copy())
    j1.run_worker(once=True)
    budget(direct_api, job_id, 1, state="cancelled")
    assert direct_api("add_queries", job_id=job_id, queries=[query])["error"]["code"] == "job_not_extendable"
    assert (seen.http, seen.renders) == before
