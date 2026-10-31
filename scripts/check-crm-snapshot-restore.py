#!/usr/bin/env python3
"""Restore and check one prepared CRM snapshot in an empty private directory.

Never points the application to this directory, starts services, or replays jobs.
The caller deletes the isolated restore after acceptance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import tarfile

from cryptography.fernet import Fernet


def _record(hasher, info, digest=""):
    item = [info.name, info.isdir(), info.size, info.mode, info.uid, info.gid, info.mtime, digest]
    hasher.update(json.dumps(item, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n")


def check_archive(archive_path: Path, destination: Path):
    if destination.is_symlink() or not destination.is_dir():
        raise ValueError("restore directory is not a directory")
    root = destination.resolve(strict=True)
    st = root.stat()
    if st.st_uid != os.geteuid() or st.st_mode & 0o077 or any(root.iterdir()):
        raise ValueError("restore directory must be empty and private")
    seen = set()
    digest = hashlib.sha256()
    directories = []
    manifest = None
    with tarfile.open(archive_path, "r:gz") as archive:
        for entry in archive:
            parts = PurePosixPath(entry.name).parts
            if (
                not parts
                or PurePosixPath(entry.name).is_absolute()
                or any(p in (".", "..") for p in parts)
                or entry.name in seen
                or not (entry.isfile() or entry.isdir())
                or manifest is not None
            ):
                raise ValueError("unsupported archive member")
            seen.add(entry.name)
            if entry.name == "manifest.json":
                if not entry.isfile() or entry.size > 4096:
                    raise ValueError("invalid manifest")
                manifest = json.load(archive.extractfile(entry))
                continue
            if not (parts[0] == "crm" or entry.name == "oauth-state.key") or (
                parts[0] == "oauth-state.key" and len(parts) != 1
            ):
                raise ValueError("unsupported archive layout")
            out = root.joinpath(*parts)
            if not out.parent.is_dir() or out.parent.is_symlink():
                raise ValueError("archive parent missing")
            if entry.isdir():
                out.mkdir(mode=0o700)
                directories.append((out, entry.mode, entry.uid, entry.gid))
                _record(digest, entry)
            else:
                fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                h = hashlib.sha256()
                remaining = entry.size
                with os.fdopen(fd, "wb") as target, archive.extractfile(entry) as source:
                    while remaining:
                        block = source.read(min(1024 * 1024, remaining))
                        if not block:
                            raise ValueError("truncated archive")
                        target.write(block)
                        h.update(block)
                        remaining -= len(block)
                os.chmod(out, entry.mode & 0o777)
                if os.geteuid() == 0:
                    os.chown(out, entry.uid, entry.gid)
                _record(digest, entry, h.hexdigest())
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != 1
        or not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("crm_revision", "")))
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(manifest.get("crm_image", "")))
        or manifest.get("tree_sha256") != digest.hexdigest()
    ):
        raise ValueError("archive integrity mismatch")
    if not {
        "crm",
        "crm/state.json",
        "crm/settings.json",
        "crm/users.json",
        "crm/mcp-oauth-state.json",
        "crm/change_feed.sqlite3",
        "oauth-state.key",
    }.issubset(seen):
        raise ValueError("archive incomplete")
    for name in ("state.json", "settings.json", "users.json"):
        with (root / "crm" / name).open("rb") as file:
            json.load(file)
    key = (root / "oauth-state.key").read_bytes()
    state = (root / "crm" / "mcp-oauth-state.json").read_bytes()
    if not isinstance(json.loads(Fernet(key).decrypt(state)), dict):
        raise ValueError("restored OAuth state invalid")
    path = root / "crm" / "change_feed.sqlite3"
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("restored SQLite corrupt")
    finally:
        connection.close()
    for path, mode, uid, gid in reversed(directories):
        os.chmod(path, mode & 0o777)
        if os.geteuid() == 0:
            os.chown(path, uid, gid)
    return manifest["crm_revision"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        revision = check_archive(args.archive, args.destination)
    except Exception:  # noqa: BLE001 - avoid leaking secret-bearing inputs in tracebacks
        print("Isolated CRM restore check failed.")
        return 1
    print("Isolated CRM data, SQLite and saved OAuth key verified; revision:", revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
