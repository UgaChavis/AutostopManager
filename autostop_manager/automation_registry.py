"""Durable, privacy-safe state for the Manager automation controller.

The controller is the only writer.  CRM, Telegram and Codex use the local
control socket and never update these tables directly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import get_automation_db_path


AUTOMATION_SCHEMA_VERSION = 6
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 24 * 60
MAX_JOBS = 100
IDEMPOTENCY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z")
JOB_ID_PATTERN = re.compile(r"auto_[0-9a-f]{24}\Z")
CURSOR_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
TIMEZONE_PATTERN = re.compile(r"[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*\Z")
CLOCK_PATTERN = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])\Z")
DISPLAY_NAME_FORBIDDEN = frozenset("`$;|&<>\\{}\r\n\t")
DISPLAY_NAME_URL = re.compile(r"(?:[a-z][a-z0-9+.-]*://|www\.|\b[a-z0-9-]+\.(?:com|net|org|ru)\b)", re.I)


class AutomationError(RuntimeError):
    """A fixed technical error safe to return across the local boundary."""

    def __init__(self, code: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = dict(details or {})


@dataclass(frozen=True)
class AutomationTemplate:
    template_id: str
    name: str
    singleton: bool
    default_every_minutes: int
    minimum_every_minutes: int
    maximum_every_minutes: int
    description: str
    notification_channel: str

    def public(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "name": self.name,
            "description": self.description,
            "singleton": self.singleton,
            "default_enabled": False,
            "default_schedule": {
                "kind": "interval",
                "every_minutes": self.default_every_minutes,
                "timezone": "Asia/Krasnoyarsk",
                "active_window": "24/7",
            },
            "limits": {
                "minimum_every_minutes": self.minimum_every_minutes,
                "maximum_every_minutes": self.maximum_every_minutes,
            },
            "notification_channel": self.notification_channel,
        }


AUTOMATION_TEMPLATES: dict[str, AutomationTemplate] = {
    "crm_digest_v1": AutomationTemplate(
        template_id="crm_digest_v1",
        name="CRM: краткий дайджест изменений",
        singleton=True,
        default_every_minutes=20,
        minimum_every_minutes=MIN_INTERVAL_MINUTES,
        maximum_every_minutes=MAX_INTERVAL_MINUTES,
        description="Сжатая сводка новых технических изменений CRM для владельца.",
        notification_channel="owner_telegram",
    )
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_timezone(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or TIMEZONE_PATTERN.fullmatch(value) is None:
        raise AutomationError("automation_timezone_invalid")
    try:
        return ZoneInfo(value).key
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise AutomationError("automation_timezone_invalid") from exc


def normalize_active_window(value: Any) -> str | dict[str, str]:
    if value == "24/7":
        return "24/7"
    if not isinstance(value, Mapping) or set(value) != {"start", "end"}:
        raise AutomationError("automation_active_window_invalid")
    start = value.get("start")
    end = value.get("end")
    if (
        not isinstance(start, str)
        or not isinstance(end, str)
        or CLOCK_PATTERN.fullmatch(start) is None
        or CLOCK_PATTERN.fullmatch(end) is None
        or start == end
    ):
        raise AutomationError("automation_active_window_invalid")
    return {"start": start, "end": end}


def _clock_seconds(value: str) -> int:
    hours, minutes = value.split(":", 1)
    return int(hours) * 3600 + int(minutes) * 60


def is_in_active_window(at: datetime, *, timezone: str, active_window: Any) -> bool:
    normalized_window = normalize_active_window(active_window)
    if normalized_window == "24/7":
        return True
    local = at.astimezone(ZoneInfo(normalize_timezone(timezone)))
    current = local.hour * 3600 + local.minute * 60 + local.second
    start = _clock_seconds(normalized_window["start"])
    end = _clock_seconds(normalized_window["end"])
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _local_boundary(local: datetime, *, start_seconds: int, add_days: int = 0) -> datetime:
    boundary_date = local.date() + timedelta(days=add_days)
    candidate = datetime(
        boundary_date.year,
        boundary_date.month,
        boundary_date.day,
        start_seconds // 3600,
        (start_seconds % 3600) // 60,
        tzinfo=local.tzinfo,
        fold=0,
    )
    # Normalize nonexistent local times (DST spring-forward) through UTC.
    return candidate.astimezone(UTC).astimezone(local.tzinfo)


def next_active_time(at: datetime, *, timezone: str, active_window: Any) -> datetime:
    normalized_window = normalize_active_window(active_window)
    candidate = at.astimezone(UTC)
    if normalized_window == "24/7":
        return candidate
    zone = ZoneInfo(normalize_timezone(timezone))
    local = candidate.astimezone(zone)
    if is_in_active_window(candidate, timezone=timezone, active_window=normalized_window):
        return candidate
    current = local.hour * 3600 + local.minute * 60 + local.second
    start = _clock_seconds(normalized_window["start"])
    end = _clock_seconds(normalized_window["end"])
    add_days = 1 if start < end and current >= end else 0
    boundary = _local_boundary(local, start_seconds=start, add_days=add_days).astimezone(UTC)
    if boundary <= candidate:
        boundary = _local_boundary(local, start_seconds=start, add_days=add_days + 1).astimezone(UTC)
    return boundary


def next_scheduled_time(
    base: datetime,
    *,
    delay_seconds: int,
    timezone: str,
    active_window: Any,
) -> datetime:
    if delay_seconds < 0:
        raise AutomationError("automation_next_delay_invalid")
    return next_active_time(
        base.astimezone(UTC) + timedelta(seconds=delay_seconds),
        timezone=timezone,
        active_window=active_window,
    )


def technical_actor_hash(kind: str, actor_id: str) -> str:
    return hashlib.sha256(f"{kind}:{actor_id}".encode()).hexdigest()


def _decode_json(value: str | None, fallback: Any) -> Any:
    try:
        decoded = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback
    return decoded


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS manager_automation_schema (
        component TEXT PRIMARY KEY,
        version INTEGER NOT NULL,
        upgraded_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS manager_automations (
        job_id TEXT PRIMARY KEY,
        template_id TEXT NOT NULL,
        name TEXT NOT NULL,
        singleton_key TEXT,
        desired_state TEXT NOT NULL CHECK(desired_state IN ('on', 'off')),
        schedule_kind TEXT NOT NULL CHECK(schedule_kind = 'interval'),
        every_seconds INTEGER NOT NULL CHECK(every_seconds >= 300 AND every_seconds <= 86400),
        timezone TEXT NOT NULL CHECK(timezone IN ('UTC', 'Asia/Krasnoyarsk')),
        schedule_timezone TEXT NOT NULL DEFAULT 'Asia/Krasnoyarsk',
        active_window_json TEXT NOT NULL DEFAULT 'null',
        revision INTEGER NOT NULL CHECK(revision >= 1),
        created_actor_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        archived_at TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_manager_automations_singleton
    ON manager_automations(singleton_key) WHERE singleton_key IS NOT NULL AND archived_at IS NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_manager_automations_state
    ON manager_automations(desired_state, archived_at, updated_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS manager_automation_runtime (
        job_id TEXT PRIMARY KEY,
        actual_state TEXT NOT NULL,
        next_run_at TEXT,
        last_run_at TEXT,
        last_success_at TEXT,
        heartbeat_at TEXT,
        lease_owner_hash TEXT,
        lease_until TEXT,
        fencing_token INTEGER NOT NULL DEFAULT 0,
        error_code TEXT,
        incident_id TEXT,
        incident_code TEXT,
        applied_revision INTEGER NOT NULL DEFAULT 1,
        applied_at TEXT,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(job_id) REFERENCES manager_automations(job_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_manager_automation_runtime_due
    ON manager_automation_runtime(next_run_at, lease_until)
    """,
    """
    CREATE TABLE IF NOT EXISTS manager_automation_commands (
        command_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        operation TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        actor_hash TEXT NOT NULL,
        response_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(source, idempotency_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS manager_automation_runs (
        run_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        trigger_kind TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
        command_id TEXT,
        scheduled_for TEXT NOT NULL,
        requested_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        lease_owner_hash TEXT,
        fencing_token INTEGER,
        result_code TEXT,
        coalesced_count INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(job_id) REFERENCES manager_automations(job_id) ON DELETE CASCADE,
        FOREIGN KEY(command_id) REFERENCES manager_automation_commands(command_id) ON DELETE SET NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_manager_automation_runs_queue
    ON manager_automation_runs(status, scheduled_for, requested_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS manager_automation_cursors (
        job_id TEXT NOT NULL,
        cursor_name TEXT NOT NULL,
        cursor_value TEXT,
        revision INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(job_id, cursor_name),
        FOREIGN KEY(job_id) REFERENCES manager_automations(job_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS manager_automation_outbox (
        outbox_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        run_id TEXT,
        kind TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('queued', 'sending', 'sent', 'failed', 'cancelled')),
        idempotency_key TEXT NOT NULL UNIQUE,
        payload_json TEXT NOT NULL DEFAULT '{}',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT NOT NULL,
        lease_owner_hash TEXT,
        lease_until TEXT,
        fencing_token INTEGER NOT NULL DEFAULT 0,
        result_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(job_id) REFERENCES manager_automations(job_id) ON DELETE CASCADE,
        FOREIGN KEY(run_id) REFERENCES manager_automation_runs(run_id) ON DELETE SET NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_manager_automation_outbox_queue
    ON manager_automation_outbox(status, next_attempt_at, lease_until)
    """,
    """
    CREATE TABLE IF NOT EXISTS manager_automation_controller_runtime (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        state TEXT NOT NULL,
        heartbeat_at TEXT,
        owner_hash TEXT,
        started_at TEXT,
        hold_enabled INTEGER NOT NULL DEFAULT 0,
        hold_reason TEXT,
        hold_owner_hash TEXT,
        hold_attempt_hash TEXT,
        hold_revision INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS manager_automation_system_timers (
        timer_id TEXT PRIMARY KEY,
        unit_name TEXT NOT NULL UNIQUE,
        control_mode TEXT NOT NULL CHECK(control_mode IN ('read_only', 'managed')),
        adopted_state_json TEXT NOT NULL,
        adopted_at TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1
    )
    """,
)


