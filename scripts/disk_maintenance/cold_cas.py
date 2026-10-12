"""Pinned public-software CAS bundles; copy hydration never extracts tar links.

The caller attests provenance/readiness separately.  This module validates the
complete carrier and restores only a new private destination, without importing
archived code, changing a production alias, or deleting any source directory.
"""

from __future__ import annotations

import hashlib
from collections import Counter
import json
import os
import platform
import re
import resource
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, cast

from .util import MaintenanceError, no_symlink_ancestors, private_directory, private_file, sha256_file

OWNER_UID = 0
SCHEMA = "autostop.public-cas.v1"
HASH = re.compile(r"[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{40}")
ROOT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}")
MAX_BYTES = 8 * 1024**3
MAX_ENTRIES = 150_000


def canonical(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def current_platform() -> dict:
    name, version = platform.libc_ver()
    return {"system": platform.system(), "machine": platform.machine(), "libc": name, "libc_version": version}


def _fail(condition: bool, code: str) -> None:
    if not condition:
        raise MaintenanceError(code)


def _relative(name: Any) -> PurePosixPath:
    _fail(isinstance(name, str) and bool(name) and "\x00" not in name, "cold_path_invalid")
    path = PurePosixPath(name)
    _fail(not path.is_absolute() and ".." not in path.parts and str(path) == name, "cold_path_invalid")
    return path


def _entry(entry: dict, objects: dict, external: dict) -> None:
    _fail(isinstance(entry, dict), "cold_entry_invalid")
    _relative(entry.get("path"))
    for key in ("uid", "gid", "mode", "mtime_ns"):
        _fail(type(entry.get(key)) is int and entry[key] >= 0, "cold_metadata_invalid")
    _fail(entry["uid"] == OWNER_UID and entry["mode"] <= 0o777, "cold_metadata_invalid")
    kind = entry.get("kind")
    _fail(kind in {"directory", "file", "symlink"}, "cold_entry_kind_invalid")
    if kind != "symlink":
        _fail(not entry["mode"] & 0o022, "cold_metadata_invalid")
    if kind == "file":
        _fail(isinstance(entry.get("sha256"), str) and type(entry.get("bytes")) is int, "cold_object_invalid")
        _fail(entry.get("sha256") in objects and entry.get("bytes") == objects[entry["sha256"]], "cold_object_invalid")
        _fail(isinstance(entry.get("group"), str) and bool(entry["group"]), "cold_group_invalid")
    if kind == "symlink":
        target = entry.get("target", "")
        _fail(isinstance(target, str) and bool(target) and "\x00" not in target, "cold_link_invalid")
        if target.startswith("/"):
            _fail(target in external, "cold_link_invalid")
        else:
            pieces = list(PurePosixPath(entry["path"]).parent.parts)
            for piece in PurePosixPath(target).parts:
                if piece == "..":
                    _fail(bool(pieces), "cold_link_invalid")
                    pieces.pop()
                elif piece != ".":
                    pieces.append(piece)


def _root(root: dict, objects: dict) -> None:
    _fail(isinstance(root, dict) and isinstance(root.get("id"), str) and ROOT_ID.fullmatch(root["id"]) is not None, "cold_root_invalid")
    _fail(isinstance(root.get("revision"), str) and REVISION.fullmatch(root["revision"]) is not None, "cold_revision_invalid")
    _fail(root.get("kind") in {"tg_runtime", "own_venv"}, "cold_root_kind_invalid")
    _fail(isinstance(root.get("source_path"), str), "cold_source_invalid")
    source = Path(root["source_path"])
    _fail(source.is_absolute() and ".." not in source.parts, "cold_source_invalid")
    _fail(isinstance(root.get("provenance"), dict), "cold_provenance_invalid")
    external = root.get("external_links", {})
    _fail(isinstance(external, dict), "cold_link_invalid")
    for path, digest in external.items():
        _fail(path in {"/usr/bin/python3", "/usr/bin/python3.11", "/usr/bin/python3.12"}, "cold_link_invalid")
        _fail(isinstance(digest, str) and HASH.fullmatch(digest) is not None, "cold_link_invalid")
    entries = root.get("entries", [])
    _fail(isinstance(entries, list) and 0 < len(entries) <= MAX_ENTRIES, "cold_entries_invalid")
    names: dict[str, str] = {}
    groups: dict[str, dict] = {}
    for entry in entries:
        _entry(entry, objects, external)
        path = entry["path"]
        _fail(path not in names, "cold_duplicate_path")
        names[path] = entry["kind"]
        if entry["kind"] == "file":
            metadata = {key: entry[key] for key in ("sha256", "bytes", "uid", "gid", "mode", "mtime_ns")}
            _fail(entry["group"] not in groups or groups[entry["group"]] == metadata, "cold_group_invalid")
            groups[entry["group"]] = metadata
    _fail(names.get(".") == "directory", "cold_root_entry_missing")
    for name in names:
        if name != ".":
            _fail(names.get(str(PurePosixPath(name).parent)) == "directory", "cold_parent_invalid")


def validate_manifest(manifest: dict, *, expected_platform: dict | None = None) -> dict:
    _fail(isinstance(manifest, dict) and manifest.get("schema") == SCHEMA, "cold_schema_invalid")
    _fail(manifest.get("platform") == (expected_platform or current_platform()), "cold_platform_mismatch")
    objects = manifest.get("objects", {})
    _fail(isinstance(objects, dict) and 0 < len(objects) <= MAX_ENTRIES, "cold_objects_invalid")
    for digest, size in objects.items():
        _fail(isinstance(digest, str) and HASH.fullmatch(digest) is not None, "cold_objects_invalid")
        _fail(type(size) is int and 0 <= size <= MAX_BYTES, "cold_objects_invalid")
    _fail(sum(objects.values()) <= MAX_BYTES, "cold_objects_budget")
    roots = manifest.get("roots", [])
    _fail(isinstance(roots, list) and 0 < len(roots) <= 16, "cold_roots_invalid")
    ids: set[str] = set()
    sources: set[str] = set()
    used: set[str] = set()
    for root in roots:
        _root(root, objects)
        _fail(root["id"] not in ids and root["source_path"] not in sources, "cold_duplicate_root")
        ids.add(root["id"])
        sources.add(root["source_path"])
        used.update(entry["sha256"] for entry in root["entries"] if entry["kind"] == "file")
    _fail(used == set(objects), "cold_unused_object")
    carrier = manifest.get("carrier", {})
    _fail(isinstance(carrier, dict), "cold_carrier_invalid")
    _fail(type(carrier.get("bytes")) is int and 0 < carrier["bytes"] <= MAX_BYTES, "cold_carrier_invalid")
    _fail(isinstance(carrier.get("sha256"), str) and HASH.fullmatch(carrier["sha256"]) is not None, "cold_carrier_invalid")
    return manifest


def load_bundle(directory: Path, manifest_sha256: str, *, expected_platform: dict | None = None) -> dict:
    private_directory(directory)
    _fail(isinstance(manifest_sha256, str) and HASH.fullmatch(manifest_sha256) is not None, "cold_pin_invalid")
    path = directory / "manifest.json"
    private_file(path)
    _fail(path.stat().st_size <= 32 * 1024**2, "cold_manifest_budget")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        raw = source.read()
    _fail(hashlib.sha256(raw).hexdigest() == manifest_sha256, "cold_manifest_hash_mismatch")
    try:
        manifest = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise MaintenanceError("cold_manifest_invalid") from exc
    _fail(canonical(manifest) == raw, "cold_manifest_not_canonical")
    validate_manifest(manifest, expected_platform=expected_platform)
    carrier = directory / "objects.tar.zst"
    private_file(carrier)
    _fail(carrier.stat().st_size == manifest["carrier"]["bytes"], "cold_carrier_size_mismatch")
    _fail(sha256_file(carrier) == manifest["carrier"]["sha256"], "cold_carrier_hash_mismatch")
    return manifest


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024**2, 768 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (300, 300))


