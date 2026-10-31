#!/usr/bin/env python3
"""Prepare one private CRM-data archive after a coordinator stops its writers.

The caller owns fencing, verifies the container did not restart, and publishes
only after that verification. This program does not stop or start services.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
from datetime import datetime, UTC

from cryptography.fernet import Fernet

EXCLUDED = frozenset({"backups", "maintenance-backups", "logs", "searxng"})
REQUIRED = frozenset({"state.json", "settings.json", "users.json", "mcp-oauth-state.json", "change_feed.sqlite3"})


def container_context(container_id: str, data_dir: Path, revision: str) -> tuple[bytes, str]:
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise ValueError("invalid container identity")
    result = subprocess.run(["docker", "inspect", container_id], capture_output=True, check=True, timeout=15)
    item = json.loads(result.stdout)
    if not isinstance(item, list) or len(item) != 1:
        raise ValueError("invalid container inspection")
    item = item[0]
    state = item["State"]
    if item["Id"] != container_id or state["Running"] or state["ExitCode"] != 0 or state["OOMKilled"]:
        raise ValueError("container is not cleanly stopped")
    image = item["Image"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("invalid image identity")
    image_result = subprocess.run(["docker", "image", "inspect", image], capture_output=True, check=True, timeout=15)
    images = json.loads(image_result.stdout)
    if (
        len(images) != 1
        or images[0]["Id"] != image
        or images[0]["Config"]["Labels"].get("org.opencontainers.image.revision") != revision
    ):
        raise ValueError("CRM image revision mismatch")
    mounts = [m for m in item["Mounts"] if m.get("Destination") == "/home/autostop/.minimal-kanban"]
    if (
        len(mounts) != 1
        or Path(mounts[0]["Source"]).resolve(strict=True) != data_dir.resolve(strict=True)
        or not mounts[0]["RW"]
    ):
        raise ValueError("CRM data mount mismatch")
    values = [s.split("=", 1)[1] for s in item["Config"]["Env"] if s.startswith("AUTOSTOP_MCP_OAUTH_STATE_KEY=")]
    if len(values) != 1:
        raise ValueError("OAuth key unavailable")
    key = values[0].encode("ascii")
    Fernet(key)
    return key, image


def _walk(root: Path):
    entries = []

    def visit(directory: Path):
        for child in sorted(directory.iterdir(), key=lambda x: x.name):
            if directory == root and child.name in EXCLUDED:
                continue
            metadata = child.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                entries.append((child, metadata, True))
                visit(child)
            elif stat.S_ISREG(metadata.st_mode):
                entries.append((child, metadata, False))
            else:
                raise ValueError("unsupported file type")

    visit(root)
    return entries


def _stable(before, after):
    return (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )


def _file(path: Path, expected):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    f = os.fdopen(fd, "rb")
    if not _stable(expected, os.fstat(fd)):
        f.close()
        raise ValueError("source changed")
    return f


class DigestReader:
    def __init__(self, source):
        self.source = source
        self.hash = hashlib.sha256()

    def read(self, size=-1):
        chunk = self.source.read(size)
        self.hash.update(chunk)
        return chunk


def _record(hasher, info, digest=""):
    item = [info.name, info.isdir(), info.size, info.mode, info.uid, info.gid, info.mtime, digest]
    hasher.update(json.dumps(item, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n")


def _info(name, metadata, directory=False):
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.mode = stat.S_IMODE(metadata.st_mode)
    info.uid, info.gid = metadata.st_uid, metadata.st_gid
    info.mtime = int(metadata.st_mtime)
    info.size = 0 if directory else metadata.st_size
    return info


def _sqlite_copy(path: Path, target: Path):
    source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    dest = sqlite3.connect(target)
    try:
        source.backup(dest)
        if dest.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("sqlite integrity check failed")
    finally:
        dest.close()
        source.close()


def create_snapshot(root: Path, output: Path, revision: str, key: bytes, image: str):  # noqa: C901 - single archive transaction
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("invalid CRM revision")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("invalid image identity")
    root = root.resolve(strict=True)
    if not root.is_dir() or not REQUIRED.issubset({p.name for p in root.iterdir()}):
        raise ValueError("CRM data incomplete")
    parent = output.parent.resolve(strict=True)
    pst = parent.stat()
    if not stat.S_ISDIR(pst.st_mode) or pst.st_uid != os.geteuid() or pst.st_mode & 0o077:
        raise ValueError("staging directory must be private")
    if parent == root or parent.is_relative_to(root):
        raise ValueError("staging directory inside source")
    output = parent / output.name
    entries = _walk(root)
    paths = {p for p, _, directory in entries if not directory}
    sqlite_paths = {root / "change_feed.sqlite3"}
    found_feed = False
    for path, metadata, is_dir in entries:
        if not is_dir and path.relative_to(root).parts[0] not in {"attachments", "shared-files"}:
            with _file(path, metadata) as file:
                if file.read(16) == b"SQLite format 3\x00":
                    if path not in sqlite_paths:
                        raise ValueError("unknown SQLite source requires review")
                    found_feed = True
    if not found_feed:
        raise ValueError("CRM feed is not SQLite")
    sidecars = {Path(str(p) + ext) for p in sqlite_paths for ext in ("-wal", "-shm")}
    if any(Path(str(p) + "-journal") in paths for p in sqlite_paths):
        raise ValueError("SQLite rollback journal present")
    state = root / "mcp-oauth-state.json"
    st = state.lstat()
    with _file(state, st) as file:
        encrypted = file.read(2 * 1024 * 1024 + 1)
        if len(encrypted) > 2 * 1024 * 1024 or not _stable(st, os.fstat(file.fileno())):
            raise ValueError("OAuth state unavailable")
    if not isinstance(json.loads(Fernet(key).decrypt(encrypted)), dict):
        raise ValueError("OAuth state invalid")
    digest = hashlib.sha256()
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as raw, tempfile.TemporaryDirectory(prefix=".crm-sqlite-", dir=parent) as scratch:
            with tarfile.open(fileobj=raw, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
                info = _info("crm", root.stat(), True)
                archive.addfile(info)
                _record(digest, info)
                for path, metadata, is_dir in entries:
                    if path in sidecars:
                        continue
                    name = "crm/" + path.relative_to(root).as_posix()
                    if is_dir:
                        info = _info(name, metadata, True)
                        archive.addfile(info)
                        _record(digest, info)
                        continue
                    source = path
                    if path in sqlite_paths:
                        source = Path(scratch) / str(len(list(Path(scratch).iterdir())))
                        _sqlite_copy(path, source)
                    info = _info(name, metadata)
                    if path in sqlite_paths:
                        info.size = source.stat().st_size
                    with source.open("rb") if source != path else _file(path, metadata) as file:
                        reader = DigestReader(file)
                        archive.addfile(info, reader)
                        if source == path and not _stable(metadata, os.fstat(file.fileno())):
                            raise ValueError("source changed")
                    _record(digest, info, reader.hash.hexdigest())
                info = tarfile.TarInfo("oauth-state.key")
                info.mode = 0o600
                info.size = len(key)
                archive.addfile(info, io.BytesIO(key))
                _record(digest, info, hashlib.sha256(key).hexdigest())
                manifest = json.dumps(
                    {
                        "schema": 1,
                        "crm_revision": revision,
                        "crm_image": image,
                        "created_utc": datetime.now(UTC).isoformat(),
                        "tree_sha256": digest.hexdigest(),
                    },
                    separators=(",", ":"),
                ).encode()
                info = tarfile.TarInfo("manifest.json")
                info.mode = 0o600
                info.size = len(manifest)
                archive.addfile(info, io.BytesIO(manifest))
            raw.flush()
            os.fsync(raw.fileno())
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--crm-revision", required=True)
    args = parser.parse_args()
    try:
        key, image = container_context(args.container_id, args.data_dir, args.crm_revision)
        create_snapshot(args.data_dir, args.output, args.crm_revision, key, image)
    except Exception:  # noqa: BLE001 - avoid leaking secret-bearing inputs in tracebacks
        print("CRM snapshot failed; no archive was published.")
        return 1
    print("CRM snapshot prepared; coordinator must confirm writers stayed stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
