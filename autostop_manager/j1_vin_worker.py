"""Bounded VIN collector, executed exclusively by the existing J1 worker."""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import threading
import time
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from . import j1_fetch as fetch
from . import j1_vin_research as api
from . import j1_vin_store as store
from .j1_sources import classify_source
from .j1_vin_documents import extract_content, ocr_pages
from .j1_vin_network import request_vin, safe_url, search_vin


def _diagnostic(value: Any, default: str) -> str:
    return str(value) if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,64}", value) else default


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _remaining(current: dict[str, Any], started: float) -> float:
    return max(
        0.0,
        min(
            store.MAX_SECONDS - current["used_seconds"] - (time.monotonic() - started),
            current["expires_at"] - time.time(),
        ),
    )


def _network_active(job_id: str) -> bool:
    if not store.enabled():
        return False
    try:
        with store.connect(job_id) as conn:
            return not store.metadata(conn)["cancel_requested"]
    except (OSError, sqlite3.Error, store.VinJobError):
        return False


class _BrowserDeadline(BaseException):
    """Escape broad library error handlers when the collector budget expires."""


def _render_browser(url: str, seconds: float) -> dict[str, Any]:
    # The serial J1 service owns its main thread. An alarm bounds the existing
    # guarded renderer's DNS, robots, preflight and render as one operation.
    if threading.current_thread() is not threading.main_thread() or signal.getitimer(signal.ITIMER_REAL)[0]:
        return {"ok": False, "error": "browser_deadline_unavailable"}
    previous = signal.getsignal(signal.SIGALRM)

    def expire(_signum: int, _frame: Any) -> None:
        raise _BrowserDeadline

    signal.signal(signal.SIGALRM, expire)
    try:
        signal.setitimer(signal.ITIMER_REAL, max(0.001, seconds))
        return fetch.fetch_browser_document(url)
    except _BrowserDeadline:
        return {"ok": False, "error": "browser_timeout"}
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _reset_interrupted(conn: sqlite3.Connection, current: dict[str, Any]) -> None:
    if current["inflight"]:
        elapsed = max(0.0, min(store.MAX_SECONDS, time.time() - current["operation_started_at"]))
        current["used_seconds"] += elapsed
    current["inflight"] = False
    current["operation_started_at"] = 0.0
    for table in ("queries", "documents", "ocr_requests"):
        conn.execute(
            f"UPDATE {table} SET status='failed',error='worker_interrupted' WHERE status='running' AND attempts>=2"
        )
        conn.execute(f"UPDATE {table} SET status='pending' WHERE status='running' AND attempts<2")
    if current["cancel_requested"]:
        current["collection_status"] = "cancelled"
    else:
        current["collection_status"] = "running"
    store.write_metadata(conn, current)


def _finish_state(current: dict[str, Any], *, status: str, reason: str = "") -> None:
    current["collection_status"] = status
    current["inflight"] = False
    current["operation_started_at"] = 0.0
    current["stop_reason"] = reason


def _next_operation(job_id: str) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    with store.connect(job_id, transaction=True) as conn:
        current = store.metadata(conn)
        if current["cancel_requested"]:
            _finish_state(current, status="cancelled", reason="cancelled_by_agent")
        elif not store.enabled():
            _finish_state(current, status="cancelled", reason="vin_research_disabled")
        elif current["used_seconds"] >= store.MAX_SECONDS - 0.01:
            _finish_state(current, status="completed", reason="collection_time_limit")
        else:
            for table in ("ocr_requests", "queries", "documents"):
                row = conn.execute(
                    f"SELECT rowid AS operation_rowid,* FROM {table} WHERE status='pending' ORDER BY rowid LIMIT 1"
                ).fetchone()
                if row is None:
                    continue
                conn.execute(
                    f"UPDATE {table} SET status='running',attempts=attempts+1 WHERE rowid=?", (row["operation_rowid"],)
                )
                current["inflight"] = True
                current["operation_started_at"] = time.time()
                store.write_metadata(conn, current)
                return table, dict(row), current
            _finish_state(current, status="completed", reason=current["stop_reason"] or "initial_collection_complete")
        store.write_metadata(conn, current)
        return None


def _finish_operation(conn: sqlite3.Connection, elapsed: float) -> tuple[dict[str, Any], bool]:
    current = store.metadata(conn)
    current["used_seconds"] += max(0.0, elapsed)
    current["inflight"] = False
    current["operation_started_at"] = 0.0
    allowed = not current["cancel_requested"] and store.enabled()
    if not allowed:
        _finish_state(
            current,
            status="cancelled",
            reason="cancelled_by_agent" if current["cancel_requested"] else "vin_research_disabled",
        )
    return current, allowed


