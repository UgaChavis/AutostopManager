from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .storage import ManagerMemoryStore, _now


def audit_memory(store: ManagerMemoryStore | None = None) -> dict[str, Any]:
    memory = store or ManagerMemoryStore()
    memory.initialize()
    now = _now()
    with memory.connect() as conn:
        rows_by_kind = {
            "note": [
                memory._row_to_dict(row)
                for row in conn.execute(
                    "SELECT *, 'note' AS kind FROM notes WHERE archived_at IS NULL ORDER BY id ASC"
                ).fetchall()
            ],
            "fact": [
                memory._row_to_dict(row)
                for row in conn.execute(
                    "SELECT *, 'fact' AS kind FROM facts WHERE archived_at IS NULL ORDER BY id ASC"
                ).fetchall()
            ],
        }

    duplicates: list[dict[str, Any]] = []
    expired: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
    for kind, rows in rows_by_kind.items():
        by_normalized: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_id = {int(row["id"]): row for row in rows}
        for row in rows:
            normalized = _normalize_memory_text(row)
            if normalized:
                by_normalized[normalized].append(row)
            expires_at = row.get("expires_at")
            if expires_at and str(expires_at) <= now:
                expired.append(_compact_memory(row))

        for group in by_normalized.values():
            if len(group) > 1:
                duplicates.append(
                    {
                        "kind": kind,
                        "ids": [int(item["id"]) for item in group],
                        "count": len(group),
                        "content_included": False,
                    }
                )

        for replacement in rows:
            supersedes_id = replacement.get("supersedes_id")
            if not supersedes_id:
                continue
            old = by_id.get(int(supersedes_id))
            if old:
                item = _compact_memory(old)
                item["superseded_by"] = int(replacement["id"])
                superseded.append(item)

    warnings: list[str] = []
    if duplicates:
        warnings.append("duplicate memories found")
    if expired:
        warnings.append("expired memories found")
    if superseded:
        warnings.append("superseded memories found")
    return {
        "ok": True,
        "duplicates": duplicates,
        "expired": expired,
        "superseded": superseded,
        "privacy": {
            "content_preview_included": False,
            "raw_private_data_redacted": True,
        },
        "warnings": warnings,
        "checked_at": now,
    }


def curate_memory(store: ManagerMemoryStore | None = None, *, apply: bool = False) -> dict[str, Any]:
    memory = store or ManagerMemoryStore()
    memory.initialize()
    audit = audit_memory(memory)
    archived_duplicates: list[int] = []
    if apply:
        archived_at = _now()
        with memory.connect() as conn:
            for duplicate in audit["duplicates"]:
                kind = duplicate["kind"]
                table = "notes" if kind == "note" else "facts"
                for memory_id in duplicate["ids"][1:]:
                    conn.execute(
                        f"UPDATE {table} SET archived_at = ?, updated_at = ? WHERE id = ?",
                        (archived_at, archived_at, memory_id),
                    )
                    archived_duplicates.append(int(memory_id))
    return {
        "ok": True,
        "apply": apply,
        "archived_duplicates": archived_duplicates,
        "duplicate_groups": audit["duplicates"],
        "expired": audit["expired"],
        "superseded": audit["superseded"],
        "checked_at": audit["checked_at"],
    }


def _memory_text(row: dict[str, Any]) -> str:
    return " ".join(
        [
            str(row.get("title") or ""),
            str(row.get("content") or ""),
            str(row.get("category") or ""),
            " ".join(str(tag) for tag in row.get("tags", [])),
        ]
    ).strip()


def _normalize_memory_text(row: dict[str, Any]) -> str:
    return re.sub(r"\s+", " ", _memory_text(row).casefold()).strip()


def _compact_memory(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": row.get("kind"),
        "id": int(row["id"]),
        "expires_at": row.get("expires_at"),
        "supersedes_id": row.get("supersedes_id"),
        "tag_count": len(row.get("tags", []) or []),
        "content_included": False,
    }
