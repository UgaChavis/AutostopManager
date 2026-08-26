from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .storage import ManagerMemoryStore, _json_list, _now, _string_list


KNOWLEDGE_MAP_PATH = PROJECT_ROOT / "docs" / "agent" / "knowledge_map.json"
COMMAND_ROUTES_PATH = PROJECT_ROOT / "docs" / "agent" / "command_routes.json"
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
    "какой",
    "какая",
    "какие",
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
    "сколько",
    "есть",
    "ли",
    "нужно",
    "надо",
    "можно",
    "the",
    "and",
    "or",
    "for",
    "with",
    "without",
}

_PROJECT_ENGINEERING_RE = re.compile(
    r"autostop\s*manager|автостоп\s*менеджер|архитектур|исходн\w*\s+код|кодовая\s+баз|проект|репозитор"
    r"|agent-brief|knowledge-probe|gateway\s+v2|action\s+contract|prepare_action_contract|workflow\s+metadata"
    r"|dry_run\s+metadata|маршрутизац",
    re.IGNORECASE,
)
_PROJECT_ENGINEERING_ACTION_RE = re.compile(
    r"аудит|баг|дефект|отлад|оптимиз|рефактор|тест|улучш|исправ|почин|маршрутизац|документац|обнов",
    re.IGNORECASE,
)

_SCOPE_EXCLUSION_RE = re.compile(
    r"\b(?:без\s+работы\s+с(?:о)?|without)\s+(?:the\s+)?(?P<after>[\w-]+)"
    r"|\bбез\s+(?P<bare>store|магазин\w*|crm|telegram|телеграм\w*|gmail|почт\w*|vpn|впн|камер\w*|сервер\w*)"
    r"|\b(?P<before>(?!(?:но|but)\b)[\w-]+)\s+(?:(?:пока\s+)?не\s+(?:трог\w*|заним\w*|использ\w*|инспектир\w*|провер\w*|диагностир\w*|меня\w*|обнов\w*)|на\s+паузе|в\s+разработке)"
    r"|\b(?:не\s+(?:трог\w*|заним\w*|использ\w*)|(?:do\s+not|don't)\s+(?:touch|use|work\s+on))"
    r"\s+(?:the\s+)?(?P<action>[\w-]+)"
)


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

