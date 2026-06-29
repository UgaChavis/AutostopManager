from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .storage import ManagerMemoryStore, _json_list, _now, _string_list


KNOWLEDGE_MAP_PATH = PROJECT_ROOT / "docs" / "agent" / "knowledge_map.json"
COMMAND_ROUTES_PATH = PROJECT_ROOT / "docs" / "agent" / "command_routes.json"
KNOWLEDGE_ANNOTATIONS_PATH = PROJECT_ROOT / "docs" / "agent" / "knowledge_annotations.jsonl"
MAX_SECTION_CHARS = 12000
MAX_PREVIEW_CHARS = 420

STOPWORDS = {
    "а",
    "без",
    "в",
    "во",
    "для",
    "и",
    "или",
    "как",
    "к",
    "ко",
    "на",
    "не",
    "но",
    "о",
    "об",
    "от",
    "по",
    "про",
    "с",
    "со",
    "у",
    "что",
    "the",
    "and",
    "or",
    "for",
    "with",
    "without",
}


@dataclass(frozen=True)
class _Section:
    heading: str
    level: int
    content: str
    ordinal: int


@dataclass(frozen=True)
class _RouteCard:
    domain: str
    title: str
    use_when: list[str]
    aliases: list[str]
    keywords: list[str]
    questions: list[str]
    source_of_truth: list[str]
    primary_files: list[str]
    reference_files: list[str]
    optional_runtime_files: list[str]
    required_context: list[str]
    search_text: str


_KNOWLEDGE_ROUTE_LIST_FIELDS = (
    "use_when",
    "aliases",
    "keywords",
    "questions",
    "questions_it_answers",
    "source_of_truth_files",
    "primary_files",
    "reference_files",
    "required_context",
    "optional_runtime_files",
    "optional_files",
)

_COMMAND_ROUTE_LIST_FIELDS = ("aliases", "keywords", "memory_queries", "next_actions")

_ANNOTATION_LIST_FIELDS = ("use_when", "keywords", "questions", "safety_flags", "related_skills")


