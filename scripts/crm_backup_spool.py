"""Private, bounded single-pending-file spool. Does not create a CRM snapshot.

The caller must supply a completed, immutable snapshot and retain the lock for
the entire transfer and acknowledgement. Lock acquisition is nonblocking and
must happen before stopping any writers. An ambiguous filesystem error must be
reported as unknown; never assume it means that publication did not happen.
An orphan without a pointer requires manual recovery, including when the very
first publication was interrupted before its pointer became durable.
"""

from __future__ import annotations

import fcntl
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class SpoolError(RuntimeError):
    pass


_GEN = re.compile(r"crm-pending-[0-9a-f]{32}\.bundle\Z")
_TMP = re.compile(r"\.crm-spool-[0-9a-f]{32}\.tmp\Z")
_POINTER = "crm-pending.current"
_LOCK = ".crm-spool.lock"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


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


class LockedSpool:
    def __init__(self, directory: int, max_bytes: int) -> None:
        self.directory = directory
        self.max_bytes = max_bytes

    def _read_pointer(self) -> str | None:
        try:
            fd = os.open(_POINTER, os.O_RDONLY | _NOFOLLOW, dir_fd=self.directory)
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
            if not _GEN.fullmatch(name):
                raise SpoolError("spool_pointer_invalid")
            return name
        finally:
            os.close(fd)

    def _open_generation(self, name: str) -> int:
        fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=self.directory)
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
        generations = [name for name in entries if _GEN.fullmatch(name)]
        temporaries = [name for name in entries if _TMP.fullmatch(name)]
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
            fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=self.directory)
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
        source = os.fstat(source_fd)
        if not stat.S_ISREG(source.st_mode) or source.st_size <= 0 or source.st_size > self.max_bytes:
            raise SpoolError("spool_source_invalid")
        self._reconcile()
        new_name = f"crm-pending-{uuid4().hex}.bundle"
        data_tmp = f".crm-spool-{uuid4().hex}.tmp"
        pointer_tmp = f".crm-spool-{uuid4().hex}.tmp"
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
        os.rename(data_tmp, new_name, src_dir_fd=self.directory, dst_dir_fd=self.directory)
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
        """Yield (generation, readable fd), or (None, None), under the lock."""
        name = self._read_pointer()
        if name is None:
            if any(_GEN.fullmatch(entry) for entry in os.listdir(self.directory)):
                raise SpoolError("spool_recovery_required")
            yield None, None
            return
        fd = self._open_generation(name)
        try:
            yield name, fd
        finally:
            os.close(fd)

    def acknowledge(self, generation: str) -> None:
        """Only call after independently confirming remote publication."""
        if not _GEN.fullmatch(generation) or self._read_pointer() != generation:
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
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise SpoolError("spool_directory_unsafe")
        lock = os.open(_LOCK, os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o600, dir_fd=directory)
        try:
            _private_regular(lock)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SpoolError("spool_busy") from exc
            try:
                yield LockedSpool(directory, max_bytes)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            os.close(lock)
    finally:
        os.close(directory)