_COMMAND_ROUTE_LIST_FIELDS = (
    "knowledge_domains",
    "effects",
    "dependencies",
)


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
    route_cards_indexed = len(domains)
    with memory.connect() as conn:
        conn.execute("DELETE FROM knowledge_sections_fts")
        conn.execute("DELETE FROM knowledge_sections")
        conn.execute("DELETE FROM knowledge_documents")

        for domain, route in domains.items():
            use_when = _string_list(route.get("use_when"))
            primary_files = _string_list(route.get("primary_files"))
            optional_runtime_files = _optional_runtime_files(route)
            optional_status = {
                raw_path: (_resolve_path(raw_path).exists() and _resolve_path(raw_path).is_file())
                for raw_path in optional_runtime_files
            }
            optional_missing_for_domain = [
                raw_path for raw_path, is_present in optional_status.items() if not is_present
            ]
            optional_missing.extend(optional_missing_for_domain)

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
    return {
        "ok": True,
        "map_path": str(KNOWLEDGE_MAP_PATH),
        "route_cards_indexed": route_cards_indexed,
        "documents_indexed": documents_indexed,
        "sections_indexed": sections_indexed,
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
    preferred_domains: list[str] | None = None,
) -> dict[str, Any]:
    """Select local documents only; command routing is deliberately out of scope."""

    del store
    limit = max(1, min(limit, 20))
    query = (query or "").strip()
    excluded_scopes = _scope_exclusions(query)
    tokens = [
        token
        for token in _tokens(query)
        if not any(_term_count(token, scope) or _term_count(scope, token) for scope in excluded_scopes)
    ]
    domain_hints = _domain_hints(query)
    for domain in _unique_strings(preferred_domains or []):
        domain_hints[domain] = max(domain_hints.get(domain, 0), 100)
    route_definitions = _load_knowledge_map().get("domains") or {}
    loaded_at = _now()
    routes: list[dict[str, Any]] = []
    for domain, definition in route_definitions.items():
        card = _build_route_card(str(domain), definition)
        item = {
            "domain": card.domain,
            "title": card.title,
            "use_when_json": _json_list(card.use_when),
            "aliases_json": _json_list(card.aliases),
            "keywords_json": _json_list(card.keywords),
            "source_of_truth_json": _json_list(card.source_of_truth),
            "search_text": card.search_text,
        }
        if _route_excluded(item, excluded_scopes):
            continue
        score, matching_terms = _score_route_card(item, tokens, query, domain_hints=domain_hints)
        if tokens and score <= 0:
            continue
        source_of_truth = card.source_of_truth
        primary_files = card.primary_files
        reference_files = card.reference_files
        open_first = (source_of_truth or primary_files or [""])[0]
        runtime_status = _optional_runtime_status(definition)
        route = {
            "domain": card.domain,
            "title": card.title,
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
            "required_context": card.required_context,
            "use_when": card.use_when,
            "indexed_at": loaded_at,
        }
        routes.append(route)

    routes.sort(key=lambda value: (value["score"], len(value["matching_terms"])), reverse=True)
    routes = routes[:limit]
    if routes and routes[0]["score"] >= 12:
        routes = [route for route in routes if route["score"] >= 12]
    if routes:
        top_score = max(int(routes[0]["score"]), 1)
        for route in routes:
            route["confidence"] = round(min(route["confidence"], 0.95 * int(route["score"]) / top_score), 2)
    best = routes[0] if routes else None
    confidence = float(best["confidence"]) if best else 0.0
    if best and len(routes) > 1:
        margin = (int(best["score"]) - int(routes[1]["score"])) / max(int(best["score"]), 1)
        confidence = round(min(confidence, 0.55 + 0.4 * margin), 2)
    has_knowledge = bool(best and best["score"] >= 12 and confidence >= 0.45)
    ambiguous = bool(best and len(routes) > 1 and routes[1]["score"] >= best["score"] * 0.75)

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
        "command_route": None,
        "routes": routes,
        "ambiguous": ambiguous,
        "next_action": "compare_route_candidates"
        if has_knowledge and ambiguous
        else "open_source_of_truth"
        if has_knowledge
        else "route_external_sources",
        "needs_broad_search": not has_knowledge,
        "probed_at": _now(),
    }


def _audit_route_paths(
    domains: dict[str, Any],
) -> tuple[list[str], list[str], list[str], list[str]]:
    missing_files: list[str] = []
    optional_missing_files: list[str] = []
    domains_without_source_of_truth: list[str] = []
    domains_without_aliases: list[str] = []
    checked_paths: set[str] = set()
    checked_optional_paths: set[str] = set()

    for domain, route in domains.items():
        if not route.get("primary_files"):
            domains_without_source_of_truth.append(domain)
        required_paths = _unique_strings(
            [
                *_string_list(route.get("source_of_truth_files")),
                *_string_list(route.get("primary_files")),
                *_string_list(route.get("reference_files")),
            ]
        )
        for raw_path in required_paths:
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
    return (
        missing_files,
        optional_missing_files,
        domains_without_source_of_truth,
        domains_without_aliases,
    )


def _knowledge_index_counts(memory: ManagerMemoryStore, *, route_card_count: int) -> dict[str, int]:
    with memory.connect() as conn:
        return {
            "route_cards": route_card_count,
            "documents": int(
                conn.execute("SELECT COUNT(*) AS count FROM knowledge_documents").fetchone()["count"] or 0
            ),
            "sections": int(conn.execute("SELECT COUNT(*) AS count FROM knowledge_sections").fetchone()["count"] or 0),
            "sections_fts": int(
                conn.execute("SELECT COUNT(*) AS count FROM knowledge_sections_fts").fetchone()["count"] or 0
            ),
        }


