#!/usr/bin/env python3
"""Private component-consistent backups; never restore or restart applications."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import time
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


FORMAT = "autostop_complete_backup_v1"
OWNED_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{12}\Z")
RESERVE = 8 * 1024**3
HELPER_SHA256 = "5c86ce33d54271c6848ff293e610c1af0be39f543404508b2281a49c3c399d9a"
DAILY_NAME = re.compile(r"autostop24-\d{8}T\d{6}Z\.dump\Z")
CRM_TRANSIENT = {"logs", "searxng", "backups", "maintenance-backups", "maintenance"}
SQLITE_SUFFIXES = {".sqlite3", ".sqlite", ".db"}
CRM_SQLITES = {"change_feed.sqlite3"}
CRM_AGENT_FILES = {
    "system_prompt.md",
    "memory.md",
    "tasks.json",
    "schedules.json",
    "status.json",
    "runs.jsonl",
    "actions.jsonl",
}
CRM_LOCKS = {
    "state.lock",
    "state.json.lock",
    "shared_files_index.lock",
    "manager_structure.lock",
    "mcp-oauth-state.lock",
    "users.lock",
    "settings.lock",
    "app.instance.lock",
    "printing/completion_act_forms.lock",
    "audit/.audit-archive.lock",
}
REQUIRED_ARTIFACTS = {
    "crm-files.tar.gz",
    "crm-agent.tar.gz",
    "crm-sqlite/change_feed.sqlite3",
    "manager.sqlite3",
    "scheduler.sqlite3",
    "roles.tar.gz",
    "store.dump",
    "uploads.tar.gz",
    "photos.tar.gz",
}


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Layout:
    root: Path = Path("/var/backups/autostop-complete")
    crm: Path = Path("/opt/autostopcrm/data")
    manager: Path = Path("/opt/AutostopManager/data/autostop_manager.sqlite3")
    scheduler: Path = Path("/var/lib/autostop-manager-scheduler/registry.sqlite3")
    roles: Path = Path("/var/lib/autostop-manager/roles/M2")
    uploads: Path = Path("/var/lib/docker/volumes/autostop-app_uploads_data/_data")
    photos: Path = Path("/var/lib/docker/volumes/autostop-app_quote_request_vin_photos_data/_data")
    daily: Path = Path("/var/backups/autostop24/database")
    pg_lock: Path = Path("/run/lock/autostop24-db-backup.lock")
    revision: Path = Path("/opt/autostop-manager-releases/current/REVISION")
    helper: Path = Path("/usr/local/sbin/autostop24-db-backup")
    store_source: Path = Path("/opt/autostopapp")


def stamp() -> str:
    return datetime.now(UTC).isoformat()


def restore_metadata(path: Path) -> dict:
    info = path.stat()
    return {"uid": info.st_uid, "gid": info.st_gid, "mode": f"{stat.S_IMODE(info.st_mode):04o}"}


def digest(path: Path, guard=lambda: None) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            guard()
            result.update(block)
    return result.hexdigest()


def private(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct_type or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise BackupError("backup_private_path_invalid")


def sync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def lock(path: Path, *, timeout: float = 3.0, create: bool = True):
    # Matches CRM ProcessFileLock's Linux flock protocol without importing
    # application-writable Python into this root service.
    flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
    fd = os.open(path, flags, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BackupError("backup_lock_busy") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def entries(root: Path, *, crm: bool = False, exclude: set[str] | None = None) -> dict[str, Path]:
    if root.is_symlink() or not root.is_dir():
        raise BackupError("backup_source_directory_invalid")
    result = {}
    for parent, directories, files in os.walk(root, followlinks=False):
        relative = Path(parent).relative_to(root)
        if crm and relative == Path("."):
            directories[:] = [name for name in directories if name not in CRM_TRANSIENT and name != "agent"]
        for name in directories + files:
            path = Path(parent) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise BackupError("backup_source_special_file")
        for name in files:
            path = Path(parent) / name
            relative_name = path.relative_to(root).as_posix()
            if exclude and relative_name in exclude:
                continue
            if crm and (
                relative_name in CRM_LOCKS
                or relative_name in {".agent-gateway-maintenance", ".agent-gateway-maintenance.lock"}
                or any(
                    relative_name == database + sidecar
                    for database in CRM_SQLITES
                    for sidecar in ("-wal", "-shm", "-journal")
                )
            ):
                continue
            result[relative_name] = path
    return result


@contextmanager
def source_identity(source: Path):
    previous_uid, previous_gid = os.geteuid(), os.getegid()
    info = source.stat()
    try:
        if previous_uid == 0:
            os.setegid(info.st_gid)
            os.seteuid(info.st_uid)
        elif previous_uid != info.st_uid:
            raise BackupError("backup_sqlite_source_owner_mismatch")
        yield
    finally:
        if os.geteuid() != previous_uid:
            os.seteuid(previous_uid)
        if os.getegid() != previous_gid:
            os.setegid(previous_gid)


def sqlite_backup(source: Path, destination: Path, guard=lambda: None, *, validate: bool = True) -> None:
    if source.is_symlink() or not source.is_file():
        raise BackupError("backup_sqlite_source_invalid")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(sqlite3.connect(destination, timeout=3)) as target:
        # Open the root-private destination before changing effective identity.
        # Its rollback journal stays in memory while SQLite reads the live source
        # as its owner, preventing new root-owned WAL/SHM in app directories.
        target.execute("PRAGMA journal_mode=MEMORY")
        with source_identity(source), closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=3)) as origin:
            origin.backup(target, pages=256, progress=lambda *_: guard(), sleep=0.05)
            # A source WAL database preserves its journal mode in online backup.
            # Publish a standalone database, with no required snapshot sidecars.
        target.execute("PRAGMA journal_mode=DELETE")
        target.commit()
    destination.chmod(0o600)
    if validate:
        sqlite_check(destination, guard)


def sqlite_check(path: Path, guard=lambda: None) -> None:
    guard()
    with closing(sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)) as connection:
        connection.set_progress_handler(lambda: guard() or 0, 10000)
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BackupError("backup_sqlite_integrity_failed")
    guard()


class GuardedReader:
    def __init__(self, handle, guard):
        self.handle, self.guard = handle, guard

    def read(self, size: int = -1, /) -> bytes:
        self.guard()
        result = self.handle.read(size)
        self.guard()
        return result


def archive(
    source: Path, destination: Path, *, crm: bool = False, exclude: set[str] | None = None, guard=lambda: None
) -> dict[str, int]:
    files = entries(source, crm=crm, exclude=exclude)
    regular = {name: path for name, path in files.items() if not (crm and name in CRM_SQLITES)}
    before = {name: digest(path, guard) for name, path in regular.items()}
    with tarfile.open(destination, "w:gz", compresslevel=1) as target:
        for name, path in sorted(regular.items()):
            guard()
            with path.open("rb") as handle:
                target.addfile(target.gettarinfo(path, arcname=name), GuardedReader(handle, guard))
    destination.chmod(0o600)
    after_files = entries(source, crm=crm, exclude=exclude)
    after = {name: digest(path, guard) for name, path in after_files.items() if not (crm and name in CRM_SQLITES)}
    if before != after or archive_hashes(destination, guard) != before:
        raise BackupError("backup_source_changed")
    return {"files": len(before), "source_bytes": sum(path.stat().st_size for path in regular.values())}


def archive_hashes(path: Path, guard=lambda: None) -> dict[str, str]:
    result = {}
    with tarfile.open(path, "r|gz") as source:
        for member in source:
            guard()
            name = Path(member.name)
            if not member.isfile() or name.is_absolute() or ".." in name.parts or member.name in result:
                raise BackupError("backup_archive_member_invalid")
            handle = source.extractfile(member)
            if handle is None:
                raise BackupError("backup_archive_member_invalid")
            hasher = hashlib.sha256()
            while chunk := handle.read(1024 * 1024):
                guard()
                hasher.update(chunk)
            result[member.name] = hasher.hexdigest()
    return result


def run(args: list[str], *, stdin=None) -> None:
    result = subprocess.run(args, stdin=stdin, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1200)
    if result.returncode:
        raise BackupError("backup_postgres_command_failed")


class Postgres:
    def __init__(self):
        self.canonical_attempted = False
        self.canonical_succeeded = False

    def check_helper(self, layout: Layout) -> None:
        info = layout.helper.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or not info.st_mode & 0o100:
            raise BackupError("backup_canonical_helper_unsafe")
        if digest(layout.helper) != HELPER_SHA256:
            raise BackupError("backup_canonical_helper_changed")

    def check(self, layout: Layout) -> dict:
        self.check_helper(layout)
        result = {}
        template = (
            '{"id":{{json .Id}},"image":{{json .Image}},"started_at":{{json .State.StartedAt}},'
            '"running":{{json .State.Running}},"mounts":{{json .Mounts}},'
            '"revision":{{json (index .Config.Labels "org.opencontainers.image.revision")}}}'
        )
        for name in ["autostopcrm", "autostop-app", "autostop-db"]:
            observed = subprocess.run(
                ["docker", "inspect", "--format", template, name], capture_output=True, text=True, timeout=15
            )
            if observed.returncode:
                raise BackupError("backup_container_inspect_failed")
            result[name] = json.loads(observed.stdout)
            # Docker mount order is unstable; retain every field in each record.
            result[name]["mounts"] = sorted(result[name]["mounts"], key=lambda mount: json.dumps(mount, sort_keys=True))
            if result[name].get("running") is not True:
                raise BackupError("backup_container_not_running")
        health = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", "autostop-db"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if health.returncode or health.stdout.strip() != "healthy":
            raise BackupError("backup_postgres_unhealthy")
        expected = {"/var/data/uploads": layout.uploads, "/var/data/quote-request-vin-photos": layout.photos}
        mount_paths = {
            m["Destination"]: Path(m["Source"]) for m in result["autostop-app"]["mounts"] if m["Type"] == "volume"
        }
        if any(mount_paths.get(key) != value for key, value in expected.items()):
            raise BackupError("backup_store_mounts_invalid")
        crm_mounts = {
            m["Destination"]: Path(m["Source"]) for m in result["autostopcrm"]["mounts"] if m["Type"] == "bind"
        }
        if crm_mounts.get("/home/autostop/.minimal-kanban") != layout.crm:
            raise BackupError("backup_crm_mount_invalid")
        git = subprocess.run(
            ["git", "-C", str(layout.store_source), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=15
        )
        if git.returncode or not re.fullmatch(r"[0-9a-f]{40}", git.stdout.strip()):
            raise BackupError("backup_store_source_revision_invalid")
        result["store_source_revision"] = git.stdout.strip()
        return result

    def helper(self, layout: Layout, guard) -> None:
        self.check_helper(layout)
        self.canonical_attempted = True
        process = subprocess.Popen(
            [str(layout.helper)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
        deadline = time.monotonic() + 1200
        try:
            while process.poll() is None:
                guard()
                if time.monotonic() > deadline:
                    raise BackupError("backup_postgres_timeout")
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=1)
            if process.returncode:
                raise BackupError("backup_postgres_command_failed")
            self.canonical_succeeded = True
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
            process.wait()

    def dump(self, layout: Layout, destination: Path, guard) -> str:
        private(layout.daily, directory=True)
        with lock(layout.pg_lock, timeout=0):
            before = daily_files(layout.daily)
        # The canonical helper takes its own flock and retains its seven points.
        # Holding that flock here would deadlock or force the helper to exit75.
        self.helper(layout, guard)
        with lock(layout.pg_lock, timeout=0):
            after = daily_files(layout.daily)
            fresh = [path for path, identity in after.items() if before.get(path) != identity]
            if len(fresh) != 1:
                raise BackupError("backup_fresh_canonical_dump_ambiguous")
            source = fresh[0]
            private(source)
            if source.stat().st_dev != destination.parent.stat().st_dev:
                raise BackupError("backup_canonical_dump_filesystem_mismatch")
            self.verify(source)
            if daily_files(layout.daily).get(source) != after[source]:
                raise BackupError("backup_canonical_dump_changed")
            os.link(source, destination)
            sync(destination)
            return source.name

    def verify(self, dump: Path) -> None:
        for options in (["--list"], ["--file=/dev/null", "--no-owner", "--no-privileges"]):
            with dump.open("rb") as source:
                run(["docker", "exec", "-i", "autostop-db", "pg_restore", *options], stdin=source)


def daily_files(directory: Path) -> dict[Path, tuple[int, int, int, int]]:
    result = {}
    for path in directory.iterdir():
        if DAILY_NAME.fullmatch(path.name):
            private(path)
            info = path.stat()
            result[path] = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
    return result


def verify(directory: Path, postgres: Postgres) -> dict:
    private(directory, directory=True)
    manifest_path = directory / "manifest.json"
    private(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict) or (
        manifest.get("format") != FORMAT
        or manifest.get("complete") is not True
        or not isinstance(manifest.get("id"), str)
        or not OWNED_ID.fullmatch(manifest["id"])
        or not isinstance(manifest.get("created_at"), str)
        or not isinstance(manifest.get("artifacts"), dict)
    ):
        raise BackupError("backup_manifest_invalid")
    if not REQUIRED_ARTIFACTS.issubset(manifest.get("artifacts", {})):
        raise BackupError("backup_manifest_coverage_missing")
    created = datetime.fromisoformat(manifest["created_at"])
    if created.utcoffset() != UTC.utcoffset(created):
        raise BackupError("backup_manifest_invalid")
    for name, expected in manifest["artifacts"].items():
        if (
            not isinstance(name, str)
            or not isinstance(expected, dict)
            or type(expected.get("bytes")) is not int
            or expected["bytes"] < 0
            or not isinstance(expected.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected["sha256"])
        ):
            raise BackupError("backup_manifest_invalid")
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise BackupError("backup_manifest_invalid")
        path = directory / relative
        if not path.resolve().is_relative_to(directory.resolve()):
            raise BackupError("backup_manifest_invalid")
        private(path)
        if path.stat().st_size != expected["bytes"] or digest(path) != expected["sha256"]:
            raise BackupError("backup_hash_mismatch")
        if path.suffix in SQLITE_SUFFIXES:
            sqlite_check(path)
        elif name.endswith(".tar.gz"):
            names = archive_hashes(path)
            if name == "crm-files.tar.gz" and "state.json" not in names:
                raise BackupError("backup_manifest_coverage_missing")
            if name == "crm-agent.tar.gz" and not CRM_AGENT_FILES.issubset(names):
                raise BackupError("backup_manifest_coverage_missing")
    postgres.verify(directory / "store.dump")
    return manifest


class Backup:
    def __init__(self, layout: Layout | None = None, *, reserve: int = RESERVE, retention: int = 3, postgres=None):
        self.layout, self.reserve, self.retention = layout or Layout(), reserve, retention
        self.postgres = postgres or Postgres()
        if retention < 1 or retention > 30 or reserve < 0:
            raise BackupError("backup_policy_invalid")

    def daily(self, *, runtime_supported: bool = True) -> dict:
        try:
            if not runtime_supported:
                raise BackupError("backup_supported_sqlite_runtime_required")
            result = self.create()
        except (BackupError, OSError, ValueError, sqlite3.Error, tarfile.TarError, subprocess.SubprocessError) as exc:
            error = str(exc) if isinstance(exc, BackupError) else "backup_operation_failed"
            if not self.postgres.canonical_attempted:
                # Preserve the pre-existing nightly DB backup even when full-copy
                # capacity, CRM, or another component fails. Never retry a helper
                # that was already attempted; its ordinary policy remains intact.
                with suppress(BackupError, OSError, ValueError, subprocess.SubprocessError):
                    self.postgres.helper(self.layout, lambda: None)
            result = {"ok": False, "error": error}
        result["canonical_attempted"] = self.postgres.canonical_attempted
        result["canonical_succeeded"] = self.postgres.canonical_succeeded
        return result

    def guard(self) -> None:
        if shutil.disk_usage(self.layout.root.parent).free < self.reserve:
            raise BackupError("backup_capacity_exhausted")
        if (self.layout.crm / ".agent-gateway-maintenance").exists():
            raise BackupError("backup_deploy_in_progress")

    def plan(self) -> dict:
        layout = self.layout
        file_bytes = sum(path.stat().st_size for path in entries(layout.crm, crm=True).values())
        file_bytes += layout.manager.stat().st_size + layout.scheduler.stat().st_size
        for path in [layout.uploads, layout.photos, layout.roles]:
            file_bytes += sum(file.stat().st_size for file in entries(path).values())
        file_bytes += sum(
            file.stat().st_size for file in entries(layout.crm / "agent", exclude={"agent.lock"}).values()
        )
        dumps = list(daily_files(layout.daily))
        if not dumps:
            raise BackupError("backup_postgres_size_baseline_missing")
        latest = max(dumps, key=lambda path: path.stat().st_mtime)
        data_estimate = file_bytes + 64 * 1024**2
        transient = latest.stat().st_size * 13 // 10
        estimate = data_estimate + transient
        free = shutil.disk_usage(layout.root.parent).free
        return {
            "estimated_new_bytes": estimate,
            "estimated_non_postgres_bytes": data_estimate,
            "estimated_canonical_transient_bytes": transient,
            "postgres_link_additional_bytes": 0,
            "source_file_bytes": file_bytes,
            "postgres_size_baseline_bytes": latest.stat().st_size,
            "reserve_bytes": self.reserve,
            "free_bytes": free,
            "capacity_ready": free >= self.reserve + estimate,
            "retention": self.retention,
            "capacity_for_additional_runs_without_prune": max(0, (free - self.reserve - transient) // data_estimate),
            "retention_capacity_ready_without_prune": free >= self.reserve + transient + data_estimate * self.retention,
            "steady_peak_capacity_ready": free >= self.reserve + transient + data_estimate * (self.retention + 1),
        }

    def capture_agent(self, temporary: Path) -> dict:
        started = stamp()
        agent_directory = self.layout.crm / "agent"
        agent_metadata = restore_metadata(agent_directory)
        # Status/task writers use this native lock. Public prompt/memory
        # writers can run independently, so full byte stability is also
        # required. Never hold this flock together with state.lock.
        with lock(agent_directory / "agent.lock", create=False):
            agent_started = time.monotonic()
            agent_deadline = agent_started + 8

            def agent_guard():
                self.guard()
                if time.monotonic() > agent_deadline:
                    raise BackupError("backup_crm_agent_lock_budget_exceeded")

            agent_info = archive(
                agent_directory, temporary / "crm-agent.tar.gz", exclude={"agent.lock"}, guard=agent_guard
            )
            agent_guard()
            agent_locked_seconds = time.monotonic() - agent_started
        return {
            "started_at": started,
            "completed_at": stamp(),
            **agent_info,
            "restore_path": "agent/",
            "lock_seconds": agent_locked_seconds,
            "directory_restore_metadata": agent_metadata,
        }

    def create(self) -> dict:
        layout = self.layout
        self.guard()
        identity = self.postgres.check(layout)
        plan = self.plan()
        if not plan["capacity_ready"]:
            raise BackupError("backup_capacity_insufficient")
        if layout.root.is_symlink():
            raise BackupError("backup_private_path_invalid")
        layout.root.mkdir(mode=0o700, exist_ok=True)
        private(layout.root, directory=True)
        revision = layout.revision.read_text().strip()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise BackupError("backup_manager_revision_invalid")
        with lock(layout.root / ".backup.lock", timeout=0):
            backup_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:12]
            temporary = Path(tempfile.mkdtemp(prefix=".partial-", dir=layout.root))
            components = {}
            try:
                started = stamp()
                before = {
                    key: {name: digest(file, self.guard) for name, file in entries(path).items()}
                    for key, path in [("uploads", layout.uploads), ("photos", layout.photos)]
                }
                canonical_dump = self.postgres.dump(layout, temporary / "store.dump", self.guard)
                for name, source in [("uploads", layout.uploads), ("photos", layout.photos)]:
                    archive(source, temporary / (name + ".tar.gz"), guard=self.guard)
                for name, source in [("uploads", layout.uploads), ("photos", layout.photos)]:
                    after = {key: digest(file, self.guard) for key, file in entries(source).items()}
                    if (
                        after != before[name]
                        or archive_hashes(temporary / (name + ".tar.gz"), self.guard) != before[name]
                    ):
                        raise BackupError("backup_store_files_changed_during_dump")
                components["store"] = {
                    "started_at": started,
                    "completed_at": stamp(),
                    "files_stable_across_dump": True,
                    "canonical_dump": canonical_dump,
                    "dump_linked": True,
                }
                components["crm_agent"] = self.capture_agent(temporary)
                started = stamp()
                # Compression and copied-byte verification can be slow. Prepare
                # the stable regular-file archive before blocking CRM writers.
                crm_info = archive(layout.crm, temporary / "crm-files.tar.gz", crm=True, guard=self.guard)
                archived_crm = archive_hashes(temporary / "crm-files.tar.gz", self.guard)
                # State metadata is committed under this exact CRM flock. Bytes
                # are written before upload metadata and removed after tombstones.
                # The whole-tree hashes additionally guard independent writers.
                with lock(layout.crm / "state.lock", create=False):
                    lock_started = time.monotonic()
                    deadline = lock_started + 8

                    def crm_guard():
                        self.guard()
                        if time.monotonic() > deadline:
                            raise BackupError("backup_crm_lock_budget_exceeded")

                    before_crm = {
                        name: digest(path, crm_guard)
                        for name, path in entries(layout.crm, crm=True).items()
                        if name not in CRM_SQLITES
                    }
                    if before_crm != archived_crm:
                        raise BackupError("backup_crm_files_changed_before_sqlite_snapshot")
                    sqlite_files = {
                        name: path for name, path in entries(layout.crm, crm=True).items() if name in CRM_SQLITES
                    }
                    sqlite_metadata = {name: restore_metadata(path) for name, path in sqlite_files.items()}
                    for name, path in sqlite_files.items():
                        sqlite_backup(path, temporary / "crm-sqlite" / name, crm_guard, validate=False)
                    after_crm = {
                        name: digest(path, crm_guard)
                        for name, path in entries(layout.crm, crm=True).items()
                        if name not in CRM_SQLITES
                    }
                    if after_crm != archived_crm:
                        raise BackupError("backup_crm_files_changed_during_sqlite_snapshot")
                    crm_guard()
                    locked_seconds = time.monotonic() - lock_started
                for name in sqlite_files:
                    sqlite_check(temporary / "crm-sqlite" / name, self.guard)
                components["crm"] = {
                    "started_at": started,
                    "completed_at": stamp(),
                    **crm_info,
                    "sqlite_files": len(sqlite_files),
                    "sqlite_restore_metadata": sqlite_metadata,
                    "lock_seconds": locked_seconds,
                }
                for name, source in [("manager", layout.manager), ("scheduler", layout.scheduler)]:
                    started = stamp()
                    source_metadata = restore_metadata(source)
                    sqlite_backup(source, temporary / (name + ".sqlite3"), self.guard)
                    components[name] = {
                        "started_at": started,
                        "completed_at": stamp(),
                        "sqlite_restore_metadata": source_metadata,
                    }
                for name, source in [("roles", layout.roles)]:
                    started = stamp()
                    info = archive(source, temporary / (name + ".tar.gz"), guard=self.guard)
                    components[name] = {"started_at": started, "completed_at": stamp(), **info}
                artifacts = {}
                for path in temporary.rglob("*"):
                    if path.is_file():
                        path.chmod(0o600)
                        with path.open("rb") as handle:
                            os.fsync(handle.fileno())
                        artifacts[path.relative_to(temporary).as_posix()] = {
                            "bytes": path.stat().st_size,
                            "sha256": digest(path),
                        }
                manifest = {
                    "format": FORMAT,
                    "id": backup_id,
                    "complete": True,
                    "created_at": stamp(),
                    "global_atomic_snapshot": False,
                    "manager_revision": revision,
                    "container_images": {
                        name: value["image"] for name, value in identity.items() if isinstance(value, dict)
                    },
                    "crm_revision": identity["autostopcrm"]["revision"],
                    "store_source_revision": identity["store_source_revision"],
                    "container_fingerprint_sha256": hashlib.sha256(
                        json.dumps(identity, sort_keys=True).encode()
                    ).hexdigest(),
                    "capacity_plan": plan,
                    "components": components,
                    "artifacts": artifacts,
                }
                manifest_path = temporary / "manifest.json"
                manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n")
                manifest_path.chmod(0o600)
                sync(manifest_path)
                verify(temporary, self.postgres)
                self.guard()
                if layout.revision.read_text().strip() != revision:
                    raise BackupError("backup_runtime_revision_changed")
                if self.postgres.check(layout) != identity:
                    raise BackupError("backup_container_identity_changed")
                sync(temporary)
                published = layout.root / backup_id
                temporary.rename(published)
                sync(layout.root)
                self.prune(published)
                return {
                    "ok": True,
                    "backup": str(published),
                    "artifact_count": len(artifacts),
                    "bytes": sum(value["bytes"] for value in artifacts.values()),
                    "non_postgres_bytes": sum(
                        value["bytes"] for name, value in artifacts.items() if name != "store.dump"
                    ),
                    "plan": plan,
                }
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)

    def prune(self, published: Path) -> None:
        owned = []
        for path in self.layout.root.iterdir():
            if OWNED_ID.fullmatch(path.name) and path.is_dir() and not path.is_symlink():
                try:
                    manifest = verify(path, self.postgres)
                except (BackupError, OSError, ValueError, sqlite3.Error, tarfile.TarError, subprocess.SubprocessError):
                    continue
                if manifest["id"] == path.name:
                    owned.append((manifest["created_at"], path))
        for _, path in sorted(owned, reverse=True)[self.retention :]:
            if path != published:
                shutil.rmtree(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["plan", "create", "daily", "verify"])
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if os.geteuid() != 0:
            raise BackupError("backup_root_required")
        if args.operation == "create" and sqlite3.sqlite_version_info < (3, 51, 3):
            raise BackupError("backup_supported_sqlite_runtime_required")
        backup = Backup()
        if args.operation == "plan":
            backup.postgres.check(backup.layout)
            result = backup.plan()
            result["runtime_checks_ok"] = True
            result["sqlite_runtime"] = sqlite3.sqlite_version
            result["canonical_helper_sha256"] = HELPER_SHA256
        elif args.operation == "create":
            result = backup.create()
        elif args.operation == "daily":
            result = backup.daily(runtime_supported=sqlite3.sqlite_version_info >= (3, 51, 3))
        else:
            if args.backup is None:
                raise BackupError("backup_path_required")
            manifest = verify(args.backup, backup.postgres)
            result = {"ok": True, "artifact_count": len(manifest["artifacts"])}
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("ok", True) else 1
    except (BackupError, OSError, ValueError, sqlite3.Error, tarfile.TarError, subprocess.SubprocessError) as exc:
        # Exception text may contain paths, customer filenames or provider data.
        code = str(exc) if isinstance(exc, BackupError) else "backup_operation_failed"
        print(json.dumps({"ok": False, "error": code}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