@dataclass(frozen=True)
class AutomationStore:
    db_path: Path | None = None

    @property
    def path(self) -> Path:
        return self.db_path or get_automation_db_path()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        path = self.path
        if not path.is_absolute() or ".." in path.parts:
            raise AutomationError("automation_db_path_invalid")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_info = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or stat.S_ISLNK(parent_info.st_mode)
            or parent_info.st_uid != os.geteuid()
            or stat.S_IMODE(parent_info.st_mode) & 0o077
        ):
            raise AutomationError("automation_db_directory_invalid")
        try:
            path_info = path.lstat()
        except FileNotFoundError:
            path_info = None
        if path_info is not None and (
            not stat.S_ISREG(path_info.st_mode) or stat.S_ISLNK(path_info.st_mode) or path_info.st_uid != os.geteuid()
        ):
            raise AutomationError("automation_db_file_invalid")
        connection = sqlite3.connect(path, timeout=5)
        os.chmod(path, 0o600, follow_symlinks=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        now = isoformat(utc_now())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(SCHEMA_STATEMENTS[0])
            row = connection.execute(
                "SELECT version FROM manager_automation_schema WHERE component = 'automation_center'"
            ).fetchone()
            if row and int(row["version"]) > AUTOMATION_SCHEMA_VERSION:
                raise AutomationError("automation_schema_newer_than_runtime")
            for statement in SCHEMA_STATEMENTS[1:]:
                connection.execute(statement)
            automation_columns = {
                str(item["name"]) for item in connection.execute("PRAGMA table_info(manager_automations)")
            }
            if "schedule_timezone" not in automation_columns:
                connection.execute(
                    "ALTER TABLE manager_automations "
                    "ADD COLUMN schedule_timezone TEXT NOT NULL DEFAULT 'Asia/Krasnoyarsk'"
                )
                connection.execute("UPDATE manager_automations SET schedule_timezone = timezone")
            runtime_columns = {
                str(item["name"]) for item in connection.execute("PRAGMA table_info(manager_automation_runtime)")
            }
            for column in ("incident_id", "incident_code"):
                if column not in runtime_columns:
                    connection.execute(f"ALTER TABLE manager_automation_runtime ADD COLUMN {column} TEXT")
            if "applied_revision" not in runtime_columns:
                connection.execute(
                    "ALTER TABLE manager_automation_runtime ADD COLUMN applied_revision INTEGER NOT NULL DEFAULT 1"
                )
            if "applied_at" not in runtime_columns:
                connection.execute("ALTER TABLE manager_automation_runtime ADD COLUMN applied_at TEXT")
                connection.execute(
                    "UPDATE manager_automation_runtime SET applied_at = updated_at WHERE applied_at IS NULL"
                )
            controller_columns = {
                str(item["name"])
                for item in connection.execute("PRAGMA table_info(manager_automation_controller_runtime)")
            }
            for column, definition in {
                "hold_enabled": "INTEGER NOT NULL DEFAULT 0",
                "hold_reason": "TEXT",
                "hold_owner_hash": "TEXT",
                "hold_attempt_hash": "TEXT",
                "hold_revision": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if column not in controller_columns:
                    connection.execute(
                        f"ALTER TABLE manager_automation_controller_runtime ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                """
                INSERT INTO manager_automation_schema(component, version, upgraded_at)
                VALUES('automation_center', ?, ?)
                ON CONFLICT(component) DO UPDATE SET version = excluded.version, upgraded_at = excluded.upgraded_at
                """,
                (AUTOMATION_SCHEMA_VERSION, now),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO manager_automation_controller_runtime(
                    singleton, state, heartbeat_at, owner_hash, started_at, updated_at
                ) VALUES(1, 'stopped', NULL, NULL, NULL, ?)
                """,
                (now,),
            )

    def schema_version(self) -> int:
        self.initialize()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT version FROM manager_automation_schema WHERE component = 'automation_center'"
            ).fetchone()
        return int(row["version"]) if row else 0

    def execute_command(
        self,
        *,
        source: str,
        idempotency_key: str,
        operation: str,
        request_hash: str,
        actor_hash: str,
        mutation: Callable[[sqlite3.Connection, str], dict[str, Any]],
    ) -> dict[str, Any]:
        if IDEMPOTENCY_PATTERN.fullmatch(idempotency_key) is None:
            raise AutomationError("idempotency_key_invalid")
        self.initialize()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT operation, request_hash, response_json
                FROM manager_automation_commands
                WHERE source = ? AND idempotency_key = ?
                """,
                (source, idempotency_key),
            ).fetchone()
            if existing:
                if existing["operation"] != operation or existing["request_hash"] != request_hash:
                    raise AutomationError("idempotency_key_conflict")
                replay = _decode_json(existing["response_json"], None)
                if not isinstance(replay, dict):
                    raise AutomationError("idempotency_result_invalid")
                return {**replay, "idempotent_replay": True}

            command_id = f"cmd_{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO manager_automation_commands(
                    command_id, source, idempotency_key, operation, request_hash,
                    actor_hash, response_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command_id,
                    source,
                    idempotency_key,
                    operation,
                    request_hash,
                    actor_hash,
                    "{}",
                    isoformat(utc_now()),
                ),
            )
            result = mutation(connection, command_id)
            connection.execute(
                "UPDATE manager_automation_commands SET response_json = ? WHERE command_id = ?",
                (canonical_json(result), command_id),
            )
            return result

    def seed_defaults(self) -> dict[str, Any]:
        """Create the approved singleton once, without changing any existing row."""

        template = AUTOMATION_TEMPLATES["crm_digest_v1"]
        actor_hash = technical_actor_hash("system", "automation-seed")

        def mutation(connection: sqlite3.Connection, _command_id: str) -> dict[str, Any]:
            existing = connection.execute(
                """
                SELECT job_id FROM manager_automations
                WHERE template_id = ? ORDER BY created_at LIMIT 1
                """,
                (template.template_id,),
            ).fetchone()
            if existing is not None:
                return {
                    "seeded": False,
                    "job": self._job_status(connection, str(existing["job_id"])),
                }
            created = self.create_from_template(
                connection,
                template_id=template.template_id,
                payload={"template_id": template.template_id},
                actor_hash=actor_hash,
            )
            return {"seeded": True, "job": created["job"]}

        return self.execute_command(
            source="system",
            idempotency_key="startup-seed-crm-digest-v1",
            operation="seed_default",
            request_hash=technical_actor_hash("automation-seed", "crm_digest_v1:v1"),
            actor_hash=actor_hash,
            mutation=mutation,
        )

    @staticmethod
    def _require_job(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        if JOB_ID_PATTERN.fullmatch(job_id) is None:
            raise AutomationError("automation_job_id_invalid")
        row = connection.execute(
            "SELECT * FROM manager_automations WHERE job_id = ? AND archived_at IS NULL",
            (job_id,),
        ).fetchone()
        if not row:
            raise AutomationError("automation_job_not_found")
        return row

    @staticmethod
    def _require_revision(row: sqlite3.Row, expected_revision: int | None) -> None:
        if type(expected_revision) is not int or expected_revision < 1:
            raise AutomationError("expected_revision_required")
        current = int(row["revision"])
        if expected_revision != current:
            raise AutomationError(
                "automation_revision_conflict",
                details={"expected_revision": expected_revision, "current_revision": current},
            )

    @staticmethod
    def _display_name(value: Any) -> str:
        if (
            not isinstance(value, str)
            or value != value.strip()
            or unicodedata.normalize("NFC", value) != value
            or not 1 <= len(value) <= 96
            or len(value.encode("utf-8")) > 256
            or any(
                character in DISPLAY_NAME_FORBIDDEN or unicodedata.category(character).startswith("C")
                for character in value
            )
            or DISPLAY_NAME_URL.search(value) is not None
        ):
            raise AutomationError("automation_display_name_invalid")
        return value

    @staticmethod
    def _schedule(
        template: AutomationTemplate,
        payload: Mapping[str, Any],
        *,
        current: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if set(payload).difference({"kind", "every_minutes", "timezone", "active_window"}):
            raise AutomationError("automation_schedule_invalid")
        baseline = current or {
            "kind": "interval",
            "every_minutes": template.default_every_minutes,
            "timezone": "Asia/Krasnoyarsk",
            "active_window": "24/7",
        }
        if payload.get("kind", baseline["kind"]) != "interval":
            raise AutomationError("automation_schedule_invalid")
        raw_minutes = payload.get("every_minutes", baseline["every_minutes"])
        if type(raw_minutes) is not int:
            raise AutomationError("automation_schedule_invalid")
        if not template.minimum_every_minutes <= raw_minutes <= template.maximum_every_minutes:
            raise AutomationError("automation_schedule_out_of_range")
        timezone = normalize_timezone(payload.get("timezone", baseline["timezone"]))
        active_window = normalize_active_window(payload.get("active_window", baseline["active_window"]))
        return {
            "kind": "interval",
            "every_minutes": raw_minutes,
            "timezone": timezone,
            "active_window": active_window,
        }

    @staticmethod
    def _row_schedule(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "kind": str(row["schedule_kind"]),
            "every_minutes": int(row["every_seconds"]) // 60,
            "timezone": str(row["schedule_timezone"] or row["timezone"]),
            "active_window": _decode_json(row["active_window_json"], "24/7"),
        }

    def create_from_template(
        self,
        connection: sqlite3.Connection,
        *,
        template_id: str,
        payload: Mapping[str, Any],
        actor_hash: str,
    ) -> dict[str, Any]:
        template = AUTOMATION_TEMPLATES.get(template_id)
        if template is None:
            raise AutomationError("automation_template_not_allowed")
        if set(payload).difference({"template_id", "name", "enabled", "schedule"}):
            raise AutomationError("automation_template_payload_invalid")
        if payload.get("enabled") not in (None, False):
            raise AutomationError("automation_template_must_start_disabled")
        raw_name = self._display_name(payload.get("name", template.name))
        schedule_payload = payload.get("schedule", {})
        if not isinstance(schedule_payload, Mapping):
            raise AutomationError("automation_schedule_invalid")
        schedule = self._schedule(template, schedule_payload)
        if template.singleton:
            existing = connection.execute(
                "SELECT job_id, revision FROM manager_automations WHERE singleton_key = ? AND archived_at IS NULL",
                (template.template_id,),
            ).fetchone()
            if existing:
                raise AutomationError(
                    "automation_singleton_exists",
                    details={"job_id": existing["job_id"], "revision": int(existing["revision"])},
                )
        job_id = f"auto_{uuid4().hex[:24]}"
        now = isoformat(utc_now())
        connection.execute(
            """
            INSERT INTO manager_automations(
                job_id, template_id, name, singleton_key, desired_state,
                schedule_kind, every_seconds, timezone, schedule_timezone, active_window_json,
                revision, created_actor_hash, created_at, updated_at, archived_at
            ) VALUES(?, ?, ?, ?, 'off', 'interval', ?, 'Asia/Krasnoyarsk', ?, ?, 1, ?, ?, ?, NULL)
            """,
            (
                job_id,
                template.template_id,
                raw_name,
                template.template_id if template.singleton else None,
                schedule["every_minutes"] * 60,
                schedule["timezone"],
                canonical_json(schedule["active_window"]),
                actor_hash,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO manager_automation_runtime(
                job_id, actual_state, next_run_at, fencing_token,
                applied_revision, applied_at, updated_at
            ) VALUES(?, 'disabled', NULL, 0, 1, ?, ?)
            """,
            (job_id, now, now),
        )
        return {"created": True, "job": self._job_status(connection, job_id)}

    def set_enabled(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        enabled: bool,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise AutomationError("automation_enabled_invalid")
        row = self._require_job(connection, job_id)
        self._require_revision(row, expected_revision)
        desired = "on" if enabled else "off"
        if row["desired_state"] == desired:
            return {"changed": False, "job": self._job_status(connection, job_id)}
        now_dt = utc_now()
        now = isoformat(now_dt)
        new_revision = int(row["revision"]) + 1
        connection.execute(
            "UPDATE manager_automations SET desired_state = ?, revision = ?, updated_at = ? WHERE job_id = ?",
            (desired, new_revision, now, job_id),
        )
        if not enabled:
            runtime = connection.execute(
                "SELECT lease_until FROM manager_automation_runtime WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            lease_active = bool(
                runtime and parse_time(runtime["lease_until"]) and parse_time(runtime["lease_until"]) > now_dt
            )
            connection.execute(
                """
                UPDATE manager_automation_runtime
                SET actual_state = CASE WHEN ? THEN 'stopping' ELSE actual_state END,
                    next_run_at = NULL, updated_at = ?
                WHERE job_id = ?
                """,
                (int(lease_active), now, job_id),
            )
            connection.execute(
                """
                UPDATE manager_automation_runs
                SET status = 'cancelled', finished_at = ?, result_code = 'automation_disabled'
                WHERE job_id = ? AND status = 'queued'
                """,
                (now, job_id),
            )
        return {"changed": True, "job": self._job_status(connection, job_id)}

    def set_schedule(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        schedule_payload: Mapping[str, Any],
        expected_revision: int | None,
    ) -> dict[str, Any]:
        row = self._require_job(connection, job_id)
        self._require_revision(row, expected_revision)
        template = AUTOMATION_TEMPLATES.get(str(row["template_id"]))
        if template is None:
            raise AutomationError("automation_template_not_allowed")
        current = self._row_schedule(row)
        schedule = self._schedule(template, schedule_payload, current=current)
        new_seconds = int(schedule["every_minutes"]) * 60
        if schedule == current:
            return {"changed": False, "job": self._job_status(connection, job_id)}
        now = isoformat(utc_now())
        connection.execute(
            """
            UPDATE manager_automations
            SET every_seconds = ?, schedule_timezone = ?, active_window_json = ?,
                revision = revision + 1, updated_at = ?
            WHERE job_id = ?
            """,
            (
                new_seconds,
                schedule["timezone"],
                canonical_json(schedule["active_window"]),
                now,
                job_id,
            ),
        )
        return {"changed": True, "job": self._job_status(connection, job_id)}

    def enqueue_run(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        command_id: str,
        expected_revision: int | None,
        trigger_kind: str = "manual",
    ) -> dict[str, Any]:
        row = self._require_job(connection, job_id)
        if expected_revision is not None:
            self._require_revision(row, expected_revision)
        if row["desired_state"] != "on":
            raise AutomationError("automation_not_enabled")
        pending = connection.execute(
            """
            SELECT run_id, coalesced_count FROM manager_automation_runs
            WHERE job_id = ? AND status IN ('queued', 'running')
            ORDER BY requested_at LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if pending:
            connection.execute(
                "UPDATE manager_automation_runs SET coalesced_count = coalesced_count + 1 WHERE run_id = ?",
                (pending["run_id"],),
            )
            return {"queued": False, "coalesced": True, "run_id": pending["run_id"]}
        now = isoformat(utc_now())
        run_id = f"run_{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO manager_automation_runs(
                run_id, job_id, trigger_kind, status, command_id,
                scheduled_for, requested_at
            ) VALUES(?, ?, ?, 'queued', ?, ?, ?)
            """,
            (run_id, job_id, trigger_kind, command_id, now, now),
        )
        connection.execute(
            "UPDATE manager_automation_runtime SET actual_state = 'queued', updated_at = ? WHERE job_id = ?",
            (now, job_id),
        )
        return {"queued": True, "coalesced": False, "run_id": run_id}

    def enqueue_notification_test(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        command_id: str,
        expected_revision: int | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        row = self._require_job(connection, job_id)
        if expected_revision is not None:
            self._require_revision(row, expected_revision)
        now = isoformat(utc_now())
        outbox_id = f"out_{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO manager_automation_outbox(
                outbox_id, job_id, run_id, kind, status, idempotency_key,
                payload_json, next_attempt_at, created_at, updated_at
            ) VALUES(?, ?, NULL, 'notification_test', 'queued', ?, ?, ?, ?, ?)
            """,
            (
                outbox_id,
                job_id,
                f"notification-test:{idempotency_key}",
                canonical_json({"template": "automation_notification_test_v1", "command_id": command_id}),
                now,
                now,
                now,
            ),
        )
        return {
            "queued": True,
            "outbox_id": outbox_id,
            "delivery_state": "awaiting_transport",
            "external_message_sent": False,
        }

    @staticmethod
    def _enqueue_incident_notification(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        run_id: str | None,
        template_id: str,
        transition: str,
        incident_id: str,
        error_code: str,
        now: str,
    ) -> None:
        if transition not in {"error", "recovery"}:
            raise AutomationError("automation_incident_transition_invalid")
        outbox_id = f"out_{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO manager_automation_outbox(
                outbox_id, job_id, run_id, kind, status, idempotency_key,
                payload_json, next_attempt_at, created_at, updated_at
            ) VALUES(?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
            """,
            (
                outbox_id,
                job_id,
                run_id,
                f"job_{transition}_alert",
                f"automation-incident:{incident_id}:{transition}",
                canonical_json(
                    {
                        "template": "automation_job_incident_v1",
                        "template_id": template_id,
                        "transition": transition,
                        "incident_id": incident_id,
                        "error_code": error_code,
                    }
                ),
                now,
                now,
                now,
            ),
        )

    def archive(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        row = self._require_job(connection, job_id)
        self._require_revision(row, expected_revision)
        if row["desired_state"] != "off":
            raise AutomationError("automation_disable_before_archive")
        runtime = connection.execute(
            "SELECT lease_until FROM manager_automation_runtime WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        lease_until = parse_time(runtime["lease_until"]) if runtime else None
        if lease_until and lease_until > utc_now():
            raise AutomationError("automation_lease_active")
        pending_delivery = connection.execute(
            """
            SELECT 1 FROM manager_automation_outbox
            WHERE job_id = ? AND status IN ('queued', 'failed', 'sending') LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if pending_delivery is not None:
            raise AutomationError("automation_delivery_pending")
        now = isoformat(utc_now())
        connection.execute(
            """
            UPDATE manager_automations
            SET archived_at = ?, revision = revision + 1, updated_at = ?
            WHERE job_id = ?
            """,
            (now, now, job_id),
        )
        connection.execute(
            """
            UPDATE manager_automation_runtime
            SET actual_state = 'archived', next_run_at = NULL,
                applied_revision = ?, applied_at = ?, updated_at = ?
            WHERE job_id = ?
            """,
            (int(row["revision"]) + 1, now, now, job_id),
        )
        connection.execute(
            """
            UPDATE manager_automation_runs
            SET status = 'cancelled', finished_at = ?, result_code = 'automation_archived'
            WHERE job_id = ? AND status = 'queued'
            """,
            (now, job_id),
        )
        return {"archived": True, "job_id": job_id, "revision": int(row["revision"]) + 1}

    def _job_status(self, connection: sqlite3.Connection, job_id: str) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT a.*, r.actual_state, r.next_run_at, r.last_run_at,
                   r.last_success_at, r.heartbeat_at, r.lease_until,
                   r.fencing_token, r.error_code, r.applied_revision, r.applied_at
            FROM manager_automations a
            JOIN manager_automation_runtime r ON r.job_id = a.job_id
            WHERE a.job_id = ?
            """,
            (job_id,),
        ).fetchone()
        if not row:
            raise AutomationError("automation_job_not_found")
        revision = int(row["revision"])
        applied_revision = int(row["applied_revision"])
        desired_updated_at = parse_time(row["updated_at"])
        lag_seconds = 0
        if revision != applied_revision and desired_updated_at is not None:
            lag_seconds = max(0, int((utc_now() - desired_updated_at).total_seconds()))
        return {
            "job_id": row["job_id"],
            "template_id": row["template_id"],
            "name": row["name"],
            "desired_state": row["desired_state"],
            "actual_state": row["actual_state"],
            "revision": revision,
            "applied_revision": applied_revision,
            "applied_at": row["applied_at"],
            "reconcile_state": "in_sync" if revision == applied_revision else "pending",
            "lag_seconds": lag_seconds,
            "schedule": self._row_schedule(row),
            "last_run_at": row["last_run_at"],
            "last_success_at": row["last_success_at"],
            "next_run_at": row["next_run_at"],
            "heartbeat_at": row["heartbeat_at"],
            "lease_until": row["lease_until"],
            "error_code": row["error_code"],
            "fencing_token": int(row["fencing_token"]),
            "archived_at": row["archived_at"],
        }

    def reconcile_next_job(self) -> dict[str, Any] | None:
        """Apply one desired registry revision to scheduler runtime state.

        Desired writes and runtime application are intentionally separate so a
        caller can observe pending revisions. The persistent daemon performs
        this bounded reconciliation before it claims work.
        """

        self.initialize()
        now_dt = utc_now()
        now = isoformat(now_dt)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            controller = connection.execute(
                "SELECT hold_enabled FROM manager_automation_controller_runtime WHERE singleton = 1"
            ).fetchone()
            if controller is not None and bool(controller["hold_enabled"]):
                return None
            row = connection.execute(
                """
                SELECT a.*, r.actual_state, r.lease_until, r.incident_id
                FROM manager_automations a
                JOIN manager_automation_runtime r ON r.job_id = a.job_id
                WHERE a.archived_at IS NULL AND a.revision != r.applied_revision
                ORDER BY a.updated_at, a.job_id LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            lease_until = parse_time(row["lease_until"])
            if lease_until is not None and lease_until > now_dt:
                if row["desired_state"] == "off":
                    connection.execute(
                        """
                        UPDATE manager_automation_runtime
                        SET actual_state = 'stopping', next_run_at = NULL, updated_at = ?
                        WHERE job_id = ?
                        """,
                        (now, row["job_id"]),
                    )
                return {"applied": False, "job": self._job_status(connection, str(row["job_id"]))}

            if row["desired_state"] == "on":
                next_run = isoformat(
                    next_scheduled_time(
                        now_dt,
                        delay_seconds=int(row["every_seconds"]),
                        timezone=str(row["schedule_timezone"] or row["timezone"]),
                        active_window=_decode_json(row["active_window_json"], "24/7"),
                    )
                )
                actual_state = "error" if row["incident_id"] else "idle"
            else:
                next_run = None
                actual_state = "disabled"
                connection.execute(
                    """
                    UPDATE manager_automation_runs
                    SET status = 'cancelled', finished_at = ?, result_code = 'automation_disabled'
                    WHERE job_id = ? AND status = 'queued'
                    """,
                    (now, row["job_id"]),
                )
            connection.execute(
                """
                UPDATE manager_automation_runtime
                SET actual_state = ?, next_run_at = ?,
                    applied_revision = ?, applied_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (actual_state, next_run, int(row["revision"]), now, now, row["job_id"]),
            )
            return {"applied": True, "job": self._job_status(connection, str(row["job_id"]))}

    def status(self, *, job_id: str | None = None, include_archived: bool = False) -> dict[str, Any]:
        self.initialize()
        now = utc_now()
        with self.connect() as connection:
            if job_id:
                jobs = [self._job_status(connection, job_id)]
            else:
                archived_clause = "" if include_archived else "WHERE archived_at IS NULL"
                identifiers = connection.execute(
                    f"SELECT job_id FROM manager_automations {archived_clause} ORDER BY created_at LIMIT ?",
                    (MAX_JOBS,),
                ).fetchall()
                jobs = [self._job_status(connection, str(row["job_id"])) for row in identifiers]
            controller = connection.execute(
                """
                SELECT state, heartbeat_at, started_at, hold_enabled, hold_reason,
                       hold_attempt_hash, hold_revision, updated_at
                FROM manager_automation_controller_runtime WHERE singleton = 1
                """
            ).fetchone()
            timers = [
                self._system_timer_status(row)
                for row in connection.execute(
                    "SELECT * FROM manager_automation_system_timers ORDER BY timer_id"
                ).fetchall()
            ]
        heartbeat_at = parse_time(controller["heartbeat_at"] if controller else None)
        controller_state = str(controller["state"] if controller else "stopped")
        if controller_state == "active" and (heartbeat_at is None or now - heartbeat_at > timedelta(seconds=30)):
            controller_state = "stale"
        readiness = self.readiness_from_jobs(jobs, controller_state=controller_state)
        return {
            "generated_at": isoformat(now),
            "can_manage": True,
            "schema_version": AUTOMATION_SCHEMA_VERSION,
            "controller": {
                "state": "held" if controller and bool(controller["hold_enabled"]) else controller_state,
                "heartbeat_at": controller["heartbeat_at"] if controller else None,
                "started_at": controller["started_at"] if controller else None,
            },
            "global_hold": {
                "enabled": bool(controller["hold_enabled"]) if controller else False,
                "reason": controller["hold_reason"] if controller else None,
                "attempt_hash": controller["hold_attempt_hash"] if controller else None,
                "revision": int(controller["hold_revision"]) if controller else 0,
                "updated_at": controller["updated_at"] if controller else None,
            },
            "jobs": jobs,
            "system_timers": timers,
            "readiness": readiness,
            "templates": [template.public() for template in AUTOMATION_TEMPLATES.values()],
        }

    @staticmethod
    def readiness_from_jobs(jobs: list[dict[str, Any]], *, controller_state: str) -> dict[str, Any]:
        checks = {
            "schema": "ready",
            "controller": "ready" if controller_state == "active" else controller_state,
            "crm_digest_executor": "not_configured",
            "notification_transport": "not_configured",
        }
        runnable = all(job["template_id"] in AUTOMATION_TEMPLATES for job in jobs)
        return {
            "ready": runnable and controller_state == "active",
            "checks": checks,
            "warnings": [key for key, value in checks.items() if value != "ready"],
        }

    def readiness(self, *, job_id: str | None = None) -> dict[str, Any]:
        status = self.status(job_id=job_id)
        return status["readiness"]

    def technical_execution_state(self, *, job_id: str | None = None) -> dict[str, Any]:
        """Return compact technical metadata without cursor values or payloads."""

        self.initialize()
        with self.connect() as connection:
            parameters: tuple[Any, ...] = ()
            cursor_filter = ""
            outbox_filter = ""
            runs_filter = ""
            if job_id is not None:
                self._require_job(connection, job_id)
                cursor_filter = "WHERE job_id = ?"
                outbox_filter = "WHERE job_id = ?"
                runs_filter = "WHERE job_id = ?"
                parameters = (job_id,)
            cursor_rows = connection.execute(
                f"""
                SELECT job_id, cursor_name, revision,
                       CASE WHEN cursor_value IS NULL THEN 0 ELSE 1 END AS present
                FROM manager_automation_cursors {cursor_filter}
                ORDER BY job_id, cursor_name LIMIT 512
                """,
                parameters,
            ).fetchall()
            outbox_rows = connection.execute(
                f"""
                SELECT status, COUNT(*) AS item_count
                FROM manager_automation_outbox {outbox_filter}
                GROUP BY status ORDER BY status
                """,
                parameters,
            ).fetchall()
            run_rows = connection.execute(
                f"""
                SELECT status, COUNT(*) AS item_count
                FROM manager_automation_runs {runs_filter}
                GROUP BY status ORDER BY status
                """,
                parameters,
            ).fetchall()
            blocked_clause = "status = 'sending' AND lease_until IS NULL"
            blocked_parameters: tuple[Any, ...] = ()
            if job_id is not None:
                blocked_clause += " AND job_id = ?"
                blocked_parameters = (job_id,)
            blocked_count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM manager_automation_outbox WHERE {blocked_clause}",
                    blocked_parameters,
                ).fetchone()[0]
            )
        outbox_by_status = {str(row["status"]): int(row["item_count"]) for row in outbox_rows}
        runs_by_status = {str(row["status"]): int(row["item_count"]) for row in run_rows}
        return {
            "cursors": [
                {
                    "job_id": str(row["job_id"]),
                    "name": str(row["cursor_name"]),
                    "revision": int(row["revision"]),
                    "present": bool(row["present"]),
                }
                for row in cursor_rows
            ],
            "outbox": {
                "by_status": outbox_by_status,
                "total_count": sum(outbox_by_status.values()),
                "pending_count": sum(outbox_by_status.get(status, 0) for status in ("queued", "failed", "sending")),
                "blocked_count": blocked_count,
            },
            "runs": {
                "by_status": runs_by_status,
                "total_count": sum(runs_by_status.values()),
            },
        }

    def preview(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        target_operation = str(payload.get("target_operation") or "")
        target_payload = payload.get("target_payload", {})
        if not isinstance(target_payload, Mapping):
            raise AutomationError("automation_preview_payload_invalid")
        if target_operation == "create_from_template":
            template_id = str(target_payload.get("template_id") or "")
            template = AUTOMATION_TEMPLATES.get(template_id)
            if template is None:
                raise AutomationError("automation_template_not_allowed")
            if set(target_payload).difference({"template_id", "name", "enabled", "schedule"}):
                raise AutomationError("automation_template_payload_invalid")
            if target_payload.get("enabled") not in (None, False):
                raise AutomationError("automation_template_must_start_disabled")
            name = self._display_name(target_payload.get("name", template.name))
            schedule_payload = target_payload.get("schedule", {})
            if not isinstance(schedule_payload, Mapping):
                raise AutomationError("automation_schedule_invalid")
            schedule = self._schedule(template, schedule_payload)
            return {
                "target_operation": target_operation,
                "would_change": True,
                "proposed": {
                    "template_id": template_id,
                    "name": name,
                    "desired_state": "off",
                    "schedule": schedule,
                },
                "external_effects": [],
            }
        if target_operation in {"set_enabled", "set_schedule", "archive", "run_now", "test_notification"}:
            job_id = str(target_payload.get("job_id") or "")
            current = self.status(job_id=job_id)["jobs"][0]
            proposed: dict[str, Any] = {"job_id": job_id}
            external_effects: list[str] = []
            if target_operation == "set_enabled":
                if type(target_payload.get("enabled")) is not bool:
                    raise AutomationError("automation_enabled_invalid")
                proposed["desired_state"] = "on" if target_payload["enabled"] else "off"
            elif target_operation == "set_schedule":
                template = AUTOMATION_TEMPLATES[current["template_id"]]
                schedule_payload = target_payload.get("schedule")
                if not isinstance(schedule_payload, Mapping):
                    raise AutomationError("automation_schedule_invalid")
                proposed["schedule"] = self._schedule(
                    template,
                    schedule_payload,
                    current=current["schedule"],
                )
            elif target_operation == "archive":
                proposed["archived"] = True
            elif target_operation == "run_now":
                proposed["run_state"] = "queued"
                external_effects = ["crm_read", "owner_notification_if_executor_configured"]
            else:
                proposed["outbox_state"] = "queued"
                external_effects = ["owner_notification_if_transport_configured"]
            return {
                "target_operation": target_operation,
                "would_change": True,
                "current": current,
                "proposed": proposed,
                "external_effects": external_effects,
            }
        raise AutomationError("automation_preview_operation_invalid")

    def claim_next_run(self, *, owner: str, lease_seconds: int = 60) -> dict[str, Any] | None:
        if not 10 <= lease_seconds <= 600:
            raise AutomationError("automation_lease_invalid")
        owner_hash = hashlib.sha256(owner.encode()).hexdigest()
        now_dt = utc_now()
        now = isoformat(now_dt)
        lease_until = isoformat(now_dt + timedelta(seconds=lease_seconds))
        self.initialize()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            controller = connection.execute(
                "SELECT hold_enabled FROM manager_automation_controller_runtime WHERE singleton = 1"
            ).fetchone()
            if controller and bool(controller["hold_enabled"]):
                return None
            expired = connection.execute(
                """
                SELECT r.job_id, r.fencing_token, r.incident_id, a.template_id
                FROM manager_automation_runtime r
                JOIN manager_automations a ON a.job_id = r.job_id
                WHERE r.lease_until IS NOT NULL AND r.lease_until <= ?
                """,
                (now,),
            ).fetchall()
            for row in expired:
                connection.execute(
                    """
                    UPDATE manager_automation_runs
                    SET status = 'failed', finished_at = ?, result_code = 'automation_lease_expired'
                    WHERE job_id = ? AND status = 'running' AND fencing_token = ?
                    """,
                    (now, row["job_id"], row["fencing_token"]),
                )
                incident_id = row["incident_id"]
                if not incident_id:
                    incident_id = f"inc_{uuid4().hex}"
                    self._enqueue_incident_notification(
                        connection,
                        job_id=str(row["job_id"]),
                        run_id=None,
                        template_id=str(row["template_id"]),
                        transition="error",
                        incident_id=incident_id,
                        error_code="automation_lease_expired",
                        now=now,
                    )
                connection.execute(
                    """
                    UPDATE manager_automation_runtime
                    SET actual_state = 'error', lease_owner_hash = NULL, lease_until = NULL,
                        error_code = 'automation_lease_expired', incident_id = ?,
                        incident_code = COALESCE(incident_code, 'automation_lease_expired'),
                        updated_at = ?
                    WHERE job_id = ? AND fencing_token = ?
                    """,
                    (incident_id, now, row["job_id"], row["fencing_token"]),
                )

            queued = connection.execute(
                """
                SELECT q.run_id, q.job_id, q.trigger_kind, a.template_id,
                       a.every_seconds, a.revision
                FROM manager_automation_runs q
                JOIN manager_automations a ON a.job_id = q.job_id
                JOIN manager_automation_runtime r ON r.job_id = q.job_id
                WHERE q.status = 'queued' AND a.archived_at IS NULL
                  AND a.desired_state = 'on'
                  AND (r.lease_until IS NULL OR r.lease_until <= ?)
                ORDER BY q.requested_at LIMIT 1
                """,
                (now,),
            ).fetchone()
            if queued is None:
                due_rows = connection.execute(
                    """
                    SELECT a.job_id, a.template_id, a.every_seconds, a.revision,
                           a.timezone, a.schedule_timezone, a.active_window_json
                    FROM manager_automations a
                    JOIN manager_automation_runtime r ON r.job_id = a.job_id
                    WHERE a.desired_state = 'on' AND a.archived_at IS NULL
                      AND r.next_run_at IS NOT NULL AND r.next_run_at <= ?
                      AND (r.lease_until IS NULL OR r.lease_until <= ?)
                    ORDER BY r.next_run_at LIMIT ?
                    """,
                    (now, now, MAX_JOBS),
                ).fetchall()
                due = None
                for candidate in due_rows:
                    timezone = str(candidate["schedule_timezone"] or candidate["timezone"])
                    active_window = _decode_json(candidate["active_window_json"], "24/7")
                    if is_in_active_window(now_dt, timezone=timezone, active_window=active_window):
                        due = candidate
                        break
                    deferred_until = next_active_time(
                        now_dt,
                        timezone=timezone,
                        active_window=active_window,
                    )
                    connection.execute(
                        """
                        UPDATE manager_automation_runtime
                        SET next_run_at = ?, updated_at = ? WHERE job_id = ?
                        """,
                        (isoformat(deferred_until), now, candidate["job_id"]),
                    )
                if due is None:
                    return None
                run_id = f"run_{uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO manager_automation_runs(
                        run_id, job_id, trigger_kind, status, scheduled_for, requested_at
                    ) VALUES(?, ?, 'schedule', 'queued', ?, ?)
                    """,
                    (run_id, due["job_id"], now, now),
                )
                queued = connection.execute(
                    """
                    SELECT ? AS run_id, ? AS job_id, 'schedule' AS trigger_kind,
                           ? AS template_id, ? AS every_seconds, ? AS revision
                    """,
                    (
                        run_id,
                        due["job_id"],
                        due["template_id"],
                        due["every_seconds"],
                        due["revision"],
                    ),
                ).fetchone()

            runtime = connection.execute(
                "SELECT fencing_token FROM manager_automation_runtime WHERE job_id = ?",
                (queued["job_id"],),
            ).fetchone()
            fencing_token = int(runtime["fencing_token"]) + 1
            connection.execute(
                """
                UPDATE manager_automation_runtime
                SET actual_state = 'running', heartbeat_at = ?, lease_owner_hash = ?,
                    lease_until = ?, fencing_token = ?, last_run_at = ?, error_code = NULL,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (now, owner_hash, lease_until, fencing_token, now, now, queued["job_id"]),
            )
            connection.execute(
                """
                UPDATE manager_automation_runs
                SET status = 'running', started_at = ?, lease_owner_hash = ?, fencing_token = ?
                WHERE run_id = ? AND status = 'queued'
                """,
                (now, owner_hash, fencing_token, queued["run_id"]),
            )
            return {
                "run_id": queued["run_id"],
                "job_id": queued["job_id"],
                "template_id": queued["template_id"],
                "trigger_kind": queued["trigger_kind"],
                "claimed_revision": int(queued["revision"]),
                "fencing_token": fencing_token,
                "lease_until": lease_until,
            }

    def run_claim_is_current(
        self,
        *,
        job_id: str,
        run_id: str,
        owner: str,
        fencing_token: int,
        claimed_revision: int,
    ) -> bool:
        """Recheck desired revision, hold, lease and fence before an external effect."""

        owner_hash = hashlib.sha256(owner.encode()).hexdigest()
        now = isoformat(utc_now())
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM manager_automation_runs q
                JOIN manager_automations a ON a.job_id = q.job_id
                JOIN manager_automation_runtime r ON r.job_id = q.job_id
                JOIN manager_automation_controller_runtime c ON c.singleton = 1
                WHERE q.run_id = ? AND q.job_id = ? AND q.status = 'running'
                  AND q.lease_owner_hash = ? AND q.fencing_token = ?
                  AND r.lease_owner_hash = ? AND r.fencing_token = ? AND r.lease_until > ?
                  AND a.archived_at IS NULL AND a.desired_state = 'on' AND a.revision = ?
                  AND c.hold_enabled = 0
                """,
                (
                    run_id,
                    job_id,
                    owner_hash,
                    fencing_token,
                    owner_hash,
                    fencing_token,
                    now,
                    claimed_revision,
                ),
            ).fetchone()
        return row is not None

    def cancel_run_claim(
        self,
        *,
        job_id: str,
        run_id: str,
        owner: str,
        fencing_token: int,
        result_code: str = "automation_claim_superseded",
    ) -> bool:
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}", result_code):
            raise AutomationError("automation_result_code_invalid")
        owner_hash = hashlib.sha256(owner.encode()).hexdigest()
        now_dt = utc_now()
        now = isoformat(now_dt)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT a.desired_state, a.revision, a.every_seconds, a.timezone,
                       a.schedule_timezone, a.active_window_json, r.incident_id
                FROM manager_automations a
                JOIN manager_automation_runtime r ON r.job_id = a.job_id
                WHERE a.job_id = ? AND r.lease_owner_hash = ? AND r.fencing_token = ?
                """,
                (job_id, owner_hash, fencing_token),
            ).fetchone()
            if row is None:
                return False
            changed = connection.execute(
                """
                UPDATE manager_automation_runs
                SET status = 'cancelled', finished_at = ?, result_code = ?
                WHERE run_id = ? AND job_id = ? AND status = 'running'
                  AND lease_owner_hash = ? AND fencing_token = ?
                """,
                (now, result_code, run_id, job_id, owner_hash, fencing_token),
            ).rowcount
            if not changed:
                return False
            next_run = None
            actual_state = "disabled"
            if row["desired_state"] == "on":
                next_run = isoformat(
                    next_scheduled_time(
                        now_dt,
                        delay_seconds=int(row["every_seconds"]),
                        timezone=str(row["schedule_timezone"] or row["timezone"]),
                        active_window=_decode_json(row["active_window_json"], "24/7"),
                    )
                )
                actual_state = "error" if row["incident_id"] else "idle"
            connection.execute(
                """
                UPDATE manager_automation_runtime
                SET actual_state = ?, next_run_at = ?, heartbeat_at = ?,
                    lease_owner_hash = NULL, lease_until = NULL,
                    applied_revision = ?, applied_at = ?, updated_at = ?
                WHERE job_id = ? AND lease_owner_hash = ? AND fencing_token = ?
                """,
                (
                    actual_state,
                    next_run,
                    now,
                    int(row["revision"]),
                    now,
                    now,
                    job_id,
                    owner_hash,
                    fencing_token,
                ),
            )
            return True

    def heartbeat_run(
        self,
        *,
        job_id: str,
        run_id: str,
        owner: str,
        fencing_token: int,
        lease_seconds: int = 60,
    ) -> bool:
        owner_hash = hashlib.sha256(owner.encode()).hexdigest()
        now_dt = utc_now()
        now = isoformat(now_dt)
        lease_until = isoformat(now_dt + timedelta(seconds=lease_seconds))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE manager_automation_runtime
                SET heartbeat_at = ?, lease_until = ?, updated_at = ?
                WHERE job_id = ? AND lease_owner_hash = ? AND fencing_token = ?
                  AND lease_until > ?
                """,
                (now, lease_until, now, job_id, owner_hash, fencing_token, now),
            ).rowcount
            run = connection.execute(
                """
                SELECT 1 FROM manager_automation_runs
                WHERE run_id = ? AND job_id = ? AND status = 'running'
                  AND lease_owner_hash = ? AND fencing_token = ?
                """,
                (run_id, job_id, owner_hash, fencing_token),
            ).fetchone()
            return bool(changed and run)

    def finish_run(
        self,
        *,
        job_id: str,
        run_id: str,
        owner: str,
        fencing_token: int,
        succeeded: bool,
        result_code: str,
        next_delay_seconds: int | None = None,
    ) -> bool:
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}", result_code):
            raise AutomationError("automation_result_code_invalid")
        owner_hash = hashlib.sha256(owner.encode()).hexdigest()
        if next_delay_seconds is not None and not 5 <= next_delay_seconds <= 24 * 60 * 60:
            raise AutomationError("automation_next_delay_invalid")
        now_dt = utc_now()
        now = isoformat(now_dt)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT a.desired_state, a.every_seconds, a.timezone, a.revision,
                       a.schedule_timezone, a.active_window_json, a.template_id,
                       r.incident_id, r.incident_code
                FROM manager_automations a
                JOIN manager_automation_runtime r ON r.job_id = a.job_id
                WHERE a.job_id = ? AND r.lease_owner_hash = ? AND r.fencing_token = ?
                """,
                (job_id, owner_hash, fencing_token),
            ).fetchone()
            if row is None:
                return False
            updated_run = connection.execute(
                """
                UPDATE manager_automation_runs
                SET status = ?, finished_at = ?, result_code = ?
                WHERE run_id = ? AND job_id = ? AND status = 'running'
                  AND lease_owner_hash = ? AND fencing_token = ?
                """,
                (
                    "succeeded" if succeeded else "failed",
                    now,
                    result_code,
                    run_id,
                    job_id,
                    owner_hash,
                    fencing_token,
                ),
            ).rowcount
            if not updated_run:
                return False
            incident_id = str(row["incident_id"] or "") or None
            incident_code = str(row["incident_code"] or "") or None
            if not succeeded and incident_id is None:
                incident_id = f"inc_{uuid4().hex}"
                incident_code = result_code
                self._enqueue_incident_notification(
                    connection,
                    job_id=job_id,
                    run_id=run_id,
                    template_id=str(row["template_id"]),
                    transition="error",
                    incident_id=incident_id,
                    error_code=result_code,
                    now=now,
                )
            elif succeeded and incident_id is not None:
                self._enqueue_incident_notification(
                    connection,
                    job_id=job_id,
                    run_id=run_id,
                    template_id=str(row["template_id"]),
                    transition="recovery",
                    incident_id=incident_id,
                    error_code=incident_code or "automation_job_failed",
                    now=now,
                )
                incident_id = None
                incident_code = None
            delay_seconds = next_delay_seconds if next_delay_seconds is not None else int(row["every_seconds"])
            next_run = None
            if row["desired_state"] == "on":
                next_run = isoformat(
                    next_scheduled_time(
                        now_dt,
                        delay_seconds=delay_seconds,
                        timezone=str(row["schedule_timezone"] or row["timezone"]),
                        active_window=_decode_json(row["active_window_json"], "24/7"),
                    )
                )
            actual_state = "idle" if row["desired_state"] == "on" else "disabled"
            if not succeeded:
                actual_state = "error"
            connection.execute(
                """
                UPDATE manager_automation_runtime
                SET actual_state = ?, next_run_at = ?, last_success_at = ?,
                    heartbeat_at = ?, lease_owner_hash = NULL, lease_until = NULL,
                    error_code = ?, incident_id = ?, incident_code = ?,
                    applied_revision = ?, applied_at = ?, updated_at = ?
                WHERE job_id = ? AND lease_owner_hash = ? AND fencing_token = ?
                """,
                (
                    actual_state,
                    next_run,
                    now if succeeded else None,
                    now,
                    None if succeeded else result_code,
                    incident_id,
                    incident_code,
                    int(row["revision"]),
                    now,
                    now,
                    job_id,
                    owner_hash,
                    fencing_token,
                ),
            )
            return True

    def update_cursor(
        self,
        *,
        job_id: str,
        cursor_name: str,
        cursor_value: str | None,
        expected_revision: int,
        owner: str,
        fencing_token: int,
    ) -> dict[str, Any]:
        if CURSOR_NAME_PATTERN.fullmatch(cursor_name) is None:
            raise AutomationError("automation_cursor_name_invalid")
        if cursor_value is not None and (len(cursor_value) > 4096 or any(ord(char) < 32 for char in cursor_value)):
            raise AutomationError("automation_cursor_value_invalid")
        now = isoformat(utc_now())
        owner_hash = hashlib.sha256(owner.encode()).hexdigest()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            runtime = connection.execute(
                """
                SELECT fencing_token FROM manager_automation_runtime
                WHERE job_id = ? AND lease_owner_hash = ? AND lease_until > ?
                """,
                (job_id, owner_hash, now),
            ).fetchone()
            if runtime is None or int(runtime["fencing_token"]) != fencing_token:
                raise AutomationError("automation_fencing_conflict")
            current = connection.execute(
                "SELECT revision FROM manager_automation_cursors WHERE job_id = ? AND cursor_name = ?",
                (job_id, cursor_name),
            ).fetchone()
            revision = int(current["revision"]) if current else 0
            if revision != expected_revision:
                raise AutomationError(
                    "automation_cursor_revision_conflict",
                    details={"expected_revision": expected_revision, "current_revision": revision},
                )
            new_revision = revision + 1
            connection.execute(
                """
                INSERT INTO manager_automation_cursors(job_id, cursor_name, cursor_value, revision, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(job_id, cursor_name) DO UPDATE SET
                    cursor_value = excluded.cursor_value,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at
                """,
                (job_id, cursor_name, cursor_value, new_revision, now),
            )
            return {"cursor_name": cursor_name, "revision": new_revision}

    def get_cursors(self, *, job_id: str) -> dict[str, dict[str, Any]]:
        self.initialize()
        with self.connect() as connection:
            self._require_job(connection, job_id)
            rows = connection.execute(
                """
                SELECT cursor_name, cursor_value, revision, updated_at
                FROM manager_automation_cursors WHERE job_id = ? ORDER BY cursor_name
                """,
                (job_id,),
            ).fetchall()
        return {
            str(row["cursor_name"]): {
                "value": row["cursor_value"],
                "revision": int(row["revision"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        }

    def claim_outbox(self, *, owner: str, lease_seconds: int = 60) -> dict[str, Any] | None:
        """Claim one due delivery intent with a monotonically increasing fence."""

        if not 10 <= lease_seconds <= 600:
            raise AutomationError("automation_lease_invalid")
        owner_hash = hashlib.sha256(owner.encode()).hexdigest()
        now_dt = utc_now()
        now = isoformat(now_dt)
        lease_until = isoformat(now_dt + timedelta(seconds=lease_seconds))
        self.initialize()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            controller = connection.execute(
                "SELECT hold_enabled FROM manager_automation_controller_runtime WHERE singleton = 1"
            ).fetchone()
            if controller and bool(controller["hold_enabled"]):
                return None
            row = connection.execute(
                """
                SELECT o.outbox_id, o.job_id, o.run_id, o.kind, o.idempotency_key,
                       o.payload_json, o.fencing_token, o.status AS prior_status,
                       a.revision
                FROM manager_automation_outbox o
                JOIN manager_automations a ON a.job_id = o.job_id
                WHERE (
                    o.status IN ('queued', 'failed') AND o.next_attempt_at <= ?
                    AND (o.lease_until IS NULL OR o.lease_until <= ?)
                    AND a.archived_at IS NULL
                    AND (a.desired_state = 'on' OR o.kind = 'notification_test')
                ) OR (
                    o.status = 'sending' AND o.lease_until IS NOT NULL AND o.lease_until <= ?
                )
                ORDER BY CASE WHEN o.status = 'sending' THEN 0 ELSE 1 END,
                         o.next_attempt_at, o.created_at LIMIT 1
                """,
                (now, now, now),
            ).fetchone()
            if row is None:
                return None
            fencing_token = int(row["fencing_token"]) + 1
            recovery_only = row["prior_status"] == "sending"
            if recovery_only:
                changed = connection.execute(
                    """
                    UPDATE manager_automation_outbox
                    SET lease_owner_hash = ?, lease_until = ?, fencing_token = ?, updated_at = ?
                    WHERE outbox_id = ? AND fencing_token = ? AND status = 'sending'
                      AND lease_until IS NOT NULL AND lease_until <= ?
                    """,
                    (
                        owner_hash,
                        lease_until,
                        fencing_token,
                        now,
                        row["outbox_id"],
                        row["fencing_token"],
                        now,
                    ),
                ).rowcount
            else:
                changed = connection.execute(
                    """
                    UPDATE manager_automation_outbox
                    SET status = 'sending', attempts = attempts + 1,
                        lease_owner_hash = ?, lease_until = ?, fencing_token = ?, updated_at = ?
                    WHERE outbox_id = ? AND fencing_token = ?
                      AND status IN ('queued', 'failed')
                    """,
                    (owner_hash, lease_until, fencing_token, now, row["outbox_id"], row["fencing_token"]),
                ).rowcount
            if not changed:
                return None
            payload = _decode_json(row["payload_json"], {})
            if not isinstance(payload, dict):
                payload = {}
            return {
                "outbox_id": row["outbox_id"],
                "job_id": row["job_id"],
                "run_id": row["run_id"],
                "kind": row["kind"],
                "idempotency_key": row["idempotency_key"],
                "payload": payload,
                "claimed_revision": int(row["revision"]),
                "recovery_only": recovery_only,
                "fencing_token": fencing_token,
                "lease_until": lease_until,
            }

    def outbox_claim_is_current(
        self,
        *,
        outbox_id: str,
        owner: str,
        fencing_token: int,
        claimed_revision: int,
    ) -> bool:
        owner_hash = hashlib.sha256(owner.encode()).hexdigest()
        now = isoformat(utc_now())
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM manager_automation_outbox o
                JOIN manager_automations a ON a.job_id = o.job_id
                JOIN manager_automation_controller_runtime c ON c.singleton = 1
                WHERE o.outbox_id = ? AND o.status = 'sending'
                  AND o.lease_owner_hash = ? AND o.fencing_token = ? AND o.lease_until > ?
                  AND a.archived_at IS NULL AND a.revision = ?
                  AND (a.desired_state = 'on' OR o.kind = 'notification_test')
                  AND c.hold_enabled = 0
                """,
                (outbox_id, owner_hash, fencing_token, now, claimed_revision),
            ).fetchone()
        return row is not None

    def cancel_outbox_claim(
        self,
        *,
        outbox_id: str,
        owner: str,
        fencing_token: int,
        result_code: str = "automation_claim_superseded",
    ) -> bool:
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}", result_code):
            raise AutomationError("automation_result_code_invalid")
        owner_hash = hashlib.sha256(owner.encode()).hexdigest()
        now = isoformat(utc_now())
        with self.connect() as connection:
            changed = connection.execute(
                """
                UPDATE manager_automation_outbox
                SET status = 'cancelled', result_code = ?,
                    lease_owner_hash = NULL, lease_until = NULL, updated_at = ?
                WHERE outbox_id = ? AND status = 'sending'
                  AND lease_owner_hash = ? AND fencing_token = ? AND lease_until > ?
                """,
                (result_code, now, outbox_id, owner_hash, fencing_token, now),
            ).rowcount
        return bool(changed)

    def block_outbox_claim(
        self,
        *,
        outbox_id: str,
        owner: str,
        fencing_token: int,
        result_code: str = "automation_delivery_manual_reconciliation_required",
    ) -> bool:
        """Leave uncertain delivery visibly blocked without making it retryable."""

        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}", result_code):
            raise AutomationError("automation_result_code_invalid")
        owner_hash = hashlib.sha256(owner.encode()).hexdigest()
        now = isoformat(utc_now())
        with self.connect() as connection:
            changed = connection.execute(
                """
                UPDATE manager_automation_outbox
                SET result_code = ?, lease_owner_hash = NULL, lease_until = NULL, updated_at = ?
                WHERE outbox_id = ? AND status = 'sending'
                  AND lease_owner_hash = ? AND fencing_token = ? AND lease_until > ?
                """,
                (result_code, now, outbox_id, owner_hash, fencing_token, now),
            ).rowcount
        return bool(changed)

    def finish_outbox(
        self,
        *,
        outbox_id: str,
        owner: str,
        fencing_token: int,
        sent: bool,
        result_code: str,
        retry_seconds: int = 60,
    ) -> bool:
        """Complete a delivery only if the current lease and fence still match."""

        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}", result_code):
            raise AutomationError("automation_result_code_invalid")
        if not 10 <= retry_seconds <= 24 * 60 * 60:
            raise AutomationError("automation_retry_invalid")
        owner_hash = hashlib.sha256(owner.encode()).hexdigest()
        now_dt = utc_now()
        now = isoformat(now_dt)
        next_attempt_at = isoformat(now_dt + timedelta(seconds=retry_seconds))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE manager_automation_outbox
                SET status = ?, result_code = ?, next_attempt_at = ?,
                    lease_owner_hash = NULL, lease_until = NULL, updated_at = ?
                WHERE outbox_id = ? AND status = 'sending'
                  AND lease_owner_hash = ? AND fencing_token = ? AND lease_until > ?
                """,
                (
                    "sent" if sent else "failed",
                    result_code,
                    now if sent else next_attempt_at,
                    now,
                    outbox_id,
                    owner_hash,
                    fencing_token,
                    now,
                ),
            ).rowcount
            return bool(changed)

    def controller_heartbeat(self, *, owner: str, state: str = "active", started: bool = False) -> None:
        if state not in {"active", "stopping", "stopped", "error"}:
            raise AutomationError("automation_controller_state_invalid")
        now = isoformat(utc_now())
        owner_hash = hashlib.sha256(owner.encode()).hexdigest()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE manager_automation_controller_runtime
                SET state = ?, heartbeat_at = ?, owner_hash = ?,
                    started_at = CASE WHEN ? THEN ? ELSE started_at END,
                    updated_at = ?
                WHERE singleton = 1
                """,
                (state, now, owner_hash, int(started), now, now),
            )

    def set_global_hold(
        self,
        connection: sqlite3.Connection,
        *,
        enabled: bool,
        reason: str | None,
        actor_hash: str,
        attempt_hash: str | None,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise AutomationError("automation_global_hold_invalid")
        if type(expected_revision) is not int or expected_revision < 0:
            raise AutomationError("expected_revision_required")
        normalized_reason = str(reason or "").strip() or None
        if normalized_reason not in {None, "release", "maintenance", "operator"}:
            raise AutomationError("automation_global_hold_reason_invalid")
        normalized_attempt = str(attempt_hash or "").strip() or None
        if normalized_attempt is not None and re.fullmatch(r"[0-9a-f]{64}", normalized_attempt) is None:
            raise AutomationError("automation_global_hold_attempt_invalid")
        if enabled and normalized_reason == "release" and normalized_attempt is None:
            raise AutomationError("automation_global_hold_attempt_required")
        if enabled and normalized_reason != "release" and normalized_attempt is not None:
            raise AutomationError("automation_global_hold_attempt_invalid")
        if not enabled and normalized_reason is not None:
            raise AutomationError("automation_global_hold_reason_invalid")
        row = connection.execute(
            """
            SELECT hold_enabled, hold_reason, hold_owner_hash, hold_attempt_hash, hold_revision
            FROM manager_automation_controller_runtime WHERE singleton = 1
            """
        ).fetchone()
        if row is None:
            raise AutomationError("automation_controller_state_missing")
        current_revision = int(row["hold_revision"])
        if expected_revision != current_revision:
            raise AutomationError(
                "automation_revision_conflict",
                details={"expected_revision": expected_revision, "current_revision": current_revision},
            )
        if bool(row["hold_enabled"]):
            same_owner = row["hold_owner_hash"] == actor_hash
            same_attempt = (row["hold_attempt_hash"] or None) == normalized_attempt
            if not enabled and row["hold_reason"] == "release" and not (same_owner and same_attempt):
                raise AutomationError("automation_global_hold_ownership_lost")
            if enabled and (row["hold_reason"] or None) == normalized_reason and not (same_owner and same_attempt):
                raise AutomationError("automation_global_hold_ownership_lost")
        if bool(row["hold_enabled"]) == enabled and (row["hold_reason"] or None) == normalized_reason:
            return {
                "changed": False,
                "global_hold": {
                    "enabled": enabled,
                    "reason": normalized_reason,
                    "attempt_hash": normalized_attempt,
                    "revision": current_revision,
                },
            }
        new_revision = current_revision + 1
        now = isoformat(utc_now())
        connection.execute(
            """
            UPDATE manager_automation_controller_runtime
            SET hold_enabled = ?, hold_reason = ?, hold_owner_hash = ?, hold_attempt_hash = ?,
                hold_revision = ?, updated_at = ?
            WHERE singleton = 1
            """,
            (
                int(enabled),
                normalized_reason,
                actor_hash if enabled else None,
                normalized_attempt if enabled else None,
                new_revision,
                now,
            ),
        )
        return {
            "changed": True,
            "global_hold": {
                "enabled": enabled,
                "reason": normalized_reason,
                "attempt_hash": normalized_attempt if enabled else None,
                "revision": new_revision,
            },
        }

    def adopt_system_timer(
        self,
        *,
        timer_id: str,
        unit_name: str,
        control_mode: str,
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        if control_mode not in {"read_only", "managed"}:
            raise AutomationError("system_timer_control_mode_invalid")
        self.initialize()
        safe_state = self._safe_system_timer_state(state)
        now = isoformat(utc_now())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM manager_automation_system_timers WHERE timer_id = ?",
                (timer_id,),
            ).fetchone()
            if existing:
                if existing["unit_name"] != unit_name or existing["control_mode"] != control_mode:
                    raise AutomationError("system_timer_policy_changed")
                return {
                    "timer_id": timer_id,
                    "adopted": False,
                    "revision": int(existing["revision"]),
                    "state": _decode_json(existing["adopted_state_json"], {}),
                }
            revision = 1
            connection.execute(
                """
                INSERT INTO manager_automation_system_timers(
                    timer_id, unit_name, control_mode, adopted_state_json, adopted_at, revision
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(timer_id) DO UPDATE SET
                    unit_name = excluded.unit_name,
                    control_mode = excluded.control_mode,
                    adopted_state_json = excluded.adopted_state_json,
                    adopted_at = excluded.adopted_at,
                    revision = excluded.revision
                """,
                (timer_id, unit_name, control_mode, canonical_json(safe_state), now, revision),
            )
        return {"timer_id": timer_id, "adopted": True, "revision": revision, "state": safe_state}

    @staticmethod
    def _safe_system_timer_state(state: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: state.get(key)
            for key in (
                "load_state",
                "active_state",
                "unit_file_state",
                "desired_state",
                "actual_state",
                "period_minutes",
                "next_run_at",
                "last_run_at",
                "calendar",
                "error_code",
                "dropin_sha256",
            )
            if isinstance(state.get(key), (str, bool, int, type(None)))
        }

    @staticmethod
    def _system_timer_status(row: sqlite3.Row) -> dict[str, Any]:
        state = _decode_json(row["adopted_state_json"], {})
        if not isinstance(state, dict):
            state = {}
        return {
            "timer_id": row["timer_id"],
            "unit_name": row["unit_name"],
            "name": row["timer_id"],
            "control_mode": row["control_mode"],
            "locked": row["control_mode"] == "read_only",
            "desired_state": state.get("desired_state", "off"),
            "actual_state": state.get("actual_state", "unknown"),
            "period_minutes": state.get("period_minutes"),
            "revision": int(row["revision"]),
            "next_run_at": state.get("next_run_at"),
            "last_run_at": state.get("last_run_at"),
            "error_code": state.get("error_code"),
            "adopted_at": row["adopted_at"],
            "dropin_sha256": state.get("dropin_sha256"),
        }

    def update_system_timer(
        self,
        connection: sqlite3.Connection,
        *,
        timer_id: str,
        unit_name: str,
        control_mode: str,
        state: Mapping[str, Any],
        expected_revision: int | None,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM manager_automation_system_timers WHERE timer_id = ?",
            (timer_id,),
        ).fetchone()
        if row is None:
            raise AutomationError("system_timer_not_adopted")
        if type(expected_revision) is not int or expected_revision < 1:
            raise AutomationError("expected_revision_required")
        current_revision = int(row["revision"])
        if current_revision != expected_revision:
            raise AutomationError(
                "automation_revision_conflict",
                details={"expected_revision": expected_revision, "current_revision": current_revision},
            )
        if row["unit_name"] != unit_name or row["control_mode"] != control_mode:
            raise AutomationError("system_timer_policy_changed")
        safe_state = self._safe_system_timer_state(state)
        new_revision = current_revision + 1
        now = isoformat(utc_now())
        connection.execute(
            """
            UPDATE manager_automation_system_timers
            SET adopted_state_json = ?, adopted_at = ?, revision = ?
            WHERE timer_id = ? AND revision = ?
            """,
            (canonical_json(safe_state), now, new_revision, timer_id, current_revision),
        )
        updated = connection.execute(
            "SELECT * FROM manager_automation_system_timers WHERE timer_id = ?",
            (timer_id,),
        ).fetchone()
        if updated is None:
            raise AutomationError("system_timer_not_adopted")
        return self._system_timer_status(updated)

    def require_system_timer_revision(
        self,
        connection: sqlite3.Connection,
        *,
        timer_id: str,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM manager_automation_system_timers WHERE timer_id = ?",
            (timer_id,),
        ).fetchone()
        if row is None:
            raise AutomationError("system_timer_not_adopted")
        if type(expected_revision) is not int or expected_revision < 1:
            raise AutomationError("expected_revision_required")
        current_revision = int(row["revision"])
        if current_revision != expected_revision:
            raise AutomationError(
                "automation_revision_conflict",
                details={"expected_revision": expected_revision, "current_revision": current_revision},
            )
        return self._system_timer_status(row)
