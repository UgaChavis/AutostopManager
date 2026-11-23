"""J1 public research queue and temporary searchable corpus.

The native Manager MCP tools call the small synchronous API below. A separate
hardened worker executes public searches and fetches. Only sanitized text
is retained, for at most seven days; this is not Manager business memory.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
import fcntl
from functools import partial
import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import sys
import time
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from . import j1_identifiers
from .j1_fetch import (
    MAX_PUBLIC_FRAGMENTS,
    MAX_PUBLIC_PDF_TEXT_CHARS,
    MAX_SEARCH_RESULTS,
    MAX_SEARCH_SNIPPET_CHARS,
    MIXED_OCR_PAGES_PER_DOCUMENT,
    _bounded_public_text,
    _pdf_fragments,
    complete_capped_text_end,
    complete_pdf_fragment_end,
    contains_sensitive,
    contains_unsafe_url,
    fetch_document,
    mixed_ocr_budget,
    parse_searxng_endpoint,
    public_url,
    quote_http_target,
    redact_sensitive,
    search_public,
)
from .config import reject_obsolete_runtime_config
from .j1_applicability import CLAUSE_BREAK, Passage, assess_applicability
from .j1_sources import classify_source, publisher_domain

SCHEMA = "autostop.j1.research.v1"
REPORT_SCHEMA = "autostop.j1.report.v1"
MAX_QUERIES = 30
MAX_PAGES = 300
SOURCE_URL_BATCH_LIMIT = 20
SOURCES_ONLY_MODE = "sources_only"
MAX_BROWSER_PAGES = 20
MAX_MIXED_OCR_PAGES_PER_JOB = 8
MAX_DOCUMENT_CHARS = 50_000
MAX_CORPUS_BYTES = 250 * 1024 * 1024
RETENTION_DAYS = 7
_IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
_SOURCE_START_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_PERCENT_OCTET = re.compile(r"%[0-9a-fA-F]{2}")
_URL_TOKEN = re.compile(r"(?i)(?<!\w)[a-z][a-z0-9+.-]*://[^\s<>]+")
_WORD = re.compile(r"[^\W_]{3,}", flags=re.UNICODE)
_STOP = False
_WORKER_FAILED = "worker_failed"
_PAGE_BUDGET_REACHED = "page_budget_reached"
_SEARCH_INTERRUPTED = "search_interrupted"

# The automotive profile remains a small extension of the generic public-web
# corpus.  It is deliberately bounded more tightly because its automatic plan
# is intended to be reviewed as one compact evidence bundle.
GENERAL_PROFILE = "general"
AUTOMOTIVE_PROFILE = "automotive"
AUTOMOTIVE_MAX_QUERIES = 12
AUTOMOTIVE_MAX_PAGES = 60
AUTOMOTIVE_MAX_MANUAL_QUERIES = 4
AUTOMOTIVE_INITIAL_QUERIES = 5
_AUTOMOTIVE_CONTEXT_LIMITS = {
    "make": 80,
    "model": 100,
    "year": 4,
    "engine": 100,
    "market": 80,
    "production_date": 10,
    "system": 120,
    "symptom": 500,
    "dtc": 120,
    "part_number": 100,
    "position": 80,
}
_CAUSE_MARKERS = (
    "caused by",
    "due to",
    "root cause",
    "because of",
    "attributed to",
    "причин",
    "вызван",
    "обусловлен",
    "из-за",
    "вследствие",
)
_MAX_CAUSE_MENTIONS = 3
_FREQUENCY_COUNT = re.compile(
    r"\b(?P<n>\d{1,6})\s*(?:out\s+of|of|/|из)\s*(?P<d>\d{1,6})\s+"
    r"(?P<unit>vehicles?|cars?|units?|cases?|автомобил\w*|машин\w*|случа\w*)\b",
    re.IGNORECASE,
)
_FREQUENCY_PERCENT = re.compile(
    r"\b(?P<percent>\d{1,3}(?:[.,]\d+)?)\s*%\s+(?:of|among|из|среди)\s+"
    r"(?P<unit>vehicles?|cars?|units?|cases?|автомобил\w*|машин\w*|случа\w*)\b",
    re.IGNORECASE,
)
_FREQUENCY_DEFECT_MARKERS = (
    "fault",
    "failure",
    "defect",
    "malfunction",
    "leak",
    "misfire",
    "неисправ",
    "дефект",
    "пропуск",
    "утеч",
    "отказ",
    "полом",
)

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


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _retention_cutoff(now: datetime) -> str:
    return (now - timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds")


def _cache_dir() -> Path:
    reject_obsolete_runtime_config()

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


def _enable_wal(connection: sqlite3.Connection) -> None:
    for attempt in range(3):
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as error:
            if attempt == 2 or error.sqlite_errorcode != sqlite3.SQLITE_BUSY:
                raise
            # Two first opens can race to change a new database's journal mode.
            time.sleep(0.02 * (attempt + 1))


@contextmanager
def _db(*, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    path = _db_path()
    if readonly:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(path, timeout=5)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        if not readonly:
            _enable_wal(connection)
            connection.execute("PRAGMA foreign_keys=ON")
            _init_schema(connection)
            # Backfill writes must finish before callers start their transaction.
            connection.commit()
        yield connection
    finally:
        connection.close()


def _canonical_url(value: str, *, supplied: bool = False) -> str:
    """Return a stable public-document key without changing meaningful parameters."""

    try:
        raw = str(value or "").strip()
        raw.encode("utf-8")
        parsed = urlsplit(raw)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not host:
        return ""
    path = parsed.path or "/"
    hostname = host.casefold().rstrip(".")
    if supplied:
        try:
            hostname = host.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError:
            return ""
        # Preserve the actual path bytes, including encoded reserved characters.
        path = _PERCENT_OCTET.sub(lambda match: match[0].upper(), quote_http_target(path))
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and not (
        (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)
    ):
        netloc += f":{port}"
    try:
        # Match discovery identity without replacing distinct legacy query bytes.
        parameters = parse_qsl(parsed.query, keep_blank_values=True, errors="surrogateescape")
    except ValueError:
        return ""
    kept = [
        (name, item)
        for name, item in parameters
        if not (name.casefold().startswith("utm_") or name.casefold() in _TRACKING_PARAMETERS)
    ]
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            netloc,
            path,
            urlencode(kept, doseq=True, errors="surrogateescape"),
            "",
        )
    )


def _classify_source(url: str, title: str, kind: str) -> tuple[str, str, str]:
    """Use the shared, auditable discovery registry after redirects too."""

    classification = classify_source(url, title=title, kind=kind)
    return classification.source_class, classification.source_basis, classification.source_tier


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

    # Bodies are already bounded to MAX_DOCUMENT_CHARS. A prefix fingerprint
    # can equate unrelated articles that share long navigation/boilerplate.
    tokens = _WORD.findall(str(value or "").casefold())
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


def _add_missing_columns(conn: sqlite3.Connection, table: str, required: dict[str, str]) -> None:
    present = _schema_columns(conn, table)
    if required.keys() <= present:
        return
    if not conn.in_transaction:
        # Recheck after acquiring the write lock: a parallel open may have
        # completed the same upgrade while this connection was waiting.
        conn.execute("BEGIN IMMEDIATE")
        present = _schema_columns(conn, table)
    for name, definition in required.items():
        if name not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _migrate_documents(conn: sqlite3.Connection) -> None:
    required = {
        "canonical_url": "TEXT NOT NULL DEFAULT ''",
        "requested_canonical_url": "TEXT NOT NULL DEFAULT ''",
        "language": "TEXT NOT NULL DEFAULT 'unknown'",
        "extraction_method": "TEXT NOT NULL DEFAULT ''",
        "browser_attempted": "INTEGER NOT NULL DEFAULT 0",
        "mixed_ocr_pages_used": "INTEGER NOT NULL DEFAULT 0",
        "pdf_limitations": "TEXT NOT NULL DEFAULT ''",
        "body_truncated": "INTEGER NOT NULL DEFAULT 0",
        "source_class": "TEXT NOT NULL DEFAULT 'unknown'",
        "source_basis": "TEXT NOT NULL DEFAULT ''",
        # Discovery fields survive an unavailable page so a report can state
        # which evidence tiers were found but could not be read.
        "source_tier": "TEXT NOT NULL DEFAULT 'unclassified'",
        "search_snippet": "TEXT NOT NULL DEFAULT ''",
        "search_rank": "INTEGER NOT NULL DEFAULT 0",
        "search_engines": "TEXT NOT NULL DEFAULT ''",
        "duplicate_of": "TEXT NOT NULL DEFAULT ''",
        "duplicate_kind": "TEXT NOT NULL DEFAULT ''",
        "duplicate_score": "REAL NOT NULL DEFAULT 0",
        "content_simhash": "TEXT NOT NULL DEFAULT ''",
    }
    _add_missing_columns(conn, "documents", required)
    conn.execute("CREATE INDEX IF NOT EXISTS documents_job_canonical ON documents(job_id, canonical_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS documents_duplicate_of ON documents(duplicate_of)")
    conn.execute("CREATE INDEX IF NOT EXISTS documents_job_search_rank ON documents(job_id, search_rank)")
    # Existing temporary jobs keep their records. Canonical backfill is cheap
    # and does not read the network or alter the original source URL.
    for row in conn.execute("SELECT id,url FROM documents WHERE canonical_url='' LIMIT 1000"):
        canonical = _canonical_url(row["url"])
        if canonical:
            conn.execute("UPDATE documents SET canonical_url=? WHERE id=?", (canonical, row["id"]))


def _migrate_jobs(conn: sqlite3.Connection) -> None:
    """Add profile and acquisition metadata without changing retained jobs."""

    required = {
        "profile": "TEXT NOT NULL DEFAULT 'general'",
        "automotive_context": "TEXT NOT NULL DEFAULT ''",
        "acquisition_mode": "TEXT NOT NULL DEFAULT 'queries'",
        "mixed_ocr_pages_used": "INTEGER NOT NULL DEFAULT 0",
    }
    _add_missing_columns(conn, "jobs", required)


def _migrate_queries(conn: sqlite3.Connection) -> None:
    _add_missing_columns(
        conn,
        "queries",
        {
            "intent": "TEXT NOT NULL DEFAULT 'manual'",
            "primary_admitted": "INTEGER NOT NULL DEFAULT 0",
        },
    )


_INDEX_DUPLICATE_TITLES = """
    INSERT INTO documents_fts(job_id,document_id,title,body)
    SELECT d.job_id,d.id,d.title,'' FROM documents AS d
    JOIN documents AS p ON p.id=d.duplicate_of AND p.job_id=d.job_id
    WHERE d.status='duplicate' AND d.duplicate_kind='exact' AND d.title<>'' AND d.title<>p.title
