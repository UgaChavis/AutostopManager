from __future__ import annotations

import json
import re
from typing import Any

from .memory_curator import _normalize_memory_text, audit_memory
from .storage import ManagerMemoryStore, _decode_json, _now


ALLOWED_ACTIONS = {"accept", "reject", "archive_duplicate"}
PRIVATE_DATA_PATTERNS = [
    re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"),
    re.compile(r"\b(?:\+?\d[\d\s().-]{8,}\d)\b"),
    re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b", re.IGNORECASE),
    re.compile(r"\b(?:TOKEN|SECRET|PASSWORD|API_KEY|OPENAI_API_KEY|GITHUB_TOKEN)\b", re.IGNORECASE),
]


def build_memory_review(store: ManagerMemoryStore | None = None) -> dict[str, Any]:
    memory = store or ManagerMemoryStore()
    memory.initialize()
    generated = _generate_review_items(memory)
    _sync_review_items(memory, generated)
    items = _list_review_items(memory)
    pending = [item for item in items if item["status"] == "pending"]
    return {
        "ok": True,
        "schema": "MemoryReviewItem",
        "generated_at": _now(),
        "items": items,
        "summary": {
            "generated_count": len(generated),
            "item_count": len(items),
            "pending_count": len(pending),
            "accepted_count": sum(1 for item in items if item["status"] == "accepted"),
            "rejected_count": sum(1 for item in items if item["status"] == "rejected"),
            "privacy_check": not memory_review_payload_has_raw_private_data({"items": items}),
        },
        "safety": {
            "raw_crm_email_secret_data_persisted": False,
            "source_records_deleted": False,
            "archive_is_non_destructive": True,
        },
    }


def apply_memory_review_item(
    item_id: str,
    action: str,
    *,
    store: ManagerMemoryStore | None = None,
) -> dict[str, Any]:
    memory = store or ManagerMemoryStore()
    memory.initialize()
    if action not in ALLOWED_ACTIONS:
        return {"ok": False, "error": "unsupported action", "allowed_actions": sorted(ALLOWED_ACTIONS)}

    build_memory_review(memory)
    item = _get_review_item(memory, item_id)
    if item is None:
        return {"ok": False, "error": "memory review item not found", "id": item_id}
    if item["status"] != "pending":
        return {
            "ok": False,
            "error": "memory review item already decided",
            "id": item_id,
            "status": item["status"],
            "decided_at": item["decided_at"],
        }

    archived_ids: list[int] = []
    if action == "archive_duplicate":
        if item["kind"] != "duplicate":
            return {"ok": False, "error": "archive_duplicate is only allowed for duplicate review items", "id": item_id}
        archive_result = _archive_duplicate_refs(memory, item["source_ref"])
        if not archive_result["ok"]:
            return {
                "ok": False,
                "error": archive_result["error"],
                "id": item_id,
                "source_ref": item["source_ref"],
                "source_records_deleted": False,
            }
        archived_ids = archive_result["archived_ids"]

    status = "rejected" if action == "reject" else "accepted"
    decided_at = _now()
    with memory.connect() as conn:
        conn.execute(
            """
            UPDATE memory_review_items
            SET status = ?, decided_at = ?
            WHERE id = ?
            """,
            (status, decided_at, item_id),
        )
    return {
        "ok": True,
        "id": item_id,
        "action": action,
        "status": status,
        "archived_duplicate_ids": archived_ids,
        "decided_at": decided_at,
        "source_records_deleted": False,
    }


def memory_review_payload_has_raw_private_data(payload: dict[str, Any]) -> bool:
    rendered = json.dumps(payload, ensure_ascii=False)
    return any(pattern.search(rendered) for pattern in PRIVATE_DATA_PATTERNS)


def _generate_review_items(memory: ManagerMemoryStore) -> list[dict[str, Any]]:
    audit = audit_memory(memory)
    items: list[dict[str, Any]] = []
    for duplicate in audit.get("duplicates") or []:
        ids = [int(value) for value in duplicate.get("ids") or []]
        if len(ids) < 2:
            continue
        kind = str(duplicate.get("kind") or "memory")
        items.append(
            _review_item(
                item_id=f"duplicate:{kind}:{'-'.join(str(value) for value in ids)}",
                kind="duplicate",
                source_ref=f"{kind}:{','.join(str(value) for value in ids)}",
                proposal={
                    "action": "archive_duplicate",
                    "keep_id": ids[0],
                    "archive_ids": ids[1:],
                    "content_included": False,
                },
                reason="Same normalized memory text appears in more than one active item.",
                risk="low",
            )
        )

    for expired in audit.get("expired") or []:
        kind = str(expired.get("kind") or "memory")
        item_id = int(expired.get("id") or 0)
        items.append(
            _review_item(
                item_id=f"expired:{kind}:{item_id}",
                kind="expired",
                source_ref=f"{kind}:{item_id}",
                proposal={"action": "review_for_archive_or_refresh", "content_included": False},
                reason="Memory item is past expires_at and should be refreshed or archived after review.",
                risk="medium",
            )
        )

    for superseded in audit.get("superseded") or []:
        kind = str(superseded.get("kind") or "memory")
        item_id = int(superseded.get("id") or 0)
        items.append(
            _review_item(
                item_id=f"superseded:{kind}:{item_id}",
                kind="superseded",
                source_ref=f"{kind}:{item_id}",
                proposal={
                    "action": "confirm_archive_superseded",
                    "superseded_by": superseded.get("superseded_by"),
                    "content_included": False,
                },
                reason="Memory item is superseded by a newer active item.",
                risk="low",
            )
        )

    items.extend(_weak_confidence_items(memory))
    items.extend(_run_ledger_lesson_candidates(memory))
    return items


