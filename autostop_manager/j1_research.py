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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
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
_URL_TOKEN = re.compile(r"(?i)(?<!\w)[a-z][a-z0-9+.-]*://[^\s<>]+")
_WORD = re.compile(r"[^\W_]{3,}", flags=re.UNICODE)
_STOP = False

# Only parameters whose documented purpose is attribution are removed. In
# particular, generic names such as ``ref`` and all unknown parameters stay:
# they can identify a real document or determine the selected language/version.
_TRACKING_PARAMETERS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "yclid",
        "ysclid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "igshid",
        "vero_id",
        "_hsenc",
        "_hsmi",
        "oly_enc_id",
        "oly_anon_id",
    }
)

# ``official_registry`` is deliberately opt-in. Other labels are collection
# aids, not a credibility verdict, and always include the matching basis.
_OFFICIAL_DOMAIN_REGISTRY: dict[str, str] = {
    "nhtsa.gov": "US National Highway Traffic Safety Administration",
    "safercar.gov": "US National Highway Traffic Safety Administration",
    "mercedes-benz.com": "Mercedes-Benz",
    "mbusa.com": "Mercedes-Benz USA",
    "bmwgroup.com": "BMW Group",
    "bmw.com": "BMW",
    "audi.com": "Audi",
    "volkswagen.com": "Volkswagen",
    "toyota.com": "Toyota",
    "honda.com": "Honda",
    "ford.com": "Ford",
    "gm.com": "General Motors",
    "stellantis.com": "Stellantis",
}
_SUPPLIER_DOMAINS = frozenset(
    {
        "autodoc.de",
        "autodoc.ru",
        "exist.ru",
        "emex.ru",
        "partsouq.com",
        "rockauto.com",
        "partsapi.ru",
    }
)
_OWNER_COMMUNITY_DOMAINS = frozenset(
    {
        "drive2.ru",
        "reddit.com",
        "mbworld.org",
        "benzworld.org",
        "bimmerpost.com",
        "vwvortex.com",
    }
)
_EDITORIAL_DOMAINS = frozenset(
    {
        "caranddriver.com",
        "motortrend.com",
        "autonews.ru",
        "zr.ru",
    }
)
_TECHNICAL_DOMAINS = frozenset(
    {
        "oemdtc.com",
        "workshop-manuals.com",
        "manualslib.com",
    }
)


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


def _canonical_url(value: str) -> str:
    """Return a stable public-document key without changing meaningful parameters."""

    try:
        parsed = urlsplit(str(value or "").strip())
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not host:
        return ""
    hostname = host.casefold().rstrip(".")
    netloc = hostname
    if port is not None and not (
        (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)
    ):
        netloc += f":{port}"
    try:
        parameters = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError:
        return ""
    kept = [
        (name, item)
        for name, item in parameters
        if not (name.casefold().startswith("utm_") or name.casefold() in _TRACKING_PARAMETERS)
    ]
    return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path or "/", urlencode(kept, doseq=True), ""))


