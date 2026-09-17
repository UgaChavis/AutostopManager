"""J1 public research queue and temporary searchable corpus.

The native Manager MCP tools call the small synchronous API below. A separate
hardened worker executes public searches and fetches. Only sanitized text
is retained, for at most seven days; this is not Manager business memory.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import sys
import time
from typing import Any
from uuid import uuid4

from .j1_fetch import (
    contains_sensitive,
    contains_unsafe_url,
    fetch_document,
    public_url,
    redact_sensitive,
    search_public,
)

SCHEMA = "autostop.j1.research.v1"
MAX_QUERIES = 30
MAX_PAGES = 300
MAX_DOCUMENT_CHARS = 50_000
MAX_CORPUS_BYTES = 250 * 1024 * 1024
RETENTION_DAYS = 7
_IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
_STOP = False


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _cache_dir() -> Path:
    return Path(
        os.environ.get("AUTOSTOP_J1_CACHE_DIR") or os.environ.get("AUTOSTOP_J1_DATA_DIR") or "/var/cache/autostop-j1"
    )


def _db_path() -> Path:
    return _cache_dir() / "research.sqlite3"


def _error(code: str, *, job_id: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "schema": SCHEMA, "error": {"code": code}}
    if job_id:
        result["job_id"] = job_id
    return result


@contextmanager
def _db(*, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    path = _db_path()
    if readonly:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        if not readonly:
            connection.execute("PRAGMA foreign_keys=ON")
            _init_schema(connection)
        yield connection
    finally:
        connection.close()


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, objective TEXT NOT NULL, max_pages INTEGER NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at);
        CREATE TABLE IF NOT EXISTS queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
            query TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            provider TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE,
            UNIQUE(job_id, query)
        );
        CREATE INDEX IF NOT EXISTS queries_pending ON queries(job_id, status, id);
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL, url TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
            body TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
            retrieved_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE,
            UNIQUE(job_id, url)
        );
        CREATE INDEX IF NOT EXISTS documents_job_status ON documents(job_id, status);
        CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
            job_id UNINDEXED, document_id UNINDEXED, title, body, tokenize='unicode61'
        );
        """
    )


def _valid_id(value: str) -> bool:
    return bool(_IDENTIFIER.fullmatch(str(value or "")))


