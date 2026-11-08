"""Manual M1/M2 task and decision registry; never starts chats, goals or duties."""

from __future__ import annotations
from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any
from uuid import uuid4
from .config import DEFAULT_AUTOMATION_DB_PATH, get_automation_db_path

ROLES = ("M1", "M2")
DEFAULT_ROOT = Path("/var/lib/autostop-manager/roles")
READ_OPERATIONS = ("status", "tasks", "questions", "approvals", "notes", "permissions", "owner_receipt")
ACTION_OPERATIONS = (
    "checkpoint",
    "task_create",
    "task_claim",
    "task_result",
    "task_accept",
    "task_return",
    "task_cancel",
    "question_create",
    "question_resolve",
    "approval_create",
    "approval_decide",
    "approval_consume",
    "note",
    "learn",
    "permission_record",
)
SCHEMA_VERSION = 2
RETIRED_TEMPLATES = ("m1_duty_v1", "m2_duty_v1")
MANUAL_STATE_SQL = """CREATE TABLE IF NOT EXISTS role_state (
 role TEXT PRIMARY KEY CHECK(role IN ('M1','M2')), revision INTEGER NOT NULL DEFAULT 0,
 context TEXT NOT NULL DEFAULT '', updated_at TEXT)"""
SCHEMA = (
    MANUAL_STATE_SQL,
    "CREATE TABLE IF NOT EXISTS role_commands (key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, response TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS role_items (id TEXT PRIMARY KEY, role TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, data TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS role_items_queue ON role_items(kind,role,status,created_at)",
    "CREATE TABLE IF NOT EXISTS role_receipts (id TEXT PRIMARY KEY, role TEXT NOT NULL, item_id TEXT, status TEXT NOT NULL, data TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS role_registry_schema (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL)",
)


class RoleError(ValueError):
    """Only fixed diagnostic codes cross the tool boundary."""


def now() -> str:
    return datetime.now(UTC).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def text_field(value: Any, *, maximum: int = 4000, optional: bool = False) -> str:
    if optional and value is None:
        return ""
    if not isinstance(value, str) or (not optional and not value.strip()) or len(value) > maximum or "\0" in value:
        raise RoleError("role_text_invalid")
    return value.strip()


def ref_field(value: Any) -> str:
    value = text_field(value, maximum=200)
    if not re.fullmatch(r"[A-Za-z0-9_.:/@+-]+", value):
        raise RoleError("role_reference_invalid")
    return value


def role_name(value: str) -> str:
    if value not in ROLES:
        raise RoleError("role_invalid")
    return value


def check_revision(expected: Any, current: int) -> None:
    if type(expected) is not int or expected != current:
        raise RoleError("role_revision_conflict")


def default_state(role: str) -> dict[str, Any]:
    return {"role": role, "revision": 0, "context": "", "updated_at": None}


