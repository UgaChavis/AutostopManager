from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, get_db_path


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json_list(values: list[str] | None) -> str:
    return json.dumps(values or [], ensure_ascii=False)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values: list[Any] = [value]
    elif isinstance(value, dict):
        return []
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
    else:
        text = str(value).strip()
        return [text] if text else []
    result: list[str] = []
    for item in values:
        text = str(item).strip()
        if text:
            result.append(text)
    return result


def _decode_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _tokens(value: str) -> list[str]:
    aliases = {
        "вин": ["vin"],
        "кузов": ["chassis", "frame"],
        "кузова": ["chassis", "frame"],
        "оригинальный": ["oem", "catalog"],
        "оригинального": ["oem", "catalog"],
        "каталожный": ["catalog", "part_number"],
        "каталожного": ["catalog", "part_number"],
        "фильтра": ["фильтр", "filter"],
        "фильтр": ["filter"],
        "фильтры": ["фильтр", "filter"],
        "запчасти": ["parts", "procurement"],
        "запчасть": ["parts", "procurement"],
        "детали": ["деталь", "part"],
        "деталь": ["part"],
        "рулевую": ["рулевая", "steering"],
        "рейку": ["рейка", "rack", "steering_rack"],
        "контрактную": ["контрактная", "contract", "used"],
        "красноярске": ["красноярск", "krasnoyarsk"],
    }
    tokens: list[str] = []
    for token in re.findall(r"[\w\-]+", value.casefold(), flags=re.UNICODE):
        tokens.append(token)
        tokens.extend(aliases.get(token, []))
    return list(dict.fromkeys(tokens))


def _matches_filter(value: str | None, expected: str | None) -> bool:
    if not expected:
        return True
    return str(value or "").casefold() == expected.casefold()


def _matches_tags(item_tags: list[str] | None, expected: list[str] | None) -> bool:
    if not expected:
        return True
    normalized = {tag.casefold() for tag in (item_tags or [])}
    return all(tag.casefold() in normalized for tag in expected)


def _clamp01(value: float) -> float:
    return max(0.0, min(float(value), 1.0))


def _add_topic(topics: dict[str, dict[str, Any]], name: str, item: dict[str, Any], examples_limit: int) -> None:
    key = name.strip()
    if not key:
        return
    topic = topics.setdefault(key, {"count": 0, "examples": []})
    topic["count"] += 1
    if len(topic["examples"]) < examples_limit:
        topic["examples"].append(
            {
                "kind": item.get("kind"),
                "id": item.get("id"),
                "title": item.get("title") or item.get("content") or item.get("event") or item.get("rule") or "",
            }
        )