def _collect_search(job_id: str, operation: dict[str, Any], current: dict[str, Any]) -> None:
    started = time.monotonic()
    result = search_vin(
        operation["query"],
        api.scope(
            current,
            deadline_at=time.time() + _remaining(current, started),
            active_check=lambda: _network_active(job_id),
        ),
    )
    with store.connect(job_id, transaction=True) as conn:
        charged = time.monotonic() - started
        updated, allowed = _finish_operation(conn, charged)
        providers = result.get("providers", [])
        code = "" if result.get("ok") else "search_unavailable"
        conn.execute(
            "UPDATE queries SET status=?,error=?,providers=? WHERE id=?",
            ("done" if result.get("ok") else "failed", code, json.dumps(providers), operation["id"]),
        )
        for entry in result.get("errors", []):
            if (
                entry.get("http_status") in {401, 403, 429}
                and entry.get("provider") in store.ENGINES
                and entry["provider"] not in updated["blocked_engines"]
            ):
                updated["blocked_engines"].append(entry["provider"])
        if allowed:
            _discover(conn, updated, result.get("results", []))
        updated["used_seconds"] += max(0.0, time.monotonic() - started - charged)
        store.write_metadata(conn, updated)


def _discover(conn: sqlite3.Connection, current: dict[str, Any], results: list[dict[str, Any]]) -> None:
    count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    for item in results:
        if count >= store.MAX_DOCUMENTS:
            current["stop_reason"] = "document_limit_reached"
            break
        url = safe_url(str(item.get("url", "")), api.scope(current))
        if not url:
            continue
        url = store.general._canonical_url(url)
        if conn.execute("SELECT 1 FROM documents WHERE url=?", (url,)).fetchone():
            continue
        classification = classify_source(url)
        conn.execute(
            "INSERT INTO documents(id,url,title,source_class,source_tier,source_basis) VALUES(?,?,?,?,?,?)",
            (
                uuid4().hex,
                url,
                api._safe_text(item.get("title", ""), 300),
                classification.source_class,
                classification.source_tier,
                classification.source_basis,
            ),
        )
        count += 1
        api._invalidation(current)