"""


def _migrate_duplicate_titles(conn: sqlite3.Connection) -> None:
    """Index retained exact copies' alternative titles once, with no repeated bodies."""
    if conn.execute("PRAGMA user_version").fetchone()[0] >= 1:
        return
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("PRAGMA user_version").fetchone()[0] >= 1:
            return
    conn.execute(_INDEX_DUPLICATE_TITLES)
    conn.execute("PRAGMA user_version=1")


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, objective TEXT NOT NULL, max_pages INTEGER NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '', profile TEXT NOT NULL DEFAULT 'general',
            automotive_context TEXT NOT NULL DEFAULT '', acquisition_mode TEXT NOT NULL DEFAULT 'queries',
            mixed_ocr_pages_used INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at);
        CREATE TABLE IF NOT EXISTS supplied_source_starts (
            key_hash TEXT PRIMARY KEY, request_hash TEXT NOT NULL, job_id TEXT NOT NULL UNIQUE,
            FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
            query TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            provider TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', intent TEXT NOT NULL DEFAULT 'manual',
            primary_admitted INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE,
            UNIQUE(job_id, query)
        );
        CREATE INDEX IF NOT EXISTS queries_pending ON queries(job_id, status, id);
        CREATE TABLE IF NOT EXISTS query_candidates (
            query_id INTEGER NOT NULL, url TEXT NOT NULL, canonical_url TEXT NOT NULL,
            title TEXT NOT NULL, source TEXT NOT NULL,
            source_class TEXT NOT NULL, source_basis TEXT NOT NULL, source_tier TEXT NOT NULL,
            search_snippet TEXT NOT NULL, search_rank INTEGER NOT NULL, search_engines TEXT NOT NULL,
            rank INTEGER NOT NULL,
            PRIMARY KEY(query_id, canonical_url),
            FOREIGN KEY(query_id) REFERENCES queries(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL, url TEXT NOT NULL,
            canonical_url TEXT NOT NULL DEFAULT '', requested_canonical_url TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
            body TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL DEFAULT '', content_simhash TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT 'unknown', extraction_method TEXT NOT NULL DEFAULT '',
            browser_attempted INTEGER NOT NULL DEFAULT 0,
            mixed_ocr_pages_used INTEGER NOT NULL DEFAULT 0,
            pdf_limitations TEXT NOT NULL DEFAULT '',
            body_truncated INTEGER NOT NULL DEFAULT 0,
            source_class TEXT NOT NULL DEFAULT 'unknown', source_basis TEXT NOT NULL DEFAULT '',
            source_tier TEXT NOT NULL DEFAULT 'unclassified', search_snippet TEXT NOT NULL DEFAULT '',
            search_rank INTEGER NOT NULL DEFAULT 0, search_engines TEXT NOT NULL DEFAULT '',
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
        CREATE TABLE IF NOT EXISTS document_fragments (
            fts_rowid INTEGER PRIMARY KEY, document_id TEXT NOT NULL, page INTEGER NOT NULL,
            fragment_kind TEXT NOT NULL, section TEXT NOT NULL, source_url TEXT NOT NULL,
            start INTEGER NOT NULL, end INTEGER NOT NULL,
            extraction_method TEXT NOT NULL DEFAULT '',
            truncated INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE,
            UNIQUE(document_id, page)
        );
        """
    )
    _migrate_jobs(conn)
    _migrate_queries(conn)
    _migrate_documents(conn)
    _add_missing_columns(
        conn,
        "document_fragments",
        {"extraction_method": "TEXT NOT NULL DEFAULT ''", "truncated": "INTEGER NOT NULL DEFAULT 0"},
    )
    _migrate_duplicate_titles(conn)
    j1_identifiers.init_schema(conn)
    _add_missing_columns(conn, "identifier_queries", {"matcher_version": "INTEGER NOT NULL DEFAULT 0"})


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
    try:
        raw.encode("utf-8")
    except UnicodeError:
        return False
    normalized = " ".join(raw.split())
    return _safe_public_input(raw) and (normalized == raw or _safe_public_input(normalized))


def _safe_public_input(raw: str) -> bool:
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


def _validated_automotive_context(value: object) -> tuple[dict[str, str] | None, str | None]:
    """Accept only de-identified technical fields used to form public queries."""

    if not isinstance(value, dict) or any(key not in _AUTOMOTIVE_CONTEXT_LIMITS for key in value):
        return None, "automotive_context_invalid"
    context: dict[str, str] = {}
    for key, raw in value.items():
        if raw is None:
            continue
        if key == "year":
            if isinstance(raw, bool) or not isinstance(raw, (int, str)):
                return None, "automotive_context_invalid"
            year = str(raw).strip()
            if not re.fullmatch(r"\d{4}", year) or not 1886 <= int(year) <= 2100:
                return None, "automotive_context_invalid"
            context[key] = year
            continue
        if not isinstance(raw, str):
            return None, "automotive_context_invalid"
        if not raw.strip():
            continue
        if not _input_is_safe(raw):
            return None, "automotive_context_invalid_or_sensitive"
        if key == "production_date":
            value = raw.strip()
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                return None, "automotive_context_invalid"
            try:
                context[key] = date.fromisoformat(value).isoformat()
            except ValueError:
                return None, "automotive_context_invalid"
            continue
        limit = _AUTOMOTIVE_CONTEXT_LIMITS[key]
        # A prefix at the context limit can be another valid identifier.
        cleaned = _clean_input(raw, limit=limit + (key in {"dtc", "part_number"}))
        if len(cleaned) > limit:
            return None, "automotive_context_invalid"
        if not _input_is_safe(cleaned):
            return None, "automotive_context_invalid_or_sensitive"
        if key == "dtc":
            cleaned = re.sub(r"\s*([-‐‑‒–—−])\s*", r"\1", cleaned)
            codes = [j1_identifiers.normalize(item, "dtc") for item in re.split(r"[,;/\s]+", cleaned) if item]
            if not codes or len(codes) > 8 or any(code is None for code in codes):
                return None, "automotive_context_invalid"
            cleaned = ", ".join(code for code in codes if code is not None)
        context[key] = cleaned
    if not context:
        return None, "automotive_context_invalid"
    has_identity = any(context.get(field) for field in ("make", "model", "engine", "part_number"))
    has_subject = any(context.get(field) for field in ("system", "symptom", "dtc", "part_number"))
    if not has_identity or not has_subject:
        return None, "automotive_context_incomplete"
    return context, None


def _context_phrase(context: dict[str, str], fields: tuple[str, ...], *, limit: int) -> str:
    values: list[str] = []
    seen: set[str] = set()
    for field in fields:
        value = context.get(field, "")
        if value and value.casefold() not in seen:
            # Keep identifiers, build date and position distinct in the search text.
            values.append(f"{value}," if field in {"dtc", "part_number", "production_date", "position"} else value)
            seen.add(value.casefold())
    phrase = " ".join(values)
    if len(phrase) > limit:
        phrase = phrase[: limit + 1].rsplit(" ", 1)[0]
    return phrase.rstrip(",")


def _automotive_query_plan(context: dict[str, str]) -> list[tuple[str, str]]:
    """Generate a small first pass for the evidence the task needs."""

    search_context = {**context, "symptom": _clean_input(context.get("symptom", ""), limit=100)}
    identity = ("market", "position", "make", "model", "year", "engine")
    catalog = ("part_number", *identity, "system")
    catalog_dated = ("part_number", "production_date", *identity, "system")
    templates: tuple[tuple[str, tuple[str, ...], str], ...]
    if context.get("part_number") and not (context.get("dtc") or context.get("symptom")):
        templates = (
            ("catalog_fitment_en", catalog, "OEM parts catalog fitment"),
            ("technical_context_en", catalog, "component manufacturer specifications"),
            ("catalog_fitment_ru", catalog_dated, "каталог оригинальных деталей применимость"),
            ("technical_context_ru", catalog, "техническая документация производителя детали"),
        )
    else:
        diagnosis = ("dtc", *identity, "system", "symptom")
        diagnosis_dated = ("dtc", "production_date", *identity, "system", "symptom")
        templates = (
            ("official_bulletin_en", diagnosis, "OEM technical service bulletin"),
            ("technical_diagnosis_en", diagnosis, "diagnostic procedure service manual"),
            ("technical_diagnosis_ru", diagnosis_dated, "руководство по ремонту диагностика"),
            ("catalog_fitment_en", catalog, "OEM parts catalog fitment")
            if context.get("part_number")
            else ("official_bulletin_ru", diagnosis, "бюллетень производителя"),
        )
    planned: list[tuple[str, str]] = []
    seen: set[str] = set()
    for intent, fields, suffix in templates:
        phrase = _context_phrase(search_context, fields, limit=500 - len(suffix) - 1)
        query = f"{phrase} {suffix}".strip()
        normalized = query.casefold()
        if query and _input_is_safe(query) and normalized not in seen:
            planned.append((query, intent))
            seen.add(normalized)
    return planned


def _profile_and_queries(
    *,
    queries: list[str] | None,
    automotive_context: object,
    profile: str,
    max_pages: int,
) -> tuple[str, dict[str, str], list[tuple[str, str]], int] | None:
    if not isinstance(profile, str) or profile not in {GENERAL_PROFILE, AUTOMOTIVE_PROFILE}:
        return None
    use_automotive = automotive_context is not None or profile == AUTOMOTIVE_PROFILE
    if not use_automotive:
        if automotive_context is not None:
            return None
        manual = _validated_queries(queries if queries is not None else [])
        if manual is None:
            return None
        return GENERAL_PROFILE, {}, [(query, "manual") for query in manual], max_pages
    context, context_error = _validated_automotive_context(automotive_context)
    if context is None or context_error:
        return None
    manual = _validated_queries(queries if queries is not None else [], allow_empty=True)
    if manual is None or len(manual) > AUTOMOTIVE_MAX_MANUAL_QUERIES:
        return None
    planned: list[tuple[str, str]] = [(query, "manual") for query in manual]
    seen = {query.casefold() for query, _intent in planned}
    for query, intent in _automotive_query_plan(context):
        if len(planned) >= AUTOMOTIVE_INITIAL_QUERIES:
            break
        if query.casefold() not in seen:
            planned.append((query, intent))
            seen.add(query.casefold())
    if not planned:
        return None
    return AUTOMOTIVE_PROFILE, context, planned, min(max_pages, AUTOMOTIVE_MAX_PAGES)


def start_research(
    objective: str,
    queries: list[str] | None = None,
    max_pages: int = MAX_PAGES,
    automotive_context: dict[str, Any] | None = None,
    profile: str = GENERAL_PROFILE,
) -> dict[str, Any]:
    """Queue one de-identified, read-only public research job."""

    if not isinstance(objective, str) or len(objective) > 2000 or not _input_is_safe(objective):
        return _error("objective_invalid_or_sensitive")
    clean_objective = _clean_input(objective, limit=2000)
    if not clean_objective:
        return _error("queries_or_objective_invalid")
    if type(max_pages) is not int or not 1 <= max_pages <= MAX_PAGES:
        return _error("max_pages_invalid")
    configured = _profile_and_queries(
        queries=queries,
        automotive_context=automotive_context,
        profile=profile,
        max_pages=max_pages,
    )
    if configured is None:
        if automotive_context is None and profile == GENERAL_PROFILE:
            return _error("queries_or_objective_invalid")
        return _error("queries_or_automotive_context_invalid")
    selected_profile, clean_context, planned_queries, effective_max_pages = configured
    job_id = uuid4().hex
    now = _utcnow()
    try:
        with _db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1").fetchone():
                return _error("j1_busy")
            conn.execute(
                """INSERT INTO jobs(id,objective,max_pages,status,created_at,updated_at,profile,automotive_context)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    clean_objective,
                    effective_max_pages,
                    "queued",
                    now,
                    now,
                    selected_profile,
                    json.dumps(clean_context, ensure_ascii=False, sort_keys=True) if clean_context else "",
                ),
            )
            conn.executemany(
                "INSERT INTO queries(job_id,query,intent) VALUES(?,?,?)",
                ((job_id, query, intent) for query, intent in planned_queries),
            )
            conn.commit()
    except (OSError, sqlite3.Error):
        return _error("j1_store_unavailable")
    return {
        "ok": True,
        "schema": SCHEMA,
        "job_id": job_id,
        "status": "queued",
        "profile": selected_profile,
        "max_pages": effective_max_pages,
        "query_count": len(planned_queries),
        "automotive_context": clean_context or None,
    }


def _validated_source_urls(source_urls: list[str]) -> dict[str, str] | None:
    if not isinstance(source_urls, list) or not 1 <= len(source_urls) <= SOURCE_URL_BATCH_LIMIT:
        return None
    urls: dict[str, str] = {}
    for value in source_urls:
        url = public_url(value) if isinstance(value, str) else ""
        canonical_url = _canonical_url(url, supplied=True)
        if not canonical_url:
            return None
        urls.setdefault(canonical_url, url)
    return urls


def _queue_supplied_sources(conn: sqlite3.Connection, job_id: str, urls: dict[str, str], now: str) -> None:
    rows = []
    for canonical_url, url in urls.items():
        source = classify_source(canonical_url)
        rows.append(
            (
                uuid4().hex,
                job_id,
                url,
                canonical_url,
                canonical_url,
                source.source_class,
                source.source_basis,
                source.source_tier,
                now,
            )
        )
    conn.executemany(
        """INSERT INTO documents(id,job_id,url,canonical_url,requested_canonical_url,source,
                                 source_class,source_basis,source_tier,created_at)
           VALUES(?,?,?,?,?,'supplied_url',?,?,?,?)""",
        rows,
    )


def _source_job_result(
    job_id: str, status: str, *, added: int, sources_total: int, max_pages: int, replayed: bool = False
) -> dict[str, Any]:
    return {
        "ok": True,
        "schema": SCHEMA,
        "job_id": job_id,
        "status": status,
        "acquisition_mode": SOURCES_ONLY_MODE,
        "added": added,
        "sources_total": sources_total,
        "max_pages": max_pages,
        "replayed": replayed,
    }