def _same_or_subdomain(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith("." + domain)


def _classify_source(url: str, title: str, kind: str) -> tuple[str, str]:
    """Classify collection provenance deterministically; never infer authority."""

    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        path = parsed.path.casefold()
    except ValueError:
        return "unknown", "fallback:invalid_url"
    for domain, label in _OFFICIAL_DOMAIN_REGISTRY.items():
        if _same_or_subdomain(host, domain):
            return "official_registry", f"registry:official:{domain}:{label}"
    for domain in _TECHNICAL_DOMAINS:
        if _same_or_subdomain(host, domain):
            return "technical", f"registry:technical:{domain}"
    for domain in _SUPPLIER_DOMAINS:
        if _same_or_subdomain(host, domain):
            return "supplier_catalog", f"registry:supplier:{domain}"
    for domain in _OWNER_COMMUNITY_DOMAINS:
        if _same_or_subdomain(host, domain):
            return "owner_community", f"registry:owner_community:{domain}"
    for domain in _EDITORIAL_DOMAINS:
        if _same_or_subdomain(host, domain):
            return "editorial", f"registry:editorial:{domain}"
    if host.startswith(("forum.", "forums.")) or any(token in path for token in ("/forum", "/forums/", "/community/")):
        return "owner_community", "heuristic:community_url"
    if any(token in host for token in ("catalog", "parts", "autoparts")):
        return "supplier_catalog", "heuristic:catalog_hostname"
    title_lower = title.casefold()
    if kind == "pdf" and any(token in (path + " " + title_lower) for token in ("manual", "workshop", "service", "tsb")):
        return "technical", "heuristic:technical_pdf"
    if any(token in host for token in ("news", "media", "journal", "magazine")):
        return "editorial", "heuristic:editorial_hostname"
    return "unknown", "fallback:unclassified"


def _detect_language(*values: str) -> str:
    text = " ".join(str(value or "") for value in values)
    cyrillic = sum(1 for char in text if "А" <= char <= "я" or char in "Ёё")
    latin = sum(1 for char in text if "A" <= char <= "Z" or "a" <= char <= "z")
    total = cyrillic + latin
    if total < 20:
        return "unknown"
    if cyrillic / total >= 0.65:
        return "ru"
    if latin / total >= 0.65:
        return "en"
    return "mixed"


def _extraction_method(fetch_result: dict[str, Any]) -> str:
    declared = _clean_input(str(fetch_result.get("extraction_method") or ""), limit=40)
    if declared:
        return declared
    return {"html": "html_text", "text": "plain_text", "pdf": "pdf_text"}.get(
        str(fetch_result.get("kind") or ""), "unknown"
    )


def _simhash(value: str) -> str:
    """Conservative 64-bit similarity fingerprint for same-job duplicate linking."""

    tokens = _WORD.findall(str(value or "").casefold())[:2_048]
    if len(tokens) < 80:
        return ""
    weights = [0] * 64
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        number = int.from_bytes(digest, "big")
        for bit in range(64):
            weights[bit] += 1 if number & (1 << bit) else -1
    result = 0
    for bit, weight in enumerate(weights):
        if weight >= 0:
            result |= 1 << bit
    return f"{result:016x}"


def _simhash_distance(left: str, right: str) -> int | None:
    try:
        if not left or not right:
            return None
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError:
        return None


def _find_duplicate(
    conn: sqlite3.Connection, *, job_id: str, digest: str, similarity_hash: str
) -> tuple[sqlite3.Row, str, float] | None:
    exact = conn.execute(
        """SELECT id,url FROM documents
           WHERE job_id=? AND content_hash=? AND status='fetched' LIMIT 1""",
        (job_id, digest),
    ).fetchone()
    if exact is not None:
        return exact, "exact", 1.0
    if not similarity_hash:
        return None
    rows = conn.execute(
        """SELECT id,url,content_simhash FROM documents
           WHERE job_id=? AND status='fetched' AND content_simhash<>''""",
        (job_id,),
    ).fetchall()
    candidate: tuple[sqlite3.Row, int] | None = None
    for row in rows:
        distance = _simhash_distance(similarity_hash, row["content_simhash"])
        if distance is None or distance > 3:
            continue
        if candidate is None or distance < candidate[1]:
            candidate = (row, distance)
    if candidate is None:
        return None
    # The threshold is intentionally strict: related articles must remain
    # separate sources unless their wording is almost identical.
    return candidate[0], "near", round(1 - candidate[1] / 64, 4)


def _schema_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_documents(conn: sqlite3.Connection) -> None:
    required = {
        "canonical_url": "TEXT NOT NULL DEFAULT ''",
        "language": "TEXT NOT NULL DEFAULT 'unknown'",
        "extraction_method": "TEXT NOT NULL DEFAULT ''",
        "source_class": "TEXT NOT NULL DEFAULT 'unknown'",
        "source_basis": "TEXT NOT NULL DEFAULT ''",
        "duplicate_of": "TEXT NOT NULL DEFAULT ''",
        "duplicate_kind": "TEXT NOT NULL DEFAULT ''",
        "duplicate_score": "REAL NOT NULL DEFAULT 0",
        "content_simhash": "TEXT NOT NULL DEFAULT ''",
    }
    present = _schema_columns(conn, "documents")
    for name, definition in required.items():
        if name not in present:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {name} {definition}")
    conn.execute("CREATE INDEX IF NOT EXISTS documents_job_canonical ON documents(job_id, canonical_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS documents_duplicate_of ON documents(duplicate_of)")
    # Existing temporary jobs keep their records. Canonical backfill is cheap
    # and does not read the network or alter the original source URL.
    for row in conn.execute("SELECT id,url FROM documents WHERE canonical_url='' LIMIT 1000"):
        canonical = _canonical_url(row["url"])
        if canonical:
            conn.execute("UPDATE documents SET canonical_url=? WHERE id=?", (canonical, row["id"]))


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
            canonical_url TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
            body TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL DEFAULT '', content_simhash TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT 'unknown', extraction_method TEXT NOT NULL DEFAULT '',
            source_class TEXT NOT NULL DEFAULT 'unknown', source_basis TEXT NOT NULL DEFAULT '',
            duplicate_of TEXT NOT NULL DEFAULT '', duplicate_kind TEXT NOT NULL DEFAULT '',
            duplicate_score REAL NOT NULL DEFAULT 0,
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
    _migrate_documents(conn)


def _valid_id(value: str) -> bool:
    return bool(_IDENTIFIER.fullmatch(str(value or "")))


def _clean_input(value: str, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _trim_url_token(value: str) -> str:
    return value.rstrip(".,;)]}")


def _input_is_safe(value: str) -> bool:
    """Apply DLP to prose while letting the URL validator inspect URLs first.

    A direct public URL can contain long numeric bulletin identifiers. It must be
    parsed by ``public_url`` before the VIN detector sees unrelated path/query
    punctuation. Every embedded URL must still pass the same validator.
    """

    raw = str(value or "").strip()
    if not raw:
        return False
    if public_url(raw):
        return True
    matches = list(_URL_TOKEN.finditer(raw))
    if not matches:
        return not contains_sensitive(raw) and not contains_unsafe_url(raw)
    remainder: list[str] = []
    cursor = 0
    for match in matches:
        remainder.append(raw[cursor : match.start()])
        if not public_url(_trim_url_token(match.group())):
            return False
        cursor = match.end()
    remainder.append(raw[cursor:])
    prose = " ".join(remainder)
    return not contains_sensitive(prose) and not contains_unsafe_url(prose)


def _validated_queries(queries: list[str], *, allow_empty: bool = False) -> list[str] | None:
    if not isinstance(queries, list) or len(queries) > MAX_QUERIES or (not queries and not allow_empty):
        return None
    result: list[str] = []
    seen: set[str] = set()
    for item in queries:
        if not isinstance(item, str) or len(item) > 500 or not _input_is_safe(item):
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

    if not isinstance(objective, str) or len(objective) > 2000 or not _input_is_safe(objective):
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


def _safe_query_suggestions(objective: str, queries: list[str]) -> list[dict[str, str]]:
    """Offer fixed-language templates; never translate, search, or queue them."""

    existing = {item.casefold() for item in queries}
    candidates = [item for item in queries if "://" not in item]
    if "://" not in objective:
        candidates.append(objective)
    seed = ""
    for candidate in candidates:
        clean = _clean_input(candidate, limit=220)
        if _input_is_safe(clean) and len(_WORD.findall(clean)) >= 2:
            seed = clean
            break
    if not seed:
        return []
    templates = (
        ("ru", "техническая документация", "technical_sources"),
        ("ru", "опыт владельцев ремонт", "owner_experience"),
        ("en", "technical documentation", "technical_sources"),
        ("en", "owner repair experience", "owner_experience"),
    )
    suggestions: list[dict[str, str]] = []
    for language, suffix, reason in templates:
        proposed = _clean_input(f"{seed} {suffix}", limit=500)
        if proposed.casefold() in existing or not _input_is_safe(proposed):
            continue
        suggestions.append({"language": language, "query": proposed, "reason": reason})
        existing.add(proposed.casefold())
    return suggestions


def _coverage_rows(conn: sqlite3.Connection, job_id: str, column: str, key: str) -> list[dict[str, Any]]:
    # All callers use fixed column names; this helper never accepts user input.
    rows = conn.execute(
        f"""SELECT COALESCE(NULLIF({column},''),'unknown') AS value, COUNT(*) AS count
             FROM documents WHERE job_id=? AND status='fetched'
             GROUP BY value ORDER BY count DESC, value""",
        (job_id,),
    )
    return [{key: row["value"], "count": row["count"]} for row in rows]


def _provider_coverage(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT COALESCE(NULLIF(provider,''),'unreported') AS provider,
                  COUNT(*) AS queries,
                  SUM(status='done') AS done,
                  SUM(status='failed') AS failed
           FROM queries WHERE job_id=? GROUP BY provider
           ORDER BY queries DESC, provider""",
        (job_id,),
    )
    return [
        {
            "provider": row["provider"],
            "queries": row["queries"],
            "done": row["done"] or 0,
            "failed": row["failed"] or 0,
        }
        for row in rows
    ]


def research_status(job_id: str) -> dict[str, Any]:
    if not _valid_id(job_id):
        return _error("job_id_invalid")
    try:
        with _db() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return _error("job_not_found", job_id=job_id)
            counts = conn.execute(
                """SELECT COUNT(*) AS total, SUM(status='fetched') AS fetched,
                          SUM(status='failed') AS unavailable,
                          SUM(status='duplicate') AS duplicates,
                          SUM(status IN ('failed','duplicate')) AS failed
                   FROM documents WHERE job_id=?""",
                (job_id,),
            ).fetchone()
            queries = conn.execute(
                "SELECT COUNT(*) AS total, SUM(status='done') AS done, SUM(status='failed') AS failed FROM queries WHERE job_id=?",
                (job_id,),
            ).fetchone()
            failures = [
                {"reason": item["error"], "count": item["count"]}
                for item in conn.execute(
                    """SELECT error,COUNT(*) AS count FROM documents
                       WHERE job_id=? AND status='failed' GROUP BY error ORDER BY count DESC, error LIMIT 10""",
                    (job_id,),
                )
            ]
            search_failures = [
                {"reason": item["error"], "count": item["count"]}
                for item in conn.execute(
                    """SELECT error,COUNT(*) AS count FROM queries
                       WHERE job_id=? AND status='failed' GROUP BY error ORDER BY count DESC, error LIMIT 5""",
                    (job_id,),
                )
            ]
            stored_queries = [
                item["query"]
                for item in conn.execute("SELECT query FROM queries WHERE job_id=? ORDER BY id", (job_id,))
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
                # Kept for compatibility: historical callers treated duplicate
                # pages as non-usable pages. New fields make the distinction clear.
                "pages_failed": counts["failed"] or 0,
                "pages_unavailable": counts["unavailable"] or 0,
                "pages_duplicates": counts["duplicates"] or 0,
                "queries_total": queries["total"],
                "queries_done": queries["done"] or 0,
                "queries_failed": queries["failed"] or 0,
                "page_failures": failures,
                "search_failures": search_failures,
                "coverage": {
                    "languages": _coverage_rows(conn, job_id, "language", "language"),
                    "source_classes": _coverage_rows(conn, job_id, "source_class", "source_class"),
                    "extraction_methods": _coverage_rows(conn, job_id, "extraction_method", "extraction_method"),
                    "search_providers": _provider_coverage(conn, job_id),
                },
                # Suggestions do not consume budget or create queries. They are
                # capped by the remaining explicit add_queries capacity.
                "query_suggestions": _safe_query_suggestions(row["objective"], stored_queries)[
                    : max(0, MAX_QUERIES - queries["total"])
                ],
                "error": row["error"] or None,
            }
    except (OSError, sqlite3.Error):
        return _error("j1_store_unavailable", job_id=job_id)


def _fts_expression(query: str) -> str:
    tokens = re.findall(r"[^\W_]{2,}", query, flags=re.UNICODE)[:10]
    return " OR ".join('"' + token.replace('"', "") + '"' for token in tokens)


def _duplicate_metadata(row: sqlite3.Row) -> tuple[str | None, dict[str, Any] | None]:
    duplicate_of = str(row["duplicate_of"] or "")
    if not duplicate_of:
        return None, None
    return duplicate_of, {
        "document_id": duplicate_of,
        "url": row["duplicate_url"] or None,
        "kind": row["duplicate_kind"] or "unknown",
        "similarity": row["duplicate_score"] or None,
    }


def _result_item(row: sqlite3.Row) -> dict[str, Any]:
    duplicate_of, duplicate = _duplicate_metadata(row)
    return {
        "document_id": row["id"],
        "url": row["url"],
        "canonical_url": row["canonical_url"] or row["url"],
        "title": row["title"],
        "source": row["source"],
        "source_class": row["source_class"] or "unknown",
        "source_basis": row["source_basis"] or "fallback:unclassified",
        "language": row["language"] or "unknown",
        "kind": row["kind"],
        "extraction_method": row["extraction_method"] or "unknown",
        "status": row["status"],
        "error": row["error"] or None,
        "duplicate_of": duplicate_of,
        "duplicate": duplicate,
        "excerpt": row["excerpt"],
        "discovered_at": row["created_at"],
        "retrieved_at": row["retrieved_at"] or None,
    }


def research_results(job_id: str, query: str = "", cursor: int = 0, limit: int = 20) -> dict[str, Any]:
    if not _valid_id(job_id):
        return _error("job_id_invalid")
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 50:
        return _error("pagination_invalid", job_id=job_id)
    if not isinstance(query, str) or len(query) > 300 or (query.strip() and not _input_is_safe(query)):
        return _error("query_invalid_or_sensitive", job_id=job_id)
    expression = _fts_expression(query)
    if query.strip() and not expression:
        return _error("query_invalid_or_sensitive", job_id=job_id)
    fields = """d.id,d.url,d.canonical_url,d.title,d.source,d.source_class,d.source_basis,d.language,
                       d.kind,d.extraction_method,d.status,d.error,d.duplicate_of,d.duplicate_kind,d.duplicate_score,
                       d.created_at,d.retrieved_at,primary_doc.url AS duplicate_url"""
    try:
        with _db() as conn:
            if not conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
                return _error("job_not_found", job_id=job_id)
            if expression:
                total = conn.execute(
                    "SELECT COUNT(*) FROM documents_fts WHERE job_id=? AND documents_fts MATCH ?", (job_id, expression)
                ).fetchone()[0]
                rows = conn.execute(
                    f"""SELECT {fields}, snippet(documents_fts,3,'[',']','…',24) AS excerpt
                       FROM documents_fts JOIN documents AS d ON d.id=documents_fts.document_id
                       LEFT JOIN documents AS primary_doc ON primary_doc.id=d.duplicate_of
                       WHERE documents_fts.job_id=? AND documents_fts MATCH ?
                       ORDER BY bm25(documents_fts) LIMIT ? OFFSET ?""",
                    (job_id, expression, limit, cursor),
                ).fetchall()
            else:
                total = conn.execute("SELECT COUNT(*) FROM documents WHERE job_id=?", (job_id,)).fetchone()[0]
                rows = conn.execute(
                    f"""SELECT {fields}, substr(d.body,1,300) AS excerpt
                       FROM documents AS d LEFT JOIN documents AS primary_doc ON primary_doc.id=d.duplicate_of
                       WHERE d.job_id=? ORDER BY d.created_at,d.id LIMIT ? OFFSET ?""",
                    (job_id, limit, cursor),
                ).fetchall()
            items = [_result_item(row) for row in rows]
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
                """SELECT d.url,d.canonical_url,d.title,d.kind,d.body,d.source,d.source_class,d.source_basis,d.language,
                          d.extraction_method,d.status,d.error,d.duplicate_of,d.duplicate_kind,d.duplicate_score,
                          d.created_at,d.retrieved_at,primary_doc.url AS duplicate_url
                   FROM documents AS d LEFT JOIN documents AS primary_doc ON primary_doc.id=d.duplicate_of
                   WHERE d.job_id=? AND d.id=?""",
                (job_id, document_id),
            ).fetchone()
            if row is None:
                return _error("document_not_found", job_id=job_id)
            duplicate_of, duplicate = _duplicate_metadata(row)
            if row["status"] != "fetched":
                result = _error("document_not_found", job_id=job_id)
                result.update(
                    {
                        "document_id": document_id,
                        "status": row["status"],
                        "error_detail": row["error"] or None,
                        "duplicate_of": duplicate_of,
                        "duplicate": duplicate,
                    }
                )
                return result
            body = row["body"]
            end = min(offset + max_chars, len(body))
            return {
                "ok": True,
                "schema": SCHEMA,
                "job_id": job_id,
                "document_id": document_id,
                "url": row["url"],
                "canonical_url": row["canonical_url"] or row["url"],
                "title": row["title"],
                "kind": row["kind"],
                "source": row["source"],
                "source_class": row["source_class"] or "unknown",
                "source_basis": row["source_basis"] or "fallback:unclassified",
                "language": row["language"] or "unknown",
                "extraction_method": row["extraction_method"] or "unknown",
                "duplicate_of": duplicate_of,
                "duplicate": duplicate,
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
                    canonical_url = _canonical_url(url)
                    if not url or not canonical_url:
                        continue
                    # Preserve the original result URL for traceability while
                    # avoiding a second fetch of its tracking-only variant.
                    if conn.execute(
                        """SELECT 1 FROM documents WHERE job_id=?
                           AND (canonical_url=? OR (canonical_url='' AND url=?)) LIMIT 1""",
                        (job_id, canonical_url, url),
                    ).fetchone():
                        continue
                    inserted = conn.execute(
                        """INSERT OR IGNORE INTO documents(
                               id,job_id,url,canonical_url,title,source,created_at
                           ) VALUES(?,?,?,?,?,?,?)""",
                        (
                            uuid4().hex,
                            job_id,
                            url,
                            canonical_url,
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
                    title = redact_sensitive(fetch_result.get("title", ""), limit=200)
                    kind = _clean_input(str(fetch_result.get("kind") or ""), limit=40)
                    fetched_url = public_url(fetch_result.get("url", "")) or url
                    canonical_url = _canonical_url(fetched_url) or _canonical_url(url)
                    # A redirect may resolve to a URL already queued by search.
                    # Keep this row's original source link in that rare case; the
                    # canonical URL still links content-level duplicates below.
                    url_conflict = conn.execute(
                        "SELECT 1 FROM documents WHERE job_id=? AND url=? AND id<>? LIMIT 1",
                        (job_id, fetched_url, doc_id),
                    ).fetchone()
                    stored_url = url if url_conflict else fetched_url
                    source_class, source_basis = _classify_source(canonical_url or stored_url, title, kind)
                    language = _detect_language(title, body)
                    extraction_method = _extraction_method(fetch_result)
                    digest = hashlib.sha256(body.encode()).hexdigest()
                    similarity_hash = _simhash(body)
                    duplicate = _find_duplicate(conn, job_id=job_id, digest=digest, similarity_hash=similarity_hash)
                    retrieved_at = _utcnow()
                    if duplicate is not None:
                        primary, duplicate_kind, duplicate_score = duplicate
                        conn.execute(
                            """UPDATE documents SET status='duplicate',url=?,canonical_url=?,title=?,kind=?,body='',
                               content_hash=?,content_simhash=?,language=?,extraction_method=?,source_class=?,source_basis=?,
                               duplicate_of=?,duplicate_kind=?,duplicate_score=?,error=?,retrieved_at=? WHERE id=?""",
                            (
                                stored_url,
                                canonical_url,
                                title,
                                kind,
                                digest,
                                similarity_hash,
                                language,
                                extraction_method,
                                source_class,
                                source_basis,
                                primary["id"],
                                duplicate_kind,
                                duplicate_score,
                                "duplicate_content" if duplicate_kind == "exact" else "duplicate_near_content",
                                retrieved_at,
                                doc_id,
                            ),
                        )
                    else:
                        # FTS repeats the text; reserve enough room only for a
                        # genuinely new document. Duplicate links use no corpus
                        # body and therefore remain useful at capacity.
                        if _cache_size() + len(body.encode("utf-8")) * 3 > MAX_CORPUS_BYTES:
                            _prune(conn)
                        if _cache_size() + len(body.encode("utf-8")) * 3 > MAX_CORPUS_BYTES:
                            conn.execute(
                                "UPDATE documents SET status='failed',error='cache_capacity_reached' WHERE id=?",
                                (doc_id,),
                            )
                            conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (_utcnow(), job_id))
                            conn.commit()
                            continue
                        conn.execute(
                            """UPDATE documents SET status='fetched',url=?,canonical_url=?,title=?,kind=?,body=?,
                               content_hash=?,content_simhash=?,language=?,extraction_method=?,source_class=?,source_basis=?,
                               duplicate_of='',duplicate_kind='',duplicate_score=0,error='',retrieved_at=? WHERE id=?""",
                            (
                                stored_url,
                                canonical_url,
                                title,
                                kind,
                                body,
                                digest,
                                similarity_hash,
                                language,
                                extraction_method,
                                source_class,
                                source_basis,
                                retrieved_at,
                                doc_id,
                            ),
                        )
                        conn.execute("DELETE FROM documents_fts WHERE document_id=?", (doc_id,))
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
