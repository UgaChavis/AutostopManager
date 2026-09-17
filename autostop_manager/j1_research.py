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
from .j1_sources import classify_source

SCHEMA = "autostop.j1.research.v1"
REPORT_SCHEMA = "autostop.j1.report.v1"
MAX_QUERIES = 30
MAX_PAGES = 300
MAX_BROWSER_PAGES = 20
MAX_DOCUMENT_CHARS = 50_000
MAX_CORPUS_BYTES = 250 * 1024 * 1024
RETENTION_DAYS = 7
_IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
_URL_TOKEN = re.compile(r"(?i)(?<!\w)[a-z][a-z0-9+.-]*://[^\s<>]+")
_WORD = re.compile(r"[^\W_]{3,}", flags=re.UNICODE)
_STOP = False

# The automotive profile remains a small extension of the generic public-web
# corpus.  It is deliberately bounded more tightly because its automatic plan
# is intended to be reviewed as one compact evidence bundle.
GENERAL_PROFILE = "general"
AUTOMOTIVE_PROFILE = "automotive"
AUTOMOTIVE_MAX_QUERIES = 12
AUTOMOTIVE_MAX_PAGES = 60
AUTOMOTIVE_MAX_MANUAL_QUERIES = 4
_AUTOMOTIVE_CONTEXT_LIMITS = {
    "make": 80,
    "model": 100,
    "year": 4,
    "engine": 100,
    "system": 120,
    "symptom": 500,
    "dtc": 120,
    "part_number": 100,
}
_AUTOMOTIVE_DTC = re.compile(r"^[PBCU][0-3A-F][0-9A-F]{3}$", re.IGNORECASE)
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
    present = _schema_columns(conn, "documents")
    for name, definition in required.items():
        if name not in present:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {name} {definition}")
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
    """Add profile metadata without changing retained generic jobs."""

    required = {
        "profile": "TEXT NOT NULL DEFAULT 'general'",
        "automotive_context": "TEXT NOT NULL DEFAULT ''",
    }
    present = _schema_columns(conn, "jobs")
    for name, definition in required.items():
        if name not in present:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")


