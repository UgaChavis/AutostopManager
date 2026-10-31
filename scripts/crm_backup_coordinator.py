#!/usr/bin/env python3
"""CRM backup coordinator; uninstalled until all snapshot and recovery checks pass.

Only the named CRM container and Manager scheduler are managed. Operations that
may outlive the caller run in a separate systemd unit while holding an operation
lock; killing the main backup service must not kill an in-progress stop request.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, UTC

ROOT = Path("/var/lib/autostop-manager/crm-backup")
STATUS = Path("/var/lib/autostop-manager/roles/M2/crm-backup-status.md")
CONTAINER = "autostopcrm"
SCHEDULER = "autostop-manager-scheduler.service"
DATA = "/opt/autostopcrm/data"
EXCLUDED = frozenset({"backups", "maintenance-backups", "logs", "searxng"})
DEPLOY_LOCK = Path("/opt/autostopcrm/.autostop-deploy.lock")
SNAPSHOT = Path(__file__).with_name("crm-snapshot.py")
TIMER = "autostop-crm-workstation-backup.timer"
# Must be set to the reviewed, installed CRM revision with the complete writer
# drain before this uninstalled coordinator is allowed to stop CRM.
APPROVED_CRM_REVISION = ""
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
ATTEMPT = re.compile(r"^[0-9a-f]{32}$")


class Uncertain(RuntimeError):
    pass


def run(*args, timeout=30):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=timeout)
    if result.returncode:
        raise Uncertain("command failed: " + Path(args[0]).name)
    return result.stdout.strip()


def durable(path, data):
    """Publish and sync new bytes; never follow a pre-existing temporary name."""
    path = Path(path)
    fd, temp = tempfile.mkstemp(prefix=".crm-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def save(path, record):
    durable(path, (json.dumps(record, sort_keys=True) + "\n").encode())


def status(path, record):
    allowed = {"pending", "running", "ok", "failed", "unknown"}

    def stage(key):
        return record.get(key) if record.get(key) in allowed else "unknown"

    names = {
        "pending": "ожидается",
        "running": "выполняется",
        "ok": "успех",
        "failed": "ошибка",
        "unknown": "не подтверждён",
    }
    attempt = record.get("attempt", "")
    if not ATTEMPT.fullmatch(attempt):
        attempt = "нет"
    started = record.get("started_at", "неизвестно")
    if not isinstance(started, str) or len(started) > 40 or "\n" in started:
        started = "неизвестно"
    delivery_at = record.get("delivery_at", "неизвестно")
    if not isinstance(delivery_at, str) or len(delivery_at) > 40 or "\n" in delivery_at:
        delivery_at = "неизвестно"
    note = "; удаление старых копий отложено" if record.get("delivery_note") == "rotation_pending" else ""
    try:
        load_state = run("systemctl", "show", TIMER, "-p", "LoadState", "--value")
        active_state = run("systemctl", "show", TIMER, "-p", "ActiveState", "--value")
        timer_state = (
            "не установлен" if load_state == "not-found" else "включён" if active_state == "active" else "не активен"
        )
    except (Uncertain, OSError, subprocess.SubprocessError):
        timer_state = "не подтверждён"
    text = (
        f"# Резервная копия CRM\n\nРасписание: 02:00 Asia/Krasnoyarsk; таймер {timer_state}.\n"
        "Назначение: DESKTOP-7RAOHT7, C:\\Users\\9860606\\Desktop\\Резерв CRM.\n"
        f"Попытка: {attempt}; начало: {started}.\nКопия: {names[stage('copy')]}.\n"
        f"Возврат служб: {names[stage('restore')]}.\n"
        f"Передача на ПК: {names[stage('delivery')]}; время: {delivery_at}{note}.\n"
    )
    durable(path, text.encode())


class Lock:
    def __init__(self, path, *, wait=True):
        self.path, self.wait = path, wait
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
                raise Uncertain("unsafe lock file")
            fcntl.flock(self.fd, fcntl.LOCK_EX | (0 if self.wait else fcntl.LOCK_NB))
        except BaseException:
            os.close(self.fd)
            raise
        return self

    def __exit__(self, *_):
        os.close(self.fd)


class System:
    def boot(self):
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def daemon(self):
        value = run("systemctl", "show", "docker.service", "-p", "InvocationID", "--value")
        if not value:
            raise Uncertain("Docker daemon identity unavailable")
        return value

    def crm(self):
        fmt = (
            "{{.Id}}|{{.State.Status}}|{{.State.StartedAt}}|{{.State.FinishedAt}}|"
            "{{.State.ExitCode}}|{{.State.OOMKilled}}|{{.RestartCount}}|"
            "{{if .State.Health}}{{.State.Health.Status}}{{end}}|"
            '{{index .Config.Labels "org.opencontainers.image.revision"}}'
        )
        parts = run("docker", "inspect", "--format", fmt, CONTAINER).split("|")
        if len(parts) != 9 or not HEX64.fullmatch(parts[0]) or not HEX40.fullmatch(parts[8]):
            raise Uncertain("invalid CRM identity or revision")
        return dict(
            zip(
                ("id", "state", "started", "finished", "exit", "oom", "restarts", "health", "revision"),
                parts,
                strict=True,
            )
        )

    def scheduler(self):
        definition = run("systemctl", "cat", SCHEDULER)
        state = run("systemctl", "show", SCHEDULER, "-p", "ActiveState", "--value")
        invocation = run("systemctl", "show", SCHEDULER, "-p", "InvocationID", "--value")
        if state not in ("active", "inactive", "failed"):
            raise Uncertain("scheduler state transitioning")
        return {"identity": hashlib.sha256(definition.encode()).hexdigest(), "state": state, "invocation": invocation}

    def stop(self, kind, identity):
        if kind == "scheduler":
            run("systemctl", "stop", SCHEDULER, timeout=None)
            result = run("systemctl", "show", SCHEDULER, "-p", "Result", "--value")
            if result != "success" or self.scheduler()["state"] != "inactive":
                raise Uncertain("scheduler stop was not clean")
        else:
            # Never send SIGKILL merely because graceful draining is slow.
            run("docker", "stop", "--time", "-1", identity, timeout=None)

    def start(self, kind, identity):
        if kind == "scheduler":
            run("systemctl", "start", SCHEDULER, timeout=None)
        else:
            run("docker", "start", identity, timeout=None)

    def healthy(self, kind, identity):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if kind == "crm":
                value = self.crm()
                if value["id"] != identity:
                    raise Uncertain("CRM identity changed")
                if value["state"] == "running" and value["health"] == "healthy":
                    return
            else:
                value = self.scheduler()
                if value["identity"] != identity:
                    raise Uncertain("scheduler definition changed")
                if value["state"] == "active":
                    return
            time.sleep(1)
        raise Uncertain("service health was not confirmed")

    def launch(self, attempt, operation, kind):
        name = f"crm-backup-{attempt}-{operation}-{kind}.service"
        run(
            "systemd-run",
            "--quiet",
            "--no-block",
            "--unit=" + name,
            "--service-type=oneshot",
            "--remain-after-exit",
            "-p",
            "TimeoutStartSec=infinity",
            sys.executable,
            str(Path(__file__).resolve()),
            "worker",
            attempt,
            operation,
            kind,
        )


def load(path):
    # The directory and state file are root-private; reject symlinks at the file.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd) as stream:
        record = json.load(stream)
    if not ATTEMPT.fullmatch(record.get("attempt", "")):
        raise Uncertain("invalid attempt record")
    return record


def amend(root, attempt, fields=None, resource=None):
    with Lock(root / "operation.lock"):
        record = load(root / "attempt.json")
        if record["attempt"] != attempt:
            raise Uncertain("attempt identity changed")
        (record[resource] if resource else record).update(fields or {})
        save(root / "attempt.json", record)
        return record


def refresh(root, status_path):
    # Every writer renders from durable state under a common short lock. No
    # network or archive reads are performed while holding it.
    with Lock(root / "status.lock"):
        record = load(root / "attempt.json")
        try:
            fd = os.open(root / "delivery-status.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(fd) as stream:
                delivery = json.load(stream)
            if (
                set(delivery) == {"id", "status", "at", "error_code"}
                and delivery["id"] == record["attempt"]
                and delivery["status"] in {"pending", "running", "ok", "failed", "unknown"}
            ):
                record["delivery"] = delivery["status"]
                record["delivery_at"] = delivery["at"]
                record["delivery_note"] = delivery["error_code"]
        except (FileNotFoundError, ValueError, TypeError):
            pass
        status(status_path, record)


def worker(root, attempt, operation, kind, system=None):
    """Own exactly one operation; journal is checked again inside its lock."""
    system = system or System()
    path = root / "attempt.json"
    with Lock(root / "operation.lock"):
        r = load(path)
        if r["attempt"] != attempt or r.get("phase") != f"{kind}_{operation}_intent":
            return  # A recovery cancelled a worker that had not begun.
        item = r[kind]
        actual = system.crm() if kind == "crm" else system.scheduler()
        if (actual["id"] if kind == "crm" else actual["identity"]) != item["identity"]:
            raise Uncertain("resource identity changed before operation")
        if operation == "stop":
            if kind == "crm" and (
                actual["state"] != "running"
                or actual["started"] != item["started"]
                or actual["restarts"] != item["restarts"]
            ):
                raise Uncertain("CRM generation changed before stop")
            if kind == "scheduler" and (actual["state"] != "active" or actual["invocation"] != item["invocation"]):
                raise Uncertain("scheduler generation changed before stop")
        r["phase"] = f"{kind}_{operation}_running"
        save(path, r)
        try:
            if operation == "stop":
                system.stop(kind, item["identity"])
            else:
                system.start(kind, item["identity"])
                system.healthy(kind, item["identity"])
        except Exception:
            # A failed CLI may still have issued a request. Recovery must not
            # assume that a request was cancelled when its client returned.
            r["phase"] = f"{kind}_{operation}_unknown"
            save(path, r)
            raise
        item["phase"] = "stopped" if operation == "stop" else "restored"
        r["phase"] = f"{kind}_{operation}_done"
        save(path, r)


def begin_operation(root, record, kind, operation, system):
    path = root / "attempt.json"
    with Lock(root / "operation.lock"):
        current = load(path)
        if current["attempt"] != record["attempt"]:
            raise Uncertain("attempt identity changed")
        current["phase"] = f"{kind}_{operation}_intent"
        save(path, current)
    # Unit is separate from the backup service's cgroup and is never killed on
    # a coordinator timeout. If it has not started, recovery cancels via phase.
    system.launch(record["attempt"], operation, kind)
    return wait_operation(root, record["attempt"], kind, operation)


def wait_operation(root, attempt, kind, operation):
    # The worker holds this lock until the dockerd/systemd request has returned.
    # Do not overtake a worker that systemd has queued but has not started yet.
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            with Lock(root / "operation.lock", wait=False):
                r = load(root / "attempt.json")
                if r["attempt"] != attempt:
                    raise Uncertain("attempt identity changed")
                if r["phase"] == f"{kind}_{operation}_done":
                    return r
                if r["phase"] != f"{kind}_{operation}_intent":
                    raise Uncertain("operation outcome not confirmed")
        except BlockingIOError:
            pass
        time.sleep(0.2)
    raise Uncertain("operation did not complete within its deadline")


def same_stopped(before, after):
    return (
        after["id"] == before["id"]
        and after["state"] == "exited"
        and after["started"] == before["started"]
        and after["restarts"] == before["restarts"]
        and after["exit"] == "0"
        and after["oom"] == "false"
        and after["finished"] not in ("", "0001-01-01T00:00:00Z")
    )


def recover(root, status_path, system=None, *, interrupted=False):
    system = system or System()
    path = root / "attempt.json"
    with Lock(root / "operation.lock"):
        r = load(path)
        phase = r["phase"]
        # Invalidate any worker that did not acquire the operation lock. A
        # worker killed while running leaves an unknown request and cannot be
        # treated as finished unless the host or daemon has since restarted.
        boot_restarted = system.boot() != r["boot"]
        daemon_restarted = system.daemon() != r["daemon"]
        if phase.endswith(("_running", "_unknown")):
            resolved = boot_restarted or (phase.startswith("crm_") and daemon_restarted)
            if not resolved:
                r["restore"] = "unknown"
                save(path, r)
                refresh(root, status_path)
                raise Uncertain("in-flight operation outcome unknown")
        if interrupted and r["copy"] == "running":
            r["copy"] = "unknown"
        r["phase"] = "recovering"
        save(path, r)
        refresh(root, status_path)
    for kind in ("crm", "scheduler"):
        item = r[kind]
        if not item["initially_active"]:
            if kind == "scheduler" and system.scheduler()["state"] == "active":
                raise Uncertain("initially inactive scheduler is unexpectedly active")
            continue
        if kind == "crm":
            current = system.crm()
            if current["id"] != item["identity"]:
                raise Uncertain("CRM identity changed")
            active = current["state"] == "running"
        else:
            current = system.scheduler()
            if current["identity"] != item["identity"]:
                raise Uncertain("scheduler identity changed")
            active = current["state"] == "active"
            if system.crm()["health"] != "healthy":
                raise Uncertain("CRM not yet healthy")
        if active:
            system.healthy(kind, item["identity"])
            r = amend(root, r["attempt"], {"phase": "restored"}, resource=kind)
        else:
            r = begin_operation(root, r, kind, "start", system)
    r = amend(root, r["attempt"], {"phase": "complete", "restore": "ok"})
    refresh(root, status_path)
    return r


def validate_stopped(initial, stopped, later):
    if not same_stopped(initial, stopped):
        raise Uncertain("CRM did not stop cleanly")
    keys = ("id", "state", "started", "finished", "exit", "oom", "restarts", "revision")
    if any(stopped[key] != later[key] for key in keys):
        raise Uncertain("CRM changed during snapshot")


def file_info(path):
    digest = hashlib.sha256()
    size = 0
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        while data := stream.read(1024 * 1024):
            size += len(data)
            digest.update(data)
    if not size:
        raise Uncertain("empty snapshot")
    return size, digest.hexdigest()


def publish(root, path, attempt):
    """Retain the old pending generation unless a new pointer is durable."""
    if not ATTEMPT.fullmatch(attempt):
        raise Uncertain("invalid attempt ID")
    size, digest = file_info(path)
    with Lock(root / "pending.lock"):
        return _publish_locked(root, path, attempt, size, digest)


def _publish_locked(root, path, attempt, size, digest):
    pending = root / "pending.json"
    try:
        old = load_pending(pending)
    except FileNotFoundError:
        old = None
    target = root / ("crm-" + attempt + ".tar.gz")
    # Hard link preserves the fully fsynced inode and cannot overwrite a file.
    os.link(path, target, follow_symlinks=False)
    # Remove our private staging name before publishing the pointer so the
    # destination has exactly one link when the delivery process opens it.
    path.unlink()
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    value = {"id": attempt, "file": target.name, "sha256": digest, "size": size}
    save(pending, value)
    if load_pending(pending) != value or file_info(target) != (size, digest):
        raise Uncertain("published snapshot not confirmed")
    if old and old != value:
        old_path = root / old["file"]
        try:
            if file_info(old_path) == (old["size"], old["sha256"]):
                old_path.unlink()
                directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except FileNotFoundError:
            pass
    return value


def load_pending(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd) as stream:
        value = json.load(stream)
    if (
        set(value) != {"id", "file", "sha256", "size"}
        or not ATTEMPT.fullmatch(value["id"])
        or value["file"] != "crm-" + value["id"] + ".tar.gz"
        or not HEX64.fullmatch(value["sha256"])
        or type(value["size"]) is not int
        or value["size"] <= 0
    ):
        raise Uncertain("invalid pending pointer")
    return value


def snapshot_command(output, initial):
    command = [
        sys.executable,
        str(SNAPSHOT),
        "--data-dir",
        DATA,
        "--container-id",
        initial["id"],
        "--output",
        str(output),
        "--crm-revision",
        initial["revision"],
    ]
    result = subprocess.run(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=None
    )
    if result.returncode:
        raise Uncertain("CRM snapshot failed")


def make_root(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not root.is_dir() or root.is_symlink() or info.st_uid != 0 or info.st_mode & 0o077:
        raise Uncertain("backup state directory is not root-private")


def check_space(root, data=DATA):
    # This is deliberately conservative: the compressed archive cannot be
    # assumed to be smaller than the input, and the old pending copy remains.
    source = Path(data)
    if source.is_symlink() or not source.is_dir():
        raise Uncertain("CRM data directory is unavailable")
    total = 0
    for directory, dirs, files in os.walk(source, followlinks=False):
        if Path(directory) == source:
            dirs[:] = [name for name in dirs if name not in EXCLUDED]
            files = [name for name in files if name not in EXCLUDED]
        for name in dirs + files:
            info = (Path(directory) / name).lstat()
            if stat.S_ISLNK(info.st_mode):
                raise Uncertain("symlink in CRM data")
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
            elif not stat.S_ISDIR(info.st_mode):
                raise Uncertain("unsupported CRM data entry")
    # The snapshotter may simultaneously hold the archive and SQLite copies.
    if shutil.disk_usage(root).free < 2 * total + total // 8 + 2 * 1024 * 1024 * 1024:
        raise Uncertain("insufficient free space")


def backup(
    root=ROOT,
    status_path=STATUS,
    system=None,
    deploy_lock=DEPLOY_LOCK,
    data=DATA,
    approved_revision=APPROVED_CRM_REVISION,
):
    system = system or System()
    make_root(root)
    path = root / "attempt.json"
    with Lock(root / "coordinator.lock", wait=False), Lock(deploy_lock, wait=False):
        if path.exists():
            old = load(path)
            if old.get("phase") != "complete":
                raise Uncertain("previous attempt requires recovery")
            (root / (".staged-" + old["attempt"] + ".tar.gz")).unlink(missing_ok=True)
        check_space(root, data)
        crm = system.crm()
        scheduler = system.scheduler()
        if not HEX40.fullmatch(approved_revision) or crm["revision"] != approved_revision:
            raise Uncertain("installed CRM revision is not approved for backup")
        if crm["state"] != "running" or crm["health"] != "healthy":
            raise Uncertain("CRM was not initially healthy")
        r = {
            "attempt": uuid.uuid4().hex,
            "started_at": datetime.now(UTC).isoformat(),
            "boot": system.boot(),
            "daemon": system.daemon(),
            "phase": "prepared",
            "copy": "pending",
            "restore": "pending",
            "delivery": "pending",
            "crm": {
                "identity": crm["id"],
                "started": crm["started"],
                "restarts": crm["restarts"],
                "initially_active": True,
                "phase": "untouched",
            },
            "scheduler": {
                "identity": scheduler["identity"],
                "invocation": scheduler["invocation"],
                "initially_active": scheduler["state"] == "active",
                "phase": "untouched",
            },
        }
        save(path, r)
        refresh(root, status_path)
        staged = None
        copy_error = None
        try:
            if r["scheduler"]["initially_active"]:
                r = begin_operation(root, r, "scheduler", "stop", system)
            r = begin_operation(root, r, "crm", "stop", system)
            stopped = system.crm()
            if not same_stopped(crm, stopped):
                raise Uncertain("CRM did not stop cleanly")
            r = amend(root, r["attempt"], {"copy": "running"})
            refresh(root, status_path)
            staged = root / (".staged-" + r["attempt"] + ".tar.gz")
            snapshot_command(staged, crm)
            validate_stopped(crm, stopped, system.crm())
            if system.boot() != r["boot"] or system.daemon() != r["daemon"]:
                raise Uncertain("host or Docker daemon restarted during snapshot")
        except Exception as exc:  # noqa: BLE001 - restore services after every snapshot failure.
            copy_error = exc
            r = amend(root, r["attempt"], {"copy": "failed"})
            refresh(root, status_path)
        try:
            r = recover(root, status_path, system)
        except Exception:
            r = amend(root, r["attempt"], {"restore": "unknown"})
            refresh(root, status_path)
            raise
        if copy_error:
            if staged is not None:
                staged.unlink(missing_ok=True)
            raise copy_error
        try:
            publish(root, staged, r["attempt"])
            r = amend(root, r["attempt"], {"copy": "ok"})
            refresh(root, status_path)
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
        return r


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "recover", "worker", "refresh"))
    parser.add_argument("attempt", nargs="?")
    parser.add_argument("operation", nargs="?", choices=("stop", "start"))
    parser.add_argument("resource", nargs="?", choices=("crm", "scheduler"))
    args = parser.parse_args()
    try:
        if args.action == "worker":
            if not args.attempt or not ATTEMPT.fullmatch(args.attempt) or not args.operation or not args.resource:
                raise Uncertain("invalid worker arguments")
            worker(ROOT, args.attempt, args.operation, args.resource)
        elif args.action == "refresh":
            refresh(ROOT, STATUS)
        elif args.action == "recover":
            make_root(ROOT)
            with Lock(ROOT / "coordinator.lock", wait=False), Lock(DEPLOY_LOCK, wait=False):
                if (ROOT / "attempt.json").exists():
                    try:
                        recover(ROOT, STATUS, interrupted=True)
                    except Exception:
                        record = load(ROOT / "attempt.json")
                        amend(ROOT, record["attempt"], {"restore": "unknown"})
                        refresh(ROOT, STATUS)
                        raise
        else:
            backup()
    except Exception as exc:  # noqa: BLE001 - prevent secret-bearing tracebacks in systemd logs.
        print("CRM backup incomplete:", type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