def _start_source_job(
    conn: sqlite3.Connection,
    *,
    objective: str,
    automotive_context: dict[str, str],
    urls: dict[str, str],
    key_hash: str,
    request_hash: str,
    now: str,
    cutoff: str,
) -> dict[str, Any]:
    receipt = conn.execute(
        """SELECT j.id,j.status,j.created_at,j.max_pages,s.request_hash
           FROM supplied_source_starts s JOIN jobs j ON j.id=s.job_id WHERE s.key_hash=?""",
        (key_hash,),
    ).fetchone()
    if receipt is not None:
        if receipt["request_hash"] != request_hash:
            return _error("idempotency_conflict")
        if receipt["created_at"] < cutoff:
            return _error("job_expired", job_id=receipt["id"])
        count = conn.execute("SELECT COUNT(*) FROM documents WHERE job_id=?", (receipt["id"],)).fetchone()[0]
        return _source_job_result(
            receipt["id"],
            receipt["status"],
            added=0,
            sources_total=count,
            max_pages=receipt["max_pages"],
            replayed=True,
        )
    if conn.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1").fetchone():
        return _error("j1_busy")
    job_id = uuid4().hex
    conn.execute(
        """INSERT INTO jobs(id,objective,max_pages,status,created_at,updated_at,acquisition_mode,profile,automotive_context)
           VALUES(?,?,?,'queued',?,?,?,?,?)""",
        (
            job_id,
            objective,
            MAX_PAGES,
            now,
            now,
            SOURCES_ONLY_MODE,
            AUTOMOTIVE_PROFILE if automotive_context else GENERAL_PROFILE,
            json.dumps(automotive_context, ensure_ascii=False, sort_keys=True) if automotive_context else "",
        ),
    )
    _queue_supplied_sources(conn, job_id, urls, now)
    conn.execute(
        "INSERT INTO supplied_source_starts(key_hash,request_hash,job_id) VALUES(?,?,?)",
        (key_hash, request_hash, job_id),
    )
    conn.commit()
    return _source_job_result(job_id, "queued", added=len(urls), sources_total=len(urls), max_pages=MAX_PAGES)


