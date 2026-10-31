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
from contextlib import suppress

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

# Executed as the CRM UID inside the exact container. It never prints the
# environment, customer data, or key material. pidfd binds the signal to the
# checked process even if its numeric PID is subsequently recycled.
CONTROL_PROBE = r"""
import json, os, re, signal, stat, sys, tempfile
request = json.load(sys.stdin)
path = "/home/autostop/.crm-backup-status.json"
def read():
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != 10001 or stat.S_IMODE(s.st_mode) != 0o600 or s.st_nlink != 1:
            raise ValueError("invalid receipt permissions")
        raw = os.read(fd, 4097)
        if len(raw) > 4096:
            raise ValueError("invalid receipt length")
        def unique(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("duplicate receipt field")
                result[key] = item
            return result
        value = json.loads(raw, object_pairs_hook=unique)
    finally:
        os.close(fd)
    if set(value) != {"pid", "start_ticks", "generation", "attempt", "state"}:
        raise ValueError("invalid receipt fields")
    if type(value["pid"]) is not int or value["pid"] <= 1 or type(value["start_ticks"]) is not int or value["start_ticks"] <= 0:
        raise ValueError("invalid receipt process")
    if not isinstance(value["generation"], str) or not re.fullmatch(r"[0-9a-f]{32}", value["generation"]):
        raise ValueError("invalid generation")
    if not isinstance(value["attempt"], str) or (value["attempt"] and not re.fullmatch(r"[0-9a-f]{32}", value["attempt"])):
        raise ValueError("invalid attempt")
    if value["state"] not in {"idle", "aborted", "preparing", "ready", "committed"}:
        raise ValueError("invalid receipt state")
    if value["state"] != "idle" and not value["attempt"]:
        raise ValueError("missing receipt attempt")
    return value
receipt = read()
if request.get("expected") is not None and receipt != request["expected"]:
    raise ValueError("receipt changed")
fd = os.pidfd_open(receipt["pid"])
try:
    with open("/proc/" + str(receipt["pid"]) + "/stat") as f:
        ticks = int(f.read().rsplit(")", 1)[1].split()[19])
    if ticks != receipt["start_ticks"] or read() != receipt:
        raise ValueError("process generation changed")
    op = request.get("operation")
    if op in ("USR1", "USR2", "HUP", "TERM"):
        if request.get("expected") is None:
            raise ValueError("signal requires exact receipt")
        allowed = {"USR1": {"idle", "aborted"}, "USR2": {"ready"}, "HUP": {"preparing", "ready"}, "TERM": {"committed"}}
        if receipt["state"] not in allowed[op]:
            raise ValueError("signal is not valid for receipt state")
        if op == "USR2":
            directory = "/home/autostop"
            d = os.lstat(directory)
            if not stat.S_ISDIR(d.st_mode) or d.st_uid != 10001 or stat.S_IMODE(d.st_mode) & 0o077:
                raise ValueError("unsafe control directory")
            command = {k: receipt[k] for k in ("generation", "attempt", "pid", "start_ticks")}
            command["operation"] = "commit"
            out, name = tempfile.mkstemp(prefix=".crm-backup-command-", dir=directory)
            try:
                os.fchmod(out, 0o600)
                with os.fdopen(out, "w") as f:
                    json.dump(command, f, separators=(",", ":"))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(name, directory + "/.crm-backup-command.json")
                dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            if read() != receipt:
                raise ValueError("receipt changed before commit signal")
        signal.pidfd_send_signal(fd, getattr(signal, "SIG" + op))
    elif op != "read":
        raise ValueError("invalid operation")
finally:
    os.close(fd)
print(json.dumps(receipt, separators=(",", ":")))
"""


class Uncertain(RuntimeError):
    pass