def _knowledge_audit_warnings(
    *,
    domain_count: int,
    counts: dict[str, int],
    missing_files: list[str],
    domains_without_source_of_truth: list[str],
    domains_without_aliases: list[str],
) -> list[str]:
    warnings: list[str] = []
    if counts["route_cards"] != domain_count:
        warnings.append("route card count does not match knowledge_map domain count")
    if domains_without_source_of_truth:
        warnings.append("some domains do not declare primary source files")
    if missing_files:
        warnings.append("some mapped files are missing")
    if counts["sections_fts"] != counts["sections"]:
        warnings.append("knowledge_sections_fts_count_mismatch")
    return warnings


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
    if _document_count(memory) == 0:
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
            "missing_files": [],
            "optional_missing_files": [],
            "missing_optional_files": [],
            "domains_without_source_of_truth": [],
            "domains_without_aliases": [],
            "warnings": ["knowledge_map_has_no_valid_domains"],
            "checked_at": _now(),
        }
    (
        missing_files,
        optional_missing_files,
        domains_without_source_of_truth,
        domains_without_aliases,
    ) = _audit_route_paths(domains)
    counts = _knowledge_index_counts(memory, route_card_count=len(domains))
    route_cards_indexed = counts["route_cards"]
    documents_indexed = counts["documents"]
    sections_indexed = counts["sections"]
    sections_fts_indexed = counts["sections_fts"]
    warnings = _knowledge_audit_warnings(
        domain_count=len(domains),
        counts=counts,
        missing_files=missing_files,
        domains_without_source_of_truth=domains_without_source_of_truth,
        domains_without_aliases=domains_without_aliases,
    )

    ok = (
        not missing_files
        and route_cards_indexed == len(domains)
        and documents_indexed > 0
        and sections_indexed > 0
        and sections_fts_indexed == sections_indexed
    )
    return {
        "ok": ok,
        "map_path": str(KNOWLEDGE_MAP_PATH),
        "domain_count": len(domains),
        "route_cards_indexed": route_cards_indexed,
        "documents_indexed": documents_indexed,
        "sections_indexed": sections_indexed,
        "sections_fts_indexed": sections_fts_indexed,
        "missing_files": missing_files,
        "optional_missing_files": optional_missing_files,
        "missing_optional_files": optional_missing_files,
        "domains_without_source_of_truth": domains_without_source_of_truth,
        "domains_without_aliases": domains_without_aliases,
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
                "score": score,
                "indexed_at": item["indexed_at"],
            }
        )

    ranked.sort(key=lambda value: value["score"], reverse=True)
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
        "routes": [_normalize_command_route(route) for route in routes if isinstance(route, dict)],
    }