def _add_supplied_sources(
    conn: sqlite3.Connection, job_id: str, urls: dict[str, str], *, now: str, cutoff: str
) -> dict[str, Any]:
    job = conn.execute("SELECT status,max_pages,created_at,acquisition_mode FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        return _error("job_not_found", job_id=job_id)
    if job["acquisition_mode"] != SOURCES_ONLY_MODE:
        return _error("job_not_sources_only", job_id=job_id)
    if job["created_at"] < cutoff:
        return _error("job_expired", job_id=job_id)
    status = job["status"]
    if status not in {"queued", "running", "completed"}:
        return _error("job_not_extendable", job_id=job_id)

    existing = conn.execute(
        "SELECT requested_canonical_url,canonical_url FROM documents WHERE job_id=?", (job_id,)
    ).fetchall()
    # Fetch can replace the current URL after a redirect. Both identities stay
    # useful across later batches while the original still consumes one slot.
    # Retained supplied jobs may have stored these keys in their original IRI spelling.
    known = {_canonical_url(key, supplied=True) for row in existing for key in row}
    additions = {key: url for key, url in urls.items() if key not in known}
    max_pages = job["max_pages"]
    total = len(existing) + len(additions)
    if total > max_pages:
        return _error("source_limit_reached", job_id=job_id)
    if additions:
        if status == "completed":
            if conn.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1").fetchone():
                return _error("j1_busy", job_id=job_id)
            status = "queued"
        _queue_supplied_sources(conn, job_id, additions, now)
        conn.execute("UPDATE jobs SET status=?,updated_at=? WHERE id=?", (status, now, job_id))
        conn.commit()
    return _source_job_result(job_id, status, added=len(additions), sources_total=total, max_pages=max_pages)


def research_sources(
    *,
    source_urls: list[str],
    objective: str | None = None,
    job_id: str | None = None,
    idempotency_key: str | None = None,
    automotive_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start or extend a research job that reads only explicitly supplied public URLs."""

    urls = _validated_source_urls(source_urls)
    if urls is None:
        return _error("source_urls_invalid_or_sensitive")
    if job_id is not None:
        if not isinstance(job_id, str) or not _valid_id(job_id):
            return _error("job_id_invalid")
        if objective is not None or idempotency_key is not None or automotive_context is not None:
            return _error("source_target_invalid")
    else:
        if not isinstance(objective, str) or len(objective) > 2000 or not _input_is_safe(objective):
            return _error("objective_invalid_or_sensitive")
        if (
            not isinstance(idempotency_key, str)
            or not _SOURCE_START_KEY.fullmatch(idempotency_key)
            or contains_sensitive(idempotency_key)
        ):
            return _error("idempotency_key_invalid")
        clean_objective = _clean_input(objective, limit=2000)
        clean_context: dict[str, str] = {}
        if automotive_context is not None:
            validated_context, context_error = _validated_automotive_context(automotive_context)
            if validated_context is None:
                return _error(context_error or "automotive_context_invalid")
            clean_context = validated_context
        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
        request: list[Any] = [objective, source_urls]
        if clean_context:
            request.append(clean_context)
        body = json.dumps(request, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        request_hash = hashlib.sha256(body.encode()).hexdigest()

    now = datetime.now(UTC)
    cutoff = _retention_cutoff(now)
    timestamp = now.isoformat(timespec="seconds")
    try:
        with _db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if job_id is not None:
                return _add_supplied_sources(conn, job_id, urls, now=timestamp, cutoff=cutoff)
            return _start_source_job(
                conn,
                objective=clean_objective,
                automotive_context=clean_context,
                urls=urls,
                key_hash=key_hash,
                request_hash=request_hash,
                now=timestamp,
                cutoff=cutoff,
            )
    except (OSError, sqlite3.Error):
        return _error("j1_store_unavailable", job_id=job_id or "")


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


def _stored_automotive_context(value: object) -> dict[str, str]:
    """Read retained technical context defensively; never surface malformed data."""

    if not isinstance(value, str) or not value:
        return {}
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    context, error = _validated_automotive_context(loaded)
    return context if context is not None and error is None else {}


def _coverage_rows(
    conn: sqlite3.Connection, job_id: str, column: str, key: str, *, fetched_only: bool = True
) -> list[dict[str, Any]]:
    """Count fixed document columns without accepting caller-controlled SQL.

    Discovery coverage deliberately includes inaccessible pages: their source
    tier is part of the evidence gap, even when a public page could not be
    fetched.
    """

    where = "job_id=? AND status='fetched'" if fetched_only else "job_id=?"
    rows = conn.execute(
        f"""SELECT COALESCE(NULLIF({column},''),'unknown') AS value, COUNT(*) AS count
             FROM documents WHERE {where}
             GROUP BY value ORDER BY count DESC, value""",
        (job_id,),
    )
    return [{key: row["value"], "count": row["count"]} for row in rows]


def _provider_coverage(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT COALESCE(NULLIF(provider,''),'unreported') AS provider,
                  COUNT(*) AS queries,
                  SUM(status='done') AS done,
                  SUM(status='failed') AS failed,
                  SUM(status='partial') AS partial
           FROM queries WHERE job_id=? AND status<>'skipped' GROUP BY provider
           ORDER BY queries DESC, provider""",
        (job_id,),
    )
    return [
        {
            "provider": row["provider"],
            "queries": row["queries"],
            "done": row["done"] or 0,
            "failed": row["failed"] or 0,
            **({"partial": row["partial"]} if row["partial"] else {}),
        }
        for row in rows
    ]


def _document_count(conn: sqlite3.Connection, job_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM documents WHERE job_id=?", (job_id,)).fetchone()[0]


def _close_queries_at_page_limit(conn: sqlite3.Connection, job_id: str) -> int:
    partial = conn.execute(
        """UPDATE queries SET status='partial',error=?
           WHERE job_id=? AND status IN ('pending','running') AND (error=? OR primary_admitted=1)""",
        (_SEARCH_INTERRUPTED, job_id, _SEARCH_INTERRUPTED),
    ).rowcount
    skipped = conn.execute(
        "UPDATE queries SET status='skipped',error=? WHERE job_id=? AND status IN ('pending','running')",
        (_PAGE_BUDGET_REACHED, job_id),
    ).rowcount
    return partial + skipped


def _search_failures(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    return [
        {"reason": row["error"], "count": row["count"]}
        for row in conn.execute(
            """SELECT error,COUNT(*) AS count FROM queries
               WHERE job_id=? AND (status='failed' OR (status='partial' AND error<>?))
               GROUP BY error ORDER BY count DESC, error LIMIT 5""",
            (job_id, _SEARCH_INTERRUPTED),
        )
    ]


def _vin_api(job_id: str) -> Any:
    from .j1_vin_store import is_vin_job

    if is_vin_job(job_id):
        from . import j1_vin_research

        return j1_vin_research
    return None


def research_status(job_id: str) -> dict[str, Any]:
    from .j1_vin_store import VinJobError

    try:
        vin = _vin_api(job_id)
    except VinJobError as exc:
        return _error(exc.code, job_id=job_id)
    if vin is not None:
        return vin.research_status(job_id)
    if not _valid_id(job_id):
        return _error("job_id_invalid")
    try:
        with _db() as conn, conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return _error("job_not_found", job_id=job_id)
            sources_only = row["acquisition_mode"] == SOURCES_ONLY_MODE
            counts = conn.execute(
                """SELECT COUNT(*) AS total, SUM(status='fetched') AS fetched,
                          SUM(status='failed') AS unavailable,
                          SUM(status='duplicate') AS duplicates,
                          SUM(status IN ('failed','duplicate')) AS failed
                   FROM documents WHERE job_id=?""",
                (job_id,),
            ).fetchone()
            queries: sqlite3.Row | dict[str, int]
            query_plan: list[dict[str, str]]
            if sources_only:
                queries = {"total": 0, "done": 0, "failed": 0}
                query_plan = []
            else:
                queries = conn.execute(
                    """SELECT COUNT(*) AS total, SUM(status='done') AS done, SUM(status='failed') AS failed,
                              SUM(status='skipped') AS skipped, SUM(status='partial') AS partial
                       FROM queries WHERE job_id=?""",
                    (job_id,),
                ).fetchone()
                query_plan = [
                    {"query": item["query"], "intent": item["intent"] or "manual"}
                    for item in conn.execute("SELECT query,intent FROM queries WHERE job_id=? ORDER BY id", (job_id,))
                ]
            failures = [
                {"reason": item["error"], "count": item["count"]}
                for item in conn.execute(
                    """SELECT error,COUNT(*) AS count FROM documents
                       WHERE job_id=? AND status='failed' GROUP BY error ORDER BY count DESC, error LIMIT 10""",
                    (job_id,),
                )
            ]
            browser_attempts = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE job_id=? AND (browser_attempted=1 OR extraction_method='browser_dom')",
                (job_id,),
            ).fetchone()[0]
            stored_queries = [item["query"] for item in query_plan]
            profile = row["profile"] or GENERAL_PROFILE
            context = _stored_automotive_context(row["automotive_context"])
            query_limit = AUTOMOTIVE_MAX_QUERIES if profile == AUTOMOTIVE_PROFILE else MAX_QUERIES
            result: dict[str, Any] = {
                "ok": True,
                "schema": SCHEMA,
                "job_id": job_id,
                "status": row["status"],
                "objective": row["objective"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "max_pages": row["max_pages"],
                "profile": profile,
                "automotive_context": context or None,
                "query_plan": query_plan,
                "pages_total": counts["total"],
                "pages_fetched": counts["fetched"] or 0,
                # Kept for compatibility: historical callers treated duplicate
                # pages as non-usable pages. New fields make the distinction clear.
                "pages_failed": counts["failed"] or 0,
                "pages_unavailable": counts["unavailable"] or 0,
                "pages_duplicates": counts["duplicates"] or 0,
                "browser_pages_attempted": browser_attempts,
                "browser_pages_remaining": max(0, MAX_BROWSER_PAGES - browser_attempts),
                "mixed_pdf_ocr_pages_attempted": row["mixed_ocr_pages_used"],
                "mixed_pdf_ocr_pages_remaining": MAX_MIXED_OCR_PAGES_PER_JOB - row["mixed_ocr_pages_used"],
                "queries_total": queries["total"],
                "queries_done": queries["done"] or 0,
                "queries_failed": queries["failed"] or 0,
                "page_failures": failures,
                "search_failures": [] if sources_only else _search_failures(conn, job_id),
                "coverage": {
                    "languages": _coverage_rows(conn, job_id, "language", "language"),
                    "source_classes": _coverage_rows(conn, job_id, "source_class", "source_class"),
                    "source_tiers": _coverage_rows(conn, job_id, "source_tier", "source_tier"),
                    "discovered_source_classes": _coverage_rows(
                        conn, job_id, "source_class", "source_class", fetched_only=False
                    ),
                    "discovered_source_tiers": _coverage_rows(
                        conn, job_id, "source_tier", "source_tier", fetched_only=False
                    ),
                    "extraction_methods": _coverage_rows(conn, job_id, "extraction_method", "extraction_method"),
                    "search_providers": [] if sources_only else _provider_coverage(conn, job_id),
                },
                # Suggestions do not consume budget or create queries. They are
                # capped by the remaining explicit add_queries capacity.
                "query_suggestions": (
                    []
                    if sources_only or profile == AUTOMOTIVE_PROFILE or counts["total"] >= row["max_pages"]
                    else _safe_query_suggestions(row["objective"], stored_queries)[
                        : max(0, query_limit - queries["total"])
                    ]
                ),
                "error": row["error"] or None,
            }
            if sources_only:
                active = row["status"] in {"queued", "running"}
                extendable = (active or row["status"] == "completed") and row["created_at"] >= _retention_cutoff(
                    datetime.now(UTC)
                )
                result.update(
                    acquisition_mode=SOURCES_ONLY_MODE,
                    queries_remaining=0,
                    pages_remaining=max(0, row["max_pages"] - counts["total"]) if extendable else 0,
                    pages_pending=counts["total"] - result["pages_fetched"] - result["pages_failed"] if active else 0,
                )
            else:
                result.update(queries_skipped=queries["skipped"] or 0, queries_partial=queries["partial"] or 0)
            return result
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
    excerpt = row["excerpt"]
    if row["body_match"] is not None:
        if not row["body_match"]:
            excerpt = row["title"]
        else:
            # FTS limits tokens, so one unbroken token can fill an entire page.
            if len(excerpt) > MAX_SEARCH_SNIPPET_CHARS:
                start = max(0, excerpt.find("\x02") - MAX_SEARCH_SNIPPET_CHARS // 3)
                excerpt = excerpt[start : start + MAX_SEARCH_SNIPPET_CHARS]
            excerpt = excerpt.replace("\x02", "[").replace("\x03", "]")
    result = {
        "document_id": row["id"],
        "url": row["url"],
        "canonical_url": row["canonical_url"] or row["url"],
        "title": row["title"],
        "source": row["source"],
        "source_class": row["source_class"] or "unknown",
        "source_basis": row["source_basis"] or "fallback:unclassified",
        "source_tier": row["source_tier"] or "unclassified",
        "search_snippet": row["search_snippet"] or None,
        "search_rank": row["search_rank"] or None,
        "search_engines": [item for item in str(row["search_engines"] or "").split(",") if item] or None,
        "language": row["language"] or "unknown",
        "kind": row["kind"],
        "extraction_method": row["extraction_method"] or "unknown",
        "status": row["status"],
        "error": row["error"] or None,
        "duplicate_of": duplicate_of,
        "duplicate": duplicate,
        "excerpt": excerpt,
        "discovered_at": row["created_at"],
        "retrieved_at": row["retrieved_at"] or None,
    }
    if row["page"] is not None and row["body_match"]:
        result.update(
            page=row["page"],
            fragment_kind=row["fragment_kind"],
            section=row["section"] or None,
            source_url=row["source_url"],
        )
        if row["fragment_kind"] == "pdf_page":
            result["fragment_extraction_method"] = row["fragment_extraction_method"] or "pdf_text"
            if row["fragment_truncated"]:
                result["fragment_truncated"] = True
    if row["pdf_limitations"]:
        result["pdf_limitations"] = json.loads(row["pdf_limitations"])
    if row["body_truncated"]:
        result["body_truncated"] = True
    if row["match_kind"] is not None:
        result["match_kind"] = row["match_kind"]
    return result


def research_results(
    job_id: str,
    query: str = "",
    cursor: int = 0,
    limit: int = 20,
    query_mode: j1_identifiers.QueryMode = "lexical",
) -> dict[str, Any]:
    from .j1_vin_store import VinJobError

    try:
        vin = _vin_api(job_id)
    except VinJobError as exc:
        return _error(exc.code, job_id=job_id)
    if vin is not None:
        if query_mode != "lexical":
            return _error("query_mode_public_only", job_id=job_id)
        return vin.research_results(job_id, query, cursor, limit)
    if not _valid_id(job_id):
        return _error("job_id_invalid")
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 50:
        return _error("pagination_invalid", job_id=job_id)
    if not isinstance(query, str) or len(query) > 300 or (query.strip() and not _input_is_safe(query)):
        return _error("query_invalid_or_sensitive", job_id=job_id)
    if query_mode not in ("lexical", "dtc", "part_number"):
        return _error("query_mode_invalid", job_id=job_id)
    exact_code = j1_identifiers.normalize(query, query_mode) if query_mode != "lexical" else None
    if query_mode != "lexical" and (exact_code is None or not _input_is_safe(exact_code)):
        return _error("query_invalid_or_sensitive", job_id=job_id)
    expression = _fts_expression(query) if query_mode == "lexical" else ""
    if query_mode == "lexical" and query.strip() and not expression:
        return _error("query_invalid_or_sensitive", job_id=job_id)
    fields = """d.id,d.url,d.canonical_url,d.title,d.source,d.source_class,d.source_basis,d.source_tier,
                       d.search_snippet,d.search_rank,d.search_engines,d.language,
                       d.kind,d.extraction_method,d.status,d.error,d.duplicate_of,d.duplicate_kind,d.duplicate_score,
                       d.created_at,d.retrieved_at,d.pdf_limitations,d.body_truncated,primary_doc.url AS duplicate_url"""
    try:
        with _db() as conn, conn:
            conn.execute("BEGIN")
            if not conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
                return _error("job_not_found", job_id=job_id)
            document_count = None
            if exact_code is not None:
                match_table, document_count = j1_identifiers.query_table(conn, job_id, query_mode, exact_code)
                total = conn.execute(
                    f"SELECT COUNT(*) FROM {match_table} WHERE job_id=? AND mode=?", (job_id, query_mode)
                ).fetchone()[0]
                rows = (
                    []
                    if cursor >= total
                    else conn.execute(
                        f"""SELECT {fields}, i.title_only=0 AS body_match,
                               CASE i.title_only WHEN 1 THEN 'title' ELSE 'body' END AS match_kind,
                               substr(d.body, max(coalesce(f.start,0),i.start-80)+1,
                                   min(coalesce(f.end,length(d.body)),i.start+280)
                                   - max(coalesce(f.start,0),i.start-80)) AS excerpt,
                               f.page,f.fragment_kind,f.section,f.source_url,
                               f.extraction_method AS fragment_extraction_method,f.truncated AS fragment_truncated
                            FROM {match_table} i JOIN documents d ON d.id=i.document_id
                            LEFT JOIN documents primary_doc ON primary_doc.id=d.duplicate_of
                            LEFT JOIN document_fragments f ON f.document_id=i.document_id AND f.page=i.page
                            WHERE i.job_id=? AND i.mode=?
                            ORDER BY i.title_only,i.document_id,i.page LIMIT ? OFFSET ?""",
                        (job_id, query_mode, limit, cursor),
                    )
                )
            elif expression:
                total = conn.execute(
                    "SELECT COUNT(*) FROM documents_fts WHERE job_id=? AND documents_fts MATCH ?", (job_id, expression)
                ).fetchone()[0]
                rows = (
                    []
                    if cursor >= total
                    else conn.execute(
                        f"""SELECT {fields}, NULL AS match_kind, snippet(documents_fts,3,char(2),char(3),'…',24) AS excerpt,
                              CASE WHEN documents_fts.title='' THEN 1 ELSE EXISTS (
                                  SELECT 1 FROM documents_fts AS hit
                                  WHERE hit.rowid=documents_fts.rowid AND hit.body MATCH ?
                              ) END AS body_match,
                              f.page,f.fragment_kind,f.section,f.source_url,
                              f.extraction_method AS fragment_extraction_method,f.truncated AS fragment_truncated
                       FROM documents_fts JOIN documents AS d ON d.id=documents_fts.document_id
                       LEFT JOIN documents AS primary_doc ON primary_doc.id=d.duplicate_of
                       LEFT JOIN document_fragments AS f ON f.fts_rowid=documents_fts.rowid AND f.document_id=d.id
                       WHERE documents_fts.job_id=? AND documents_fts MATCH ?
                       ORDER BY bm25(documents_fts),d.id,f.page LIMIT ? OFFSET ?""",
                        (expression, job_id, expression, limit, cursor),
                    )
                )
            else:
                total = conn.execute("SELECT COUNT(*) FROM documents WHERE job_id=?", (job_id,)).fetchone()[0]
                rows = (
                    []
                    if cursor >= total
                    else conn.execute(
                        f"""SELECT {fields}, NULL AS match_kind, substr(d.body,1,300) AS excerpt,
                                   NULL AS page, NULL AS body_match
                       FROM documents AS d LEFT JOIN documents AS primary_doc ON primary_doc.id=d.duplicate_of
                       WHERE d.job_id=? ORDER BY d.created_at,d.id LIMIT ? OFFSET ?""",
                        (job_id, limit, cursor),
                    )
                )
            items = [_result_item(row) for row in rows]
            if exact_code is not None and document_count is not None:
                conn.commit()
                j1_identifiers.cache_query(conn, job_id, query_mode, exact_code, document_count)
            return {
                "ok": True,
                "schema": SCHEMA,
                "job_id": job_id,
                "query": query.strip(),
                "total": total,
                "cursor": cursor,
                "next_cursor": cursor + len(items) if cursor + len(items) < total else None,
                "results": items,
                **({"query_mode": query_mode} if exact_code is not None else {}),
            }
    except (OSError, sqlite3.Error):
        return _error("j1_store_unavailable", job_id=job_id)


def research_document(
    job_id: str,
    document_id: str,
    offset: int = 0,
    max_chars: int = 8000,
    page: int | None = None,
    ocr: bool = False,
) -> dict[str, Any]:
    from .j1_vin_store import VinJobError

    try:
        vin = _vin_api(job_id)
    except VinJobError as exc:
        return _error(exc.code, job_id=job_id)
    if vin is not None:
        return vin.research_document(job_id, document_id, offset, max_chars, page, ocr)
    if ocr is not False:
        return _error("vin_page_mode_required", job_id=job_id)
    if not _valid_id(job_id) or not _valid_id(document_id):
        return _error("identifier_invalid")
    if type(offset) is not int or offset < 0 or type(max_chars) is not int or not 1 <= max_chars <= 8000:
        return _error("pagination_invalid", job_id=job_id)
    if page is not None and (type(page) is not int or not 1 <= page <= MAX_PUBLIC_FRAGMENTS):
        return _error("pagination_invalid", job_id=job_id)
    try:
        with _db() as conn, conn:
            conn.execute("BEGIN")
            row = conn.execute(
                """SELECT d.url,d.canonical_url,d.title,d.kind,d.body,d.source,d.source_class,d.source_basis,d.source_tier,
                          d.search_snippet,d.search_rank,d.search_engines,d.language,
                          d.extraction_method,d.status,d.error,d.duplicate_of,d.duplicate_kind,d.duplicate_score,
                          d.created_at,d.retrieved_at,d.pdf_limitations,d.body_truncated,primary_doc.url AS duplicate_url
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
            paging = {}
            if row["pdf_limitations"]:
                paging["pdf_limitations"] = json.loads(row["pdf_limitations"])
            if row["body_truncated"]:
                paging["body_truncated"] = True
            if page is not None:
                fragment = conn.execute(
                    """SELECT f.*,
                              (SELECT MAX(page) FROM document_fragments WHERE document_id=f.document_id AND page<f.page)
                                  AS previous_page,
                              (SELECT MIN(page) FROM document_fragments WHERE document_id=f.document_id AND page>f.page)
                                  AS next_page
                       FROM document_fragments AS f WHERE f.document_id=? AND f.page=?""",
                    (document_id, page),
                ).fetchone()
                if fragment is None:
                    result = _error("document_page_unavailable", job_id=job_id)
                    result.update(document_id=document_id, page=page, **paging)
                    return result
                paging.update(
                    {
                        "page": page,
                        "fragment_kind": fragment["fragment_kind"],
                        "section": fragment["section"] or None,
                        "source_url": fragment["source_url"],
                        "previous_page": fragment["previous_page"],
                        "next_page": fragment["next_page"],
                    }
                )
                if fragment["fragment_kind"] == "pdf_page":
                    paging["fragment_extraction_method"] = fragment["extraction_method"] or "pdf_text"
                    if fragment["truncated"] or body.find("\f", fragment["end"]) == -1:
                        paging["fragment_truncated"] = True
                body = body[fragment["start"] : fragment["end"]]
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
                "source_tier": row["source_tier"] or "unclassified",
                "search_snippet": row["search_snippet"] or None,
                "search_rank": row["search_rank"] or None,
                "search_engines": [item for item in str(row["search_engines"] or "").split(",") if item] or None,
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
                **paging,
            }
    except (OSError, sqlite3.Error):
        return _error("j1_store_unavailable", job_id=job_id)


def _report_error(code: str, *, job_id: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "schema": REPORT_SCHEMA, "error": {"code": code}}
    if job_id:
        result["job_id"] = job_id
    return result


def _report_source_tier(stored_tier: str, source_class: str, source_basis: str = "") -> tuple[str, str]:
    """Use discovery tiers without treating guessed PDF type as authority."""

    if source_basis == "heuristic:technical_pdf":
        return "D", "unverified_technical_pdf"

    if stored_tier in {"A", "B", "C", "D"}:
        return stored_tier, "discovery_registry"

    source_class = str(source_class or "unknown")
    if source_class in {"official_registry", "official_oem", "official_regulator"}:
        return "A", "official_or_regulatory"
    if source_class in {"technical", "technical_manufacturer", "engineering"}:
        return "B", "technical_or_engineering"
    if source_class in {"supplier_catalog", "catalog"}:
        return "C", "catalog_or_fitment"
    if source_class in {"owner_community", "forum", "technical_reference", "editorial"}:
        return "D", "owner_experience"
    return "unrated", "unrated_source"


def _evidence_confidence(tier: str, applicability: str) -> str:
    if applicability in {"excluded", "conflict"}:
        return "insufficient"
    if tier in {"A", "B"} and applicability in {"exact", "analog"}:
        return "moderate"
    if tier in {"B", "C", "D"} or applicability == "general":
        return "low"
    return "insufficient"


def _report_source_item(
    conn: sqlite3.Connection, row: sqlite3.Row, context: dict[str, str]
) -> tuple[dict[str, Any], frozenset[int] | None]:
    tier, tier_basis = _report_source_tier(
        str(row["source_tier"] or ""), row["source_class"], str(row["source_basis"] or "")
    )
    applicability = assess_applicability(context, _source_passages(conn, row["id"], row["url"], row["body"]))
    source = {
        "document_id": row["id"],
        "url": row["url"],
        "canonical_url": row["canonical_url"] or row["url"],
        "title": row["title"],
        "source_class": row["source_class"] or "unknown",
        "source_basis": row["source_basis"] or "fallback:unclassified",
        "source_tier": tier,
        "source_tier_basis": tier_basis,
        "search_snippet": _report_excerpt(str(row["search_snippet"] or "")) or None,
        "search_rank": row["search_rank"] or None,
        "applicability": applicability.label,
        "matched_context_fields": applicability.fields,
        "applicability_basis": applicability.basis,
        "applicability_evidence": applicability.evidence,
        "scope_status": {"exact": "supported", "excluded": "excluded", "conflict": "conflicting"}.get(
            applicability.label, "unknown"
        ),
        "confidence": _evidence_confidence(tier, applicability.label),
        "retrieved_at": row["retrieved_at"] or None,
        **({"pdf_limitations": json.loads(row["pdf_limitations"])} if row["pdf_limitations"] else {}),
        **({"body_truncated": True} if row["body_truncated"] else {}),
    }
    return source, applicability.summary_passages


def _report_source_sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
    tier_order = {"A": 0, "B": 1, "C": 2, "D": 3, "unrated": 4}
    applicability_order = {"exact": 0, "conflict": 1, "analog": 2, "general": 3, "unknown": 4, "not_assessed": 5}
    return (
        tier_order.get(str(item["source_tier"]), 5),
        applicability_order.get(str(item["applicability"]), 5),
        str(item["url"]),
    )


def _report_excerpt(value: str) -> str:
    return _clean_input(redact_sensitive(value, limit=320), limit=280)


_REPORT_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+|[\r\n\f]+")
_PDF_SCOPE_WRAP = re.compile(
    r"^(?P<indent> *)(?:this|the) (?:service |workshop )?(?:bulletin|manual|procedure|document) "
    r"(?:applies to|is applicable to|covers) "
    r"(?:(?! {2}|\bengines?\b)[^\r\n\f\t|.;:]){1,400}\bvehicles? *\r?\n"
    r"(?P=indent)with (?:the )?engine (?:code )?(?=[A-Z0-9‐‑–—-]*[0-9])[A-Z0-9‐‑–—-]+\. *\r?$",
    re.IGNORECASE | re.MULTILINE,
)


def _report_passage_texts(fragment: str, *, pdf_page: bool) -> Iterator[str]:
    # Repair only a recognizable grammatical wrap with the same alignment.
    # Retain the PDF's exact characters, including the physical line break.
    start = 0
    if pdf_page:
        for scope in _PDF_SCOPE_WRAP.finditer(fragment):
            yield from _REPORT_SENTENCE_BOUNDARY.split(fragment[start : scope.start()])
            yield scope[0]
            start = scope.end()
    yield from _REPORT_SENTENCE_BOUNDARY.split(fragment[start:])


def _source_passages(conn: sqlite3.Connection, document_id: str, url: str, body: str) -> Iterator[Passage]:
    rows = conn.execute(
        """SELECT f.start,f.end,f.source_url,f.page,f.fragment_kind,f.section,d.kind,d.body_truncated
           FROM documents d LEFT JOIN document_fragments f ON f.document_id=d.id
           WHERE d.id=? ORDER BY f.page""",
        (document_id,),
    ).fetchall()
    unpadded_end = len(body.rstrip())
    for start, end, source_url, page, fragment_kind, heading, kind, capped in rows:
        if start is None:
            start, end, source_url = 0, len(body), url
        if fragment_kind == "pdf_page" or (kind == "pdf" and end in {MAX_DOCUMENT_CHARS, MAX_PUBLIC_PDF_TEXT_CHARS}):
            end = complete_pdf_fragment_end(body, start, end)
        elif kind != "pdf" and end >= unpadded_end and (capped or len(body) == MAX_DOCUMENT_CHARS):
            end = complete_capped_text_end(body, start, end)
        fragment = body[start:end]
        if heading and fragment.startswith(heading):
            rest = fragment[len(heading) :]
            if not rest or rest.startswith("\n") or (len(heading) < 200 and rest[0].isspace()):
                yield Passage(heading, page, source_url, is_heading=True)
                fragment = rest
        for sentence in _report_passage_texts(fragment, pdf_page=fragment_kind == "pdf_page"):
            if text := sentence.strip():
                yield Passage(text, page, source_url)


def _selected_passages(
    conn: sqlite3.Connection, source: dict[str, Any], relevant: frozenset[int] | None
) -> Iterator[Passage]:
    document_id = source["document_id"]
    body = conn.execute("SELECT body FROM documents WHERE id=?", (document_id,)).fetchone()[0]
    for index, passage in enumerate(_source_passages(conn, document_id, source["url"], body)):
        if relevant is None or index in relevant:
            yield passage


def _alternative_cause_mentions(
    conn: sqlite3.Connection, sources: list[dict[str, Any]], relevant: dict[str, frozenset[int]]
) -> list[dict[str, Any]]:
    """Return cited source mentions only; no automatic diagnosis is inferred."""

    selected: dict[str, dict[str, Any]] = {}
    additional: dict[str, dict[str, Any]] = {}
    for source in sources:
        if source["applicability"] in {"excluded", "conflict"}:
            continue
        from_source = False
        for passage in _selected_passages(conn, source, relevant.get(source["document_id"])):
            sentence = _report_excerpt(passage.text)
            key = sentence.casefold()
            if len(sentence) < 24 or key in selected or not any(marker in key for marker in _CAUSE_MARKERS):
                continue
            mention = {
                "status": "unverified_source_mention",
                "source_document_id": source["document_id"],
                "source_url": passage.source_url,
                "source_tier": source["source_tier"],
                "applicability": source["applicability"],
                "confidence": source["confidence"],
                "excerpt": sentence,
            }
            if not from_source:
                selected[key] = mention
                additional.pop(key, None)
                from_source = True
                if len(selected) == _MAX_CAUSE_MENTIONS:
                    return list(selected.values())
            else:
                additional.setdefault(key, mention)
            # Reserve remaining slots for distinct mentions from other sources.
            if len(additional) >= _MAX_CAUSE_MENTIONS - len(selected):
                break
    return [*selected.values(), *additional.values()][:_MAX_CAUSE_MENTIONS]


def _cited_identifier(patterns: list[j1_identifiers.IdentifierPattern], text: str, excerpt: str) -> bool:
    # Match the full stored text first. Checking only the compact prefix can
    # turn an identifier clipped before its suffix into an exact match.
    for pattern in patterns:
        if match := pattern.search(text):
            prefix = _clean_input(text[: match.end()], limit=match.end())
            if excerpt.startswith(prefix):
                return True
    return False


def _measured_excerpt(
    passage: Passage, patterns: list[j1_identifiers.IdentifierPattern], subject_terms: list[str]
) -> tuple[str, str] | None:
    for clause in CLAUSE_BREAK.split(passage.text):
        if not (_FREQUENCY_COUNT.search(clause) or _FREQUENCY_PERCENT.search(clause)):
            continue
        excerpt = _report_excerpt(clause)
        match = _FREQUENCY_COUNT.search(excerpt) or _FREQUENCY_PERCENT.search(excerpt)
        if match is None or len(excerpt) < 24:
            continue
        normalized = excerpt.casefold()
        mentions_subject = (
            _cited_identifier(patterns, clause, excerpt)
            if patterns
            else any(term in normalized for term in subject_terms)
        )
        if mentions_subject and any(marker in normalized for marker in _FREQUENCY_DEFECT_MARKERS):
            return match.group(0), excerpt
    return None


def _frequency_measurements(
    conn: sqlite3.Connection,
    sources: list[dict[str, Any]],
    context: dict[str, str],
    relevant: dict[str, frozenset[int]],
) -> list[dict[str, Any]]:
    """Expose a cohort only when its sentence names this investigated defect.

    A source's authority and an exact vehicle match cannot turn a population
    count about a feature, campaign, or unrelated repair into failure rate.
    Requiring a technical subject from the profile plus a defect marker is
    intentionally conservative; ambiguous counts remain unmeasured.
    """

    measurements: list[dict[str, Any]] = []
    mode: j1_identifiers.QueryMode = "dtc" if context.get("dtc") else "part_number"
    identifiers = context.get(mode, "")
    patterns = [
        j1_identifiers.match_pattern(code, mode)
        for value in (identifiers.split(",") if mode == "dtc" else [identifiers])
        if (code := j1_identifiers.normalize(value, mode)) is not None
    ]
    subject_terms = [
        value.casefold() for key in ("system", "symptom") if len(value := context.get(key, "").strip()) >= 3
    ]
    if not patterns and (identifiers or not subject_terms):
        return measurements
    for source in sources:
        if source["source_tier"] not in {"A", "B"} or source["applicability"] not in {"exact", "analog"}:
            continue
        for passage in _selected_passages(conn, source, relevant.get(source["document_id"])):
            candidate = _measured_excerpt(passage, patterns, subject_terms)
            if candidate is None:
                continue
            measurement, excerpt = candidate
            measurements.append(
                {
                    "status": "reported_source_measurement",
                    "source_document_id": source["document_id"],
                    "source_url": passage.source_url,
                    "source_tier": source["source_tier"],
                    "applicability": source["applicability"],
                    "measurement": measurement,
                    "excerpt": excerpt,
                }
            )
            break
        if len(measurements) >= 3:
            break
    return measurements


def _confidence_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the source examples that establish high or moderate coverage."""
    direct = [
        source for source in sources if source["source_tier"] in {"A", "B"} and source["applicability"] == "exact"
    ]
    if not direct:
        return [
            source for source in sources if source["source_tier"] in {"A", "B"} and source["applicability"] == "analog"
        ][:1]
    representatives: dict[str, dict[str, Any]] = {}
    for source in direct:
        domain = publisher_domain(str(source.get("canonical_url") or source["url"]))
        if domain:
            representatives.setdefault(domain, source)
            if len(representatives) == 2:
                return list(representatives.values())
    return direct[:1]


def _report_source_examples(
    sources: list[dict[str, Any]], confidence_sources: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    needed = {source["document_id"] for source in confidence_sources}
    # Retain one cited example of each restriction so an evidence shortlist
    # cannot hide a qualified exclusion behind more positively ranked sources.
    for label in ("conflict", "excluded"):
        restriction = next(
            (source for source in sources if source["source_tier"] in {"A", "B"} and source["applicability"] == label),
            None,
        )
        if restriction is not None:
            needed.add(restriction["document_id"])
    other = (source for source in sources if source["document_id"] not in needed)
    needed.update(source["document_id"] for source in islice(other, 20 - len(needed)))
    return [source for source in sources if source["document_id"] in needed]


def _overall_confidence(sources: list[dict[str, Any]], confidence_sources: list[dict[str, Any]]) -> str:
    if len(confidence_sources) >= 2:
        return "high"
    if confidence_sources:
        return "moderate"
    if sources:
        return "low"
    return "insufficient"


def _source_domains(sources: list[dict[str, Any]]) -> int:
    domains: set[str] = set()
    for source in sources:
        domain = publisher_domain(str(source.get("canonical_url") or source["url"]))
        if domain:
            domains.add(domain)
    return len(domains)


def _report_limitations(
    *,
    status: str,
    has_automotive_context: bool,
    sources: list[dict[str, Any]],
    unavailable: list[dict[str, Any]],
    search_failures: list[dict[str, Any]],
    frequency_measurements: list[dict[str, Any]],
) -> list[dict[str, str]]:
    limitations: list[dict[str, str]] = []
    if status != "completed":
        limitations.append({"code": "research_incomplete", "detail": "job_not_completed"})
    if not sources:
        limitations.append({"code": "no_retrieved_sources", "detail": "no_fetched_public_evidence"})
    if not any(source["source_tier"] == "A" for source in sources):
        limitations.append({"code": "no_official_source", "detail": "no_tier_a_source_retrieved"})
    if has_automotive_context and sources and not any(source["applicability"] == "exact" for source in sources):
        limitations.append(
            {"code": "no_exact_context_match", "detail": "fitment_and_diagnosis_require_separate_verification"}
        )
    if any(
        source["source_tier"] in {"A", "B"} and source["applicability"] in {"excluded", "conflict"}
        for source in sources
    ):
        limitations.append({"code": "qualified_source_scope_limits", "detail": "tier_a_b_exclusion_or_conflict"})
    if any(source.get("body_truncated") for source in sources):
        limitations.append({"code": "source_text_truncated", "detail": "one_or_more_public_texts_reached_the_limit"})
    if has_automotive_context and not frequency_measurements:
        limitations.append({"code": "frequency_not_measured", "detail": "no_qualified_population_measurement"})
    if any(failure["reason"] != _WORKER_FAILED for failure in search_failures):
        limitations.append({"code": "search_access_limited", "detail": "one_or_more_searches_returned_no_results"})
    if any(source["reason"] != _WORKER_FAILED for source in unavailable):
        limitations.append({"code": "source_access_limited", "detail": "one_or_more_public_sources_unavailable"})
    limitations.append({"code": "no_automatic_diagnosis", "detail": "source_text_requires_human_review"})
    return limitations


def research_report(job_id: str) -> dict[str, Any]:
    """Build a read-only evidence ledger from a retained J1 research job."""
    from .j1_vin_store import VinJobError

    try:
        vin = _vin_api(job_id)
    except VinJobError as exc:
        return _report_error(exc.code, job_id=job_id)
    if vin is not None:
        return vin.research_report(job_id)

    if not _valid_id(job_id):
        return _report_error("job_id_invalid")
    try:
        with _db() as conn, conn:
            conn.execute("BEGIN")
            job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                return _report_error("job_not_found", job_id=job_id)
            context = _stored_automotive_context(job["automotive_context"])
            sources_only = job["acquisition_mode"] == SOURCES_ONLY_MODE
            search_failures = [] if sources_only else _search_failures(conn, job_id)
            query_outcomes = (
                {}
                if sources_only
                else dict(
                    conn.execute(
                        "SELECT status,COUNT(*) FROM queries WHERE job_id=? AND status IN ('skipped','partial') GROUP BY status",
                        (job_id,),
                    ).fetchall()
                )
            )
            rows = conn.execute(
                """SELECT id,url,canonical_url,title,CASE WHEN ? THEN body ELSE '' END AS body,
                          source_class,source_basis,source_tier,
                          search_snippet,search_rank,retrieved_at,pdf_limitations,body_truncated
                   FROM documents WHERE job_id=? AND status='fetched'""",
                (bool(context), job_id),
            )
            # Rank lightweight metadata. Read a body for excerpt extraction only
            # while examining that source, even for a full 300-document job.
            all_sources = []
            relevant: dict[str, frozenset[int]] = {}
            for row in rows:
                source, source_passages = _report_source_item(conn, row, context)
                all_sources.append(source)
                if source_passages is not None:
                    relevant[source["document_id"]] = source_passages
            all_sources.sort(key=_report_source_sort_key)
            confidence_sources = _confidence_sources(all_sources)
            sources = _report_source_examples(all_sources, confidence_sources)
            alternative_causes = _alternative_cause_mentions(conn, sources, relevant)
            frequency_measurements = _frequency_measurements(conn, all_sources, context, relevant)
            duplicates = [
                {
                    "document_id": row["id"],
                    "url": row["url"],
                    "duplicate_of": row["duplicate_of"] or None,
                    "kind": row["duplicate_kind"] or "unknown",
                }
                for row in conn.execute(
                    """SELECT id,url,duplicate_of,duplicate_kind FROM documents
                       WHERE job_id=? AND status='duplicate' ORDER BY created_at,id LIMIT 20""",
                    (job_id,),
                )
            ]
            unavailable = [
                {
                    "document_id": row["id"],
                    "url": row["url"],
                    "title": row["title"] or None,
                    "reason": row["error"] or "fetch_failed",
                    # Kept for consumers of the original grouped form.  Each
                    # row is now one inspectable unavailable document.
                    "count": 1,
                    "source_class": row["source_class"] or "unknown",
                    "source_tier": row["source_tier"] or "unclassified",
                }
                for row in conn.execute(
                    """SELECT id,url,title,error,source_class,source_tier FROM documents
                       WHERE job_id=? AND status='failed' ORDER BY created_at,id""",
                    (job_id,),
                )
            ]
    except (OSError, sqlite3.Error):
        return _report_error("j1_store_unavailable", job_id=job_id)

    confirmed = [
        {**source, "evidence_kind": "strong_context_source"}
        for source in sources
        if source["source_tier"] == "A" and source["applicability"] == "exact"
    ][:5]
    confirmed_ids = {source["document_id"] for source in confirmed}
    hypotheses = [
        {**source, "evidence_kind": "requires_source_review"}
        for source in sources
        if source["document_id"] not in confirmed_ids and source["applicability"] not in {"excluded", "conflict"}
    ][:10]
    tier_counts = {
        tier: sum(1 for source in all_sources if source["source_tier"] == tier)
        for tier in ("A", "B", "C", "D", "unrated")
    }
    frequency: dict[str, Any]
    if frequency_measurements:
        frequency = {
            "status": "measured_in_source",
            "measurements": frequency_measurements,
            "note": "reported_measurement_is_limited_to_the_source_population",
        }
    else:
        frequency = {
            "status": "not_measured",
            "reason": "no_qualified_population_measurement",
        }
    limitations = _report_limitations(
        status=str(job["status"]),
        has_automotive_context=bool(context),
        sources=all_sources,
        unavailable=unavailable,
        search_failures=search_failures,
        frequency_measurements=frequency_measurements,
    )
    if len(all_sources) > len(sources):
        limitations.append({"code": "source_list_truncated", "detail": "report_examples_cover_first_20_ranked_sources"})
    if query_outcomes.get("skipped"):
        limitations.append({"code": _PAGE_BUDGET_REACHED, "detail": "search_queries_skipped"})
    if query_outcomes.get("partial"):
        limitations.append({"code": "search_incomplete", "detail": "interrupted_search_returned_partial_results"})
    result = {
        "ok": True,
        "schema": REPORT_SCHEMA,
        "job_id": job_id,
        "status": job["status"],
        "profile": job["profile"] or GENERAL_PROFILE,
        "automotive_context": context or None,
        "report": {
            "evidence_confidence": {
                "level": _overall_confidence(all_sources, confidence_sources),
                "retrieved_sources": len(all_sources),
                "independent_domains": _source_domains(all_sources),
                "source_tiers": tier_counts,
            },
            "frequency": frequency,
            "confirmed": confirmed,
            "hypotheses": hypotheses,
            "alternative_causes": alternative_causes,
            "sources": sources,
            "duplicates": duplicates,
            "unavailable": unavailable[:20],
            "search_failures": search_failures,
            "limitations": limitations,
            "read_only": True,
            "fitment_confirmed": False,
            "crm_written": False,
        },
    }
    if sources_only:
        result["acquisition_mode"] = SOURCES_ONLY_MODE
    return result


def research_add_queries(job_id: str, queries: list[str]) -> dict[str, Any]:
    from .j1_vin_store import VinJobError

    try:
        vin = _vin_api(job_id)
    except VinJobError as exc:
        return _error(exc.code, job_id=job_id)
    if vin is not None:
        return vin.research_add_queries(job_id, queries)
    if not _valid_id(job_id):
        return _error("job_id_invalid")
    clean_queries = _validated_queries(queries)
    if clean_queries is None:
        return _error("queries_invalid_or_sensitive", job_id=job_id)
    try:
        with _db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT status,profile,acquisition_mode,max_pages FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if job is None:
                return _error("job_not_found", job_id=job_id)
            if job["acquisition_mode"] == SOURCES_ONLY_MODE:
                return _error("queries_not_allowed_for_sources_only", job_id=job_id)
            if job["status"] in {"cancelled", "failed"}:
                return _error("job_not_extendable", job_id=job_id)
            count = conn.execute("SELECT COUNT(*) FROM queries WHERE job_id=?", (job_id,)).fetchone()[0]
            existing = {
                row[0].casefold() for row in conn.execute("SELECT query FROM queries WHERE job_id=?", (job_id,))
            }
            additions = [item for item in clean_queries if item.casefold() not in existing]
            query_limit = AUTOMOTIVE_MAX_QUERIES if job["profile"] == AUTOMOTIVE_PROFILE else MAX_QUERIES
            if count + len(additions) > query_limit:
                return _error("query_limit_reached", job_id=job_id)
            page_budget_full = _document_count(conn, job_id) >= job["max_pages"]
            reopen_job = bool(additions) and job["status"] == "completed" and not page_budget_full
            if reopen_job:
                busy = conn.execute(
                    "SELECT 1 FROM jobs WHERE status IN ('queued','running') AND id<>? LIMIT 1", (job_id,)
                ).fetchone()
                if busy:
                    return _error("j1_busy", job_id=job_id)
            query_status = "skipped" if page_budget_full else "pending"
            error = _PAGE_BUDGET_REACHED if page_budget_full else ""
            conn.executemany(
                "INSERT INTO queries(job_id,query,intent,status,error) VALUES(?,?,'manual_follow_up',?,?)",
                ((job_id, item, query_status, error) for item in additions),
            )
            if additions:
                status = "queued" if reopen_job else job["status"]
                conn.execute("UPDATE jobs SET status=?,updated_at=? WHERE id=?", (status, _utcnow(), job_id))
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
    from .j1_vin_store import VinJobError

    try:
        vin = _vin_api(job_id)
    except VinJobError as exc:
        return _error(exc.code, job_id=job_id)
    if vin is not None:
        return vin.research_cancel(job_id)
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


def _compact_cache(conn: sqlite3.Connection) -> bool:
    # A read snapshot can pin old WAL frames. Vacuum would append another copy
    # of the database while the reader still prevents us from reclaiming space.
    if conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
        return False
    conn.execute("VACUUM")
    # VACUUM itself writes the compacted database into the WAL. Reconcile it
    # before the physical DB+WAL capacity check, while this connection is open.
    return conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0


def _evict_terminal_job(conn: sqlite3.Connection, job_id: str) -> bool:
    # A follow-up can reopen a candidate after the initial scan. The guarded
    # delete takes the write lock before touching its searchable evidence.
    removed = conn.execute("DELETE FROM jobs WHERE id=? AND status NOT IN ('queued','running')", (job_id,)).rowcount
    if removed:
        conn.execute("DELETE FROM documents_fts WHERE job_id=?", (job_id,))
    return bool(removed)


def _prune(conn: sqlite3.Connection) -> None:
    cutoff = _retention_cutoff(datetime.now(UTC))
    expired = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM jobs WHERE created_at<? AND status NOT IN ('queued','running')", (cutoff,)
        )
    ]
    for job_id in expired:
        _evict_terminal_job(conn, job_id)
    conn.commit()
    if not expired and _cache_size() <= MAX_CORPUS_BYTES:
        return
    # Reclaim unused pages and the WAL before evicting retained jobs. In
    # particular, a private VIN job has only a tiny routing stub in this DB;
    # deleting it cannot be the way to reclaim an unrelated SQLite freelist.
    if not _compact_cache(conn) or _cache_size() <= MAX_CORPUS_BYTES:
        return

    from .j1_vin_store import PROFILE as vin_profile, RETENTION_SECONDS as vin_retention_seconds

    # VIN receipts live in tmpfs, but every public API and receipt replay needs
    # this de-identified pointer until that private scope expires. Use queue
    # creation time only, without entering the private-store/start-lock order.
    # The queue timestamp is rounded down to seconds; one second of routing
    # grace covers that roundoff without extending any private data's expiry.
    vin_cutoff = (datetime.now(UTC) - timedelta(seconds=vin_retention_seconds + 1)).isoformat(timespec="microseconds")
    old = [
        row[0]
        for row in conn.execute(
            """SELECT id FROM jobs WHERE status NOT IN ('queued','running')
               AND (profile<>? OR created_at<?) ORDER BY created_at""",
            (vin_profile, vin_cutoff),
        )
    ]
    for job_id in old:
        removed = _evict_terminal_job(conn, job_id)
        conn.commit()
        if not removed:
            continue
        if not _compact_cache(conn) or _cache_size() <= MAX_CORPUS_BYTES:
            break


def _claim_job(conn: sqlite3.Connection) -> str | None:
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
    if row:
        conn.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?", (_utcnow(), row["id"]))
    conn.commit()
    return row["id"] if row else None


def _lock_running_job(conn: sqlite3.Connection, job_id: str) -> bool:
    conn.execute("BEGIN IMMEDIATE")
    return conn.execute("SELECT 1 FROM jobs WHERE id=? AND status='running'", (job_id,)).fetchone() is not None


def _discovery_metadata(row: dict[str, Any], url: str, canonical_url: str) -> tuple[str, str, str, str, str, int, str]:
    """Normalize discovery-only fields before they enter the temporary corpus."""

    title = redact_sensitive(str(row.get("title") or ""), limit=200)
    classification = classify_source(canonical_url or url, title=title)
    snippet = _report_excerpt(str(row.get("snippet") or ""))
    raw_rank = row.get("search_rank")
    rank = raw_rank if type(raw_rank) is int and 0 < raw_rank <= 100_000 else 0
    raw_engines = row.get("engines")
    if not isinstance(raw_engines, (list, tuple, set)):
        raw_engines = [row.get("engine") or row.get("source") or ""]
    engines = sorted(
        {value for item in raw_engines if re.fullmatch(r"[a-z0-9_-]{1,32}", (value := str(item).casefold().strip()))}
    )[:8]
    return (
        title,
        classification.source_class,
        classification.source_basis,
        classification.source_tier,
        snippet,
        rank,
        ",".join(engines),
    )


def _work_job(job_id: str) -> None:
    """Do one bounded unit at a time so cancellation is observed promptly."""
    if _vin_api(job_id) is not None:
        from .j1_vin_worker import run_job

        run_job(job_id)
        return

    try:
        _work_public_job(job_id)
    finally:
        if _STOP:
            with _db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                requeued = conn.execute(
                    "UPDATE jobs SET status='queued',updated_at=? WHERE id=? AND status='running'",
                    (_utcnow(), job_id),
                ).rowcount
                if requeued:
                    for table in ("queries", "documents"):
                        conn.execute(
                            f"UPDATE {table} SET status='pending' WHERE job_id=? AND status='running'", (job_id,)
                        )
                conn.commit()


def _work_query(job_id: str, query_id: int, query: str, max_pages: int) -> bool:
    try:
        results, provider = search_public(
            query,
            searxng_url=os.environ.get("AUTOSTOP_J1_SEARXNG_URL", ""),
            stop_requested=lambda: _STOP,
        )
    except Exception:  # noqa: BLE001 - one provider error must not fail the corpus.
        results, provider = [], "search_failed"
    if _STOP and not results:
        return False
    with _db() as conn:
        if not _lock_running_job(conn, job_id):
            return False
        query_state = conn.execute(
            "SELECT primary_admitted,provider,error FROM queries WHERE id=?", (query_id,)
        ).fetchone()
        refresh_ranking = (
            provider != _SEARCH_INTERRUPTED
            and query_state["error"] == _SEARCH_INTERRUPTED
            and query_state["provider"] in ("", provider)
        )
        _stage_query_candidates(conn, query_id, results, refresh_ranking=refresh_ranking)
        if (
            not query_state["primary_admitted"]
            and _document_count(conn, job_id) < max_pages
            and _enqueue_query_candidate(conn, job_id, query_id) is not None
        ):
            conn.execute("UPDATE queries SET primary_admitted=1 WHERE id=?", (query_id,))
        if provider == _SEARCH_INTERRUPTED:
            if results:
                source = next(
                    (row["source"] for row in results if row.get("source") in ("searxng", "duckduckgo", "direct_url")),
                    query_state["provider"],
                )
                conn.execute(
                    "UPDATE queries SET provider=?,error=? WHERE id=?", (source, _SEARCH_INTERRUPTED, query_id)
                )
        elif not results and (query_state["error"] == _SEARCH_INTERRUPTED or query_state["primary_admitted"]):
            conn.execute("UPDATE queries SET status='partial',error=? WHERE id=?", (provider[:80], query_id))
        else:
            conn.execute(
                "UPDATE queries SET status=?,provider=?,error=? WHERE id=?",
                ("done" if results else "failed", provider[:40], "" if results else provider[:80], query_id),
            )
        conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (_utcnow(), job_id))
        conn.commit()
    return True


def _stage_query_candidates(
    conn: sqlite3.Connection, query_id: int, results: list[dict[str, Any]], *, refresh_ranking: bool
) -> None:
    candidates: dict[str, tuple[int, str, str, str, str, str, str, str, str, int, str, int]] = {}
    for position, row in enumerate(results[:MAX_SEARCH_RESULTS], start=1):
        url = public_url(row.get("url", ""))
        canonical_url = _canonical_url(url)
        if not url or not canonical_url:
            continue
        title, source_class, source_basis, source_tier, snippet, search_rank, engines = _discovery_metadata(
            row, url, canonical_url
        )
        rank = search_rank or position
        if canonical_url in candidates and candidates[canonical_url][-1] <= rank:
            continue
        candidates[canonical_url] = (
            query_id,
            url,
            canonical_url,
            title,
            redact_sensitive(str(row.get("source") or ""), limit=40),
            source_class,
            source_basis,
            source_tier,
            snippet,
            search_rank,
            engines,
            rank,
        )
    if refresh_ranking and candidates:
        # A completed response can reorder an interrupted response from the
        # same provider. Keep candidates unique to the earlier response as fallbacks.
        complete_rank_limit = max(candidate[-1] for candidate in candidates.values())
        conn.execute("UPDATE query_candidates SET rank=rank+? WHERE query_id=?", (complete_rank_limit, query_id))
    conn.executemany(
        """INSERT INTO query_candidates(
               query_id,url,canonical_url,title,source,source_class,source_basis,source_tier,
               search_snippet,search_rank,search_engines,rank
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(query_id,canonical_url) DO UPDATE SET
               url=excluded.url,title=excluded.title,source=excluded.source,source_class=excluded.source_class,
               source_basis=excluded.source_basis,source_tier=excluded.source_tier,search_snippet=excluded.search_snippet,
               search_rank=excluded.search_rank,search_engines=excluded.search_engines,rank=excluded.rank
           WHERE ?""",
        ((*candidate, refresh_ranking) for candidate in candidates.values()),
    )
    # A resumed search can have candidates from its previous partial result.
    conn.execute(
        """DELETE FROM query_candidates WHERE query_id=? AND rowid IN (
               SELECT rowid FROM query_candidates WHERE query_id=? ORDER BY rank,canonical_url LIMIT -1 OFFSET ?
           )""",
        (query_id, query_id, MAX_SEARCH_RESULTS),
    )


def _enqueue_query_candidate(
    conn: sqlite3.Connection, job_id: str, query_id: int | None = None
) -> dict[str, str] | None:
    query_scope = "" if query_id is None else "AND candidate.query_id=?"
    parameters = (job_id,) if query_id is None else (job_id, query_id)
    candidate = conn.execute(
        f"""SELECT candidate.* FROM query_candidates candidate
            JOIN queries q ON q.id=candidate.query_id
            WHERE q.job_id=? {query_scope} AND NOT EXISTS (
                SELECT 1 FROM documents d WHERE d.job_id=q.job_id AND (
                    d.canonical_url=candidate.canonical_url OR d.requested_canonical_url=candidate.canonical_url
                    OR d.url=candidate.url
                )
            )
            ORDER BY candidate.rank,candidate.query_id,candidate.canonical_url LIMIT 1""",
        parameters,
    ).fetchone()
    if candidate is None:
        return None
    document_id = uuid4().hex
    conn.execute(
        """INSERT INTO documents(
               id,job_id,url,canonical_url,title,source,source_class,source_basis,source_tier,
               search_snippet,search_rank,search_engines,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            document_id,
            job_id,
            candidate["url"],
            candidate["canonical_url"],
            candidate["title"],
            candidate["source"],
            candidate["source_class"],
            candidate["source_basis"],
            candidate["source_tier"],
            candidate["search_snippet"],
            candidate["search_rank"],
            candidate["search_engines"],
            _utcnow(),
        ),
    )
    conn.execute(
        "DELETE FROM query_candidates WHERE query_id=? AND canonical_url=?",
        (candidate["query_id"], candidate["canonical_url"]),
    )
    return {"id": document_id, "url": candidate["url"]}


def _clear_query_candidates(conn: sqlite3.Connection, job_id: str) -> int:
    return conn.execute(
        "DELETE FROM query_candidates WHERE query_id IN (SELECT id FROM queries WHERE job_id=?)", (job_id,)
    ).rowcount


_DocumentFragment = tuple[int, str, str, str, int, int, str, bool]


def _document_fragments(fetch_result: dict[str, Any], body: str, url: str) -> list[_DocumentFragment]:
    raw_fragments = fetch_result.get("fragments")
    if not isinstance(raw_fragments, list) or not raw_fragments:
        return []
    if fetch_result.get("text") != body:
        if not all(item["fragment_kind"] == "pdf_page" for item in raw_fragments):
            return []
        # Final redaction can change offsets while retaining physical page
        # separators. Rebuild PDF offsets against the text being stored.
        raw_fragments = _pdf_fragments(
            body,
            frozenset(item["page"] for item in raw_fragments if item.get("extraction_method") == "pdf_ocr"),
            frozenset(item["page"] for item in raw_fragments if item.get("truncated") is True),
        )
    fragments = []
    last_page = last_end = 0
    for item in raw_fragments[:MAX_PUBLIC_FRAGMENTS]:
        page, start, end = item["page"], item["start"], item["end"]
        fragment_kind = item["fragment_kind"]
        truncated = fragment_kind == "pdf_page" and item.get("truncated") is True
        if not (last_page < page <= MAX_PUBLIC_FRAGMENTS and last_end <= start <= end <= len(body)):
            return []
        if start == end and not truncated:
            return []
        section = redact_sensitive(item.get("section", ""), limit=200)
        anchor = f"page={page}" if fragment_kind == "pdf_page" else quote(item.get("anchor", ""), safe="")
        source_url = f"{url}#{anchor}" if anchor else url
        method = "pdf_ocr" if item.get("extraction_method") == "pdf_ocr" else ""
        fragments.append(
            (page, fragment_kind, section, source_url if public_url(source_url) else url, start, end, method, truncated)
        )
        last_page, last_end = page, end
    return fragments


def _index_document(
    conn: sqlite3.Connection,
    job_id: str,
    document_id: str,
    title: str,
    body: str,
    fragments: list[_DocumentFragment],
) -> None:
    # Only pending documents are claimed. Their first index and fetched status
    # commit together, so a restarted worker cannot inherit a partial index.
    insert_text = "INSERT INTO documents_fts(job_id,document_id,title,body) VALUES(?,?,?,?)"
    if not fragments:
        conn.execute(insert_text, (job_id, document_id, title, body))
        return
    for index, (page, kind, section, source_url, start, end, method, truncated) in enumerate(fragments):
        cursor = conn.execute(insert_text, (job_id, document_id, title if index == 0 else "", body[start:end]))
        conn.execute(
            """INSERT INTO document_fragments(
                   fts_rowid,document_id,page,fragment_kind,section,source_url,start,end,extraction_method,truncated
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (cursor.lastrowid, document_id, page, kind, section, source_url, start, end, method, truncated),
        )


def _reserve_mixed_ocr_page(job_id: str, document_id: str) -> str | None:
    try:
        with _db() as conn, conn:
            if _STOP or not _lock_running_job(conn, job_id):
                return "ocr_interrupted"
            used = conn.execute("SELECT mixed_ocr_pages_used FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
            if used >= MAX_MIXED_OCR_PAGES_PER_JOB:
                return "ocr_job_budget"
            reserved = conn.execute(
                """UPDATE documents SET mixed_ocr_pages_used=mixed_ocr_pages_used+1
                   WHERE id=? AND job_id=? AND status='running' AND mixed_ocr_pages_used<?""",
                (document_id, job_id, MIXED_OCR_PAGES_PER_DOCUMENT),
            ).rowcount
            if not reserved:
                return "ocr_document_budget"
            conn.execute("UPDATE jobs SET mixed_ocr_pages_used=mixed_ocr_pages_used+1 WHERE id=?", (job_id,))
    except (OSError, sqlite3.Error):
        return "ocr_budget_unavailable"
    return None


def _work_public_job(job_id: str) -> None:
    # Alternate discovery and reading when both queues have work.
    prefer_document = True
    while not _STOP:
        from .j1_vin_store import prune as prune_vin

        prune_vin()
        with _db() as conn:
            # Serialize claims and completion with cancellation/query additions.
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT status,max_pages,acquisition_mode,profile FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if job is None or job["status"] != "running":
                return
            sources_only = job["acquisition_mode"] == SOURCES_ONLY_MODE
            pending_query = None
            can_admit_pages = not sources_only and _document_count(conn, job_id) < job["max_pages"]
            if can_admit_pages:
                pending_query = conn.execute(
                    "SELECT id,query FROM queries WHERE job_id=? AND status='pending' ORDER BY id LIMIT 1", (job_id,)
                ).fetchone()
            elif not sources_only:
                _close_queries_at_page_limit(conn, job_id)
            # Submission order for supplied URLs, ranked admission order for queries.
            pending_doc = conn.execute(
                "SELECT id,url FROM documents WHERE job_id=? AND status='pending' ORDER BY created_at,rowid LIMIT 1",
                (job_id,),
            ).fetchone()
            if can_admit_pages and pending_query is None and pending_doc is None:
                pending_doc = _enqueue_query_candidate(conn, job_id)
            if pending_query and not (pending_doc and prefer_document):
                conn.execute("UPDATE queries SET status='running' WHERE id=?", (pending_query["id"],))
                conn.commit()
                query_id, query = pending_query["id"], pending_query["query"]
                phase = "query"
            elif pending_doc:
                browser_attempts = conn.execute(
                    "SELECT COUNT(*) FROM documents WHERE job_id=? AND (browser_attempted=1 OR extraction_method='browser_dom')",
                    (job_id,),
                ).fetchone()[0]
                # Retain the discovery key before a redirect replaces both url and canonical_url.
                conn.execute(
                    """UPDATE documents SET status='running',
                       requested_canonical_url=COALESCE(NULLIF(requested_canonical_url,''),?) WHERE id=?""",
                    (_canonical_url(pending_doc["url"], supplied=sources_only), pending_doc["id"]),
                )
                conn.commit()
                doc_id, url = pending_doc["id"], pending_doc["url"]
                allow_browser = browser_attempts < MAX_BROWSER_PAGES
                phase = "document"
            else:
                if not sources_only:
                    _clear_query_candidates(conn, job_id)
                conn.execute("UPDATE jobs SET status='completed',updated_at=? WHERE id=?", (_utcnow(), job_id))
                conn.commit()
                return
        if _STOP:
            return
        prefer_document = phase == "query"
        if phase == "query":
            if not _work_query(job_id, query_id, query, job["max_pages"]):
                return
        else:
            try:
                with mixed_ocr_budget(partial(_reserve_mixed_ocr_page, job_id, doc_id)):
                    fetch_result = fetch_document(url, allow_browser=allow_browser)
            except Exception:  # noqa: BLE001 - one malformed page must not fail the corpus.
                fetch_result = {"ok": False, "error": "fetch_failed"}
            with _db() as conn:
                if not _lock_running_job(conn, job_id):
                    return
                # Keep prior usage even when a retry or recovery yields static text.
                conn.execute(
                    """UPDATE documents SET browser_attempted=(browser_attempted=1 OR extraction_method='browser_dom' OR ?)
                       WHERE id=?""",
                    (
                        fetch_result.get("browser_attempted") is True
                        or fetch_result.get("extraction_method") == "browser_dom",
                        doc_id,
                    ),
                )
                if fetch_result.get("ok") is True:
                    title = redact_sensitive(fetch_result.get("title", ""), limit=200)
                    kind = _clean_input(str(fetch_result.get("kind") or ""), limit=40)
                    is_pdf_text = kind == "pdf" and fetch_result.get("extraction_method") in {"pdf_text", "pdf_mixed"}
                    limitations = fetch_result.get("pdf_limitations")
                    pdf_limitations = json.dumps(limitations) if limitations else ""
                    body_truncated = fetch_result.get("body_truncated") is True
                    raw_body = str(fetch_result.get("text") or "")
                    if kind == "pdf":
                        body = redact_sensitive(
                            raw_body,
                            limit=MAX_PUBLIC_PDF_TEXT_CHARS if is_pdf_text else MAX_DOCUMENT_CHARS,
                            preserve_page_breaks=is_pdf_text,
                        )
                    else:
                        body, storage_truncated = _bounded_public_text(raw_body, MAX_DOCUMENT_CHARS)
                        body_truncated |= storage_truncated
                    fetched_url = public_url(fetch_result.get("url", "")) or url
                    canonical_url = _canonical_url(fetched_url, supplied=sources_only) or _canonical_url(
                        url, supplied=sources_only
                    )
                    # A redirect may resolve to a URL already queued by search.
                    # Keep this row's original source link in that rare case; the
                    # canonical URL still links content-level duplicates below.
                    url_conflict = conn.execute(
                        "SELECT 1 FROM documents WHERE job_id=? AND url=? AND id<>? LIMIT 1",
                        (job_id, fetched_url, doc_id),
                    ).fetchone()
                    stored_url = url if url_conflict else fetched_url
                    source_class, source_basis, source_tier = _classify_source(canonical_url or stored_url, title, kind)
                    language = _detect_language(title, body)
                    extraction_method = _extraction_method(fetch_result)
                    digest = hashlib.sha256(body.encode()).hexdigest()
                    # A one-character revision of a part number or DTC can be the
                    # whole reason to read a second source or a particular page.
                    preserve_variants = (
                        sources_only or job["profile"] == AUTOMOTIVE_PROFILE or bool(fetch_result.get("fragments"))
                    )
                    similarity_hash = "" if preserve_variants else _simhash(body)
                    duplicate = _find_duplicate(conn, job_id=job_id, digest=digest, similarity_hash=similarity_hash)
                    retrieved_at = _utcnow()
                    if duplicate is not None:
                        primary, duplicate_kind, duplicate_score = duplicate
                        conn.execute(
                            """UPDATE documents SET status='duplicate',url=?,canonical_url=?,title=?,kind=?,body='',
                               content_hash=?,content_simhash=?,language=?,extraction_method=?,source_class=?,source_basis=?,
                               source_tier=?,pdf_limitations=?,body_truncated=?,
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
                                source_tier,
                                pdf_limitations,
                                body_truncated,
                                primary["id"],
                                duplicate_kind,
                                duplicate_score,
                                "duplicate_content" if duplicate_kind == "exact" else "duplicate_near_content",
                                retrieved_at,
                                doc_id,
                            ),
                        )
                        conn.execute(_INDEX_DUPLICATE_TITLES + " AND d.id=?", (doc_id,))
                    else:
                        # FTS repeats the text; reserve enough room only for a
                        # genuinely new document. Duplicate links use no corpus
                        # body and therefore remain useful at capacity.
                        fragments = _document_fragments(fetch_result, body, fetched_url)
                        expected_bytes = len(body.encode("utf-8")) * 3 + sum(
                            256 + len(section.encode("utf-8")) + len(source_url.encode("utf-8"))
                            for _, _, section, source_url, _, _, _, _ in fragments
                        )
                        if _cache_size() + expected_bytes > MAX_CORPUS_BYTES:
                            _prune(conn)
                            # Pruning commits before vacuum. Cancellation can
                            # win while those transactions are released.
                            if not conn.in_transaction and not _lock_running_job(conn, job_id):
                                return
                        if _cache_size() + expected_bytes > MAX_CORPUS_BYTES:
                            conn.execute(
                                "UPDATE documents SET status='failed',error='cache_capacity_reached',extraction_method=? WHERE id=?",
                                (extraction_method, doc_id),
                            )
                            conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (_utcnow(), job_id))
                            conn.commit()
                            continue
                        conn.execute(
                            """UPDATE documents SET status='fetched',url=?,canonical_url=?,title=?,kind=?,body=?,
                               content_hash=?,content_simhash=?,language=?,extraction_method=?,source_class=?,source_basis=?,
                               source_tier=?,pdf_limitations=?,body_truncated=?,
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
                                source_tier,
                                pdf_limitations,
                                body_truncated,
                                retrieved_at,
                                doc_id,
                            ),
                        )
                        _index_document(conn, job_id, doc_id, title, body, fragments)
                else:
                    failure_method = _clean_input(str(fetch_result.get("extraction_method") or ""), limit=40)
                    conn.execute(
                        """UPDATE documents SET status='failed',error=?,
                           extraction_method=CASE WHEN ?<>'' THEN ? ELSE extraction_method END WHERE id=?""",
                        (
                            str(fetch_result.get("error") or "fetch_failed")[:80],
                            failure_method,
                            failure_method,
                            doc_id,
                        ),
                    )
                conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (_utcnow(), job_id))
                conn.commit()


def _on_stop(_signum: int, _frame: Any) -> None:
    global _STOP
    _STOP = True


@contextmanager
def _worker_lock() -> Iterator[None]:
    with (_cache_dir() / "worker.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def run_worker(*, once: bool = False) -> None:
    reject_obsolete_runtime_config()
    _cache_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    with _worker_lock():
        _run_locked_worker(once=once)


def prepare_rollback() -> dict[str, Any]:
    """Move deferred URLs into the document queue understood by older workers."""

    if not _db_path().is_file():
        return _error("j1_store_unavailable")
    reconciled = 0
    queued = 0
    discarded = 0
    try:
        with _worker_lock(), _db() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            jobs = conn.execute(
                """SELECT j.id,j.max_pages FROM jobs j WHERE j.status IN ('queued','running')
                   AND EXISTS (SELECT 1 FROM queries q WHERE q.job_id=j.id) ORDER BY j.id"""
            ).fetchall()
            for job in jobs:
                job_id = job["id"]
                available = max(0, job["max_pages"] - _document_count(conn, job_id))
                moved = 0
                for _ in range(available):
                    if _enqueue_query_candidate(conn, job_id) is None:
                        break
                    moved += 1
                cleared = _clear_query_candidates(conn, job_id)
                # Older workers cannot inspect this budget before dispatching a pending search.
                closed = _close_queries_at_page_limit(conn, job_id) if moved == available else 0
                reconciled += bool(moved or cleared or closed)
                queued += moved
                discarded += cleared
    except BlockingIOError:
        return _error("j1_worker_active")
    except (OSError, sqlite3.Error):
        return _error("j1_store_unavailable")
    return {
        "ok": True,
        "schema": SCHEMA,
        "jobs_reconciled": reconciled,
        "documents_queued": queued,
        "candidates_discarded": discarded,
    }


def _run_locked_worker(*, once: bool) -> None:
    signal.signal(signal.SIGTERM, _on_stop)
    signal.signal(signal.SIGINT, _on_stop)
    with _db() as conn:
        conn.execute("UPDATE jobs SET status='queued' WHERE status='running'")
        conn.execute("UPDATE queries SET status='pending' WHERE status='running'")
        conn.execute("UPDATE documents SET status='pending' WHERE status='running'")
        conn.commit()
    last_prune = 0.0
    last_vin_prune = -60.0
    while not _STOP:
        if time.monotonic() - last_vin_prune >= 60:
            from .j1_vin_store import prune as prune_vin

            prune_vin()
            last_vin_prune = time.monotonic()
        with _db() as conn:
            if time.monotonic() - last_prune > 3600:
                _prune(conn)
                last_prune = time.monotonic()
            job_id = _claim_job(conn)
        if job_id:
            try:
                _work_job(job_id)
            except Exception:  # noqa: BLE001 - errors are reported without URLs or source text.
                if _STOP:
                    # Startup can recover a running job if the stop-time write could not commit.
                    return
                with _db() as conn:
                    failed = conn.execute(
                        "UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id=? AND status='running'",
                        (_WORKER_FAILED, _utcnow(), job_id),
                    ).rowcount
                    if failed:
                        # Close only the phases already claimed under this job.
                        conn.execute(
                            """UPDATE queries SET status=CASE WHEN error=? OR primary_admitted=1 THEN 'partial'
                                                       ELSE 'failed' END,error=?
                               WHERE job_id=? AND status='running'""",
                            (_SEARCH_INTERRUPTED, _WORKER_FAILED, job_id),
                        )
                        conn.execute(
                            "UPDATE documents SET status='failed',error=? WHERE job_id=? AND status='running'",
                            (_WORKER_FAILED, job_id),
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
            try:
                parse_searxng_endpoint(searxng)
            except ValueError:
                return _error("searxng_url_invalid")
        try:
            from .j1_browser import browser_status

            browser = browser_status()
        except Exception:  # noqa: BLE001 - optional browser health must not break static J1.
            browser = {"browser_ready": False, "browser_reason": "browser_status_unavailable"}
        return {
            "ok": True,
            "schema": SCHEMA,
            "cache_ready": True,
            "search_configured": bool(searxng),
            "vin_research": _vin_runtime_status(),
            **browser,
        }
    except (OSError, sqlite3.Error):
        return _error("j1_store_unavailable")


def _vin_runtime_status() -> dict[str, Any]:
    from .j1_vin_store import runtime_status

    return runtime_status()


def main() -> None:
    parser = argparse.ArgumentParser(description="J1 public research worker")
    parser.add_argument("command", choices=("worker", "probe", "prepare-rollback"))
    parser.add_argument("--once", action="store_true", help="Process one queued job and exit")
    args = parser.parse_args()
    if args.command == "worker":
        run_worker(once=args.once)
    else:
        result = prepare_rollback() if args.command == "prepare-rollback" else probe()
        print(json.dumps(result, ensure_ascii=False))
        if not result["ok"]:
            sys.exit(1)


if __name__ == "__main__":
    main()
