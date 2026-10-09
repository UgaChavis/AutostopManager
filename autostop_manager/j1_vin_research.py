"""Independent VIN web research: scoped collection and agent-assessed claims.

The collector never calls a decoder or an LLM. Evidence links and quotations
are checked mechanically; automotive interpretation remains the caller's job.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from contextlib import suppress
from functools import wraps
import hashlib
import html
import json
import math
import re
import sqlite3
from typing import Any
import unicodedata
from urllib.parse import unquote
from uuid import uuid4

from . import j1_fetch as fetch
from . import j1_vin_store as store
from .j1_vin_network import VinScope, normalize_vin, safe_query

_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FACT_FIELDS = frozenset(store.DEFAULT_FIELDS) | {
    "manufacturer",
    "generation",
    "production_year",
    "country_of_manufacture",
    "engine_code",
    "engine_family",
    "cylinder_count",
    "fuel_type",
    "gear_count",
    "steering",
    "color",
}
_CLAIM_KEYS = frozenset(
    {
        "claim_id",
        "field",
        "value",
        "unit",
        "document_id",
        "document_revision",
        "quote",
        "page",
        "relationship",
        "derivation",
        "reasoning",
        "basis_claim_ids",
        "match_evidence_id",
        "support",
    }
)


def _safe_api(function: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    @wraps(function)
    def call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        candidate = kwargs.get("job_id", args[0] if args else "")
        job_id = candidate if isinstance(candidate, str) and store.IDENTIFIER.fullmatch(candidate) else ""
        try:
            store.prune()
            return function(*args, **kwargs)
        except store.VinJobError as exc:
            if job_id and exc.code in {"vin_job_expired", "vin_ephemeral_state_lost"}:
                _mark_lost(job_id, exc.code)
            return store.error(exc.code, job_id)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            return store.error("vin_store_unavailable", job_id)

    return call


def _mark_lost(job_id: str, code: str) -> None:
    with suppress(OSError, sqlite3.Error):
        store.set_stub(job_id, "failed", code)


def _check_key(key: str) -> None:
    if not isinstance(key, str) or not _KEY.fullmatch(key) or fetch.contains_sensitive(key):
        raise store.VinJobError("idempotency_key_invalid")


def scope(
    current: dict[str, Any], *, deadline_at: float | None = None, active_check: Callable[[], bool] | None = None
) -> VinScope:
    engines = tuple(engine for engine in store.ENGINES if engine not in current["blocked_engines"])
    return VinScope(
        vin=current["vin"],
        job_id=current["job_id"],
        expires_at=min(current["expires_at"], deadline_at) if deadline_at is not None else current["expires_at"],
        allowed_engines=engines,
        active_check=active_check,
    )


def _safe_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKC", value[: max(0, limit * 2)])
    # Decode escaped identifiers before redaction. Deeply layered content is
    # discarded after a bounded number of passes rather than kept in the FTS.
    for _ in range(16):
        decoded = html.unescape(unquote(text))
        if decoded == text:
            break
        text = decoded
    else:
        return "[redacted encoded source text]"
    text = "".join(char for char in text if unicodedata.category(char) != "Cf")
    clean = fetch.redact_sensitive(text, limit=limit)
    return "[redacted source text]" if fetch.contains_sensitive(clean) else clean


def _budget(current: dict[str, Any], conn: sqlite3.Connection) -> dict[str, Any]:
    queries = conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0]
    documents = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    return {
        "queries": {"used": queries, "limit": store.MAX_QUERIES},
        "documents": {"used": documents, "limit": store.MAX_DOCUMENTS},
        "browser_pages": {"used": current["browser_pages"], "limit": store.MAX_BROWSER_PAGES},
        "ocr_pages": {"used": current["ocr_pages"], "limit": store.MAX_OCR_PAGES},
        "collection_seconds": {"used": round(current["used_seconds"], 3), "limit": store.MAX_SECONDS},
    }


def _read_status(conn: sqlite3.Connection) -> dict[str, Any]:
    current = store.metadata(conn)
    counts = {row[0]: row[1] for row in conn.execute("SELECT status,COUNT(*) FROM documents GROUP BY status")}
    return {
        "ok": True,
        "schema": store.SCHEMA,
        "job_id": current["job_id"],
        "profile": store.PROFILE,
        "target": "[redacted VIN]",
        "status": current["collection_status"],
        "collection_status": current["collection_status"],
        "analysis_status": current["analysis_status"],
        "revision": current["revision"],
        "created_at": current["created_at"],
        "expires_at": current["expires_at"],
        "budget": _budget(current, conn),
        "requested_fields": current["fields"],
        "documents": sum(counts.values()),
        "fetched": counts.get("fetched", 0),
        "failed": counts.get("failed", 0),
        "duplicates": conn.execute("SELECT COUNT(*) FROM documents WHERE duplicate_of<>''").fetchone()[0],
        "network_inflight": current["inflight"],
        "cancel_requested": current["cancel_requested"],
        "stop_reason": current["stop_reason"] or None,
        "search_recipients": list(store.ENGINES),
        "query_plan": [
            {
                "query": _safe_text(row["query"], 500),
                "status": row["status"],
                "error": row["error"] or None,
                "providers": json.loads(row["providers"]),
            }
            for row in conn.execute("SELECT * FROM queries ORDER BY id")
        ],
    }


@_safe_api
def j1_research_vin(vin: str, idempotency_key: str) -> dict[str, Any]:
    if not store.enabled():
        return store.error("vin_research_disabled")
    try:
        normalized = normalize_vin(vin)
    except ValueError:
        return store.error("vin_invalid")
    _check_key(idempotency_key)
    queries = [f'"{normalized}"', f'"{normalized[:3]}" VIN manufacturer', f'"{normalized[:8]}" VIN identification']
    job_id = store.create(normalized, idempotency_key, queries)
    with store.connect(job_id) as conn:
        return _read_status(conn)


@_safe_api
def research_status(job_id: str) -> dict[str, Any]:
    with store.connect(job_id) as conn:
        return _read_status(conn)


def _document_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "document_id": row["id"],
        "url": _safe_text(row["final_url"] or row["url"], 2000),
        "requested_url": _safe_text(row["url"], 2000),
        "title": row["title"],
        "document_revision": row["version"],
        "source_class": row["source_class"],
        "source_tier": row["source_tier"],
        "source_basis": row["source_basis"],
        "status": row["status"],
        "kind": row["kind"],
        "extraction_method": row["method"],
        "retrieved_at": row["retrieved_at"],
        "page_count": row["page_count"],
        "duplicate_of": row["duplicate_of"] or None,
        "limitations": json.loads(row["limitations"]),
        "error": row["error"] or None,
    }


@_safe_api
def research_results(job_id: str, query: str = "", cursor: int = 0, limit: int = 20) -> dict[str, Any]:
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 50:
        return store.error("pagination_invalid", job_id)
    with store.connect(job_id) as conn:
        current = store.metadata(conn)
        if not isinstance(query, str) or len(query) > 300 or (query.strip() and not safe_query(query, scope(current))):
            return store.error("query_invalid_or_sensitive", job_id)
        tokens = re.findall(r"[^\W_]{2,}", query, re.UNICODE)[:10]
        parameters: tuple[Any, ...] = ()
        sql = "SELECT * FROM documents"
        matching_pages: dict[str, list[int]] = defaultdict(list)
        if query.strip():
            if not tokens:
                return store.error("query_invalid_or_sensitive", job_id)
            expression = " OR ".join('"' + token.replace('"', "") + '"' for token in tokens)
            hits = conn.execute(
                "SELECT document_id,page FROM pages_fts WHERE pages_fts MATCH ? LIMIT 1000", (expression,)
            )
            for row in hits:
                matching_pages[row["document_id"]].append(int(row["page"]))
            sql += " WHERE id IN (SELECT document_id FROM pages_fts WHERE pages_fts MATCH ?)"
            parameters = (expression,)
        rows = conn.execute(sql + " ORDER BY rowid", parameters).fetchall()
        results = []
        for row in rows[cursor : cursor + limit]:
            item = _document_item(row)
            item["matching_pages"] = sorted(set(matching_pages.get(row["id"], [])))
            results.append(item)
        next_cursor = cursor + len(results)
        return {
            "ok": True,
            "schema": store.SCHEMA,
            "job_id": job_id,
            "results": results,
            "total": len(rows),
            "next_cursor": next_cursor if next_cursor < len(rows) else None,
        }


def _read_document(
    conn: sqlite3.Connection, document_id: str, page: int | None, offset: int, max_chars: int
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
    if row is None:
        raise store.VinJobError("document_not_found")
    selected = page
    if selected is None:
        first = conn.execute("SELECT MIN(page) FROM pages WHERE document_id=?", (document_id,)).fetchone()[0]
        selected = first if first is not None else 1
    text_row = conn.execute("SELECT * FROM pages WHERE document_id=? AND page=?", (document_id, selected)).fetchone()
    if text_row is None:
        raise store.VinJobError("document_page_unavailable")
    text = text_row["text"]
    return {
        "ok": True,
        "schema": store.SCHEMA,
        **_document_item(row),
        "page": selected,
        "text": text[offset : offset + max_chars],
        "offset": offset,
        "total_chars": len(text),
        "next_offset": offset + max_chars if offset + max_chars < len(text) else None,
        "match_evidence_id": text_row["match_evidence_id"] or None,
        "match_kind": "literal_only" if text_row["match_evidence_id"] else None,
        "available_pages": [
            r[0] for r in conn.execute("SELECT page FROM pages WHERE document_id=? ORDER BY page", (document_id,))
        ],
    }


@_safe_api
def research_document(
    job_id: str, document_id: str, offset: int = 0, max_chars: int = 8000, page: int | None = None, ocr: bool = False
) -> dict[str, Any]:
    if not isinstance(document_id, str) or not store.IDENTIFIER.fullmatch(document_id):
        return store.error("identifier_invalid", job_id)
    if type(offset) is not int or offset < 0 or type(max_chars) is not int or not 80 <= max_chars <= 50_000:
        return store.error("document_range_invalid", job_id)
    if (page is not None and (type(page) is not int or not 1 <= page <= 1000)) or type(ocr) is not bool:
        return store.error("document_page_invalid", job_id)
    if ocr:
        if page is None:
            return store.error("ocr_page_required", job_id)
        from .j1_vin_worker import queue_ocr

        return queue_ocr(job_id, document_id, page)
    with store.connect(job_id) as conn:
        return {**_read_document(conn, document_id, page, offset, max_chars), "job_id": job_id}


def _invalidation(current: dict[str, Any]) -> None:
    current["revision"] += 1
    current["analysis_status"] = "draft"


@_safe_api
def research_add_queries(job_id: str, queries: list[str]) -> dict[str, Any]:
    if not store.enabled():
        return store.error("vin_research_disabled", job_id)
    if not isinstance(queries, list) or not 1 <= len(queries) <= store.MAX_QUERIES:
        return store.error("queries_invalid", job_id)
    with store.start_lock(), store.connect(job_id, transaction=True) as conn:
        current = store.metadata(conn)
        if current["collection_status"] in {"cancelled", "failed"} or current["cancel_requested"]:
            return store.error("job_not_extendable", job_id)
        if current["used_seconds"] >= store.MAX_SECONDS:
            return store.error("vin_time_limit_reached", job_id)
        if any(
            not isinstance(q, str) or not 1 <= len(q.strip()) <= 500 or not safe_query(q, scope(current))
            for q in queries
        ):
            return store.error("queries_invalid_or_sensitive", job_id)
        existing = {row[0].casefold() for row in conn.execute("SELECT query FROM queries")}
        additions = []
        seen = set(existing)
        for query in queries:
            if query.strip().casefold() not in seen:
                additions.append(query.strip())
                seen.add(query.strip().casefold())
        if len(existing) + len(additions) > store.MAX_QUERIES:
            return store.error("query_limit_reached", job_id)
        if not additions:
            return {"ok": True, "schema": store.SCHEMA, "job_id": job_id, "added": 0, "revision": current["revision"]}
        with store.general._db() as queue:
            queue.execute("BEGIN IMMEDIATE")
            if queue.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running') AND id<>?", (job_id,)).fetchone():
                return store.error("j1_busy", job_id)
            conn.executemany("INSERT INTO queries(query) VALUES(?)", ((q,) for q in additions))
            _invalidation(current)
            if current["collection_status"] != "running":
                current["collection_status"] = "queued"
            current["stop_reason"] = ""
            store.write_metadata(conn, current)
            # Queue the pointer first while the private transaction is locked.
            # A failed private commit may cause a harmless empty worker pass;
            # committing private work first could leave it permanently unscheduled.
            queue.execute("UPDATE jobs SET status=?,error='' WHERE id=?", (current["collection_status"], job_id))
            queue.commit()
        return {
            "ok": True,
            "schema": store.SCHEMA,
            "job_id": job_id,
            "added": len(additions),
            "queries_total": len(existing) + len(additions),
            "revision": current["revision"],
        }


@_safe_api
def research_cancel(job_id: str) -> dict[str, Any]:
    with store.connect(job_id, transaction=True) as conn:
        current = store.metadata(conn)
        current["cancel_requested"] = True
        current["stop_reason"] = "cancelled_by_agent"
        if not current["inflight"]:
            current["collection_status"] = "cancelled"
        store.write_metadata(conn, current)
        result = _read_status(conn)
    # Do not release the global active-job slot while a request is in flight.
    if not current["inflight"]:
        store.set_stub(job_id, "cancelled")
    return result


def _value_valid(value: Any) -> bool:
    if type(value) is float and not math.isfinite(value):
        return False
    if type(value) in (str, int, float):
        return len(str(value)) <= 1000 and bool(str(value).strip()) and not fetch.contains_sensitive(str(value))
    return (
        isinstance(value, list)
        and 1 <= len(value) <= 30
        and all(type(v) in (str, int, float) and _value_valid(v) for v in value)
    )


def _validate_claim(conn: sqlite3.Connection, current: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict) or set(item) - _CLAIM_KEYS:
        raise store.VinJobError("fact_schema_invalid")
    field = item.get("field")
    value = item.get("value")
    if (
        not isinstance(field, str)
        or not _FIELD.fullmatch(field)
        or field not in _FACT_FIELDS
        or not _value_valid(value)
    ):
        raise store.VinJobError("fact_value_invalid")
    document_id, version, page = item.get("document_id"), item.get("document_revision"), item.get("page")
    if (
        not isinstance(document_id, str)
        or not store.IDENTIFIER.fullmatch(document_id)
        or type(version) is not int
        or type(page) is not int
    ):
        raise store.VinJobError("fact_document_invalid")
    doc = conn.execute("SELECT * FROM documents WHERE id=? AND status='fetched'", (document_id,)).fetchone()
    text = conn.execute("SELECT * FROM pages WHERE document_id=? AND page=?", (document_id, page)).fetchone()
    if doc is None or text is None or doc["version"] != version:
        raise store.VinJobError("fact_document_revision_invalid")
    quote = item.get("quote")
    if (
        not isinstance(quote, str)
        or not 1 <= len(quote) <= 2000
        or quote not in text["text"]
        or fetch.contains_sensitive(quote)
    ):
        raise store.VinJobError("fact_quote_invalid")
    relationship = item.get("relationship")
    if not isinstance(relationship, str) or relationship not in {"vin_specific", "family", "general"}:
        raise store.VinJobError("fact_relationship_invalid")
    match = item.get("match_evidence_id", "") or ""
    if relationship == "vin_specific" and (not text["match_evidence_id"] or match != text["match_evidence_id"]):
        raise store.VinJobError("vin_match_evidence_required")
    derivation = item.get("derivation", "direct")
    basis = item.get("basis_claim_ids", [])
    reasoning = item.get("reasoning", "")
    if not isinstance(reasoning, str) or len(reasoning) > 2000 or fetch.contains_sensitive(reasoning):
        raise store.VinJobError("fact_reasoning_invalid")
    _validate_derivation(conn, derivation, basis, reasoning, value, quote)
    unit = item.get("unit", "")
    support = item.get("support", "single_source")
    if (
        not isinstance(unit, str)
        or len(unit) > 40
        or fetch.contains_sensitive(unit)
        or not isinstance(support, str)
        or support not in {"single_source", "hypothesis", "corroborated", "conflicted"}
    ):
        raise store.VinJobError("fact_support_invalid")
    if support == "corroborated":
        _validate_corroboration(conn, doc, field, value, unit, relationship, basis)
    claim_id = item.get("claim_id", uuid4().hex)
    if not isinstance(claim_id, str) or not store.IDENTIFIER.fullmatch(claim_id):
        raise store.VinJobError("claim_id_invalid")
    return {
        "claim_id": claim_id,
        "field": field,
        "value": value,
        "unit": unit,
        "document_id": document_id,
        "document_revision": version,
        "quote": quote,
        "page": page,
        "relationship": relationship,
        "match_evidence_id": match if relationship == "vin_specific" else None,
        "derivation": derivation,
        "reasoning": reasoning,
        "basis_claim_ids": basis,
        "support": support,
        "source_url": _safe_text(doc["final_url"] or doc["url"], 2000),
        "source_tier": doc["source_tier"],
        "retrieved_at": doc["retrieved_at"],
        "semantic_assessment": "agent",
        "citation_verified": True,
    }


def _validate_corroboration(
    conn: sqlite3.Connection, doc: sqlite3.Row, field: str, value: Any, unit: str, relationship: str, basis: list[str]
) -> None:
    sources = {doc["duplicate_of"] or doc["id"]}
    for basis_id in basis:
        basis_row = conn.execute("SELECT payload FROM facts WHERE claim_id=?", (basis_id,)).fetchone()
        source_claim = json.loads(basis_row[0])
        if (
            source_claim["field"] != field
            or source_claim["unit"].casefold() != unit.casefold()
            or source_claim["relationship"] != relationship
            or json.dumps(source_claim["value"], sort_keys=True).casefold()
            != json.dumps(value, sort_keys=True).casefold()
        ):
            continue
        source_doc = conn.execute(
            "SELECT id,duplicate_of FROM documents WHERE id=?", (source_claim["document_id"],)
        ).fetchone()
        sources.add(source_doc["duplicate_of"] or source_doc["id"])
    if len(sources) < 2:
        raise store.VinJobError("fact_corroboration_basis_required")


def _validate_derivation(
    conn: sqlite3.Connection, derivation: Any, basis: Any, reasoning: str, value: Any, quote: str
) -> None:
    if (
        not isinstance(derivation, str)
        or derivation not in {"direct", "inference"}
        or not isinstance(basis, list)
        or len(basis) > 20
    ):
        raise store.VinJobError("fact_derivation_invalid")
    if derivation == "direct":
        values = value if isinstance(value, list) else [value]
        if any(str(v).casefold() not in quote.casefold() for v in values):
            raise store.VinJobError("fact_value_not_in_quote")
    elif not basis or not reasoning.strip():
        raise store.VinJobError("fact_inference_basis_required")
    seen: set[str] = set()
    for claim_id in basis:
        if not isinstance(claim_id, str) or not store.IDENTIFIER.fullmatch(claim_id):
            raise store.VinJobError("fact_inference_basis_invalid")
        if claim_id in seen:
            raise store.VinJobError("fact_inference_basis_invalid")
        seen.add(claim_id)
        row = conn.execute("SELECT payload FROM facts WHERE claim_id=?", (claim_id,)).fetchone()
        if row is None or not _claim_current(conn, json.loads(row[0])):
            raise store.VinJobError("fact_inference_basis_invalid")


def _claim_current(
    conn: sqlite3.Connection,
    claim: dict[str, Any],
    visited: frozenset[str] = frozenset(),
    cache: dict[str, bool] | None = None,
) -> bool:
    if cache is None:
        cache = {}
    claim_id = claim["claim_id"]
    if claim_id in visited or len(visited) >= 100:
        return False
    if claim_id in cache:
        return cache[claim_id]
    row = conn.execute(
        "SELECT version FROM documents WHERE id=? AND status='fetched'", (claim["document_id"],)
    ).fetchone()
    if row is None or row[0] != claim["document_revision"]:
        cache[claim_id] = False
        return False
    for basis_id in claim.get("basis_claim_ids", []):
        basis = conn.execute("SELECT payload FROM facts WHERE claim_id=?", (basis_id,)).fetchone()
        if basis is None or not _claim_current(conn, json.loads(basis[0]), visited | {claim_id}, cache):
            cache[claim_id] = False
            return False
    cache[claim_id] = True
    return True


def _claims(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    result = []
    cache: dict[str, bool] = {}
    for row in conn.execute("SELECT payload FROM facts ORDER BY rowid"):
        claim = json.loads(row[0])
        claim["evidence_current"] = _claim_current(conn, claim, cache=cache)
        result.append(claim)
    return result


def _conflicts(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for claim in claims:
        if claim["evidence_current"]:
            groups[(claim["field"], claim["unit"].casefold())].append(claim)
    result = []
    for (field, unit), items in groups.items():
        values = {json.dumps(item["value"], ensure_ascii=False, sort_keys=True).casefold() for item in items}
        if len(values) > 1:
            result.append(
                {
                    "field": field,
                    "unit": unit,
                    "claim_ids": [item["claim_id"] for item in items],
                    "kind": "potential_conflict",
                    "requires_agent_review": True,
                }
            )
    return result


def _analysis_state(conn: sqlite3.Connection, current: dict[str, Any]) -> str:
    claims = _claims(conn)
    covered = {
        item["field"]
        for item in claims
        if item["evidence_current"]
        and item["relationship"] == "vin_specific"
        and item["support"] not in {"hypothesis", "conflicted"}
    }
    if set(current["fields"]) - covered or _conflicts(claims) or any(not c["evidence_current"] for c in claims):
        return "ready_partial"
    return "ready"


@_safe_api
def j1_research_record_facts(
    job_id: str, expected_revision: int, facts: list[dict[str, Any]], idempotency_key: str, finalize: bool = False
) -> dict[str, Any]:
    if not store.enabled():
        return store.error("vin_research_disabled", job_id)
    _check_key(idempotency_key)
    if type(expected_revision) is not int or expected_revision < 0 or type(finalize) is not bool:
        return store.error("fact_revision_invalid", job_id)
    if not isinstance(facts, list) or len(facts) > 100:
        return store.error("facts_invalid", job_id)
    request = json.dumps(
        {"revision": expected_revision, "facts": facts, "finalize": finalize},
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    )
    if len(request.encode("utf-8")) > 250_000:
        return store.error("facts_invalid", job_id)
    with store.connect(job_id, transaction=True) as conn:
        current = store.metadata(conn)
        receipt = conn.execute("SELECT request,response FROM receipts WHERE key=?", (idempotency_key,)).fetchone()
        if receipt:
            if receipt["request"] != request:
                return store.error("idempotency_conflict", job_id)
            return json.loads(receipt["response"])
        if current["revision"] != expected_revision:
            return {**store.error("revision_conflict", job_id), "current_revision": current["revision"]}
        if finalize and (
            current["inflight"] or current["collection_status"] not in {"completed", "cancelled", "failed"}
        ):
            return store.error("collection_not_quiescent", job_id)
        claims = []
        for item in facts:
            claim = _validate_claim(conn, current, item)
            payload = json.dumps(claim, ensure_ascii=False, sort_keys=True)
            existing = conn.execute("SELECT payload FROM facts WHERE claim_id=?", (claim["claim_id"],)).fetchone()
            if existing and existing[0] != payload:
                raise store.VinJobError("claim_id_conflict")
            conn.execute(
                "INSERT OR IGNORE INTO facts(claim_id,field,payload) VALUES(?,?,?)",
                (claim["claim_id"], claim["field"], payload),
            )
            claims.append(claim["claim_id"])
        _invalidation(current)
        if finalize:
            current["analysis_status"] = _analysis_state(conn, current)
            current["finalized_revision"] = current["revision"]
        store.write_metadata(conn, current)
        result = {
            "ok": True,
            "schema": store.SCHEMA,
            "job_id": job_id,
            "claim_ids": claims,
            "revision": current["revision"],
            "analysis_status": current["analysis_status"],
        }
        conn.execute(
            "INSERT INTO receipts(key,request,response) VALUES(?,?,?)", (idempotency_key, request, json.dumps(result))
        )
        return result


@_safe_api
def research_report(job_id: str) -> dict[str, Any]:
    with store.connect(job_id) as conn:
        current = store.metadata(conn)
        claims = _claims(conn)
        covered = {
            claim["field"]
            for claim in claims
            if claim["evidence_current"]
            and claim["relationship"] == "vin_specific"
            and claim["support"] not in {"hypothesis", "conflicted"}
        }
        sources = [_document_item(row) for row in conn.execute("SELECT * FROM documents ORDER BY rowid")]
        return {
            "ok": True,
            "schema": store.REPORT_SCHEMA,
            "job_id": job_id,
            "target": "[redacted VIN]",
            "collection_status": current["collection_status"],
            "analysis_status": current["analysis_status"],
            "revision": current["revision"],
            "finalized_revision": current.get("finalized_revision"),
            "claims": claims,
            "conflicts": _conflicts(claims),
            "unknown_fields": sorted(set(current["fields"]) - covered),
            "sources": sources,
            "budget": _budget(current, conn),
            "stop_reason": current["stop_reason"] or None,
            "limitations": [
                "Claims are interpreted by the agent; citation checks do not certify automotive truth.",
                "A literal VIN match links a source record but does not prove factory configuration.",
                "Family specifications do not establish this VIN's engine, options or production date.",
                "Absence of an exact search result does not establish absence of the vehicle.",
                "Model year and calendar production year are separate; accuracy has not been measured.",
            ],
        }


def ingest_pages(
    conn: sqlite3.Connection,
    current: dict[str, Any],
    document_id: str,
    pages: list[dict[str, Any]],
    *,
    replace: bool = True,
    proof_allowed: bool = True,
) -> None:
    """Derive literal linkage before redaction; only sanitized text is indexed."""

    if len(pages) > 1000 or sum(len(str(item.get("text", "")).encode("utf-8")) for item in pages) > 2 * 1024 * 1024:
        raise store.VinJobError("document_text_limit_reached")
    if replace:
        conn.execute("DELETE FROM pages WHERE document_id=?", (document_id,))
        conn.execute("DELETE FROM pages_fts WHERE document_id=?", (document_id,))
    for item in pages:
        page = item.get("page")
        raw = item.get("text")
        if type(page) is not int or not 1 <= page <= 1000 or not isinstance(raw, str):
            raise store.VinJobError("document_extraction_invalid")
        matches = [m for m in fetch._VIN.finditer(raw) if fetch._looks_like_vin(m.group())]
        matched = any(re.sub(r"[ ._/\\-]", "", m.group()).upper() == current["vin"] for m in matches)
        evidence = uuid4().hex if proof_allowed and matched else ""
        text = _safe_text(raw, 2 * 1024 * 1024)
        conn.execute("DELETE FROM pages_fts WHERE document_id=? AND page=?", (document_id, page))
        conn.execute(
            "INSERT OR REPLACE INTO pages(document_id,page,text,match_evidence_id) VALUES(?,?,?,?)",
            (document_id, page, text, evidence),
        )
        conn.execute("INSERT INTO pages_fts(document_id,page,text) VALUES(?,?,?)", (document_id, page, text))
    _invalidation(current)


def content_digest(pages: list[dict[str, Any]]) -> str:
    value = "\n".join(_safe_text(p.get("text", ""), 2 * 1024 * 1024) for p in pages)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