def _normalize_command_route(route: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_list_fields(route, _COMMAND_ROUTE_LIST_FIELDS)
    raw_signals = route.get("signals")
    signals: dict[str, Any] = raw_signals if isinstance(raw_signals, dict) else {}
    normalized["signals"] = {
        "phrases": _string_list(signals.get("phrases")),
        "all": [
            _string_list(group) if isinstance(group, list) else _string_list([group])
            for group in signals.get("all", [])
            if isinstance(group, (str, list))
        ],
        "any": _string_list(signals.get("any")),
        "exclude": _string_list(signals.get("exclude")),
    }
    return normalized


def _scope_exclusions(query: str) -> set[str]:
    return {
        next(value for value in match.groups() if value).casefold()
        for match in _SCOPE_EXCLUSION_RE.finditer((query or "").casefold())
    }


def _route_excluded(route: dict[str, Any], scopes: set[str]) -> bool:
    scope_text = " ".join(
        str(route.get(key) or "")
        for key in ("domain", "title", "knowledge_domains", "signals", "use_when_json", "aliases_json")
    ).casefold()
    return any(_term_count(scope_text, scope) for scope in scopes)


def plan_command_routes(query: str, *, intent: str | None = None) -> list[dict[str, Any]]:
    """Return every independently matched workflow in safe execution order."""

    lowered = (query or "").casefold()
    normalized_intent = (intent or "").casefold()
    excluded_scopes = _scope_exclusions(query)
    available = [
        dict(route) for route in _load_command_routes().get("routes", []) if not _route_excluded(route, excluded_scopes)
    ]
    if normalized_intent:
        explicit = [route for route in available if normalized_intent == str(route.get("intent") or "").casefold()]
        if explicit:
            available = explicit

    matches: list[dict[str, Any]] = []
    for route in available:
        if normalized_intent and normalized_intent == str(route.get("intent") or "").casefold():
            score, terms = 1000, [str(route.get("intent") or "")]
        else:
            score, terms = _score_command_signals(lowered, route.get("signals") or {})
        if score <= 0:
            continue
        route["score"] = score
        route["confidence"] = 1.0 if score >= 1000 else round(min(0.98, 0.55 + score / 250), 2)
        route["uncertainty"] = round(1.0 - float(route["confidence"]), 2)
        route["matching_terms"] = terms
        route["domain"] = (route.get("knowledge_domains") or [None])[0]
        matches.append(route)

    matches.sort(
        key=lambda route: (
            int(route.get("phase") or 0),
            -int(route.get("priority") or 0),
            str(route.get("workflow_id") or route.get("command_id") or ""),
        )
    )
    return matches


def _score_command_signals(lowered: str, signals: dict[str, Any]) -> tuple[int, list[str]]:
    if any(_signal_present(lowered, term) for term in _string_list(signals.get("exclude"))):
        return 0, []
    matched: list[str] = []
    phrase_score = 0
    for phrase in _string_list(signals.get("phrases")):
        if not _signal_present(lowered, phrase):
            continue
        matched.append(phrase)
        phrase_score = max(phrase_score, 120 if lowered.strip() == phrase.casefold() else 70)

    all_groups = signals.get("all") if isinstance(signals.get("all"), list) else []
    all_score = 0
    if all_groups:
        for group in all_groups:
            term = next((term for term in _string_list(group) if _signal_present(lowered, term)), None)
            if term is None:
                all_score = 0
                break
            matched.append(term)
            all_score += 35

    any_score = 0
    for term in _string_list(signals.get("any")):
        if _signal_present(lowered, term):
            matched.append(term)
            any_score += 10
    base = max(phrase_score, all_score)
    return (base + min(any_score, 30), list(dict.fromkeys(matched))) if base else (0, [])


def _signal_present(lowered: str, signal: str) -> bool:
    text = str(signal or "").casefold().strip()
    return bool(text and _term_count(lowered, text))


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
        note = (
            "optional runtime files are missing; exact private facts are unavailable until local runtime files exist."
        )
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
        "грм": [
            "timing",
            "timing_belt",
            "timing_chain",
            "camshaft_timing",
            "repair_procedures",
            "torque_specs",
            "special_tools",
        ],
        "метка": ["timing", "camshaft_timing", "repair_procedures"],
        "метки": ["timing", "camshaft_timing", "repair_procedures"],
        "фаза": ["timing", "camshaft_timing", "repair_procedures"],
        "фазы": ["timing", "camshaft_timing", "repair_procedures"],
        "цепь": ["timing_chain", "timing", "repair_procedures"],
        "цепи": ["timing_chain", "timing", "repair_procedures"],
        "ремень": ["timing_belt", "timing", "repair_procedures"],
        "ремня": ["timing_belt", "timing", "repair_procedures"],
        "распредвал": ["camshaft", "camshaft_timing", "timing"],
        "распредвалы": ["camshaft", "camshaft_timing", "timing"],
        "коленвал": ["crankshaft", "timing", "repair_procedures"],
        "коленвала": ["crankshaft", "timing", "repair_procedures"],
        "момент": ["torque", "torque_specs"],
        "моменты": ["torque", "torque_specs"],
        "затяжка": ["torque", "torque_specs"],
        "затяжки": ["torque", "torque_specs"],
        "доворот": ["angle_torque", "torque_specs"],
        "гбц": ["cylinder_head", "torque_specs", "repair_procedures"],
        "регламент": ["maintenance", "maintenance_intervals", "service_information"],
        "интервал": ["maintenance", "maintenance_intervals", "service_information"],
        "устройство": ["system_operation", "repair", "service_information"],
        "агрегат": ["component", "assembly", "repair"],
        "агрегата": ["component", "assembly", "repair"],
        "форум": ["forum", "forum_research", "web_research"],
        "форумы": ["forum", "forum_research", "web_research"],
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


def _term_count(text: str, term: str) -> int:
    if len(term) <= 3:
        return len(re.findall(rf"(?<![^\W_]){re.escape(term)}(?![^\W_])", text))
    return text.count(term)


def _domain_hints(query: str) -> dict[str, int]:
    lowered = query.lower()
    hints: dict[str, int] = {}
    documentation_terms = (
        "документац",
        "инструкц",
        "база знаний",
        "базу знаний",
        "базе знаний",
        "knowledge base",
        "knowledge",
        "documentation",
        "индексац",
        "аннотац",
        "разметк",
        "полк",
        "source pack",
    )
    documentation_actions = (
        "обнов",
        "актуализ",
        "привести в актуаль",
        "приведи в актуаль",
        "почист",
        "cleanup",
        "очист",
        "удал",
        "убер",
        "мусор",
        "стар",
        "неактуаль",
        "добав",
        "сохран",
        "внес",
        "усил",
        "структур",
        "систематиз",
        "проиндекс",
        "разберись",
    )
    if any(term in lowered for term in documentation_terms) and any(
        action in lowered for action in documentation_actions
    ):
        hints["knowledge_intake"] = 90

    remote_access_terms = (
        "home-pc",
        "managed-pc",
        "autostop_remote",
        "reverse ssh",
        "удаленный компьютер",
        "удалённый компьютер",
        "удаленного компьютера",
        "удалённого компьютера",
        "домашний компьютер",
        "домашний пк",
        "windows компьютер",
        "windows пк",
    )
    if any(term in lowered for term in remote_access_terms):
        hints["remote_codex_access"] = 82
    if any(term in lowered for term in ("инфраструктур", "сервер", "резервн", "backup", "backups")):
        hints["remote_codex_access"] = max(hints.get("remote_codex_access", 0), 50)
    store_context_terms = (
        "магазин",
        "нашем каталоге",
        "нашего каталога",
        "на складе",
        "состояние склада",
        "место хранения",
        "где она лежит",
        "где лежит",
        "физически, зарезервировано",
        "физический остаток",
        "зарезервирован",
        "доступный остаток",
        "заявк на подбор",
        "запрос на процен",
        "заказ на процен",
        "заявк на процен",
        "проценк запчаст",
        "приходы и отгрузки",
        "низкий остаток",
        "заканчиваются",
        "ошибки выгрузки",
        "marketplace errors",
        "store_",
        "autostop app",
    )
    store_subject_terms = (
        "заказ",
        "заявк",
        "запчаст",
        "детал",
        "каталог",
        "склад",
        "парт",
        "поставщик",
        "приход",
        "отгруз",
        "остат",
        "хранен",
        "avito",
        "авито",
        "drom",
        "дром",
        "marketplace",
    )
    if any(term in lowered for term in store_context_terms) and any(term in lowered for term in store_subject_terms):
        hints["store_management"] = 80
    if _PROJECT_ENGINEERING_RE.search(lowered) and _PROJECT_ENGINEERING_ACTION_RE.search(lowered):
        hints["startup_and_identity"] = 70
    for domain, score in _focused_navigation_hints(lowered).items():
        hints[domain] = max(hints.get(domain, 0), score)
    if any(word in lowered for word in ["масло", "моторное", "жидк", "заправ", " то "]):
        hints["fluids"] = 20
    technical_repair_terms = (
        "грм",
        "метк",
        "фаз",
        "цеп",
        "ремн",
        "распредвал",
        "коленвал",
        "момент затяж",
        "доворот",
        "гбц",
        "регламент то",
        "регламент технического",
        "интервал то",
        "интервал обслуж",
        "устройство агрегат",
        "как устроен",
    )
    if (
        any(word in lowered for word in ["диагност", "ошиб", "dtc", "скан"])
        or any(term in lowered for term in technical_repair_terms)
        or bool(re.search(r"\bp[0-3]\d{4}\b", lowered))
    ):
        hints["automotive_repair"] = 44
    if any(word in lowered for word in ["вин", "vin", "oem", "каталог", "кузов"]):
        hints["vehicle_identity_and_oem"] = 10
    crm_vin_terms = [
        "crm",
        "карточк",
        "заказ-наряд",
        "заказ наряд",
        "writeback",
        "запиши",
        "записать",
        "внеси",
        "внести",
    ]
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
        and any(word in lowered for word in crm_vin_terms)
    ):
        hints["crm_vin_oem_parts_lookup"] = max(hints.get("crm_vin_oem_parts_lookup", 0), 34)
        hints["parts_sourcing"] = max(hints.get("parts_sourcing", 0), 12)
    if not any(word in lowered for word in identifier_terms) and any(
        _term_count(lowered, word)
        for word in ["заказ-наряд", "зн", "материал", "материалы", "заменитель", "цена", "закуп"]
    ):
        hints["parts_sourcing"] = max(hints.get("parts_sourcing", 0), 40)
    internal_store_reference = any(
        phrase in lowered
        for phrase in (
            "у нас в магазине",
            "в нашем магазине",
            "в нашем каталоге",
            "наш каталог",
            "на нашем складе",
        )
    )
    if internal_store_reference:
        hints["store_management"] = max(hints.get("store_management", 0), 80)
    if "store_management" not in hints and any(
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
            "поставщик",
            "рулев",
            "рейк",
        ]
    ):
        hints["parts_sourcing"] = max(hints.get("parts_sourcing", 0), 36)
    if any(word in lowered for word in ["bmw", "бмв", "n63", "n63tu", "f15", "e15", "g05"]):
        hints["automotive_repair"] = max(hints.get("automotive_repair", 0), 60)
    if any(word in lowered for word in ["приберись", "board_cleanup_autopilot"]):
        hints["board_cleanup_autopilot"] = max(hints.get("board_cleanup_autopilot", 0), 30)
    elif "cleanup" in lowered and any(term in lowered for term in ("crm", "board", "карточк", "клиент", "автомобил")):
        hints["board_cleanup_autopilot"] = max(hints.get("board_cleanup_autopilot", 0), 30)
    if any(
        term in lowered
        for term in [
            "таймер более двух суток",
            "таймеры более двух суток",
            "не менее двух суток",
            "bulk_set_deadline_if_below",
            "timer floor",
        ]
    ) and not (_PROJECT_ENGINEERING_RE.search(lowered) and _PROJECT_ENGINEERING_ACTION_RE.search(lowered)):
        hints["service_management"] = max(hints.get("service_management", 0), 60)
    return hints


