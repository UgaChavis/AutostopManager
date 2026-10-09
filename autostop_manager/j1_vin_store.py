"""Private, expiring VIN research state; persistent J1 holds only queue pointers.

The state directory must be on tmpfs. Each job has its own database so expiry
removes its journal, full-text index and parser artifacts together. This module
does not configure or mount filesystems and never writes customer memory.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import time
from typing import Any
from uuid import uuid4

from . import j1_research as general

PROFILE = "vin"
SCHEMA = "autostop.j1.vin_research.v1"
REPORT_SCHEMA = "autostop.j1.vin_report.v1"
MAX_QUERIES = 12
MAX_DOCUMENTS = 60
MAX_BROWSER_PAGES = 6
MAX_OCR_PAGES = 12
MAX_SECONDS = 300.0
MAX_STORE_BYTES = 128 * 1024 * 1024
RETENTION_SECONDS = 24 * 60 * 60
ENGINES = ("bing", "yahoo", "duckduckgo")
DEFAULT_FIELDS = (
    "make",
    "model",
    "model_year",
    "production_date",
    "engine",
    "displacement",
    "power",
    "transmission",
    "body",
    "drive",
    "market",
    "plant",
    "trim",
    "options",
    "history",
)
IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")


class VinJobError(Exception):
    """Static diagnostic only: never carry input, source text or a URL."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def enabled() -> bool:
    return os.environ.get("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "0") == "1"


def error(code: str, job_id: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "schema": SCHEMA, "error": {"code": code}}
    if IDENTIFIER.fullmatch(job_id):
        result["job_id"] = job_id
    return result


def _is_tmpfs(path: Path) -> bool:
    """Check the longest mount match in this process's mount namespace."""

    try:
        matches = []
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
            fields = line.split()
            separator = fields.index("-")
            mount = Path(fields[4].replace("\\040", " ").replace("\\134", "\\"))
            if path == mount or mount in path.parents:
                matches.append((len(str(mount)), fields[separator + 1]))
        return bool(matches and max(matches)[1] == "tmpfs")
    except (OSError, ValueError, IndexError):
        return False


def _private_directory(path: Path) -> None:
    details = path.lstat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid() or details.st_mode & 0o077:
        raise VinJobError("vin_store_permissions_invalid")
    if path.resolve() != path.absolute():
        raise VinJobError("vin_store_permissions_invalid")


def runtime_root(*, create: bool = False) -> Path:
    value = os.environ.get("AUTOSTOP_J1_VIN_CACHE_DIR", "/run/autostop-j1-vin")
    path = Path(value)
    if not path.is_absolute() or not _is_tmpfs(path):
        raise VinJobError("vin_tmpfs_required")
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    _private_directory(path)
    return path


def used_bytes(root: Path) -> int:
    total = 0
    for directory, _, files in os.walk(root, followlinks=False):
        for name in files:
            details = (Path(directory) / name).lstat()
            if stat.S_ISREG(details.st_mode):
                total += details.st_size
    return total


def check_capacity(root: Path, additional: int = 0) -> None:
    if used_bytes(root) + max(0, additional) > MAX_STORE_BYTES:
        raise VinJobError("vin_storage_limit_reached")
    if shutil.disk_usage(root).free < max(1_048_576, additional):
        raise VinJobError("vin_storage_unavailable")


@contextmanager
def start_lock() -> Iterator[Path]:
    root = runtime_root(create=True)
    fd = os.open(root / "start.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        details = os.fstat(fd)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid() or details.st_mode & 0o077:
            raise VinJobError("vin_store_permissions_invalid")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield root
    finally:
        os.close(fd)


def is_vin_job(job_id: str) -> bool:
    if not isinstance(job_id, str) or not IDENTIFIER.fullmatch(job_id) or not general._db_path().is_file():
        return False
    try:
        with general._db(readonly=True) as conn:
            row = conn.execute("SELECT profile FROM jobs WHERE id=?", (job_id,)).fetchone()
            return row is not None and row[0] == PROFILE
    except (OSError, sqlite3.Error):
        return False


def set_stub(job_id: str, status: str, reason: str = "") -> None:
    with general._db() as conn:
        conn.execute(
            "UPDATE jobs SET status=?,error=?,updated_at=? WHERE id=? AND profile=?",
            (status, reason, general._utcnow(), job_id, PROFILE),
        )
        conn.commit()


def job_directory(job_id: str) -> Path:
    if not IDENTIFIER.fullmatch(job_id):
        raise VinJobError("job_id_invalid")
    try:
        root = runtime_root()
    except FileNotFoundError as exc:
        raise VinJobError("vin_ephemeral_state_lost") from exc
    path = root / job_id
    if not path.exists():
        raise VinJobError("vin_ephemeral_state_lost")
    _private_directory(path)
    return path


def _database(path: Path, *, create: bool = False) -> sqlite3.Connection:
    database = path / "research.sqlite3"
    if database.exists():
        details = database.lstat()
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid() or details.st_mode & 0o077:
            raise VinJobError("vin_store_permissions_invalid")
    flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT | os.O_EXCL if create else 0)
    try:
        fd = os.open(database, flags, 0o600)
    except FileNotFoundError as exc:
        raise VinJobError("vin_ephemeral_state_lost") from exc
    os.close(fd)
    # URI mode=rw also prevents SQLite itself from recreating an unlinked file.
    conn = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA secure_delete=ON")
    conn.execute(f"PRAGMA max_page_count={MAX_STORE_BYTES // 4096}")
    return conn


def metadata(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        row = conn.execute("SELECT payload FROM metadata WHERE id=1").fetchone()
        value = json.loads(row[0]) if row else None
        if (
            not isinstance(value, dict)
            or type(value.get("expires_at")) not in (int, float)
            or not math.isfinite(value["expires_at"])
        ):
            raise VinJobError("vin_ephemeral_state_lost")
        return value
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise VinJobError("vin_ephemeral_state_lost") from exc


def write_metadata(conn: sqlite3.Connection, value: dict[str, Any]) -> None:
    conn.execute("UPDATE metadata SET payload=? WHERE id=1", (json.dumps(value, ensure_ascii=False),))


def _delete_job(root: Path, job_id: str) -> None:
    path = root / job_id
    _private_directory(path)
    shutil.rmtree(path)


@contextmanager
def connect(job_id: str, *, transaction: bool = False) -> Iterator[sqlite3.Connection]:
    path = job_directory(job_id)
    conn = _database(path)
    try:
        if transaction:
            conn.execute("BEGIN IMMEDIATE")
        current = metadata(conn)
        expiry = current["expires_at"]
        if time.time() >= expiry:
            raise VinJobError("vin_job_expired")
        yield conn
        if transaction:
            if time.time() >= expiry:
                raise VinJobError("vin_job_expired")
            check_capacity(path.parent)
            conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _failed_worker_info(job_id: str) -> tuple[str, float] | None:
    if not general._db_path().is_file():
        return None
    with general._db(readonly=True) as conn:
        row = conn.execute(
            "SELECT status,error,updated_at FROM jobs WHERE id=? AND profile=?", (job_id, PROFILE)
        ).fetchone()
    if row is None or row["status"] != "failed":
        return None
    reason = row["error"]
    reason = reason if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,64}", reason) else "vin_collection_failed"
    return reason, datetime.fromisoformat(row["updated_at"]).timestamp()


def _reconcile_failed_worker(job_id: str) -> None:
    failure = _failed_worker_info(job_id)
    if failure is None:
        return
    reason, failed_at = failure
    with connect(job_id) as conn:
        current = metadata(conn)
        if current["collection_status"] in {"failed", "cancelled"} and not current["inflight"]:
            return
    with connect(job_id, transaction=True) as conn:
        current = metadata(conn)
        if current["inflight"]:
            # Recovery may happen much later; idle time after the failure
            # marker is not active collection time and must not consume budget.
            elapsed = max(0.0, failed_at - current["operation_started_at"])
            current["used_seconds"] += min(elapsed, max(0.0, MAX_SECONDS - current["used_seconds"]))
        current.update(collection_status="failed", inflight=False, operation_started_at=0.0, stop_reason=reason)
        for table in ("queries", "documents", "ocr_requests"):
            conn.execute(f"UPDATE {table} SET status='failed',error=? WHERE status='running'", (reason,))
        write_metadata(conn, current)


def reconcile_failed_job(job_id: str) -> None:
    """Recover stopped worker state once storage is available, without network.

    The queue's failed marker is written only after the worker stops. A full
    tmpfs may prevent that terminal update in the private DB; retain the marker
    and apply the same capacity-checked update before the next API operation.
    """

    if _failed_worker_info(job_id) is not None:
        with start_lock():
            _reconcile_failed_worker(job_id)


def fail_worker(job_id: str, reason: str) -> None:
    """Publish a terminal marker and attempt bounded private-state recovery."""

    try:
        with start_lock():
            # Exclude enqueue while publishing failure and reconciling. Keep
            # this pointer if the private transaction cannot commit on tmpfs.
            set_stub(job_id, "failed", reason)
            _reconcile_failed_worker(job_id)
    except (OSError, ValueError, sqlite3.Error, VinJobError):
        set_stub(job_id, "failed", reason)


def _fallback_expiry(path: Path) -> float:
    """A damaged job may use its de-identified queue creation time for cleanup."""
    if general._db_path().is_file():
        with general._db(readonly=True) as conn:
            row = conn.execute("SELECT created_at FROM jobs WHERE id=? AND profile=?", (path.name, PROFILE)).fetchone()
        if row:
            return datetime.fromisoformat(row[0]).timestamp() + RETENTION_SECONDS
    # A fresh directory without a committed stub can be initialization in progress.
    return path.stat().st_mtime + RETENTION_SECONDS


def prune() -> None:
    try:
        root = runtime_root()
    except (OSError, VinJobError):
        return
    for path in root.iterdir():
        if not IDENTIFIER.fullmatch(path.name):
            continue
        try:
            _private_directory(path)
            try:
                conn = _database(path)
                try:
                    expiry = min(metadata(conn)["expires_at"], _fallback_expiry(path))
                finally:
                    conn.close()
                reason = "vin_job_expired"
            except (sqlite3.Error, ValueError, VinJobError):
                expiry = _fallback_expiry(path)
                reason = "vin_ephemeral_state_lost"
            if time.time() < expiry:
                continue
            _delete_job(root, path.name)
            if general._db_path().is_file():
                set_stub(path.name, "failed", reason)
        except (OSError, sqlite3.Error, ValueError, VinJobError):
            # Fail closed; never include the damaged state's contents in logs.
            continue


def find_receipt(root: Path, key: str, vin: str) -> str:
    for path in root.iterdir():
        if not IDENTIFIER.fullmatch(path.name):
            continue
        try:
            with connect(path.name) as conn:
                current = metadata(conn)
                if current["start_key"] != key:
                    continue
                if current["vin"] != vin:
                    raise VinJobError("idempotency_conflict")
                if not is_vin_job(path.name):
                    raise VinJobError("vin_ephemeral_state_lost")
                return path.name
        except VinJobError as exc:
            if exc.code != "vin_ephemeral_state_lost":
                raise
            if is_vin_job(path.name):
                set_stub(path.name, "failed", exc.code)
    return ""


def create(vin: str, key: str, queries: list[str]) -> str:
    with start_lock() as root:
        prune()
        existing = find_receipt(root, key, vin)
        if existing:
            return existing
        check_capacity(root)
        job_id = uuid4().hex
        path = root / job_id
        with general._db() as queue:
            queue.execute("BEGIN IMMEDIATE")
            if queue.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1").fetchone():
                raise VinJobError("j1_busy")
            path.mkdir(mode=0o700)
            try:
                initialize(path, job_id, vin, key, queries)
                now = general._utcnow()
                queue.execute(
                    """INSERT INTO jobs(id,objective,max_pages,status,created_at,updated_at,profile,automotive_context)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (job_id, "Independent VIN public research", MAX_DOCUMENTS, "queued", now, now, PROFILE, ""),
                )
                queue.commit()
            except BaseException:
                queue.rollback()
                _delete_job(root, job_id)
                raise
        return job_id


def initialize(path: Path, job_id: str, vin: str, key: str, queries: list[str]) -> None:
    conn = _database(path, create=True)
    try:
        conn.executescript(
            """
            CREATE TABLE metadata(id INTEGER PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE queries(id INTEGER PRIMARY KEY,query TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',error TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,providers TEXT NOT NULL DEFAULT '[]');
            CREATE TABLE documents(id TEXT PRIMARY KEY,url TEXT UNIQUE NOT NULL,title TEXT NOT NULL DEFAULT '',
                final_url TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',error TEXT NOT NULL DEFAULT '',kind TEXT NOT NULL DEFAULT '',
                method TEXT NOT NULL DEFAULT '',source_class TEXT NOT NULL,source_tier TEXT NOT NULL,
                source_basis TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,retrieved_at REAL,
                duplicate_of TEXT NOT NULL DEFAULT '',digest TEXT NOT NULL DEFAULT '',
                raw_file TEXT NOT NULL DEFAULT '',limitations TEXT NOT NULL DEFAULT '[]',
                page_count INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE pages(document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                page INTEGER NOT NULL,text TEXT NOT NULL,match_evidence_id TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(document_id,page));
            CREATE VIRTUAL TABLE pages_fts USING fts5(document_id UNINDEXED,page UNINDEXED,text);
            CREATE TABLE facts(claim_id TEXT PRIMARY KEY,field TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE receipts(key TEXT PRIMARY KEY,request TEXT NOT NULL,response TEXT NOT NULL);
            CREATE TABLE ocr_requests(document_id TEXT NOT NULL REFERENCES documents(id),page INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',error TEXT NOT NULL DEFAULT '',attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(document_id,page));
            """
        )
        now = time.time()
        value = {
            "job_id": job_id,
            "vin": vin,
            "start_key": key,
            "created_at": now,
            "expires_at": now + RETENTION_SECONDS,
            "collection_status": "queued",
            "analysis_status": "awaiting_agent",
            "revision": 0,
            "used_seconds": 0.0,
            "browser_pages": 0,
            "ocr_pages": 0,
            "inflight": False,
            "operation_started_at": 0.0,
            "cancel_requested": False,
            "stop_reason": "",
            "blocked_engines": [],
            "blocked_hosts": [],
            "fields": list(DEFAULT_FIELDS),
        }
        conn.execute("INSERT INTO metadata(id,payload) VALUES(1,?)", (json.dumps(value),))
        conn.executemany("INSERT INTO queries(query) VALUES(?)", ((query,) for query in queries))
        conn.commit()
        check_capacity(path.parent)
    finally:
        conn.close()


def runtime_status() -> dict[str, Any]:
    result: dict[str, Any] = {"enabled": enabled(), "ready": False}
    if not result["enabled"]:
        return result
    try:
        root = runtime_root()
        check_capacity(root)
        return {**result, "ready": True, "retention_seconds": RETENTION_SECONDS}
    except (OSError, VinJobError) as exc:
        code = exc.code if isinstance(exc, VinJobError) else "vin_store_unavailable"
        return {**result, "error": code}
