"""Small standard-library primitives shared by the maintenance components."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path


ROOT_UID = 0


class MaintenanceError(RuntimeError):
    """A fixed technical error code, never an external command's raw stderr."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def run(argv: list[str], *, timeout: float = 30, input_file=None) -> subprocess.CompletedProcess[str]:
    """Capture output for parsing; callers must never log raw command errors."""
    try:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"NOTIFY_SOCKET", "WATCHDOG_PID", "WATCHDOG_USEC"}
        }
        if input_file is None:
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, env=environment)
        path = Path(input_file)
        no_symlink_ancestors(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise MaintenanceError("command_input_invalid")
            return subprocess.run(
                argv, stdin=source, capture_output=True, text=True, timeout=timeout, check=False, env=environment
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MaintenanceError("command_unavailable") from exc


def checked_run(argv: list[str], *, timeout: float = 30, input_file=None) -> str:
    result = run(argv, timeout=timeout, input_file=input_file)
    if result.returncode:
        raise MaintenanceError("command_failed")
    return result.stdout


def no_symlink_ancestors(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise MaintenanceError("path_not_canonical")
    for parent in reversed(path.parents):
        if parent.is_symlink() or not parent.is_dir():
            raise MaintenanceError("path_parent_invalid")


def private_directory(path: Path) -> None:
    no_symlink_ancestors(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != ROOT_UID or stat.S_IMODE(info.st_mode) != 0o700:
        raise MaintenanceError("private_directory_invalid")


def private_file(path: Path) -> None:
    private_directory(path.parent)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != ROOT_UID or stat.S_IMODE(info.st_mode) != 0o600:
        raise MaintenanceError("private_file_invalid")


def read_json(path: Path, *, private: bool = False) -> dict:
    path = Path(path)
    no_symlink_ancestors(path)
    if private:
        private_file(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024**2:
            raise MaintenanceError("json_file_invalid")
        if private and (info.st_uid != ROOT_UID or stat.S_IMODE(info.st_mode) != 0o600):
            raise MaintenanceError("private_file_invalid")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as source:
            payload = json.load(source)
    except (ValueError, UnicodeError) as exc:
        raise MaintenanceError("json_file_invalid") from exc
    finally:
        os.close(fd)
    if not isinstance(payload, dict):
        raise MaintenanceError("json_object_required")
    return payload


def atomic_json(path: Path, payload: dict) -> None:
    path = Path(path)
    private_directory(path.parent)
    if path.exists() or path.is_symlink():
        private_file(path)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=".maintenance-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def file_identity(path: Path) -> dict:
    info = Path(path).lstat()
    kind = "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "other"
    return {
        "dev": info.st_dev,
        "ino": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "nlink": info.st_nlink,
        "allocated_bytes": info.st_blocks * 512,
        "type": kind,
    }


def sha256_file(path: Path) -> str:
    path = Path(path)
    no_symlink_ancestors(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise MaintenanceError("hash_source_invalid")
        hasher = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as source:
            while chunk := source.read(1024**2):
                hasher.update(chunk)
        after = os.fstat(fd)
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise MaintenanceError("hash_source_changed")
        return hasher.hexdigest()
    finally:
        os.close(fd)


def unique_allocated(path: Path) -> int:
    """Physical blocks once per inode, including directory blocks, no mount escape."""
    root = Path(path)
    no_symlink_ancestors(root)
    device = root.lstat().st_dev
    seen = set()
    total = 0
    pending = [root]
    while pending:
        current = pending.pop()
        info = current.lstat()
        if info.st_dev != device or (current != root and os.path.ismount(current)):
            raise MaintenanceError("source_mount_escape")
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise MaintenanceError("source_special_file")
        identity = (info.st_dev, info.st_ino)
        if identity not in seen:
            seen.add(identity)
            total += info.st_blocks * 512
        if stat.S_ISDIR(info.st_mode):
            pending.extend(current.iterdir())
    return total


def _within(value: Path, root: Path) -> bool:
    return value == root or value.is_relative_to(root)


def process_references(paths: Iterable[Path]) -> dict[str, list[str]]:
    """Return only content-free PID reference labels, never argv/environment."""
    roots = [Path(path).absolute() for path in paths]
    result: dict[str, list[str]] = {str(root): [] for root in roots}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        locations = [entry / "cwd", entry / "exe"]
        try:
            locations.extend((entry / "fd").iterdir())
        except FileNotFoundError:
            continue
        except PermissionError as exc:
            raise MaintenanceError("process_references_unavailable") from exc
        for location in locations:
            try:
                raw = os.readlink(location)
            except PermissionError as exc:
                raise MaintenanceError("process_references_unavailable") from exc
            except (FileNotFoundError, OSError):
                continue
            target = Path(raw.removesuffix(" (deleted)"))
            if not target.is_absolute():
                continue
            for root in roots:
                label = "pid:" + entry.name
                if _within(target, root) and label not in result[str(root)]:
                    result[str(root)].append(label)
        try:
            maps = (entry / "maps").read_text().splitlines()
        except FileNotFoundError:
            continue
        except PermissionError as exc:
            raise MaintenanceError("process_references_unavailable") from exc
        for line in maps:
            columns = line.split(maxsplit=5)
            if len(columns) != 6 or not columns[5].startswith("/"):
                continue
            target = Path(columns[5].removesuffix(" (deleted)"))
            for root in roots:
                label = "pid:" + entry.name
                if _within(target, root) and label not in result[str(root)]:
                    result[str(root)].append(label)
    return result


def unit_references(paths: Iterable[Path]) -> dict[str, list[str]]:
    """Detect path references in loaded units without returning their raw config."""
    roots = [Path(path).absolute() for path in paths]
    result: dict[str, list[str]] = {str(root): [] for root in roots}
    loaded = checked_run(["systemctl", "list-units", "--all", "--type=service", "--no-legend", "--plain"])
    names = [line.split()[0] for line in loaded.splitlines() if line.split()]
    if not names:
        return result
    properties = checked_run(
        ["systemctl", "show", *names, "-p", "Id", "-p", "WorkingDirectory", "-p", "ExecStart", "-p", "EnvironmentFiles"]
    )
    for block in properties.split("\n\n"):
        unit = next((line[3:] for line in block.splitlines() if line.startswith("Id=")), "unknown-unit")
        for root in roots:
            needle = str(root)
            if any(needle in line for line in block.splitlines() if not line.startswith("Id=")):
                result[needle].append(unit)
    return result