class PrepareAborted(Uncertain):
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
    allowed = {"pending", "running", "ok", "failed", "unknown", "not_needed"}

    def stage(key):
        return record.get(key) if record.get(key) in allowed else "unknown"

    names = {
        "pending": "ожидается",
        "running": "выполняется",
        "ok": "успех",
        "failed": "ошибка",
        "unknown": "не подтверждён",
        "not_needed": "не требовался",
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
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1:
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

    def control_enabled(self, identity):
        # The full environment is held only in memory and never logged.
        env = json.loads(run("docker", "inspect", "--format", "{{json .Config.Env}}", identity))
        values = [v for v in env if isinstance(v, str) and v.startswith("AUTOSTOP_CRM_BACKUP_CONTROL=")]
        return values == ["AUTOSTOP_CRM_BACKUP_CONTROL=1"]

    def restart_policy(self, identity):
        return run("docker", "inspect", "--format", "{{.HostConfig.RestartPolicy.Name}}", identity)

    def control(self, identity, operation="read", expected=None):
        request = json.dumps({"operation": operation, "expected": expected})
        result = subprocess.run(
            ["docker", "exec", "--interactive", "--user", "10001", identity, "python", "-c", CONTROL_PROBE],
            input=request,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )
        if result.returncode:
            raise Uncertain("CRM backup control unavailable or changed")
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise Uncertain("invalid CRM control receipt")
        return value

    @staticmethod
    def _same_process(left, right):
        return all(left[k] == right[k] for k in ("pid", "start_ticks", "generation"))

    def prepare(self, identity, expected):
        if not self.control_enabled(identity):
            raise PrepareAborted("CRM backup control not enabled")
        initial = self.control(identity)
        if initial["state"] not in ("idle", "aborted"):
            raise PrepareAborted("CRM backup control not idle")
        if not self._same_process(initial, expected["control"]):
            raise PrepareAborted("CRM control process changed before prepare")
        current = self.crm()
        if (
            current["id"] != identity
            or current["state"] != "running"
            or current["started"] != expected["started"]
            or current["restarts"] != expected["restarts"]
            or current["revision"] != expected["revision"]
        ):
            raise PrepareAborted("CRM generation changed before prepare")
        last = initial
        try:
            self.control(identity, "USR1", initial)
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                last = self.control(identity)
                if not self._same_process(initial, last):
                    raise Uncertain("CRM process changed during prepare")
                if last["attempt"] != initial["attempt"]:
                    if last["state"] == "ready":
                        return last
                    if last["state"] == "aborted":
                        raise PrepareAborted("CRM did not drain")
                time.sleep(0.25)
        except PrepareAborted:
            raise
        except Exception:  # noqa: BLE001 - abort only a verified pre-COMMIT attempt.
            pass
        # USR2 has not been sent by this worker. Abort only the exact observed
        # preparation; a late or ambiguous signal is allowed to expire.
        try:
            last = self.control(identity)
            if (
                self._same_process(initial, last)
                and last["attempt"] != initial["attempt"]
                and last["state"] in ("preparing", "ready")
            ):
                self.control(identity, "HUP", last)
                deadline = time.monotonic() + 35
                while time.monotonic() < deadline:
                    current = self.control(identity)
                    if not self._same_process(last, current) or current["attempt"] != last["attempt"]:
                        break
                    if current["state"] == "aborted":
                        raise PrepareAborted("CRM prepare aborted")
                    time.sleep(0.25)
        except PrepareAborted:
            raise
        except Exception:  # noqa: BLE001 - retain unknown state, never send stop.
            pass
        raise Uncertain("CRM prepare outcome unknown")

    def commit(self, identity, ready):
        if ready["state"] != "ready":
            raise Uncertain("CRM was not ready")
        self.control(identity, "USR2", ready)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            value = self.control(identity)
            if not self._same_process(ready, value) or value["attempt"] != ready["attempt"]:
                raise Uncertain("CRM generation changed during commit")
            if value["state"] == "committed":
                return value
            if value["state"] == "aborted":
                raise PrepareAborted("CRM prepare expired before commit")
            time.sleep(0.25)
        raise Uncertain("CRM commit outcome unknown")

    def stop(self, kind, identity):
        if kind == "scheduler":
            run("systemctl", "stop", SCHEDULER, timeout=None)
            result = run("systemctl", "show", SCHEDULER, "-p", "Result", "--value")
            if result != "success" or self.scheduler()["state"] != "inactive":
                raise Uncertain("scheduler stop was not clean")
        else:
            # Called only after exact PREPARE and COMMIT receipts. A forced
            # exit is detected and makes the snapshot ineligible.
            run("docker", "stop", "--time", "30", identity, timeout=None)

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
        try:
            save(root / "attempt.json", record)
        except BaseException:
            if resource is None and (fields or {}).get("copy") == "ok":
                # os.replace may have succeeded before directory fsync failed.
                # Keep the lock while making a best-effort unknown marker.
                record["copy"] = "unknown"
                with suppress(Exception):
                    save(root / "attempt.json", record)
            raise
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


def require_open_control(system, item):
    receipt = system.control(item["identity"])
    if receipt.get("state") not in ("idle", "aborted"):
        raise Uncertain("CRM backup gate is not confirmed open")
    previous = item.get("drain") or {}
    if item["phase"] == "stopped" and receipt.get("generation") == previous.get("generation"):
        raise Uncertain("CRM still uses the stopped control generation")


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
                or actual["revision"] != item["revision"]
            ):
                raise Uncertain("CRM generation changed before stop")
            if kind == "scheduler" and (actual["state"] != "active" or actual["invocation"] != item["invocation"]):
                raise Uncertain("scheduler generation changed before stop")
        if kind == "crm" and operation == "stop" and item["phase"] == "commit_verified":
            # Recovery may finish a committed drain only when the journal proves
            # the original worker had not yet issued Docker stop.
            if item.get("drain", {}).get("state") != "committed" or system.control(item["identity"]) != item["drain"]:
                raise Uncertain("committed CRM generation changed before recovery stop")
        elif kind == "crm" and operation == "stop":
            r["phase"] = "crm_prepare_running"
            save(path, r)
            try:
                ready = system.prepare(item["identity"], item)
            except PrepareAborted:
                r["phase"] = "crm_prepare_aborted"
                save(path, r)
                raise
            except Exception:
                r["phase"] = "crm_prepare_unknown"
                save(path, r)
                raise
            item["drain"] = ready
            r["phase"] = "crm_commit_intent"
            save(path, r)
            try:
                committed = system.commit(item["identity"], ready)
                again = system.crm()
                if (
                    again["id"] != item["identity"]
                    or again["state"] != "running"
                    or again["started"] != item["started"]
                    or again["restarts"] != item["restarts"]
                    or again["revision"] != item["revision"]
                    or system.control(item["identity"]) != committed
                ):
                    raise Uncertain("CRM generation changed after commit")
            except PrepareAborted:
                r["phase"] = "crm_prepare_aborted"
                save(path, r)
                raise
            except Exception:
                r["phase"] = "crm_commit_unknown"
                save(path, r)
                raise
            item["drain"] = committed
        r["phase"] = f"{kind}_{operation}_running"
        save(path, r)
        try:
            if operation == "stop":
                system.stop(kind, item["identity"])
            else:
                system.start(kind, item["identity"])
                system.healthy(kind, item["identity"])
                if kind == "crm":
                    require_open_control(system, item)
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