def _sort_topic_map(topics: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return dict(sorted(topics.items(), key=lambda entry: (-int(entry[1]["count"]), entry[0].casefold())))


def _memory_context_queries(task: str) -> list[str]:
    lowered = task.casefold()
    queries: list[str] = []
    if any(term in lowered for term in ["vin", "вин", "oem", "каталож", "оригиналь", "номер кузова"]):
        queries.append("vin-oem-lookup-workflow original catalog numbers VIN OEM catalog")
    if any(term in lowered for term in ["рейк", "контракт", "красноярск", "закуп", "наличие", "дром", "zzap", "ззап"]):
        queries.append("parts_sourcing закупочная цена запчастей Красноярск selected part")
    if any(term in lowered for term in ["база знаний", "базу знаний", "knowledge", "индексац", "аннотац"]):
        queries.append("knowledge-intake-boundary knowledge-annotation-index memory-mcp-sync")
    queries.append(task)
    return list(dict.fromkeys(query for query in queries if query.strip()))


def _suppress_board_cleanup_context(task: str) -> bool:
    lowered = task.casefold()
    automotive_lookup = any(
        term in lowered
        for term in [
            "vin",
            "вин",
            "oem",
            "каталож",
            "оригиналь",
            "номер кузова",
            "фильтр",
            "детал",
            "запчаст",
            "рейк",
            "контракт",
            "аналоги",
        ]
    )
    explicit_cleanup = any(
        term in lowered
        for term in [
            "приберись",
            "уборк",
            "очист",
            "доску",
            "board cleanup",
            "cleanup",
            "card cleanup",
        ]
    )
    return automotive_lookup and not explicit_cleanup


def _suppress_admin_context(task: str) -> bool:
    lowered = task.casefold()
    automotive_lookup = any(
        term in lowered
        for term in [
            "vin",
            "вин",
            "oem",
            "каталож",
            "оригиналь",
            "номер кузова",
            "фильтр",
            "детал",
            "запчаст",
            "рейк",
            "контракт",
            "аналоги",
        ]
    )
    explicit_admin = any(
        term in lowered
        for term in [
            "база знаний",
            "базу знаний",
            "knowledge",
            "индексац",
            "аннотац",
            "github",
            "публикац",
            "коммит",
            "репозитор",
        ]
    )
    return automotive_lookup and not explicit_admin


def _suppress_style_context(task: str) -> bool:
    lowered = task.casefold()
    automotive_lookup = any(
        term in lowered
        for term in [
            "vin",
            "вин",
            "oem",
            "каталож",
            "оригиналь",
            "номер кузова",
            "фильтр",
            "детал",
            "запчаст",
            "рейк",
            "контракт",
            "аналоги",
        ]
    )
    explicit_text_work = any(
        term in lowered
        for term in [
            "приберись",
            "описание",
            "оформи",
            "текст",
            "напиши",
            "сообщение",
            "комментарий",
        ]
    )
    return automotive_lookup and not explicit_text_work


def _memory_item_text(item: dict[str, Any]) -> str:
    parts = [
        str(item.get("title") or ""),
        str(item.get("content") or item.get("event") or item.get("rule") or item.get("details") or ""),
        str(item.get("category") or item.get("applies_to") or item.get("scope") or ""),
        str(item.get("source") or ""),
        " ".join(str(tag) for tag in item.get("tags", [])),
    ]
    return " ".join(parts).casefold()


def _is_context_noise(
    item: dict[str, Any],
    *,
    task_text: str,
    suppress_board_cleanup: bool,
    suppress_admin_context: bool,
    suppress_style_context: bool,
) -> bool:
    text = _memory_item_text(item)
    category = str(item.get("category") or item.get("applies_to") or item.get("scope") or "").casefold()
    title = str(item.get("title") or "").casefold()
    if suppress_board_cleanup:
        if category in {"board_cleanup", "board_cleanup_autopilot"}:
            return True
        if title.startswith("board-cleanup"):
            return True
        if any(marker in text for marker in ["board cleanup", "board_cleanup", "приберись"]):
            return True
    if suppress_style_context and category in {"crm_style", "style"}:
        return True
    if suppress_admin_context and title.startswith(("knowledge-", "github-", "documentation-", "memory-")):
        return True
    if suppress_admin_context:
        vehicle_families = [
            (["toyota gr yaris", "yaris gr", "gxpa16", "g16e-gts"], ["toyota", "yaris", "gxpa16", "g16e"]),
            (["bmw f15", "n63"], ["bmw", "f15", "n63", "x5"]),
        ]
        for item_markers, task_markers in vehicle_families:
            if any(marker in text for marker in item_markers) and not any(marker in task_text for marker in task_markers):
                return True
    return False


def _unique_memory_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for item in items:
        key = (str(item.get("kind") or ""), int(item.get("id") or 0))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _score_memory_item(item: dict[str, Any], query: str, tokens: list[str]) -> float:
    title = str(item.get("title") or "")
    content = str(item.get("content") or item.get("event") or item.get("rule") or item.get("details") or "")
    category = str(item.get("category") or item.get("scope") or "")
    source = str(item.get("source") or "")
    status = str(item.get("status") or "")
    tags = " ".join(str(tag) for tag in item.get("tags", []))
    haystack = " ".join([title, content, category, source, status, tags]).casefold()
    query_lower = query.casefold()
    score = float(item.get("fts_score") or 0)
    if query_lower and query_lower in haystack:
        score += 20
    for token in tokens:
        token_score = 0.0
        if token in title.casefold():
            token_score += 8
        if token in tags.casefold():
            token_score += 10
        if token in content.casefold():
            token_score += 4
        if token in category.casefold() or token in source.casefold() or token in status.casefold():
            token_score += 2
        if token_score:
            score += token_score
    if item.get("kind") in {"note", "fact"}:
        score += float(item.get("importance") or 0.5) * 8
    if item.get("kind") == "fact":
        score += float(item.get("confidence") or 0.0) * 3
    if item.get("kind") == "rule":
        priority = int(item.get("priority") or 100)
        score += max(0, 30 - priority) / 2
    return score


@dataclass(frozen=True)
class ManagerMemoryStore:
    db_path: Path | None = None

    @property
    def path(self) -> Path:
        return self.db_path or get_db_path()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general',
                    source TEXT NOT NULL DEFAULT 'codex',
                    importance REAL NOT NULL DEFAULT 0.5,
                    expires_at TEXT,
                    supersedes_id INTEGER,
                    sensitivity TEXT NOT NULL DEFAULT 'normal',
                    last_used_at TEXT,
                    archived_at TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general',
                    source TEXT NOT NULL DEFAULT 'codex',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    importance REAL NOT NULL DEFAULT 0.5,
                    expires_at TEXT,
                    supersedes_id INTEGER,
                    sensitivity TEXT NOT NULL DEFAULT 'normal',
                    last_used_at TEXT,
                    archived_at TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS lessons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    applies_to TEXT NOT NULL DEFAULT 'general',
                    signal TEXT NOT NULL DEFAULT 'manager_observation',
                    recommendation TEXT NOT NULL DEFAULT '',
                    avoid TEXT NOT NULL DEFAULT '',
                    importance REAL NOT NULL DEFAULT 0.5,
                    confidence REAL NOT NULL DEFAULT 0.7,
                    source TEXT NOT NULL DEFAULT 'codex',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT,
                    archived_at TEXT
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    due_at TEXT,
                    source TEXT NOT NULL DEFAULT 'codex',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    remind_at TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    source TEXT NOT NULL DEFAULT 'codex',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'codex',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    archived_at TEXT
                );

                CREATE TABLE IF NOT EXISTS manager_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    rule TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'general',
                    priority INTEGER NOT NULL DEFAULT 100,
                    source TEXT NOT NULL DEFAULT 'system',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS knowledge_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT NOT NULL,
                    path TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    document_type TEXT NOT NULL DEFAULT 'file',
                    use_when_json TEXT NOT NULL DEFAULT '[]',
                    content_hash TEXT NOT NULL DEFAULT '',
                    indexed_at TEXT NOT NULL,
                    UNIQUE(domain, path)
                );

                CREATE TABLE IF NOT EXISTS knowledge_route_cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL DEFAULT '',
                    use_when_json TEXT NOT NULL DEFAULT '[]',
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    questions_json TEXT NOT NULL DEFAULT '[]',
                    source_of_truth_json TEXT NOT NULL DEFAULT '[]',
                    primary_files_json TEXT NOT NULL DEFAULT '[]',
                    reference_files_json TEXT NOT NULL DEFAULT '[]',
                    required_context_json TEXT NOT NULL DEFAULT '[]',
                    search_text TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL DEFAULT '',
                    indexed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS knowledge_sections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL,
                    domain TEXT NOT NULL,
                    path TEXT NOT NULL,
                    heading TEXT NOT NULL DEFAULT '',
                    level INTEGER NOT NULL DEFAULT 0,
                    ordinal INTEGER NOT NULL DEFAULT 0,
                    content TEXT NOT NULL DEFAULT '',
                    preview TEXT NOT NULL DEFAULT '',
                    search_text TEXT NOT NULL DEFAULT '',
                    indexed_at TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES knowledge_documents(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS knowledge_annotations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    annotation_id TEXT NOT NULL UNIQUE,
                    domain TEXT NOT NULL,
                    path TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    use_when_json TEXT NOT NULL DEFAULT '[]',
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    questions_json TEXT NOT NULL DEFAULT '[]',
                    source_type TEXT NOT NULL DEFAULT '',
                    trust_level TEXT NOT NULL DEFAULT '',
                    refresh_cadence TEXT NOT NULL DEFAULT '',
                    safety_flags_json TEXT NOT NULL DEFAULT '[]',
                    related_skills_json TEXT NOT NULL DEFAULT '[]',
                    search_text TEXT NOT NULL DEFAULT '',
                    indexed_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_knowledge_documents_domain
                    ON knowledge_documents(domain);

                CREATE INDEX IF NOT EXISTS idx_knowledge_sections_domain
                    ON knowledge_sections(domain);

                CREATE INDEX IF NOT EXISTS idx_knowledge_sections_search
                    ON knowledge_sections(search_text);

                CREATE INDEX IF NOT EXISTS idx_knowledge_route_cards_search
                    ON knowledge_route_cards(search_text);

                CREATE INDEX IF NOT EXISTS idx_knowledge_annotations_domain
                    ON knowledge_annotations(domain);

                CREATE INDEX IF NOT EXISTS idx_knowledge_annotations_search
                    ON knowledge_annotations(search_text);

                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_sections_fts
                USING fts5(domain, path, heading, search_text);

                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_annotations_fts
                USING fts5(domain, path, title, search_text);

                CREATE TABLE IF NOT EXISTS manager_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent TEXT NOT NULL DEFAULT '',
                    query TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'running',
                    dry_run INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'codex',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    summary TEXT NOT NULL DEFAULT '',
                    verification_json TEXT NOT NULL DEFAULT '{}',
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS manager_run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    target_type TEXT NOT NULL DEFAULT '',
                    target_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES manager_runs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS memory_review_items (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    source_ref TEXT NOT NULL DEFAULT '',
                    proposal_json TEXT NOT NULL DEFAULT '{}',
                    reason TEXT NOT NULL DEFAULT '',
                    risk TEXT NOT NULL DEFAULT 'low',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_manager_runs_status
                    ON manager_runs(status, started_at);

                CREATE INDEX IF NOT EXISTS idx_manager_run_events_run_id
                    ON manager_run_events(run_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_memory_review_items_status
                    ON memory_review_items(status, created_at);
                """
            )
            self._ensure_columns(conn)
            self._ensure_memory_fts(conn)

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        desired = {
            "notes": {
                "importance": "REAL NOT NULL DEFAULT 0.5",
                "expires_at": "TEXT",
                "supersedes_id": "INTEGER",
                "sensitivity": "TEXT NOT NULL DEFAULT 'normal'",
                "last_used_at": "TEXT",
                "archived_at": "TEXT",
            },
            "facts": {
                "importance": "REAL NOT NULL DEFAULT 0.5",
                "expires_at": "TEXT",
                "supersedes_id": "INTEGER",
                "sensitivity": "TEXT NOT NULL DEFAULT 'normal'",
                "last_used_at": "TEXT",
                "archived_at": "TEXT",
            },
            "lessons": {
                "last_used_at": "TEXT",
                "archived_at": "TEXT",
            },
            "journal": {
                "archived_at": "TEXT",
            },
            "knowledge_route_cards": {
                "reference_files_json": "TEXT NOT NULL DEFAULT '[]'",
            },
        }
        for table, columns in desired.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _ensure_memory_fts(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts
                USING fts5(title, content, category, source, tags)
                """
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
                USING fts5(content, category, source, tags)
                """
            )
        except sqlite3.OperationalError:
            return
        conn.execute(
            """
            INSERT OR REPLACE INTO notes_fts(rowid, title, content, category, source, tags)
            SELECT id, title, content, category, source, tags_json
            FROM notes
            WHERE archived_at IS NULL
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO facts_fts(rowid, content, category, source, tags)
            SELECT id, content, category, source, tags_json
            FROM facts
            WHERE archived_at IS NULL
            """
        )

    def seed_default_rules(self) -> dict[str, Any]:
        self.initialize()
        rules_path = PROJECT_ROOT / "docs" / "agent" / "manager_rules.json"
        if not rules_path.exists():
            return {"ok": False, "error": "manager_rules.json not found", "inserted": 0}
        try:
            payload = json.loads(rules_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "error": "manager_rules.json invalid_json",
                "error_detail": str(exc),
                "inserted": 0,
                "updated": 0,
            }
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "error": "manager_rules.json invalid_structure",
                "error_detail": type(payload).__name__,
                "inserted": 0,
                "updated": 0,
            }
        rules = payload.get("rules")
        if not isinstance(rules, list):
            return {
                "ok": False,
                "error": "manager_rules.json invalid_rules",
                "error_detail": type(rules).__name__,
                "inserted": 0,
                "updated": 0,
            }
        inserted = 0
        updated = 0
        now = _now()
        with self.connect() as conn:
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                title = str(rule.get("id") or "").strip()
                text = str(rule.get("rule") or "").strip()
                if not title or not text:
                    continue
                scope = str(rule.get("scope") or "general")
                priority = int(rule.get("priority") or 100)
                source = "docs/agent/manager_rules.json"
                exists = conn.execute(
                    "SELECT id, rule, scope, priority, source FROM manager_rules WHERE title = ? LIMIT 1",
                    (title,),
                ).fetchone()
                if exists:
                    if (
                        exists["rule"] != text
                        or exists["scope"] != scope
                        or int(exists["priority"]) != priority
                        or exists["source"] != source
                    ):
                        conn.execute(
                            """
                            UPDATE manager_rules
                            SET rule = ?, scope = ?, priority = ?, source = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (text, scope, priority, source, now, exists["id"]),
                        )
                        updated += 1
                    continue
                conn.execute(
                    """
                    INSERT INTO manager_rules (title, rule, scope, priority, source, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        title,
                        text,
                        scope,
                        priority,
                        source,
                        now,
                        now,
                    ),
                )
                inserted += 1
        return {"ok": True, "inserted": inserted, "updated": updated}

    def remember(
        self,
        content: str,
        *,
        kind: str = "note",
        title: str = "",
        category: str = "general",
        source: str = "codex",
        tags: list[str] | None = None,
        importance: float = 0.5,
        confidence: float = 1.0,
        expires_at: str | None = None,
        supersedes_id: int | None = None,
        sensitivity: str = "normal",
    ) -> dict[str, Any]:
        self.initialize()
        now = _now()
        table = "facts" if kind == "fact" else "notes"
        importance = _clamp01(importance)
        confidence = _clamp01(confidence)
        row_id = 0
        with self.connect() as conn:
            if table == "facts":
                cursor = conn.execute(
                    """
                    INSERT INTO facts
                        (content, category, source, confidence, importance, expires_at, supersedes_id, sensitivity, tags_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        content,
                        category,
                        source,
                        float(confidence),
                        float(importance),
                        expires_at,
                        supersedes_id,
                        sensitivity,
                        _json_list(tags),
                        now,
                        now,
                    ),
                )
                row_id = int(cursor.lastrowid)
                self._upsert_memory_fts(conn, table, row_id)
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO notes
                        (title, content, category, source, importance, expires_at, supersedes_id, sensitivity, tags_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        title,
                        content,
                        category,
                        source,
                        float(importance),
                        expires_at,
                        supersedes_id,
                        sensitivity,
                        _json_list(tags),
                        now,
                        now,
                    ),
                )
                row_id = int(cursor.lastrowid)
                self._upsert_memory_fts(conn, table, row_id)
        result = {"ok": True, "kind": table[:-1], "id": row_id, "created_at": now}
        if table == "facts":
            result["confidence"] = float(confidence)
        return result

    def learn_from_feedback(
        self,
        content: str,
        *,
        title: str = "",
        applies_to: str = "general",
        signal: str = "manager_observation",
        recommendation: str = "",
        avoid: str = "",
        importance: float = 0.5,
        confidence: float = 0.7,
        source: str = "codex",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        now = _now()
        importance = _clamp01(importance)
        confidence = _clamp01(confidence)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO lessons (
                    title, content, applies_to, signal, recommendation, avoid,
                    importance, confidence, source, tags_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title,
                    content,
                    applies_to,
                    signal,
                    recommendation,
                    avoid,
                    importance,
                    confidence,
                    source,
                    _json_list(tags),
                    now,
                    now,
                ),
            )
        return {
            "ok": True,
            "kind": "lesson",
            "id": cursor.lastrowid,
            "created_at": now,
            "applies_to": applies_to,
            "signal": signal,
            "importance": importance,
            "confidence": confidence,
        }

    def add_task(
        self,
        title: str,
        *,
        details: str = "",
        due_at: str | None = None,
        source: str = "codex",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        now = _now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tasks (title, details, due_at, source, tags_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (title, details, due_at, source, _json_list(tags), now, now),
            )
        return {"ok": True, "id": cursor.lastrowid, "created_at": now}

    def add_reminder(
        self,
        title: str,
        *,
        remind_at: str,
        details: str = "",
        source: str = "codex",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        now = _now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO reminders (title, remind_at, details, source, tags_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (title, remind_at, details, source, _json_list(tags), now, now),
            )
        return {"ok": True, "id": cursor.lastrowid, "created_at": now}

    def journal(self, event: str, *, source: str = "codex", tags: list[str] | None = None) -> dict[str, Any]:
        self.initialize()
        now = _now()
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO journal (event, source, tags_json, created_at) VALUES (?, ?, ?, ?)",
                (event, source, _json_list(tags), now),
            )
        return {"ok": True, "id": cursor.lastrowid, "created_at": now}

    def recall(
        self,
        query: str = "",
        *,
        limit: int = 20,
        kind: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        limit = max(1, min(limit, 100))
        query = query.strip()
        query_tokens = _tokens(query)
        results: list[dict[str, Any]] = []
        with self.connect() as conn:
            for row_kind, table, order_column in [
                ("note", "notes", "updated_at"),
                ("fact", "facts", "updated_at"),
            ]:
                if kind and row_kind != kind:
                    continue
                rows = conn.execute(
                    f"""
                    SELECT *, ? AS kind FROM {table}
                    WHERE archived_at IS NULL
                        AND (expires_at IS NULL OR expires_at > ?)
                        AND id NOT IN (
                            SELECT supersedes_id FROM {table}
                            WHERE supersedes_id IS NOT NULL AND archived_at IS NULL
                        )
                    ORDER BY {order_column} DESC
                    LIMIT ?
                    """,
                    (row_kind, _now(), max(limit * 10, 100)),
                ).fetchall()
                for row in rows:
                    item = self._row_to_dict(row)
                    if not _matches_filter(item.get("category"), category):
                        continue
                    if not _matches_tags(item.get("tags", []), tags):
                        continue
                    score, matched_fields = self._score_memory_item(item, query, query_tokens)
                    if query_tokens and score <= 0:
                        continue
                    score += int(_clamp01(float(item.get("importance") or 0.5)) * 8)
                    if row_kind == "fact":
                        score += int(_clamp01(float(item.get("confidence") or 0.0)) * 3)
                    item["score"] = score
                    item["matched_fields"] = matched_fields
                    results.append(item)

            searches = [
                ("task", "tasks", "updated_at"),
                ("reminder", "reminders", "updated_at"),
                ("journal", "journal", "created_at"),
                ("rule", "manager_rules", "updated_at"),
                ("lesson", "lessons", "updated_at"),
            ]
            for row_kind, table, order_column in searches:
                if kind and row_kind != kind:
                    continue
                row_limit = 1000 if row_kind == "rule" else max(limit * 10, 100)
                where = "WHERE archived_at IS NULL" if row_kind in {"lesson", "journal"} else ""
                rows = conn.execute(
                    f"""
                    SELECT *, ? AS kind FROM {table}
                    {where}
                    ORDER BY {order_column} DESC
                    LIMIT ?
                    """,
                    (row_kind, row_limit),
                ).fetchall()
                for row in rows:
                    item = self._row_to_dict(row)
                    if not _matches_filter(item.get("category"), category):
                        continue
                    if not _matches_tags(item.get("tags", []), tags):
                        continue
                    score, matched_fields = self._score_memory_item(item, query, query_tokens)
                    if query_tokens and score <= 0:
                        continue
                    if row_kind == "rule":
                        score += max(0, 30 - int(item.get("priority") or 100)) // 2
                    if row_kind == "lesson":
                        score += int(_clamp01(float(item.get("importance") or 0)) * 8)
                        score += int(_clamp01(float(item.get("confidence") or 0)) * 3)
                    item["score"] = score
                    item["matched_fields"] = matched_fields
                    results.append(item)
        results.sort(
            key=lambda item: (
                int(item.get("score") or 0),
                item.get("updated_at") or item.get("created_at") or "",
            ),
            reverse=True,
        )
        selected = results[:limit]
        used_at = _now()
        with self.connect() as conn:
            for item in selected:
                item_kind = item.get("kind")
                if item_kind in {"note", "fact"}:
                    table = "notes" if item_kind == "note" else "facts"
                    conn.execute("UPDATE " + table + " SET last_used_at = ? WHERE id = ?", (used_at, item["id"]))
                    item["last_used_at"] = used_at
        return {
            "ok": True,
            "query": query,
            "filters": {"kind": kind, "category": category, "tags": tags or []},
            "items": selected,
            "total_returned": len(selected),
            "total_matches": len(results),
        }

    def recall_lessons(
        self,
        query: str = "",
        *,
        limit: int = 20,
        applies_to: str | None = None,
        signal: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        limit = max(1, min(limit, 100))
        query = query.strip()
        query_tokens = _tokens(query)
        results: list[dict[str, Any]] = []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT *, 'lesson' AS kind FROM lessons
                WHERE archived_at IS NULL
                ORDER BY importance DESC, confidence DESC, updated_at DESC
                LIMIT ?
                """,
                (max(limit * 10, 100),),
            ).fetchall()
            for row in rows:
                item = self._row_to_dict(row)
                if not _matches_filter(item.get("applies_to"), applies_to):
                    continue
                if not _matches_filter(item.get("signal"), signal):
                    continue
                if not _matches_tags(item.get("tags", []), tags):
                    continue
                score, matched_fields = self._score_memory_item(item, query, query_tokens)
                if query_tokens and score <= 0:
                    continue
                item["score"] = score
                item["matched_fields"] = matched_fields
                results.append(item)
        results.sort(
            key=lambda item: (
                int(item.get("score") or 0),
                float(item.get("importance") or 0),
                float(item.get("confidence") or 0),
                item.get("updated_at") or "",
            ),
            reverse=True,
        )
        return {
            "ok": True,
            "query": query,
            "filters": {"applies_to": applies_to, "signal": signal, "tags": tags or []},
            "items": results[:limit],
            "total_returned": min(len(results), limit),
            "total_matches": len(results),
        }

    def memory_map(self) -> dict[str, Any]:
        self.initialize()
        sections = {
            "notes": self._section_summary("notes", "updated_at"),
            "facts": self._section_summary("facts", "updated_at"),
            "lessons": self._section_summary("lessons", "updated_at", where="archived_at IS NULL"),
            "tasks": self._section_summary("tasks", "updated_at", where="status = 'open'"),
            "reminders": self._section_summary("reminders", "updated_at", where="status = 'open'"),
            "journal": self._section_summary("journal", "created_at", where="archived_at IS NULL"),
            "rules": self._section_summary("manager_rules", "updated_at"),
        }
        return {
            "ok": True,
            "generated_at": _now(),
            "sections": sections,
            "recommended_flow": [
                "today_context",
                "memory_context_for",
                "recall_lessons",
                "learn_from_feedback after strong owner/result signals",
                "memory_gaps during memory review",
            ],
        }

    def memory_topics(self, *, examples_limit: int = 3) -> dict[str, Any]:
        self.initialize()
        categories: dict[str, dict[str, Any]] = {}
        tags: dict[str, dict[str, Any]] = {}
        with self.connect() as conn:
            rows: list[dict[str, Any]] = []
            for table, kind in [
                ("notes", "note"),
                ("facts", "fact"),
                ("tasks", "task"),
                ("reminders", "reminder"),
                ("journal", "journal"),
                ("lessons", "lesson"),
                ("manager_rules", "rule"),
            ]:
                order_column = "created_at" if table == "journal" else "updated_at"
                where = "WHERE archived_at IS NULL" if table in {"lessons", "journal"} else ""
                rows.extend(
                    self._row_to_dict(row)
                    for row in conn.execute(
                        f"SELECT *, ? AS kind FROM {table} {where} ORDER BY {order_column} DESC LIMIT 200",
                        (kind,),
                    ).fetchall()
                )
        for item in rows:
            category = str(item.get("category") or item.get("applies_to") or item.get("scope") or "").strip()
            if category:
                _add_topic(categories, category, item, examples_limit)
            for tag in item.get("tags") or []:
                _add_topic(tags, str(tag), item, examples_limit)
        return {
            "ok": True,
            "generated_at": _now(),
            "categories": _sort_topic_map(categories),
            "tags": _sort_topic_map(tags),
        }

    def memory_context_for(self, task: str, *, limit: int = 5) -> dict[str, Any]:
        self.initialize()
        task = task.strip()
        limit = max(1, min(limit, 20))
        context_queries = _memory_context_queries(task)
        suppress_board_cleanup = _suppress_board_cleanup_context(task)
        suppress_admin_context = _suppress_admin_context(task)
        suppress_style_context = _suppress_style_context(task)
        task_text = task.casefold()
        lesson_queries = context_queries[:1] if len(context_queries) > 1 else context_queries
        lessons = [
            item
            for item in _unique_memory_items(
                [
                    item
                    for query in lesson_queries
                    for item in self.recall_lessons(query, limit=limit)["items"]
                ]
            )
            if not _is_context_noise(
                item,
                task_text=task_text,
                suppress_board_cleanup=suppress_board_cleanup,
                suppress_admin_context=suppress_admin_context,
                suppress_style_context=suppress_style_context,
            )
        ][:limit]
        if not lessons and len(context_queries) == 1:
            lessons = [
                item
                for item in self.recall_lessons("", limit=min(limit, 3))["items"]
                if not _is_context_noise(
                    item,
                    task_text=task_text,
                    suppress_board_cleanup=suppress_board_cleanup,
                    suppress_admin_context=suppress_admin_context,
                    suppress_style_context=suppress_style_context,
                )
            ]

        recalled = _unique_memory_items(
            [
                item
                for query in context_queries
                for item in self.recall(query, limit=limit * 3)["items"]
            ]
        )
        recalled = [
            item
            for item in recalled
            if not _is_context_noise(
                item,
                task_text=task_text,
                suppress_board_cleanup=suppress_board_cleanup,
                suppress_admin_context=suppress_admin_context,
                suppress_style_context=suppress_style_context,
            )
        ]
        preferences_or_facts = [
            item
            for item in recalled
            if item.get("kind") in {"fact", "note", "rule"} and item.get("kind") != "lesson"
        ][:limit]
        if not preferences_or_facts:
            preferences_or_facts = self.recall("", limit=limit, kind="fact")["items"]

        return {
            "ok": True,
            "query": task,
            "generated_at": _now(),
            "lessons": lessons[:limit],
            "preferences_or_facts": preferences_or_facts[:limit],
            "source_boundaries": [
                "CRM is source of truth for cards, clients, vehicles, repair orders, payments, and cashboxes.",
                "Manager memory stores style, owner preferences, durable lessons, and operating context only.",
                "Use memory as context for judgment, not as a rigid text template.",
            ],
            "suggested_use": [
                "Read lessons and preferences before writing CRM/email/customer-facing text.",
                "Check live CRM data before making factual statements about board state or money.",
                "After strong praise, criticism, success, or failure, write a concise lesson.",
            ],
        }

    def memory_gaps(self) -> dict[str, Any]:
        self.initialize()
        sections = {
            "notes": self._count_rows("notes"),
            "facts": self._count_rows("facts"),
            "lessons": self._count_rows("lessons", where="archived_at IS NULL"),
            "tasks": self._count_rows("tasks", where="status = 'open'"),
            "reminders": self._count_rows("reminders", where="status = 'open'"),
            "journal": self._count_rows("journal", where="archived_at IS NULL"),
            "rules": self._count_rows("manager_rules"),
        }
        empty_sections = {name: count for name, count in sections.items() if count == 0}
        sparse_sections = {name: count for name, count in sections.items() if 0 < count < 2}
        return {
            "ok": True,
            "generated_at": _now(),
            "empty_sections": empty_sections,
            "sparse_sections": sparse_sections,
            "conflicts": [],
            "review_prompts": [
                "Add lessons after strong owner feedback or clearly successful/failed work.",
                "Keep CRM facts in CRM; store only reusable operating conclusions in memory.",
                "Review sparse topics before relying on memory for style-sensitive work.",
            ],
        }

    def _upsert_memory_fts(self, conn: sqlite3.Connection, table: str, row_id: int) -> None:
        try:
            if table == "notes":
                row = conn.execute("SELECT * FROM notes WHERE id = ? LIMIT 1", (row_id,)).fetchone()
                if row:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO notes_fts(rowid, title, content, category, source, tags)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (row["id"], row["title"], row["content"], row["category"], row["source"], row["tags_json"]),
                    )
            elif table == "facts":
                row = conn.execute("SELECT * FROM facts WHERE id = ? LIMIT 1", (row_id,)).fetchone()
                if row:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO facts_fts(rowid, content, category, source, tags)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (row["id"], row["content"], row["category"], row["source"], row["tags_json"]),
                    )
        except sqlite3.OperationalError:
            return

    def today_context(self, *, limit: int = 20) -> dict[str, Any]:
        self.initialize()
        warnings: list[str] = []
        if self._manager_rule_count() == 0:
            seed_result = self.seed_default_rules()
            if not seed_result.get("ok", True):
                warnings.append(f"manager_rules_seed_failed: {seed_result.get('error', 'unknown')}")
        now = _now()
        limit = max(1, min(limit, 100))
        with self.connect() as conn:
            tasks = [
                self._row_to_dict(row)
                for row in conn.execute(
                    """
                    SELECT *, 'task' AS kind FROM tasks
                    WHERE status = 'open' AND (due_at IS NULL OR due_at <= ?)
                    ORDER BY due_at IS NULL, due_at ASC, created_at DESC
                    LIMIT ?
                    """,
                    (now, limit),
                ).fetchall()
            ]
            reminders = [
                self._row_to_dict(row)
                for row in conn.execute(
                    """
                    SELECT *, 'reminder' AS kind FROM reminders
                    WHERE status = 'open' AND remind_at <= ?
                    ORDER BY remind_at ASC
                    LIMIT ?
                    """,
                    (now, limit),
                ).fetchall()
            ]
            journal_rows = [
                self._row_to_dict(row)
                for row in conn.execute(
                    """
                    SELECT *, 'journal' AS kind FROM journal
                    WHERE archived_at IS NULL
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            ]
            rules = [
                self._row_to_dict(row)
                for row in conn.execute(
                    "SELECT *, 'rule' AS kind FROM manager_rules ORDER BY priority ASC, updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            ]
        return {
            "ok": True,
            "generated_at": now,
            "tasks": tasks,
            "reminders": reminders,
            "recent_journal": journal_rows,
            "manager_rules": rules,
            "crm_read_order": [
                "bootstrap_context",
                "get_board_context",
                "review_board",
                "search_cards",
                "get_card_context",
                "list_repair_orders",
            ],
            "memory_use_order": [
                "today_context",
                "memory_context_for before context-sensitive CRM/Gmail/writing tasks",
                "recall owner/style/rule terms when the request depends on prior preferences",
                "recall_lessons for similar prior successes or failures",
                "probe_knowledge_base for local knowledge routing",
                "learn_from_feedback after strong praise, criticism, success, or failure",
                "manager_journal after important decisions",
            ],
            "warnings": warnings,
        }

    def start_manager_run(
        self,
        *,
        intent: str,
        query: str = "",
        dry_run: bool = False,
        source: str = "codex",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        now = _now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO manager_runs
                    (intent, query, status, dry_run, source, metadata_json, started_at, updated_at)
                VALUES (?, ?, 'running', ?, ?, ?, ?, ?)
                """,
                (intent, query, 1 if dry_run else 0, source, json.dumps(metadata or {}, ensure_ascii=False), now, now),
            )
        return {"ok": True, "id": cursor.lastrowid, "started_at": now, "status": "running"}

    def record_manager_run_event(
        self,
        run_id: int,
        *,
        event_type: str,
        message: str = "",
        target_type: str = "",
        target_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        now = _now()
        with self.connect() as conn:
            run = conn.execute("SELECT id FROM manager_runs WHERE id = ? LIMIT 1", (run_id,)).fetchone()
            if not run:
                return {"ok": False, "error": "manager run not found", "run_id": run_id}
            cursor = conn.execute(
                """
                INSERT INTO manager_run_events
                    (run_id, event_type, message, target_type, target_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    event_type,
                    message,
                    target_type,
                    target_id,
                    json.dumps(payload or {}, ensure_ascii=False),
                    now,
                ),
            )
            conn.execute("UPDATE manager_runs SET updated_at = ? WHERE id = ?", (now, run_id))
        return {"ok": True, "id": cursor.lastrowid, "run_id": run_id, "created_at": now}

    def finish_manager_run(
        self,
        run_id: int,
        *,
        status: str = "completed",
        summary: str = "",
        verification: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        now = _now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE manager_runs
                SET status = ?, summary = ?, verification_json = ?, finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, summary, json.dumps(verification or {}, ensure_ascii=False), now, now, run_id),
            )
        if cursor.rowcount == 0:
            return {"ok": False, "error": "manager run not found", "run_id": run_id}
        return {"ok": True, "id": run_id, "status": status, "finished_at": now}

    def list_manager_runs(self, *, limit: int = 20, include_events: bool = False) -> dict[str, Any]:
        self.initialize()
        limit = max(1, min(limit, 100))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM manager_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            items = [self._row_to_dict(row) for row in rows]
            if include_events and items:
                events_by_run: dict[int, list[dict[str, Any]]] = {int(item["id"]): [] for item in items}
                placeholders = ",".join("?" for _ in events_by_run)
                event_rows = conn.execute(
                    f"""
                    SELECT * FROM manager_run_events
                    WHERE run_id IN ({placeholders})
                    ORDER BY created_at ASC
                    """,
                    list(events_by_run.keys()),
                ).fetchall()
                for row in event_rows:
                    event = self._row_to_dict(row)
                    events_by_run[int(event["run_id"])].append(event)
                for item in items:
                    item["events"] = events_by_run[int(item["id"])]
        return {"ok": True, "items": items, "total_returned": len(items)}

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        if "tags_json" in item:
            item["tags"] = _decode_json(item.pop("tags_json"), [])
        if "metadata_json" in item:
            item["metadata"] = _decode_json(item.pop("metadata_json"), {})
        if "verification_json" in item:
            item["verification"] = _decode_json(item.pop("verification_json"), {})
        if "payload_json" in item:
            item["payload"] = _decode_json(item.pop("payload_json"), {})
        if "dry_run" in item:
            item["dry_run"] = bool(item["dry_run"])
        return item

    def _manager_rule_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM manager_rules").fetchone()
        return int(row["count"] or 0)

    def _count_rows(self, table: str, *, where: str | None = None) -> int:
        query = f"SELECT COUNT(*) AS count FROM {table}"
        if where:
            query += f" WHERE {where}"
        with self.connect() as conn:
            row = conn.execute(query).fetchone()
        return int(row["count"] or 0)

    def _section_summary(self, table: str, order_column: str, *, where: str | None = None) -> dict[str, Any]:
        query = f"SELECT COUNT(*) AS count, MAX({order_column}) AS last_updated FROM {table}"
        if where:
            query += f" WHERE {where}"
        with self.connect() as conn:
            row = conn.execute(query).fetchone()
        return {"count": int(row["count"] or 0), "last_updated": row["last_updated"]}

    def _score_memory_item(self, item: dict[str, Any], query: str, tokens: list[str]) -> tuple[int, list[str]]:
        if not tokens:
            return 0, []
        fields = self._memory_search_fields(item)
        score = 0
        matched_fields: list[str] = []
        normalized_query = query.casefold()
        for field, raw_value in fields.items():
            value = raw_value.casefold()
            if not value:
                continue
            field_score = 0
            if normalized_query and normalized_query in value:
                field_score += 20
            for token in tokens:
                if token in value:
                    field_score += self._memory_field_weight(field)
            if field_score:
                score += field_score
                matched_fields.append(field)
        return score, matched_fields

    def _memory_search_fields(self, item: dict[str, Any]) -> dict[str, str]:
        tags = " ".join(str(tag) for tag in (item.get("tags") or []))
        kind = item.get("kind")
        if kind == "note":
            return {
                "title": str(item.get("title") or ""),
                "content": str(item.get("content") or ""),
                "category": str(item.get("category") or ""),
                "source": str(item.get("source") or ""),
                "tags": tags,
            }
        if kind == "fact":
            return {
                "content": str(item.get("content") or ""),
                "category": str(item.get("category") or ""),
                "source": str(item.get("source") or ""),
                "tags": tags,
            }
        if kind == "lesson":
            return {
                "title": str(item.get("title") or ""),
                "content": str(item.get("content") or ""),
                "applies_to": str(item.get("applies_to") or ""),
                "signal": str(item.get("signal") or ""),
                "recommendation": str(item.get("recommendation") or ""),
                "avoid": str(item.get("avoid") or ""),
                "source": str(item.get("source") or ""),
                "tags": tags,
            }
        if kind in {"task", "reminder"}:
            return {
                "title": str(item.get("title") or ""),
                "details": str(item.get("details") or ""),
                "status": str(item.get("status") or ""),
                "source": str(item.get("source") or ""),
                "tags": tags,
            }
        if kind == "journal":
            return {
                "event": str(item.get("event") or ""),
                "source": str(item.get("source") or ""),
                "tags": tags,
            }
        if kind == "rule":
            return {
                "title": str(item.get("title") or ""),
                "rule": str(item.get("rule") or ""),
                "scope": str(item.get("scope") or ""),
                "source": str(item.get("source") or ""),
            }
        return {key: str(value or "") for key, value in item.items()}

    def _memory_field_weight(self, field: str) -> int:
        return {
            "title": 8,
            "tags": 7,
            "category": 5,
            "rule": 5,
            "content": 4,
            "details": 4,
            "event": 4,
            "recommendation": 6,
            "avoid": 5,
            "applies_to": 5,
            "signal": 3,
            "scope": 3,
            "status": 2,
            "source": 1,
        }.get(field, 1)