def _focused_navigation_hints(lowered: str) -> dict[str, int]:
    hints = {
        domain: score
        for domain, score, terms in (
            ("startup_and_identity", 70, ("подготовь менеджера", "manager startup", "менеджера к работе")),
            ("automotive_repair", 70, ("dsg", "dq200", "dq250", "мехатроник", "odis", "svm")),
            ("automotive_repair", 78, ("kombi", "приборк", "coding", "кодирован", "a2l", "odx", "dcm")),
        )
        if any(term in lowered for term in terms)
    }
    if any(term in lowered for term in ("сцеплен", "clutch", "коробк", "gearbox", "трансмисс")):
        hints["automotive_repair"] = 70
    if any(term in lowered for term in ("счет", "счёт", "invoice", "акт", "коммерческ")) and any(
        term in lowered for term in ("созд", "сформир", "выстав", "шаблон", "pdf")
    ):
        hints["business_documents"] = 75
    if any(term in lowered for term in ("реквизит", "огрнип", "карточка предприятия")):
        hints["business_identity"] = 75
    return hints


def _score_route_card(
    item: dict[str, Any],
    tokens: list[str],
    query: str,
    *,
    domain_hints: dict[str, int],
) -> tuple[int, list[str]]:
    haystack = str(item.get("search_text") or "").lower()
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
        if _term_count(domain, token):
            token_score += 8
        if _term_count(aliases, token):
            token_score += 7
        if _term_count(keywords, token):
            token_score += 5
        if _term_count(title, token):
            token_score += 4
        if _term_count(source_paths, token):
            token_score += 3
        occurrences = _term_count(haystack, token)
        if occurrences:
            token_score += min(occurrences, 4)
        if token_score:
            score += token_score
            matching_terms.append(token)

    return score, list(dict.fromkeys(matching_terms))[:12]


def _confidence(score: int) -> float:
    if score <= 0:
        return 0.0
    return round(min(0.95, score / 35), 2)


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
    return score