class RoleStore:
    def __init__(self, path: str | Path | None = None, *, views_root: str | Path | None = None) -> None:
        self.path = Path(path or get_automation_db_path())
        self.views_root = Path(
            views_root or (DEFAULT_ROOT if self.path == DEFAULT_AUTOMATION_DB_PATH else self.path.parent / "roles")
        )

    def initialize(self) -> None:
        """Retire dormant duties atomically, preserving manual records and receipts."""
        with self.connection(write=True):
            pass

    @staticmethod
    def _prepare(db: sqlite3.Connection) -> None:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "role_registry_schema" in tables:
            row = db.execute("SELECT version FROM role_registry_schema WHERE singleton=1").fetchone()
            if row is not None and row[0] > SCHEMA_VERSION:
                raise RoleError("role_schema_unsupported")
            if row is not None and row[0] == SCHEMA_VERSION:
                return
        if (
            "role_dispatches" in tables
            and db.execute(
                "SELECT 1 FROM role_dispatches WHERE status NOT IN ('completed','failed') LIMIT 1"
            ).fetchone()
        ):
            raise RoleError("role_retirement_requires_review")
        if "manager_automations" in tables:
            for table, state in (("manager_automation_runs", "running"), ("manager_automation_outbox", "sending")):
                if (
                    table in tables
                    and db.execute(
                        f"SELECT 1 FROM {table} WHERE status=? AND job_id IN "
                        "(SELECT job_id FROM manager_automations WHERE template_id IN (?,?)) LIMIT 1",
                        (state, *RETIRED_TEMPLATES),
                    ).fetchone()
                ):
                    raise RoleError("role_retirement_requires_review")
        for statement in SCHEMA:
            db.execute(statement)
        columns = {row[1] for row in db.execute("PRAGMA table_info(role_state)")}
        if columns - {"role", "revision", "context", "updated_at"}:
            # Old executor-owned running work is never resumed or declared complete.
            # Retire its claim into explicit human review so unrelated work is not blocked.
            items = db.execute("SELECT id,data FROM role_items WHERE kind='task' AND status='running'").fetchall()
            for ident, encoded in items:
                data = json.loads(encoded)
                data["retirement_review"] = {"reason": "legacy_executor_retired", "original_status": "running"}
                db.execute(
                    "UPDATE role_items SET status='review',revision=revision+1,data=?,updated_at=? WHERE id=?",
                    (canonical(data), now(), ident),
                )
            db.execute("ALTER TABLE role_state RENAME TO role_state_retiring")
            db.execute(MANUAL_STATE_SQL)
            db.execute("INSERT INTO role_state SELECT role,revision,context,updated_at FROM role_state_retiring")
            db.execute("DROP TABLE role_state_retiring")
        if "role_dispatches" in tables:
            names = [row[1] for row in db.execute("PRAGMA table_info(role_dispatches)")]
            for values in db.execute("SELECT * FROM role_dispatches"):
                row = dict(zip(names, values, strict=True))
                receipt = {
                    key: row.get(key)
                    for key in (
                        "id",
                        "role",
                        "kind",
                        "item_id",
                        "thread_id",
                        "before_turn_id",
                        "turn_id",
                        "status",
                        "started_at",
                        "updated_at",
                        "error",
                    )
                }
                db.execute(
                    "INSERT OR IGNORE INTO role_receipts VALUES (?,?,?,?,?)",
                    (row["id"], row["role"], row["item_id"], row["status"], canonical(receipt)),
                )
            db.execute("DROP TABLE role_dispatches")
        if "manager_automations" in tables:
            stamp = now()
            jobs = [
                row[0]
                for row in db.execute(
                    "SELECT job_id FROM manager_automations WHERE template_id IN (?,?)", RETIRED_TEMPLATES
                )
            ]
            for ident in jobs:
                db.execute(
                    "UPDATE manager_automations SET desired_state='off',archived_at=COALESCE(archived_at,?),updated_at=? WHERE job_id=?",
                    (stamp, stamp, ident),
                )
                if "manager_automation_runtime" in tables:
                    db.execute(
                        "UPDATE manager_automation_runtime SET actual_state='disabled',next_run_at=NULL,lease_owner_hash=NULL,lease_until=NULL,error_code=NULL,updated_at=? WHERE job_id=?",
                        (stamp, ident),
                    )
                if "manager_automation_runs" in tables:
                    db.execute(
                        "UPDATE manager_automation_runs SET status='cancelled',finished_at=?,result_code='role_duty_retired' WHERE job_id=? AND status='queued'",
                        (stamp, ident),
                    )
                if "manager_automation_outbox" in tables:
                    db.execute(
                        "UPDATE manager_automation_outbox SET status='cancelled',updated_at=?,result_code='role_duty_retired' WHERE job_id=? AND status='queued'",
                        (stamp, ident),
                    )
        for role in ROLES:
            db.execute("INSERT OR IGNORE INTO role_state(role) VALUES (?)", (role,))
        db.execute("INSERT OR REPLACE INTO role_registry_schema VALUES (1,?)", (SCHEMA_VERSION,))

    @contextmanager
    def connection(self, *, write: bool = False):
        if self.path.is_symlink():
            raise RoleError("role_storage_invalid")
        if write:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.path.parent.is_symlink():
                raise RoleError("role_storage_invalid")
            db = sqlite3.connect(self.path, timeout=5)
        else:
            if not self.path.exists():
                yield None
                return
            db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            if write:
                os.chmod(self.path, 0o600)
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("BEGIN IMMEDIATE")
                self._prepare(db)
            else:
                db.execute("PRAGMA query_only=ON")
            db.row_factory = sqlite3.Row
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _state(db: sqlite3.Connection | None, role: str) -> dict[str, Any]:
        role_name(role)
        exists = db and db.execute("SELECT 1 FROM sqlite_master WHERE name='role_state'").fetchone()
        row = (
            db.execute("SELECT role,revision,context,updated_at FROM role_state WHERE role=?", (role,)).fetchone()
            if db and exists
            else None
        )
        return dict(row) if row else default_state(role)

    def role_state(self, role: str = "M1") -> dict[str, Any]:
        with self.connection() as db:
            return self._state(db, role)

    @staticmethod
    def _item(db: sqlite3.Connection, item_id: str, kind: str | None = None) -> dict[str, Any]:
        row = db.execute("SELECT * FROM role_items WHERE id=?", (item_id,)).fetchone()
        if row is None or (kind and row["kind"] != kind):
            raise RoleError("role_item_not_found")
        return {**dict(row), "data": json.loads(row["data"])}

    @staticmethod
    def _insert(
        db: sqlite3.Connection,
        role: str,
        kind: str,
        data: dict[str, Any],
        *,
        status: str = "open",
        item_id: str | None = None,
    ) -> dict[str, Any]:
        item_id = item_id or str(uuid4())
        stamp = now()
        db.execute(
            "INSERT INTO role_items VALUES (?,?,?,?,1,?,?,?)",
            (item_id, role, kind, status, canonical(data), stamp, stamp),
        )
        return RoleStore._item(db, item_id)

    @staticmethod
    def _update_item(db: sqlite3.Connection, item: dict[str, Any], status: str, data: dict[str, Any]) -> dict[str, Any]:
        db.execute(
            "UPDATE role_items SET status=?,data=?,revision=revision+1,updated_at=? WHERE id=?",
            (status, canonical(data), now(), item["id"]),
        )
        return RoleStore._item(db, item["id"])

    def read(
        self, operation: str = "status", role: str = "M1", *, reference_id: str = "", limit: int = 30
    ) -> dict[str, Any]:
        role_name(role)
        if operation not in READ_OPERATIONS or type(limit) is not int or not 1 <= limit <= 100:
            raise RoleError("role_read_invalid")
        if operation == "owner_receipt":
            return {"ok": True, "receipt": self.owner_event_receipt(reference_id)}
        with self.connection() as db:
            if operation == "status":
                return {
                    "ok": True,
                    "roles": [self._state(db, name) for name in ROLES],
                }
            kinds = {
                "tasks": ("task",),
                "questions": ("question",),
                "approvals": ("approval",),
                "notes": ("observation", "idea", "knowledge"),
                "permissions": ("permission",),
            }[operation]
            if db is None or not db.execute("SELECT 1 FROM sqlite_master WHERE name='role_items'").fetchone():
                return {"ok": True, "items": []}
            if reference_id:
                item = self._item(db, reference_id)
                if item["kind"] not in kinds:
                    raise RoleError("role_item_not_found")
                return {"ok": True, "items": [item]}
            slots = ",".join("?" for _ in kinds)
            order = "created_at DESC"
            if operation == "tasks":
                order = (
                    "CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'review' THEN 2 ELSE 3 END, "
                    "CASE json_extract(data,'$.source') WHEN 'owner' THEN 0 WHEN 'M1' THEN 1 ELSE 2 END, "
                    "CASE json_extract(data,'$.urgent') WHEN 1 THEN 0 ELSE 1 END, created_at"
                )
            elif operation in {"questions", "approvals"}:
                order = "CASE WHEN status IN ('open','pending','approved') THEN 0 ELSE 1 END, created_at"
            rows = db.execute(
                f"SELECT * FROM role_items WHERE role=? AND kind IN ({slots}) ORDER BY {order} LIMIT ?",
                (role, *kinds, limit),
            ).fetchall()
            items = [{**dict(row), "data": json.loads(row["data"])} for row in rows]
            return {"ok": True, "items": items}

    @staticmethod
    def _task_priority(data: dict[str, Any]) -> tuple[int, int]:
        return ({"owner": 0, "M1": 1, "M2": 2}.get(data.get("source", ""), 3), -int(data.get("urgent") is True))

    def action(
        self,
        operation: str,
        role: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str,
        expected_revision: int | None = None,
        actor: str = "owner",
    ) -> dict[str, Any]:
        role_name(role)
        if operation not in ACTION_OPERATIONS or actor not in ("owner", "M1", "M2"):
            raise RoleError("role_action_invalid")
        key = text_field(idempotency_key, maximum=200)
        payload = payload or {}
        encoded = canonical(payload)
        if not isinstance(payload, dict) or len(encoded.encode()) > 16000:
            raise RoleError("role_payload_invalid")
        digest = hashlib.sha256(canonical([operation, role, payload, expected_revision, actor]).encode()).hexdigest()
        with self.connection(write=True) as db:
            old = db.execute("SELECT * FROM role_commands WHERE key=?", (key,)).fetchone()
            if old:
                if old["request_hash"] != digest:
                    raise RoleError("role_idempotency_conflict")
                return json.loads(old["response"])
            result = {"ok": True, **self._apply(db, operation, role, payload, expected_revision, actor)}
            db.execute("INSERT INTO role_commands VALUES (?,?,?,?)", (key, digest, canonical(result), now()))
        self.export_views()
        return result

    def _apply(
        self, db: sqlite3.Connection, operation: str, role: str, p: dict[str, Any], revision: int | None, actor: str
    ) -> dict[str, Any]:
        if operation == "checkpoint":
            state = self._state(db, role)
            check_revision(revision, state["revision"])
            if actor not in {"owner", role}:
                raise RoleError("role_actor_denied")
            db.execute(
                "UPDATE role_state SET context=?,revision=revision+1,updated_at=? WHERE role=?",
                (text_field(p.get("text"), maximum=8000), now(), role),
            )
            return {"role": self._state(db, role)}
        if operation.startswith("task_"):
            return self._task_action(db, operation, role, p, revision, actor)
        if operation.startswith("approval_"):
            return self._approval_action(db, operation, role, p, revision, actor)
        if operation == "question_resolve":
            item = self._item(db, ref_field(p.get("id")), "question")
            check_revision(revision, item["revision"])
            data = {**item["data"], "answer": text_field(p.get("answer")), "answer_ref": ref_field(p.get("answer_ref"))}
            return {"item": self._update_item(db, item, "resolved", data)}
        kind = {
            "question_create": "question",
            "note": p.get("kind", "observation"),
            "learn": "knowledge",
            "permission_record": "permission",
        }.get(operation)
        if kind not in {"question", "observation", "idea", "knowledge", "permission"}:
            raise RoleError("role_note_kind_invalid")
        if kind == "permission" and actor != "owner":
            raise RoleError("role_owner_required")
        data = {
            "text": text_field(p.get("text")),
            "source_ref": text_field(p.get("source_ref"), maximum=400, optional=True),
        }
        if kind in {"knowledge", "permission"} and not data["source_ref"]:
            raise RoleError("role_evidence_required")
        return {"item": self._insert(db, role, kind, data, status="verified" if kind == "knowledge" else "open")}

    def _task_action(
        self, db: sqlite3.Connection, op: str, role: str, p: dict[str, Any], revision: int | None, actor: str
    ) -> dict[str, Any]:
        if op == "task_create":
            resource = ref_field(p.get("resource"))
            for row in db.execute(
                "SELECT data FROM role_items WHERE kind='task' AND status IN ('queued','running','review')"
            ):
                if json.loads(row[0]).get("resource") == resource:
                    raise RoleError("role_task_resource_busy")
            data = {
                "title": text_field(p.get("title"), maximum=200),
                "objective": text_field(p.get("objective")),
                "resource": resource,
                "source": actor,
                "acceptance": text_field(p.get("acceptance")),
                "claimed_by": None,
                "urgent": p.get("urgent") is True,
            }
            task = self._insert(db, "M2", "task", data, status="queued")
            return {"item": task}
        item = self._item(db, ref_field(p.get("id")), "task")
        check_revision(revision, item["revision"])
        data = item["data"]
        if op == "task_claim":
            if actor != "owner" and actor != role:
                raise RoleError("role_task_actor_mismatch")
            if item["status"] != "queued":
                raise RoleError("role_task_not_queued")
            running = db.execute(
                "SELECT 1 FROM role_items WHERE kind='task' AND status='running' AND json_extract(data,'$.resource')=?",
                (data["resource"],),
            ).fetchone()
            if running:
                raise RoleError("role_engineer_busy")
            queued = db.execute(
                "SELECT data FROM role_items WHERE kind='task' AND status='queued' AND id<>?", (item["id"],)
            ).fetchall()
            if actor != "owner" and any(
                self._task_priority(json.loads(row[0])) < self._task_priority(data) for row in queued
            ):
                raise RoleError("role_higher_priority_task")
            data = {**data, "claimed_by": role}
            status = "running"
        elif op == "task_result":
            if item["status"] != "running" or data.get("claimed_by") != role or actor not in {"owner", role}:
                raise RoleError("role_task_not_owned")
            data = {
                **data,
                "result": text_field(p.get("result")),
                "verification": text_field(p.get("verification")),
                "artifact_ref": text_field(p.get("artifact_ref"), maximum=400, optional=True),
                "release_required": p.get("release_required") is True,
                "source_state": p.get("source_state", "local"),
                "deployment_state": p.get(
                    "deployment_state", "pending" if p.get("release_required") else "not_required"
                ),
                "installed_revision": text_field(p.get("installed_revision"), maximum=80, optional=True),
            }
            if data["source_state"] not in {"local", "published", "merged"} or data["deployment_state"] not in {
                "not_required",
                "pending",
                "deployed",
                "failed",
            }:
                raise RoleError("role_task_result_state_invalid")
            if data["deployment_state"] == "deployed" and not data["installed_revision"]:
                raise RoleError("role_deployment_evidence_required")
            status = "review"
        elif op == "task_accept":
            if actor not in {"M1", "owner"} or item["status"] != "review":
                raise RoleError("role_task_accept_denied")
            data = {**data, "acceptance_result": text_field(p.get("verification"))}
            status = "completed"
        elif op == "task_return":
            if actor not in {"M1", "owner"} or item["status"] != "review":
                raise RoleError("role_task_return_denied")
            data = {**data, "rework_reason": text_field(p.get("reason")), "claimed_by": None}
            status = "queued"
        elif op == "task_cancel":
            if actor not in {"M1", "owner"} or item["status"] == "running":
                raise RoleError("role_task_cancel_denied")
            status = "cancelled"
        else:
            raise RoleError("role_action_invalid")
        return {"item": self._update_item(db, item, status, data)}

    def _approval_action(
        self, db: sqlite3.Connection, op: str, role: str, p: dict[str, Any], revision: int | None, actor: str
    ) -> dict[str, Any]:
        if op == "approval_create":
            data = {
                key: text_field(p.get(key), maximum=1000)
                for key in ("action", "target", "target_revision", "proposal_ref")
            }
            data["proposal_hash"] = text_field(p.get("proposal_hash"), maximum=64)
            if not re.fullmatch(r"[a-f0-9]{64}", data["proposal_hash"]):
                raise RoleError("role_proposal_hash_invalid")
            return {"item": self._insert(db, role, "approval", data, status="pending")}
        item = self._item(db, ref_field(p.get("id")), "approval")
        check_revision(revision, item["revision"])
        data = item["data"]
        if op == "approval_decide":
            if actor != "owner" or item["status"] != "pending" or p.get("decision") not in {"approved", "rejected"}:
                raise RoleError("role_approval_decision_denied")
            data = {**data, "decision_ref": text_field(p.get("decision_ref"), maximum=400), "decided_at": now()}
            status = p["decision"]
        else:
            if item["status"] != "approved":
                raise RoleError("role_approval_not_available")
            if any(p.get(key) != data[key] for key in ("action", "target", "target_revision", "proposal_hash")):
                raise RoleError("role_approval_stale")
            data = {**data, "used_at": now()}
            status = "used"
        return {"item": self._update_item(db, item, status, data)}

    def owner_event_receipt(self, event_id: str) -> dict[str, Any] | None:
        """Read historical owner evidence; never schedule work."""
        item_id = "owner-" + hashlib.sha256(ref_field(event_id).encode()).hexdigest()[:32]
        with self.connection() as db:
            if db is None or not db.execute("SELECT 1 FROM sqlite_master WHERE name='role_items'").fetchone():
                return None
            row = db.execute("SELECT * FROM role_items WHERE id=? AND kind='owner_event'", (item_id,)).fetchone()
            if row is None:
                return None
            data = json.loads(row["data"])
            receipts = []
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='role_receipts'").fetchone():
                receipts = [
                    json.loads(r[0])
                    for r in db.execute("SELECT data FROM role_receipts WHERE item_id=? ORDER BY id", (item_id,))
                ]
            return {
                "receipt_id": item_id,
                "event_id": data["event_id"],
                "event_ids": data["event_ids"],
                "role": "M1",
                "status": row["status"],
                "instruction_root": data.get("instruction_root"),
                "instruction_revision": data.get("instruction_revision"),
                "historical": True,
                "dispatch_receipts": receipts,
            }

    def export_views(self) -> None:
        """Readable manual context; M2's independent technical journal is untouched."""
        for role in ROLES:
            state = self.role_state(role)
            body = f"# {role} — рабочий контекст\n\n{state['context']}\n"
            notes = self.read("notes", role, limit=100)["items"]
            note_text = f"# {role} — наблюдения, идеи и опыт\n\n" + "\n".join(
                f"- {item['created_at']} [{item['kind']}]: {item['data']['text']} ({item['data']['source_ref']})"
                for item in notes
            )
            directory = self.views_root / role
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            for name, content in (("context.md", body), ("notes.md", note_text)):
                target = directory / name
                if target.is_symlink():
                    raise RoleError("role_view_invalid")
                fd, temporary = tempfile.mkstemp(prefix=".role-", dir=directory)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        stream.write(content)
                    os.replace(temporary, target)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