def _clean_input(value: str, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _validated_queries(queries: list[str], *, allow_empty: bool = False) -> list[str] | None:
    if not isinstance(queries, list) or len(queries) > MAX_QUERIES or (not queries and not allow_empty):
        return None
    result: list[str] = []
    seen: set[str] = set()
    for item in queries:
        if not isinstance(item, str) or len(item) > 500 or contains_sensitive(item) or contains_unsafe_url(item):
            return None
        query = _clean_input(item, limit=500)
        if not query:
            return None
        normalized = query.casefold()
        if normalized not in seen:
            seen.add(normalized)
            result.append(query)
    return result


def start_research(objective: str, queries: list[str], max_pages: int = MAX_PAGES) -> dict[str, Any]:
    """Queue one de-identified, read-only public research job."""

    if (
        not isinstance(objective, str)
        or len(objective) > 2000
        or contains_sensitive(objective)
        or contains_unsafe_url(objective)
    ):
        return _error("objective_invalid_or_sensitive")
    clean_objective = _clean_input(objective, limit=2000)
    clean_queries = _validated_queries(queries)
    if not clean_objective or clean_queries is None:
        return _error("queries_or_objective_invalid")
    if type(max_pages) is not int or not 1 <= max_pages <= MAX_PAGES:
        return _error("max_pages_invalid")
    job_id = uuid4().hex
    now = _utcnow()
    try:
        with _db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1").fetchone():
                return _error("j1_busy")
            conn.execute(
                "INSERT INTO jobs(id,objective,max_pages,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (job_id, clean_objective, max_pages, "queued", now, now),
            )
            conn.executemany(
                "INSERT INTO queries(job_id,query) VALUES(?,?)", ((job_id, query) for query in clean_queries)
            )
            conn.commit()
    except (OSError, sqlite3.Error):
        return _error("j1_store_unavailable")
    return {"ok": True, "schema": SCHEMA, "job_id": job_id, "status": "queued", "query_count": len(clean_queries)}


def research_status(job_id: str) -> dict[str, Any]:
    if not _valid_id(job_id):
        return _error("job_id_invalid")
    try:
        with _db() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return _error("job_not_found", job_id=job_id)
            counts = conn.execute(
                "SELECT COUNT(*) AS total, SUM(status='fetched') AS fetched, SUM(status IN ('failed','duplicate')) AS failed FROM documents WHERE job_id=?",
                (job_id,),
            ).fetchone()
            queries = conn.execute(
                "SELECT COUNT(*) AS total, SUM(status='done') AS done, SUM(status='failed') AS failed FROM queries WHERE job_id=?",
                (job_id,),
            ).fetchone()
            failures = [
                {"reason": row["error"], "count": row["count"]}
                for row in conn.execute(
                    """SELECT error,COUNT(*) AS count FROM documents
                       WHERE job_id=? AND status='failed' GROUP BY error ORDER BY count DESC LIMIT 10""",
                    (job_id,),
                )
            ]
            search_failures = [
                {"reason": row["error"], "count": row["count"]}
                for row in conn.execute(
                    """SELECT error,COUNT(*) AS count FROM queries
                       WHERE job_id=? AND status='failed' GROUP BY error ORDER BY count DESC LIMIT 5""",
                    (job_id,),
                )
            ]
            return {
                "ok": True,
                "schema": SCHEMA,
                "job_id": job_id,
                "status": row["status"],
                "objective": row["objective"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "max_pages": row["max_pages"],
                "pages_total": counts["total"],
                "pages_fetched": counts["fetched"] or 0,
                "pages_failed": counts["failed"] or 0,
                "queries_total": queries["total"],
                "queries_done": queries["done"] or 0,
                "queries_failed": queries["failed"] or 0,
                "page_failures": failures,
                "search_failures": search_failures,
                "error": row["error"] or None,
            }
    except (OSError, sqlite3.Error):
        return _error("j1_store_unavailable", job_id=job_id)


def _fts_expression(query: str) -> str:
    tokens = re.findall(r"[^\W_]{2,}", query, flags=re.UNICODE)[:10]
    return " OR ".join('"' + token.replace('"', "") + '"' for token in tokens)


def research_results(job_id: str, query: str = "", cursor: int = 0, limit: int = 20) -> dict[str, Any]:
    if not _valid_id(job_id):
        return _error("job_id_invalid")
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 50:
        return _error("pagination_invalid", job_id=job_id)
    if not isinstance(query, str) or len(query) > 300 or contains_sensitive(query):
        return _error("query_invalid_or_sensitive", job_id=job_id)
    expression = _fts_expression(query)
    if query.strip() and not expression:
        return _error("query_invalid_or_sensitive", job_id=job_id)
    try:
        with _db() as conn:
            if not conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
                return _error("job_not_found", job_id=job_id)
            if expression:
                total = conn.execute(
                    "SELECT COUNT(*) FROM documents_fts WHERE job_id=? AND documents_fts MATCH ?", (job_id, expression)
                ).fetchone()[0]
                rows = conn.execute(
                    """SELECT d.id,d.url,d.title,d.source,d.kind,d.status,d.error,d.created_at,d.retrieved_at,
                              snippet(documents_fts,3,'[',']','…',24) AS excerpt
                       FROM documents_fts JOIN documents AS d ON d.id=documents_fts.document_id
                       WHERE documents_fts.job_id=? AND documents_fts MATCH ?
                       ORDER BY bm25(documents_fts) LIMIT ? OFFSET ?""",
                    (job_id, expression, limit, cursor),
                ).fetchall()
            else:
                total = conn.execute("SELECT COUNT(*) FROM documents WHERE job_id=?", (job_id,)).fetchone()[0]
                rows = conn.execute(
                    """SELECT id,url,title,source,kind,status,error,created_at,retrieved_at,
                              substr(body,1,300) AS excerpt
                       FROM documents WHERE job_id=?
                       ORDER BY created_at,id LIMIT ? OFFSET ?""",
                    (job_id, limit, cursor),
                ).fetchall()
            items = [
                {
                    "document_id": row["id"],
                    "url": row["url"],
                    "title": row["title"],
                    "source": row["source"],
                    "kind": row["kind"],
                    "status": row["status"],
                    "error": row["error"] or None,
                    "excerpt": row["excerpt"],
                    "discovered_at": row["created_at"],
                    "retrieved_at": row["retrieved_at"] or None,
                }
                for row in rows
            ]
            return {
                "ok": True,
                "schema": SCHEMA,
                "job_id": job_id,
                "query": query.strip(),
                "total": total,
                "cursor": cursor,
                "next_cursor": cursor + len(items) if cursor + len(items) < total else None,
                "results": items,
            }
    except (OSError, sqlite3.Error):
        return _error("j1_store_unavailable", job_id=job_id)


def research_document(job_id: str, document_id: str, offset: int = 0, max_chars: int = 8000) -> dict[str, Any]:
    if not _valid_id(job_id) or not _valid_id(document_id):
        return _error("identifier_invalid")
    if type(offset) is not int or offset < 0 or type(max_chars) is not int or not 1 <= max_chars <= 8000:
        return _error("pagination_invalid", job_id=job_id)
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT url,title,kind,body,source,created_at,retrieved_at FROM documents WHERE job_id=? AND id=? AND status='fetched'",
                (job_id, document_id),
            ).fetchone()
            if row is None:
                return _error("document_not_found", job_id=job_id)
            body = row["body"]
            end = min(offset + max_chars, len(body))
            return {
                "ok": True,
                "schema": SCHEMA,
                "job_id": job_id,
                "document_id": document_id,
                "url": row["url"],
                "title": row["title"],
                "kind": row["kind"],
                "source": row["source"],
                "discovered_at": row["created_at"],
                "retrieved_at": row["retrieved_at"],
                "text": body[offset:end],
                "offset": offset,
                "total_chars": len(body),
                "next_offset": end if end < len(body) else None,
            }
    except (OSError, sqlite3.Error):
        return _error("j1_store_unavailable", job_id=job_id)


def research_add_queries(job_id: str, queries: list[str]) -> dict[str, Any]:
    if not _valid_id(job_id):
        return _error("job_id_invalid")
    clean_queries = _validated_queries(queries)
    if clean_queries is None:
        return _error("queries_invalid_or_sensitive", job_id=job_id)
    try:
        with _db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                return _error("job_not_found", job_id=job_id)
            if job["status"] in {"cancelled", "failed"}:
                return _error("job_not_extendable", job_id=job_id)
            count = conn.execute("SELECT COUNT(*) FROM queries WHERE job_id=?", (job_id,)).fetchone()[0]
            existing = {
                row[0].casefold() for row in conn.execute("SELECT query FROM queries WHERE job_id=?", (job_id,))
            }
            additions = [item for item in clean_queries if item.casefold() not in existing]
            if count + len(additions) > MAX_QUERIES:
                return _error("query_limit_reached", job_id=job_id)
            if job["status"] == "completed":
                busy = conn.execute(
                    "SELECT 1 FROM jobs WHERE status IN ('queued','running') AND id<>? LIMIT 1", (job_id,)
                ).fetchone()
                if busy:
                    return _error("j1_busy", job_id=job_id)
            conn.executemany("INSERT INTO queries(job_id,query) VALUES(?,?)", ((job_id, item) for item in additions))
            if additions and job["status"] == "completed":
                conn.execute("UPDATE jobs SET status='queued',updated_at=? WHERE id=?", (_utcnow(), job_id))
            conn.commit()
            return {
                "ok": True,
                "schema": SCHEMA,
                "job_id": job_id,
                "added": len(additions),
                "queries_total": count + len(additions),
            }
    except (OSError, sqlite3.Error):
        return _error("j1_store_unavailable", job_id=job_id)


def research_cancel(job_id: str) -> dict[str, Any]:
    if not _valid_id(job_id):
        return _error("job_id_invalid")
    try:
        with _db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                return _error("job_not_found", job_id=job_id)
            if job["status"] in {"queued", "running"}:
                conn.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE id=?", (_utcnow(), job_id))
            conn.commit()
            return {
                "ok": True,
                "schema": SCHEMA,
                "job_id": job_id,
                "status": "cancelled" if job["status"] in {"queued", "running"} else job["status"],
            }
    except (OSError, sqlite3.Error):
        return _error("j1_store_unavailable", job_id=job_id)


def _cache_size() -> int:
    path = _db_path()
    return sum(part.stat().st_size for part in (path, Path(str(path) + "-wal")) if part.exists())


def _prune(conn: sqlite3.Connection) -> None:
    cutoff = (datetime.now(UTC) - timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")
    expired = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM jobs WHERE created_at<? AND status NOT IN ('queued','running')", (cutoff,)
        )
    ]
    for job_id in expired:
        conn.execute("DELETE FROM documents_fts WHERE job_id=?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    conn.commit()
    if expired:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    if _cache_size() <= MAX_CORPUS_BYTES:
        return
    old = [
        row[0]
        for row in conn.execute("SELECT id FROM jobs WHERE status NOT IN ('queued','running') ORDER BY created_at")
    ]
    for job_id in old:
        conn.execute("DELETE FROM documents_fts WHERE job_id=?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        if _cache_size() <= MAX_CORPUS_BYTES:
            break


def _claim_job(conn: sqlite3.Connection) -> str | None:
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
    if row:
        conn.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?", (_utcnow(), row["id"]))
    conn.commit()
    return row["id"] if row else None


def _work_job(job_id: str) -> None:
    """Do one bounded unit at a time so cancellation is observed promptly."""

    while not _STOP:
        with _db() as conn:
            job = conn.execute("SELECT status,max_pages FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None or job["status"] != "running":
                return
            pending_query = conn.execute(
                "SELECT id,query FROM queries WHERE job_id=? AND status='pending' ORDER BY id LIMIT 1", (job_id,)
            ).fetchone()
            pending_doc = conn.execute(
                "SELECT id,url FROM documents WHERE job_id=? AND status='pending' ORDER BY created_at,id LIMIT 1",
                (job_id,),
            ).fetchone()
            if pending_query:
                conn.execute("UPDATE queries SET status='running' WHERE id=?", (pending_query["id"],))
                conn.commit()
                query_id, query = pending_query["id"], pending_query["query"]
                phase = "query"
            elif pending_doc:
                conn.execute("UPDATE documents SET status='running' WHERE id=?", (pending_doc["id"],))
                conn.commit()
                doc_id, url = pending_doc["id"], pending_doc["url"]
                phase = "document"
            else:
                conn.execute("UPDATE jobs SET status='completed',updated_at=? WHERE id=?", (_utcnow(), job_id))
                conn.commit()
                return
        if phase == "query":
            try:
                results, provider = search_public(query, searxng_url=os.environ.get("AUTOSTOP_J1_SEARXNG_URL", ""))
            except Exception:  # noqa: BLE001 - one provider error must not fail the corpus.
                results, provider = [], "search_failed"
            with _db() as conn:
                if conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] != "running":
                    return
                current_count = conn.execute("SELECT COUNT(*) FROM documents WHERE job_id=?", (job_id,)).fetchone()[0]
                for row in results:
                    if current_count >= job["max_pages"]:
                        break
                    url = public_url(row.get("url", ""))
                    if not url:
                        continue
                    inserted = conn.execute(
                        "INSERT OR IGNORE INTO documents(id,job_id,url,title,source,created_at) VALUES(?,?,?,?,?,?)",
                        (
                            uuid4().hex,
                            job_id,
                            url,
                            redact_sensitive(row.get("title", ""), limit=200),
                            row.get("source", "")[:40],
                            _utcnow(),
                        ),
                    )
                    current_count += inserted.rowcount
                conn.execute(
                    "UPDATE queries SET status=?,provider=?,error=? WHERE id=?",
                    ("done" if results else "failed", provider[:40], "" if results else provider[:80], query_id),
                )
                conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (_utcnow(), job_id))
                conn.commit()
        else:
            try:
                fetch_result = fetch_document(url)
            except Exception:  # noqa: BLE001 - one malformed page must not fail the corpus.
                fetch_result = {"ok": False, "error": "fetch_failed"}
            with _db() as conn:
                if conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] != "running":
                    return
                if fetch_result.get("ok") is True:
                    body = redact_sensitive(fetch_result.get("text", ""), limit=MAX_DOCUMENT_CHARS)
                    # FTS repeats the text; reserve enough room before either insert.
                    if _cache_size() + len(body.encode("utf-8")) * 3 > MAX_CORPUS_BYTES:
                        _prune(conn)
                    if _cache_size() + len(body.encode("utf-8")) * 3 > MAX_CORPUS_BYTES:
                        conn.execute(
                            "UPDATE documents SET status='failed',error='cache_capacity_reached' WHERE id=?", (doc_id,)
                        )
                        conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (_utcnow(), job_id))
                        conn.commit()
                        continue
                    digest = hashlib.sha256(body.encode()).hexdigest()
                    exists = conn.execute(
                        "SELECT 1 FROM documents WHERE job_id=? AND content_hash=? AND status='fetched' LIMIT 1",
                        (job_id, digest),
                    ).fetchone()
                    if exists:
                        conn.execute(
                            "UPDATE documents SET status='duplicate',error='duplicate_content' WHERE id=?", (doc_id,)
                        )
                    else:
                        title = redact_sensitive(fetch_result.get("title", ""), limit=200)
                        conn.execute(
                            "UPDATE documents SET status='fetched',url=?,title=?,kind=?,body=?,content_hash=?,retrieved_at=? WHERE id=?",
                            (
                                fetch_result.get("url", url),
                                title,
                                fetch_result.get("kind", ""),
                                body,
                                digest,
                                _utcnow(),
                                doc_id,
                            ),
                        )
                        conn.execute(
                            "INSERT INTO documents_fts(job_id,document_id,title,body) VALUES(?,?,?,?)",
                            (job_id, doc_id, title, body),
                        )
                else:
                    conn.execute(
                        "UPDATE documents SET status='failed',error=? WHERE id=?",
                        (str(fetch_result.get("error") or "fetch_failed")[:80], doc_id),
                    )
                conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (_utcnow(), job_id))
                conn.commit()