def _migrate_queries(conn: sqlite3.Connection) -> None:
    """Keep a non-sensitive intent label for automatic automotive queries."""

    if "intent" not in _schema_columns(conn, "queries"):
        conn.execute("ALTER TABLE queries ADD COLUMN intent TEXT NOT NULL DEFAULT 'manual'")


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, objective TEXT NOT NULL, max_pages INTEGER NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '', profile TEXT NOT NULL DEFAULT 'general',
            automotive_context TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at);
        CREATE TABLE IF NOT EXISTS queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
            query TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            provider TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', intent TEXT NOT NULL DEFAULT 'manual',
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
        """
    )
    _migrate_jobs(conn)
    _migrate_queries(conn)
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
        cleaned = _clean_input(raw, limit=_AUTOMOTIVE_CONTEXT_LIMITS[key])
        if not cleaned:
            continue
        if not _input_is_safe(cleaned):
            return None, "automotive_context_invalid_or_sensitive"
        if key == "dtc":
            codes = [item for item in re.split(r"[,;/\s]+", cleaned.upper()) if item]
            if not codes or len(codes) > 8 or not all(_AUTOMOTIVE_DTC.fullmatch(code) for code in codes):
                return None, "automotive_context_invalid"
            cleaned = ", ".join(codes)
        context[key] = cleaned
    if not context:
        return None, "automotive_context_invalid"
    has_identity = any(context.get(field) for field in ("make", "model", "engine", "part_number"))
    has_subject = any(context.get(field) for field in ("system", "symptom", "dtc", "part_number"))
    if not has_identity or not has_subject:
        return None, "automotive_context_incomplete"
    return context, None


def _context_phrase(context: dict[str, str], *fields: str) -> str:
    values: list[str] = []
    seen: set[str] = set()
    for field in fields:
        value = context.get(field, "")
        if value and value.casefold() not in seen:
            values.append(value)
            seen.add(value.casefold())
    return _clean_input(" ".join(values), limit=500)


def _automotive_query_plan(context: dict[str, str]) -> list[tuple[str, str]]:
    """Build a balanced, deterministic public-search plan without network I/O."""

    identity = _context_phrase(context, "make", "model", "year", "engine")
    technical_identity = _context_phrase(context, "engine", "make", "model") or identity
    diagnostic = _context_phrase(context, "system", "symptom", "dtc")
    catalog_subject = _context_phrase(context, "part_number", "system", "engine") or identity
    subject = diagnostic or catalog_subject
    templates = (
        ("official_bulletin_en", f"{identity} {subject} OEM technical service bulletin"),
        ("technical_diagnosis_en", f"{technical_identity} {subject} diagnostic procedure"),
        ("catalog_fitment_en", f"{catalog_subject} OEM parts catalog fitment"),
        ("owner_experience_en", f"{identity} {subject} owner forum repair experience"),
        ("official_bulletin_ru", f"{identity} {subject} бюллетень производителя"),
        ("technical_diagnosis_ru", f"{technical_identity} {subject} диагностика техническая документация"),
        ("catalog_fitment_ru", f"{catalog_subject} каталог оригинальных деталей применимость"),
        ("owner_experience_ru", f"{identity} {subject} форум владельцев опыт ремонта"),
        ("alternative_causes_en", f"{technical_identity} {subject} possible causes technical"),
        ("alternative_causes_ru", f"{technical_identity} {subject} возможные причины техническая информация"),
        ("technical_context_en", f"{technical_identity} {subject} service manual technical documentation"),
        ("technical_context_ru", f"{technical_identity} {subject} руководство по ремонту техническое описание"),
    )
    planned: list[tuple[str, str]] = []
    seen: set[str] = set()
    for intent, raw in templates:
        query = _clean_input(raw, limit=500)
        normalized = query.casefold()
        if query and _input_is_safe(query) and normalized not in seen:
            planned.append((query, intent))
            seen.add(normalized)
    return planned[:AUTOMOTIVE_MAX_QUERIES]


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
        if query.casefold() not in seen:
            planned.append((query, intent))
            seen.add(query.casefold())
        if len(planned) >= AUTOMOTIVE_MAX_QUERIES:
            break
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
            browser_attempts = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE job_id=? AND extraction_method='browser_dom'", (job_id,)
            ).fetchone()[0]
            query_plan = [
                {"query": item["query"], "intent": item["intent"] or "manual"}
                for item in conn.execute("SELECT query,intent FROM queries WHERE job_id=? ORDER BY id", (job_id,))
            ]
            stored_queries = [item["query"] for item in query_plan]
            profile = row["profile"] or GENERAL_PROFILE
            context = _stored_automotive_context(row["automotive_context"])
            query_limit = AUTOMOTIVE_MAX_QUERIES if profile == AUTOMOTIVE_PROFILE else MAX_QUERIES
            return {
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
                "queries_total": queries["total"],
                "queries_done": queries["done"] or 0,
                "queries_failed": queries["failed"] or 0,
                "page_failures": failures,
                "search_failures": search_failures,
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
                    "search_providers": _provider_coverage(conn, job_id),
                },
                # Suggestions do not consume budget or create queries. They are
                # capped by the remaining explicit add_queries capacity.
                "query_suggestions": (
                    []
                    if profile == AUTOMOTIVE_PROFILE
                    else _safe_query_suggestions(row["objective"], stored_queries)[
                        : max(0, query_limit - queries["total"])
                    ]
                ),
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
    fields = """d.id,d.url,d.canonical_url,d.title,d.source,d.source_class,d.source_basis,d.source_tier,
                       d.search_snippet,d.search_rank,d.search_engines,d.language,
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
                """SELECT d.url,d.canonical_url,d.title,d.kind,d.body,d.source,d.source_class,d.source_basis,d.source_tier,
                          d.search_snippet,d.search_rank,d.search_engines,d.language,
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
            }
    except (OSError, sqlite3.Error):
        return _error("j1_store_unavailable", job_id=job_id)


def _report_error(code: str, *, job_id: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "schema": REPORT_SCHEMA, "error": {"code": code}}
    if job_id:
        result["job_id"] = job_id
    return result


def _report_source_tier(stored_tier: str, source_class: str) -> tuple[str, str]:
    """Use the discovery registry tier, with a safe fallback for old jobs."""

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


def _context_matches(row: sqlite3.Row, context: dict[str, str]) -> tuple[str, list[str]]:
    """Describe literal context overlap, never a fitment or diagnostic conclusion."""

    if not context:
        return "not_assessed", []
    haystack = " ".join((str(row["title"] or ""), str(row["body"] or ""))).casefold()
    matched = [field for field, value in context.items() if value.casefold() in haystack]
    matched_set = set(matched)
    if (
        ("model" in matched_set and "engine" in matched_set)
        or ("part_number" in matched_set and {"make", "model"}.issubset(matched_set))
        or ({"model", "year"}.issubset(matched_set) and "engine" in matched_set)
    ):
        return "exact", matched
    if matched_set & {"model", "engine", "part_number"}:
        return "analog", matched
    if matched_set:
        return "general", matched
    return "unknown", []


def _evidence_confidence(tier: str, applicability: str) -> str:
    if tier in {"A", "B"} and applicability in {"exact", "analog"}:
        return "moderate"
    if tier in {"B", "C", "D"} or applicability == "general":
        return "low"
    return "insufficient"


def _report_source_item(row: sqlite3.Row, context: dict[str, str]) -> dict[str, Any]:
    tier, tier_basis = _report_source_tier(str(row["source_tier"] or ""), row["source_class"])
    applicability, matched = _context_matches(row, context)
    return {
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
        "applicability": applicability,
        "matched_context_fields": matched,
        "confidence": _evidence_confidence(tier, applicability),
        "retrieved_at": row["retrieved_at"] or None,
    }


def _report_source_sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
    tier_order = {"A": 0, "B": 1, "C": 2, "D": 3, "unrated": 4}
    applicability_order = {"exact": 0, "analog": 1, "general": 2, "unknown": 3, "not_assessed": 4}
    return (
        tier_order.get(str(item["source_tier"]), 5),
        applicability_order.get(str(item["applicability"]), 5),
        str(item["url"]),
    )


def _report_excerpt(value: str) -> str:
    return _clean_input(redact_sensitive(value, limit=320), limit=280)


def _source_sentences(body: str) -> Iterator[str]:
    for sentence in re.split(r"(?<=[.!?])\s+|[\r\n]+", body):
        compact = _report_excerpt(sentence)
        if len(compact) >= 24:
            yield compact


def _alternative_cause_mentions(rows: list[sqlite3.Row], sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return cited source mentions only; no automatic diagnosis is inferred."""

    found: list[dict[str, Any]] = []
    for row, source in zip(rows, sources, strict=True):
        for sentence in _source_sentences(str(row["body"] or "")):
            if not any(marker in sentence.casefold() for marker in _CAUSE_MARKERS):
                continue
            found.append(
                {
                    "status": "unverified_source_mention",
                    "source_document_id": source["document_id"],
                    "source_url": source["url"],
                    "source_tier": source["source_tier"],
                    "applicability": source["applicability"],
                    "confidence": source["confidence"],
                    "excerpt": sentence,
                }
            )
            break
        if len(found) >= 3:
            break
    return found