def _weak_confidence_items(memory: ManagerMemoryStore) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with memory.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, confidence, importance, updated_at
            FROM facts
            WHERE archived_at IS NULL
                AND (confidence < 0.45 OR importance < 0.25)
            ORDER BY confidence ASC, importance ASC, updated_at DESC
            LIMIT 20
            """
        ).fetchall()
    for row in rows:
        items.append(
            _review_item(
                item_id=f"weak:fact:{int(row['id'])}",
                kind="weak_confidence",
                source_ref=f"fact:{int(row['id'])}",
                proposal={
                    "action": "verify_or_lower_importance",
                    "confidence": float(row["confidence"] or 0),
                    "importance": float(row["importance"] or 0),
                    "content_included": False,
                },
                reason="Fact has weak confidence or low importance and may need verification.",
                risk="medium",
            )
        )
    return items


def _run_ledger_lesson_candidates(memory: ManagerMemoryStore) -> list[dict[str, Any]]:
    with memory.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, status, finished_at
            FROM manager_runs
            WHERE summary != '' AND status IN ('completed', 'failed', 'partial')
            ORDER BY finished_at DESC, updated_at DESC
            LIMIT 10
            """
        ).fetchall()
    return [
        _review_item(
            item_id=f"run_lesson:{int(row['id'])}",
            kind="lesson_candidate",
            source_ref=f"manager_run:{int(row['id'])}",
            proposal={"action": "consider_lesson_from_run_ledger", "content_included": False},
            reason=f"Completed run ledger entry with status '{row['status']}' may contain a reusable lesson.",
            risk="low",
        )
        for row in rows
    ]


def _review_item(
    *,
    item_id: str,
    kind: str,
    source_ref: str,
    proposal: dict[str, Any],
    reason: str,
    risk: str,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "kind": kind,
        "source_ref": source_ref,
        "proposal": proposal,
        "reason": reason,
        "risk": risk,
        "status": "pending",
        "created_at": _now(),
        "decided_at": None,
    }


def _sync_review_items(memory: ManagerMemoryStore, items: list[dict[str, Any]]) -> None:
    with memory.connect() as conn:
        for item in items:
            existing = conn.execute(
                "SELECT id, status FROM memory_review_items WHERE id = ? LIMIT 1",
                (item["id"],),
            ).fetchone()
            proposal_json = json.dumps(item["proposal"], ensure_ascii=False)
            if existing:
                if existing["status"] == "pending":
                    conn.execute(
                        """
                        UPDATE memory_review_items
                        SET kind = ?, source_ref = ?, proposal_json = ?, reason = ?, risk = ?
                        WHERE id = ?
                        """,
                        (item["kind"], item["source_ref"], proposal_json, item["reason"], item["risk"], item["id"]),
                    )
                continue
            conn.execute(
                """
                INSERT INTO memory_review_items
                    (id, kind, source_ref, proposal_json, reason, risk, status, created_at, decided_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL)
                """,
                (item["id"], item["kind"], item["source_ref"], proposal_json, item["reason"], item["risk"], item["created_at"]),
            )


def _list_review_items(memory: ManagerMemoryStore) -> list[dict[str, Any]]:
    with memory.connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM memory_review_items
            ORDER BY status = 'pending' DESC, created_at DESC, id ASC
            LIMIT 200
            """
        ).fetchall()
    return [_row_to_review_item(row) for row in rows]


def _get_review_item(memory: ManagerMemoryStore, item_id: str) -> dict[str, Any] | None:
    with memory.connect() as conn:
        row = conn.execute("SELECT * FROM memory_review_items WHERE id = ? LIMIT 1", (item_id,)).fetchone()
    return _row_to_review_item(row) if row else None


def _row_to_review_item(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "source_ref": row["source_ref"],
        "proposal": _decode_json(row["proposal_json"], {}),
        "reason": row["reason"],
        "risk": row["risk"],
        "status": row["status"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
    }


def _archive_duplicate_refs(memory: ManagerMemoryStore, source_ref: str) -> dict[str, Any]:
    kind, ids = _parse_source_ref(source_ref)
    if kind not in {"note", "fact"} or len(ids) < 2:
        return {"ok": False, "error": "invalid duplicate source reference", "archived_ids": []}
    table = "notes" if kind == "note" else "facts"
    placeholders = ",".join("?" for _ in ids)
    with memory.connect() as conn:
        rows = conn.execute(
            f"SELECT *, ? AS kind FROM {table} WHERE archived_at IS NULL AND id IN ({placeholders})",
            (kind, *ids),
        ).fetchall()
    active_by_id = {int(row["id"]): memory._row_to_dict(row) for row in rows}
    if any(item_id not in active_by_id for item_id in ids):
        return {"ok": False, "error": "duplicate review item is stale", "archived_ids": []}
    normalized = [_normalize_memory_text(active_by_id[item_id]) for item_id in ids]
    if not normalized[0] or len(set(normalized)) != 1:
        return {"ok": False, "error": "duplicate review item no longer points to duplicate memories", "archived_ids": []}
    archived_at = _now()
    archive_ids = ids[1:]
    with memory.connect() as conn:
        for item_id in archive_ids:
            conn.execute(
                f"UPDATE {table} SET archived_at = ?, updated_at = ? WHERE id = ?",
                (archived_at, archived_at, item_id),
            )
    return {"ok": True, "error": "", "archived_ids": archive_ids}


def _parse_source_ref(source_ref: str) -> tuple[str, list[int]]:
    if ":" not in source_ref:
        return "", []
    kind, raw_ids = source_ref.split(":", 1)
    ids = []
    for raw_id in raw_ids.split(","):
        try:
            ids.append(int(raw_id))
        except ValueError:
            continue
    return kind, ids
