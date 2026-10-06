#!/usr/bin/env python3
"""Prepare one new hash-pinned runtime; never activate services or install in Telegram Python."""

from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
from typing import Any
import urllib.request
import zipfile

MAX_ARCHIVE_BYTES = 40_000_000
MAX_DATABASE_BYTES = 160_000_000
RESERVE_BYTES = 1_073_741_824
SOURCE_ROOT = Path(__file__).resolve().parent.parent


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1_048_576), b""):
            digest.update(block)
    return digest.hexdigest()


def acquire(metadata: dict[str, Any], supplied: str | None, cache: Path) -> Path:
    if supplied:
        archive = Path(supplied).resolve()
    else:
        archive = cache / Path(metadata["archive_url"]).name
        request = urllib.request.Request(
            metadata["archive_url"], headers={"User-Agent": "AutoStop-offline-preparation/1"}
        )
        with urllib.request.urlopen(request, timeout=120) as response, archive.open("xb") as output:
            total = 0
            for chunk in iter(lambda: response.read(1_048_576), b""):
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise ValueError("archive_size_limit")
                output.write(chunk)
    if archive.stat().st_size != metadata["archive_bytes"] or sha256(archive) != metadata["archive_sha256"]:
        raise ValueError("archive_hash_mismatch")
    return archive


def write_owned(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    sources = json.loads((SOURCE_ROOT / "docs/agent/automotive_offline_sources.json").read_text())
    output = Path(args.output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("output_must_be_new_owned_directory")
    parent = output.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("output_parent_missing_or_symlink")
    required = RESERVE_BYTES + 2 * sources["corgi"]["archive_bytes"] + 2 * sources["corgi"]["database_bytes"]
    if shutil.disk_usage(parent).free < required:
        raise ValueError("offline_preparation_insufficient_capacity")
    node = shutil.which(args.node)
    if node is None:
        raise ValueError("node_runtime_missing")
    node = str(Path(node).resolve())
    version = subprocess.check_output([node, "--version"], text=True, timeout=10).strip()
    version_parts = tuple(int(part) for part in version.lstrip("v").split("."))
    if version_parts < (22, 13, 0):
        raise ValueError("node_runtime_requires_22_13_or_newer")
    output.mkdir(mode=0o700)
    cache = output / "archives"
    cache.mkdir(mode=0o700)
    corgi = acquire(sources["corgi"], args.corgi_archive, cache)
    wheel = acquire(sources["vininfo"], args.vininfo_wheel, cache)
    with tarfile.open(corgi) as archive:
        for source, target in [
            ("package/dist/browser.mjs", "corgi-browser.mjs"),
            ("package/LICENSE", "licenses/Corgi-ISC.txt"),
        ]:
            item = archive.getmember(source)
            if not item.isfile() or item.size > 2_000_000:
                raise ValueError("invalid_corgi_artifact")
            stream = archive.extractfile(item)
            if stream is None:
                raise ValueError("missing_corgi_artifact")
            write_owned(output / target, stream.read())
        compressed = archive.extractfile("package/dist/db/vpic.lite.db.gz")
        if compressed is None:
            raise ValueError("missing_bundled_database")
        with gzip.GzipFile(fileobj=compressed) as source, (output / "vpic.lite.db").open("xb") as target:
            size = 0
            for chunk in iter(lambda: source.read(1_048_576), b""):
                size += len(chunk)
                if size > MAX_DATABASE_BYTES:
                    raise ValueError("database_size_limit")
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        (output / "vpic.lite.db").chmod(0o600)
    if sha256(output / "vpic.lite.db") != sources["corgi"]["database_sha256"]:
        raise ValueError("database_hash_mismatch")
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.endswith("/"):
                continue
            relative = Path(name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not name.startswith(("vininfo/", "vininfo-1.11.0.dist-info/"))
            ):
                raise ValueError("invalid_vininfo_wheel_member")
            if archive.getinfo(name).file_size > 1_000_000:
                raise ValueError("vininfo_member_size_limit")
            write_owned(output / "python-packages" / relative, archive.read(name))
    with sqlite3.connect(f"file:{output / 'vpic.lite.db'}?mode=ro&immutable=1", uri=True) as database:
        if database.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("database_integrity_failed")
        counts = {
            table: database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("Wmi", "Pattern")
        }
    files = {
        str(path.relative_to(output)): sha256(path)
        for path in sorted(output.rglob("*"))
        if path.is_file() and not path.is_relative_to(cache)
    }
    manifest = {
        "schema": "autostop.automotive-offline-runtime.v1",
        "prepared_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "files": files,
        "vininfo": sources["vininfo"],
        "corgi": {**sources["corgi"], "record_counts": counts},
        "node": {"executable": node, "version": version, "sha256": sha256(Path(node))},
        "preparation_network": "explicit_pinned_archive_downloads_only",
        "decode_network": "forbidden",
    }
    write_owned(output / "manifest.json", (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode())
    return {
        "ok": True,
        "runtime": str(output),
        "manifest_sha256": sha256(output / "manifest.json"),
        "database_bytes": (output / "vpic.lite.db").stat().st_size,
        "record_counts": counts,
        "activated": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="New owned directory; this command never replaces current")
    parser.add_argument("--corgi-archive", help="Already downloaded archive with the exact lock hash")
    parser.add_argument("--vininfo-wheel", help="Already downloaded wheel with the exact lock hash")
    parser.add_argument("--node", default="node", help="Node >=22.13 with node:sqlite; exact executable is attested")
    args = parser.parse_args()
    try:
        receipt = prepare(args)
    except (
        OSError,
        ValueError,
        KeyError,
        subprocess.SubprocessError,
        tarfile.TarError,
        zipfile.BadZipFile,
        sqlite3.Error,
    ) as error:
        print(json.dumps({"ok": False, "error_type": type(error).__name__, "error": str(error)}))
        return 1
    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