def _docker_restart_resolved(record, system):
    """Docker may be restarted with live-restore while CRM stays committed."""
    item = record["crm"]
    current = system.crm()
    if current["id"] != item["identity"]:
        return False
    if current["state"] in ("exited", "created"):
        return True
    if current["state"] != "running" or current["started"] == item.get("started"):
        return False
    try:
        receipt = system.control(item["identity"])
    except Exception:  # noqa: BLE001 - unreadable control state is not a confirmed recovery.
        return False
    previous = item.get("drain") or {}
    return receipt.get("state") in ("idle", "aborted") and receipt.get("generation") != previous.get("generation")


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


def recover(root, status_path, system=None, *, interrupted=False, wait=True):  # noqa: C901 - explicit recovery states.
    system = system or System()
    path = root / "attempt.json"
    with Lock(root / "operation.lock", wait=wait):
        r = load(path)
        phase = r["phase"]
        changed = False
        if interrupted and r["copy"] in ("pending", "running"):
            r["copy"] = "failed" if r["copy"] == "pending" else "unknown"
            changed = True
        if phase == "complete":
            # The owner may have intentionally changed services afterwards.
            # A completed recovery is immutable, even across a reboot.
            if changed:
                save(path, r)
                refresh(root, status_path)
            return r
        if phase in ("crm_commit_intent", "crm_commit_unknown", "crm_term_intent"):
            # No Docker stop was sent in these phases. For an exactly proven
            # COMMIT, signal only the same process via pidfd and let its
            # existing restart=always policy start a fresh generation.
            item = r["crm"]
            previous = item.get("drain") or {}
            try:
                current = system.crm()
                if current["id"] != item["identity"] or current["revision"] != item["revision"]:
                    raise Uncertain("CRM identity changed during commit recovery")
                if current["state"] == "running":
                    receipt = system.control(item["identity"])
                    old = current["started"] == item["started"] and current["restarts"] == item["restarts"]
                    if (
                        old
                        and receipt["state"] == "committed"
                        and previous.get("attempt") == receipt["attempt"]
                        and System._same_process(previous, receipt)
                    ):
                        if system.restart_policy(item["identity"]) != "always":
                            raise Uncertain("CRM automatic restart is not configured")
                        if phase != "crm_term_intent" or r["restore"] != "unknown" or changed:
                            item["drain"] = receipt
                            r["phase"] = "crm_term_intent"
                            r["restore"] = "unknown"
                            save(path, r)
                            refresh(root, status_path)
                        with suppress(Exception):
                            system.control(item["identity"], "TERM", receipt)
                        raise Uncertain("waiting for CRM to restart after committed drain")
                    if (
                        phase in ("crm_commit_intent", "crm_commit_unknown")
                        and old
                        and receipt["state"] == "aborted"
                        and previous.get("attempt") == receipt["attempt"]
                        and System._same_process(previous, receipt)
                    ):
                        # The CRM control protocol binds delayed COMMIT signals
                        # to the old attempt, so this exact aborted receipt
                        # proves its gates are open and no stop was issued.
                        item["phase"] = "untouched"
                        r["phase"] = "recovering"
                        phase = "recovering"
                        changed = True
                    if (
                        not old
                        and receipt["state"] in ("idle", "aborted")
                        and receipt["generation"] != previous.get("generation")
                    ):
                        system.healthy("crm", item["identity"])
                        item["phase"] = "restored"
                        r["phase"] = "recovering"
                        phase = "recovering"
                        changed = True
            except Uncertain:
                if changed and r["phase"] != "crm_term_intent":
                    r["restore"] = "unknown"
                    save(path, r)
                    refresh(root, status_path)
                raise
            except Exception:  # noqa: BLE001 - unreadable receipt cannot authorize a signal.
                if changed:
                    r["restore"] = "unknown"
                    save(path, r)
                    refresh(root, status_path)
                raise Uncertain("committed CRM state not confirmed") from None
        # Invalidate any worker that did not acquire the operation lock. A
        # worker killed while running leaves an unknown request and cannot be
        # treated as finished unless the host or daemon has since restarted.
        boot_restarted = system.boot() != r["boot"]
        daemon_restarted = system.daemon() != r["daemon"]
        if phase.endswith(("_running", "_unknown")) or phase in ("crm_commit_intent", "crm_term_intent"):
            resolved = boot_restarted or (
                phase.startswith("crm_") and daemon_restarted and _docker_restart_resolved(r, system)
            )
            if not resolved:
                if r["restore"] != "unknown":
                    r["restore"] = "unknown"
                    changed = True
                if changed:
                    save(path, r)
                    refresh(root, status_path)
                raise Uncertain("in-flight operation outcome unknown")
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
            if kind == "crm":
                require_open_control(system, item)
            r = amend(root, r["attempt"], {"phase": "restored"}, resource=kind)
        else:
            if item["phase"] != "stopped":
                raise Uncertain("resource was not confirmed stopped by this attempt")
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


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink


def _checked_stat(stream):
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1:
        raise Uncertain("unsafe snapshot file")
    return info


def _file_info(stream):
    digest = hashlib.sha256()
    size = 0
    before = _checked_stat(stream)
    stream.seek(0)
    while data := stream.read(1024 * 1024):
        size += len(data)
        digest.update(data)
    if not size or size != before.st_size or _signature(before) != _signature(_checked_stat(stream)):
        raise Uncertain("snapshot changed while hashing")
    return before, digest.hexdigest()


def _open_snapshot(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    return os.fdopen(fd, "rb")


def publish(root, path, attempt):
    """Retain the old pending generation unless a new pointer is durable."""
    if not ATTEMPT.fullmatch(attempt):
        raise Uncertain("invalid attempt ID")
    with _open_snapshot(path) as staged:
        staged_info, digest = _file_info(staged)
        with Lock(root / "pending.lock"):
            try:
                old = load_pending(root / "pending.json")
            except FileNotFoundError:
                old = None
            try:
                old_ready = load_pending(root / "pending-ready.json")
            except FileNotFoundError:
                old_ready = None
            try:
                old_stream = _open_snapshot(root / old["file"]) if old and old == old_ready else None
            except FileNotFoundError:
                old_stream = None
        old_info = None
        if old_stream:
            with old_stream:
                try:
                    old_info, old_digest = _file_info(old_stream)
                    if old_info.st_size != old["size"] or old_digest != old["sha256"]:
                        old_info = None
                except (OSError, Uncertain):
                    # The previous file is never removed if it cannot be verified.
                    old_info = None
        with Lock(root / "pending.lock"):
            try:
                current = load_pending(root / "pending.json")
            except FileNotFoundError:
                current = None
            try:
                current_ready = load_pending(root / "pending-ready.json")
            except FileNotFoundError:
                current_ready = None
            if current is not None and current != old:
                raise Uncertain("pending generation changed during publication")
            if current is not None and current_ready != old_ready:
                raise Uncertain("pending readiness changed during publication")
            if _signature(staged_info) != _signature(_checked_stat(staged)) or _signature(staged_info) != _signature(
                path.lstat()
            ):
                raise Uncertain("staged snapshot changed before publication")
            return _publish_locked(root, path, attempt, staged_info, digest, current, old_info)


def _publish_locked(root, path, attempt, staged_info, digest, old, old_info):
    pending = root / "pending.json"
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
    value = {"id": attempt, "file": target.name, "sha256": digest, "size": staged_info.st_size}
    save(pending, value)
    target_info = target.lstat()
    if (
        load_pending(pending) != value
        or not stat.S_ISREG(target_info.st_mode)
        or target_info.st_nlink != 1
        or (target_info.st_dev, target_info.st_ino, target_info.st_size, target_info.st_mtime_ns)
        != (staged_info.st_dev, staged_info.st_ino, staged_info.st_size, staged_info.st_mtime_ns)
    ):
        raise Uncertain("published snapshot not confirmed")
    save(root / "pending-ready.json", value)
    if load_pending(root / "pending-ready.json") != value:
        raise Uncertain("snapshot readiness not confirmed")
    return value, old, old_info


def cleanup_previous(root, value, old, old_info):
    if not old or old == value or not old_info:
        return
    with Lock(root / "pending.lock"):
        try:
            if load_pending(root / "pending.json") != value or load_pending(root / "pending-ready.json") != value:
                return
            old_path = root / old["file"]
            if _signature(old_path.lstat()) != _signature(old_info):
                return
            old_path.unlink()
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except FileNotFoundError:
            pass


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
        old = None
        if path.exists():
            old = load(path)
            if old.get("phase") != "complete":
                raise Uncertain("previous attempt requires recovery")
        r = {
            "attempt": uuid.uuid4().hex,
            "started_at": datetime.now(UTC).isoformat(),
            # Until preflight is complete no service can have been stopped.
            "phase": "complete",
            "copy": "running",
            "restore": "not_needed",
            "delivery": "pending",
        }
        with Lock(root / "operation.lock"):
            save(path, r)
        refresh(root, status_path)
        try:
            if old:
                (root / (".staged-" + old["attempt"] + ".tar.gz")).unlink(missing_ok=True)
            check_space(root, data)
            crm = system.crm()
            scheduler = system.scheduler()
            if not HEX40.fullmatch(approved_revision) or crm["revision"] != approved_revision:
                raise Uncertain("installed CRM revision is not approved for backup")
            if crm["state"] != "running" or crm["health"] != "healthy":
                raise Uncertain("CRM was not initially healthy")
            if not system.control_enabled(crm["id"]):
                raise Uncertain("CRM backup control is not enabled")
            control = system.control(crm["id"])
            if control["state"] not in ("idle", "aborted"):
                raise Uncertain("CRM backup control is not idle")
            boot = system.boot()
            daemon = system.daemon()
        except Exception:
            amend(root, r["attempt"], {"copy": "failed"})
            refresh(root, status_path)
            raise
        r = amend(
            root,
            r["attempt"],
            {
                "boot": boot,
                "daemon": daemon,
                "phase": "prepared",
                "copy": "pending",
                "restore": "pending",
                "crm": {
                    "identity": crm["id"],
                    "started": crm["started"],
                    "restarts": crm["restarts"],
                    "revision": crm["revision"],
                    "control": control,
                    "initially_active": True,
                    "phase": "untouched",
                },
                "scheduler": {
                    "identity": scheduler["identity"],
                    "invocation": scheduler["invocation"],
                    "initially_active": scheduler["state"] == "active",
                    "phase": "untouched",
                },
            },
        )
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
            try:
                published, previous, previous_info = publish(root, staged, r["attempt"])
            except Exception:
                amend(root, r["attempt"], {"copy": "unknown"})
                refresh(root, status_path)
                raise
            try:
                r = amend(root, r["attempt"], {"copy": "ok"})
            except Exception:
                with suppress(Exception):
                    refresh(root, status_path)
                raise
            refresh(root, status_path)
            with suppress(OSError):
                cleanup_previous(root, published, previous, previous_info)
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
                        recover(ROOT, STATUS, interrupted=True, wait=False)
                    except BlockingIOError:
                        # A live worker owns the operation; retry later.
                        raise
                    except Exception:
                        record = load(ROOT / "attempt.json")
                        if record["restore"] != "unknown":
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
