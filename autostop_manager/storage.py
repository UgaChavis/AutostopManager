from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, get_db_path


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json_list(values: list[str] | None) -> str:
    return json.dumps(values or [], ensure_ascii=False)


def _decode_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


@dataclass(frozen=True)
class ManagerMemoryStore:
    db_path: Path | None = None

    @property
    def path(self) -> Path:
        return self.db_path or get_db_path()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

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
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
                    created_at TEXT NOT NULL
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

                CREATE INDEX IF NOT EXISTS idx_knowledge_documents_domain
                    ON knowledge_documents(domain);

                CREATE INDEX IF NOT EXISTS idx_knowledge_sections_domain
                    ON knowledge_sections(domain);

                CREATE INDEX IF NOT EXISTS idx_knowledge_sections_search
                    ON knowledge_sections(search_text);

                CREATE INDEX IF NOT EXISTS idx_knowledge_route_cards_search
                    ON knowledge_route_cards(search_text);
                """
            )

    def seed_default_rules(self) -> dict[str, Any]:
        self.initialize()
        rules_path = PROJECT_ROOT / "docs" / "agent" / "manager_rules.json"
        if not rules_path.exists():
            return {"ok": False, "error": "manager_rules.json not found", "inserted": 0}
        payload = json.loads(rules_path.read_text(encoding="utf-8"))
        inserted = 0
        updated = 0
        now = _now()
        with self.connect() as conn:
            for rule in payload.get("rules", []):
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
    ) -> dict[str, Any]:
        self.initialize()
        now = _now()
        table = "facts" if kind == "fact" else "notes"
        with self.connect() as conn:
            if table == "facts":
                cursor = conn.execute(
                    """
                    INSERT INTO facts (content, category, source, tags_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (content, category, source, _json_list(tags), now, now),
                )
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO notes (title, content, category, source, tags_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (title, content, category, source, _json_list(tags), now, now),
                )
        return {"ok": True, "kind": table[:-1], "id": cursor.lastrowid, "created_at": now}

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

    def recall(self, query: str = "", *, limit: int = 20) -> dict[str, Any]:
        self.initialize()
        limit = max(1, min(limit, 100))
        pattern = f"%{query.strip()}%" if query.strip() else "%"
        results: list[dict[str, Any]] = []
        with self.connect() as conn:
            searches = [
                ("note", "notes", "title || ' ' || content || ' ' || category", "updated_at"),
                ("fact", "facts", "content || ' ' || category", "updated_at"),
                ("task", "tasks", "title || ' ' || details || ' ' || status", "updated_at"),
                ("reminder", "reminders", "title || ' ' || details || ' ' || status", "updated_at"),
                ("journal", "journal", "event || ' ' || source", "created_at"),
                ("rule", "manager_rules", "title || ' ' || rule || ' ' || scope", "updated_at"),
            ]
            for kind, table, haystack, order_column in searches:
                rows = conn.execute(
                    f"""
                    SELECT *, ? AS kind FROM {table}
                    WHERE {haystack} LIKE ?
                    ORDER BY {order_column} DESC
                    LIMIT ?
                    """,
                    (kind, pattern, limit),
                ).fetchall()
                results.extend(self._row_to_dict(row) for row in rows)
        results.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
        return {"ok": True, "query": query, "items": results[:limit], "total_returned": min(len(results), limit)}

    def today_context(self, *, limit: int = 20) -> dict[str, Any]:
        self.initialize()
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
                    "SELECT *, 'journal' AS kind FROM journal ORDER BY created_at DESC LIMIT ?",
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
        }

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        if "tags_json" in item:
            item["tags"] = _decode_json(item.pop("tags_json"), [])
        return item
