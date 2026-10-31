#!/usr/bin/env python3
"""Retry delivery of the one pending CRM archive to the pinned Windows PC.

The snapshot producer owns pending.json and uses the same short pending.lock.
No service control occurs here. A successful delivery is independently read
back before the pointer or any known old remote generation may be removed.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import selectors
import stat
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path

ROOT = Path("/var/lib/autostop-manager/crm-backup")
REFRESH = "/usr/local/libexec/autostop-crm-backup/crm_backup_coordinator.py"
OWNER_UID = 0
VERIFY_TIMEOUT_SECONDS = 21600
_spec = importlib.util.spec_from_file_location(
    "crm_windows_transfer", Path(__file__).with_name("transfer-crm-backup-windows.py")
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("transfer_helper_unavailable")
transfer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(transfer)

STATUS = r"""
function Inspect([string]$path) {
    if (-not (Test-Path -LiteralPath $path -ErrorAction Stop)) { return $null }
    Assert-PrivateFile $path
    $f = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
    try {
        $hasher = [Security.Cryptography.SHA256]::Create()
        try { $hash = [BitConverter]::ToString($hasher.ComputeHash($f)).Replace('-', '').ToLowerInvariant() }
        finally { $hasher.Dispose() }
        return @{ size = $f.Length; sha256 = $hash }
    } finally { $f.Dispose() }
}
@{ part = (Inspect $part); final = (Inspect $final) } | ConvertTo-Json -Compress
"""

# Open without sharing read/write. Deletion is allowed only while holding this
# handle and only if the file still has the independently observed contents.
DELETE = r"""
$path = __PATH__
Assert-PrivateFile $path
$f = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Delete)
try {
    if ($f.Length -ne __SIZE__) { throw 'size changed' }
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { $hash = [BitConverter]::ToString($hasher.ComputeHash($f)).Replace('-', '').ToLowerInvariant() }
    finally { $hasher.Dispose() }
    if ($hash -cne '__HASH__') { throw 'hash changed' }
    [IO.File]::Delete($path)
} finally { $f.Dispose() }
"""


class DeliveryError(Exception):
    def __init__(self, code: str, *, unknown: bool = False):
        super().__init__(code)
        self.code = code
        self.unknown = unknown


def valid_record(value):
    if not isinstance(value, dict) or set(value) != {"id", "file", "sha256", "size"}:
        raise DeliveryError("invalid_record")
    ident, name, digest, size = (value[k] for k in ("id", "file", "sha256", "size"))
    if (
        not isinstance(ident, str)
        or not re.fullmatch(r"[0-9a-f]{32}", ident)
        or name != f"crm-{ident}.tar.gz"
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size <= 0
    ):
        raise DeliveryError("invalid_record")
    return value


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _open_file(dirfd, name, flags=os.O_RDONLY):
    return os.open(name, flags | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=dirfd)


def read_json(dirfd, name, *, missing=None):
    try:
        fd = _open_file(dirfd, name)
    except FileNotFoundError:
        return missing
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != OWNER_UID or st.st_mode & 0o077 or st.st_nlink != 1:
            raise DeliveryError("unsafe_metadata")
        data = f.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise DeliveryError("metadata_too_large")
    try:
        return json.loads(data, object_pairs_hook=_unique_pairs)
    except (UnicodeDecodeError, ValueError) as exc:
        raise DeliveryError("invalid_metadata") from exc


def write_json(dirfd, name, value):
    data = (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=dirfd,
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
        os.fsync(dirfd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=dirfd)


def unlink_file(dirfd, name):
    os.unlink(name, dir_fd=dirfd)
    os.fsync(dirfd)


def lock(dirfd, name, *, nonblocking=False):
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=dirfd)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != OWNER_UID or st.st_mode & 0o077 or st.st_nlink != 1:
        os.close(fd)
        raise DeliveryError("unsafe_lock")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
    except BaseException:
        os.close(fd)
        raise
    return fd


def current(dirfd):
    value = read_json(dirfd, "pending.json")
    ready = read_json(dirfd, "pending-ready.json")
    if value is None and ready is None:
        os.fsync(dirfd)
        if read_json(dirfd, "pending.json") is not None or read_json(dirfd, "pending-ready.json") is not None:
            raise DeliveryError("pending_not_ready")
        return None
    if value is None and ready is not None:
        # A crash may occur after confirmed delivery removed pending.json but
        # before it removed the marker. Only that exact completed intent can
        # dispose of the stale marker; no archive or remote file is touched.
        marker = valid_record(ready)
        intent = read_json(dirfd, "delivery-intent.json")
        if intent == marker and marker in journal(dirfd):
            unlink_file(dirfd, "pending-ready.json")
            return None
    if value is None or ready is None or valid_record(value) != valid_record(ready):
        raise DeliveryError("pending_not_ready")
    # A marker may have become visible just before the publisher's directory
    # fsync failed. Make both names durable ourselves and read them back while
    # holding the same short pending lock.
    os.fsync(dirfd)
    if read_json(dirfd, "pending.json") != value or read_json(dirfd, "pending-ready.json") != value:
        raise DeliveryError("pending_not_ready")
    return value


def open_source(dirfd, record):
    """Acquire an immutable-generation fd while holding only the short lock."""
    guard = lock(dirfd, "pending.lock")
    try:
        try:
            fd = _open_file(dirfd, record["file"])
        except FileNotFoundError:
            return None
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != OWNER_UID or st.st_mode & 0o077 or st.st_nlink != 1:
            os.close(fd)
            raise DeliveryError("unsafe_source")
        return os.fdopen(fd, "rb")
    finally:
        os.close(guard)


def pending_record(dirfd):
    guard = lock(dirfd, "pending.lock")
    try:
        return current(dirfd)
    finally:
        os.close(guard)


def verify_source(stream, record):
    before = os.fstat(stream.fileno())
    size, digest = transfer.digest(stream)
    after = os.fstat(stream.fileno())
    if size != record["size"] or digest.hex() != record["sha256"] or _identity(before) != _identity(after):
        raise DeliveryError("source_integrity")
    stream.seek(0)
    return after


def _identity(st):
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


class Windows:
    def run(self, script, record, *, final=False, stdin=subprocess.DEVNULL, timeout=120):
        command = transfer.command(script, record["id"], record["size"], final=final)
        try:
            result = subprocess.run(
                command, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeliveryError("ssh_unknown", unknown=True) from exc
        if result.returncode != 0:
            raise DeliveryError("ssh_unknown", unknown=True)
        return result.stdout

    def inspect(self, record):
        data = self.run(STATUS, record, timeout=21600)
        try:
            value = json.loads(data, object_pairs_hook=_unique_pairs)
        except (UnicodeDecodeError, ValueError) as exc:
            raise DeliveryError("remote_metadata_unknown", unknown=True) from exc
        if not isinstance(value, dict) or set(value) != {"part", "final"}:
            raise DeliveryError("remote_metadata_unknown", unknown=True)
        for item in value.values():
            if item is not None and (
                not isinstance(item, dict)
                or set(item) != {"size", "sha256"}
                or isinstance(item["size"], bool)
                or not isinstance(item["size"], int)
                or item["size"] < 0
                or not isinstance(item["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            ):
                raise DeliveryError("remote_metadata_unknown", unknown=True)
        return value

    def write(self, record, source):
        valid_record(record)
        # Keep the already verified generation open even if the pending name
        # changes. SFTP opens its own reader through this process's stable fd.
        local = f"/proc/{os.getpid()}/fd/{source.fileno()}"
        remote = f"{transfer.SFTP_DIRECTORY}/crm-{record['id']}.part"
        batch = f'put "{local}" "{remote}"\n'.encode()
        # Precreate the empty file with its private ACL. Native SFTP truncates
        # that same file; ordinary put does not replace its security descriptor.
        self.run(transfer.CREATE, record)
        try:
            result = subprocess.run(
                transfer.SFTP, input=batch, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=21600
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeliveryError("ssh_unknown", unknown=True) from exc
        if result.returncode != 0:
            raise DeliveryError("ssh_unknown", unknown=True)
        self.run(transfer.FLUSH, record)

    def verify(self, record, *, final):
        try:
            observed = bounded_remote_digest(
                transfer.command(transfer.READ, record["id"], final=final),
                record["size"],
                VERIFY_TIMEOUT_SECONDS,
            )
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            raise DeliveryError("remote_read_unknown", unknown=True) from exc
        if observed != (record["size"], bytes.fromhex(record["sha256"])):
            raise DeliveryError("remote_integrity", unknown=True)

    def publish(self, record):
        self.run(transfer.PUBLISH, record)

    def delete(self, record, observed, *, final):
        script = DELETE.replace("__SIZE__", str(observed["size"])).replace("__HASH__", observed["sha256"])
        self.run(script, record, final=final, timeout=21600)


def bounded_remote_digest(command, expected_size, timeout):
    """Hash SSH stdout, including a wall-clock bound even if the peer hangs."""
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        selector = selectors.DefaultSelector()
    except BaseException:
        with suppress(ProcessLookupError):
            process.kill()
        process.wait()
        process.stdout.close()
        raise
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        digest = hashlib.sha256()
        size = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise subprocess.TimeoutExpired(command, timeout)
            block = os.read(process.stdout.fileno(), transfer.CHUNK)
            if not block:
                break
            size += len(block)
            if size > expected_size:
                raise RuntimeError("remote file is larger than source")
            digest.update(block)
        remaining = max(0, deadline - time.monotonic())
        if process.wait(timeout=remaining) != 0:
            raise RuntimeError("remote read failed")
        return size, digest.digest()
    finally:
        selector.close()
        if process.poll() is None:
            with suppress(ProcessLookupError):
                process.kill()
            process.wait()
        process.stdout.close()


def matches(observed, record):
    return observed == {"size": record["size"], "sha256": record["sha256"]}


def journal(dirfd):
    rows = read_json(dirfd, "delivered.json", missing=[])
    if not isinstance(rows, list):
        raise DeliveryError("invalid_journal")
    seen = set()
    for row in rows:
        valid_record(row)
        if row["id"] in seen:
            raise DeliveryError("invalid_journal")
        seen.add(row["id"])
    return rows


def remember(dirfd, record):
    rows = journal(dirfd)
    for existing in rows:
        if existing["id"] == record["id"]:
            if existing != record:
                raise DeliveryError("journal_conflict")
            return
    rows.append(record)
    write_json(dirfd, "delivered.json", rows)


def rotation_authorized(dirfd, record):
    guard = lock(dirfd, "pending.lock")
    try:
        pending = current(dirfd)
        if pending is not None and pending != record:
            return False
    finally:
        os.close(guard)
    try:
        guard = lock(dirfd, "operation.lock", nonblocking=True)
    except BlockingIOError:
        return False
    try:
        attempt = read_json(dirfd, "attempt.json")
        if not isinstance(attempt, dict) or attempt.get("attempt") != record["id"] or attempt.get("copy") != "ok":
            return False
        os.fsync(dirfd)
        return read_json(dirfd, "attempt.json") == attempt
    finally:
        os.close(guard)


def rotate(dirfd, remote):
    rows = journal(dirfd)
    if len(rows) <= 2:
        return
    if not rotation_authorized(dirfd, rows[-1]):
        raise DeliveryError("rotation_unconfirmed", unknown=True)
    # Do not prune any generation unless both latest known generations pass a
    # fresh private-file, size and hash check on the exact destination.
    for recent in rows[-2:]:
        if not matches(remote.inspect(recent)["final"], recent):
            raise DeliveryError("rotation_precondition", unknown=True)
    while len(rows) > 2:
        old = rows[0]
        observed = remote.inspect(old)["final"]
        if observed is not None:
            if not matches(observed, old):
                raise DeliveryError("rotation_precondition", unknown=True)
            remote.delete(old, observed, final=True)
        rows.pop(0)
        write_json(dirfd, "delivered.json", rows)


def cleanup_pending(dirfd, record, stream):
    guard = lock(dirfd, "pending.lock")
    try:
        if current(dirfd) != record:
            return
        # A file from a new generation is never removed using an old fd.
        try:
            st = os.stat(record["file"], dir_fd=dirfd, follow_symlinks=False)
        except FileNotFoundError:
            st = None
        if st is not None:
            if stream is None or (st.st_dev, st.st_ino) != (
                os.fstat(stream.fileno()).st_dev,
                os.fstat(stream.fileno()).st_ino,
            ):
                raise DeliveryError("source_identity_changed")
            unlink_file(dirfd, record["file"])
        unlink_file(dirfd, "pending.json")
        unlink_file(dirfd, "pending-ready.json")
    finally:
        os.close(guard)


def refresh_status():
    try:
        result = subprocess.run(
            [sys.executable, REFRESH, "refresh"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeliveryError("status_refresh_failed") from exc
    if result.returncode != 0:
        raise DeliveryError("status_refresh_failed")


def set_status(dirfd, record, status, error=None):
    write_json(
        dirfd,
        "delivery-status.json",
        {"id": record["id"], "status": status, "at": dt.datetime.now(dt.UTC).isoformat(), "error_code": error},
    )
    refresh_status()


def deliver_once(dirfd, remote):
    # Recover the visible M2 state even when the preceding run already
    # delivered and removed its pending pointer before refresh failed.
    refresh_status()
    pending = pending_record(dirfd)
    record = read_json(dirfd, "delivery-intent.json")
    if record is None:
        record = pending
        if record is None:
            rotate(dirfd, remote)
            status = read_json(dirfd, "delivery-status.json")
            if (
                isinstance(status, dict)
                and status.get("status") == "ok"
                and status.get("error_code") == "rotation_pending"
            ):
                rows = journal(dirfd)
                if rows and status.get("id") == rows[-1]["id"]:
                    set_status(dirfd, rows[-1], "ok")
            return
        # Persist the exact ID and byte identity before any remote operation.
        write_json(dirfd, "delivery-intent.json", record)
    else:
        valid_record(record)
    set_status(dirfd, record, "running")
    stream = None
    inspection_failed = False
    try:
        stream = open_source(dirfd, record)
        before = verify_source(stream, record) if stream is not None else None
        try:
            observed = remote.inspect(record)
        except DeliveryError:
            inspection_failed = True
            raise
        if observed["final"] is not None:
            if not matches(observed["final"], record):
                raise DeliveryError("remote_conflict", unknown=True)
        else:
            if observed["part"] is not None and not matches(observed["part"], record):
                # The producer may have replaced the only pending generation.
                # Keep the unidentified incomplete file if its source is gone.
                if stream is None:
                    raise DeliveryError("source_missing", unknown=True)
                remote.delete(record, observed["part"], final=False)
                observed["part"] = None
            if observed["part"] is None:
                if stream is None:
                    raise DeliveryError("source_missing", unknown=True)
                remote.write(record, stream)
                if _identity(before) != _identity(os.fstat(stream.fileno())):
                    raise DeliveryError("source_changed", unknown=True)
            remote.verify(record, final=False)
            remote.publish(record)
        remote.verify(record, final=True)
        remember(dirfd, record)
        rotation_note = None
        try:
            rotate(dirfd, remote)
        except (DeliveryError, OSError):
            # Delivery is confirmed; failed housekeeping must not be reported
            # as a failed copy or prevent a newer pending generation.
            rotation_note = "rotation_pending"
        cleanup_pending(dirfd, record, stream)
        unlink_file(dirfd, "delivery-intent.json")
        set_status(dirfd, record, "ok", rotation_note)
    except (DeliveryError, OSError) as exc:
        error = exc if isinstance(exc, DeliveryError) else DeliveryError("local_io")
        if error.code == "status_refresh_failed":
            # The durable status was already recorded. Do not replace a
            # confirmed delivery with a synthetic failure or repeat its write.
            raise
        # If a newer snapshot replaced an unavailable source, preserve the old
        # remote files but stop blocking delivery of the new generation.
        if error.code in {"source_missing", "remote_conflict"} or (stream is None and inspection_failed):
            replacement = pending_record(dirfd)
            if replacement is not None and replacement != record:
                unlink_file(dirfd, "delivery-intent.json")
        set_status(dirfd, record, "unknown" if error.unknown else "failed", error.code)
        if error is exc:
            raise
        raise error from exc
    finally:
        if stream is not None:
            stream.close()


def main():
    try:
        dirfd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        print("delivery incomplete: local_error", file=sys.stderr)
        return 1
    try:
        st = os.fstat(dirfd)
        if st.st_uid != OWNER_UID or st.st_mode & 0o077:
            raise DeliveryError("unsafe_directory")
        guard = lock(dirfd, "delivery.lock", nonblocking=True)
        try:
            deliver_once(dirfd, Windows())
        finally:
            os.close(guard)
    except (DeliveryError, OSError) as error:
        print(f"delivery incomplete: {getattr(error, 'code', 'local_error')}", file=sys.stderr)
        return 1
    finally:
        os.close(dirfd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
