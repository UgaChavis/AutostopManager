"""Offline regressions for queue publication, worker failure and OCR evidence."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from typing import Any
from uuid import uuid4

import pytest

from autostop_manager import j1_research as general
from autostop_manager import j1_vin_research as api
from autostop_manager import j1_vin_store as store
from autostop_manager import j1_vin_worker as worker

VIN = "1HGCM82673A000000"
QUOTE = "Engine Alpha; shared public details."


def _request_key(prefix: str) -> str:
    # Preserve UUID entropy without incidental phone or 17-character VIN runs.
    encoded = uuid4().hex.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))
    nonce = "i".join(encoded[index : index + 8] for index in range(0, 32, 8))
    return f"{prefix}-{nonce}"


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    if not Path("/dev/shm").is_dir():
        pytest.skip("private tmpfs test storage is unavailable")
    with tempfile.TemporaryDirectory(prefix="autostop-vin-review-", dir="/dev/shm") as directory:
        root = Path(directory)
        if not store._is_tmpfs(root):
            pytest.skip("VIN state requires tmpfs")
        monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "general"))
        monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(root))
        monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "1")
        monkeypatch.setattr(general, "_STOP", False)

        def forbidden(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("regression tests must not access live services")

        monkeypatch.setattr(worker, "request_vin", forbidden)
        monkeypatch.setattr(worker, "search_vin", forbidden)
        monkeypatch.setattr(worker.fetch, "fetch_browser_document", forbidden)
        yield root


def _start(*, completed: bool = True) -> str:
    result = api.j1_research_vin(VIN, _request_key("start"))
    assert result["ok"], result
    job = result["job_id"]
    if completed:
        with store.connect(job, transaction=True) as conn:
            current = store.metadata(conn)
            current["fields"] = ["engine"]
            current["collection_status"] = "completed"
            conn.execute("UPDATE queries SET status='done'")
            store.write_metadata(conn, current)
        store.set_stub(job, "completed")
    return job


def _pdf(job: str, text: str, *, second_page: str = "") -> str:
    identifier = uuid4().hex
    pages = [{"page": 1, "text": f"VIN {VIN}. {QUOTE} {text}"}]
    if second_page:
        pages.append({"page": 2, "text": second_page})
    raw = store.job_directory(job) / f"{identifier}.pdf"
    raw.write_bytes(b"%PDF-1.4 synthetic offline input")
    raw.chmod(0o600)
    with store.connect(job, transaction=True) as conn:
        current = store.metadata(conn)
        url = f"https://example.org/synthetic/{identifier}.pdf"
        conn.execute(
            "INSERT INTO documents(id,url,title,source_class,source_tier,source_basis) VALUES(?,?,?,?,?,?)",
            (identifier, url, "Synthetic PDF", "unknown", "unclassified", "synthetic-test"),
        )
        worker._save_document(
            conn,
            current,
            {"id": identifier, "url": url, "title": "Synthetic PDF"},
            {
                "kind": "pdf",
                "extraction_method": "pdf_text",
                "pages": pages,
                "page_count": len(pages),
                "raw_file": raw.name,
            },
        )
        store.write_metadata(conn, current)
    return identifier


def _record(job: str, identifier: str, *, basis: list[str] | None = None) -> dict[str, Any]:
    document = api.research_document(job, identifier, page=1)
    fact = {
        "field": "engine",
        "value": "Alpha",
        "document_id": identifier,
        "document_revision": document["document_revision"],
        "page": 1,
        "quote": QUOTE,
        "relationship": "vin_specific",
        "derivation": "direct",
        "basis_claim_ids": basis or [],
        "match_evidence_id": document["match_evidence_id"],
        "support": "corroborated" if basis else "single_source",
    }
    return api.j1_research_record_facts(job, api.research_status(job)["revision"], [fact], _request_key("facts"), True)


@pytest.mark.parametrize("addition", ["query", "ocr"])
def test_enqueue_during_worker_final_publication_remains_scheduled(sandbox, monkeypatch, addition: str) -> None:
    job = _start()
    document_id = _pdf(job, "Original page") if addition == "ocr" else ""
    completed = threading.Event()
    original = store.set_stub
    response: list[dict[str, Any]] = []
    threads: list[threading.Thread] = []

    def enqueue() -> None:
        try:
            result = (
                api.research_add_queries(job, ["synthetic extra engine information"])
                if addition == "query"
                else api.research_document(job, document_id, page=1, ocr=True)
            )
            response.append(result)
        finally:
            completed.set()

    def publish(identifier: str, status: str, reason: str = "") -> None:
        if identifier == job and status == "completed" and not threads:
            thread = threading.Thread(target=enqueue)
            threads.append(thread)
            thread.start()
            # Without a private transaction the enqueue finishes before the
            # stale status write. With the fix it waits until publication commits.
            completed.wait(0.5)
        original(identifier, status, reason)

    monkeypatch.setattr(store, "set_stub", publish)
    worker.run_job(job)
    assert threads
    threads[0].join(6)
    assert not threads[0].is_alive()
    assert response and response[0]["ok"], response
    assert api.research_status(job)["collection_status"] == "queued"
    with general._db() as conn:
        assert general._claim_job(conn) == job
    monkeypatch.setattr(worker, "search_vin", lambda *_args: {"ok": True, "results": [], "errors": [], "providers": []})
    monkeypatch.setattr(
        worker, "ocr_pages", lambda *_args, **_kwargs: {"ok": True, "pages": [{"page": 1, "text": QUOTE}]}
    )
    worker.run_job(job)
    assert api.research_status(job)["collection_status"] == "completed"
    with store.connect(job) as conn:
        table = "queries" if addition == "query" else "ocr_requests"
        assert not conn.execute(f"SELECT 1 FROM {table} WHERE status IN ('pending','running')").fetchone()


@pytest.mark.parametrize("code", ["vin_storage_unavailable", "vin_storage_limit_reached"])
def test_fatal_storage_error_reconciles_before_read_finalize_and_cancel(sandbox, monkeypatch, code: str) -> None:
    job = _start(completed=False)
    capacity = store.check_capacity
    full = False

    def check(*args: Any, **kwargs: Any) -> None:
        if full:
            raise store.VinJobError(code)
        capacity(*args, **kwargs)

    def search(*_args: Any) -> dict[str, Any]:
        nonlocal full
        full = True
        return {"ok": True, "results": [], "providers": [], "errors": []}

    monkeypatch.setattr(store, "check_capacity", check)
    monkeypatch.setattr(worker, "search_vin", search)
    worker.run_job(job)
    with general._db(readonly=True) as conn:
        assert tuple(conn.execute("SELECT status,error FROM jobs WHERE id=?", (job,)).fetchone()) == ("failed", code)
    # The disk remains full; recovery must not bypass the storage budget or
    # present the stopped worker as an active network operation.
    unavailable = api.research_status(job)
    assert not unavailable["ok"] and unavailable["error"]["code"] == code
    full = False
    recovered_at = time.time() + 1800
    monkeypatch.setattr(store.time, "time", lambda: recovered_at)
    status = api.research_status(job)
    assert status["collection_status"] == "failed"
    assert not status["network_inflight"] and status["stop_reason"] == code
    assert status["budget"]["collection_seconds"]["used"] < 10
    with store.connect(job) as conn:
        assert not conn.execute("SELECT 1 FROM queries WHERE status='running'").fetchone()
    result = api.j1_research_record_facts(job, status["revision"], [], "finalize-failed-storage", True)
    assert result["ok"], result
    cancelled = api.research_cancel(job)
    assert cancelled["collection_status"] == "cancelled" and not cancelled["network_inflight"]


def test_failure_with_available_storage_is_terminal_without_read_repair(sandbox, monkeypatch) -> None:
    job = _start(completed=False)

    def fail(_job: str) -> None:
        raise store.VinJobError("vin_storage_unavailable")

    monkeypatch.setattr(worker, "_next_operation", fail)
    worker.run_job(job)
    with store.connect(job) as conn:
        current = store.metadata(conn)
        assert current["collection_status"] == "failed" and not current["inflight"]
        assert current["stop_reason"] == "vin_storage_unavailable"


def test_start_receipt_replay_recovers_a_failed_worker(sandbox, monkeypatch) -> None:
    key = _request_key("replay")
    initial = api.j1_research_vin(VIN, key)
    assert initial["ok"]
    job = initial["job_id"]
    capacity = store.check_capacity
    full = False

    def check(*args: Any, **kwargs: Any) -> None:
        if full:
            raise store.VinJobError("vin_storage_unavailable")
        capacity(*args, **kwargs)

    def search(*_args: Any) -> dict[str, Any]:
        nonlocal full
        full = True
        return {"ok": True, "results": [], "providers": [], "errors": []}

    monkeypatch.setattr(store, "check_capacity", check)
    monkeypatch.setattr(worker, "search_vin", search)
    worker.run_job(job)
    full = False
    replay = api.j1_research_vin(VIN, key)
    assert replay["ok"] and replay["job_id"] == job
    assert replay["collection_status"] == "failed" and not replay["network_inflight"]
    assert replay["stop_reason"] == "vin_storage_unavailable"
    with general._db(readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_profile_lookup_error_cannot_complete_vin_job_as_generic_research(sandbox, monkeypatch) -> None:
    job = _start(completed=False)
    original = general._db
    failed = False

    class ProfileFailure:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn

        def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
            nonlocal failed
            if sql.startswith("SELECT profile FROM jobs") and not failed:
                failed = True
                raise sqlite3.OperationalError("synthetic transient profile lookup failure")
            return self.conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.conn, name)

    @contextmanager
    def unreliable(*args: Any, **kwargs: Any) -> Iterator[Any]:
        with original(*args, **kwargs) as conn:
            yield ProfileFailure(conn) if kwargs.get("readonly") else conn

    monkeypatch.setattr(general, "_db", unreliable)
    monkeypatch.setattr(general.signal, "signal", lambda *_args: None)
    general.run_worker(once=True)
    assert failed
    with general._db(readonly=True) as conn:
        assert conn.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()[0] == "failed"
    assert api.research_status(job)["collection_status"] == "failed"


@pytest.mark.parametrize("explicit_limitations", [True, False])
def test_clipped_ocr_preserves_existing_and_new_document_limitations(
    sandbox, monkeypatch, explicit_limitations
) -> None:
    job = _start()
    identifier = _pdf(job, "Original extraction")
    with store.connect(job, transaction=True) as conn:
        conn.execute("UPDATE documents SET limitations='[\"original_text_limit\"]' WHERE id=?", (identifier,))
    assert api.research_document(job, identifier, page=1, ocr=True)["ok"]
    monkeypatch.setattr(
        worker,
        "ocr_pages",
        lambda *_args, **_kwargs: {
            "ok": True,
            "truncated": True,
            "limitations": ["page_text_limit"] if explicit_limitations else [],
            "pages": [{"page": 1, "text": f"VIN {VIN}. {QUOTE} Clipped OCR.", "truncated": True}],
        },
    )
    worker.run_job(job)
    fact = _record(job, identifier)
    assert fact["ok"], fact
    report = api.research_report(job)
    expected = "page_text_limit" if explicit_limitations else "ocr_text_limit"
    assert set(report["sources"][0]["limitations"]) == {"original_text_limit", expected}
    assert report["stop_reason"] == "ocr_text_limit"
    document = api.research_document(job, identifier, page=1)
    assert set(document["limitations"]) == {"original_text_limit", expected}


@pytest.mark.parametrize("retained_pages_differ", [False, True])
def test_ocr_recomputes_duplicate_clusters_from_all_current_pages(sandbox, monkeypatch, retained_pages_differ) -> None:
    job = _start()
    first = _pdf(job, "First extraction", second_page="Retained first" if retained_pages_differ else "")
    second = _pdf(job, "Second extraction", second_page="Retained second" if retained_pages_differ else "")
    for identifier in (first, second):
        assert api.research_document(job, identifier, page=1, ocr=True)["ok"]
    monkeypatch.setattr(
        worker,
        "ocr_pages",
        lambda *_args, **_kwargs: {"ok": True, "pages": [{"page": 1, "text": f"VIN {VIN}. {QUOTE} Same OCR."}]},
    )
    worker.run_job(job)
    with store.connect(job) as conn:
        rows = list(conn.execute("SELECT id,digest,duplicate_of FROM documents ORDER BY rowid"))
        pages = [
            [
                dict(row)
                for row in conn.execute("SELECT page,text FROM pages WHERE document_id=? ORDER BY page", (identifier,))
            ]
            for identifier in (first, second)
        ]
        assert [row["digest"] for row in rows] == [api.content_digest(items) for items in pages]
        assert bool(rows[1]["duplicate_of"]) is not retained_pages_differ
    recorded = _record(job, first)
    assert recorded["ok"], recorded
    corroborated = _record(job, second, basis=recorded["claim_ids"])
    if retained_pages_differ:
        assert corroborated["ok"], corroborated
    else:
        assert corroborated["error"]["code"] == "fact_corroboration_basis_required"


def test_ocr_of_cluster_primary_rebinds_copies_and_releases_independent_source(sandbox, monkeypatch) -> None:
    job = _start()
    first, second, third = [_pdf(job, "Identical original") for _ in range(3)]
    assert api.research_document(job, first, page=1, ocr=True)["ok"]
    monkeypatch.setattr(
        worker,
        "ocr_pages",
        lambda *_args, **_kwargs: {"ok": True, "pages": [{"page": 1, "text": f"VIN {VIN}. {QUOTE} Different OCR."}]},
    )
    worker.run_job(job)
    with store.connect(job) as conn:
        copies = {row["id"]: row["duplicate_of"] for row in conn.execute("SELECT id,duplicate_of FROM documents")}
        assert copies == {first: "", second: "", third: second}
    recorded = _record(job, second)
    assert recorded["ok"], recorded
    assert _record(job, third, basis=recorded["claim_ids"])["error"]["code"] == "fact_corroboration_basis_required"
    assert _record(job, first, basis=recorded["claim_ids"])["ok"]
