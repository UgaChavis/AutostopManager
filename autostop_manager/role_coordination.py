"""Durable M1/M2 coordination. Source business records stay in their own systems."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import tempfile
from typing import Any
from uuid import UUID, uuid4

from .config import DEFAULT_AUTOMATION_DB_PATH, get_automation_db_path


ROLES = ("M1", "M2")
MODEL = "gpt-6.1-terra"
DEFAULT_ROOT = Path("/var/lib/autostop-manager/roles")
PROJECT_DIR = "/opt/AutostopManager"
READ_OPERATIONS = ("status", "tasks", "questions", "approvals", "notes", "permissions", "owner_receipt")
ACTION_OPERATIONS = (
    "bind",
    "start",
    "stop",
    "stop_all",
    "sleep",
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
    "reconcile_turn",
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


def executor_host_id() -> str:
    return os.environ.get("AUTOSTOP_ROLE_EXECUTOR_HOST_ID") or socket.gethostname()


def check_revision(expected: Any, current: int) -> None:
    if type(expected) is not int or expected != current:
        raise RoleError("role_revision_conflict")


def default_state(role: str) -> dict[str, Any]:
    return {
        "role": role,
        "host_id": executor_host_id(),
        "thread_id": None,
        "model": MODEL,
        "desired_state": "off",
        "duty_active": False,
        "revision": 0,
        "generation": 0,
        "state": "off",
        "next_wake_at": None,
        "active_dispatch_id": None,
        "goal_owned": False,
        "goal_applied": None,
        "instruction_root": None,
        "instruction_revision": None,
        "last_error": None,
        "context": "",
    }


SCHEMA = """
CREATE TABLE IF NOT EXISTS role_state (
 role TEXT PRIMARY KEY CHECK(role IN ('M1','M2')), thread_id TEXT UNIQUE, host_id TEXT NOT NULL, model TEXT NOT NULL,
 desired_state TEXT NOT NULL DEFAULT 'off', revision INTEGER NOT NULL DEFAULT 0,
 generation INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'off', next_wake_at TEXT,
 active_dispatch_id TEXT, goal_owned INTEGER NOT NULL DEFAULT 0, goal_applied TEXT,
 instruction_root TEXT, instruction_revision TEXT, last_error TEXT, context TEXT NOT NULL DEFAULT '', updated_at TEXT
);
CREATE TABLE IF NOT EXISTS role_commands (
 key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, response TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS role_items (
 id TEXT PRIMARY KEY, role TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, data TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS role_items_queue ON role_items(kind,role,status,created_at);
CREATE TABLE IF NOT EXISTS role_dispatches (
 id TEXT PRIMARY KEY, role TEXT NOT NULL, generation INTEGER NOT NULL, kind TEXT NOT NULL,
 item_id TEXT, thread_id TEXT NOT NULL, turn_id TEXT, status TEXT NOT NULL,
 before_turn_id TEXT, started_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT, scan_cursor TEXT
);
"""


class RoleStore:
    def __init__(self, path: str | Path | None = None, *, views_root: str | Path | None = None) -> None:
        self.path = Path(path or get_automation_db_path())
        self.views_root = Path(
            views_root or (DEFAULT_ROOT if self.path == DEFAULT_AUTOMATION_DB_PATH else self.path.parent / "roles")
        )

    def initialize(self) -> None:
        with self.connection(write=True):
            pass
        self.project_automation_jobs()

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
                db.executescript(SCHEMA)
                columns = {row[1] for row in db.execute("PRAGMA table_info(role_state)")}
                if "host_id" not in columns:
                    db.execute("ALTER TABLE role_state ADD COLUMN host_id TEXT")
                    db.execute("UPDATE role_state SET host_id=? WHERE host_id IS NULL", (executor_host_id(),))
                dispatch_columns = {row[1] for row in db.execute("PRAGMA table_info(role_dispatches)")}
                if "scan_cursor" not in dispatch_columns:
                    db.execute("ALTER TABLE role_dispatches ADD COLUMN scan_cursor TEXT")
                for role in ROLES:
                    db.execute(
                        "INSERT OR IGNORE INTO role_state(role,host_id,model) VALUES (?,?,?)",
                        (role, executor_host_id(), MODEL),
                    )
                db.commit()
                db.execute("BEGIN IMMEDIATE")
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
        exists = db and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='role_state'").fetchone()
        row = db.execute("SELECT * FROM role_state WHERE role=?", (role,)).fetchone() if db and exists else None
        result = dict(row) if row else default_state(role)
        result["duty_active"] = result["desired_state"] == "on"
        result["goal_owned"] = bool(result["goal_owned"])
        return result

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
                    "dispatches": {name: self.pending_dispatches(name) for name in ROLES},
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
        self.project_automation_jobs()
        return result

    def _apply(
        self, db: sqlite3.Connection, operation: str, role: str, p: dict[str, Any], revision: int | None, actor: str
    ) -> dict[str, Any]:
        if operation == "stop_all":
            if actor != "owner" or role != "M1":
                raise RoleError("role_owner_required")
            self._role_action(db, "stop", "M1", {}, revision, actor)
            self._role_action(db, "stop", "M2", {}, p.get("m2_revision"), actor)
            return {"roles": [self._state(db, name) for name in ROLES]}
        if operation in {"bind", "start", "stop", "sleep", "checkpoint", "reconcile_turn"}:
            return self._role_action(db, operation, role, p, revision, actor)
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

    def _role_action(
        self, db: sqlite3.Connection, operation: str, role: str, p: dict[str, Any], revision: int | None, actor: str
    ) -> dict[str, Any]:
        state = self._state(db, role)
        check_revision(revision, state["revision"])
        if actor != "owner" and actor != role and not (actor == "M1" and role == "M2" and operation == "start"):
            raise RoleError("role_actor_denied")
        if operation == "bind":
            pending = db.execute(
                "SELECT 1 FROM role_dispatches WHERE role=? AND status IN ('starting','running','unknown') LIMIT 1",
                (role,),
            ).fetchone()
            if (
                actor != "owner"
                or pending
                or state["active_dispatch_id"]
                or state["desired_state"] != "off"
                or state["goal_owned"]
            ):
                raise RoleError("role_bind_requires_idle")
            thread = str(UUID(text_field(p.get("thread_id"), maximum=50)))
            host = p.get("host_id", executor_host_id())
            if host != executor_host_id():
                raise RoleError("role_host_mismatch")
            root, sha = p.get("instruction_root"), p.get("instruction_revision")
            if (root is None) != (sha is None):
                raise RoleError("role_instruction_invalid")
            if root is not None and (not Path(root).is_absolute() or not re.fullmatch(r"[0-9a-f]{40}", str(sha))):
                raise RoleError("role_instruction_invalid")
            try:
                db.execute(
                    "UPDATE role_state SET thread_id=?,host_id=?,instruction_root=?,instruction_revision=?,last_error=NULL WHERE role=?",
                    (thread, host, root, sha, role),
                )
            except sqlite3.IntegrityError as exc:
                raise RoleError("role_thread_already_bound") from exc
        elif operation == "start":
            if actor != "owner" and not (actor == "M1" and role == "M2"):
                raise RoleError("role_owner_required")
            if not state["thread_id"]:
                raise RoleError("role_unbound")
            db.execute(
                "UPDATE role_state SET desired_state='on',state='idle',next_wake_at=?,generation=generation+1,last_error=NULL,goal_applied=NULL WHERE role=?",
                (now(), role),
            )
        elif operation == "stop":
            if actor != "owner":
                raise RoleError("role_owner_required")
            db.execute(
                "UPDATE role_state SET desired_state='off',state='off',next_wake_at=NULL,generation=generation+1 WHERE role=?",
                (role,),
            )
        elif operation == "sleep":
            seconds = p.get("seconds")
            if type(seconds) is not int or not 1 <= seconds <= 3600 or not state["duty_active"]:
                raise RoleError("role_sleep_invalid")
            wake_at = (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()
            db.execute("UPDATE role_state SET state='waiting',next_wake_at=? WHERE role=?", (wake_at, role))
        elif operation == "checkpoint":
            db.execute("UPDATE role_state SET context=? WHERE role=?", (text_field(p.get("text"), maximum=8000), role))
        elif operation == "reconcile_turn":
            if actor != "owner":
                raise RoleError("role_owner_required")
            dispatch = db.execute(
                "SELECT * FROM role_dispatches WHERE id=? AND role=?",
                (p.get("dispatch_id", state["active_dispatch_id"]), role),
            ).fetchone()
            if dispatch is None or dispatch["status"] != "unknown":
                raise RoleError("role_dispatch_not_unknown")
            turn_id = text_field(p.get("turn_id"), maximum=160)
            db.execute(
                "UPDATE role_dispatches SET turn_id=?,status='running',updated_at=? WHERE id=?",
                (turn_id, now(), dispatch["id"]),
            )
        db.execute("UPDATE role_state SET revision=revision+1,updated_at=? WHERE role=?", (now(), role))
        return {"role": self._state(db, role)}

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
            self._insert(db, "M2", "task_notice", {"task_id": task["id"], "urgent": data["urgent"]}, status="queued")
            return {"item": task}
        item = self._item(db, ref_field(p.get("id")), "task")
        check_revision(revision, item["revision"])
        data = item["data"]
        if op == "task_claim":
            if actor != "owner" and actor != role:
                raise RoleError("role_task_actor_mismatch")
            if item["status"] != "queued":
                raise RoleError("role_task_not_queued")
            if role == "M1" and self._state(db, "M2")["duty_active"]:
                raise RoleError("role_m2_is_on_duty")
            running = db.execute("SELECT 1 FROM role_items WHERE kind='task' AND status='running'").fetchone()
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
            self._insert(db, "M1", "task_notice", {"task_id": item["id"], "result_ready": True}, status="queued")
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
            self._insert(db, "M2", "task_notice", {"task_id": item["id"], "rework": True}, status="queued")
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

    def submit_owner_event(
        self,
        event_id: str,
        *,
        event_ids: list[str] | None = None,
        job_id: str | None = None,
        instruction_root: str | None = None,
        instruction_revision: str | None = None,
    ) -> dict[str, Any]:
        """Called only by the trusted root wake after the bridge verifies owner identity."""
        event_id = ref_field(event_id)
        events = list(dict.fromkeys(event_ids or [event_id]))
        if event_id not in events or not 1 <= len(events) <= 50:
            raise RoleError("role_owner_events_invalid")
        for value in events:
            ref_field(value)
        data = {
            "event_id": event_id,
            "event_ids": events,
            "job_id": job_id,
            "instruction_root": instruction_root,
            "instruction_revision": instruction_revision,
        }
        item_id = "owner-" + hashlib.sha256(event_id.encode()).hexdigest()[:32]
        with self.connection(write=True) as db:
            old = db.execute("SELECT data FROM role_items WHERE id=?", (item_id,)).fetchone()
            if old:
                existing = json.loads(old[0])
                if any(existing.get(key) != value for key, value in data.items()):
                    raise RoleError("role_owner_event_conflict")
            else:
                self._insert(db, "M1", "owner_event", data, status="queued", item_id=item_id)
        return self.owner_event_receipt(event_id) or {}

    def owner_event_receipt(self, event_id: str) -> dict[str, Any] | None:
        item_id = "owner-" + hashlib.sha256(ref_field(event_id).encode()).hexdigest()[:32]
        with self.connection() as db:
            if db is None or not db.execute("SELECT 1 FROM sqlite_master WHERE name='role_items'").fetchone():
                return None
            row = db.execute("SELECT * FROM role_items WHERE id=? AND kind='owner_event'", (item_id,)).fetchone()
            if row is None:
                return None
            data = json.loads(row["data"])
            dispatch = db.execute(
                "SELECT * FROM role_dispatches WHERE item_id=? ORDER BY started_at DESC LIMIT 1", (item_id,)
            ).fetchone()
            return {
                "receipt_id": item_id,
                "event_id": data["event_id"],
                "event_ids": data["event_ids"],
                "role": "M1",
                "status": row["status"],
                "instruction_root": data.get("instruction_root"),
                "instruction_revision": data.get("instruction_revision"),
                "thread_id": dispatch["thread_id"] if dispatch else None,
                "turn_id": dispatch["turn_id"] if dispatch else None,
                "dispatch_id": dispatch["id"] if dispatch else None,
            }

    def dispatch_state(self, role: str) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
        with self.connection() as db:
            state = self._state(db, role)
            if db is None or not db.execute("SELECT 1 FROM sqlite_master WHERE name='role_items'").fetchone():
                return state, None, None
            dispatch = db.execute("SELECT * FROM role_dispatches WHERE id=?", (state["active_dispatch_id"],)).fetchone()
            owner = db.execute(
                "SELECT id FROM role_items WHERE role=? AND status='queued' AND (kind='owner_event' OR (kind='task_notice' AND ?)) ORDER BY CASE kind WHEN 'owner_event' THEN 0 ELSE 1 END,created_at LIMIT 1",
                (role, state["duty_active"]),
            ).fetchone()
            return state, dict(dispatch) if dispatch else None, self._item(db, owner[0]) if owner else None

    def prepare_dispatch(
        self,
        role: str,
        *,
        revision: int,
        generation: int,
        item_id: str | None,
        before_turn_id: str | None,
        steer: bool = False,
        instruction_pin: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        with self.connection(write=True) as db:
            state = self._state(db, role)
            check_revision(revision, state["revision"])
            if (
                state["generation"] != generation
                or (state["active_dispatch_id"] and not steer)
                or not state["thread_id"]
            ):
                raise RoleError("role_dispatch_conflict")
            if steer and not item_id:
                raise RoleError("role_dispatch_conflict")
            if item_id is not None:
                item = self._item(db, item_id)
                if item["kind"] not in {"owner_event", "task_notice"}:
                    raise RoleError("role_dispatch_conflict")
                if item["status"] != "queued":
                    raise RoleError("role_dispatch_conflict")
                self._update_item(db, item, "running", item["data"])
            elif not state["duty_active"]:
                raise RoleError("role_duty_off")
            dispatch_id = str(uuid4())
            stamp = now()
            db.execute(
                "INSERT INTO role_dispatches(id,role,generation,kind,item_id,thread_id,turn_id,status,before_turn_id,started_at,updated_at,error) VALUES (?,?,?,?,?,?,NULL,'starting',?,?,?,NULL)",
                (
                    dispatch_id,
                    role,
                    generation,
                    ("owner" if item["kind"] == "owner_event" else "notice") if item_id else "duty",
                    item_id,
                    state["thread_id"],
                    before_turn_id,
                    stamp,
                    stamp,
                ),
            )
            if not steer:
                if item_id is not None and any(
                    item["data"].get(key) is not None for key in ("instruction_root", "instruction_revision")
                ):
                    # An accepted request already owns its immutable origin pin.
                    instruction_pin = None
                root, pin_revision = instruction_pin or (state["instruction_root"], state["instruction_revision"])
                db.execute(
                    "UPDATE role_state SET active_dispatch_id=?,state='running',instruction_root=?,instruction_revision=?,updated_at=? WHERE role=?",
                    (dispatch_id, root, pin_revision, stamp, role),
                )
            return dict(db.execute("SELECT * FROM role_dispatches WHERE id=?", (dispatch_id,)).fetchone())

    def pending_dispatches(self, role: str) -> list[dict[str, Any]]:
        with self.connection() as db:
            if db is None or not db.execute("SELECT 1 FROM sqlite_master WHERE name='role_dispatches'").fetchone():
                return []
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM role_dispatches WHERE role=? AND status IN ('starting','running','unknown') ORDER BY started_at",
                    (role,),
                )
            ]

    def advance_dispatch_scan(self, dispatch_id: str, cursor: str) -> None:
        with self.connection(write=True) as db:
            db.execute("UPDATE role_dispatches SET scan_cursor=? WHERE id=?", (cursor, dispatch_id))

    def project_automation_jobs(self) -> None:
        """The center shows ordinary singleton jobs; the role remains authoritative."""
        from .automation_registry import AutomationStore, ROLE_TEMPLATES, isoformat, parse_time, technical_actor_hash

        automation = AutomationStore(self.path)
        automation.initialize()
        with self.connection(write=True) as db:
            stamp = isoformat(datetime.now(UTC))
            for template_id, role in ROLE_TEMPLATES.items():
                row = db.execute(
                    "SELECT * FROM manager_automations WHERE template_id=? AND archived_at IS NULL", (template_id,)
                ).fetchone()
                if row is None:
                    created = automation.create_from_template(
                        db,
                        template_id=template_id,
                        payload={"template_id": template_id},
                        actor_hash=technical_actor_hash("system", "role-controller"),
                    )
                    job_id = created["job"]["job_id"]
                    row = automation._require_job(db, job_id)
                job_id = row["job_id"]
                state = self._state(db, role)
                desired = state["desired_state"]
                revision = row["revision"] + int(row["desired_state"] != desired)
                runtime = db.execute("SELECT * FROM manager_automation_runtime WHERE job_id=?", (job_id,)).fetchone()
                next_run = None
                if state["duty_active"]:
                    wake = parse_time(state["next_wake_at"]) if state["state"] == "waiting" else None
                    next_run = isoformat(wake) if wake else runtime["next_run_at"] or stamp
                actual = (
                    "error"
                    if state["state"] == "attention"
                    else "disabled"
                    if not state["duty_active"]
                    else "running"
                    if state["state"] == "running"
                    else "idle"
                )
                db.execute(
                    "UPDATE manager_automations SET desired_state=?,revision=?,updated_at=? WHERE job_id=?",
                    (desired, revision, stamp, job_id),
                )
                db.execute(
                    "UPDATE manager_automation_runtime SET actual_state=?,next_run_at=?,applied_revision=?,applied_at=?,error_code=?,heartbeat_at=?,updated_at=? WHERE job_id=?",
                    (actual, next_run, revision, stamp, state["last_error"], stamp, stamp, job_id),
                )
                if not state["duty_active"]:
                    db.execute(
                        "UPDATE manager_automation_runs SET status='cancelled',finished_at=?,result_code='automation_disabled' WHERE job_id=? AND status='queued'",
                        (stamp, job_id),
                    )

    def maintenance_held(self) -> bool:
        with self.connection() as db:
            if (
                db is None
                or not db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='manager_automation_controller_runtime'"
                ).fetchone()
            ):
                return False
            row = db.execute(
                "SELECT hold_enabled FROM manager_automation_controller_runtime WHERE singleton=1"
            ).fetchone()
            return bool(row and row[0])

    def mark_dispatch(
        self, dispatch_id: str, status: str, *, turn_id: str | None = None, error: str | None = None
    ) -> None:
        if status not in {"running", "completed", "failed", "unknown"}:
            raise RoleError("role_dispatch_status_invalid")
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM role_dispatches WHERE id=?", (dispatch_id,)).fetchone()
            if row is None:
                raise RoleError("role_dispatch_missing")
            db.execute(
                "UPDATE role_dispatches SET status=?,turn_id=COALESCE(?,turn_id),error=?,updated_at=? WHERE id=?",
                (status, turn_id, error, now(), dispatch_id),
            )
            if row["item_id"]:
                item = self._item(db, row["item_id"])
                self._update_item(db, item, status, item["data"])
            if status in {"completed", "failed"}:
                state = self._state(db, row["role"])
                next_state = "waiting" if state["state"] == "waiting" else "idle" if state["duty_active"] else "off"
                db.execute(
                    "UPDATE role_state SET active_dispatch_id=NULL,state=?,last_error=?,updated_at=? WHERE role=? AND active_dispatch_id=?",
                    (next_state, error, now(), row["role"], dispatch_id),
                )
            elif status == "unknown":
                db.execute(
                    "UPDATE role_state SET state='attention',last_error=?,updated_at=? WHERE role=?",
                    (error or "role_launch_unknown", now(), row["role"]),
                )
            elif row["status"] == "unknown":
                db.execute(
                    "UPDATE role_state SET state='running',last_error=NULL,updated_at=? WHERE role=? AND state='attention' AND last_error='role_launch_unknown'",
                    (now(), row["role"]),
                )
        self.export_views()

    def runtime_update(self, role: str, **fields: Any) -> None:
        allowed = {"state", "next_wake_at", "last_error", "goal_applied", "goal_owned"}
        if not fields or set(fields) - allowed:
            raise RoleError("role_runtime_fields_invalid")
        with self.connection(write=True) as db:
            names = ",".join(f"{key}=?" for key in fields)
            db.execute(
                f"UPDATE role_state SET {names},updated_at=? WHERE role=?", (*fields.values(), now(), role_name(role))
            )

    def export_views(self) -> None:
        """Readable projections, never the authority for running work or permissions."""
        for role in ROLES:
            state = self.role_state(role)
            body = f"# {role} — состояние дежурства\n\nРежим: {state['desired_state']}\nСостояние: {state['state']}\n"
            body += f"Следующая проверка: {state['next_wake_at'] or 'не назначена'}\n\n{state['context']}\n"
            notes = self.read("notes", role, limit=100)["items"]
            note_text = f"# {role} — наблюдения, идеи и опыт\n\n" + "\n".join(
                f"- {item['created_at']} [{item['kind']}]: {item['data']['text']} ({item['data']['source_ref']})"
                for item in notes
            )
            directory = self.views_root / role
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            for name, content in (
                ("current-state.md" if role == "M1" else "duty-state.md", body),
                ("notes.md", note_text),
            ):
                target = directory / name
                if target.is_symlink():
                    raise RoleError("role_view_invalid")
                fd, temporary = tempfile.mkstemp(prefix=".role-", dir=directory)
                try:
                    with os.fdopen(fd, "w") as stream:
                        stream.write(content)
                    os.replace(temporary, target)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)


def submit_owner_event(event_id: str, **kwargs: Any) -> dict[str, Any]:
    from .automation_control import AutomationControlClient

    response = AutomationControlClient(actor={"kind": "system", "id": "owner-wake", "is_admin": True}).request(
        "role_owner_submit", {"event_id": event_id, **kwargs}
    )
    if response.get("ok") is not True or not isinstance(response.get("data"), dict):
        raise RoleError("role_owner_route_unavailable")
    return response["data"]


def read_owner_receipt(event_id: str) -> dict[str, Any] | None:
    return RoleStore().owner_event_receipt(event_id)


def read_role_state(role: str = "M1") -> dict[str, Any]:
    return RoleStore().role_state(role)