def _download(job_id: str, operation: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    url = operation["url"]
    if _host(url) in current["blocked_hosts"]:
        return {"ok": False, "error": "source_access_blocked"}
    classification = classify_source(url)
    max_bytes = (
        24 * 1024 * 1024
        if classification.source_tier in {"A", "B"} and classification.source_basis.startswith("registry:")
        else 8_000_000
    )
    remaining = _remaining(current, started)
    if remaining <= 0:
        return {"ok": False, "error": "collection_time_limit"}
    status, headers, body, final_url = request_vin(
        url,
        api.scope(current, deadline_at=time.time() + remaining, active_check=lambda: _network_active(job_id)),
        max_bytes=max_bytes,
        timeout=min(10.0, remaining),
    )
    if status != 200:
        return {
            "ok": False,
            "error": "http_access_denied"
            if status in {401, 403}
            else "http_rate_limited"
            if status == 429
            else "http_source_error",
            "http_status": status,
            "url": final_url,
        }
    workspace = store.job_directory(job_id)
    extracted = extract_content(
        body, headers.get("content-type", ""), final_url, workspace, timeout_seconds=_remaining(current, started)
    )
    extracted["url"] = final_url
    extracted["http_status"] = status
    if extracted.get("kind") == "pdf" and extracted.get("ok"):
        store.check_capacity(workspace.parent, len(body))
        raw = workspace / (operation["id"] + ".pdf")
        fd = os.open(raw, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(body)
        extracted["raw_file"] = raw.name
    return extracted


def _browser_fallback(url: str, current: dict[str, Any], result: dict[str, Any], *, seconds: float) -> dict[str, Any]:
    if result.get("http_status") != 200 or result.get("kind") != "html":
        return result
    if result.get("kind") == "pdf" or not fetch.public_url(url) or current["browser_pages"] >= store.MAX_BROWSER_PAGES:
        return result
    if result.get("ok") and any(p.get("text", "").strip() for p in result.get("pages", [])):
        return result
    # The original VIN is never passed to Chromium. Its generic guard blocks
    # VIN-bearing initial URLs; redacted DOM cannot establish a literal match.
    current["browser_pages"] += 1
    rendered = _render_browser(url, seconds)
    if not rendered.get("ok"):
        return result
    return {
        "ok": True,
        "kind": "html",
        "extraction_method": "browser_dom",
        "url": url,
        "title": rendered.get("title", ""),
        "pages": [{"page": 1, "text": rendered.get("text", "")}],
        "limitations": ["Browser DOM was redacted before VIN linkage; no exact match can be certified."],
    }


def _collect_document(job_id: str, operation: dict[str, Any], current: dict[str, Any]) -> None:
    started = time.monotonic()
    result = _download(job_id, operation, current)
    with store.connect(job_id) as conn:
        fresh = store.metadata(conn)
    remaining = _remaining(fresh, started)
    if store.enabled() and not fresh["cancel_requested"] and remaining > 0.01:
        result = _browser_fallback(result.get("url", operation["url"]), fresh, result, seconds=min(35.0, remaining))
    with store.connect(job_id, transaction=True) as conn:
        charged = time.monotonic() - started
        updated, allowed = _finish_operation(conn, charged)
        updated["browser_pages"] = max(updated["browser_pages"], fresh["browser_pages"])
        if allowed and result.get("ok"):
            _save_document(conn, updated, operation, result)
        else:
            reason = _diagnostic(result.get("error"), "document_fetch_failed") if allowed else "collection_cancelled"
            conn.execute("UPDATE documents SET status='failed',error=? WHERE id=?", (reason, operation["id"]))
        if result.get("http_status") in {401, 403, 429}:
            for host in {_host(operation["url"]), _host(result.get("url", operation["url"]))}:
                if host not in updated["blocked_hosts"]:
                    updated["blocked_hosts"].append(host)
        updated["used_seconds"] += max(0.0, time.monotonic() - started - charged)
        store.write_metadata(conn, updated)


def _save_document(
    conn: sqlite3.Connection, current: dict[str, Any], operation: dict[str, Any], result: dict[str, Any]
) -> None:
    pages = result.get("pages", [])
    classification = classify_source(result.get("url", operation["url"]))
    conn.execute(
        """UPDATE documents SET status='fetched',final_url=?,title=?,kind=?,method=?,source_class=?,source_tier=?,source_basis=?,
           retrieved_at=?,raw_file=?,limitations=?,page_count=?,error='' WHERE id=?""",
        (
            result.get("url", operation["url"]),
            api._safe_text(result.get("title") or operation["title"], 300),
            result.get("kind", ""),
            result.get("extraction_method", "unknown"),
            classification.source_class,
            classification.source_tier,
            classification.source_basis,
            time.time(),
            result.get("raw_file", ""),
            json.dumps(result.get("limitations", [])),
            result.get("page_count", len(pages)),
            operation["id"],
        ),
    )
    api.ingest_pages(
        conn, current, operation["id"], pages, proof_allowed=result.get("extraction_method") != "browser_dom"
    )
    _refresh_duplicates(conn, operation["id"])


def _refresh_duplicates(conn: sqlite3.Connection, document_id: str) -> None:
    pages = [
        dict(row)
        for row in conn.execute("SELECT page,text FROM pages WHERE document_id=? ORDER BY page", (document_id,))
    ]
    digest = api.content_digest(pages) if any(page["text"].strip() for page in pages) else ""
    conn.execute("UPDATE documents SET digest=? WHERE id=?", (digest, document_id))
    # Rebuild direct links to the first current copy in each cluster. OCR can
    # change the primary document, so updating only its own link is insufficient.
    primary: dict[str, str] = {}
    for row in conn.execute("SELECT id,status,digest,duplicate_of FROM documents ORDER BY rowid"):
        duplicate = ""
        if row["status"] == "fetched" and row["digest"]:
            duplicate = primary.setdefault(row["digest"], row["id"])
            if duplicate == row["id"]:
                duplicate = ""
        if duplicate != row["duplicate_of"]:
            conn.execute("UPDATE documents SET duplicate_of=? WHERE id=?", (duplicate, row["id"]))


def queue_ocr(job_id: str, document_id: str, page: int) -> dict[str, Any]:
    if not store.enabled():
        return store.error("vin_research_disabled", job_id)
    with store.start_lock(), store.connect(job_id, transaction=True) as conn:
        current = store.metadata(conn)
        if current["cancel_requested"] or current["collection_status"] in {"cancelled", "failed"}:
            return store.error("job_not_extendable", job_id)
        doc = conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if doc is None or doc["kind"] != "pdf" or not doc["raw_file"]:
            return store.error("ocr_document_unavailable", job_id)
        previous = conn.execute(
            "SELECT status,error FROM ocr_requests WHERE document_id=? AND page=?", (document_id, page)
        ).fetchone()
        if previous:
            if previous["status"] == "done":
                return {**api._read_document(conn, document_id, page, 0, 8000), "job_id": job_id}
            return {
                "ok": previous["status"] != "failed",
                "schema": store.SCHEMA,
                "job_id": job_id,
                "document_id": document_id,
                "page": page,
                "ocr_status": previous["status"],
                "error": previous["error"] or None,
            }
        if current["ocr_pages"] >= store.MAX_OCR_PAGES or current["used_seconds"] >= store.MAX_SECONDS:
            return store.error("ocr_budget_exhausted", job_id)
        if doc["page_count"] and page > doc["page_count"]:
            return store.error("document_page_invalid", job_id)
        with store.general._db() as queue:
            queue.execute("BEGIN IMMEDIATE")
            if queue.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running') AND id<>?", (job_id,)).fetchone():
                return store.error("j1_busy", job_id)
            current["ocr_pages"] += 1
            current["collection_status"] = "running" if current["inflight"] else "queued"
            api._invalidation(current)
            conn.execute("INSERT INTO ocr_requests(document_id,page) VALUES(?,?)", (document_id, page))
            store.write_metadata(conn, current)
            queue.execute("UPDATE jobs SET status=?,error='' WHERE id=?", (current["collection_status"], job_id))
            queue.commit()
        return {
            "ok": True,
            "schema": store.SCHEMA,
            "job_id": job_id,
            "document_id": document_id,
            "page": page,
            "ocr_status": "queued",
            "revision": current["revision"],
        }


def _collect_ocr(job_id: str, operation: dict[str, Any], current: dict[str, Any]) -> None:
    started = time.monotonic()
    with store.connect(job_id) as conn:
        doc = conn.execute("SELECT * FROM documents WHERE id=?", (operation["document_id"],)).fetchone()
    path = store.job_directory(job_id) / doc["raw_file"]
    if path.is_symlink() or path.parent != store.job_directory(job_id):
        raise store.VinJobError("ocr_document_unavailable")
    result = ocr_pages(
        path.read_bytes(), [operation["page"]], path.parent, timeout_seconds=_remaining(current, started)
    )
    with store.connect(job_id, transaction=True) as conn:
        charged = time.monotonic() - started
        updated, allowed = _finish_operation(conn, charged)
        pages = result.get("pages", []) if allowed else []
        if pages:
            conn.execute(
                "UPDATE documents SET version=version+1,method='pdf_ocr',status='fetched' WHERE id=?",
                (operation["document_id"],),
            )
            api.ingest_pages(conn, updated, operation["document_id"], pages, replace=False)
            _refresh_duplicates(conn, operation["document_id"])
        reason = "" if result.get("ok") and allowed else _diagnostic(result.get("error"), "ocr_failed")
        conn.execute(
            "UPDATE ocr_requests SET status=?,error=? WHERE document_id=? AND page=?",
            ("done" if not reason else "failed", reason, operation["document_id"], operation["page"]),
        )
        updated["used_seconds"] += max(0.0, time.monotonic() - started - charged)
        store.write_metadata(conn, updated)


def _operation_failed(job_id: str, table: str, operation: dict[str, Any], started: float, code: str) -> None:
    with store.connect(job_id, transaction=True) as conn:
        current, _ = _finish_operation(conn, time.monotonic() - started)
        conn.execute(f"UPDATE {table} SET status='failed',error=? WHERE rowid=?", (code, operation["operation_rowid"]))
        if code == "document_text_limit_reached":
            current["stop_reason"] = code
        store.write_metadata(conn, current)


def run_job(job_id: str) -> None:
    try:
        with store.connect(job_id, transaction=True) as conn:
            current = store.metadata(conn)
            _reset_interrupted(conn, current)
        while not store.general._STOP:
            pending = _next_operation(job_id)
            if pending is None:
                break
            table, operation, current = pending
            started = time.monotonic()
            try:
                if table == "queries":
                    _collect_search(job_id, operation, current)
                elif table == "documents":
                    _collect_document(job_id, operation, current)
                else:
                    _collect_ocr(job_id, operation, current)
            except (OSError, ValueError, sqlite3.Error, store.VinJobError) as exc:
                safe_codes = {
                    "scope_expired",
                    "fetch_timeout",
                    "fetch_failed",
                    "unsafe_url",
                    "unsafe_redirect",
                    "unsafe_dns_answer",
                    "robots_disallowed",
                    "redirect_robots_disallowed",
                    "document_too_large",
                    "document_text_limit_reached",
                    "unsupported_content_encoding",
                    "too_many_redirects",
                    "large_document_source_untrusted",
                    "ocr_document_unavailable",
                    "vin_storage_limit_reached",
                    "vin_storage_unavailable",
                }
                code = str(exc) if str(exc) in safe_codes else "collection_operation_failed"
                _operation_failed(job_id, table, operation, started, code)
        with store.connect(job_id, transaction=True) as conn:
            current = store.metadata(conn)
            if current["collection_status"] == "running":
                current["collection_status"] = "queued"
                store.write_metadata(conn, current)
            # Publish while the private state is locked, just like enqueue.
            # Publishing after commit could overwrite a newly queued request.
            store.set_stub(job_id, current["collection_status"], current["stop_reason"])
    except (OSError, ValueError, sqlite3.Error, store.VinJobError) as exc:
        code = exc.code if isinstance(exc, store.VinJobError) else "vin_collection_failed"
        store.fail_worker(job_id, code)
