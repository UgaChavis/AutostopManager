"""Content-free, bridge-owned durable work Telegram intake and execution ledger.

Only the bridge opens this database. Wake and agents use the authenticated bridge
socket; they never receive database credentials or write SQLite themselves.
Message bodies, media, display names and arbitrary diagnostic text are not part
of this schema. Exact locators and fixed state/error codes survive restarts.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

INBOX_SCHEMA_VERSION = 4
SUPPORTED_INBOX_SCHEMA_VERSIONS = (1, 2, 3, 4)
WORKER_PROTOCOL_VERSION = 2
REQUEST_DEADLINE_SECONDS = 1200

EVENT_PATTERN = re.compile(r"inbound-([1-9][0-9]{0,39})")
JOB_PATTERN = re.compile(r"job-[a-f0-9]{32}")
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
SHA256_PATTERN = re.compile(r"[a-f0-9]{64}")
REVISION_PATTERN = re.compile(r"[a-f0-9]{40}")
CODE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,95}")
OUTCOMES = frozenset({"answered", "awaiting_client", "handoff", "no_reply_needed"})
NO_REPLY_REASONS = frozenset(
    {
        "duplicate",
        "spam",
        "reaction_only",
        "already_answered",
        "not_actionable",
        "human_answered",
        "paused",
        "human_takeover",
    }
)
EFFECT_KINDS = frozenset({"ack", "result", "clarification", "handoff", "mutation"})
ACTIVE_STATES = frozenset({"claimed", "start_intent", "running", "needs_review"})


class InboxError(ValueError):
    """A fixed, content-free failure suitable for the private bridge protocol."""


@dataclass(frozen=True)
class InboundReference:
    sequence: int
    peer_id: int
    message_id: int
    received_at: str


def _positive(value: Any, code: str) -> int:
    if type(value) is not int or value < 1 or value > 9223372036854775807:
        raise InboxError(code)
    return value


def _token(value: Any, code: str) -> str:
    if not isinstance(value, str) or TOKEN_PATTERN.fullmatch(value) is None:
        raise InboxError(code)
    return value


def _code(value: Any) -> str:
    if not isinstance(value, str) or CODE_PATTERN.fullmatch(value) is None:
        raise InboxError("inbox_reason_invalid")
    return value


def _health_incident_cause(component: str, error_code: str | None) -> str | None:
    if error_code is None:
        return None
    prefix = component + "_"
    return error_code if error_code.startswith(prefix) else prefix + error_code


def _timestamp(value: str | datetime) -> str:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC).isoformat(timespec="microseconds")
    except (ValueError, TypeError, OverflowError) as exc:
        raise InboxError("inbox_timestamp_invalid") from exc


class TelegramInbox:
    """One writer, persisted event identities, and one global execution lease."""

    def __init__(
        self,
        path: Path,
        *,
        max_open_events: int = 10000,
        retention_seconds: int = 7 * 24 * 3600,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        _positive(max_open_events, "inbox_limit_invalid")
        _positive(retention_seconds, "inbox_retention_invalid")
        self.path = Path(path).absolute()
        self._max_open_events = max_open_events
        self._retention_seconds = retention_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._instruction_pair: tuple[str, str] | None = None
        self._lock_fd: int | None = None
        self._closed = False
        self._secure_directory()
        self._acquire_writer()
        try:
            self._db = sqlite3.connect(self.path, timeout=5)
            self._db.row_factory = sqlite3.Row
            self._validate_existing_schema()
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._create_schema()
            self._secure_files()
        except (OSError, sqlite3.Error, InboxError):
            self.close()
            raise InboxError("inbox_open_failed") from None

    def _secure_directory(self) -> None:
        parent = self.path.parent
        if parent in {Path("/"), Path("/tmp"), Path("/var/lib")}:
            raise InboxError("inbox_directory_invalid")
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if parent.resolve() != parent or parent.stat().st_uid != os.geteuid() or self.path.is_symlink():
            raise InboxError("inbox_directory_invalid")
        parent.chmod(0o700)

    def _acquire_writer(self) -> None:
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
        try:
            fd = os.open(str(self.path) + ".lock", flags, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd = fd
            os.fchmod(fd, 0o600)
            db_fd = os.open(self.path, flags, 0o600)
            try:
                os.fchmod(db_fd, 0o600)
            finally:
                os.close(db_fd)
        except (OSError, BlockingIOError):
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None
            elif "fd" in locals():
                os.close(fd)
            raise InboxError("inbox_writer_active") from None

    def _secure_files(self) -> None:
        for suffix in ("", "-wal", "-shm", ".lock"):
            file = Path(str(self.path) + suffix)
            if file.exists():
                if file.is_symlink() or file.stat().st_uid != os.geteuid():
                    raise InboxError("inbox_file_invalid")
                file.chmod(0o600)

    def _validate_existing_schema(self) -> None:
        if self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone() is None:
            return
        version = self._db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if version is None or version[0] not in {str(value) for value in SUPPORTED_INBOX_SCHEMA_VERSIONS}:
            raise InboxError("inbox_schema_unsupported")

    def _create_schema(self) -> None:
        # executescript normally commits a pending transaction first. Begin
        # inside the script so defaults, DDL and the version migrate together.
        # Partial age indexes keep intake pruning from scanning recent history.
        with self._db:
            self._db.executescript(
                f"""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT OR IGNORE INTO metadata VALUES ('schema_version', '{INBOX_SCHEMA_VERSION}');
            INSERT OR IGNORE INTO metadata VALUES ('generation', '0');
            INSERT OR IGNORE INTO metadata VALUES ('duty_enabled', '0');
            INSERT OR IGNORE INTO metadata VALUES ('execution_paused', '0');
            INSERT OR IGNORE INTO metadata VALUES ('execution_revision', '1');
            INSERT OR IGNORE INTO metadata VALUES ('rejected_events', '0');
            CREATE TABLE IF NOT EXISTS peers (
                peer_id INTEGER PRIMARY KEY, context_revision INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, peer_id INTEGER NOT NULL REFERENCES peers(peer_id),
                state TEXT NOT NULL, revision INTEGER NOT NULL, lease TEXT NOT NULL,
                context_revision INTEGER NOT NULL, instruction_root TEXT NOT NULL,
                instruction_revision TEXT NOT NULL, thread_id TEXT, turn_id TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, outcome TEXT, reason_code TEXT
            );
            CREATE INDEX IF NOT EXISTS jobs_peer_state ON jobs(peer_id, state);
            CREATE INDEX IF NOT EXISTS jobs_retention_updated_at ON jobs(updated_at)
                WHERE state IN ('completed','released');
            CREATE TABLE IF NOT EXISTS events (
                sequence TEXT PRIMARY KEY, peer_id INTEGER NOT NULL REFERENCES peers(peer_id),
                message_id INTEGER NOT NULL, received_at TEXT NOT NULL, state TEXT NOT NULL,
                context_revision INTEGER NOT NULL, job_id TEXT REFERENCES jobs(job_id), completed_at TEXT,
                UNIQUE(peer_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS events_queue ON events(state, received_at);
            CREATE INDEX IF NOT EXISTS events_job ON events(job_id);
            CREATE INDEX IF NOT EXISTS events_retention_completed_at ON events(completed_at)
                WHERE completed_at IS NOT NULL;
            CREATE TABLE IF NOT EXISTS effects (
                effect_key TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                kind TEXT NOT NULL, context_revision INTEGER NOT NULL, effect_slot INTEGER NOT NULL, text_sha256 TEXT,
                peer_id INTEGER NOT NULL, reply_to_message_id INTEGER NOT NULL,
                receipt_key TEXT UNIQUE, sent_message_id INTEGER, verified INTEGER NOT NULL DEFAULT 0,
                attempted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, verified_at TEXT,
                UNIQUE(job_id, kind, context_revision, effect_slot)
            );
            CREATE TABLE IF NOT EXISTS duty_windows (
                window_id INTEGER PRIMARY KEY AUTOINCREMENT, opened_at TEXT NOT NULL,
                closed_at TEXT, recovered_at TEXT
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                peer_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL, received_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gaps (
                peer_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL,
                received_at TEXT NOT NULL, reason_code TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS incidents (
                incident_id TEXT PRIMARY KEY, reason_code TEXT NOT NULL, opened_at TEXT NOT NULL,
                recovered_at TEXT, opened_receipt TEXT, recovery_receipt TEXT
            );
            CREATE TABLE IF NOT EXISTS health (
                component TEXT PRIMARY KEY, state TEXT NOT NULL, error_code TEXT, incident_id TEXT
            );
            CREATE TABLE IF NOT EXISTS reconciliation_cursor (
                singleton INTEGER PRIMARY KEY CHECK (singleton=1), peer_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL, received_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS boundary_holds (
                sequence TEXT PRIMARY KEY REFERENCES events(sequence) ON DELETE CASCADE,
                reason_code TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS peer_threads (
                peer_id INTEGER PRIMARY KEY REFERENCES peers(peer_id),
                thread_id TEXT UNIQUE, creation_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK(state IN ('creating','ready','creation_unknown')),
                revision INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS global_slot (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id), lease TEXT NOT NULL,
                phase TEXT NOT NULL CHECK(phase IN ('claimed','start_intent','running','manual'))
            );
            CREATE TABLE IF NOT EXISTS owner_routes (
                sequence TEXT PRIMARY KEY REFERENCES events(sequence) ON DELETE CASCADE,
                receipt_id TEXT NOT NULL UNIQUE,
                job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                accepted_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS owner_role_epochs (
                peer_id INTEGER PRIMARY KEY, started_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outgoing_observations (
                peer_id INTEGER NOT NULL REFERENCES peers(peer_id), message_id INTEGER NOT NULL,
                observed_at TEXT NOT NULL, origin TEXT NOT NULL,
                PRIMARY KEY(peer_id,message_id)
            );
            CREATE TABLE IF NOT EXISTS client_controls (
                peer_id INTEGER PRIMARY KEY REFERENCES peers(peer_id), state TEXT NOT NULL,
                revision INTEGER NOT NULL, guidance_pending INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS effects_peer_message ON effects(peer_id,sent_message_id);
            CREATE INDEX IF NOT EXISTS effects_peer_unknown ON effects(peer_id) WHERE attempted=1 AND verified=0;
                """
            )
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(jobs)")}
            additions = {
                "deadline_at": "TEXT",
                "execution_revision": "INTEGER",
                "execution_proof": "TEXT",
                "proof_at": "TEXT",
                "context_mode": "TEXT NOT NULL DEFAULT 'legacy'",
                "manual_recovery": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in additions.items():
                if name not in columns:
                    self._db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            self._db.execute(
                "UPDATE metadata SET value=? WHERE key='schema_version' AND value<>?",
                (str(INBOX_SCHEMA_VERSION), str(INBOX_SCHEMA_VERSION)),
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        db = getattr(self, "_db", None)
        if db is not None:
            db.close()
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def __enter__(self) -> TelegramInbox:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def _now(self) -> str:
        return _timestamp(self._clock())

    def _meta(self, key: str) -> str:
        row = self._db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        if row is None:
            raise InboxError("inbox_metadata_unavailable")
        return str(row[0])

    @property
    def generation(self) -> int:
        return int(self._meta("generation"))

    @contextmanager
    def _write(self) -> Iterator[None]:
        try:
            with self._db:
                self._db.execute("BEGIN IMMEDIATE")
                yield
                self._db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='generation'")
            self._secure_files()
        except sqlite3.Error:
            raise InboxError("inbox_storage_failed") from None

    @staticmethod
    def _event_sequence(event_id: Any) -> str:
        if not isinstance(event_id, str) or EVENT_PATTERN.fullmatch(event_id) is None:
            raise InboxError("inbound_event_invalid")
        return event_id.removeprefix("inbound-")

    def _event_row(self, event_id: str) -> sqlite3.Row:
        row = self._db.execute("SELECT * FROM events WHERE sequence=?", (self._event_sequence(event_id),)).fetchone()
        if row is None:
            raise InboxError("inbound_event_unavailable")
        return row

    def resolve(self, event_id: str) -> InboundReference:
        row = self._event_row(event_id)
        return InboundReference(int(row["sequence"]), row["peer_id"], row["message_id"], row["received_at"])

    def event_state(self, event_id: str) -> str:
        return str(self._event_row(event_id)["state"])

    def require_open(self, event_id: str) -> InboundReference:
        event = self.resolve(event_id)
        if self.event_state(event_id) != "open":
            raise InboxError("inbound_event_not_open")
        return event

    def configure_owner_route(self, peer_id: int | None) -> None:
        """Startup cutover: old client test refs never acquire owner authority."""
        if peer_id is None:
            return
        _positive(peer_id, "inbound_peer_invalid")
        if self._db.execute("SELECT 1 FROM owner_role_epochs WHERE peer_id=?", (peer_id,)).fetchone():
            return
        if self._instruction_pair is None:
            raise InboxError("inbox_instruction_invalid")
        with self._write():
            self._db.execute("INSERT INTO owner_role_epochs VALUES (?,?)", (peer_id, self._now()))
            sequences = [
                row[0]
                for row in self._db.execute(
                    "SELECT sequence FROM events WHERE peer_id=? AND state='open' AND job_id IS NULL", (peer_id,)
                )
            ]
            if sequences:
                self._hold_refs(
                    sequences, peer_id, *self._instruction_pair, reason_code="owner_role_transition_review_required"
                )
                self._db.executemany("DELETE FROM boundary_holds WHERE sequence=?", [(item,) for item in sequences])

    def owner_pending(self, peer_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
        """Exact unclaimed owner locators, independent of the client execution slot."""
        _positive(peer_id, "inbound_peer_invalid")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InboxError("inbox_page_limit_invalid")
        if self._db.execute("SELECT 1 FROM metadata WHERE key='work_client_only'").fetchone():
            return []
        return [
            {"event_id": f"inbound-{row['sequence']}", "received_at": row["received_at"]}
            for row in self._db.execute(
                "SELECT events.sequence,events.received_at FROM events JOIN owner_role_epochs USING(peer_id) "
                "WHERE peer_id=? AND state='open' AND job_id IS NULL AND received_at>=started_at "
                "AND NOT EXISTS (SELECT 1 FROM boundary_holds WHERE boundary_holds.sequence=events.sequence) "
                "ORDER BY received_at,events.rowid LIMIT ?",
                (peer_id, limit),
            )
        ]

    def configure_client_only(self, previous_owner_peer_id: int | None) -> None:
        """Cut over once; do not replay old management messages as new clients."""
        if self._db.execute("SELECT 1 FROM metadata WHERE key='work_client_only'").fetchone():
            return
        with self._write():
            if previous_owner_peer_id is not None and self._instruction_pair is not None:
                sequences = [
                    row[0]
                    for row in self._db.execute(
                        "SELECT sequence FROM events WHERE peer_id=? AND state='open' AND job_id IS NULL",
                        (previous_owner_peer_id,),
                    )
                ]
                if sequences:
                    self._hold_refs(
                        sequences, previous_owner_peer_id, *self._instruction_pair, reason_code="retired_owner_route"
                    )
                # Old management locators must neither replay as clients nor
                # permanently hold future genuine client messages. Archive only
                # proven unstarted, effect-free routing-boundary records.
                self._db.execute(
                    "UPDATE jobs SET state='retired',revision=revision+1,updated_at=? WHERE peer_id=? "
                    "AND reason_code IN ('retired_owner_route','owner_role_transition_review_required') "
                    "AND execution_proof='not_started' AND NOT EXISTS "
                    "(SELECT 1 FROM effects WHERE effects.job_id=jobs.job_id AND (attempted=1 OR verified=1)) "
                    "AND NOT EXISTS (SELECT 1 FROM global_slot WHERE global_slot.job_id=jobs.job_id)",
                    (self._now(), previous_owner_peer_id),
                )
                self._db.execute(
                    "UPDATE events SET state='retired',completed_at=? WHERE job_id IN "
                    "(SELECT job_id FROM jobs WHERE peer_id=? AND state='retired')",
                    (self._now(), previous_owner_peer_id),
                )
            self._db.execute("INSERT INTO metadata VALUES ('work_client_only','1')")

    def require_owner_source(self, event_id: str, owner_peer_id: int) -> InboundReference:
        event = self._event_row(event_id)
        if event["peer_id"] != owner_peer_id:
            raise InboxError("inbox_owner_target_mismatch")
        epoch = self._db.execute(
            "SELECT started_at FROM owner_role_epochs WHERE peer_id=?", (owner_peer_id,)
        ).fetchone()
        if event["state"] == "routed" and self.owner_route_receipt(event_id) is not None:
            return self.resolve(event_id)
        if self._db.execute("SELECT 1 FROM metadata WHERE key='work_client_only'").fetchone():
            raise InboxError("inbox_owner_route_retired")
        if (
            epoch is None
            or event["received_at"] < epoch[0]
            or event["state"] != "open"
            or event["job_id"] is not None
            or self._db.execute("SELECT 1 FROM boundary_holds WHERE sequence=?", (event["sequence"],)).fetchone()
        ):
            raise InboxError("inbox_owner_event_not_pending")
        return self.resolve(event_id)

    def owner_route_receipt(self, event_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT receipt_id,job_id,accepted_at FROM owner_routes WHERE sequence=?",
            (self._event_sequence(event_id),),
        ).fetchone()
        return None if row is None else {"event_id": event_id, **dict(row), "outcome": "routed", "role": "M1"}

    def owner_routes_unfinished(self, *, limit: int = 50) -> list[dict[str, str]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InboxError("inbox_page_limit_invalid")
        return [
            {"event_id": f"inbound-{row['sequence']}", "receipt_id": row["receipt_id"]}
            for row in self._db.execute(
                "SELECT owner_routes.sequence,receipt_id FROM owner_routes JOIN events USING(sequence) "
                "WHERE events.completed_at IS NULL ORDER BY accepted_at,events.rowid LIMIT ?",
                (limit,),
            )
        ]

    def complete_owner_route(self, event_id: str, receipt_id: str) -> dict[str, Any]:
        receipt = self.owner_route_receipt(event_id)
        if receipt is None or receipt["receipt_id"] != receipt_id:
            raise InboxError("inbox_owner_receipt_conflict")
        if self._event_row(event_id)["completed_at"] is None:
            with self._write():
                self._db.execute(
                    "UPDATE events SET completed_at=? WHERE sequence=?", (self._now(), self._event_sequence(event_id))
                )
        return receipt

    def route_owner_event(
        self,
        *,
        event_id: str,
        owner_peer_id: int,
        receipt_id: str,
        instruction_root: str,
        instruction_revision: str,
    ) -> dict[str, Any]:
        """Commit a verified local delivery receipt without starting a client turn.

        Only the authenticated root router supplies receipts after independently
        reading the role inbox. This terminal outcome is not a client reply.
        """
        self._instruction(instruction_root, instruction_revision)
        _positive(owner_peer_id, "inbound_peer_invalid")
        _token(receipt_id, "inbox_owner_receipt_invalid")
        event = self._event_row(event_id)
        if event["peer_id"] != owner_peer_id:
            raise InboxError("inbox_owner_target_mismatch")
        existing = self.owner_route_receipt(event_id)
        if existing is not None:
            if existing["receipt_id"] != receipt_id:
                raise InboxError("inbox_owner_receipt_conflict")
            return existing
        self.require_owner_source(event_id, owner_peer_id)
        now = self._now()
        job_id = "job-" + secrets.token_hex(16)
        with self._write():
            self._db.execute(
                "INSERT INTO jobs(job_id,peer_id,state,revision,lease,context_revision,instruction_root,"
                "instruction_revision,created_at,updated_at,outcome,execution_proof,proof_at,context_mode) "
                "VALUES (?,?,'completed',1,?,?,?,?,?,?,'routed','not_started',?,'owner_m1')",
                (
                    job_id,
                    owner_peer_id,
                    secrets.token_urlsafe(32),
                    event["context_revision"],
                    instruction_root,
                    instruction_revision,
                    now,
                    now,
                    now,
                ),
            )
            # Keep the exact reference while M1 may still need to read it.
            self._db.execute("UPDATE events SET state='routed',job_id=? WHERE sequence=?", (job_id, event["sequence"]))
            self._db.execute("INSERT INTO owner_routes VALUES (?,?,?,?)", (event["sequence"], receipt_id, job_id, now))
        return self.owner_route_receipt(event_id) or {}

    def record(
        self,
        peer_id: int,
        message_id: int,
        received_at: str | None = None,
        *,
        timestamp_precision: str = "exact",
    ) -> str | None:
        """Commit one locator only inside an explicitly enabled duty window."""

        _positive(peer_id, "inbound_peer_invalid")
        _positive(message_id, "inbound_message_invalid")
        timestamp = self._now() if received_at is None else _timestamp(received_at)
        old = self._db.execute(
            "SELECT sequence FROM events WHERE peer_id=? AND message_id=?", (peer_id, message_id)
        ).fetchone()
        if old is not None:
            return f"inbound-{old['sequence']}"
        checkpoint = self.checkpoint(peer_id)
        if checkpoint is not None and message_id <= checkpoint:
            return None
        classification = self.duty_classification(timestamp, timestamp_precision=timestamp_precision)
        if classification == "excluded":
            return None
        rejected = False
        sequence = str(secrets.randbits(128) or 1)
        with self._write():
            self._prune()
            count = self._db.execute("SELECT COUNT(*) FROM events WHERE state='open'").fetchone()[0]
            if count >= self._max_open_events:
                self._db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='rejected_events'")
                self._gap(peer_id, message_id, timestamp, "capacity")
                rejected = True
            else:
                self._db.execute("INSERT OR IGNORE INTO peers(peer_id) VALUES (?)", (peer_id,))
                # A proven, effect-free failed turn is retried only on genuinely
                # new client input. A repeated update never replays that turn.
                self._db.execute(
                    "UPDATE events SET job_id=NULL WHERE peer_id=? AND state='open' AND job_id IN "
                    "(SELECT job_id FROM jobs WHERE peer_id=? AND state='failed_waiting_input')",
                    (peer_id, peer_id),
                )
                self._db.execute("UPDATE peers SET context_revision=context_revision+1 WHERE peer_id=?", (peer_id,))
                context = self._peer_revision(peer_id)
                self._db.execute(
                    "INSERT INTO events(sequence,peer_id,message_id,received_at,state,context_revision) "
                    "VALUES (?,?,?,?,'open',?)",
                    (sequence, peer_id, message_id, timestamp, context),
                )
                owner_epoch = self._db.execute(
                    "SELECT started_at FROM owner_role_epochs WHERE peer_id=?", (peer_id,)
                ).fetchone()
                if self._db.execute("SELECT 1 FROM metadata WHERE key='work_client_only'").fetchone():
                    owner_epoch = None
                hold_reason = (
                    "owner_role_transition_review_required"
                    if owner_epoch and timestamp < owner_epoch[0]
                    else "duty_boundary_ambiguous"
                    if classification == "ambiguous"
                    else None
                )
                if hold_reason:
                    if self._instruction_pair is not None:
                        self._hold_refs([sequence], peer_id, *self._instruction_pair, reason_code=hold_reason)
                    else:
                        self._db.execute("INSERT INTO boundary_holds VALUES (?,?)", (sequence, hold_reason))
                        self._gap(peer_id, message_id, timestamp, "boundary_instruction_unavailable")
        if rejected:
            raise InboxError("inbound_monitor_full")
        return f"inbound-{sequence}"

    def import_event(
        self,
        event_id: str,
        peer_id: int,
        message_id: int,
        received_at: str,
        *,
        state: str = "open",
        instruction_root: str,
        instruction_revision: str,
    ) -> str:
        self.import_references(
            [
                {
                    "event_id": event_id,
                    "peer_id": peer_id,
                    "message_id": message_id,
                    "received_at": received_at,
                    "state": state,
                }
            ],
            instruction_root=instruction_root,
            instruction_revision=instruction_revision,
        )
        return event_id

    def import_references(
        self, references: list[dict[str, Any]], *, instruction_root: str, instruction_revision: str
    ) -> dict[str, Any]:
        """Atomically import exact legacy locators, held for explicit reconciliation.

        A legacy ``reply_verified`` can mean only an initial acknowledgement, so
        every imported locator requires review rather than automatically replaying
        or treating the old technical closure as a final business result.
        """

        self._instruction(instruction_root, instruction_revision)
        if not isinstance(references, list) or len(references) > self._max_open_events:
            raise InboxError("inbox_import_invalid")
        prepared: list[tuple[str, int, int, str]] = []
        seen_ids: set[str] = set()
        seen_refs: set[tuple[int, int]] = set()
        fields = {"event_id", "peer_id", "message_id", "received_at", "state"}
        for reference in references:
            if not isinstance(reference, dict) or set(reference) != fields:
                raise InboxError("inbox_import_invalid")
            sequence = self._event_sequence(reference["event_id"])
            peer = _positive(reference["peer_id"], "inbound_peer_invalid")
            message = _positive(reference["message_id"], "inbound_message_invalid")
            at = _timestamp(reference["received_at"])
            state = reference["state"]
            if not isinstance(state, str) or state not in {"open", "reply_verified", "no_reply_needed"}:
                raise InboxError("inbound_state_invalid")
            if sequence in seen_ids or (peer, message) in seen_refs:
                raise InboxError("inbound_import_conflict")
            seen_ids.add(sequence)
            seen_refs.add((peer, message))
            old = self._db.execute(
                "SELECT sequence,peer_id,message_id FROM events WHERE sequence=? OR (peer_id=? AND message_id=?)",
                (sequence, peer, message),
            ).fetchone()
            if old is not None:
                if (old["sequence"], old["peer_id"], old["message_id"]) != (sequence, peer, message):
                    raise InboxError("inbound_import_conflict")
            else:
                prepared.append((sequence, peer, message, at))
        if self.status()["open_events"] + len(prepared) > self._max_open_events:
            raise InboxError("inbound_monitor_full")
        if not prepared:
            return {
                "imported": 0,
                "event_ids": [f"inbound-{sequence}" for sequence in sorted(seen_ids)],
                "review_job_ids": [],
            }
        jobs: list[str] = []
        with self._write():
            for sequence, peer, message, at in prepared:
                self._db.execute("INSERT OR IGNORE INTO peers(peer_id) VALUES (?)", (peer,))
                self._db.execute("UPDATE peers SET context_revision=context_revision+1 WHERE peer_id=?", (peer,))
                self._db.execute(
                    "INSERT INTO events(sequence,peer_id,message_id,received_at,state,context_revision) "
                    "VALUES (?,?,?,?,'open',?)",
                    (sequence, peer, message, at, self._peer_revision(peer)),
                )
            remaining = sorted(prepared, key=lambda item: (item[3], item[0]))
            while remaining:
                first = remaining[0]
                upper = _timestamp(datetime.fromisoformat(first[3]) + timedelta(seconds=300))
                group = [item for item in remaining if item[1] == first[1] and item[3] <= upper][:5]
                remaining = [item for item in remaining if item not in group]
                job_id = "job-" + secrets.token_hex(16)
                jobs.append(job_id)
                now = self._now()
                self._db.execute(
                    "INSERT INTO jobs(job_id,peer_id,state,revision,lease,context_revision,instruction_root,"
                    "instruction_revision,created_at,updated_at,reason_code) "
                    "VALUES (?,?,'needs_review',1,?,?,?,?,?,?,'legacy_import_review_required')",
                    (
                        job_id,
                        first[1],
                        secrets.token_urlsafe(32),
                        self._peer_revision(first[1]),
                        instruction_root,
                        instruction_revision,
                        now,
                        now,
                    ),
                )
                self._db.executemany(
                    "UPDATE events SET job_id=? WHERE sequence=?", [(job_id, item[0]) for item in group]
                )
        return {
            "imported": len(prepared),
            "event_ids": [f"inbound-{item[0]}" for item in prepared],
            "review_job_ids": jobs,
        }

    def events(self, *, limit: int = 100, after_event_id: str | None = None) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 256:
            raise InboxError("inbox_page_limit_invalid")
        cursor = 0
        if after_event_id is not None:
            row = self._db.execute(
                "SELECT rowid FROM events WHERE sequence=?", (self._event_sequence(after_event_id),)
            ).fetchone()
            if row is None:
                raise InboxError("inbound_event_unavailable")
            cursor = row[0]
        return [
            {"event_id": f"inbound-{row['sequence']}", "received_at": row["received_at"], "state": row["state"]}
            for row in self._db.execute(
                "SELECT sequence,received_at,state FROM events WHERE rowid>? ORDER BY rowid LIMIT ?", (cursor, limit)
            )
        ]

    def context(self, event_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 5:
            raise InboxError("inbound_context_limit_invalid")
        anchor = self._event_row(event_id)
        lower = _timestamp(datetime.fromisoformat(anchor["received_at"]) - timedelta(seconds=300))
        upper = _timestamp(datetime.fromisoformat(anchor["received_at"]) + timedelta(seconds=300))
        rows = self._db.execute(
            "SELECT sequence,received_at,state FROM ("
            "SELECT rowid AS insertion_order,sequence,received_at,state FROM events "
            "WHERE peer_id=? AND received_at BETWEEN ? AND ? "
            "ORDER BY CASE WHEN sequence=? THEN 0 ELSE 1 END,"
            "ABS(julianday(received_at)-julianday(?)),rowid LIMIT ?) "
            "ORDER BY received_at,insertion_order",
            (anchor["peer_id"], lower, upper, anchor["sequence"], anchor["received_at"], limit),
        )
        return [{"event_id": f"inbound-{row[0]}", "received_at": row[1], "state": row[2]} for row in rows]

    def _peer_revision(self, peer_id: int) -> int:
        row = self._db.execute("SELECT context_revision FROM peers WHERE peer_id=?", (peer_id,)).fetchone()
        if row is None:
            raise InboxError("inbox_peer_unavailable")
        return int(row[0])

    def status(self) -> dict[str, Any]:
        counts = dict(self._db.execute("SELECT state,COUNT(*) FROM events GROUP BY state"))
        oldest = self._db.execute("SELECT MIN(received_at) FROM events WHERE state='open'").fetchone()[0]
        age = (
            0
            if oldest is None
            else max(0, int((datetime.fromisoformat(self._now()) - datetime.fromisoformat(oldest)).total_seconds()))
        )
        return {
            "enabled": self._meta("duty_enabled") == "1",
            "execution_paused": self._meta("execution_paused") == "1",
            "execution_revision": int(self._meta("execution_revision")),
            "generation": self.generation,
            "retention": "protected_sqlite",
            "pending_events": counts.get("open", 0),
            "open_events": counts.get("open", 0),
            "retained_events": sum(counts.values()),
            "reply_verified_events": counts.get("reply_verified", 0),
            "routed_events": counts.get("routed", 0),
            "no_reply_needed_events": counts.get("no_reply_needed", 0),
            "dropped_events": 0,
            "dropped_open_events": 0,
            "rejected_events": int(self._meta("rejected_events")),
            "oldest_pending_age_seconds": age,
            "gap_count": self._db.execute("SELECT COUNT(*) FROM gaps").fetchone()[0],
            **self.job_status(),
        }

    def job_status(self) -> dict[str, Any]:
        counts = dict(self._db.execute("SELECT state,COUNT(*) FROM jobs GROUP BY state"))
        slot = self._db.execute(
            "SELECT jobs.job_id,jobs.deadline_at,jobs.state,global_slot.phase AS slot_phase "
            "FROM global_slot JOIN jobs USING(job_id) WHERE singleton=1"
        ).fetchone()
        return {
            "active_jobs": sum(counts.get(state, 0) for state in ACTIVE_STATES)
            + int(slot is not None and slot["state"] == "waiting_control"),
            "waiting_control_jobs": counts.get("waiting_control", 0),
            "needs_review_jobs": counts.get("needs_review", 0),
            "completed_jobs": counts.get("completed", 0),
            "needs_review_events": self._db.execute(
                "SELECT COUNT(*) FROM events LEFT JOIN jobs USING(job_id) "
                "WHERE events.state='open' AND (jobs.state='needs_review' OR EXISTS "
                "(SELECT 1 FROM boundary_holds WHERE boundary_holds.sequence=events.sequence))"
            ).fetchone()[0],
            "schema_version": INBOX_SCHEMA_VERSION,
            "worker_protocol": WORKER_PROTOCOL_VERSION,
            "context_mode": "serial_peer_v1",
            "global_slot": (
                {key: slot[key] for key in ("job_id", "deadline_at", "slot_phase")} if slot is not None else None
            ),
            "compatibility_hold": self._compatibility_hold(),
            "held_peers": self._db.execute(
                "SELECT COUNT(*) FROM peers WHERE EXISTS "
                "(SELECT 1 FROM jobs WHERE jobs.peer_id=peers.peer_id AND jobs.state='needs_review') "
                "OR EXISTS (SELECT 1 FROM peer_threads WHERE peer_threads.peer_id=peers.peer_id "
                "AND peer_threads.state<>'ready')"
            ).fetchone()[0],
        }

    def _compatibility_hold(self) -> bool:
        return (
            self._db.execute(
                "SELECT 1 FROM jobs WHERE context_mode='legacy' AND execution_proof IS NULL "
                "AND state IN ('claimed','start_intent','running','needs_review') LIMIT 1"
            ).fetchone()
            is not None
        )

    def execution_slot(self) -> dict[str, Any] | None:
        row = self._db.execute("SELECT job_id,phase FROM global_slot WHERE singleton=1").fetchone()
        if row is None:
            return None
        return {**self.read_job(row["job_id"]), "slot_phase": row["phase"]}

    def configure_instructions(self, instruction_root: str, instruction_revision: str) -> None:
        """Set the bridge's validated physical capture snapshot for new holds."""

        self._instruction(instruction_root, instruction_revision)
        self._instruction_pair = (instruction_root, instruction_revision)

    def _hold_refs(
        self, sequences: list[str], peer_id: int, instruction_root: str, instruction_revision: str, *, reason_code: str
    ) -> None:
        job_id = "job-" + secrets.token_hex(16)
        now = self._now()
        self._db.execute(
            "INSERT INTO jobs(job_id,peer_id,state,revision,lease,context_revision,instruction_root,"
            "instruction_revision,created_at,updated_at,reason_code,context_mode,execution_proof,proof_at) "
            "VALUES (?,?,'needs_review',1,?,?,?,?,?,?,?,'peer_v1','not_started',?)",
            (
                job_id,
                peer_id,
                secrets.token_urlsafe(32),
                self._peer_revision(peer_id),
                instruction_root,
                instruction_revision,
                now,
                now,
                reason_code,
                now,
            ),
        )
        self._db.executemany(
            "UPDATE events SET job_id=? WHERE sequence=?", [(job_id, sequence) for sequence in sequences]
        )

    def _promote_boundary_holds(self, instruction_root: str, instruction_revision: str) -> None:
        rows = list(
            self._db.execute(
                "SELECT events.sequence,events.peer_id,boundary_holds.reason_code FROM events JOIN boundary_holds USING(sequence) "
                "WHERE events.state='open' AND events.job_id IS NULL ORDER BY events.received_at,events.rowid"
            )
        )
        if not rows:
            return
        with self._write():
            for row in rows:
                self._hold_refs(
                    [row["sequence"]],
                    row["peer_id"],
                    instruction_root,
                    instruction_revision,
                    reason_code=row["reason_code"],
                )
                self._db.execute("DELETE FROM boundary_holds WHERE sequence=?", (row["sequence"],))

    @staticmethod
    def _instruction(instruction_root: str, instruction_revision: str) -> None:
        if (
            not isinstance(instruction_root, str)
            or len(instruction_root) > 4096
            or "\x00" in instruction_root
            or "\n" in instruction_root
            or not Path(instruction_root).is_absolute()
            or ".." in Path(instruction_root).parts
            or not isinstance(instruction_revision, str)
            or REVISION_PATTERN.fullmatch(instruction_revision) is None
        ):
            raise InboxError("inbox_instruction_invalid")

    def claim(
        self,
        *,
        instruction_root: str,
        instruction_revision: str,
        exclude_peer_id: int | None = None,
    ) -> dict[str, Any] | None:
        self._instruction(instruction_root, instruction_revision)
        if exclude_peer_id is not None:
            _positive(exclude_peer_id, "inbound_peer_invalid")
        if self._meta("duty_enabled") != "1" or self._meta("execution_paused") == "1":
            return None
        self._promote_boundary_holds(instruction_root, instruction_revision)
        if self.execution_slot() is not None or self._compatibility_hold():
            return None
        first = self._db.execute(
            "SELECT events.* FROM events WHERE state='open' AND job_id IS NULL "
            "AND (? IS NULL OR events.peer_id<>?) "
            "AND NOT EXISTS (SELECT 1 FROM client_controls WHERE client_controls.peer_id=events.peer_id "
            "AND state<>'active') "
            "AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.peer_id=events.peer_id "
            "AND jobs.state IN ('claimed','start_intent','running','needs_review')) "
            "AND NOT EXISTS (SELECT 1 FROM peer_threads WHERE peer_threads.peer_id=events.peer_id "
            "AND peer_threads.state<>'ready') ORDER BY received_at,events.rowid LIMIT 1",
            (exclude_peer_id, exclude_peer_id),
        ).fetchone()
        if first is None:
            return None
        upper = _timestamp(datetime.fromisoformat(first["received_at"]) + timedelta(seconds=300))
        rows = list(
            self._db.execute(
                "SELECT sequence FROM events WHERE state='open' AND job_id IS NULL AND peer_id=? "
                "AND received_at BETWEEN ? AND ? ORDER BY received_at,rowid LIMIT ?",
                (first["peer_id"], first["received_at"], upper, 5),
            )
        )
        job_id = "job-" + secrets.token_hex(16)
        lease = secrets.token_urlsafe(32)
        now = self._now()
        deadline = _timestamp(datetime.fromisoformat(now) + timedelta(seconds=REQUEST_DEADLINE_SECONDS))
        with self._write():
            self._db.execute(
                "INSERT INTO jobs(job_id,peer_id,state,revision,lease,context_revision,instruction_root,"
                "instruction_revision,created_at,updated_at,deadline_at,execution_revision,context_mode) "
                "VALUES (?,?,'claimed',1,?,?,?,?,?,?,?,?, 'peer_v1')",
                (
                    job_id,
                    first["peer_id"],
                    lease,
                    self._peer_revision(first["peer_id"]),
                    instruction_root,
                    instruction_revision,
                    now,
                    now,
                    deadline,
                    int(self._meta("execution_revision")),
                ),
            )
            self._db.execute("INSERT INTO global_slot VALUES (1,?,?,'claimed')", (job_id, lease))
            self._db.executemany("UPDATE events SET job_id=? WHERE sequence=?", [(job_id, row[0]) for row in rows])
        return self.read_job(job_id)

    def _job_row(self, job_id: Any) -> sqlite3.Row:
        if not isinstance(job_id, str) or JOB_PATTERN.fullmatch(job_id) is None:
            raise InboxError("inbox_job_invalid")
        row = self._db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise InboxError("inbox_job_unavailable")
        return row

    def read_job(self, job_id: str) -> dict[str, Any]:
        result = dict(self._job_row(job_id))
        result["event_ids"] = self._job_event_ids(job_id)
        result["effects"] = [
            dict(row)
            for row in self._db.execute("SELECT * FROM effects WHERE job_id=? ORDER BY created_at,rowid", (job_id,))
        ]
        result["outgoing_context"] = self.outgoing_context(result["peer_id"])
        result["client_control"] = self.client_control(result["peer_id"])
        result["prior_acknowledgements"] = [
            dict(row)
            for row in self._db.execute(
                "SELECT effect_key,job_id,receipt_key,sent_message_id,reply_to_message_id FROM effects "
                "WHERE peer_id=? AND kind='ack' AND verified=1 AND job_id<>? AND reply_to_message_id IN "
                "(SELECT message_id FROM events WHERE job_id=?) ORDER BY created_at",
                (result["peer_id"], job_id, job_id),
            )
        ]
        return result

    def outgoing_context(self, peer_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self._db.execute(
                "SELECT message_id,origin FROM outgoing_observations WHERE peer_id=? ORDER BY message_id DESC LIMIT 100",
                (peer_id,),
            )
        ]

    def observe_outgoing(self, peer_id: int, message_id: int, *, native_origin: str | None = None) -> str:
        """Transport-authenticated self messages are context, never inbound work.

        An unresolved agent effect prevents attributing an unmatched message to
        a person. Exact verification later reconciles it without fake receipts.
        """
        _positive(peer_id, "inbound_peer_invalid")
        _positive(message_id, "inbound_message_invalid")
        existing = self._db.execute(
            "SELECT origin FROM outgoing_observations WHERE peer_id=? AND message_id=?", (peer_id, message_id)
        ).fetchone()
        if native_origin not in {None, "agent", "unresolved"}:
            raise InboxError("inbox_outgoing_origin_invalid")
        if existing is not None and existing[0] == "agent":
            return "agent"
        agent = self._db.execute(
            "SELECT 1 FROM effects WHERE peer_id=? AND sent_message_id=? AND verified=1 AND kind<>'handoff'",
            (peer_id, message_id),
        ).fetchone()
        unknown = self._db.execute(
            "SELECT 1 FROM effects WHERE peer_id=? AND attempted=1 AND verified=0 AND kind<>'handoff'", (peer_id,)
        ).fetchone()
        origin = (
            "agent"
            if agent or native_origin == "agent"
            else "unresolved"
            if unknown or native_origin == "unresolved"
            else "human"
        )
        if existing is not None:
            # A new pending send cannot change an already known human message.
            if existing[0] == origin or (existing[0] == "human" and origin != "agent"):
                return str(existing[0])
            with self._write():
                self._db.execute(
                    "UPDATE outgoing_observations SET origin=? WHERE peer_id=? AND message_id=?",
                    (origin, peer_id, message_id),
                )
                if origin == "human":
                    self._db.execute("UPDATE peers SET context_revision=context_revision+1 WHERE peer_id=?", (peer_id,))
            return origin
        with self._write():
            self._db.execute("INSERT OR IGNORE INTO peers(peer_id) VALUES (?)", (peer_id,))
            self._db.execute(
                "INSERT INTO outgoing_observations VALUES (?,?,?,?)", (peer_id, message_id, self._now(), origin)
            )
            if origin == "human":
                self._db.execute("UPDATE peers SET context_revision=context_revision+1 WHERE peer_id=?", (peer_id,))
        return origin

    def client_control(self, peer_id: int) -> dict[str, Any]:
        row = self._db.execute(
            "SELECT state,revision,guidance_pending FROM client_controls WHERE peer_id=?", (peer_id,)
        ).fetchone()
        return dict(row) if row else {"state": "active", "revision": 0, "guidance_pending": 0}

    def set_client_control(self, peer_id: int, *, action: str, expected_revision: int) -> dict[str, Any]:
        """Called only by the explicit trusted Codex control endpoint."""
        _positive(peer_id, "inbound_peer_invalid")
        if action not in {"check_variant", "context", "reconsider", "pause", "takeover", "resume"}:
            raise InboxError("inbox_client_control_invalid")
        if self._db.execute("SELECT 1 FROM peers WHERE peer_id=?", (peer_id,)).fetchone() is None:
            raise InboxError("inbox_client_unknown")
        old = self.client_control(peer_id)
        if type(expected_revision) is not int or old["revision"] != expected_revision:
            raise InboxError("inbox_revision_conflict")
        state = {"pause": "paused", "takeover": "human_takeover", "resume": "active"}.get(action, old["state"])
        with self._write():
            self._db.execute(
                "INSERT INTO client_controls VALUES (?,?,?,?) ON CONFLICT(peer_id) DO UPDATE SET "
                "state=excluded.state,revision=excluded.revision,guidance_pending=excluded.guidance_pending",
                (peer_id, state, old["revision"] + 1, int(action in {"check_variant", "context", "reconsider"})),
            )
            self._db.execute("UPDATE peers SET context_revision=context_revision+1 WHERE peer_id=?", (peer_id,))
            if action == "resume":
                self._release_control_waiters(peer_id)
        return self.client_control(peer_id)

    def _release_control_waiters(self, peer_id: int) -> None:
        """Release only proven-stopped requests after explicit client resume.

        Called inside the current write transaction. Unknown effects and jobs
        whose model turn has not stopped retain their original binding.
        """
        if self.client_control(peer_id)["state"] != "active":
            return
        rows = list(
            self._db.execute(
                "SELECT job_id FROM jobs WHERE peer_id=? AND state='waiting_control' AND execution_proof IS NOT NULL",
                (peer_id,),
            )
        )
        for row in rows:
            job_id = row["job_id"]
            if self._db.execute(
                "SELECT 1 FROM effects WHERE job_id=? AND attempted=1 AND (verified=0 OR kind<>'ack')",
                (job_id,),
            ).fetchone():
                continue
            self._transition(job_id, "released", reason_code="client_resumed")
            self._db.execute("UPDATE events SET job_id=NULL WHERE job_id=? AND state='open'", (job_id,))

    def _job_event_ids(self, job_id: str) -> list[str]:
        return [
            f"inbound-{row[0]}"
            for row in self._db.execute(
                "SELECT sequence FROM events WHERE job_id=? ORDER BY received_at,rowid", (job_id,)
            )
        ]

    def active_jobs(self) -> list[dict[str, Any]]:
        return [
            self.read_job(row[0])
            for row in self._db.execute(
                "SELECT job_id FROM jobs WHERE state IN ('claimed','start_intent','running','needs_review') "
                "OR (state='waiting_control' AND execution_proof IS NULL) "
                "ORDER BY created_at,rowid"
            )
        ]

    def active_job_page(self, *, limit: int = 50, after_job_id: str | None = None) -> dict[str, Any]:
        """Bounded IPC summaries in stable insertion order, excluding large payloads.

        A cursor still resolves after its job completes or releases. Full effects
        and the instruction path remain available through an exact ``read_job``.
        """

        if type(limit) is not int or not 1 <= limit <= 100:
            raise InboxError("inbox_page_limit_invalid")
        cursor = 0
        if after_job_id is not None:
            if not isinstance(after_job_id, str) or JOB_PATTERN.fullmatch(after_job_id) is None:
                raise InboxError("inbox_job_cursor_invalid")
            row = self._db.execute("SELECT rowid FROM jobs WHERE job_id=?", (after_job_id,)).fetchone()
            if row is None:
                raise InboxError("inbox_job_cursor_unavailable")
            cursor = row[0]
        rows = list(
            self._db.execute(
                "SELECT job_id,peer_id,state,revision,lease,context_revision,instruction_revision,"
                "thread_id,turn_id,reason_code,deadline_at,execution_revision,execution_proof,proof_at,"
                "context_mode,manual_recovery FROM jobs "
                "WHERE rowid>? AND (state IN ('claimed','start_intent','running','needs_review') "
                "OR (state='waiting_control' AND execution_proof IS NULL)) "
                "ORDER BY rowid LIMIT ?",
                (cursor, limit + 1),
            )
        )
        has_more = len(rows) > limit
        jobs = [{**dict(row), "event_ids": self._job_event_ids(row["job_id"])} for row in rows[:limit]]
        return {"jobs": jobs, "next_job_id": jobs[-1]["job_id"] if has_more else None}

    def _lease(self, job_id: str, lease: str, expected_revision: int) -> sqlite3.Row:
        job = self._binding(job_id, lease, expected_revision)
        if job["state"] not in ACTIVE_STATES:
            raise InboxError("inbox_job_not_active")
        return job

    def _binding(self, job_id: str, lease: str, expected_revision: int) -> sqlite3.Row:
        _token(lease, "inbox_lease_invalid")
        _positive(expected_revision, "inbox_revision_invalid")
        job = self._job_row(job_id)
        if not hmac.compare_digest(job["lease"], lease):
            raise InboxError("inbox_lease_mismatch")
        if job["revision"] != expected_revision:
            raise InboxError("inbox_revision_conflict")
        return job

    def _require_execution(self, job: sqlite3.Row) -> None:
        slot = self._db.execute("SELECT * FROM global_slot WHERE singleton=1").fetchone()
        if slot is None or slot["job_id"] != job["job_id"] or not hmac.compare_digest(slot["lease"], job["lease"]):
            raise InboxError("inbox_execution_slot_mismatch")
        paused = self._meta("execution_paused") == "1"
        if job["manual_recovery"]:
            if not paused:
                raise InboxError("inbox_execution_stale")
        elif paused or self._meta("duty_enabled") != "1":
            raise InboxError("inbox_execution_paused")
        if job["execution_revision"] != int(self._meta("execution_revision")):
            raise InboxError("inbox_execution_stale")
        if not job["manual_recovery"] and (
            job["deadline_at"] is None
            or datetime.fromisoformat(job["deadline_at"]) <= datetime.fromisoformat(self._now())
        ):
            raise InboxError("inbox_execution_deadline")

    def execution_authority(self, *, job_id: str, lease: str, expected_revision: int) -> dict[str, Any]:
        """A revocable write permit; delivery readback never requires this permit."""

        job = self._lease(job_id, lease, expected_revision)
        if job["state"] != "running":
            raise InboxError("inbox_effect_state_invalid")
        self._require_execution(job)
        control = self.client_control(job["peer_id"])
        if control["state"] != "active":
            raise InboxError("inbox_client_" + control["state"])
        return {
            "job_id": job_id,
            "lease": lease,
            "revision": expected_revision,
            "execution_revision": job["execution_revision"],
            "deadline_at": job["deadline_at"],
            "thread_id": job["thread_id"],
            "turn_id": job["turn_id"],
            "manual_recovery": bool(job["manual_recovery"]),
            "peer_context_revision": self._peer_revision(job["peer_id"]),
            "client_control_revision": control["revision"],
            "duty_window_id": self._db.execute(
                "SELECT MAX(window_id) FROM duty_windows WHERE closed_at IS NULL"
            ).fetchone()[0],
        }

    def check_execution_authority(self, snapshot: dict[str, Any]) -> None:
        if not isinstance(snapshot, dict):
            raise InboxError("inbox_execution_stale")
        try:
            current = self.execution_authority(
                job_id=snapshot["job_id"], lease=snapshot["lease"], expected_revision=snapshot["revision"]
            )
        except (KeyError, TypeError) as exc:
            raise InboxError("inbox_execution_stale") from exc
        if snapshot.get("peer_context_revision") != current["peer_context_revision"]:
            raise InboxError("inbox_context_stale")
        if snapshot != current:
            raise InboxError("inbox_execution_stale")

    def read_peer_thread(self, job_id: str) -> dict[str, Any] | None:
        job = self._job_row(job_id)
        row = self._db.execute("SELECT * FROM peer_threads WHERE peer_id=?", (job["peer_id"],)).fetchone()
        return dict(row) if row is not None else None

    def read_peer_thread_by_peer(self, peer_id: int) -> dict[str, Any] | None:
        _positive(peer_id, "inbound_peer_invalid")
        row = self._db.execute("SELECT * FROM peer_threads WHERE peer_id=?", (peer_id,)).fetchone()
        return dict(row) if row is not None else None

    def forget_missing_peer_thread(
        self,
        *,
        peer_id: int,
        expected_thread_id: str,
        expected_creation_key: str,
        expected_peer_thread_revision: int,
        expected_execution_revision: int,
        missing_thread_reviewed: bool = False,
        effects_reviewed: bool = False,
    ) -> bool:
        """Forget one reviewed old binding only while the entire queue is idle."""
        _positive(peer_id, "inbound_peer_invalid")
        _token(expected_thread_id, "inbox_thread_invalid")
        _token(expected_creation_key, "inbox_peer_thread_key_invalid")
        _positive(expected_peer_thread_revision, "inbox_revision_invalid")
        _positive(expected_execution_revision, "inbox_revision_invalid")
        if missing_thread_reviewed is not True or effects_reviewed is not True:
            raise InboxError("inbox_peer_thread_forget_review_required")
        with self._write():
            if self._meta("execution_paused") != "1":
                raise InboxError("inbox_peer_thread_forget_held")
            if int(self._meta("execution_revision")) != expected_execution_revision:
                raise InboxError("inbox_execution_conflict")
            if (
                self._db.execute("SELECT 1 FROM global_slot LIMIT 1").fetchone()
                or self._db.execute("SELECT 1 FROM events WHERE state='open' LIMIT 1").fetchone()
                or self._db.execute(
                    "SELECT 1 FROM jobs WHERE state IN ('claimed','start_intent','running','needs_review') LIMIT 1"
                ).fetchone()
                or self._db.execute("SELECT 1 FROM effects WHERE attempted=1 AND verified=0 LIMIT 1").fetchone()
            ):
                raise InboxError("inbox_peer_thread_forget_held")
            mapping = self.read_peer_thread_by_peer(peer_id)
            if (
                mapping is None
                or mapping["state"] != "ready"
                or mapping["thread_id"] != expected_thread_id
                or mapping["revision"] != expected_peer_thread_revision
                or not hmac.compare_digest(mapping["creation_key"], expected_creation_key)
            ):
                raise InboxError("inbox_peer_thread_conflict")
            removed = self._db.execute(
                "DELETE FROM peer_threads WHERE peer_id=? AND state='ready' AND thread_id=? "
                "AND revision=? AND creation_key=?",
                (peer_id, expected_thread_id, expected_peer_thread_revision, expected_creation_key),
            )
            if removed.rowcount != 1:
                raise InboxError("inbox_peer_thread_conflict")
        return True

    def prepare_peer_thread(self, *, job_id: str, lease: str, expected_revision: int) -> dict[str, Any]:
        job = self._lease(job_id, lease, expected_revision)
        if job["state"] != "claimed" or job["context_mode"] != "peer_v1":
            raise InboxError("inbox_peer_thread_state_invalid")
        self._require_execution(job)
        existing = self.read_peer_thread(job_id)
        if existing is not None:
            return {**existing, "created": False}
        key = secrets.token_urlsafe(32)
        with self._write():
            self._db.execute("INSERT INTO peer_threads VALUES (?,NULL,?,'creating',1)", (job["peer_id"], key))
        return {
            "peer_id": job["peer_id"],
            "thread_id": None,
            "creation_key": key,
            "state": "creating",
            "revision": 1,
            "created": True,
        }

    def bind_peer_thread(
        self,
        *,
        job_id: str,
        lease: str,
        expected_revision: int,
        creation_key: str,
        thread_id: str,
        effects_reviewed: bool = False,
    ) -> dict[str, Any]:
        job = self._lease(job_id, lease, expected_revision)
        _token(creation_key, "inbox_peer_thread_key_invalid")
        _token(thread_id, "inbox_thread_invalid")
        existing = self.read_peer_thread(job_id)
        if existing is None or not hmac.compare_digest(existing["creation_key"], creation_key):
            raise InboxError("inbox_peer_thread_conflict")
        if existing["state"] == "ready":
            if existing["thread_id"] != thread_id:
                raise InboxError("inbox_peer_thread_conflict")
            return existing
        if effects_reviewed is True:
            if (
                self._meta("execution_paused") != "1"
                or job["state"] != "needs_review"
                or job["execution_proof"] != "not_started"
                or job["thread_id"] is not None
                or job["turn_id"] is not None
                or self.execution_slot() is not None
                or self._db.execute(
                    "SELECT 1 FROM effects WHERE job_id=? AND (attempted=1 OR verified=1) LIMIT 1", (job_id,)
                ).fetchone()
            ):
                raise InboxError("inbox_peer_thread_recovery_required")
        else:
            if job["state"] != "claimed" or existing["state"] != "creating":
                raise InboxError("inbox_peer_thread_unknown")
            self._require_execution(job)
        if self._db.execute("SELECT 1 FROM peer_threads WHERE thread_id=?", (thread_id,)).fetchone():
            raise InboxError("inbox_peer_thread_conflict")
        with self._write():
            self._db.execute(
                "UPDATE peer_threads SET state='ready',thread_id=?,revision=revision+1 WHERE peer_id=?",
                (thread_id, job["peer_id"]),
            )
        return self.read_peer_thread(job_id) or {}

    def replace_missing_peer_thread(
        self,
        *,
        job_id: str,
        lease: str,
        expected_revision: int,
        expected_thread_id: str,
        expected_peer_thread_revision: int,
        thread_id: str,
        missing_thread_reviewed: bool = False,
        effects_reviewed: bool = False,
    ) -> dict[str, Any]:
        """Replace only an explicitly reviewed missing, never-started peer binding."""
        job = self._lease(job_id, lease, expected_revision)
        _token(expected_thread_id, "inbox_thread_invalid")
        _token(thread_id, "inbox_thread_invalid")
        _positive(expected_peer_thread_revision, "inbox_revision_invalid")
        if missing_thread_reviewed is not True or effects_reviewed is not True:
            raise InboxError("inbox_peer_thread_recovery_review_required")
        if (
            self._meta("execution_paused") != "1"
            or job["context_mode"] != "peer_v1"
            or job["state"] != "needs_review"
            or job["execution_proof"] != "not_started"
            or job["thread_id"] is not None
            or job["turn_id"] is not None
            or self.execution_slot() is not None
            or self._db.execute("SELECT 1 FROM effects WHERE job_id=? LIMIT 1", (job_id,)).fetchone()
        ):
            raise InboxError("inbox_peer_thread_recovery_required")
        if (
            thread_id == expected_thread_id
            or self._db.execute("SELECT 1 FROM peer_threads WHERE thread_id=?", (thread_id,)).fetchone()
        ):
            raise InboxError("inbox_peer_thread_conflict")
        with self._write():
            changed = self._db.execute(
                "UPDATE peer_threads SET thread_id=?,creation_key=?,revision=revision+1 "
                "WHERE peer_id=? AND state='ready' AND thread_id=? AND revision=?",
                (
                    thread_id,
                    secrets.token_urlsafe(32),
                    job["peer_id"],
                    expected_thread_id,
                    expected_peer_thread_revision,
                ),
            )
            if changed.rowcount != 1:
                raise InboxError("inbox_peer_thread_conflict")
        return self.read_peer_thread(job_id) or {}

    def mark_peer_thread_unknown(
        self, *, job_id: str, lease: str, expected_revision: int, creation_key: str
    ) -> dict[str, Any]:
        job = self._lease(job_id, lease, expected_revision)
        _token(creation_key, "inbox_peer_thread_key_invalid")
        existing = self.read_peer_thread(job_id)
        if existing is None or not hmac.compare_digest(existing["creation_key"], creation_key):
            raise InboxError("inbox_peer_thread_conflict")
        if existing["state"] == "creating":
            with self._write():
                self._db.execute(
                    "UPDATE peer_threads SET state='creation_unknown',revision=revision+1 WHERE peer_id=?",
                    (job["peer_id"],),
                )
        return self.read_peer_thread(job_id) or {}

    def finish_execution(
        self,
        *,
        job_id: str,
        lease: str,
        expected_revision: int,
        proof_kind: str,
        local_tasks_done: bool,
        thread_id: str | None = None,
        turn_id: str | None = None,
        terminal_status: str | None = None,
    ) -> dict[str, Any]:
        _token(lease, "inbox_lease_invalid")
        _positive(expected_revision, "inbox_revision_invalid")
        job = self._job_row(job_id)
        if not hmac.compare_digest(job["lease"], lease):
            raise InboxError("inbox_lease_mismatch")
        if local_tasks_done is not True:
            raise InboxError("inbox_execution_proof_invalid")
        if proof_kind == "start_rejected":
            if (
                job["state"] not in {"start_intent", "needs_review"}
                or job["turn_id"] is not None
                or thread_id != job["thread_id"]
                or not thread_id
                or turn_id is not None
                or terminal_status is not None
            ):
                raise InboxError("inbox_execution_proof_invalid")
            proof = "not_started"
        elif proof_kind == "not_started":
            if any(value is not None for value in (thread_id, turn_id, terminal_status)):
                raise InboxError("inbox_execution_proof_invalid")
            if job["thread_id"] is not None or job["turn_id"] is not None:
                raise InboxError("inbox_execution_proof_invalid")
            proof = "not_started"
        elif proof_kind == "terminal":
            if (
                not isinstance(thread_id, str)
                or not thread_id
                or not isinstance(turn_id, str)
                or not turn_id
                or (thread_id, turn_id) != (job["thread_id"], job["turn_id"])
                or not isinstance(terminal_status, str)
                or terminal_status not in {"completed", "failed", "interrupted"}
            ):
                raise InboxError("inbox_execution_proof_invalid")
            proof = terminal_status
        else:
            raise InboxError("inbox_execution_proof_invalid")
        if job["execution_proof"] is not None:
            if job["execution_proof"] != proof:
                raise InboxError("inbox_execution_proof_invalid")
            slot = self._db.execute("SELECT * FROM global_slot WHERE job_id=?", (job_id,)).fetchone()
            if slot is not None and slot["phase"] == "manual":
                if job["revision"] != expected_revision:
                    raise InboxError("inbox_revision_conflict")
                with self._write():
                    self._transition(job_id, "needs_review", reason_code="manual_execution_stopped")
                    self._db.execute("UPDATE jobs SET manual_recovery=0 WHERE job_id=?", (job_id,))
                    self._db.execute("DELETE FROM global_slot WHERE job_id=?", (job_id,))
            return self.read_job(job_id)
        if job["revision"] != expected_revision:
            raise InboxError("inbox_revision_conflict")
        slot = self._db.execute("SELECT * FROM global_slot WHERE singleton=1").fetchone()
        legacy = job["context_mode"] == "legacy" and job["state"] in ACTIVE_STATES
        if slot is None:
            if not legacy or proof_kind != "terminal":
                raise InboxError("inbox_execution_slot_mismatch")
        elif slot["job_id"] != job_id or not hmac.compare_digest(slot["lease"], lease):
            raise InboxError("inbox_execution_slot_mismatch")
        elif proof_kind == "not_started" and slot["phase"] != "claimed":
            raise InboxError("inbox_execution_proof_invalid")
        with self._write():
            self._db.execute(
                "UPDATE jobs SET execution_proof=?,proof_at=?,revision=revision+1,updated_at=? WHERE job_id=?",
                (proof, self._now(), self._now(), job_id),
            )
            if job["state"] in ACTIVE_STATES and job["state"] != "needs_review":
                self._db.execute(
                    "UPDATE jobs SET state='needs_review',reason_code='execution_stopped' WHERE job_id=?", (job_id,)
                )
            self._db.execute("DELETE FROM global_slot WHERE job_id=?", (job_id,))
            self._release_control_waiters(job["peer_id"])
        return self.read_job(job_id)

    def _transition(self, job_id: str, state: str, *, reason_code: str | None = None) -> None:
        if reason_code is not None:
            _code(reason_code)
        self._db.execute(
            "UPDATE jobs SET state=?,revision=revision+1,updated_at=?,reason_code=? WHERE job_id=?",
            (state, self._now(), reason_code, job_id),
        )

    def start_intent(self, *, job_id: str, lease: str, expected_revision: int, thread_id: str) -> dict[str, Any]:
        job = self._lease(job_id, lease, expected_revision)
        _token(thread_id, "inbox_thread_invalid")
        if job["state"] != "claimed":
            raise InboxError("inbox_start_state_invalid")
        self._require_execution(job)
        mapping = self.read_peer_thread(job_id)
        if (
            job["context_mode"] != "peer_v1"
            or mapping is None
            or mapping["state"] != "ready"
            or mapping["thread_id"] != thread_id
            or job["execution_proof"] is not None
        ):
            raise InboxError("inbox_peer_thread_conflict")
        with self._write():
            self._transition(job_id, "start_intent")
            self._db.execute("UPDATE jobs SET thread_id=? WHERE job_id=?", (thread_id, job_id))
            self._db.execute("UPDATE global_slot SET phase='start_intent' WHERE job_id=?", (job_id,))
        return self.read_job(job_id)

    begin_start = start_intent

    def bind_turn(self, *, job_id: str, lease: str, expected_revision: int, turn_id: str) -> dict[str, Any]:
        _token(turn_id, "inbox_turn_invalid")
        job = self._job_row(job_id)
        if not isinstance(lease, str) or not hmac.compare_digest(job["lease"], lease):
            raise InboxError("inbox_lease_mismatch")
        if job["turn_id"] == turn_id:
            return self.read_job(job_id)
        job = self._lease(job_id, lease, expected_revision)
        if job["state"] not in {"start_intent", "needs_review"} or job["turn_id"] is not None:
            raise InboxError("inbox_turn_state_invalid")
        slot = self._db.execute("SELECT * FROM global_slot WHERE job_id=?", (job_id,)).fetchone()
        if slot is None or slot["phase"] != "start_intent" or job["thread_id"] is None:
            raise InboxError("inbox_execution_slot_mismatch")
        with self._write():
            self._transition(job_id, "running")
            self._db.execute("UPDATE jobs SET turn_id=? WHERE job_id=?", (turn_id, job_id))
            self._db.execute("UPDATE global_slot SET phase='running' WHERE job_id=?", (job_id,))
        return self.read_job(job_id)

    def release_job(self, *, job_id: str, lease: str, expected_revision: int) -> dict[str, Any]:
        job = self._lease(job_id, lease, expected_revision)
        if job["state"] != "claimed":
            raise InboxError("inbox_release_unsafe")
        mapping = self.read_peer_thread(job_id)
        with self._write():
            if mapping is not None and mapping["state"] != "ready":
                self._transition(job_id, "needs_review", reason_code="peer_thread_creation_unknown")
                self._db.execute(
                    "UPDATE peer_threads SET state='creation_unknown',revision=revision+1 "
                    "WHERE peer_id=? AND state='creating'",
                    (job["peer_id"],),
                )
                self._db.execute(
                    "UPDATE jobs SET execution_proof='not_started',proof_at=? WHERE job_id=?", (self._now(), job_id)
                )
            else:
                self._transition(job_id, "released")
                self._db.execute("UPDATE events SET job_id=NULL WHERE job_id=?", (job_id,))
            self._db.execute("DELETE FROM global_slot WHERE job_id=? AND phase='claimed'", (job_id,))
        return self.read_job(job_id)

    release_claim = release_job

    def needs_review(self, *, job_id: str, lease: str, expected_revision: int, reason_code: str) -> dict[str, Any]:
        self._lease(job_id, lease, expected_revision)
        _code(reason_code)
        with self._write():
            self._transition(job_id, "needs_review", reason_code=reason_code)
        return self.read_job(job_id)

    mark_needs_review = needs_review

    def defer_failed_request(self, *, job_id: str, lease: str, expected_revision: int) -> dict[str, Any]:
        """Allow the next new input after a proven failure with at most an ACK.

        This does not retry old events, finish a client request, or clear an
        unknown start/send. Such cases retain their original review boundary.
        Verified acknowledgements remain attached to their original job.
        """
        job = self._lease(job_id, lease, expected_revision)
        if (
            job["state"] != "needs_review"
            or job["execution_proof"] not in {"failed", "not_started"}
            or self.execution_slot() is not None
        ):
            raise InboxError("inbox_failure_recovery_unsafe")
        if self._db.execute(
            "SELECT 1 FROM effects WHERE job_id=? AND attempted=1 AND (verified=0 OR kind<>'ack')", (job_id,)
        ).fetchone():
            raise InboxError("inbox_failure_recovery_unsafe")
        mapping = self.read_peer_thread(job_id)
        if mapping is not None and mapping["state"] != "ready":
            raise InboxError("inbox_failure_recovery_unsafe")
        with self._write():
            self._transition(job_id, "failed_waiting_input", reason_code="retry_on_new_client_input")
            # A continuation may already have arrived while the failed turn
            # was stopping. Adopt it with the original question now; waiting
            # for another record() would strand and later replay the question.
            if self._db.execute(
                "SELECT 1 FROM events WHERE peer_id=? AND state='open' AND job_id IS NULL",
                (job["peer_id"],),
            ).fetchone():
                self._db.execute("UPDATE events SET job_id=NULL WHERE job_id=? AND state='open'", (job_id,))
        return self.read_job(job_id)

    def recover_on_startup(self) -> None:
        """Return unstarted claims; retain every potentially started/effectful job."""

        jobs = self.active_jobs()
        if not jobs:
            return
        with self._write():
            for job in jobs:
                if job["state"] == "claimed":
                    mapping = self.read_peer_thread(job["job_id"])
                    if mapping is not None and mapping["state"] in {"creating", "creation_unknown"}:
                        self._db.execute(
                            "UPDATE peer_threads SET state='creation_unknown',revision=revision+1 WHERE peer_id=?",
                            (job["peer_id"],),
                        )
                        self._transition(job["job_id"], "needs_review", reason_code="peer_thread_creation_unknown")
                        self._db.execute(
                            "UPDATE jobs SET execution_proof='not_started',proof_at=? WHERE job_id=?",
                            (self._now(), job["job_id"]),
                        )
                    else:
                        self._transition(job["job_id"], "released")
                        self._db.execute("UPDATE events SET job_id=NULL WHERE job_id=?", (job["job_id"],))
                    self._db.execute("DELETE FROM global_slot WHERE job_id=? AND phase='claimed'", (job["job_id"],))
                elif job["state"] in {"start_intent", "running"}:
                    self._transition(job["job_id"], "needs_review", reason_code="restart_outcome_unknown")

    def refresh_job(
        self, *, job_id: str, lease: str, expected_revision: int, event_ids: list[str] | None = None
    ) -> dict[str, Any]:
        """Acknowledge context reread, optionally cover more exact current events."""

        job = self._lease(job_id, lease, expected_revision)
        if job["state"] not in {"claimed", "running"}:
            raise InboxError("inbox_context_state_invalid")
        self._require_execution(job)
        current = self.read_job(job_id)["event_ids"]
        extra: list[sqlite3.Row] = []
        if event_ids is not None:
            self._event_ids(event_ids)
            if not set(current).issubset(event_ids) or len(event_ids) > 5:
                raise InboxError("inbox_context_coverage_invalid")
            for event_id in event_ids:
                row = self._event_row(event_id)
                if row["peer_id"] != job["peer_id"] or row["state"] != "open" or row["job_id"] not in {None, job_id}:
                    raise InboxError("inbox_context_coverage_invalid")
                extra.append(row)
        with self._write():
            # A dry-run reservation is not an attempted external effect.  Once
            # the context is reread, retire only those harmless preparations;
            # attempted sends keep their original keys and uncertainty gates.
            self._db.execute("DELETE FROM effects WHERE job_id=? AND attempted=0", (job_id,))
            self._db.execute(
                "UPDATE jobs SET revision=revision+1,context_revision=?,updated_at=? WHERE job_id=?",
                (self._peer_revision(job["peer_id"]), self._now(), job_id),
            )
            self._db.executemany(
                "UPDATE events SET job_id=? WHERE sequence=?", [(job_id, row["sequence"]) for row in extra]
            )
        return self.read_job(job_id)

    @staticmethod
    def _event_ids(event_ids: Any) -> None:
        if not isinstance(event_ids, list) or not event_ids:
            raise InboxError("inbox_coverage_invalid")
        for event_id in event_ids:
            TelegramInbox._event_sequence(event_id)
        if len(event_ids) != len(set(event_ids)):
            raise InboxError("inbox_coverage_invalid")

    def reserve_effect(
        self,
        *,
        job_id: str,
        lease: str,
        expected_revision: int,
        kind: str,
        text_sha256: str | None,
        peer_id: int,
        reply_to_message_id: int,
        effect_slot: int = 0,
    ) -> str:
        """Persist a stable key before writing; a changed body cannot replace it."""

        job = self._lease(job_id, lease, expected_revision)
        if job["state"] != "running":
            raise InboxError("inbox_effect_state_invalid")
        self.execution_authority(job_id=job_id, lease=lease, expected_revision=expected_revision)
        if not isinstance(kind, str) or kind not in EFFECT_KINDS:
            raise InboxError("inbox_effect_kind_invalid")
        if type(effect_slot) is not int or not 0 <= effect_slot <= 31:
            raise InboxError("inbox_effect_slot_invalid")
        if text_sha256 is not None and (
            not isinstance(text_sha256, str) or SHA256_PATTERN.fullmatch(text_sha256) is None
        ):
            raise InboxError("inbox_effect_digest_invalid")
        _positive(peer_id, "inbound_peer_invalid")
        _positive(reply_to_message_id, "inbound_message_invalid")
        if peer_id != job["peer_id"]:
            raise InboxError("inbox_effect_target_invalid")
        targets = {self.resolve(event_id).message_id for event_id in self.read_job(job_id)["event_ids"]}
        if reply_to_message_id not in targets:
            raise InboxError("inbox_effect_target_invalid")
        if job["context_revision"] != self._peer_revision(peer_id):
            raise InboxError("inbox_context_stale")
        old = self._db.execute(
            "SELECT * FROM effects WHERE job_id=? AND kind=? AND context_revision=? AND effect_slot=?",
            (job_id, kind, job["context_revision"], effect_slot),
        ).fetchone()
        if old is not None:
            if (old["text_sha256"], old["peer_id"], old["reply_to_message_id"]) != (
                text_sha256,
                peer_id,
                reply_to_message_id,
            ):
                raise InboxError("inbox_effect_conflict")
            self._require_no_other_unknown_effects(job_id, str(old["effect_key"]))
            return str(old["effect_key"])
        self._require_no_other_unknown_effects(job_id)
        key = "tgjob-" + hashlib.sha256(f"{job_id}:{kind}:{job['context_revision']}:{effect_slot}".encode()).hexdigest()
        with self._write():
            self._db.execute(
                "INSERT INTO effects(effect_key,job_id,kind,context_revision,effect_slot,text_sha256,peer_id,"
                "reply_to_message_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    job_id,
                    kind,
                    job["context_revision"],
                    effect_slot,
                    text_sha256,
                    peer_id,
                    reply_to_message_id,
                    self._now(),
                ),
            )
        return key

    def _require_no_other_unknown_effects(self, job_id: str, effect_key: str | None = None) -> None:
        if self._db.execute(
            "SELECT 1 FROM effects WHERE job_id=? AND attempted=1 AND verified=0 "
            "AND (? IS NULL OR effect_key<>?) LIMIT 1",
            (job_id, effect_key, effect_key),
        ).fetchone():
            raise InboxError("inbox_effect_outcome_unknown")

    def mark_effect_intent(self, effect_key: str) -> dict[str, Any]:
        effect = self.read_effect(effect_key)
        self._require_no_other_unknown_effects(effect["job_id"], effect_key)
        if effect["attempted"]:
            return effect
        job = self._job_row(effect["job_id"])
        self.execution_authority(job_id=job["job_id"], lease=job["lease"], expected_revision=job["revision"])
        if effect["context_revision"] != self._peer_revision(effect["peer_id"]):
            raise InboxError("inbox_context_stale")
        with self._write():
            self._db.execute("UPDATE effects SET attempted=1 WHERE effect_key=?", (effect_key,))
        return self.read_effect(effect_key)

    def read_effect(self, effect_key: str) -> dict[str, Any]:
        _token(effect_key, "inbox_effect_key_invalid")
        row = self._db.execute("SELECT * FROM effects WHERE effect_key=?", (effect_key,)).fetchone()
        if row is None:
            raise InboxError("inbox_effect_unavailable")
        return dict(row)

    def verify_effect(
        self, *, effect_key: str, receipt_key: str, message_id: int, verified: bool = True
    ) -> dict[str, Any]:
        """Called by bridge only after independent exact-target effect readback."""

        effect = self.read_effect(effect_key)
        _token(receipt_key, "inbox_receipt_invalid")
        _positive(message_id, "inbound_message_invalid")
        if verified is not True:
            raise InboxError("inbox_receipt_unverified")
        if effect["verified"]:
            if effect["receipt_key"] != receipt_key or effect["sent_message_id"] != message_id:
                raise InboxError("inbox_receipt_conflict")
            return effect
        other = self._db.execute("SELECT effect_key FROM effects WHERE receipt_key=?", (receipt_key,)).fetchone()
        if other is not None:
            raise InboxError("inbox_receipt_conflict")
        with self._write():
            self._db.execute(
                "UPDATE effects SET verified=1,attempted=1,receipt_key=?,sent_message_id=?,verified_at=? WHERE effect_key=?",
                (receipt_key, message_id, self._now(), effect_key),
            )
            if effect["kind"] != "handoff":
                self._db.execute(
                    "UPDATE outgoing_observations SET origin='agent' WHERE peer_id=? AND message_id=?",
                    (effect["peer_id"], message_id),
                )
        return self.read_effect(effect_key)

    def _validate_no_reply_result(
        self, job: sqlite3.Row, event_ids: list[str], reason_code: Any, human_message_ids: Any
    ) -> bool:
        if not isinstance(reason_code, str) or reason_code not in NO_REPLY_REASONS:
            raise InboxError("inbox_no_reply_reason_invalid")
        if reason_code == "human_answered":
            if (
                not isinstance(human_message_ids, list)
                or not human_message_ids
                or any(type(value) is not int for value in human_message_ids)
                or len(human_message_ids) != len(set(human_message_ids))
            ):
                raise InboxError("inbox_human_evidence_required")
            latest = max(self.resolve(event).message_id for event in event_ids)
            known = {row["message_id"] for row in self.outgoing_context(job["peer_id"]) if row["origin"] == "human"}
            if not set(human_message_ids).issubset(known) or min(human_message_ids) <= latest:
                raise InboxError("inbox_human_evidence_required")
        elif human_message_ids:
            raise InboxError("inbox_human_evidence_invalid")
        waiting_control = reason_code in {"paused", "human_takeover"}
        if waiting_control and self.client_control(job["peer_id"])["state"] != reason_code:
            raise InboxError("inbox_client_control_invalid")
        return waiting_control

    def result(
        self,
        *,
        job_id: str,
        lease: str,
        expected_revision: int,
        outcome: str,
        event_ids: list[str],
        receipt_keys: list[str],
        reason_code: str | None = None,
        human_message_ids: list[int] | None = None,
        guidance_revision: int | None = None,
    ) -> dict[str, Any]:
        job = self._lease(job_id, lease, expected_revision)
        if job["state"] not in {"running", "needs_review"}:
            raise InboxError("inbox_result_state_invalid")
        self._event_ids(event_ids)
        if not isinstance(outcome, str) or outcome not in OUTCOMES:
            raise InboxError("inbox_outcome_invalid")
        if set(event_ids) != set(self.read_job(job_id)["event_ids"]):
            raise InboxError("inbox_coverage_invalid")
        if not isinstance(receipt_keys, list):
            raise InboxError("inbox_receipt_invalid")
        for key in receipt_keys:
            _token(key, "inbox_receipt_invalid")
        if len(receipt_keys) != len(set(receipt_keys)):
            raise InboxError("inbox_receipt_invalid")
        effects = self.read_job(job_id)["effects"]
        supplied = {
            effect["receipt_key"] for effect in effects if effect["verified"] and effect["receipt_key"] in receipt_keys
        }
        if set(receipt_keys) != supplied:
            raise InboxError("inbox_receipt_unverified")
        if any(effect["attempted"] and not effect["verified"] for effect in effects):
            raise InboxError("inbox_effect_outcome_unknown")
        kinds = {
            effect["kind"]
            for effect in effects
            if effect["receipt_key"] in supplied and effect["context_revision"] == job["context_revision"]
        }
        expected = {"answered": "result", "awaiting_client": "clarification", "handoff": "handoff"}
        waiting_control = False
        if outcome == "no_reply_needed":
            waiting_control = self._validate_no_reply_result(job, event_ids, reason_code, human_message_ids)
        else:
            if reason_code is not None:
                raise InboxError("inbox_reason_invalid")
            if expected[outcome] not in kinds:
                raise InboxError("inbox_result_receipt_required")
        if (outcome != "no_reply_needed" or reason_code == "human_answered") and job[
            "context_revision"
        ] != self._peer_revision(job["peer_id"]):
            raise InboxError("inbox_context_stale")
        if waiting_control and any(effect["attempted"] and effect["kind"] != "ack" for effect in effects):
            raise InboxError("inbox_control_result_effects_present")
        control = self.client_control(job["peer_id"])
        consume_guidance = (
            not waiting_control
            and control["guidance_pending"]
            and type(guidance_revision) is int
            and guidance_revision == control["revision"]
            and job["context_revision"] == self._peer_revision(job["peer_id"])
        )
        with self._write():
            self._transition(job_id, "waiting_control" if waiting_control else "completed", reason_code=reason_code)
            self._db.execute("UPDATE jobs SET outcome=? WHERE job_id=?", (None if waiting_control else outcome, job_id))
            if not waiting_control:
                self._db.execute(
                    "UPDATE events SET state=?,completed_at=? WHERE job_id=?",
                    ("no_reply_needed" if outcome == "no_reply_needed" else "reply_verified", self._now(), job_id),
                )
            if consume_guidance:
                self._db.execute(
                    "UPDATE client_controls SET guidance_pending=0 WHERE peer_id=? AND revision=?",
                    (job["peer_id"], guidance_revision),
                )
            if job["manual_recovery"] and job["execution_proof"] is not None:
                self._db.execute("DELETE FROM global_slot WHERE job_id=? AND phase='manual'", (job_id,))
            self._prune()
        return self.read_job(job_id)

    finish = result

    def resolve_job(
        self,
        *,
        job_id: str,
        lease: str,
        expected_revision: int,
        decision: str,
        effects_reviewed: bool = False,
    ) -> dict[str, Any]:
        job = self._lease(job_id, lease, expected_revision)
        if job["state"] != "needs_review" or effects_reviewed is not True:
            raise InboxError("inbox_resolution_review_required")
        if decision not in {"retry_no_effects", "continue_bound_turn"}:
            raise InboxError("inbox_resolution_invalid")
        if self._meta("execution_paused") != "1":
            raise InboxError("inbox_resolution_pause_required")
        if job["execution_proof"] is None:
            raise InboxError("inbox_execution_proof_required")
        if self.execution_slot() is not None:
            raise InboxError("inbox_execution_slot_mismatch")
        if self._db.execute(
            "SELECT 1 FROM effects WHERE job_id=? AND attempted=1 AND verified=0 LIMIT 1", (job_id,)
        ).fetchone():
            raise InboxError("inbox_effect_outcome_unknown")
        if decision == "continue_bound_turn":
            if job["thread_id"] is None or job["turn_id"] is None:
                raise InboxError("inbox_turn_unavailable")
            state = "running"
        else:
            if self._db.execute(
                "SELECT 1 FROM effects WHERE job_id=? AND (attempted=1 OR verified=1) LIMIT 1", (job_id,)
            ).fetchone():
                raise InboxError("inbox_retry_effects_present")
            state = "released"
        with self._write():
            self._transition(job_id, state)
            new_lease = secrets.token_urlsafe(32)
            self._db.execute(
                "UPDATE jobs SET lease=?,execution_revision=?,manual_recovery=? WHERE job_id=?",
                (new_lease, int(self._meta("execution_revision")), int(state == "running"), job_id),
            )
            if state == "released":
                self._db.execute("DELETE FROM effects WHERE job_id=? AND attempted=0 AND verified=0", (job_id,))
                self._db.execute("UPDATE events SET job_id=NULL WHERE job_id=?", (job_id,))
            else:
                self._db.execute("INSERT INTO global_slot VALUES (1,?,?,'manual')", (job_id, new_lease))
        return self.read_job(job_id)

    def duty_set(self, enabled: bool, *, at: str | None = None) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise InboxError("inbox_duty_invalid")
        now = self._now() if at is None else _timestamp(at)
        if enabled == (self._meta("duty_enabled") == "1"):
            return self.status()
        with self._write():
            if enabled:
                self._db.execute("INSERT INTO duty_windows(opened_at) VALUES (?)", (now,))
            else:
                self._db.execute("UPDATE duty_windows SET closed_at=? WHERE closed_at IS NULL", (now,))
            self._db.execute("UPDATE metadata SET value=? WHERE key='duty_enabled'", ("1" if enabled else "0",))
        return self.status()

    def set_execution_paused(self, *, paused: bool, expected_revision: int) -> dict[str, Any]:
        """Persist explicit executor control with a revision independent of intake."""

        if type(paused) is not bool:
            raise InboxError("inbox_execution_invalid")
        _positive(expected_revision, "inbox_execution_revision_invalid")
        if int(self._meta("execution_revision")) != expected_revision:
            raise InboxError("inbox_execution_revision_conflict")
        # An explicit pause also revokes a grant issued under that pause.
        # Once revoked, repeating it must leave the control revision alone.
        if paused == (self._meta("execution_paused") == "1") and (
            not paused
            or self._db.execute(
                "SELECT 1 FROM global_slot s JOIN jobs j ON j.job_id=s.job_id "
                "WHERE s.phase='manual' AND j.state NOT IN ('completed','released') "
                "AND j.execution_revision=? LIMIT 1",
                (expected_revision,),
            ).fetchone()
            is None
        ):
            return self.status()
        with self._write():
            if (
                not paused
                and self._db.execute(
                    "SELECT 1 FROM global_slot s JOIN jobs j ON j.job_id=s.job_id "
                    "WHERE s.phase='manual' AND j.state NOT IN ('completed','released') LIMIT 1"
                ).fetchone()
            ):
                raise InboxError("inbox_manual_recovery_active")
            self._db.execute("UPDATE metadata SET value=? WHERE key='execution_paused'", ("1" if paused else "0",))
            self._db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='execution_revision'")
        return self.status()

    def duty_windows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._db.execute("SELECT * FROM duty_windows ORDER BY window_id")]

    def recovery_windows(self) -> list[dict[str, Any]]:
        return [
            {**dict(row), "started_at": row["opened_at"], "ended_at": row["closed_at"]}
            for row in self._db.execute(
                "SELECT * FROM duty_windows WHERE closed_at IS NULL OR recovered_at IS NULL ORDER BY window_id"
            )
        ]

    def duty_classification(self, received_at: str, *, timestamp_precision: str = "exact") -> str:
        """Retain partially covered Telegram seconds, without rounding duty."""

        if not isinstance(timestamp_precision, str) or timestamp_precision not in {"exact", "second"}:
            raise InboxError("inbox_timestamp_precision_invalid")
        at = _timestamp(received_at)
        if timestamp_precision == "exact":
            allowed = (
                self._db.execute(
                    "SELECT 1 FROM duty_windows WHERE opened_at<=? AND (closed_at IS NULL OR closed_at>?) LIMIT 1",
                    (at, at),
                ).fetchone()
                is not None
            )
            return "allowed" if allowed else "excluded"
        start = datetime.fromisoformat(at).replace(microsecond=0)
        end = start + timedelta(seconds=1)
        windows = list(
            self._db.execute(
                "SELECT opened_at,closed_at FROM duty_windows WHERE opened_at<? "
                "AND (closed_at IS NULL OR closed_at>?) ORDER BY opened_at",
                (_timestamp(end), _timestamp(start)),
            )
        )
        if not windows:
            return "excluded"
        covered_to = start
        for window in windows:
            lower = max(start, datetime.fromisoformat(window["opened_at"]))
            upper = end if window["closed_at"] is None else min(end, datetime.fromisoformat(window["closed_at"]))
            if lower > covered_to:
                return "ambiguous"
            covered_to = max(covered_to, upper)
        return "allowed" if covered_to >= end else "ambiguous"

    def recovery_allowed(self, received_at: str, *, timestamp_precision: str = "exact") -> bool:
        return self.duty_classification(received_at, timestamp_precision=timestamp_precision) != "excluded"

    def mark_window_recovered(self, window_id: int) -> None:
        _positive(window_id, "inbox_window_invalid")
        if self._db.execute("SELECT 1 FROM duty_windows WHERE window_id=?", (window_id,)).fetchone() is None:
            raise InboxError("inbox_window_unavailable")
        with self._write():
            self._db.execute("UPDATE duty_windows SET recovered_at=? WHERE window_id=?", (self._now(), window_id))

    def checkpoint(self, peer_id: int, message_id: int | None = None, received_at: str | None = None) -> int | None:
        _positive(peer_id, "inbound_peer_invalid")
        if message_id is None:
            row = self._db.execute("SELECT message_id FROM checkpoints WHERE peer_id=?", (peer_id,)).fetchone()
            return 0 if row is None else int(row[0])
        _positive(message_id, "inbound_message_invalid")
        at = self._now() if received_at is None else _timestamp(received_at)
        with self._write():
            self._db.execute(
                "INSERT INTO checkpoints VALUES (?,?,?) ON CONFLICT(peer_id) DO UPDATE SET "
                "message_id=excluded.message_id,received_at=excluded.received_at WHERE excluded.message_id>checkpoints.message_id",
                (peer_id, message_id, at),
            )
        return None

    def advance_checkpoint(self, peer_id: int, message_id: int, received_at: str | None = None) -> None:
        self.checkpoint(peer_id, message_id, received_at)

    def checkpoints(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._db.execute("SELECT * FROM checkpoints ORDER BY peer_id")]

    def reconciliation_cursor(self) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT peer_id,message_id,received_at FROM reconciliation_cursor WHERE singleton=1"
        ).fetchone()
        return None if row is None else dict(row)

    def set_reconciliation_cursor(self, peer_id: int, message_id: int, received_at: str) -> None:
        _positive(peer_id, "inbound_peer_invalid")
        _positive(message_id, "inbound_message_invalid")
        at = _timestamp(received_at)
        new = {"peer_id": peer_id, "message_id": message_id, "received_at": at}
        if self.reconciliation_cursor() == new:
            return
        with self._write():
            self._db.execute(
                "INSERT INTO reconciliation_cursor VALUES (1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET "
                "peer_id=excluded.peer_id,message_id=excluded.message_id,received_at=excluded.received_at",
                (peer_id, message_id, at),
            )

    def clear_reconciliation_cursor(self) -> None:
        if self.reconciliation_cursor() is None:
            return
        with self._write():
            self._db.execute("DELETE FROM reconciliation_cursor")

    def _gap(self, peer_id: int, message_id: int, at: str, reason_code: str) -> None:
        self._db.execute(
            "INSERT INTO gaps VALUES (?,?,?,?) ON CONFLICT(peer_id) DO UPDATE SET "
            "message_id=excluded.message_id,received_at=excluded.received_at,reason_code=excluded.reason_code "
            "WHERE excluded.message_id<gaps.message_id",
            (peer_id, message_id, at, reason_code),
        )

    def mark_gap(self, *, peer_id: int, message_id: int, received_at: str, reason_code: str) -> None:
        _positive(peer_id, "inbound_peer_invalid")
        _positive(message_id, "inbound_message_invalid")
        _code(reason_code)
        at = _timestamp(received_at)
        with self._write():
            self._gap(peer_id, message_id, at, reason_code)

    def gaps(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._db.execute("SELECT * FROM gaps ORDER BY received_at,peer_id")]

    def clear_gap(self, *, peer_id: int, through_message_id: int) -> None:
        _positive(peer_id, "inbound_peer_invalid")
        _positive(through_message_id, "inbound_message_invalid")
        with self._write():
            self._db.execute("DELETE FROM gaps WHERE peer_id=? AND message_id<=?", (peer_id, through_message_id))

    def open_incident(self, reason_code: str) -> dict[str, Any]:
        _code(reason_code)
        old = self._db.execute(
            "SELECT * FROM incidents WHERE reason_code=? AND recovered_at IS NULL", (reason_code,)
        ).fetchone()
        if old is not None:
            return dict(old)
        with self._write():
            return self._create_incident(reason_code)

    def _create_incident(self, reason_code: str) -> dict[str, Any]:
        incident = "incident-" + secrets.token_hex(16)
        self._db.execute(
            "INSERT INTO incidents(incident_id,reason_code,opened_at) VALUES (?,?,?)",
            (incident, reason_code, self._now()),
        )
        return self.read_incident(incident)

    def read_incident(self, incident_id: str) -> dict[str, Any]:
        _token(incident_id, "inbox_incident_invalid")
        row = self._db.execute("SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        if row is None:
            raise InboxError("inbox_incident_unavailable")
        return dict(row)

    def recover_incident(self, incident_id: str) -> dict[str, Any]:
        old = self.read_incident(incident_id)
        if old["recovered_at"] is None:
            with self._write():
                return self._recover_incident(old)
        return old

    def _recover_incident(self, incident: dict[str, Any]) -> dict[str, Any]:
        if incident["recovered_at"] is None:
            self._db.execute(
                "UPDATE incidents SET recovered_at=? WHERE incident_id=?", (self._now(), incident["incident_id"])
            )
        return self.read_incident(incident["incident_id"])

    def pending_notifications(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in self._db.execute(
            "SELECT * FROM incidents WHERE opened_receipt IS NULL OR (recovered_at IS NOT NULL AND recovery_receipt IS NULL) "
            "ORDER BY opened_at,rowid"
        ):
            if row["opened_receipt"] is None:
                result.append(
                    {
                        **dict(row),
                        "notification_id": row["incident_id"] + "-error",
                        "kind": "error",
                        "code": row["reason_code"],
                    }
                )
            if (
                row["recovered_at"] is not None
                and row["recovery_receipt"] is None
                and row["opened_receipt"] is not None
            ):
                result.append(
                    {
                        **dict(row),
                        "notification_id": row["incident_id"] + "-recovery",
                        "kind": "recovery",
                        "code": row["reason_code"],
                    }
                )
        return result

    def set_health(self, component: str, state: str, error_code: str | None = None) -> dict[str, Any]:
        if not isinstance(component, str) or component not in {"codex", "telegram", "intake"}:
            raise InboxError("inbox_health_component_invalid")
        if not isinstance(state, str) or state not in {
            "running",
            "ready",
            "waiting_thread",
            "held",
            "paused",
            "offline",
            "connected",
        }:
            raise InboxError("inbox_health_state_invalid")
        if error_code is not None:
            _code(error_code)
            if len(error_code) > 80:
                raise InboxError("inbox_reason_invalid")
        if state in {"held", "offline"} and error_code is None:
            error_code = component + "_" + state
        old = self._db.execute("SELECT * FROM health WHERE component=?", (component,)).fetchone()
        if old is not None and (old["state"], old["error_code"]) == (state, error_code):
            return self.health()
        with self._write():
            # Incident transitions and their health pointer must commit together.
            # Public incident methods own standalone transactions, so use only
            # private mutations inside this transaction.
            cause = _health_incident_cause(component, error_code)
            incident: dict[str, Any] | None = None
            if old is not None and old["incident_id"] is not None:
                previous = self.read_incident(old["incident_id"])
                if _health_incident_cause(component, old["error_code"]) == cause:
                    # Preserve the legacy ID and receipts for equivalent causes.
                    incident = previous
                else:
                    self._recover_incident(previous)
            if cause is not None and (incident is None or incident["recovered_at"] is not None):
                existing = self._db.execute(
                    "SELECT * FROM incidents WHERE reason_code=? AND recovered_at IS NULL", (cause,)
                ).fetchone()
                incident = dict(existing) if existing is not None else self._create_incident(cause)
            self._db.execute(
                "INSERT INTO health VALUES (?,?,?,?) ON CONFLICT(component) DO UPDATE SET "
                "state=excluded.state,error_code=excluded.error_code,incident_id=excluded.incident_id",
                (component, state, error_code, None if incident is None else incident["incident_id"]),
            )
        return self.health()

    def health(self) -> dict[str, Any]:
        return {
            row["component"]: {"state": row["state"], "error_code": row["error_code"]}
            for row in self._db.execute("SELECT * FROM health ORDER BY component")
        }

    def notification_verified(self, notification_id: str, receipt_key: str) -> None:
        _token(notification_id, "inbox_notification_invalid")
        if notification_id.endswith("-error"):
            incident, recovery = notification_id.removesuffix("-error"), False
        elif notification_id.endswith("-recovery"):
            incident, recovery = notification_id.removesuffix("-recovery"), True
        else:
            raise InboxError("inbox_notification_invalid")
        self.mark_notification(incident_id=incident, recovery=recovery, receipt_key=receipt_key)

    def mark_notification(self, *, incident_id: str, recovery: bool, receipt_key: str) -> None:
        old = self.read_incident(incident_id)
        _token(receipt_key, "inbox_receipt_invalid")
        if type(recovery) is not bool or (recovery and (old["recovered_at"] is None or old["opened_receipt"] is None)):
            raise InboxError("inbox_notification_state_invalid")
        field = "recovery_receipt" if recovery else "opened_receipt"
        if old[field] is not None:
            if old[field] != receipt_key:
                raise InboxError("inbox_receipt_conflict")
            return
        with self._write():
            self._db.execute(f"UPDATE incidents SET {field}=? WHERE incident_id=?", (receipt_key, incident_id))

    def _prune(self) -> int:
        cutoff = _timestamp(datetime.fromisoformat(self._now()) - timedelta(seconds=self._retention_seconds))
        # Recovery may encounter an old completed locator again until history
        # has durably covered it. Keep its dedup evidence in the meantime.
        removed = self._db.execute(
            "DELETE FROM events WHERE completed_at IS NOT NULL AND completed_at<? "
            "AND NOT EXISTS (SELECT 1 FROM global_slot WHERE global_slot.job_id=events.job_id) "
            "AND EXISTS (SELECT 1 FROM checkpoints WHERE checkpoints.peer_id=events.peer_id "
            "AND checkpoints.message_id>=events.message_id)",
            (cutoff,),
        ).rowcount
        self._db.execute(
            "DELETE FROM jobs WHERE state IN ('completed','released','retired','failed_waiting_input') AND updated_at<? "
            "AND NOT EXISTS (SELECT 1 FROM global_slot WHERE global_slot.job_id=jobs.job_id) "
            "AND NOT EXISTS (SELECT 1 FROM events WHERE events.job_id=jobs.job_id)",
            (cutoff,),
        )
        self._db.execute("DELETE FROM duty_windows WHERE closed_at<? AND recovered_at IS NOT NULL", (cutoff,))
        self._db.execute("DELETE FROM incidents WHERE recovered_at<? AND recovery_receipt IS NOT NULL", (cutoff,))
        self._db.execute(
            "DELETE FROM outgoing_observations WHERE observed_at<? AND NOT EXISTS "
            "(SELECT 1 FROM jobs WHERE jobs.peer_id=outgoing_observations.peer_id "
            "AND jobs.state IN ('claimed','start_intent','running','needs_review'))",
            (cutoff,),
        )
        return removed

    def prune(self) -> int:
        with self._write():
            return self._prune()
