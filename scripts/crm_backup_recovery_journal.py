"""Durable recovery intent only; never inspect or change a real service.

The caller must hold all separately verified writer/deployment fences for the
entire attempt and supply independently checked identities and state. A
successful journal write is a prerequisite, not authorization, for an effect.
After an ambiguous write or effect, reread before deciding what to do. Recovery
plans are hints requiring a service-specific identity and ownership check.
The directory is reserved for this journal. Store only nonsecret technical
identifiers; this file must not contain credentials or business data.
After a reboot, transitions fail closed; no automatic reconciliation is supplied.
The caller must check fence identity again before every external effect and keep
the parent paths and lock private from other same-UID processes throughout.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any


class RecoveryJournalError(RuntimeError):
    pass


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_JOURNAL = "crm-recovery.json"
_TEMP = ".crm-recovery.tmp"
_LOCK = ".crm-recovery.lock"
_IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:@-]{0,127}\Z")
_HEX = re.compile(r"[0-9a-f]{32}\Z")
_STEPS = {"untouched", "stop_intent", "stopped", "restore_intent", "restored"}
_MAX_BYTES = 256 * 1024
_MAX_ATTEMPTS = 4096


def _current_boot() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip().replace("-", "")
    except (OSError, UnicodeError) as exc:
        raise RecoveryJournalError("recovery_boot_unavailable") from exc
    if not _HEX.fullmatch(value):
        raise RecoveryJournalError("recovery_boot_invalid")
    return value


def _require_current_boot(value: str) -> None:
    if not isinstance(value, str) or not _HEX.fullmatch(value) or value != _current_boot():
        raise RecoveryJournalError("recovery_boot_mismatch")


def _private(fd: int) -> os.stat_result:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise RecoveryJournalError("recovery_file_unsafe")
    return info


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("short write")
        view = view[count:]


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryJournalError("recovery_state_invalid")
        result[key] = value
    return result


def _validate(state: Any) -> None:
    if not isinstance(state, dict) or set(state) != {"schema", "attempt", "attempts", "boot", "complete", "services"}:
        raise RecoveryJournalError("recovery_state_invalid")
    if (
        type(state["schema"]) is not int
        or state["schema"] != 2
        or not isinstance(state["attempt"], str)
        or not _HEX.fullmatch(state["attempt"])
        or not isinstance(state["attempts"], list)
        or not 1 <= len(state["attempts"]) <= _MAX_ATTEMPTS
        or any(not isinstance(value, str) or not _HEX.fullmatch(value) for value in state["attempts"])
        or len(set(state["attempts"])) != len(state["attempts"])
        or state["attempt"] != state["attempts"][-1]
        or not isinstance(state["boot"], str)
        or not _HEX.fullmatch(state["boot"])
        or type(state["complete"]) is not bool
        or not isinstance(state["services"], list)
        or not 1 <= len(state["services"]) <= 32
    ):
        raise RecoveryJournalError("recovery_state_invalid")
    seen = set()
    for item in state["services"]:
        if not isinstance(item, dict) or set(item) != {"name", "identity", "was_active", "step"}:
            raise RecoveryJournalError("recovery_state_invalid")
        if (
            not isinstance(item["name"], str)
            or not _IDENTIFIER.fullmatch(item["name"])
            or item["name"] in seen
            or not isinstance(item["identity"], str)
            or not _IDENTIFIER.fullmatch(item["identity"])
            or type(item["was_active"]) is not bool
            or not isinstance(item["step"], str)
            or item["step"] not in _STEPS
            or (not item["was_active"] and item["step"] != "untouched")
            or (state["complete"] and item["step"] not in {"untouched", "restored"})
        ):
            raise RecoveryJournalError("recovery_state_invalid")
        seen.add(item["name"])


def _synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        # An inherited RLock can be locked by a thread that vanished at fork.
        # Reject before acquiring it, not only after entering the mutex.
        self._require_owner_process()
        with self._mutex:
            self._require_owner_process()
            if not self._valid:
                raise RecoveryJournalError("recovery_session_closed")
            self._check_identity()
            return method(self, *args, **kwargs)

    return call


class RecoveryJournal:
    def __init__(self, directory: int, check_identity) -> None:
        self.directory = directory
        self._check_identity = check_identity
        self._mutex = threading.RLock()
        self._unstable = True
        self._valid = True
        self._owner_pid = os.getpid()

    def _require_owner_process(self) -> None:
        if os.getpid() != self._owner_pid:
            raise RecoveryJournalError("recovery_session_wrong_process")

    def _invalidate(self) -> None:
        # Serialize against the entire transaction before releasing flock/fds.
        # An old reference remains permanently unusable even if fd numbers recur.
        if os.getpid() != self._owner_pid:
            self._valid = False
            return
        # Prevent new operations immediately; existing ones finish under mutex.
        self._valid = False
        with self._mutex:
            pass

    @_synchronized
    def read(self) -> dict[str, Any] | None:
        if self._unstable:
            raise RecoveryJournalError("recovery_durability_unknown")
        return self._read_raw()

    def _read_raw(self) -> dict[str, Any] | None:
        try:
            fd = os.open(_JOURNAL, os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=self.directory)
        except FileNotFoundError:
            return None
        try:
            info = _private(fd)
            if info.st_size > _MAX_BYTES:
                raise RecoveryJournalError("recovery_state_invalid")
            data = os.read(fd, _MAX_BYTES + 1)
            if len(data) != info.st_size:
                raise RecoveryJournalError("recovery_state_invalid")
            try:
                state = json.loads(data, object_pairs_hook=_unique_pairs)
            except (ValueError, UnicodeDecodeError) as exc:
                raise RecoveryJournalError("recovery_state_invalid") from exc
            _validate(state)
            return state
        finally:
            os.close(fd)

    @_synchronized
    def confirm_durable(self) -> dict[str, Any] | None:
        """Reopen and fsync the exact published state; an orphan temp blocks use."""
        self._unstable = True
        try:
            os.stat(_TEMP, dir_fd=self.directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RecoveryJournalError("recovery_temp_present")
        state = self._read_raw()
        if state is not None:
            fd = os.open(_JOURNAL, os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=self.directory)
            try:
                _private(fd)
                os.fsync(fd)
            finally:
                os.close(fd)
        os.fsync(self.directory)
        self._check_identity()
        self._unstable = False
        return state

    def _save(self, state: dict[str, Any]) -> None:
        self._require_owner_process()
        if self._unstable:
            raise RecoveryJournalError("recovery_durability_unknown")
        _validate(state)
        data = json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(data) > _MAX_BYTES:
            raise RecoveryJournalError("recovery_state_invalid")
        # A leftover temp is an uncertain previous write: preserve it and fail closed.
        self._unstable = True
        fd = os.open(_TEMP, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=self.directory)
        try:
            _private(fd)
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(_TEMP, _JOURNAL, src_dir_fd=self.directory, dst_dir_fd=self.directory)
        os.fsync(self.directory)
        self._check_identity()
        self._unstable = False

    @_synchronized
    def begin(self, *, attempt: str, boot: str, services: list[dict[str, Any]]) -> None:
        _require_current_boot(boot)
        previous = self.read()
        if previous is not None and not previous["complete"]:
            raise RecoveryJournalError("recovery_previous_incomplete")
        attempts = [] if previous is None else previous["attempts"]
        if attempt in attempts:
            raise RecoveryJournalError("recovery_attempt_reused")
        if len(attempts) >= _MAX_ATTEMPTS:
            raise RecoveryJournalError("recovery_attempt_history_full")
        state = {
            "schema": 2,
            "attempt": attempt,
            "attempts": [*attempts, attempt],
            "boot": boot,
            "complete": False,
            "services": services,
        }
        _validate(state)
        if any(item["step"] != "untouched" for item in services):
            raise RecoveryJournalError("recovery_initial_step_invalid")
        self._save(state)

    @_synchronized
    def transition(self, *, attempt: str, current_boot: str, name: str, expected: str, new: str) -> None:
        _require_current_boot(current_boot)
        state = self.read()
        if state is None or state["attempt"] != attempt or state["complete"]:
            raise RecoveryJournalError("recovery_attempt_mismatch")
        if state["boot"] != current_boot:
            raise RecoveryJournalError("recovery_boot_mismatch")
        if (expected, new) not in {
            ("untouched", "stop_intent"),
            ("stop_intent", "stopped"),
            ("stop_intent", "restored"),
            ("stopped", "restore_intent"),
            ("restore_intent", "restored"),
        }:
            raise RecoveryJournalError("recovery_transition_invalid")
        item = next((item for item in state["services"] if item["name"] == name), None)
        if item is None or not item["was_active"] or item["step"] != expected:
            raise RecoveryJournalError("recovery_transition_conflict")
        item["step"] = new
        self._save(state)

    @_synchronized
    def complete(self, *, attempt: str, current_boot: str) -> None:
        _require_current_boot(current_boot)
        state = self.read()
        if state is None or state["attempt"] != attempt or state["complete"]:
            raise RecoveryJournalError("recovery_attempt_mismatch")
        if state["boot"] != current_boot:
            raise RecoveryJournalError("recovery_boot_mismatch")
        if any(item["step"] not in {"untouched", "restored"} for item in state["services"]):
            raise RecoveryJournalError("recovery_incomplete")
        state["complete"] = True
        self._save(state)

    @_synchronized
    def recovery_plan(self, *, current_boot: str) -> dict[str, Any]:
        if self._unstable:
            raise RecoveryJournalError("recovery_durability_unknown")
        _require_current_boot(current_boot)
        state = self.read()
        if state is None:
            return {"status": "absent", "services": []}
        actions = {
            "untouched": "leave_untouched",
            "stop_intent": "inspect_stop_outcome",
            "stopped": "verify_ownership_then_restore",
            "restore_intent": "inspect_restore_outcome",
            "restored": "verify_restored",
        }
        boot_changed = state["boot"] != current_boot
        return {
            "status": "complete" if state["complete"] else "incomplete",
            "boot_changed": boot_changed,
            "services": [
                {
                    "name": item["name"],
                    "identity": item["identity"],
                    "was_active": item["was_active"],
                    "action": ("inspect_after_boot" if boot_changed else actions[item["step"]]),
                }
                for item in state["services"]
            ],
        }


@contextmanager
def locked_journal(path: Path):
    """Hold a nonblocking lock from before capture until all effects end."""
    owner_pid = os.getpid()
    safe_release = True
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise RecoveryJournalError("recovery_directory_unsafe")
    descriptors: list[int] = []
    links: list[tuple[int, str, int]] = []
    root = os.open("/", os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
    descriptors.append(root)
    try:
        parent = root
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=parent)
            descriptors.append(child)
            links.append((parent, part, child))
            info = os.fstat(child)
            if info.st_uid not in {0, os.geteuid()} or (
                info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
            ):
                raise RecoveryJournalError("recovery_directory_unsafe")
            parent = child
        directory = parent
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise RecoveryJournalError("recovery_directory_unsafe")
        lock = os.open(_LOCK, os.O_RDWR | os.O_CREAT | _NOFOLLOW | _NONBLOCK, 0o600, dir_fd=directory)
        try:
            _private(lock)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RecoveryJournalError("recovery_busy") from exc
            try:

                def verify_identity() -> None:
                    for parent_fd, name, child_fd in links:
                        opened = os.fstat(child_fd)
                        try:
                            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                        except FileNotFoundError as exc:
                            raise RecoveryJournalError("recovery_directory_changed") from exc
                        if not stat.S_ISDIR(current.st_mode) or (opened.st_dev, opened.st_ino) != (
                            current.st_dev,
                            current.st_ino,
                        ):
                            raise RecoveryJournalError("recovery_directory_changed")
                    opened_lock = _private(lock)
                    try:
                        current_lock = os.stat(_LOCK, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError as exc:
                        raise RecoveryJournalError("recovery_lock_changed") from exc
                    if not stat.S_ISREG(current_lock.st_mode) or (opened_lock.st_dev, opened_lock.st_ino) != (
                        current_lock.st_dev,
                        current_lock.st_ino,
                    ):
                        raise RecoveryJournalError("recovery_lock_changed")

                journal = RecoveryJournal(directory, verify_identity)
                # If invalidation is interrupted, leaking the open descriptors
                # retains flock; closing them could admit a concurrent writer.
                safe_release = False
                try:
                    journal.confirm_durable()
                    yield journal
                finally:
                    journal._invalidate()
                    safe_release = True
            finally:
                # flock is tied to the shared open file description. A forked
                # child's LOCK_UN would release the parent's live lock too.
                if os.getpid() == owner_pid and safe_release:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            if safe_release or os.getpid() != owner_pid:
                os.close(lock)
    finally:
        if safe_release or os.getpid() != owner_pid:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
