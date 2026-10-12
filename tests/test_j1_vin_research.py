"""Offline collector integration; constructed VINs and isolated stores only."""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
import signal
import tempfile
import time
from typing import Any
from uuid import uuid4

import pytest

from autostop_manager import j1_research as general
from autostop_manager import j1_vin_research as api
from autostop_manager import j1_vin_store as store
from autostop_manager import j1_vin_worker as worker

VIN = "Z94K241BBKR000000"
FOREIGN = "1HGCM82673A000000"


def _request_key() -> str:
    encoded = uuid4().hex.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))
    nonce = "i".join(encoded[index : index + 8] for index in range(0, 32, 8))
    return "start-" + nonce


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="autostop-j1-vin-collector-", dir="/dev/shm") as directory:
        root = Path(directory)
        if not store._is_tmpfs(root):
            pytest.skip("temporary VIN storage requires tmpfs")
        monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "general"))
        monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(root))
        monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "1")
        monkeypatch.setattr(general, "_STOP", False)

        def forbidden(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("offline VIN tests must not contact a service")

        monkeypatch.setattr(worker, "search_vin", forbidden)
        monkeypatch.setattr(worker, "request_vin", forbidden)
        monkeypatch.setattr(worker.fetch, "fetch_browser_document", forbidden)
        monkeypatch.setattr(general, "search_public", forbidden)
        yield root


def start() -> str:
    result = api.j1_research_vin(VIN, _request_key())
    assert result["ok"], result
    return result["job_id"]


def discovered(monkeypatch: pytest.MonkeyPatch, urls: list[str]) -> None:
    monkeypatch.setattr(
        worker,
        "search_vin",
        lambda *_args: {
            "ok": True,
            "result_class": "results" if urls else "empty",
            "results": [{"url": url, "title": "Synthetic reference"} for url in urls],
            "providers": [{"provider": "bing", "outcome": "results" if urls else "empty"}],
            "errors": [],
        },
    )


def fetched(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    monkeypatch.setattr(
        worker,
        "request_vin",
        lambda url, *_args, **_kwargs: (
            200,
            {"content-type": "text/html"},
            body,
            url,
        ),
    )


def claim(document: dict[str, Any], field: str, value: str, relationship: str) -> dict[str, Any]:
    return {
        "field": field,
        "value": value,
        "document_id": document["document_id"],
        "document_revision": document["document_revision"],
        "page": 1,
        "quote": document["text"],
        "relationship": relationship,
        "match_evidence_id": document["match_evidence_id"],
    }


def test_offline_job_collects_then_agent_records_and_finalizes(isolated, monkeypatch) -> None:
    exact = f"https://example.org/record/{VIN}"
    family = "https://example.org/family"
    discovered(monkeypatch, [exact, family])
    monkeypatch.setattr(
        worker,
        "request_vin",
        lambda url, *_args, **_kwargs: (
            200,
            {"content-type": "text/html"},
            (
                f"<p>VIN {VIN}; make TestMake; model TestModel.</p>"
                if url == exact
                else "<p>Family engine Alpha; transmission Manual.</p>"
            ).encode(),
            url,
        ),
    )
    job = start()
    general._work_job(job)
    status = general.research_status(job)
    assert status["collection_status"] == "completed"
    assert status["analysis_status"] == "draft"
    assert status["fetched"] == 2
    results = general.research_results(job)
    documents = [general.research_document(job, row["document_id"]) for row in results["results"]]
    linked = next(doc for doc in documents if doc["match_evidence_id"])
    family_doc = next(doc for doc in documents if not doc["match_evidence_id"])
    facts = [claim(linked, "make", "TestMake", "vin_specific"), claim(family_doc, "engine", "Alpha", "family")]
    result = api.j1_research_record_facts(job, status["revision"], facts, "record-synthetic-facts", True)
    assert result["ok"] and result["analysis_status"] == "ready_partial"
    report = general.research_report(job)
    assert "make" not in report["unknown_fields"]
    assert "engine" in report["unknown_fields"]
    assert {item["relationship"] for item in report["claims"]} == {"vin_specific", "family"}
    assert all(item["citation_verified"] for item in report["claims"])
    assert VIN not in json.dumps([status, results, documents, report])
    with general._db(readonly=True) as conn:
        assert conn.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()[0] == "completed"
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_empty_search_is_partial_not_complete_decode(isolated, monkeypatch) -> None:
    discovered(monkeypatch, [])
    job = start()
    worker.run_job(job)
    status = api.research_status(job)
    assert status["collection_status"] == "completed" and status["fetched"] == 0
    result = api.j1_research_record_facts(job, status["revision"], [], "finalize-empty-search", True)
    assert result["analysis_status"] == "ready_partial"
    assert len(api.research_report(job)["unknown_fields"]) == len(store.DEFAULT_FIELDS)


def test_engine_authorization_failure_is_not_retried(isolated, monkeypatch) -> None:
    scopes = []

    def search(_query: str, scope: Any) -> dict[str, Any]:
        scopes.append(scope)
        return {
            "ok": False,
            "results": [],
            "providers": [],
            "errors": [{"provider": "bing", "http_status": 403, "error": "access_denied"}],
        }

    monkeypatch.setattr(worker, "search_vin", search)
    job = start()
    worker.run_job(job)
    assert "bing" in scopes[0].allowed_engines
    assert all("bing" not in scope.allowed_engines for scope in scopes[1:])
    assert len(scopes) == 3


@pytest.mark.parametrize("status", [401, 403, 429])
def test_access_denial_blocks_host_and_does_not_start_browser(isolated, monkeypatch, status) -> None:
    discovered(monkeypatch, ["https://example.org/a", "https://example.org/b"])
    requests: list[str] = []

    def request(url: str, *_args: Any, **_kwargs: Any) -> tuple:
        requests.append(url)
        return status, {"content-type": "text/html"}, b"", url

    monkeypatch.setattr(worker, "request_vin", request)
    job = start()
    worker.run_job(job)
    assert len(requests) == 1
    items = api.research_results(job)["results"]
    assert all(item["status"] == "failed" for item in items)
    assert {item["error"] for item in items} >= {"source_access_blocked"}
    assert api.research_status(job)["budget"]["browser_pages"]["used"] == 0


def test_browser_budget_is_persisted_and_dom_cannot_prove_literal_vin(isolated, monkeypatch) -> None:
    discovered(monkeypatch, [f"https://example.org/empty/{n}" for n in range(9)])
    fetched(monkeypatch, b"<html><body></body></html>")
    renders: list[str] = []

    def render(url: str, _seconds: float) -> dict[str, Any]:
        renders.append(url)
        return {"ok": True, "text": f"VIN {VIN}; synthetic dynamic page."}

    monkeypatch.setattr(worker, "_render_browser", render)
    job = start()
    worker.run_job(job)
    assert len(renders) == store.MAX_BROWSER_PAGES
    status = api.research_status(job)
    assert status["budget"]["browser_pages"]["used"] == store.MAX_BROWSER_PAGES
    for item in api.research_results(job)["results"]:
        doc = api.research_document(job, item["document_id"])
        assert not doc["match_evidence_id"] and VIN not in doc["text"]


def test_browser_alarm_bounds_entire_existing_renderer(isolated, monkeypatch) -> None:
    previous = signal.getsignal(signal.SIGALRM)
    monkeypatch.setattr(worker.fetch, "fetch_browser_document", lambda _url: time.sleep(5))
    started = time.monotonic()
    assert worker._render_browser("https://example.org/", 0.02)["error"] == "browser_timeout"
    assert time.monotonic() - started < 0.5
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0
    assert signal.getsignal(signal.SIGALRM) == previous


def test_cancel_during_fetch_discards_result_and_blocks_browser(isolated, monkeypatch) -> None:
    discovered(monkeypatch, ["https://example.org/empty", "https://example.org/next"])
    job = start()
    requests = []

    def request(url: str, *_args: Any, **_kwargs: Any) -> tuple:
        requests.append(url)
        cancel = api.research_cancel(job)
        assert cancel["network_inflight"] and cancel["cancel_requested"]
        with general._db(readonly=True) as conn:
            assert conn.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()[0] in {"running", "queued"}
        return 200, {"content-type": "text/html"}, b"<html></html>", url

    monkeypatch.setattr(worker, "request_vin", request)
    worker.run_job(job)
    status = api.research_status(job)
    assert status["collection_status"] == "cancelled" and not status["network_inflight"]
    assert status["fetched"] == 0 and len(requests) == 1
    assert api.j1_research_record_facts(job, status["revision"], [], "finalize-cancelled-job", True)["ok"]


@pytest.mark.parametrize(
    "code", ["fetch_timeout", "robots_disallowed", "unsafe_redirect", "large_document_source_untrusted"]
)
def test_network_failure_preserves_safe_specific_reason(isolated, monkeypatch, code) -> None:
    discovered(monkeypatch, ["https://example.org/document"])

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError(code)

    monkeypatch.setattr(worker, "request_vin", fail)
    job = start()
    worker.run_job(job)
    assert api.research_results(job)["results"][0]["error"] == code
    assert api.research_status(job)["collection_status"] == "completed"


def test_queries_share_budget_and_remove_finalization(isolated, monkeypatch) -> None:
    discovered(monkeypatch, [])
    job = start()
    worker.run_job(job)
    status = api.research_status(job)
    assert api.j1_research_record_facts(job, status["revision"], [], "finalize-first-round", True)["ok"]
    first_used = status["budget"]["collection_seconds"]["used"]
    added = api.research_add_queries(job, ["Synthetic engine", "synthetic ENGINE"])
    assert added["ok"] and added["added"] == 1
    extended = api.research_status(job)
    assert extended["collection_status"] == "queued" and extended["analysis_status"] == "draft"
    assert extended["budget"]["collection_seconds"]["used"] >= first_used
    rejected = api.research_add_queries(job, [f"synthetic question {n}" for n in range(12)])
    assert rejected["error"]["code"] == "query_limit_reached"
    assert api.research_add_queries(job, [FOREIGN])["error"]["code"] == "queries_invalid_or_sensitive"
    worker.run_job(job)
    with store.connect(job, transaction=True) as conn:
        current = store.metadata(conn)
        current["used_seconds"] = 300
        store.write_metadata(conn, current)
    assert api.research_add_queries(job, ["extra source"])["error"]["code"] == "vin_time_limit_reached"


def test_search_scope_uses_remaining_active_time_not_whole_retention(isolated, monkeypatch) -> None:
    job = start()
    with store.connect(job, transaction=True) as conn:
        current = store.metadata(conn)
        current["used_seconds"] = 299.5
        store.write_metadata(conn, current)
    observed = []

    def search(_query: str, scope: Any) -> dict[str, Any]:
        observed.append(scope.expires_at - time.time())
        return {"ok": True, "results": [], "providers": [], "errors": []}

    monkeypatch.setattr(worker, "search_vin", search)
    worker.run_job(job)
    assert observed and all(0 < remaining <= 0.5 for remaining in observed)


def test_no_collection_starts_when_budget_already_exhausted(isolated) -> None:
    job = start()
    with store.connect(job, transaction=True) as conn:
        current = store.metadata(conn)
        current["used_seconds"] = 300
        store.write_metadata(conn, current)
    worker.run_job(job)
    assert api.research_status(job)["stop_reason"] == "collection_time_limit"


def test_serial_worker_performs_private_ttl_cleanup_when_idle(isolated, monkeypatch) -> None:
    job = start()
    api.research_cancel(job)
    with store.connect(job, transaction=True) as conn:
        current = store.metadata(conn)
        current["expires_at"] = time.time() - 1
        store.write_metadata(conn, current)
    monkeypatch.setattr(general.signal, "signal", lambda *_args: None)
    general._run_locked_worker(once=True)
    assert not (isolated / job).exists()
    with general._db(readonly=True) as conn:
        assert conn.execute("SELECT error FROM jobs WHERE id=?", (job,)).fetchone()[0] == "vin_job_expired"


@pytest.mark.parametrize(
    "text",
    [
        "%31HGCM82673A000000 owner%40example.org",
        "&#49;HGCM82673A000000 owner&#64;example.org",
        "1HGCM82673A\u200b000000",
        "api_key%3Dabcdefgh12345678",
        "%25" * 100 + "31HGCM82673A000000",
    ],
)
def test_encoded_private_text_does_not_reach_document_or_fts(isolated, monkeypatch, text) -> None:
    discovered(monkeypatch, ["https://example.org/source"])
    fetched(monkeypatch, f"<p>{text}</p>".encode())
    job = start()
    worker.run_job(job)
    document_id = api.research_results(job)["results"][0]["document_id"]
    document = api.research_document(job, document_id)
    assert not document["match_evidence_id"]
    assert not worker.fetch.contains_sensitive(document["text"])
    assert FOREIGN not in document["text"] and "owner@example.org" not in document["text"]
    with store.connect(job) as conn:
        rows = conn.execute("SELECT text FROM pages_fts").fetchall()
        assert all(not worker.fetch.contains_sensitive(row[0]) for row in rows)


def test_metadata_requires_strings_and_deep_encoded_text_fails_closed() -> None:
    assert api._safe_text({"token": "abcdefghijklmnop"}, 1000) == ""
    layered = "%31HGCM82673A000000"
    for _ in range(20):
        layered = layered.replace("%", "%25")
    assert api._safe_text(layered, 1000) == "[redacted encoded source text]"


def test_distinct_links_to_same_final_url_keep_provenance_and_count_duplicate(isolated, monkeypatch) -> None:
    urls = ["https://example.org/a", "https://example.org/b"]
    discovered(monkeypatch, urls)
    monkeypatch.setattr(
        worker,
        "request_vin",
        lambda *_args, **_kwargs: (
            200,
            {"content-type": "text/html"},
            b"<p>Family engine Alpha.</p>",
            "https://example.org/final",
        ),
    )
    job = start()
    worker.run_job(job)
    results = api.research_results(job)["results"]
    assert len(results) == 2 and all(row["status"] == "fetched" for row in results)
    assert {row["url"] for row in results} == {"https://example.org/final"}
    assert {row["requested_url"] for row in results} == set(urls)
    assert sum(bool(row["duplicate_of"]) for row in results) == 1
    assert api.research_status(job)["duplicates"] == 1


def test_ocr_is_queued_then_reads_cached_pdf_with_remaining_budget(isolated, monkeypatch) -> None:
    discovered(monkeypatch, ["https://example.org/reference.pdf"])
    calls = []

    def download(url: str, *_args: Any, **kwargs: Any) -> tuple:
        calls.append((url, kwargs["max_bytes"]))
        return 200, {"content-type": "application/pdf"}, b"%PDF-synthetic-only", url

    monkeypatch.setattr(worker, "request_vin", download)
    monkeypatch.setattr(
        worker,
        "extract_content",
        lambda *_args, **_kwargs: {
            "ok": True,
            "kind": "pdf",
            "extraction_method": "pdf_text",
            "page_count": 456,
            "pages": [{"page": 456, "text": "Family engine Alpha."}],
            "limitations": [],
        },
    )
    job = start()
    worker.run_job(job)
    row = api.research_results(job, "Alpha")["results"][0]
    assert row["matching_pages"] == [456]
    document_id = row["document_id"]
    raw = isolated / job / (document_id + ".pdf")
    assert raw.read_bytes() == b"%PDF-synthetic-only" and raw.stat().st_mode & 0o777 == 0o600
    queued = general.research_document(job, document_id, page=456, ocr=True)
    assert queued["ocr_status"] == "queued" and len(calls) == 1
    observed = []

    def ocr(body: bytes, pages: list[int], workspace: Path, **kwargs: Any) -> dict[str, Any]:
        assert body == raw.read_bytes() and pages == [456] and workspace == raw.parent
        observed.append(kwargs["timeout_seconds"])
        return {
            "ok": True,
            "kind": "pdf",
            "extraction_method": "pdf_ocr",
            "pages": [{"page": 456, "text": f"VIN {VIN}; engine Alpha."}],
        }

    monkeypatch.setattr(worker, "ocr_pages", ocr)
    worker.run_job(job)
    document = general.research_document(job, document_id, page=456, ocr=True)
    assert document["document_revision"] == 2 and document["match_evidence_id"]
    assert document["page"] == 456 and VIN not in document["text"]
    assert len(calls) == 1 and len(observed) == 1 and 0 < observed[0] <= 300
    assert api.research_status(job)["budget"]["ocr_pages"]["used"] == 1


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.hyundai.com/manual.pdf", 24 * 1024 * 1024),
        ("https://example.org/manual.pdf", 8_000_000),
    ],
)
def test_download_passes_source_limit_and_remaining_time_to_parsers(isolated, monkeypatch, url, expected) -> None:
    job = start()
    with store.connect(job) as conn:
        current = store.metadata(conn)
    current["used_seconds"] = 299.5
    captured = {}

    def download(target: str, scoped: Any, **kwargs: Any) -> tuple:
        captured.update(kwargs)
        assert 0 < scoped.expires_at - time.time() <= 0.5
        time.sleep(0.02)
        return 200, {"content-type": "application/pdf"}, b"%PDF-synthetic", target

    def parse(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        captured["parser_timeout"] = kwargs["timeout_seconds"]
        return {"ok": False, "kind": "pdf", "pages": [], "error": "pdf_extract_failed"}

    monkeypatch.setattr(worker, "request_vin", download)
    monkeypatch.setattr(worker, "extract_content", parse)
    result = worker._download(job, {"url": url, "id": uuid4().hex}, current)
    assert result["error"] == "pdf_extract_failed"
    assert captured["max_bytes"] == expected and 0 < captured["timeout"] <= 0.5
    assert 0 < captured["parser_timeout"] < 0.5 - 0.015
