"""Exact local identifier lookup over J1's redacted public documents."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from typing import Literal, NamedTuple

QueryMode = Literal["lexical", "dtc", "part_number"]

_HORIZONTAL_SPACE = " \t\u00a0\u202f"
_DASH_CHARS = "-‐‑‒–—−"
_HYPHENS = "-‐‑"
_DASHES = re.escape(_DASH_CHARS)
_SPACE = re.escape(_HORIZONTAL_SPACE)
_SEPARATORS = f"[{_SPACE}{_DASHES}]"
_PART_NUMBER = re.compile(rf"[a-zA-Z0-9]+(?:{_SEPARATORS}+[a-zA-Z0-9]+)*")
_DTC = re.compile(r"[PBCU][0-3A-F][0-9A-F]{3}(?:-[0-9A-F]{2})?")


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


def _connected_part(text: str, boundary: int, direction: int) -> bool:
    position = boundary
    while 0 <= position < len(text) and text[position] in _HORIZONTAL_SPACE:
        position += direction
    if not 0 <= position < len(text) or text[position] not in _DASH_CHARS:
        return False
    dash = position
    position += direction
    while 0 <= position < len(text) and text[position] in _HORIZONTAL_SPACE:
        position += direction
    # A typographic dash surrounded by spaces also separates ordinary prose.
    if text[dash] not in _HYPHENS and boundary != dash and abs(position - dash) > 1:
        return False
    return 0 <= position < len(text) and text[position].isascii() and text[position].isalnum()


class _PartPattern(NamedTuple):
    candidates: re.Pattern[str]

    def search(self, text: str) -> re.Match[str] | None:
        for match in self.candidates.finditer(text):
            if not _connected_part(text, match.start() - 1, -1) and not _connected_part(text, match.end(), 1):
                return match
        return None


IdentifierPattern = re.Pattern[str] | _PartPattern


def match_pattern(code: str, mode: QueryMode) -> IdentifierPattern:
    """Match a normalized identifier in the original text."""
    if mode == "dtc":
        body = code.replace("-", rf"[{_SPACE}]*[{_DASHES}][{_SPACE}]*")
        continuation = rf"[{_SPACE}]*[{_DASHES}][{_SPACE}]*(?ai:[A-F0-9]{{2}})(?!\w)"
        return re.compile(rf"(?<!\w)(?<!\w[{_DASHES}])(?ai:{body})(?!\w|{continuation})")
    # Horizontal separators can be internal to an article. Check both ends
    # of each candidate against the surrounding dash-connected components.
    body = f"{_SEPARATORS}*".join(code)
    return _PartPattern(re.compile(rf"(?<!\w)(?ai:{body})(?!\w)"))


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS identifier_queries (
           job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
           mode TEXT NOT NULL, code TEXT NOT NULL, document_count INTEGER NOT NULL,
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
            "SELECT page,start,end FROM document_fragments WHERE document_id=? ORDER BY page",
            (document["id"],),
        ).fetchall()
        if body
        else []
    )
    found = False
    for page, start, end in fragments or [(0, 0, len(body))]:
        # Search a slice so a section boundary is an actual word boundary.
        match = pattern.search(body[start:end])
        if match:
            found = True
            yield page, 0, start + match.start()
    if not found and pattern.search(document["title"]):
        yield 0, 1, 0


def index_query(conn: sqlite3.Connection, job_id: str, mode: QueryMode, code: str) -> None:
    """Materialize the last exact lookup per mode, storing offsets and no text.

    A corpus only appends completed documents until its entire job is evicted.
    A new completion invalidates the cached lookup. Retained jobs use the same
    paths and do not have to fetch their documents again.
    """
    count = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE job_id=? AND status IN ('fetched','duplicate')", (job_id,)
    ).fetchone()[0]
    cached = conn.execute(
        "SELECT code,document_count FROM identifier_queries WHERE job_id=? AND mode=?", (job_id, mode)
    ).fetchone()
    if cached is not None and (cached["code"], cached["document_count"]) == (code, count):
        return

    conn.execute("DELETE FROM identifier_queries WHERE job_id=? AND mode=?", (job_id, mode))
    conn.execute(
        "INSERT INTO identifier_queries(job_id,mode,code,document_count) VALUES(?,?,?,?)",
        (job_id, mode, code, count),
    )
    pattern = match_pattern(code, mode)
    documents = conn.execute(
        """SELECT d.id,d.title,d.body FROM documents d
           LEFT JOIN documents p ON p.id=d.duplicate_of
           WHERE d.job_id=? AND (
               d.status='fetched' OR (d.status='duplicate' AND d.duplicate_kind='exact' AND d.title<>p.title)
           )""",
        (job_id,),
    )
    conn.executemany(
        "INSERT INTO identifier_matches(job_id,mode,document_id,page,title_only,start) VALUES(?,?,?,?,?,?)",
        (
            (job_id, mode, document["id"], page, title_only, offset)
            for document in documents
            for page, title_only, offset in _positions(conn, document, pattern)
        ),
    )