def _on_stop(_signum: int, _frame: Any) -> None:
    global _STOP
    _STOP = True


def run_worker(*, once: bool = False) -> None:
    _cache_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    with (_cache_dir() / "worker.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _run_locked_worker(once=once)


def _run_locked_worker(*, once: bool) -> None:
    signal.signal(signal.SIGTERM, _on_stop)
    signal.signal(signal.SIGINT, _on_stop)
    with _db() as conn:
        conn.execute("UPDATE jobs SET status='queued' WHERE status='running'")
        conn.execute("UPDATE queries SET status='pending' WHERE status='running'")
        conn.execute("UPDATE documents SET status='pending' WHERE status='running'")
        conn.commit()
    last_prune = 0.0
    while not _STOP:
        with _db() as conn:
            if time.monotonic() - last_prune > 3600:
                _prune(conn)
                last_prune = time.monotonic()
            job_id = _claim_job(conn)
        if job_id:
            try:
                _work_job(job_id)
            except Exception:  # noqa: BLE001 - errors are reported without URLs or source text.
                with _db() as conn:
                    conn.execute(
                        "UPDATE jobs SET status='failed',error='worker_failed',updated_at=? WHERE id=? AND status='running'",
                        (_utcnow(), job_id),
                    )
                    conn.commit()
        elif once:
            return
        else:
            time.sleep(2)
        if once:
            return


def probe() -> dict[str, Any]:
    """Cheap read-only health check after the worker created the cache."""

    if not _db_path().exists():
        return _error("j1_store_unavailable")
    try:
        with _db(readonly=True) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"jobs", "queries", "documents", "documents_fts"}.issubset(tables):
                return _error("j1_schema_invalid")
            conn.execute("SELECT COUNT(*) FROM documents_fts").fetchone()
        searxng = os.environ.get("AUTOSTOP_J1_SEARXNG_URL", "")
        if searxng:
            from urllib.parse import urlsplit

            parsed = urlsplit(searxng)
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
                return _error("searxng_url_invalid")
        return {"ok": True, "schema": SCHEMA, "cache_ready": True, "search_configured": bool(searxng)}
    except (OSError, sqlite3.Error):
        return _error("j1_store_unavailable")


def main() -> None:
    parser = argparse.ArgumentParser(description="J1 public research worker")
    parser.add_argument("command", choices=("worker", "probe"))
    parser.add_argument("--once", action="store_true", help="Process one queued job and exit")
    args = parser.parse_args()
    if args.command == "worker":
        run_worker(once=args.once)
    else:
        result = probe()
        print(json.dumps(result, ensure_ascii=False))
        if not result["ok"]:
            sys.exit(1)


if __name__ == "__main__":
    main()
