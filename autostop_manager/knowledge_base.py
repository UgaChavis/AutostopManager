from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .storage import ManagerMemoryStore, _json_list, _now


KNOWLEDGE_MAP_PATH = PROJECT_ROOT / "docs" / "agent" / "knowledge_map.json"
MAX_SECTION_CHARS = 12000
MAX_PREVIEW_CHARS = 420


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
    required_context: list[str]
    search_text: str


def sync_knowledge_base(store: ManagerMemoryStore | None = None) -> dict[str, Any]:
    memory = store or ManagerMemoryStore()
    memory.initialize()
    if not KNOWLEDGE_MAP_PATH.exists():
        return {"ok": False, "error": "knowledge_map.json not found", "documents_indexed": 0, "sections_indexed": 0}

    payload = _load_knowledge_map()
    domains: dict[str, Any] = payload.get("domains", {})
    now = _now()
    missing: list[str] = []
    documents_indexed = 0
    sections_indexed = 0
    route_cards_indexed = 0

    with memory.connect() as conn:
        conn.execute("DELETE FROM knowledge_sections")
        conn.execute("DELETE FROM knowledge_documents")
        conn.execute("DELETE FROM knowledge_route_cards")

        for domain, route in domains.items():
            use_when = [str(item) for item in route.get("use_when", [])]
            primary_files = [str(item) for item in route.get("primary_files", [])]
            skill_path = str(route.get("skill_path") or "")
            route_card = _build_route_card(domain, route)
            _insert_route_card(conn, route_card, indexed_at=now)
            route_cards_indexed += 1
            route_path = f"knowledge_map:{domain}"
            route_content = "\n".join(
                [
                    f"Domain: {domain}",
                    "Use when:",
                    *[f"- {item}" for item in use_when],
                    "Primary files:",
                    *[f"- {item}" for item in primary_files],
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

            for raw_path in primary_files:
                resolved = _resolve_path(raw_path)
                if not resolved.exists() or not resolved.is_file():
                    missing.append(raw_path)
                    continue
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
                    indexed_at=now,
                )
                documents_indexed += 1
                sections_indexed += _insert_sections(
                    conn,
                    document_id=document_id,
                    domain=domain,
                    path=raw_path,
                    sections=sections,
                    indexed_at=now,
                )

    return {
        "ok": True,
        "map_path": str(KNOWLEDGE_MAP_PATH),
        "route_cards_indexed": route_cards_indexed,
        "documents_indexed": documents_indexed,
        "sections_indexed": sections_indexed,
        "domains": sorted(domains.keys()),
        "missing_files": missing,
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
    domain_hints = _domain_hints(query)
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
                required_context_json,
                search_text,
                indexed_at
            FROM knowledge_route_cards
            """
        ).fetchall()

    routes: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        score, matching_terms = _score_route_card(item, tokens, query, domain_hints=domain_hints)
        if tokens and score <= 0:
            continue
        source_of_truth = json.loads(item["source_of_truth_json"] or "[]")
        primary_files = json.loads(item["primary_files_json"] or "[]")
        route = {
            "domain": item["domain"],
            "title": item["title"],
            "score": score,
            "confidence": _confidence(score),
            "matching_terms": matching_terms,
            "open_first": (source_of_truth or primary_files or [""])[0],
            "source_of_truth": source_of_truth,
            "primary_files": primary_files,
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
        "routes": routes,
        "next_action": "open_source_of_truth" if has_knowledge else "route_external_sources",
        "needs_broad_search": not has_knowledge,
        "probed_at": _now(),
    }


def audit_knowledge_base(store: ManagerMemoryStore | None = None) -> dict[str, Any]:
    memory = store or ManagerMemoryStore()
    memory.initialize()
    if not KNOWLEDGE_MAP_PATH.exists():
        return {"ok": False, "error": "knowledge_map.json not found", "checked_at": _now()}
    if _route_card_count(memory) == 0 or _document_count(memory) == 0:
        sync_knowledge_base(memory)

    payload = _load_knowledge_map()
    domains: dict[str, Any] = payload.get("domains", {})
    missing_files: list[str] = []
    domains_without_source_of_truth: list[str] = []
    domains_without_aliases: list[str] = []
    checked_paths: set[str] = set()

    for domain, route in domains.items():
        if not route.get("source_of_truth_files") and not route.get("primary_files"):
            domains_without_source_of_truth.append(domain)
        if not route.get("aliases"):
            domains_without_aliases.append(domain)
        for raw_path in _unique_strings(
            [
                *[str(item) for item in route.get("source_of_truth_files", [])],
                *[str(item) for item in route.get("primary_files", [])],
            ]
        ):
            if raw_path in checked_paths:
                continue
            checked_paths.add(raw_path)
            resolved = _resolve_path(raw_path)
            if not resolved.exists() or not resolved.is_file():
                missing_files.append(raw_path)

    with memory.connect() as conn:
        route_cards_indexed = int(conn.execute("SELECT COUNT(*) AS count FROM knowledge_route_cards").fetchone()["count"] or 0)
        documents_indexed = int(conn.execute("SELECT COUNT(*) AS count FROM knowledge_documents").fetchone()["count"] or 0)
        sections_indexed = int(conn.execute("SELECT COUNT(*) AS count FROM knowledge_sections").fetchone()["count"] or 0)

    warnings: list[str] = []
    if route_cards_indexed != len(domains):
        warnings.append("route card count does not match knowledge_map domain count")
    if domains_without_source_of_truth:
        warnings.append("some domains do not declare source_of_truth_files")
    if domains_without_aliases:
        warnings.append("some domains do not declare aliases")
    if missing_files:
        warnings.append("some mapped files are missing")

    ok = not missing_files and route_cards_indexed == len(domains) and documents_indexed > 0 and sections_indexed > 0
    return {
        "ok": ok,
        "map_path": str(KNOWLEDGE_MAP_PATH),
        "domain_count": len(domains),
        "route_cards_indexed": route_cards_indexed,
        "documents_indexed": documents_indexed,
        "sections_indexed": sections_indexed,
        "missing_files": missing_files,
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
    if _document_count(memory) == 0:
        sync_knowledge_base(memory)

    tokens = _tokens(query)
    domain_hints = _domain_hints(query)
    params: list[Any] = []
    where = ""
    if domain:
        where = "WHERE s.domain = ?"
        params.append(domain)

    with memory.connect() as conn:
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
            FROM knowledge_sections s
            JOIN knowledge_documents d ON d.id = s.document_id
            {where}
            """,
            params,
        ).fetchall()

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

    ranked.sort(key=lambda value: (value["score"], value["document_type"] == "domain_route"), reverse=True)
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


def _route_card_count(memory: ManagerMemoryStore) -> int:
    with memory.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM knowledge_route_cards").fetchone()
    return int(row["count"] or 0)


def _load_knowledge_map() -> dict[str, Any]:
    return json.loads(KNOWLEDGE_MAP_PATH.read_text(encoding="utf-8-sig"))


def _build_route_card(domain: str, route: dict[str, Any]) -> _RouteCard:
    use_when = _unique_strings([str(item) for item in route.get("use_when", [])])
    primary_files = _unique_strings([str(item) for item in route.get("primary_files", [])])
    source_of_truth = _unique_strings([str(item) for item in route.get("source_of_truth_files", [])]) or primary_files[:3]
    aliases = _unique_strings(
        [
            domain,
            domain.replace("_", " "),
            *[str(item) for item in route.get("aliases", [])],
        ]
    )
    keywords = _unique_strings([str(item) for item in route.get("keywords", [])])
    questions = _unique_strings(
        [
            *[str(item) for item in route.get("questions", [])],
            *[str(item) for item in route.get("questions_it_answers", [])],
        ]
    )
    required_context = _unique_strings([str(item) for item in route.get("required_context", [])])
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


def _resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


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


def _parse_sections(content: str, *, document_type: str) -> list[_Section]:
    if document_type == "markdown":
        return _parse_markdown_sections(content)
    if document_type == "json":
        return _parse_json_sections(content)
    if document_type == "jsonl":
        return _parse_jsonl_sections(content)
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


def _jsonl_heading(payload: dict[str, Any], index: int) -> str:
    preferred_keys = [
        "code",
        "source_id",
        "engine",
        "symptom_ru",
        "system",
        "abbreviation",
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
                required_context_json,
                search_text,
                content_hash,
                indexed_at
            )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        conn.execute(
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
    }
    tokens: list[str] = []
    for token in re.findall(r"[\w\-]+", query.lower(), flags=re.UNICODE):
        if len(token) < 2:
            continue
        tokens.append(token)
        tokens.extend(aliases.get(token, []))
    return list(dict.fromkeys(tokens))


def _domain_hints(query: str) -> dict[str, int]:
    lowered = query.lower()
    hints: dict[str, int] = {}
    if any(word in lowered for word in ["масло", "моторное", "жидк", "заправ", " то "]):
        hints["fluids"] = 20
    if any(word in lowered for word in ["диагност", "ошиб", "dtc", "скан"]):
        hints["automotive_repair"] = 10
    if any(word in lowered for word in ["вин", "vin", "oem", "каталог", "кузов"]):
        hints["vehicle_identity_and_oem"] = 10
    if any(word in lowered for word in ["bmw", "бмв", "n63", "f15", "e15", "x5"]):
        hints["bmw_repair"] = max(hints.get("bmw_repair", 0), 10)
    if any(word in lowered for word in ["f15", "e15", "n63", "x5"]):
        hints["bmw_f15_n63"] = max(hints.get("bmw_f15_n63", 0), 18)
    if any(word in lowered for word in ["toyota", "тойота", "yaris gr", "gr yaris", "ярис", "gxpa16", "g16e"]):
        hints["toyota_gr_yaris"] = max(hints.get("toyota_gr_yaris", 0), 18)
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
