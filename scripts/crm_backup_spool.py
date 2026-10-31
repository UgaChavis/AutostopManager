"""Private, bounded single-pending-file spool. Does not create a CRM snapshot.

The caller must supply a completed, immutable snapshot and retain the lock for
the entire transfer and acknowledgement. Lock acquisition is nonblocking and
must happen before stopping any writers. An ambiguous filesystem error must be
reported as unknown; never assume it means that publication did not happen.
An orphan without a pointer requires manual recovery, including when the very
first publication was interrupted before its pointer became durable.
The private key authenticates names owned by this spool; it is not encryption.
Losing or corrupting the key requires manual recovery and never permits cleanup.
One process and one owner thread must use the session. The implementation owns
its private descriptors; callers must not close, duplicate, replace, or alter
them. The pending descriptor is borrowed and must not be closed by the caller.
An ambiguous close, ownership violation, or changed descriptor may deliberately
retain locks until process exit; report that condition instead of retrying.
Use a short-lived process and terminate it after an uncertain close; do not fork
while a pending descriptor is borrowed, since the child inherits its lock.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import os
import re
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class SpoolError(RuntimeError):
    pass


_GEN = re.compile(r"crm-pending-([0-9a-f]{32})-([0-9a-f]{64})\.bundle\Z")
_TMP = re.compile(r"\.crm-spool-([0-9a-f]{32})-([0-9a-f]{64})\.tmp\Z")
_POINTER = "crm-pending.current"
_LOCK = ".crm-spool.lock"
_KEY = ".crm-spool.key"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def _private_regular(fd: int) -> os.stat_result:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise SpoolError("spool_file_unsafe")
    return info


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("short write")
        view = view[count:]


def _load_or_create_key(directory: int) -> bytes:
    try:
        fd = os.open(_KEY, os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        names = os.listdir(directory)
        if any(name == _POINTER or name.startswith(("crm-pending-", ".crm-spool-")) for name in names):
            raise SpoolError("spool_key_missing") from None
        fd = os.open(_KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=directory)
        try:
            _private_regular(fd)
            key = os.urandom(32)
            _write_all(fd, key)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(directory)
        return key
    try:
        info = _private_regular(fd)
        if info.st_size != 32:
            raise SpoolError("spool_key_invalid")
        key = os.read(fd, 33)
        if len(key) != 32:
            raise SpoolError("spool_key_invalid")
        return key
    finally:
        os.close(fd)


class LockedSpool:
    def __init__(self, directory: int, lock: int, path: Path, max_bytes: int, key: bytes) -> None:
        self.directory = directory
        self._lock = lock
        self._path = path
        self.max_bytes = max_bytes
        self.key = key
        self._pid = os.getpid()
        # Thread identifiers can be reused after a thread exits.
        self._thread = threading.current_thread()
        self._active = True
        self._borrowed: set[object] = set()
        self._release_deferred = False
        self._poisoned = False
        self._directory_identity = self._identity(directory)
        self._lock_identity = self._identity(lock)
        self._lock_guard = os.dup(lock)
        try:
            # The dedicated private directory gets its own OFD guard so a
            # reused descriptor for the same inode cannot impersonate it.
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._directory_guard = os.dup(directory)
        except BaseException:
            os.close(self._lock_guard)
            raise

    @staticmethod
    def _identity(fd: int) -> tuple[int, int]:
        info = os.fstat(fd)
        return info.st_dev, info.st_ino

    def _check_owner(self) -> None:
        if not self._active:
            raise SpoolError("spool_context_closed")
        if self._poisoned:
            raise SpoolError("spool_recovery_required")
        if os.getpid() != self._pid:
            raise SpoolError("spool_wrong_process")
        if threading.current_thread() is not self._thread:
            raise SpoolError("spool_wrong_thread")
        try:
            _private_regular(self._lock)
            self._check_same_locked_file(self._lock, self._lock_guard, self._lock_identity)
            self._check_same_locked_file(self.directory, self._directory_guard, self._directory_identity)
            current_directory = os.stat(self._path, follow_symlinks=False)
            current_lock = os.stat(_LOCK, dir_fd=self.directory, follow_symlinks=False)
            if (
                self._identity(self.directory) != self._directory_identity
                or self._identity(self._lock) != self._lock_identity
                or not stat.S_ISDIR(current_directory.st_mode)
                or current_directory.st_uid != os.geteuid()
                or stat.S_IMODE(current_directory.st_mode) != 0o700
                or (current_directory.st_dev, current_directory.st_ino) != self._directory_identity
                or not stat.S_ISREG(current_lock.st_mode)
                or (current_lock.st_dev, current_lock.st_ino) != self._lock_identity
            ):
                raise SpoolError("spool_descriptor_changed")
        except OSError as exc:
            raise SpoolError("spool_descriptor_changed") from exc

    @classmethod
    def _check_same_locked_file(cls, first: int, second: int, expected: tuple[int, int]) -> None:
        try:
            if cls._identity(first) != expected or cls._identity(second) != expected:
                raise SpoolError("spool_descriptor_changed")
            # Duplicates share the flock; a separately reopened descriptor for
            # the same inode conflicts with the guard's exclusive lock.
            fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SpoolError("spool_descriptor_changed") from exc

    def _release(self) -> None:
        # Keep the flock while the directory descriptor is still usable.
        # Closing the last lock FD releases flock; never LOCK_UN an inherited OFD.
        self._check_same_locked_file(self._lock, self._lock_guard, self._lock_identity)
        self._check_same_locked_file(self.directory, self._directory_guard, self._directory_identity)
        os.close(self.directory)
        os.close(self._directory_guard)
        # If either close is interrupted, the guard retains the lock.
        os.close(self._lock)
        os.close(self._lock_guard)

    def _close_scope(self) -> None:
        self._active = False
        if os.getpid() != self._pid:
            self._release()
            return
        if threading.current_thread() is not self._thread:
            # An unexpected cross-thread exit must not release a lock while its
            # owner may still be operating. Leak safely until process exit.
            raise SpoolError("spool_wrong_thread")
        if self._poisoned:
            raise SpoolError("spool_recovery_required")
        if self._borrowed:
            self._release_deferred = True
            raise SpoolError("spool_pending_active")
        self._release()

    def _tag(self, kind: str, nonce: str) -> str:
        return hmac.new(self.key, f"{kind}:{nonce}".encode("ascii"), hashlib.sha256).hexdigest()

    def _new_name(self, kind: str) -> str:
        nonce = uuid4().hex
        tag = self._tag(kind, nonce)
        if kind == "generation":
            return f"crm-pending-{nonce}-{tag}.bundle"
        return f".crm-spool-{nonce}-{tag}.tmp"

    def _valid_name(self, name: str, kind: str) -> bool:
        match = (_GEN if kind == "generation" else _TMP).fullmatch(name)
        return bool(match and hmac.compare_digest(match.group(2), self._tag(kind, match.group(1))))

    def _read_pointer(self) -> str | None:
        try:
            fd = os.open(_POINTER, os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=self.directory)
        except FileNotFoundError:
            return None
        try:
            _private_regular(fd)
            raw = os.read(fd, 128)
            if os.read(fd, 1) or not raw.endswith(b"\n"):
                raise SpoolError("spool_pointer_invalid")
            try:
                name = raw[:-1].decode("ascii")
            except UnicodeDecodeError as exc:
                raise SpoolError("spool_pointer_invalid") from exc
            if not self._valid_name(name, "generation"):
                raise SpoolError("spool_pointer_invalid")
            return name
        finally:
            os.close(fd)

    def _open_generation(self, name: str) -> int:
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=self.directory)
        try:
            info = _private_regular(fd)
            if info.st_size <= 0 or info.st_size > self.max_bytes:
                raise SpoolError("spool_size_invalid")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _reconcile(self) -> str | None:
        current = self._read_pointer()
        entries = os.listdir(self.directory)
        generation_names = [name for name in entries if name.startswith("crm-pending-")]
        temporary_names = [name for name in entries if name.startswith(".crm-spool-")]
        if any(not self._valid_name(name, "generation") for name in generation_names) or any(
            not self._valid_name(name, "temporary") for name in temporary_names
        ):
            raise SpoolError("spool_unknown_file")
        generations = generation_names
        temporaries = temporary_names
        if current is None and generations:
            # Could be an interrupted acknowledgement: preserve everything.
            raise SpoolError("spool_recovery_required")
        if current is not None:
            fd = self._open_generation(current)
            os.close(fd)
            # Make the observed pointer durable before deleting an older generation.
            os.fsync(self.directory)
        for name in [*generations, *temporaries]:
            if name == current:
                continue
            fd = os.open(name, os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=self.directory)
            try:
                _private_regular(fd)
            finally:
                os.close(fd)
            os.unlink(name, dir_fd=self.directory)
        if generations or temporaries:
            os.fsync(self.directory)
        return current

    def publish(self, source_fd: int) -> str:
        """Publish a completed immutable file; return its opaque generation ID."""
        self._check_owner()
        source = os.fstat(source_fd)
        if not stat.S_ISREG(source.st_mode) or source.st_size <= 0 or source.st_size > self.max_bytes:
            raise SpoolError("spool_source_invalid")
        self._reconcile()
        new_name = self._new_name("generation")
        data_tmp = self._new_name("temporary")
        pointer_tmp = self._new_name("temporary")
        output = os.open(data_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=self.directory)
        try:
            _private_regular(output)
            position = 0
            while position < source.st_size:
                data = os.pread(source_fd, min(1024 * 1024, source.st_size - position), position)
                if not data:
                    raise SpoolError("spool_source_changed")
                _write_all(output, data)
                position += len(data)
            after = os.fstat(source_fd)
            if (source.st_dev, source.st_ino, source.st_size, source.st_mtime_ns, source.st_ctime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise SpoolError("spool_source_changed")
            os.fsync(output)
        finally:
            os.close(output)
        # link(2) fails if the generation already exists; rename would overwrite it.
        # An interruption between link and unlink leaves a hard link and fails
        # closed for manual recovery instead of deleting an uncertain object.
        os.link(data_tmp, new_name, src_dir_fd=self.directory, dst_dir_fd=self.directory, follow_symlinks=False)
        os.unlink(data_tmp, dir_fd=self.directory)
        os.fsync(self.directory)
        pointer = os.open(pointer_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=self.directory)
        try:
            _private_regular(pointer)
            _write_all(pointer, (new_name + "\n").encode("ascii"))
            os.fsync(pointer)
        finally:
            os.close(pointer)
        os.replace(pointer_tmp, _POINTER, src_dir_fd=self.directory, dst_dir_fd=self.directory)
        os.fsync(self.directory)
        # Any failure here is an unknown result, never a reason to delete current.
        self._reconcile()
        return new_name

    @contextmanager
    def pending(self):
        """Yield a borrowed readable fd under the lock; callers must not close it."""
        self._check_owner()
        name = self._read_pointer()
        if name is None:
            if any(entry.startswith("crm-pending-") for entry in os.listdir(self.directory)):
                raise SpoolError("spool_recovery_required")
            yield None, None
            return
        fd = None
        borrowed_fd = None
        ticket = object()
        try:
            fd = self._open_generation(name)
            identity = self._identity(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            borrowed_fd = os.dup(fd)
            self._borrowed.add(ticket)
            yield name, borrowed_fd
        finally:
            if os.getpid() != self._pid:
                raise SpoolError("spool_wrong_process")
            if threading.current_thread() is not self._thread:
                raise SpoolError("spool_wrong_thread")
            try:
                if borrowed_fd is not None:
                    self._check_same_locked_file(borrowed_fd, fd, identity)
                    os.close(borrowed_fd)
                if fd is not None:
                    os.close(fd)
            except BaseException:
                # close() may have taken effect despite an interruption. Never
                # release the spool lock after an ambiguous borrowed-fd close.
                self._poisoned = True
                raise
            try:
                self._borrowed.discard(ticket)
            finally:
                if self._release_deferred and not self._borrowed:
                    self._release()
                    self._release_deferred = False

    def acknowledge(self, generation: str) -> None:
        """Only call after independently confirming remote publication."""
        self._check_owner()
        if not self._valid_name(generation, "generation") or self._read_pointer() != generation:
            raise SpoolError("spool_generation_mismatch")
        fd = self._open_generation(generation)
        os.close(fd)
        os.unlink(_POINTER, dir_fd=self.directory)
        # A crash or error here leaves the generation for manual reconciliation.
        os.fsync(self.directory)
        os.unlink(generation, dir_fd=self.directory)
        os.fsync(self.directory)


@contextmanager
def locked_spool(path: Path, *, max_bytes: int):
    """Try to lock an existing private directory without waiting for a transfer."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise SpoolError("spool_limit_invalid")
    path = Path(os.path.abspath(path))
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
    lock = None
    holder = None
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise SpoolError("spool_directory_unsafe")
        lock = os.open(_LOCK, os.O_RDWR | os.O_CREAT | _NOFOLLOW | _NONBLOCK, 0o600, dir_fd=directory)
        _private_regular(lock)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SpoolError("spool_busy") from exc
        holder = LockedSpool(directory, lock, path, max_bytes, _load_or_create_key(directory))
        yield holder
    finally:
        if holder is None:
            try:
                os.close(directory)
            finally:
                if lock is not None:
                    os.close(lock)
        else:
            try:
                holder._close_scope()
            finally:
                # Even a signal before the close body must poison escaped handles.
                holder._active = False