def _frequency_measurements(rows: list[sqlite3.Row], sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose only explicit population measurements from stronger evidence tiers."""

    measurements: list[dict[str, Any]] = []
    for row, source in zip(rows, sources, strict=True):
        if source["source_tier"] not in {"A", "B"} or source["applicability"] not in {"exact", "analog"}:
            continue
        for sentence in _source_sentences(str(row["body"] or "")):
            match = _FREQUENCY_COUNT.search(sentence) or _FREQUENCY_PERCENT.search(sentence)
            if match is None:
                continue
            measurements.append(
                {
                    "status": "reported_source_measurement",
                    "source_document_id": source["document_id"],
                    "source_url": source["url"],
                    "source_tier": source["source_tier"],
                    "applicability": source["applicability"],
                    "measurement": match.group(0),
                    "excerpt": sentence,
                }
            )
            break
        if len(measurements) >= 3:
            break
    return measurements


def _overall_confidence(sources: list[dict[str, Any]]) -> str:
    direct = [
        source for source in sources if source["source_tier"] in {"A", "B"} and source["applicability"] == "exact"
    ]
    if len(direct) >= 2 and _source_domains(direct) >= 2:
        return "high"
    if direct or any(source["source_tier"] in {"A", "B"} and source["applicability"] == "analog" for source in sources):
        return "moderate"
    if sources:
        return "low"
    return "insufficient"


def _source_domains(sources: list[dict[str, Any]]) -> int:
    domains: set[str] = set()
    for source in sources:
        try:
            hostname = (urlsplit(str(source["url"])).hostname or "").casefold()
        except ValueError:
            hostname = ""
        if hostname:
            domains.add(hostname)
    return len(domains)


def _report_limitations(
    *,
    status: str,
    sources: list[dict[str, Any]],
    unavailable: list[dict[str, Any]],
    frequency_measurements: list[dict[str, Any]],
) -> list[dict[str, str]]:
    limitations: list[dict[str, str]] = []
    if status != "completed":
        limitations.append({"code": "research_incomplete", "detail": "job_not_completed"})
    if not sources:
        limitations.append({"code": "no_retrieved_sources", "detail": "no_fetched_public_evidence"})
    if not any(source["source_tier"] == "A" for source in sources):
        limitations.append({"code": "no_official_source", "detail": "no_tier_a_source_retrieved"})
    if sources and not any(source["applicability"] == "exact" for source in sources):
        limitations.append(
            {"code": "no_exact_context_match", "detail": "fitment_and_diagnosis_require_separate_verification"}
        )
    if not frequency_measurements:
        limitations.append({"code": "frequency_not_measured", "detail": "no_qualified_population_measurement"})
    if unavailable:
        limitations.append({"code": "source_access_limited", "detail": "one_or_more_public_sources_unavailable"})
    limitations.append({"code": "no_automatic_diagnosis", "detail": "source_text_requires_human_review"})
    return limitations


def research_report(job_id: str) -> dict[str, Any]:
    """Build a read-only evidence ledger from a retained J1 research job."""

    if not _valid_id(job_id):
        return _report_error("job_id_invalid")
    try:
        with _db() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                return _report_error("job_not_found", job_id=job_id)
            context = _stored_automotive_context(job["automotive_context"])
            rows = conn.execute(
                """SELECT id,url,canonical_url,title,body,source_class,source_basis,source_tier,
                          search_snippet,search_rank,retrieved_at
                   FROM documents WHERE job_id=? AND status='fetched'""",
                (job_id,),
            ).fetchall()
            pairs = [(row, _report_source_item(row, context)) for row in rows]
            pairs.sort(key=lambda pair: _report_source_sort_key(pair[1]))
            pairs = pairs[:20]
            report_rows = [pair[0] for pair in pairs]
            sources = [pair[1] for pair in pairs]
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
                    "reason": row["error"] or "fetch_failed",
                    "count": row["count"],
                    "source_class": row["source_class"] or "unknown",
                    "source_tier": row["source_tier"] or "unclassified",
                }
                for row in conn.execute(
                    """SELECT error,source_class,source_tier,COUNT(*) AS count FROM documents
                       WHERE job_id=? AND status='failed'
                       GROUP BY error,source_class,source_tier ORDER BY count DESC,error LIMIT 10""",
                    (job_id,),
                )
            ]
    except (OSError, sqlite3.Error):
        return _report_error("j1_store_unavailable", job_id=job_id)

    frequency_measurements = _frequency_measurements(report_rows, sources)
    confirmed = [
        {**source, "evidence_kind": "strong_context_source"}
        for source in sources
        if source["source_tier"] == "A" and source["applicability"] == "exact"
    ][:5]
    confirmed_ids = {source["document_id"] for source in confirmed}
    hypotheses = [
        {**source, "evidence_kind": "requires_source_review"}
        for source in sources
        if source["document_id"] not in confirmed_ids
    ][:10]
    tier_counts = {
        tier: sum(1 for source in sources if source["source_tier"] == tier) for tier in ("A", "B", "C", "D", "unrated")
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
        sources=sources,
        unavailable=unavailable,
        frequency_measurements=frequency_measurements,
    )
    return {
        "ok": True,
        "schema": REPORT_SCHEMA,
        "job_id": job_id,
        "status": job["status"],
        "profile": job["profile"] or GENERAL_PROFILE,
        "automotive_context": context or None,
        "report": {
            "evidence_confidence": {
                "level": _overall_confidence(sources),
                "retrieved_sources": len(sources),
                "independent_domains": _source_domains(sources),
                "source_tiers": tier_counts,
            },
            "frequency": frequency,
            "confirmed": confirmed,
            "hypotheses": hypotheses,
            "alternative_causes": _alternative_cause_mentions(report_rows, sources),
            "sources": sources,
            "duplicates": duplicates,
            "unavailable": unavailable,
            "limitations": limitations,
            "read_only": True,
            "fitment_confirmed": False,
            "crm_written": False,
        },
    }


def research_add_queries(job_id: str, queries: list[str]) -> dict[str, Any]:
    if not _valid_id(job_id):
        return _error("job_id_invalid")
    clean_queries = _validated_queries(queries)
    if clean_queries is None:
        return _error("queries_invalid_or_sensitive", job_id=job_id)
    try:
        with _db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute("SELECT status,profile FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                return _error("job_not_found", job_id=job_id)
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
            if job["status"] == "completed":
                busy = conn.execute(
                    "SELECT 1 FROM jobs WHERE status IN ('queued','running') AND id<>? LIMIT 1", (job_id,)
                ).fetchone()
                if busy:
                    return _error("j1_busy", job_id=job_id)
            conn.executemany(
                "INSERT INTO queries(job_id,query,intent) VALUES(?,?,?)",
                ((job_id, item, "manual_follow_up") for item in additions),
            )
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
                browser_attempts = conn.execute(
                    "SELECT COUNT(*) FROM documents WHERE job_id=? AND extraction_method='browser_dom'", (job_id,)
                ).fetchone()[0]
                conn.execute("UPDATE documents SET status='running' WHERE id=?", (pending_doc["id"],))
                conn.commit()
                doc_id, url = pending_doc["id"], pending_doc["url"]
                allow_browser = browser_attempts < MAX_BROWSER_PAGES
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
                    (
                        title,
                        source_class,
                        source_basis,
                        source_tier,
                        snippet,
                        search_rank,
                        search_engines,
                    ) = _discovery_metadata(row, url, canonical_url)
                    inserted = conn.execute(
                        """INSERT OR IGNORE INTO documents(
                               id,job_id,url,canonical_url,title,source,source_class,source_basis,source_tier,
                               search_snippet,search_rank,search_engines,created_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            uuid4().hex,
                            job_id,
                            url,
                            canonical_url,
                            title,
                            _clean_input(str(row.get("source") or ""), limit=40),
                            source_class,
                            source_basis,
                            source_tier,
                            snippet,
                            search_rank,
                            search_engines,
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
                fetch_result = fetch_document(url, allow_browser=allow_browser)
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
                    source_class, source_basis, source_tier = _classify_source(canonical_url or stored_url, title, kind)
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
                               source_tier=?,
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
                                "UPDATE documents SET status='failed',error='cache_capacity_reached',extraction_method=? WHERE id=?",
                                (extraction_method, doc_id),
                            )
                            conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (_utcnow(), job_id))
                            conn.commit()
                            continue
                        conn.execute(
                            """UPDATE documents SET status='fetched',url=?,canonical_url=?,title=?,kind=?,body=?,
                               content_hash=?,content_simhash=?,language=?,extraction_method=?,source_class=?,source_basis=?,
                               source_tier=?,
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
