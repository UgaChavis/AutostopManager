#!/usr/bin/env python3
"""One cold Manager companion archive; callers must stop its writers first."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import tarfile
import tempfile

ROOTS = tuple(
    Path(path)
    for path in (
        "/var/lib/autostop-manager",
        "/var/lib/autostop-manager-scheduler",
        "/var/lib/autostop-work-telegram",
        "/var/lib/autostop-telegram",
        "/opt/AutostopManager/data",
    )
)
EXCLUDED = {
    ROOTS[0] / "crm-first-transition",
    ROOTS[0] / "crm-backup",
    Path("/opt/AutostopManager/data/offline_parts_catalogs"),
}
MANIFEST = "checkpoint-manifest.json"
REQUIRED_SQLITE_VERSION = "3.51.3"


def signature(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def inventory(roots, config_root=None, excluded=EXCLUDED):
    entries = []
    sockets = 0

    def visit(path, archive_name):
        nonlocal sockets
        if path in excluded:
            return
        info = path.lstat()
        if stat.S_ISSOCK(info.st_mode):
            sockets += 1
            return
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            raise ValueError("unsupported cold checkpoint entry")
        target = os.readlink(path) if stat.S_ISLNK(info.st_mode) else None
        entries.append((path, archive_name, signature(info), target))
        if stat.S_ISDIR(info.st_mode):
            for child in sorted(path.iterdir()):
                visit(child, archive_name + "/" + child.name)

    for root in roots:
        if not root.is_dir() or root.is_symlink():
            raise ValueError("cold checkpoint source unavailable")
        visit(root, root.as_posix().lstrip("/"))
    if config_root is not None:
        if not config_root.is_dir() or config_root.is_symlink():
            raise ValueError("private configuration pack unavailable")
        visit(config_root, "configuration")
    return entries, sockets


def fingerprint(entries):
    return [(name, metadata, target) for _, name, metadata, target in entries]


def private_parent(output):
    parent = output.parent.resolve(strict=True)
    info = parent.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077 or output.is_symlink():
        raise ValueError("checkpoint destination is not private")
    return parent / output.name


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def create_archive(roots, output, config_root=None, excluded=EXCLUDED):
    output = private_parent(output)
    entries, sockets = inventory(roots, config_root, excluded)
    rows = []
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            with tarfile.open(fileobj=stream, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
                for path, name, expected, link_target in entries:
                    current = path.lstat()
                    if signature(current) != expected:
                        raise ValueError("cold source changed")
                    info = tarfile.TarInfo(name)
                    info.mode = stat.S_IMODE(current.st_mode)
                    info.uid, info.gid = current.st_uid, current.st_gid
                    info.mtime = int(current.st_mtime)
                    row = {
                        "name": name,
                        "mode": info.mode,
                        "uid": info.uid,
                        "gid": info.gid,
                        "mtime": info.mtime,
                    }
                    if stat.S_ISDIR(current.st_mode):
                        info.type = tarfile.DIRTYPE
                        row["kind"] = "directory"
                        archive.addfile(info)
                    elif stat.S_ISLNK(current.st_mode):
                        if os.readlink(path) != link_target:
                            raise ValueError("cold symlink metadata changed")
                        info.type, info.linkname = tarfile.SYMTYPE, link_target
                        row.update(kind="symlink_metadata", target=link_target)
                        archive.addfile(info)
                    else:
                        info.size = current.st_size
                        handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                        try:
                            if signature(os.fstat(handle)) != expected:
                                raise ValueError("cold source descriptor changed")
                            with os.fdopen(handle, "rb", closefd=False) as source:
                                # Compute the hash on this descriptor, then rewind
                                # and archive the same immutable cold source.
                                hasher = hashlib.sha256()
                                for block in iter(lambda: source.read(1024 * 1024), b""):
                                    hasher.update(block)
                                source.seek(0)
                                archive.addfile(info, source)
                            if signature(os.fstat(handle)) != expected:
                                raise ValueError("cold source changed during read")
                        finally:
                            os.close(handle)
                        row.update(kind="file", size=info.size, sha256=hasher.hexdigest())
                    rows.append(row)
                if fingerprint(inventory(roots, config_root, excluded)[0]) != fingerprint(entries):
                    raise ValueError("cold source tree changed")
                manifest = {
                    "schema": 1,
                    "scope": "manager_cold_companion",
                    "symlinks": "metadata_only_never_followed",
                    "skipped_socket_count": sockets,
                    "entries": rows,
                }
                payload = json.dumps(manifest, separators=(",", ":"), ensure_ascii=True).encode()
                info = tarfile.TarInfo(MANIFEST)
                info.mode, info.size = 0o600, len(payload)
                archive.addfile(info, io.BytesIO(payload))
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return manifest


def restore_regular(archive_path, destination):
    manifest = None
    observed = []
    with tarfile.open(archive_path, "r:gz") as archive:
        seen = set()
        for entry in archive:
            parts = PurePosixPath(entry.name).parts
            if (
                not parts
                or PurePosixPath(entry.name).is_absolute()
                or ".." in parts
                or entry.name in seen
                or manifest is not None
            ):
                raise ValueError("invalid companion archive member")
            seen.add(entry.name)
            if entry.name == MANIFEST:
                if not entry.isfile() or entry.size > 64 * 1024 * 1024:
                    raise ValueError("invalid companion manifest")
                manifest = json.load(archive.extractfile(entry))
                continue
            row = {
                "name": entry.name,
                "mode": entry.mode,
                "uid": entry.uid,
                "gid": entry.gid,
                "mtime": entry.mtime,
            }
            if entry.issym():
                row.update(kind="symlink_metadata", target=entry.linkname)
                observed.append(row)
                continue  # Never create or follow even a benign symlink.
            out = destination.joinpath(*parts)
            out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if entry.isdir():
                out.mkdir(mode=0o700, exist_ok=True)
                row["kind"] = "directory"
            elif entry.isfile():
                fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                hasher = hashlib.sha256()
                with (
                    os.fdopen(fd, "wb") as target,
                    archive.extractfile(entry) as source,
                ):
                    remaining = entry.size
                    while remaining:
                        block = source.read(min(1024 * 1024, remaining))
                        if not block:
                            raise ValueError("truncated companion member")
                        target.write(block)
                        hasher.update(block)
                        remaining -= len(block)
                row.update(kind="file", size=entry.size, sha256=hasher.hexdigest())
            else:
                raise ValueError("unsupported companion member")
            observed.append(row)
    if not isinstance(manifest, dict) or manifest.get("schema") != 1 or manifest.get("entries") != observed:
        raise ValueError("companion content integrity failed")
    return manifest


def verify_databases(root):
    if sqlite3.sqlite_version != REQUIRED_SQLITE_VERSION:
        raise ValueError("qualified SQLite runtime required")
    count = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        with path.open("rb") as source:
            header = source.read(16)
        if header != b"SQLite format 3\0":
            continue
        # Only the disposable extraction is opened as SQLite. mode=rw permits
        # WAL-index recovery; immutable=1 would incorrectly ignore copied WAL.
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
        try:
            connection.execute("PRAGMA query_only=ON")
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("restored companion database corrupt")
        finally:
            connection.close()
        count += 1
    return count


def verify_archive(archive_path):
    before = digest(archive_path)
    with tempfile.TemporaryDirectory(prefix=".manager-check-", dir=archive_path.parent) as temporary:
        root = Path(temporary)
        manifest = restore_regular(archive_path, root)
        databases = verify_databases(root)
    if digest(archive_path) != before:
        raise ValueError("companion archive changed during verification")
    return {
        "ok": True,
        "archive_sha256": before,
        "sqlite_databases_verified": databases,
        "sqlite_runtime": sqlite3.sqlite_version,
        "regular_files_verified": sum(row["kind"] == "file" for row in manifest["entries"]),
        "directories_verified": sum(row["kind"] == "directory" for row in manifest["entries"]),
        "symlinks_metadata_only": sum(row["kind"] == "symlink_metadata" for row in manifest["entries"]),
        "skipped_socket_count": manifest["skipped_socket_count"],
        "isolated_extraction_removed": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config-root", type=Path)
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise ValueError("root required")
        if sqlite3.sqlite_version != REQUIRED_SQLITE_VERSION:
            raise ValueError("qualified SQLite runtime required")
        create_archive(ROOTS, args.output, args.config_root)
        result = verify_archive(args.output)
        receipt = args.output.with_name(args.output.name + ".receipt.json")
        fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(result, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        directory = os.open(receipt.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:  # noqa: BLE001 - never expose private checkpoint payloads in logs.
        print("Manager cold companion incomplete; existing copies preserved.")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
