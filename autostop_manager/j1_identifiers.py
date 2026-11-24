"""Exact local identifier lookup over J1's redacted public documents."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import suppress
from typing import Literal, NamedTuple

from .j1_fetch import MAX_PUBLIC_PDF_TEXT_CHARS, MAX_TEXT_CHARS, complete_capped_text_end, complete_pdf_fragment_end

QueryMode = Literal["lexical", "dtc", "part_number"]
_MatchTable = Literal["identifier_matches", "identifier_scan"]

# Retained offsets depend on the matcher and on which fragments are safe.
_MATCHER_VERSION = 2
_HORIZONTAL_SPACE = " \t\u00a0\u202f"
_DASH_CHARS = "-‐‑‒–—−"
_HYPHENS = "-‐‑"
_DASHES = re.escape(_DASH_CHARS)
_SPACE = re.escape(_HORIZONTAL_SPACE)
_SEPARATORS = f"[{_SPACE}{_DASHES}]"
_PART_NUMBER = re.compile(rf"[a-zA-Z0-9]+(?:{_SEPARATORS}+[a-zA-Z0-9]+)*")
_DTC = re.compile(r"[PBCU][0-3A-F][0-9A-F]{3}(?:-[0-9A-F]{2})?")
# A dash introducing an explicit population measurement is punctuation.
_DTC_SEPARATOR = (
    rf"(?![{_SPACE}]+[{_DASHES}][{_SPACE}]+[0-9]+(?:[{_SPACE}]*(?:%|/[{_SPACE}]*[0-9])|"
    rf"[{_SPACE}]+(?:of|out[{_SPACE}]+of|из)[{_SPACE}]+[0-9]))[{_SPACE}]*[{_DASHES}][{_SPACE}]*"
)
_SPACE_LINK = re.compile(rf"([a-zA-Z0-9]+)[{_SPACE}]+(?=([a-zA-Z0-9]+))")
_PROSE_WORDS = frozenset(
    {
        "and",
        "are",
        "available",
        "for",
        "fits",
        "from",
        "in",
        "is",
        "kit",
        "new",
        "no",
        "not",
        "of",
        "oe",
        "oem",
        "old",
        "only",
        "or",
        "part",
        "pn",
        "qty",
        "ref",
        "replaced",
        "see",
        "sku",
        "superseded",
        "the",
        "to",
        "use",
        "used",
        "via",
        "was",
        "with",
    }
)
_FIELD_PROSE = "|".join(sorted(_PROSE_WORDS - {"oe", "oem", "pn", "ref", "sku"}))
_PART_FIELD = re.compile(
    rf"\b(?i:part[{_SPACE}]+(?:number|no\.?)|(?:OE|OEM)[{_SPACE}]+(?:number|no\.?)|артикул|номер[{_SPACE}]+детали)"
    rf"[{_SPACE}]*[:#=][{_SPACE}]*(?P<code>[a-zA-Z0-9]+(?:{_SEPARATORS}+[a-zA-Z0-9]+)*?)"
    rf"(?=[{_SPACE}]*(?:[.!?,;:/\r\n(\)\[\]]|$)|[{_SPACE}]+(?i:{_FIELD_PROSE})\b)"
)


def normalize(query: str, mode: QueryMode) -> str | None:
    """Normalize a whole requested code, retaining DTC subcodes."""
    raw = query.strip()
    if not _PART_NUMBER.fullmatch(raw):
        return None
    if mode == "dtc":
        code = re.sub(rf"[{_SPACE}]*[{_DASHES}][{_SPACE}]*", "-", raw).upper()
        return code if _DTC.fullmatch(code) else None
    code = re.sub(_SEPARATORS, "", raw).upper()
    return code if 2 <= len(code) <= 64 else None


def _ascii_alnum(text: str, position: int) -> bool:
    return 0 <= position < len(text) and text[position].isascii() and text[position].isalnum()


def _dash_connected(text: str, boundary: int, direction: int) -> bool:
    position = boundary
    while 0 <= position < len(text) and text[position] in _HORIZONTAL_SPACE:
        position += direction
    if not 0 <= position < len(text) or text[position] not in _DASH_CHARS:
        return False
    dash = position
    position += direction
    while 0 <= position < len(text) and text[position] in _HORIZONTAL_SPACE + _HYPHENS:
        position += direction
    # A typographic dash surrounded by spaces also separates ordinary prose.
    if text[dash] not in _HYPHENS and boundary != dash and abs(position - dash) > 1:
        return False
    return _ascii_alnum(text, position)


def _word_at(text: str, position: int) -> str:
    start, end = position, position + 1
    while _ascii_alnum(text, start - 1):
        start -= 1
    while _ascii_alnum(text, end):
        end += 1
    return text[start:end]


def _short_code_word(word: str, max_uppercase: int) -> bool:
    return len(word) <= (max_uppercase if word.isupper() else 2) and word.casefold() not in _PROSE_WORDS


def _space_connected(text: str, boundary: int, direction: int) -> bool:
    if not 0 <= boundary < len(text) or text[boundary] not in _HORIZONTAL_SPACE:
        return False
    neighbor = boundary
    while 0 <= neighbor < len(text) and text[neighbor] in _HORIZONTAL_SPACE:
        neighbor += direction
    if not _ascii_alnum(text, neighbor):
        return False
    left = _word_at(text, boundary - direction)
    right = _word_at(text, neighbor)
    # A numeric component can connect to digits, a short catalog component,
    # or a mixed letter/digit component. Two complete mixed identifiers only
    # separated by whitespace may be separate part numbers.
    if left.isdigit() or right.isdigit():
        other = right if left.isdigit() else left
        return not other.isalpha() or _short_code_word(other, 4)
    if left.isalpha() != right.isalpha():
        return _short_code_word(left if left.isalpha() else right, 3)
    return False


def _connected_part(text: str, boundary: int, direction: int) -> bool:
    return _dash_connected(text, boundary, direction) or _space_connected(text, boundary, direction)


def _mixed_component(value: str) -> bool:
    return not value.isalpha() and not value.isdigit()


class _PartPattern(NamedTuple):
    candidates: re.Pattern[str]

    def search(self, text: str) -> re.Match[str] | None:
        fields: Iterator[re.Match[str]] | None = None
        field = None
        for match in self.candidates.finditer(text):
            if fields is None:
                fields = _PART_FIELD.finditer(text)
                field = next(fields, None)
            while field is not None and field.end("code") <= match.start():
                field = next(fields, None)
            if field is not None and field.start("code") < match.end():
                if match.span() == field.span("code"):
                    return match
                continue
            if _connected_part(text, match.start() - 1, -1) or _connected_part(text, match.end(), 1):
                continue
            # Without a catalog field or a dash, two complete alphanumeric
            # tokens can be separate stock numbers.
            if any(_mixed_component(left) and _mixed_component(right) for left, right in _SPACE_LINK.findall(match[0])):
                continue
            return match
        return None


IdentifierPattern = re.Pattern[str] | _PartPattern


def match_pattern(code: str, mode: QueryMode) -> IdentifierPattern:
    """Match a normalized identifier in the original text."""
    if mode == "dtc":
        base, _, subcode = code.partition("-")
        body = f"{base}{_DTC_SEPARATOR}{subcode}" if subcode else base
        continuation = rf"{_DTC_SEPARATOR}(?ai:[A-F0-9]{{2,}})(?!\w)"
        return re.compile(rf"(?<!\w)(?<!\w[{_DASHES}]){body}(?!\w|{continuation})", re.I)
    # Check both ends against the components of a separated catalog number.
    body = f"{_SEPARATORS}*".join(code)
    return _PartPattern(re.compile(rf"(?<!\w)(?ai:{body})(?!\w)"))


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS identifier_queries (
           job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
           mode TEXT NOT NULL, code TEXT NOT NULL, document_count INTEGER NOT NULL,
           matcher_version INTEGER NOT NULL DEFAULT 0,
           PRIMARY KEY(job_id, mode)
        ) WITHOUT ROWID"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS identifier_matches (
           job_id TEXT NOT NULL, mode TEXT NOT NULL,
           document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
           page INTEGER NOT NULL, title_only INTEGER NOT NULL, start INTEGER NOT NULL,
           FOREIGN KEY(job_id, mode) REFERENCES identifier_queries(job_id, mode) ON DELETE CASCADE,
           PRIMARY KEY(job_id, mode, document_id, page)
        ) WITHOUT ROWID"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS identifier_matches_document ON identifier_matches(document_id)")


def _positions(
    conn: sqlite3.Connection, document: sqlite3.Row, pattern: IdentifierPattern
) -> Iterator[tuple[int, int, int]]:
    body = document["body"]
    fragments = (
        conn.execute(
            "SELECT page,start,end,fragment_kind FROM document_fragments WHERE document_id=? ORDER BY page",
            (document["id"],),
        ).fetchall()
        if body
        else []
    )
    found = False
    capped_tail = len(body.rstrip()) if document["body_truncated"] or len(body) == MAX_TEXT_CHARS else None
    for page, start, end, kind in fragments or [(0, 0, len(body), document["kind"])]:
        if kind == "pdf_page" or (kind == "pdf" and end in {MAX_TEXT_CHARS, MAX_PUBLIC_PDF_TEXT_CHARS}):
            end = complete_pdf_fragment_end(body, start, end)
        elif capped_tail is not None and end >= capped_tail:
            end = complete_capped_text_end(body, start, end)
        # Search a slice so a section boundary is an actual word boundary.
        match = pattern.search(body[start:end])
        if match:
            found = True
            yield page, 0, start + match.start()
    if not found:
        title = document["title"]
        if len(title) == 200:
            title = title[: complete_capped_text_end(title, 0, len(title))]
        if pattern.search(title):
            yield 0, 1, 0


def _document_count(conn: sqlite3.Connection, job_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM documents WHERE job_id=? AND status IN ('fetched','duplicate')", (job_id,)
    ).fetchone()[0]


def _cached(conn: sqlite3.Connection, job_id: str, mode: QueryMode, code: str, count: int) -> bool:
    cached = conn.execute(
        "SELECT code,document_count,matcher_version FROM identifier_queries WHERE job_id=? AND mode=?", (job_id, mode)
    ).fetchone()
    return cached is not None and tuple(cached) == (code, count, _MATCHER_VERSION)


def query_table(conn: sqlite3.Connection, job_id: str, mode: QueryMode, code: str) -> tuple[_MatchTable, int | None]:
    """Read cached offsets or scan this snapshot into a connection-local table."""
    count = _document_count(conn, job_id)
    if _cached(conn, job_id, mode, code, count):
        return "identifier_matches", None
    conn.execute("CREATE TEMP TABLE identifier_scan AS SELECT * FROM identifier_matches WHERE 0")
    pattern = match_pattern(code, mode)
    documents = conn.execute(
        """SELECT d.id,d.title,d.kind,d.body,d.body_truncated FROM documents d
           LEFT JOIN documents p ON p.id=d.duplicate_of
           WHERE d.job_id=? AND (
               d.status='fetched' OR (d.status='duplicate' AND d.duplicate_kind='exact' AND d.title<>p.title)
           )""",
        (job_id,),
    )
    conn.executemany(
        "INSERT INTO identifier_scan(job_id,mode,document_id,page,title_only,start) VALUES(?,?,?,?,?,?)",
        (
            (job_id, mode, document["id"], page, title_only, offset)
            for document in documents
            for page, title_only, offset in _positions(conn, document, pattern)
        ),
    )
    return "identifier_scan", count


def cache_query(conn: sqlite3.Connection, job_id: str, mode: QueryMode, code: str, count: int) -> None:
    """Publish offsets after reading the response, if the corpus still matches."""
    # No result depends on publishing a cache entry. Let a competing writer
    # proceed and keep the reader's snapshot even if the cache cannot be saved.
    with suppress(OSError, sqlite3.Error):
        conn.execute("PRAGMA busy_timeout=0")
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
                return
            # Completed documents only append until this whole job is evicted.
            if _document_count(conn, job_id) != count or _cached(conn, job_id, mode, code, count):
                return
            conn.execute("DELETE FROM identifier_queries WHERE job_id=? AND mode=?", (job_id, mode))
            conn.execute(
                "INSERT INTO identifier_queries(job_id,mode,code,document_count,matcher_version) VALUES(?,?,?,?,?)",
                (job_id, mode, code, count, _MATCHER_VERSION),
            )
            conn.execute(
                """INSERT INTO identifier_matches(job_id,mode,document_id,page,title_only,start)
                   SELECT job_id,mode,document_id,page,title_only,start FROM identifier_scan"""
            )
