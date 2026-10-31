"""Narrow, externally supervised idle gate for the reviewed legacy CRM image.

The caller must persist capture() before freeze(), own the maintenance lock,
and arrange independent rollback before calling any mutating function here.
This module neither stops Docker nor copies data or changes restart policies.
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import platform
import signal
import socket
import stat
import struct
import time

REVISION = "fe3c3c73d2800b6381ed2e1a489ec4be742d9e9c"
PORTS = (41731, 41831)
LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.syscall.restype = ctypes.c_long


class Error(RuntimeError):
    """A gate condition was not proved; never attach private payloads."""


def _stat(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
    return int(fields[19]), fields[0]


def _cgroup_pids(cgroup):
    lines = cgroup.strip().splitlines()
    if len(lines) != 1 or not lines[0].startswith("0::/"):
        raise Error("unsupported CRM cgroup")
    relative = Path(lines[0][3:])
    if ".." in relative.parts:
        raise Error("invalid CRM cgroup")
    path = Path("/sys/fs/cgroup") / str(relative).lstrip("/") / "cgroup.procs"
    return sorted(int(value) for value in path.read_text().split())


def _environment(pid):
    values = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    return dict(value.split(b"=", 1) for value in values if b"=" in value)


def _board_control_disabled(pid):
    value = _environment(pid).get(b"MINIMAL_KANBAN_BOARD_CONTROL_ENABLED", b"")
    if value.strip().lower() in {b"1", b"true", b"yes", b"y", b"on"}:
        raise Error("legacy board control is enabled")


def _check(record):
    pid = record["pid"]
    if record.get("revision") != REVISION or _stat(pid)[0] != record["start_ticks"]:
        raise Error("legacy CRM process changed")
    if Path(f"/proc/{pid}/cgroup").read_text() != record["cgroup"]:
        raise Error("legacy CRM cgroup changed")
    if os.readlink(f"/proc/{pid}/ns/net") != record["netns"]:
        raise Error("legacy CRM network namespace changed")
    if _cgroup_pids(record["cgroup"]) != record["pids"]:
        raise Error("legacy CRM process topology changed")
    if Path(f"/proc/{pid}").stat().st_uid != record["uid"]:
        raise Error("legacy CRM process owner changed")


@contextmanager
def _process(record):
    if os.geteuid() != 0 or platform.machine() != "x86_64":
        raise Error("legacy gate requires root on the reviewed Linux architecture")
    fd = os.pidfd_open(record["pid"])
    try:
        _check(record)
        yield fd
    finally:
        os.close(fd)


def _duplicate(pidfd, fd):
    duplicate = LIBC.syscall(438, pidfd, fd, 0)  # pidfd_getfd, Linux x86_64.
    if duplicate < 0:
        raise OSError(ctypes.get_errno(), "CRM socket descriptor unavailable")
    return socket.socket(fileno=duplicate)


def _sockets(record, pidfd):
    found = {}
    base = Path(f"/proc/{record['pid']}/fd")
    for path in base.iterdir():
        try:
            inode = os.readlink(path)
            if not inode.startswith("socket:[") or inode in found:
                continue
            with _duplicate(pidfd, int(path.name)) as sock:
                if sock.family not in (socket.AF_INET, socket.AF_INET6):
                    continue
                if sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
                    continue
                address = list(sock.getsockname())
                listening = bool(sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN))
                # Linux tcp_get_info exposes max accept backlog as tcpi_sacked
                # for LISTEN sockets. Do not guess Uvicorn's configured default.
                info = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 32)
                backlog = struct.unpack_from("=I", info, 28)[0] if listening else 0
                found[inode] = {
                    "fd": int(path.name),
                    "inode": inode,
                    "family": int(sock.family),
                    "address": address,
                    "backlog": backlog,
                    "listening": listening,
                }
        except OSError as exc:
            if exc.errno not in (errno.EBADF, errno.ENOENT):
                raise
    return found


def capture(pid, agent_dir, expected_cgroup, expected_uid=10001, *, expected_pids=None):
    """Read identities only; the caller must bind the revision/container itself."""
    pid = int(pid)
    ticks, state = _stat(pid)
    if state in {"T", "t", "Z", "X"}:
        raise Error("legacy CRM was not initially running")
    record = {
        "revision": REVISION,
        "pid": pid,
        "start_ticks": ticks,
        "cgroup": expected_cgroup,
        "netns": os.readlink(f"/proc/{pid}/ns/net"),
        "uid": expected_uid,
        "agent_dir": str(Path(agent_dir).absolute()),
        "pids": sorted(expected_pids if expected_pids is not None else _cgroup_pids(expected_cgroup)),
    }
    with _process(record) as pidfd:
        _board_control_disabled(pid)
        listeners = [value for value in _sockets(record, pidfd).values() if value["listening"]]
        if sorted(value["address"][1] for value in listeners) != sorted(PORTS):
            raise Error("legacy CRM listener profile changed")
        if any(value["backlog"] <= 0 for value in listeners):
            raise Error("legacy CRM listener backlog unavailable")
        record["listeners"] = sorted(listeners, key=lambda value: value["address"][1])
        _check(record)
    return record


def _frozen(record):
    base = Path(f"/proc/{record['pid']}/task")
    try:
        tasks = sorted(path.name for path in base.iterdir())
        if not tasks or any(_stat(int(task))[1] != "T" for task in tasks):
            return False
        return tasks == sorted(path.name for path in base.iterdir())
    except FileNotFoundError:
        return False


def freeze(record, timeout=2):
    """Write-ahead intent and independent recovery must already exist."""
    with _process(record) as pidfd:
        signal.pidfd_send_signal(pidfd, signal.SIGSTOP)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _check(record)
            if _frozen(record):
                return
            time.sleep(0.01)
    raise Error("legacy CRM group stop not confirmed")


def _owned(path, uid, *, directory=False):
    info = path.lstat()
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid or info.st_uid != uid or info.st_mode & 0o022 or (not directory and info.st_nlink != 1):
        raise Error("unsafe legacy agent state")
    return info


def _agent_json(root, name, uid):
    path = root / name
    before = _owned(path, uid)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        current = os.fstat(fd)
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino) or current.st_size > 64 * 1024 * 1024:
            raise Error("legacy agent state changed")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return json.load(stream)
    finally:
        os.close(fd)


def _agent_idle(record):
    root, uid = Path(record["agent_dir"]), record["uid"]
    if root.resolve(strict=True) != root:
        raise Error("legacy agent state contains a symlink")
    _owned(root, uid, directory=True)
    path = root / "agent.lock"
    info = _owned(path, uid)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (info.st_dev, info.st_ino):
            raise Error("legacy agent lock changed")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error("legacy agent lock busy") from None
        tasks = _agent_json(root, "tasks.json", uid)
        schedules = _agent_json(root, "schedules.json", uid)
        status = _agent_json(root, "status.json", uid)
        if not isinstance(tasks, list) or any(not isinstance(item, dict) for item in tasks):
            raise Error("legacy agent task schema changed")
        if not isinstance(schedules, list) or any(not isinstance(item, dict) for item in schedules):
            raise Error("legacy agent schedule schema changed")
        if (
            not isinstance(status, dict)
            or type(status.get("running")) is not bool
            or "current_task_id" not in status
            or "current_run_id" not in status
        ):
            raise Error("legacy agent status schema changed")
        known_statuses = {"pending", "running", "completed", "failed", "cancelled"}
        if any(item.get("status") not in known_statuses for item in tasks):
            raise Error("legacy agent task status changed")
        if (
            any(item.get("status") in {"pending", "running"} for item in tasks)
            or any(bool(item.get("active")) for item in schedules)
            or status.get("running")
            or status.get("current_task_id")
            or status.get("current_run_id")
        ):
            raise Error("legacy agent is not idle")
        after = path.lstat()
        if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
            raise Error("legacy agent lock changed")
    finally:
        os.close(fd)


@contextmanager
def _listener(record, pidfd, item):
    if os.readlink(f"/proc/{record['pid']}/fd/{item['fd']}") != item["inode"]:
        raise Error("legacy CRM listener descriptor changed")
    with _duplicate(pidfd, item["fd"]) as sock:
        if int(sock.family) != item["family"] or list(sock.getsockname()) != item["address"]:
            raise Error("legacy CRM listener address changed")
        yield sock


def gate(record):
    """Leave the process frozen; caller commits Docker stop or calls rollback."""
    with _process(record) as pidfd:
        if not _frozen(record):
            raise Error("legacy CRM is not frozen")
        _board_control_disabled(record["pid"])
        expected = {item["inode"] for item in record["listeners"]}
        observed = _sockets(record, pidfd)
        if {inode for inode, item in observed.items() if item["listening"]} != expected:
            raise Error("legacy CRM listener profile changed")
        if any(item["address"][1] in PORTS and not item["listening"] for item in observed.values()):
            raise Error("legacy CRM still has accepted requests")
        _agent_idle(record)
        for item in record["listeners"]:
            with _listener(record, pidfd, item) as sock:
                sock.shutdown(socket.SHUT_RDWR)
                if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
                    raise Error("legacy CRM listener did not close")
        _check(record)


def rollback(record):
    """Idempotent pre-commit recovery; never call after Docker stop intent."""
    with _process(record) as pidfd:
        # If freeze itself failed, no listeners were gated; CONT is still needed.
        closed = []
        for item in record["listeners"]:
            with _listener(record, pidfd, item) as sock:
                if not sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
                    closed.append(item)
        if closed and not _frozen(record):
            raise Error("gated legacy CRM was unexpectedly resumed")
        for item in closed:
            with _listener(record, pidfd, item) as sock:
                sock.listen(item["backlog"])
                if list(sock.getsockname()) != item["address"] or not sock.getsockopt(
                    socket.SOL_SOCKET, socket.SO_ACCEPTCONN
                ):
                    raise Error("legacy CRM listener rollback not confirmed")
        _check(record)
        signal.pidfd_send_signal(pidfd, signal.SIGCONT)


def _term_pending(record):
    mask = 1 << (signal.SIGTERM - 1)
    for task in Path(f"/proc/{record['pid']}/task").iterdir():
        for line in (task / "status").read_text().splitlines():
            if line.startswith(("SigPnd:", "ShdPnd:")) and int(line.split()[1], 16) & mask:
                return True
    return False


def thaw_after_term(record, timeout=10):
    """Resume only after the supervised Docker stop sent its pending SIGTERM."""
    with _process(record) as pidfd:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _check(record)
            if not _frozen(record):
                raise Error("legacy CRM resumed before stop signal")
            if _term_pending(record):
                for item in record["listeners"]:
                    with _listener(record, pidfd, item) as sock:
                        if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
                            raise Error("legacy CRM listener reopened before commit")
                signal.pidfd_send_signal(pidfd, signal.SIGCONT)
                return
            time.sleep(0.01)
    raise Error("Docker stop signal was not confirmed")
