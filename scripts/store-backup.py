#!/usr/bin/env python3
"""Keep one verified Store data/settings backup; never restore a live database."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
from datetime import UTC, datetime

SCHEMA = "autostop.store-backup.v1"
GENERATION = re.compile(r"\d{8}T\d{6}Z-\d+\Z")
SOURCES = {
    "uploads": Path("/var/lib/docker/volumes/autostop-app_uploads_data/_data"),
    "photos": Path("/var/lib/docker/volumes/autostop-app_quote_request_vin_photos_data/_data"),
    "settings/store.env": Path("/opt/autostop-app/.env"),
    "settings/docker-compose.yml": Path("/opt/autostop-app/docker-compose.yml"),
    "settings/deploy-current.env": Path("/opt/autostop-app-deploys/current.env"),
}


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def inventory(sources):
    records, paths = {}, {}
    for prefix, source in sources.items():
        pending = [(prefix, source)]
        while pending:
            name, path = pending.pop()
            info = path.lstat()
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise RuntimeError("unsupported_backup_source_type")
            record = {
                "directory": stat.S_ISDIR(info.st_mode),
                "mode": stat.S_IMODE(info.st_mode),
                "uid": info.st_uid,
                "gid": info.st_gid,
                "mtime_ns": info.st_mtime_ns,
            }
            if record["directory"]:
                pending.extend((name + "/" + child.name, child) for child in path.iterdir())
            else:
                record.update(bytes=info.st_size, sha256=digest(path))
            records[name], paths[name] = record, path
    return records, paths


def archive_files(destination, paths):
    with destination.open("xb") as output:
        with tarfile.open(fileobj=output, mode="w", dereference=False) as archive:
            for name, path in sorted(paths.items()):
                item = archive.gettarinfo(str(path), arcname=name)
                if item.isfile() or item.islnk():
                    item.type, item.linkname = tarfile.REGTYPE, ""
                    item.size = path.stat().st_size
                    with path.open("rb") as source:
                        archive.addfile(item, source)
                else:
                    archive.addfile(item)
        output.flush()
        os.fsync(output.fileno())


def verify_archive(path, expected):
    seen = set()
    with tarfile.open(path, "r|") as archive:
        for item in archive:
            if item.name in seen or item.name not in expected:
                raise RuntimeError("archive_member_mismatch")
            record = expected[item.name]
            if (item.mode, item.uid, item.gid) != (record["mode"], record["uid"], record["gid"]):
                raise RuntimeError("archive_permissions_mismatch")
            if record["directory"]:
                if not item.isdir():
                    raise RuntimeError("archive_type_mismatch")
            else:
                if not item.isfile() or item.size != record["bytes"]:
                    raise RuntimeError("archive_type_or_size_mismatch")
                with archive.extractfile(item) as source:
                    if hashlib.file_digest(source, "sha256").hexdigest() != record["sha256"]:
                        raise RuntimeError("archive_hash_mismatch")
            seen.add(item.name)
    if seen != set(expected):
        raise RuntimeError("archive_missing_member")


def quiet_run(arguments, *, input_file=None, output_file=None):
    with input_file.open("rb") if input_file else open(os.devnull, "rb") as source:
        result = subprocess.run(
            arguments, stdin=source, stdout=output_file or subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600
        )
    if result.returncode:
        raise RuntimeError("backup_command_failed")


def container_identity():
    result = subprocess.run(
        ["docker", "inspect", "autostop-db", "autostop-app"], capture_output=True, timeout=30, check=True
    )
    db, app = json.loads(result.stdout)
    if not db["State"]["Running"] or db["State"].get("Health", {}).get("Status") != "healthy":
        raise RuntimeError("postgres_not_healthy")
    return {
        "db": {"id": db["Id"], "image": db["Image"], "pid": db["State"]["Pid"]},
        "app": {"id": app["Id"], "image": app["Image"], "pid": app["State"]["Pid"]},
    }


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def retire_previous(root, current):
    removed = []
    for old in root.iterdir():
        if old == current or not GENERATION.fullmatch(old.name):
            continue
        if old.is_symlink() or not old.is_dir() or old.stat().st_uid != os.geteuid():
            raise RuntimeError("previous_backup_ownership_invalid")
        metadata = old / "manifest.json"
        if metadata.is_symlink() or not metadata.is_file():
            raise RuntimeError("previous_backup_manifest_missing")
        record = json.loads(metadata.read_text())
        if record.get("schema") != SCHEMA or record.get("complete") is not True:
            raise RuntimeError("previous_backup_manifest_invalid")
        shutil.rmtree(old)
        removed.append(old.name)
    sync_directory(root)
    return removed


def create_backup(root, lock_path):
    os.umask(0o077)
    if root.is_symlink():
        raise RuntimeError("private_root_backup_required")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.stat().st_uid != os.geteuid() or root.stat().st_mode & 0o077:
        raise RuntimeError("backup_root_permissions_invalid")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    stage = None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = container_identity()
        records, paths = inventory(SOURCES)
        required = sum(x.get("bytes", 0) for x in records.values()) + 2 * 1024**3
        if shutil.disk_usage(root).free < required + 2 * 1024**3:
            raise RuntimeError("backup_disk_reserve_insufficient")
        name = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + str(os.getpid())
        stage, final = root / (".partial-" + name), root / name
        stage.mkdir(mode=0o700)
        dump = stage / "database.dump"
        with dump.open("xb") as output:
            quiet_run(
                [
                    "docker",
                    "exec",
                    "autostop-db",
                    "sh",
                    "-eu",
                    "-c",
                    'exec pg_dump --format=custom --compress=6 -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
                ],
                output_file=output,
            )
            output.flush()
            os.fsync(output.fileno())
        with dump.open("rb") as source:
            if source.read(5) != b"PGDMP":
                raise RuntimeError("postgres_archive_header_invalid")
        files = stage / "files.tar"
        archive_files(files, paths)
        verify_archive(files, records)
        for flags in (["--list"], ["--file=/dev/null", "--no-owner", "--no-privileges"]):
            quiet_run(["docker", "exec", "-i", "autostop-db", "pg_restore", *flags], input_file=dump)
        if inventory(SOURCES)[0] != records or container_identity() != identity:
            raise RuntimeError("source_changed_during_backup")
        manifest = {
            "schema": SCHEMA,
            "complete": True,
            "created_at": datetime.now(UTC).isoformat(),
            "container_identity": identity,
            "source_files": records,
            "artifacts": {p.name: {"bytes": p.stat().st_size, "sha256": digest(p)} for p in (dump, files)},
            "validation": {
                "pg_archive_fully_streamed": True,
                "files_hashes_and_permissions": True,
                "source_stable_across_dump": True,
                "live_restore_performed": False,
            },
        }
        with (stage / "manifest.json").open("x") as output:
            json.dump(manifest, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        sync_directory(stage)
        stage.rename(final)
        sync_directory(root)
        removed = retire_previous(root, final)
        return {
            "ok": True,
            "backup": str(final),
            "manifest_sha256": digest(final / "manifest.json"),
            "retired_generations": removed,
        }
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage)
        os.close(lock_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/var/backups/autostop-current/store"))
    parser.add_argument("--lock", type=Path, default=Path("/run/lock/autostop24-db-backup.lock"))
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise RuntimeError("root_required")
        print(json.dumps(create_backup(args.root, args.lock)))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, tarfile.TarError) as error:
        # No subprocess stderr, archive contents or credentials in the journal.
        print(json.dumps({"ok": False, "error_type": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