def _stream(directory: Path, manifest: dict, destinations: dict | None = None, *, floor_bytes: int = 0) -> None:
    descriptor = os.open(directory / "objects.tar.zst", os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as compressed:
        process = subprocess.Popen(
            ["/usr/bin/zstd", "-d", "--stdout", "--quiet"], stdin=compressed,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, preexec_fn=_limits,
        )
        try:
            _fail(process.stdout is not None, "cold_decompress_failed")
            seen = set()
            with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                for member in archive:
                    match = re.fullmatch(r"objects/([0-9a-f]{64})", member.name)
                    _fail(bool(match) and member.isfile() and not member.pax_headers, "cold_tar_member_invalid")
                    digest = cast(re.Match[str], match)[1]
                    _fail(digest in manifest["objects"] and digest not in seen, "cold_tar_duplicate_or_unknown")
                    _fail(member.size == manifest["objects"][digest], "cold_object_size_mismatch")
                    seen.add(digest)
                    source = archive.extractfile(member)
                    _fail(source is not None, "cold_object_missing")
                    _copy_object(source, digest, destinations or {}, floor_bytes)
                # Read through tarfile's buffered stream, including its unread tail.
                trailer = archive.fileobj.read(65536)
                _fail(len(trailer) <= 10240 and not trailer.strip(b"\x00"), "cold_tar_trailing_data")
            _fail(process.wait(timeout=30) == 0, "cold_decompress_failed")
            _fail(seen == set(manifest["objects"]), "cold_object_missing")
        except (tarfile.TarError, OSError, subprocess.SubprocessError) as exc:
            raise MaintenanceError("cold_carrier_invalid") from exc
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def _write_block(handle, block: bytes, floor_bytes: int) -> None:
    if floor_bytes:
        space = os.statvfs("/")
        _fail(space.f_bavail * space.f_frsize >= floor_bytes + len(block), "cold_scratch_floor")
    handle.write(block)


def _copy_object(source, digest: str, destinations: dict, floor_bytes: int) -> None:
    # A common object can have thousands of independent paths.  Keep only one
    # destination descriptor open, then copy bytes; never turn CAS refs into links.
    targets = destinations.get(digest, [])
    first = None
    try:
        if targets:
            fd = os.open(targets[0], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            first = os.fdopen(fd, "wb")
        hashed = hashlib.sha256()
        while block := source.read(1024 * 1024):
            hashed.update(block)
            if first is not None:
                _write_block(first, block, floor_bytes)
        _fail(hashed.hexdigest() == digest, "cold_object_hash_mismatch")
        if first is not None:
            first.flush()
            os.fsync(first.fileno())
            first.close()
            first = None
        for target in targets[1:]:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            original = os.open(targets[0], os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "wb") as output, os.fdopen(original, "rb") as duplicate:
                while block := duplicate.read(1024 * 1024):
                    _write_block(output, block, floor_bytes)
                output.flush()
                os.fsync(output.fileno())
    finally:
        if first is not None:
            first.close()


def verify_bundle(directory: Path, manifest_sha256: str, *, expected_platform: dict | None = None) -> dict:
    manifest = load_bundle(directory, manifest_sha256, expected_platform=expected_platform)
    _stream(directory, manifest)
    _fail(load_bundle(directory, manifest_sha256, expected_platform=expected_platform) == manifest, "cold_bundle_changed")
    return {"ok": True, "manifest_sha256": manifest_sha256, "object_count": len(manifest["objects"])}


def _metadata(path: Path, entry: dict) -> None:
    os.chown(path, entry["uid"], entry["gid"], follow_symlinks=False)
    if entry["kind"] != "symlink":
        os.chmod(path, entry["mode"], follow_symlinks=False)
    os.utime(path, ns=(entry["mtime_ns"], entry["mtime_ns"]), follow_symlinks=False)


def verify_tree(destination: Path, root: dict) -> dict:
    actual = set()
    for parent, directories, files in os.walk(destination, followlinks=False):
        for name in [".", *directories, *files]:
            child = Path(parent) if name == "." else Path(parent) / name
            actual.add(child.relative_to(destination).as_posix())
    _fail(actual == {entry["path"] for entry in root["entries"]}, "cold_tree_membership_mismatch")
    groups: dict[str, tuple[int, int]] = {}
    reverse: dict[tuple[int, int], str] = {}
    counts = Counter(row.get("group") for row in root["entries"] if row["kind"] == "file")
    for entry in root["entries"]:
        path = destination / entry["path"]
        info = path.lstat()
        kind = "symlink" if stat.S_ISLNK(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "directory"
        _fail(kind == entry["kind"], "cold_tree_kind_mismatch")
        _fail((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_mtime_ns) ==
              (entry["uid"], entry["gid"], entry["mode"], entry["mtime_ns"]), "cold_tree_metadata_mismatch")
        if kind == "file":
            _fail(info.st_size == entry["bytes"] and sha256_file(path) == entry["sha256"], "cold_tree_hash_mismatch")
            inode = (info.st_dev, info.st_ino)
            group = entry["group"]
            _fail(group not in groups or groups[group] == inode, "cold_tree_group_mismatch")
            _fail(inode not in reverse or reverse[inode] == group, "cold_tree_group_mismatch")
            groups[group], reverse[inode] = inode, group
            _fail(info.st_nlink == counts[group], "cold_external_hardlink")
        elif kind == "symlink":
            _fail(os.readlink(path) == entry["target"], "cold_tree_link_mismatch")
    return {"ok": True, "entry_count": len(actual), "independent_file_groups": len(groups)}


def restore_root(directory: Path, manifest_sha256: str, root_id: str, destination: Path, *,
                 expected_revision: str, expected_platform: dict | None = None, floor_bytes: int = 0) -> dict:
    manifest = load_bundle(directory, manifest_sha256, expected_platform=expected_platform)
    roots = [root for root in manifest["roots"] if root["id"] == root_id]
    _fail(len(roots) == 1 and roots[0]["revision"] == expected_revision, "cold_root_revision_mismatch")
    root = roots[0]
    no_symlink_ancestors(destination)
    private_directory(destination.parent)
    _fail(not destination.exists() and not destination.is_symlink(), "cold_destination_exists")
    for path, digest in root["external_links"].items():
        _fail(sha256_file(Path(path).resolve()) == digest, "cold_interpreter_mismatch")
    destination.mkdir(mode=0o700)
    try:
        entries = sorted(root["entries"], key=lambda row: (len(PurePosixPath(row["path"]).parts), row["path"]))
        targets: dict[str, list[Path]] = {}
        groups: dict[str, Path] = {}
        for entry in entries:
            path = destination / entry["path"]
            if entry["kind"] == "directory" and entry["path"] != ".":
                path.mkdir(mode=0o700)
            elif entry["kind"] == "file" and entry["group"] not in groups:
                groups[entry["group"]] = path
                targets.setdefault(entry["sha256"], []).append(path)
        _stream(directory, manifest, targets, floor_bytes=floor_bytes)
        for entry in entries:
            path = destination / entry["path"]
            if entry["kind"] == "symlink":
                os.symlink(entry["target"], path)
            elif entry["kind"] == "file" and path != groups[entry["group"]]:
                os.link(groups[entry["group"]], path, follow_symlinks=False)
        for entry in reversed(entries):
            _metadata(destination / entry["path"], entry)
        result = verify_tree(destination, root)
        _fail(load_bundle(directory, manifest_sha256, expected_platform=expected_platform) == manifest, "cold_bundle_changed")
        return {**result, "manifest_sha256": manifest_sha256, "root_id": root_id, "revision": expected_revision}
    except BaseException:
        shutil.rmtree(destination)
        raise