def _normalize_list_fields(item: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    normalized = dict(item)
    for field in fields:
        normalized[field] = _string_list(normalized.get(field))
    return normalized


def sync_knowledge_base(store: ManagerMemoryStore | None = None) -> dict[str, Any]:
    memory = store or ManagerMemoryStore()
    memory.initialize()
    if not KNOWLEDGE_MAP_PATH.exists():
        return {
            "ok": False,
            "error": "knowledge_map.json not found",
            "documents_indexed": 0,
            "sections_indexed": 0,
            "missing_files": [],
            "optional_missing_files": [],
        }

    payload = _load_knowledge_map()
    domains: dict[str, Any] = payload.get("domains", {})
    if not domains:
        return {
            "ok": False,
            "error": "knowledge_map.json has no valid domains",
            "documents_indexed": 0,
            "sections_indexed": 0,
            "missing_files": [],
            "optional_missing_files": [],
            "missing_optional_files": [],
        }
    now = _now()
    missing: list[str] = []
    optional_missing: list[str] = []
    documents_indexed = 0
    sections_indexed = 0
    route_cards_indexed = 0
    annotations_indexed = 0

    with memory.connect() as conn:
        conn.execute("DELETE FROM knowledge_annotations_fts")
        conn.execute("DELETE FROM knowledge_sections_fts")
        conn.execute("DELETE FROM knowledge_annotations")
        conn.execute("DELETE FROM knowledge_sections")
        conn.execute("DELETE FROM knowledge_documents")
        conn.execute("DELETE FROM knowledge_route_cards")

        for domain, route in domains.items():
            use_when = _string_list(route.get("use_when"))
            primary_files = _string_list(route.get("primary_files"))
            optional_runtime_files = _optional_runtime_files(route)
            optional_status = {
                raw_path: (_resolve_path(raw_path).exists() and _resolve_path(raw_path).is_file())
                for raw_path in optional_runtime_files
            }
            optional_missing_for_domain = [raw_path for raw_path, is_present in optional_status.items() if not is_present]
            optional_present_for_domain = [raw_path for raw_path, is_present in optional_status.items() if is_present]
            optional_missing.extend(optional_missing_for_domain)
            skill_path = str(route.get("skill_path") or "")
            route_card = _build_route_card(domain, route)
            _insert_route_card(conn, route_card, indexed_at=now)
            route_cards_indexed += 1
            route_path = f"knowledge_map:{domain}"
            optional_runtime_lines: list[str] = []
            if optional_runtime_files:
                optional_runtime_lines = [
                    "Optional runtime files:",
                    *[f"- {item}" for item in optional_runtime_files],
                    "Optional runtime status:",
                    *[f"- indexed locally: {item}" for item in optional_present_for_domain],
                    *[f"- missing locally: {item}" for item in optional_missing_for_domain],
                ]
                if optional_missing_for_domain:
                    optional_runtime_lines.append("Current private facts are unavailable until these local runtime files exist.")
            route_content = "\n".join(
                [
                    f"Domain: {domain}",
                    f"Title: {route_card.title}",
                    "Use when:",
                    *[f"- {item}" for item in route_card.use_when],
                    "Aliases:",
                    *[f"- {item}" for item in route_card.aliases],
                    "Keywords:",
                    *[f"- {item}" for item in route_card.keywords],
                    "Questions:",
                    *[f"- {item}" for item in route_card.questions],
                    "Source of truth:",
                    *[f"- {item}" for item in route_card.source_of_truth],
                    "Primary files:",
                    *[f"- {item}" for item in route_card.primary_files],
                    "Reference files:",
                    *[f"- {item}" for item in route_card.reference_files],
                    "Required context:",
                    *[f"- {item}" for item in route_card.required_context],
                    *optional_runtime_lines,
                    f"Skill path: {skill_path}" if skill_path else "",
                ]
            ).strip()
            document_id = _insert_document(
                conn,
                domain=domain,
                path=route_path,
                title=f"{domain} route",
                document_type="domain_route",
                use_when=use_when,
                content=route_content,
                indexed_at=now,
            )
            documents_indexed += 1
            sections_indexed += _insert_sections(
                conn,
                document_id=document_id,
                domain=domain,
                path=route_path,
                sections=[_Section("Domain Route", 1, route_content, 0)],
                indexed_at=now,
            )

            indexed_paths: set[str] = set()
            for raw_path in primary_files:
                resolved = _resolve_path(raw_path)
                if not resolved.exists() or not resolved.is_file():
                    missing.append(raw_path)
                    continue
                document_count, section_count = _index_knowledge_file(
                    conn,
                    domain=domain,
                    use_when=use_when,
                    raw_path=raw_path,
                    indexed_at=now,
                )
                documents_indexed += document_count
                sections_indexed += section_count
                indexed_paths.add(raw_path.lower())

            for raw_path in optional_runtime_files:
                if raw_path.lower() in indexed_paths or not optional_status.get(raw_path):
                    continue
                document_count, section_count = _index_knowledge_file(
                    conn,
                    domain=domain,
                    use_when=use_when,
                    raw_path=raw_path,
                    indexed_at=now,
                )
                documents_indexed += document_count
                sections_indexed += section_count
                indexed_paths.add(raw_path.lower())

        annotations_indexed = _insert_annotations(conn, indexed_at=now)

    return {
        "ok": True,
        "map_path": str(KNOWLEDGE_MAP_PATH),
        "route_cards_indexed": route_cards_indexed,
        "documents_indexed": documents_indexed,
        "sections_indexed": sections_indexed,
        "annotations_indexed": annotations_indexed,
        "domains": sorted(domains.keys()),
        "missing_files": _unique_strings(missing),
        "optional_missing_files": _unique_strings(optional_missing),
        "missing_optional_files": _unique_strings(optional_missing),
        "indexed_at": now,
    }


def probe_knowledge_base(
    store: ManagerMemoryStore | None,
    query: str,
    *,
    limit: int = 5,
) -> dict[str, Any]:
    memory = store or ManagerMemoryStore()
    memory.initialize()
    limit = max(1, min(limit, 20))
    query = (query or "").strip()
    if _route_card_count(memory) == 0:
        sync_knowledge_base(memory)

    tokens = _tokens(query)
    command_route = find_command_route(query)
    domain_hints = _domain_hints(query)
    route_definitions = (_load_knowledge_map().get("domains") or {}) if KNOWLEDGE_MAP_PATH.exists() else {}
    if command_route:
        domain_hints[str(command_route.get("domain") or "")] = max(
            domain_hints.get(str(command_route.get("domain") or ""), 0),
            int(command_route.get("score") or 0),
        )
    with memory.connect() as conn:
        rows = conn.execute(
            """
            SELECT
                domain,
                title,
                use_when_json,
                aliases_json,
                keywords_json,
                questions_json,
                source_of_truth_json,
                primary_files_json,
                reference_files_json,
                required_context_json,
                search_text,
                indexed_at
            FROM knowledge_route_cards
            """
        ).fetchall()
        annotation_rows = conn.execute(
            """
            SELECT domain, path, title, summary, use_when_json, keywords_json, questions_json, search_text
            FROM knowledge_annotations
            """
        ).fetchall()

    annotations_by_domain: dict[str, list[dict[str, Any]]] = {}
    for row in annotation_rows:
        annotation = dict(row)
        annotations_by_domain.setdefault(str(annotation.get("domain") or ""), []).append(annotation)

    routes: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["annotation_text"] = "\n".join(
            str(annotation.get("search_text") or "") for annotation in annotations_by_domain.get(str(item["domain"]), [])
        )
        score, matching_terms = _score_route_card(item, tokens, query, domain_hints=domain_hints)
        if tokens and score <= 0:
            continue
        source_of_truth = json.loads(item["source_of_truth_json"] or "[]")
        primary_files = json.loads(item["primary_files_json"] or "[]")
        reference_files = json.loads(item["reference_files_json"] or "[]")
        open_first = (source_of_truth or primary_files or [""])[0]
        if command_route and item["domain"] == command_route.get("domain") and command_route.get("open_first"):
            open_first = str(command_route["open_first"])
            if open_first not in source_of_truth:
                source_of_truth = [open_first, *source_of_truth]
        runtime_status = _optional_runtime_status(route_definitions.get(str(item["domain"]), {}))
        route = {
            "domain": item["domain"],
            "title": item["title"],
            "score": score,
            "confidence": _confidence(score),
            "matching_terms": matching_terms,
            "open_first": open_first,
            "source_of_truth": source_of_truth,
            "primary_files": primary_files,
            "reference_files": reference_files,
            "optional_runtime_files": runtime_status["files"],
            "optional_available_files": runtime_status["available_files"],
            "optional_missing_files": runtime_status["missing_files"],
            "optional_runtime_available": runtime_status["all_available"],
            "optional_runtime_note": runtime_status["note"],
            "required_context": json.loads(item["required_context_json"] or "[]"),
            "use_when": json.loads(item["use_when_json"] or "[]"),
            "indexed_at": item["indexed_at"],
        }
        routes.append(route)

    routes.sort(key=lambda value: (value["score"], len(value["matching_terms"])), reverse=True)
    routes = routes[:limit]
    best = routes[0] if routes else None
    confidence = float(best["confidence"]) if best else 0.0
    has_knowledge = bool(best and best["score"] >= 12 and confidence >= 0.45)

    return {
        "ok": True,
        "query": query,
        "has_knowledge": has_knowledge,
        "confidence": confidence,
        "best_domain": best["domain"] if best else None,
        "open_first": best["open_first"] if best else None,
        "source_of_truth": best["source_of_truth"] if best else [],
        "reference_files": best["reference_files"] if best else [],
        "optional_runtime_files": best["optional_runtime_files"] if best else [],
        "optional_available_files": best["optional_available_files"] if best else [],
        "optional_missing_files": best["optional_missing_files"] if best else [],
        "optional_runtime_available": best["optional_runtime_available"] if best else False,
        "optional_runtime_note": best["optional_runtime_note"] if best else "",
        "command_route": command_route,
        "routes": routes,
        "next_action": "open_source_of_truth" if has_knowledge else "route_external_sources",
        "needs_broad_search": not has_knowledge,
        "probed_at": _now(),
    }


def audit_knowledge_base(store: ManagerMemoryStore | None = None) -> dict[str, Any]:
    memory = store or ManagerMemoryStore()
    memory.initialize()
    if not KNOWLEDGE_MAP_PATH.exists():
        return {
            "ok": False,
            "error": "knowledge_map.json not found",
            "missing_files": [],
            "optional_missing_files": [],
            "checked_at": _now(),
        }
    if _route_card_count(memory) == 0 or _document_count(memory) == 0:
        sync_knowledge_base(memory)

    payload = _load_knowledge_map()
    domains: dict[str, Any] = payload.get("domains", {})
    if not domains:
        return {
            "ok": False,
            "map_path": str(KNOWLEDGE_MAP_PATH),
            "domain_count": 0,
            "route_cards_indexed": 0,
            "documents_indexed": 0,
            "sections_indexed": 0,
            "sections_fts_indexed": 0,
            "annotations_indexed": 0,
            "annotations_fts_indexed": 0,
            "missing_files": [],
            "optional_missing_files": [],
            "missing_optional_files": [],
            "domains_without_source_of_truth": [],
            "domains_without_aliases": [],
            "warnings": ["knowledge_map_has_no_valid_domains"],
            "checked_at": _now(),
        }
    missing_files: list[str] = []
    optional_missing_files: list[str] = []
    domains_without_source_of_truth: list[str] = []
    domains_without_aliases: list[str] = []
    checked_paths: set[str] = set()
    checked_optional_paths: set[str] = set()

    for domain, route in domains.items():
        if not route.get("source_of_truth_files") and not route.get("primary_files"):
            domains_without_source_of_truth.append(domain)
        if not route.get("aliases"):
            domains_without_aliases.append(domain)
        for raw_path in _unique_strings(
            [
                *_string_list(route.get("source_of_truth_files")),
                *_string_list(route.get("primary_files")),
                *_string_list(route.get("reference_files")),
            ]
        ):
            if raw_path in checked_paths:
                continue
            checked_paths.add(raw_path)
            resolved = _resolve_path(raw_path)
            if not resolved.exists() or not resolved.is_file():
                missing_files.append(raw_path)
        for raw_path in _optional_runtime_files(route):
            if raw_path in checked_paths or raw_path in checked_optional_paths:
                continue
            checked_optional_paths.add(raw_path)
            resolved = _resolve_path(raw_path)
            if not resolved.exists() or not resolved.is_file():
                optional_missing_files.append(raw_path)

    with memory.connect() as conn:
        route_cards_indexed = int(conn.execute("SELECT COUNT(*) AS count FROM knowledge_route_cards").fetchone()["count"] or 0)
        documents_indexed = int(conn.execute("SELECT COUNT(*) AS count FROM knowledge_documents").fetchone()["count"] or 0)
        sections_indexed = int(conn.execute("SELECT COUNT(*) AS count FROM knowledge_sections").fetchone()["count"] or 0)
        sections_fts_indexed = int(conn.execute("SELECT COUNT(*) AS count FROM knowledge_sections_fts").fetchone()["count"] or 0)
        annotations_indexed = int(conn.execute("SELECT COUNT(*) AS count FROM knowledge_annotations").fetchone()["count"] or 0)
        annotations_fts_indexed = int(
            conn.execute("SELECT COUNT(*) AS count FROM knowledge_annotations_fts").fetchone()["count"] or 0
        )

    warnings: list[str] = []
    if route_cards_indexed != len(domains):
        warnings.append("route card count does not match knowledge_map domain count")
    if domains_without_source_of_truth:
        warnings.append("some domains do not declare source_of_truth_files")
    if domains_without_aliases:
        warnings.append("some domains do not declare aliases")
    if missing_files:
        warnings.append("some mapped files are missing")
    if KNOWLEDGE_ANNOTATIONS_PATH.exists() and annotations_indexed == 0:
        warnings.append("knowledge_annotations.jsonl exists but no annotations are indexed")
    if sections_fts_indexed != sections_indexed:
        warnings.append("knowledge_sections_fts_count_mismatch")
    if annotations_fts_indexed != annotations_indexed:
        warnings.append("knowledge_annotations_fts_count_mismatch")

    ok = (
        not missing_files
        and route_cards_indexed == len(domains)
        and documents_indexed > 0
        and sections_indexed > 0
        and sections_fts_indexed == sections_indexed
        and annotations_fts_indexed == annotations_indexed
    )
    if KNOWLEDGE_ANNOTATIONS_PATH.exists():
        ok = ok and annotations_indexed > 0
    return {
        "ok": ok,
        "map_path": str(KNOWLEDGE_MAP_PATH),
        "domain_count": len(domains),
        "route_cards_indexed": route_cards_indexed,
        "documents_indexed": documents_indexed,
        "sections_indexed": sections_indexed,
        "sections_fts_indexed": sections_fts_indexed,
        "annotations_indexed": annotations_indexed,
        "annotations_fts_indexed": annotations_fts_indexed,
        "missing_files": missing_files,
        "optional_missing_files": optional_missing_files,
        "missing_optional_files": optional_missing_files,
        "domains_without_source_of_truth": domains_without_source_of_truth,
        "domains_without_aliases": domains_without_aliases,
        "warnings": warnings,
        "checked_at": _now(),
    }


def audit_knowledge_annotations(store: ManagerMemoryStore | None = None) -> dict[str, Any]:
    memory = store or ManagerMemoryStore()
    memory.initialize()
    if _annotation_count(memory) == 0:
        sync_knowledge_base(memory)

    payload = _load_knowledge_map()
    domains = set((payload.get("domains") or {}).keys())
    with memory.connect() as conn:
        rows = conn.execute(
            """
            SELECT domain, COUNT(*) AS count
            FROM knowledge_annotations
            GROUP BY domain
            ORDER BY domain ASC
            """
        ).fetchall()
    by_domain = {str(row["domain"]): int(row["count"] or 0) for row in rows}
    missing_domains = sorted(domain for domain in domains if by_domain.get(domain, 0) == 0)
    unknown_domains = sorted(domain for domain in by_domain if domain not in domains)
    warnings: list[str] = []
    if missing_domains:
        warnings.append("some knowledge_map domains have no compact annotation")
    if unknown_domains:
        warnings.append("some annotations reference unknown domains")
    return {
        "ok": not warnings and sum(by_domain.values()) > 0,
        "path": str(KNOWLEDGE_ANNOTATIONS_PATH),
        "annotations_indexed": sum(by_domain.values()),
        "domains": by_domain,
        "missing_domains": missing_domains,
        "unknown_domains": unknown_domains,
        "warnings": warnings,
        "checked_at": _now(),
    }


def search_knowledge_base(
    store: ManagerMemoryStore | None,
    query: str,
    *,
    domain: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    memory = store or ManagerMemoryStore()
    memory.initialize()
    limit = max(1, min(limit, 50))
    query = (query or "").strip()
    if _document_count(memory) == 0 or _knowledge_sections_fts_count(memory) == 0:
        sync_knowledge_base(memory)

    tokens = _tokens(query)
    fts_query = _knowledge_fts_query(tokens)
    domain_hints = _domain_hints(query)
    candidate_limit = max(limit * 80, 500)

    with memory.connect() as conn:
        rows = _select_knowledge_section_candidates(
            conn,
            domain=domain,
            fts_query=fts_query,
            candidate_limit=candidate_limit,
        )
        annotation_rows = _select_knowledge_annotation_candidates(
            conn,
            domain=domain,
            fts_query=fts_query,
            candidate_limit=candidate_limit,
        )

    ranked: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        score = _score(item, tokens, query, domain_hints=domain_hints)
        if tokens and score <= 0:
            continue
        use_when = json.loads(item.pop("use_when_json") or "[]")
        ranked.append(
            {
                "domain": item["domain"],
                "path": item["path"],
                "title": item["title"],
                "document_type": item["document_type"],
                "heading": item["heading"],
                "level": item["level"],
                "preview": item["preview"],
                "use_when": use_when,
                "score": score + _document_type_boost(item["document_type"], item["domain"], domain_hints),
                "indexed_at": item["indexed_at"],
            }
        )

    for row in annotation_rows:
        item = dict(row)
        score = _score(item, tokens, query, domain_hints=domain_hints)
        if tokens and score <= 0:
            continue
        use_when = json.loads(item.get("use_when_json") or "[]")
        ranked.append(
            {
                "domain": item["domain"],
                "path": item["path"],
                "title": item["title"],
                "document_type": "annotation",
                "heading": item["title"],
                "level": 0,
                "preview": item["summary"],
                "use_when": use_when,
                "score": score + 10,
                "indexed_at": item["indexed_at"],
            }
        )

    ranked.sort(
        key=lambda value: (
            value["score"],
            2
            if value["document_type"] == "domain_route"
            else 1
            if value["document_type"] == "annotation"
            else 0,
        ),
        reverse=True,
    )
    return {
        "ok": True,
        "query": query,
        "domain": domain,
        "items": ranked[:limit],
        "total_matches": len(ranked),
        "searched_at": _now(),
    }


def _document_count(memory: ManagerMemoryStore) -> int:
    with memory.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM knowledge_documents").fetchone()
    return int(row["count"] or 0)


def _knowledge_sections_fts_count(memory: ManagerMemoryStore) -> int:
    try:
        with memory.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM knowledge_sections_fts").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["count"] or 0)


def _route_card_count(memory: ManagerMemoryStore) -> int:
    with memory.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM knowledge_route_cards").fetchone()
    return int(row["count"] or 0)


def _annotation_count(memory: ManagerMemoryStore) -> int:
    with memory.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM knowledge_annotations").fetchone()
    return int(row["count"] or 0)


def _select_knowledge_section_candidates(
    conn: Any,
    *,
    domain: str | None,
    fts_query: str,
    candidate_limit: int,
) -> list[Any]:
    domain_clause = "AND s.domain = ?" if domain else ""
    params: list[Any] = []
    if fts_query:
        params.append(fts_query)
    if domain:
        params.append(domain)
    params.append(candidate_limit)

    if fts_query:
        rows = conn.execute(
            f"""
            SELECT
                s.domain,
                s.path,
                s.heading,
                s.level,
                s.preview,
                s.content,
                s.search_text,
                d.title,
                d.document_type,
                d.use_when_json,
                d.indexed_at
            FROM knowledge_sections_fts
            JOIN knowledge_sections s ON s.id = knowledge_sections_fts.rowid
            JOIN knowledge_documents d ON d.id = s.document_id
            WHERE knowledge_sections_fts MATCH ?
            {domain_clause}
            LIMIT ?
            """,
            params,
        ).fetchall()
        if rows:
            return list(rows)

    fallback_clause = "WHERE s.domain = ?" if domain else ""
    fallback_params: list[Any] = [domain] if domain else []
    fallback_params.append(candidate_limit)
    return list(
        conn.execute(
            f"""
            SELECT
                s.domain,
                s.path,
                s.heading,
                s.level,
                s.preview,
                s.content,
                s.search_text,
                d.title,
                d.document_type,
                d.use_when_json,
                d.indexed_at
            FROM knowledge_sections s
            JOIN knowledge_documents d ON d.id = s.document_id
            {fallback_clause}
            LIMIT ?
            """,
            fallback_params,
        ).fetchall()
    )


def _select_knowledge_annotation_candidates(
    conn: Any,
    *,
    domain: str | None,
    fts_query: str,
    candidate_limit: int,
) -> list[Any]:
    domain_clause = "AND a.domain = ?" if domain else ""
    params: list[Any] = []
    if fts_query:
        params.append(fts_query)
    if domain:
        params.append(domain)
    params.append(candidate_limit)

    if fts_query:
        rows = conn.execute(
            f"""
            SELECT
                a.domain,
                a.path,
                a.title,
                a.summary,
                a.use_when_json,
                a.keywords_json,
                a.questions_json,
                a.source_type,
                a.trust_level,
                a.search_text,
                a.indexed_at
            FROM knowledge_annotations_fts
            JOIN knowledge_annotations a ON a.id = knowledge_annotations_fts.rowid
            WHERE knowledge_annotations_fts MATCH ?
            {domain_clause}
            LIMIT ?
            """,
            params,
        ).fetchall()
        if rows:
            return list(rows)

    fallback_clause = "WHERE domain = ?" if domain else ""
    fallback_params: list[Any] = [domain] if domain else []
    fallback_params.append(candidate_limit)
    return list(
        conn.execute(
            f"""
            SELECT
                domain,
                path,
                title,
                summary,
                use_when_json,
                keywords_json,
                questions_json,
                source_type,
                trust_level,
                search_text,
                indexed_at
            FROM knowledge_annotations
            {fallback_clause}
            LIMIT ?
            """,
            fallback_params,
        ).fetchall()
    )


def _load_knowledge_map() -> dict[str, Any]:
    try:
        payload = json.loads(KNOWLEDGE_MAP_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    domains = payload.get("domains")
    if domains is not None and not isinstance(domains, dict):
        return {}
    if isinstance(domains, dict):
        payload = {
            **payload,
            "domains": {
                str(key): _normalize_list_fields(value, _KNOWLEDGE_ROUTE_LIST_FIELDS)
                for key, value in domains.items()
                if isinstance(value, dict)
            },
        }
    return payload


@lru_cache(maxsize=1)
def _load_command_routes() -> dict[str, Any]:
    if not COMMAND_ROUTES_PATH.exists():
        return {"routes": []}
    try:
        payload = json.loads(COMMAND_ROUTES_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"routes": []}
    if not isinstance(payload, dict):
        return {"routes": []}
    routes = payload.get("routes")
    if not isinstance(routes, list):
        return {"routes": []}
    return {
        **payload,
        "routes": [_normalize_list_fields(route, _COMMAND_ROUTE_LIST_FIELDS) for route in routes if isinstance(route, dict)],
    }


def find_command_route(query: str, *, intent: str | None = None) -> dict[str, Any] | None:
    lowered = (query or "").casefold()
    normalized_intent = (intent or "").casefold()
    best: dict[str, Any] | None = None
    best_score = 0
    for raw_route in _load_command_routes().get("routes", []):
        route = dict(raw_route)
        score = 0
        matching_terms: list[str] = []
        if normalized_intent and normalized_intent == str(route.get("intent") or "").casefold():
            score += 40
            matching_terms.append(str(route.get("intent")))
        for alias in _string_list(route.get("aliases")):
            text = str(alias).casefold()
            if not text:
                continue
            if lowered == text:
                score += 100
                matching_terms.append(str(alias))
            elif text in lowered:
                score += 70
                matching_terms.append(str(alias))
        for keyword in _string_list(route.get("keywords")):
            text = str(keyword).casefold()
            if text and text in lowered:
                score += 15
                matching_terms.append(str(keyword))
        if score > best_score:
            route["score"] = score
            route["matching_terms"] = list(dict.fromkeys(matching_terms))
            best = route
            best_score = score
    return best if best_score >= 30 else None


def _build_route_card(domain: str, route: dict[str, Any]) -> _RouteCard:
    use_when = _unique_strings(_string_list(route.get("use_when")))
    primary_files = _unique_strings(_string_list(route.get("primary_files")))
    reference_files = _unique_strings(_string_list(route.get("reference_files")))
    optional_runtime_files = _optional_runtime_files(route)
    source_of_truth = _unique_strings(_string_list(route.get("source_of_truth_files"))) or primary_files[:3]
    aliases = _unique_strings(
        [
            domain,
            domain.replace("_", " "),
            *_string_list(route.get("aliases")),
        ]
    )
    keywords = _unique_strings(_string_list(route.get("keywords")))
    questions = _unique_strings(
        [
            *_string_list(route.get("questions")),
            *_string_list(route.get("questions_it_answers")),
        ]
    )
    required_context = _unique_strings(_string_list(route.get("required_context")))
    title = str(route.get("title") or f"{domain} route")
    skill_path = str(route.get("skill_path") or "")
    search_text = "\n".join(
        [
            domain,
            title,
            skill_path,
            *use_when,
            *aliases,
            *keywords,
            *questions,
            *source_of_truth,
            *primary_files,
            *reference_files,
            *optional_runtime_files,
            *required_context,
        ]
    ).lower()
    return _RouteCard(
        domain=domain,
        title=title,
        use_when=use_when,
        aliases=aliases,
        keywords=keywords,
        questions=questions,
        source_of_truth=source_of_truth,
        primary_files=primary_files,
        reference_files=reference_files,
        optional_runtime_files=optional_runtime_files,
        required_context=required_context,
        search_text=search_text,
    )


def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _optional_runtime_status(route: dict[str, Any]) -> dict[str, Any]:
    files = _optional_runtime_files(route)
    available_files: list[str] = []
    missing_files: list[str] = []
    for raw_path in files:
        resolved = _resolve_path(raw_path)
        if resolved.exists() and resolved.is_file():
            available_files.append(raw_path)
        else:
            missing_files.append(raw_path)

    note = ""
    if files and missing_files:
        note = "optional runtime files are missing; exact private facts are unavailable until local runtime files exist."
    elif files:
        note = "optional runtime files are available locally."

    return {
        "files": files,
        "available_files": available_files,
        "missing_files": missing_files,
        "all_available": bool(files) and not missing_files,
        "note": note,
    }


def _optional_runtime_files(route: dict[str, Any]) -> list[str]:
    return _unique_strings(
        [
            *_string_list(route.get("optional_runtime_files")),
            *_string_list(route.get("optional_files")),
        ]
    )


def _knowledge_root() -> Path:
    project_root = PROJECT_ROOT.resolve(strict=False)
    map_path = KNOWLEDGE_MAP_PATH.resolve(strict=False)
    if len(map_path.parents) >= 3 and map_path.parent.name == "agent" and map_path.parent.parent.name == "docs":
        return map_path.parents[2]
    if map_path == project_root or project_root in map_path.parents:
        return project_root
    return map_path.parent


def _unsafe_path_sentinel(raw_path: str) -> Path:
    digest = hashlib.sha256(str(raw_path).encode("utf-8", errors="replace")).hexdigest()[:12]
    return _knowledge_root() / ".invalid_knowledge_path" / digest


def _resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    root = _knowledge_root()
    candidate = path if path.is_absolute() else root / path
    try:
        resolved = candidate.resolve(strict=False)
        resolved_root = root.resolve(strict=False)
    except OSError:
        return _unsafe_path_sentinel(raw_path)
    if resolved == resolved_root or resolved_root in resolved.parents:
        return resolved
    return _unsafe_path_sentinel(raw_path)


def _document_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".md":
        return "markdown"
    if suffix == ".json":
        return "json"
    if suffix == ".jsonl":
        return "jsonl"
    return suffix.lstrip(".") or "file"


def _title_for(raw_path: str, content: str) -> str:
    first_heading = re.search(r"^#\s+(.+)$", content, flags=re.MULTILINE)
    if first_heading:
        return first_heading.group(1).strip()
    return Path(raw_path).name


def _index_knowledge_file(
    conn: Any,
    *,
    domain: str,
    raw_path: str,
    use_when: list[str],
    indexed_at: str,
) -> tuple[int, int]:
    resolved = _resolve_path(raw_path)
    content = resolved.read_text(encoding="utf-8-sig", errors="replace")
    document_type = _document_type(resolved)
    sections = _parse_sections(content, document_type=document_type)
    document_id = _insert_document(
        conn,
        domain=domain,
        path=raw_path,
        title=_title_for(raw_path, content),
        document_type=document_type,
        use_when=use_when,
        content=content,
        indexed_at=indexed_at,
    )
    sections_indexed = _insert_sections(
        conn,
        document_id=document_id,
        domain=domain,
        path=raw_path,
        sections=sections,
        indexed_at=indexed_at,
    )
    return 1, sections_indexed


def _parse_sections(content: str, *, document_type: str) -> list[_Section]:
    if document_type == "markdown":
        return _parse_markdown_sections(content)
    if document_type == "json":
        return _parse_json_sections(content)
    if document_type == "jsonl":
        return _parse_jsonl_sections(content)
    if document_type == "csv":
        return _parse_csv_sections(content)
    return [_Section("Document", 1, _clip(content), 0)]


def _parse_markdown_sections(content: str) -> list[_Section]:
    matches = list(re.finditer(r"^(#{1,6})\s+(.+)$", content, flags=re.MULTILINE))
    if not matches:
        return [_Section("Document", 1, _clip(content), 0)]

    sections: list[_Section] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        heading = match.group(2).strip()
        level = len(match.group(1))
        section_content = content[start:end].strip()
        sections.append(_Section(heading, level, _clip(section_content), index))
    return sections


def _parse_json_sections(content: str) -> list[_Section]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return [_Section("JSON Document", 1, _clip(content), 0)]

    if isinstance(payload, dict):
        sections: list[_Section] = []
        for index, (key, value) in enumerate(payload.items()):
            dumped = json.dumps(value, ensure_ascii=False, indent=2)
            sections.append(_Section(str(key), 1, _clip(dumped), index))
        return sections or [_Section("JSON Document", 1, "{}", 0)]
    dumped = json.dumps(payload, ensure_ascii=False, indent=2)
    return [_Section("JSON Document", 1, _clip(dumped), 0)]


def _parse_jsonl_sections(content: str) -> list[_Section]:
    sections: list[_Section] = []
    for index, line in enumerate(content.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            sections.append(_Section(f"JSONL row {index + 1}", 1, _clip(line), index))
            continue
        if isinstance(payload, dict):
            heading = _jsonl_heading(payload, index)
            dumped = json.dumps(payload, ensure_ascii=False, indent=2)
            sections.append(_Section(heading, 1, _clip(dumped), index))
            continue
        dumped = json.dumps(payload, ensure_ascii=False, indent=2)
        sections.append(_Section(f"JSONL row {index + 1}", 1, _clip(dumped), index))
    return sections or [_Section("JSONL Document", 1, "", 0)]


def _parse_csv_sections(content: str) -> list[_Section]:
    try:
        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)
    except csv.Error:
        return [_Section("CSV Document", 1, _clip(content), 0)]
    if not reader.fieldnames or not rows:
        return [_Section("CSV Document", 1, _clip(content), 0)]

    sections: list[_Section] = []
    for index, row in enumerate(rows):
        normalized = {str(key or "").strip(): str(value or "").strip() for key, value in row.items() if key}
        if not any(normalized.values()):
            continue
        heading = _csv_heading(normalized, index)
        dumped = json.dumps(normalized, ensure_ascii=False, indent=2)
        sections.append(_Section(heading, 1, _clip(dumped), index))
    return sections or [_Section("CSV Document", 1, _clip(content), 0)]


def _jsonl_heading(payload: dict[str, Any], index: int) -> str:
    preferred_keys = [
        "code",
        "scenario_id",
        "risk_id",
        "source_id",
        "engine",
        "symptom",
        "symptom_ru",
        "effect",
        "system",
        "abbreviation",
        "abbr",
        "term",
        "transmission",
        "chassis",
        "body_code",
        "publisher",
        "title",
    ]
    parts: list[str] = []
    for key in preferred_keys:
        value = str(payload.get(key) or "").strip()
        if value:
            parts.append(value)
        if len(parts) == 2:
            break
    return " - ".join(parts) if parts else f"JSONL row {index + 1}"


def _csv_heading(row: dict[str, str], index: int) -> str:
    preferred_keys = [
        "module",
        "extension",
        "sid",
        "code",
        "item",
        "title",
        "source_id",
        "name",
        "service",
        "domain",
        "full_name",
    ]
    parts: list[str] = []
    for key in preferred_keys:
        value = str(row.get(key) or "").strip()
        if value:
            parts.append(value)
        if len(parts) == 2:
            break
    return " - ".join(parts) if parts else f"CSV row {index + 1}"


def _insert_route_card(conn: Any, card: _RouteCard, *, indexed_at: str) -> None:
    digest_payload = {
        "domain": card.domain,
        "title": card.title,
        "use_when": card.use_when,
        "aliases": card.aliases,
        "keywords": card.keywords,
        "questions": card.questions,
        "source_of_truth": card.source_of_truth,
        "primary_files": card.primary_files,
        "reference_files": card.reference_files,
        "optional_runtime_files": card.optional_runtime_files,
        "required_context": card.required_context,
    }
    digest = hashlib.sha256(
        json.dumps(digest_payload, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="replace")
    ).hexdigest()
    conn.execute(
        """
        INSERT INTO knowledge_route_cards
            (
                domain,
                title,
                use_when_json,
                aliases_json,
                keywords_json,
                questions_json,
                source_of_truth_json,
                primary_files_json,
                reference_files_json,
                required_context_json,
                search_text,
                content_hash,
                indexed_at
            )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            card.domain,
            card.title,
            _json_list(card.use_when),
            _json_list(card.aliases),
            _json_list(card.keywords),
            _json_list(card.questions),
            _json_list(card.source_of_truth),
            _json_list(card.primary_files),
            _json_list(card.reference_files),
            _json_list(card.required_context),
            card.search_text,
            digest,
            indexed_at,
        ),
    )


def _insert_document(
    conn: Any,
    *,
    domain: str,
    path: str,
    title: str,
    document_type: str,
    use_when: list[str],
    content: str,
    indexed_at: str,
) -> int:
    digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    cursor = conn.execute(
        """
        INSERT INTO knowledge_documents
            (domain, path, title, document_type, use_when_json, content_hash, indexed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (domain, path, title, document_type, _json_list(use_when), digest, indexed_at),
    )
    return int(cursor.lastrowid)


def _insert_sections(
    conn: Any,
    *,
    document_id: int,
    domain: str,
    path: str,
    sections: list[_Section],
    indexed_at: str,
) -> int:
    count = 0
    for section in sections:
        preview = _preview(section.content)
        search_text = "\n".join([domain, path, section.heading, section.content]).lower()
        cursor = conn.execute(
            """
            INSERT INTO knowledge_sections
                (document_id, domain, path, heading, level, ordinal, content, preview, search_text, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                domain,
                path,
                section.heading,
                section.level,
                section.ordinal,
                section.content,
                preview,
                search_text,
                indexed_at,
            ),
        )
        section_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO knowledge_sections_fts
                (rowid, domain, path, heading, search_text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (section_id, domain, path, section.heading, search_text),
        )
        count += 1
    return count


def _insert_annotations(conn: Any, *, indexed_at: str) -> int:
    count = 0
    for annotation in _load_annotations():
        annotation_id = str(annotation.get("annotation_id") or "").strip()
        domain = str(annotation.get("domain") or "").strip()
        path = str(annotation.get("path") or "").strip()
        if not annotation_id or not domain or not path:
            continue
        title = str(annotation.get("title") or "").strip()
        summary = str(annotation.get("summary") or "").strip()
        use_when = _unique_strings(_string_list(annotation.get("use_when")))
        keywords = _unique_strings(_string_list(annotation.get("keywords")))
        questions = _unique_strings(_string_list(annotation.get("questions")))
        safety_flags = _unique_strings(_string_list(annotation.get("safety_flags")))
        related_skills = _unique_strings(_string_list(annotation.get("related_skills")))
        source_type = str(annotation.get("source_type") or "").strip()
        trust_level = str(annotation.get("trust_level") or "").strip()
        refresh_cadence = str(annotation.get("refresh_cadence") or "").strip()
        search_text = "\n".join(
            [
                annotation_id,
                domain,
                path,
                title,
                summary,
                source_type,
                trust_level,
                refresh_cadence,
                *use_when,
                *keywords,
                *questions,
                *safety_flags,
                *related_skills,
            ]
        ).lower()
        cursor = conn.execute(
            """
            INSERT INTO knowledge_annotations
                (
                    annotation_id,
                    domain,
                    path,
                    title,
                    summary,
                    use_when_json,
                    keywords_json,
                    questions_json,
                    source_type,
                    trust_level,
                    refresh_cadence,
                    safety_flags_json,
                    related_skills_json,
                    search_text,
                    indexed_at
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                annotation_id,
                domain,
                path,
                title,
                summary,
                _json_list(use_when),
                _json_list(keywords),
                _json_list(questions),
                source_type,
                trust_level,
                refresh_cadence,
                _json_list(safety_flags),
                _json_list(related_skills),
                search_text,
                indexed_at,
            ),
        )
        annotation_id_int = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO knowledge_annotations_fts
                (rowid, domain, path, title, search_text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (annotation_id_int, domain, path, title, search_text),
        )
        count += 1
    return count


def _load_annotations() -> list[dict[str, Any]]:
    if not KNOWLEDGE_ANNOTATIONS_PATH.exists():
        return []
    annotations: list[dict[str, Any]] = []
    try:
        annotations_content = KNOWLEDGE_ANNOTATIONS_PATH.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return []
    for line in annotations_content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            annotations.append(_normalize_list_fields(payload, _ANNOTATION_LIST_FIELDS))
    return annotations


def _clip(value: str) -> str:
    value = value.strip()
    return value[:MAX_SECTION_CHARS]


def _preview(value: str) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    return compact[:MAX_PREVIEW_CHARS]


def _tokens(query: str) -> list[str]:
    aliases = {
        "бмв": ["bmw"],
        "тойота": ["toyota"],
        "ярис": ["yaris"],
        "кузов": ["body", "chassis"],
        "кузова": ["body", "chassis", "frame"],
        "мотор": ["engine"],
        "двигатель": ["engine"],
        "электрика": ["electrical", "electronics", "wiring"],
        "электроника": ["electrical", "electronics", "control_unit"],
        "сцепление": ["clutch", "transmission"],
        "раздатка": ["transfer", "transfer_case", "xdrive", "driveline"],
        "пинки": ["shudder", "jerk", "driveline"],
        "акпп": ["transmission", "automatic", "zf"],
        "коробка": ["transmission", "gearbox"],
        "масло": ["oil", "fluid", "fluids", "engine_oil"],
        "масла": ["oil", "fluid", "fluids", "engine_oil"],
        "моторное": ["engine", "engine_oil", "oil"],
        "моторный": ["engine", "engine_oil", "oil"],
        "жидкость": ["fluid", "fluids", "capacity"],
        "жидкости": ["fluid", "fluids", "capacity"],
        "заправка": ["fill", "capacity", "service"],
        "заправочные": ["fill", "capacity", "service"],
        "то": ["maintenance", "service", "fluids"],
        "диагностика": ["diagnostics", "diagnosis", "repair"],
        "ошибка": ["dtc", "fault", "diagnostics"],
        "ошибки": ["dtc", "fault", "diagnostics"],
        "память": ["memory", "recall"],
        "воспоминание": ["memory", "recall"],
        "воспоминания": ["memory", "recall"],
        "индексация": ["indexing", "index"],
        "индексацию": ["индексация", "indexing", "index"],
        "аннотация": ["annotation", "annotations"],
        "аннотации": ["аннотация", "annotation", "annotations"],
        "разметка": ["annotation", "metadata", "indexing"],
        "знаний": ["знания", "knowledge"],
        "качество": ["quality"],
        "устаревшие": ["stale", "expired"],
        "дубли": ["duplicates", "duplicate"],
        "свечи": ["spark", "plugs", "parts", "oem"],
        "свеча": ["spark", "plug", "parts", "oem"],
        "колодки": ["brake", "pads", "parts", "oem"],
        "колодка": ["brake", "pad", "parts", "oem"],
        "фильтр": ["filter", "parts", "oem"],
        "фильтры": ["filters", "parts", "oem"],
        "запчасти": ["parts", "spare_parts", "procurement"],
        "запчасть": ["parts", "spare_part", "procurement"],
        "рулевую": ["рулевая", "steering"],
        "рулевая": ["steering"],
        "рейку": ["рейка", "rack", "steering_rack"],
        "рейка": ["rack", "steering_rack"],
        "рейки": ["рейка", "rack", "steering_rack"],
        "контрактную": ["контрактная", "contract", "used"],
        "контрактная": ["contract", "used"],
        "контрактные": ["contract", "used"],
        "красноярске": ["красноярск", "krasnoyarsk"],
        "красноярск": ["krasnoyarsk"],
        "закупка": ["procurement", "purchase_price"],
        "закупочная": ["procurement", "purchase_price"],
        "аналоги": ["analog", "cross", "replacements"],
        "аналог": ["analog", "cross", "replacement"],
        "кроссы": ["cross", "crosses", "replacements"],
        "кросс": ["cross", "replacement"],
        "оригинальный": ["oem", "original", "catalog"],
        "каталожный": ["oem", "catalog", "part_number"],
    }
    tokens: list[str] = []
    for token in re.findall(r"[\w\-]+", query.lower(), flags=re.UNICODE):
        if len(token) < 2:
            continue
        if token in STOPWORDS:
            continue
        tokens.append(token)
        tokens.extend(aliases.get(token, []))
    return list(dict.fromkeys(tokens))


def _knowledge_fts_query(tokens: list[str]) -> str:
    escaped: list[str] = []
    for token in tokens:
        text = token.replace('"', '""').strip()
        if text:
            escaped.append(f'"{text}"')
    return " OR ".join(escaped)


def _domain_hints(query: str) -> dict[str, int]:
    lowered = query.lower()
    hints: dict[str, int] = {}
    knowledge_intake_terms = [
        "база знаний",
        "базу знаний",
        "базе знаний",
        "knowledge base",
        "knowledge",
        "индексац",
        "аннотац",
        "разметк",
        "полк",
        "source pack",
    ]
    knowledge_intake_actions = [
        "обнови",
        "добавь",
        "добавить",
        "сохрани",
        "сохранить",
        "внеси",
        "внести",
        "усиль",
        "усилить",
        "структур",
        "систематиз",
        "проиндекс",
    ]
    if any(term in lowered for term in knowledge_intake_terms) and any(
        action in lowered for action in knowledge_intake_actions
    ):
        hints["knowledge_intake"] = max(hints.get("knowledge_intake", 0), 55)
    if any(word in lowered for word in ["масло", "моторное", "жидк", "заправ", " то "]):
        hints["fluids"] = 20
    if any(word in lowered for word in ["диагност", "ошиб", "dtc", "скан"]):
        hints["automotive_repair"] = 10
    if any(word in lowered for word in ["вин", "vin", "oem", "каталог", "кузов"]):
        hints["vehicle_identity_and_oem"] = 10
    crm_vin_terms = ["crm", "карточк", "заказ-наряд", "зн", "writeback", "запиши", "записать"]
    part_terms = [
        "запчаст",
        "детал",
        "свеч",
        "колод",
        "фильтр",
        "oem",
        "каталож",
        "оригиналь",
        "аналог",
        "кросс",
        "закуп",
        "цена",
    ]
    identifier_terms = ["вин", "vin", "frame", "кузов", "body number", "номер кузова"]
    if (
        any(word in lowered for word in identifier_terms)
        and any(word in lowered for word in part_terms)
        and (any(word in lowered for word in crm_vin_terms) or any(word in lowered for word in ["закуп", "цена", "аналог", "кросс"]))
    ):
        hints["crm_vin_oem_parts_lookup"] = max(hints.get("crm_vin_oem_parts_lookup", 0), 34)
        hints["parts_sourcing"] = max(hints.get("parts_sourcing", 0), 12)
    if not any(word in lowered for word in identifier_terms) and any(
        word in lowered for word in ["заказ-наряд", "зн", "материал", "материалы", "заменитель", "цена", "закуп"]
    ):
        hints["parts_sourcing"] = max(hints.get("parts_sourcing", 0), 40)
    if any(
        word in lowered
        for word in [
            "красноярск",
            "дром",
            "zzap",
            "ззап",
            "avito",
            "авито",
            "контракт",
            "разбор",
            "наличие",
            "поставщик",
            "рулев",
            "рейк",
        ]
    ):
        hints["parts_sourcing"] = max(hints.get("parts_sourcing", 0), 36)
    if any(word in lowered for word in ["bmw", "бмв", "n63", "f15", "e15", "x5"]):
        hints["bmw_repair"] = max(hints.get("bmw_repair", 0), 10)
    if any(word in lowered for word in ["f15", "e15", "n63", "x5"]):
        hints["bmw_f15_n63"] = max(hints.get("bmw_f15_n63", 0), 18)
    if any(word in lowered for word in ["toyota", "тойота", "yaris gr", "gr yaris", "ярис", "gxpa16", "g16e"]):
        hints["toyota_gr_yaris"] = max(hints.get("toyota_gr_yaris", 0), 18)
    if any(word in lowered for word in ["приберись", "board_cleanup_autopilot", "cleanup"]):
        hints["board_cleanup_autopilot"] = max(hints.get("board_cleanup_autopilot", 0), 30)
    return hints


def _score_route_card(
    item: dict[str, Any],
    tokens: list[str],
    query: str,
    *,
    domain_hints: dict[str, int],
) -> tuple[int, list[str]]:
    annotation_text = str(item.get("annotation_text") or "").lower()
    haystack = "\n".join([str(item.get("search_text") or ""), annotation_text]).lower()
    domain = str(item.get("domain") or "").lower()
    title = str(item.get("title") or "").lower()
    aliases = " ".join(json.loads(item.get("aliases_json") or "[]")).lower()
    keywords = " ".join(json.loads(item.get("keywords_json") or "[]")).lower()
    source_paths = " ".join(json.loads(item.get("source_of_truth_json") or "[]")).lower()
    score = domain_hints.get(domain, 0)
    matching_terms: list[str] = []
    lowered_query = query.lower()

    if lowered_query and lowered_query in haystack:
        score += 30
        matching_terms.append(lowered_query)

    for token in tokens:
        token_score = 0
        if token in domain:
            token_score += 8
        if token in aliases:
            token_score += 7
        if token in keywords:
            token_score += 5
        if token in title:
            token_score += 4
        if token in source_paths:
            token_score += 3
        if token in annotation_text:
            token_score += 8
        occurrences = haystack.count(token)
        if occurrences:
            token_score += min(occurrences, 4)
        if token_score:
            score += token_score
            matching_terms.append(token)

    return score, list(dict.fromkeys(matching_terms))[:12]


def _confidence(score: int) -> float:
    if score <= 0:
        return 0.0
    return round(min(0.99, score / 35), 2)


def _score(item: dict[str, Any], tokens: list[str], query: str, *, domain_hints: dict[str, int]) -> int:
    haystack = str(item.get("search_text") or "").lower()
    domain = str(item.get("domain") or "").lower()
    path = str(item.get("path") or "").lower()
    heading = str(item.get("heading") or "").lower()
    score = domain_hints.get(domain, 0)
    if query and query.lower() in haystack:
        score += 20
    for token in tokens:
        occurrences = haystack.count(token)
        if occurrences:
            score += min(occurrences, 5)
        if token in domain:
            score += 4
        if token in heading:
            score += 3
        if token in path:
            score += 2
    if item.get("document_type") == "domain_route" and (score > 0 or not tokens):
        score += 3
    return score


def _document_type_boost(document_type: str, domain: str, domain_hints: dict[str, int]) -> int:
    if document_type != "domain_route":
        return 0
    boost = 4
    if domain_hints.get(str(domain or "").lower(), 0) >= 15:
        boost += 12
    return boost
