#!/usr/bin/env python3
"""Create one atomic online SQLite backup of Automation Center state."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import tempfile
from pathlib import Path


DEFAULT_SOURCE = Path("/var/lib/autostop-manager-scheduler/registry.sqlite3")


def _absolute_path(raw: str, *, code: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(code)
    return path


def _private_regular_file(path: Path, *, code: str) -> None:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise ValueError(code)


def _private_directory(path: Path, *, code: str) -> None:
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise ValueError(code)


def backup(source: Path, output: Path) -> dict[str, object]:
    _private_regular_file(source, code="automation_backup_source_invalid")
    _private_directory(output.parent, code="automation_backup_directory_invalid")
    try:
        output.lstat()
    except FileNotFoundError:
        pass
    else:
        raise ValueError("automation_backup_output_exists")

    descriptor, raw_temporary = tempfile.mkstemp(prefix=".automation-registry.", suffix=".tmp", dir=output.parent)
    temporary = Path(raw_temporary)
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    try:
        source_uri = f"file:{source.as_posix()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True, timeout=10) as origin:
            with sqlite3.connect(temporary, timeout=10) as destination:
                origin.backup(destination)
                check = destination.execute("PRAGMA quick_check").fetchone()
                version = destination.execute(
                    "SELECT version FROM manager_automation_schema WHERE component = 'automation_center'"
                ).fetchone()
                if check != ("ok",) or version is None or type(version[0]) is not int:
                    raise ValueError("automation_backup_verification_failed")
        os.chmod(temporary, 0o600, follow_symlinks=False)
        with temporary.open("rb") as backup_file:
            os.fsync(backup_file.fileno())
        os.replace(temporary, output)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        directory_fd = os.open(output.parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "ok": True,
        "format": "autostop_manager_automation_backup_v1",
        "schema_version": int(version[0]),
        "output": str(output),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default=os.environ.get("AUTOSTOP_AUTOMATION_DB", str(DEFAULT_SOURCE)),
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = backup(
            _absolute_path(args.source, code="automation_backup_source_invalid"),
            _absolute_path(args.output, code="automation_backup_output_invalid"),
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        code = str(exc) if isinstance(exc, ValueError) else "automation_backup_failed"
        print(json.dumps({"ok": False, "error": code}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
