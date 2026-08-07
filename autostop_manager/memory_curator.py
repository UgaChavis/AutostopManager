from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .storage import ManagerMemoryStore, _now


_SENSITIVE_MEMORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)),
    ("phone", re.compile(r"(?<!\d)(?:\+7|8)[\s()\-]*\d(?:[\s()\-]*\d){9}(?!\d)")),
    ("vin", re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b", re.IGNORECASE)),
    (
        "vehicle_identifier_label",
        re.compile(r"\b(?:vin|frame|chassis|кузов|шасси)\s*(?:number|номер)?\s*[:=]?\s*[A-Z0-9-]{6,}", re.IGNORECASE),
    ),
    (
        "license_plate",
        re.compile(r"\b[АВЕКМНОРСТУХABEKMHOPCTYX]\d{3}[АВЕКМНОРСТУХABEKMHOPCTYX]{2}\d{2,3}\b", re.IGNORECASE),
    ),
    ("crm_entity_ref", re.compile(r"\b(?:C|CL)-[0-9A-F]{8}\b", re.IGNORECASE)),
    (
        "entity_uuid_label",
        re.compile(
            r"\b(?:card|client|repair[ _-]?order|cashbox)[ _-]?(?:id)?\s*[:=]?\s*"
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        ),
    ),
    ("api_key", re.compile(r"\bsk-[A-Z0-9_-]{8,}\b", re.IGNORECASE)),
    ("private_key", re.compile(r"-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----", re.IGNORECASE)),
    (
        "secret_assignment",
        re.compile(r"\b(?:api[_ -]?key|token|password|secret)\s*[:=]\s*\S+", re.IGNORECASE),
    ),
)


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
    sensitive_candidates: list[dict[str, Any]] = []
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
            detectors = _sensitive_memory_detectors(row)
            if detectors:
                candidate = _compact_memory(row)
                candidate["detectors"] = detectors
                sensitive_candidates.append(candidate)

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
    if sensitive_candidates:
        warnings.append("sensitive memory candidates found; review without exporting content")
    return {
        "ok": True,
        "duplicates": duplicates,
        "expired": expired,
        "superseded": superseded,
        "sensitive_candidates": sensitive_candidates,
        "privacy": {
            "content_preview_included": False,
            "raw_private_data_redacted": True,
            "sensitive_candidate_content_redacted": True,
            "sensitive_candidate_count": len(sensitive_candidates),
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
        "sensitive_candidates": audit["sensitive_candidates"],
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


def _sensitive_memory_detectors(row: dict[str, Any]) -> list[str]:
    text = _memory_text(row)
    return [name for name, pattern in _SENSITIVE_MEMORY_PATTERNS if pattern.search(text)]


def _compact_memory(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": row.get("kind"),
        "id": int(row["id"]),
        "expires_at": row.get("expires_at"),
        "supersedes_id": row.get("supersedes_id"),
        "tag_count": len(row.get("tags", []) or []),
        "content_included": False,
    }
