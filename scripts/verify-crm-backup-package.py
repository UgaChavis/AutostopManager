#!/usr/bin/env python3
"""Verify the CRM backup package against a local Manager Git commit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PACKAGE_FILES = (
    "crm_backup_coordinator.py",
    "crm-snapshot.py",
    "check-crm-snapshot-restore.py",
    "deliver-crm-backup.py",
    "transfer-crm-backup-windows.py",
)


def git_read(root: Path, *args: str) -> bytes:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="")
    return subprocess.run(
        ["git", "cat-file", *args],
        cwd=root,
        env=environment,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ).stdout


def package_bytes(directory: Path, name: str) -> bytes:
    path = directory / name
    try:
        if not path.is_symlink() and path.is_file():
            return path.read_bytes()
    except OSError:
        pass
    raise ValueError(f"package_file_unreadable:{name}")


def manifest_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    manifest = dict(pairs)
    if len(manifest) != len(pairs):
        raise ValueError("package_manifest_invalid")
    return manifest


def verify(root: Path, revision: str, package: Path) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("package_source_revision_invalid")
    try:
        kind = git_read(root, "-t", revision)
    except (OSError, subprocess.CalledProcessError):
        raise ValueError("package_source_revision_unavailable") from None
    if kind != b"commit\n":
        raise ValueError("package_source_revision_invalid")
    if package_bytes(package, "REVISION").strip() != revision.encode("ascii"):
        raise ValueError("package_revision_mismatch")

    manifest_bytes = package_bytes(package, "source-manifest.json")
    try:
        manifest = json.loads(manifest_bytes, object_pairs_hook=manifest_object)
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("package_manifest_invalid") from None
    if not isinstance(manifest, dict) or manifest.keys() != set(PACKAGE_FILES):
        raise ValueError("package_manifest_files_mismatch")

    for name in PACKAGE_FILES:
        actual = hashlib.sha256(package_bytes(package, name)).hexdigest()
        if manifest[name] != actual:
            raise ValueError(f"package_manifest_hash_mismatch:{name}")
        try:
            source = git_read(root, "blob", f"{revision}:scripts/{name}")
        except (OSError, subprocess.CalledProcessError):
            raise ValueError(f"package_source_file_unavailable:{name}") from None
        if hashlib.sha256(source).hexdigest() != actual:
            raise ValueError(f"package_source_hash_mismatch:{name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True, help="Full Manager commit SHA available in this checkout")
    parser.add_argument("--package-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        verify(Path(__file__).resolve().parents[1], args.revision, args.package_dir)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "source_revision": args.revision, "files": len(PACKAGE_FILES)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
