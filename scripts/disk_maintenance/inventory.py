"""Read-only, fail-closed inventory of owned technical recovery artifacts.

No application source, shell environment, business JSON, or archive payload is
executed or interpreted.  Candidates are proposals; the caller holds the native
locks, verifies the cold keepers, and revalidates each identity before deletion.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sqlite3
import stat
import tarfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from .util import (
    MaintenanceError,
    checked_run,
    file_identity,
    process_references,
    run,
    sha256_file,
    unique_allocated,
    unit_references,
)

OWNER_UID = 0

PG_NAME = re.compile(r"autostop24-([0-9]{8}T[0-9]{6})Z\.dump")
FULL_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}")
CRM_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}-[0-9]+")
STORE_NAME = re.compile(r"(?:pre-rollback-)?([0-9]{8}-[0-9]{6})-([0-9a-f]{40})")
MANAGER_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}-[0-9]+-manager-([0-9a-f]{12})")
TELEGRAM_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z-([0-9a-f]{12})")
SHA = re.compile(r"[0-9a-f]{40}")
HASH = re.compile(r"[0-9a-f]{64}")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
CRM_TAG = re.compile(
    r"autostopcrm:(?:crm-only-)?[0-9a-f]{12}|autostopcrm-rollback:[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}-[0-9]+"
)
STORE_TAG = re.compile(r"autostop-app:rollback-[0-9]{8}-[0-9]{6}")
ENV_KEYS = frozenset(
    {
        "git_sha",
        "image_id",
        "backup_dir",
        "incoming_git_sha",
        "previous_image_id",
        "rollback_image_tag",
        "previous_db_image_id",
        "rollback_db_image_tag",
    }
)
CRM_ARTIFACTS = {
    "state": "state.json",
    "change_feed_sqlite": "change_feed.sqlite3",
    "audit_archive": "audit-archive.tar.gz",
    "manager_sqlite": "autostop_manager.sqlite3",
    "completion_act_forms": "completion_act_forms.json",
    "manager_structure": "manager_structure.json",
    "manager_role_workspace": "manager-m2-workspace.tar.gz",
}
FULL_REQUIRED = frozenset(
    {
        "store.dump",
        "uploads.tar.gz",
        "photos.tar.gz",
        "crm-files.tar.gz",
        "crm-agent.tar.gz",
        "crm-sqlite/change_feed.sqlite3",
        "manager.sqlite3",
        "scheduler.sqlite3",
        "roles.tar.gz",
    }
)
STORE_REQUIRED = frozenset(
    {
        "metadata.env",
        "incoming-source.sha256",
        "database.dump",
        "source.tar.gz",
        "uploads.tar.gz",
        "quote-request-vin-photos.tar.gz",
    }
)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _identity(path: Path) -> dict:
    return {**file_identity(path), "ctime_ns": path.lstat().st_ctime_ns}


def _inventory(policy: dict) -> dict:
    value = policy.get("inventory")
    if not isinstance(value, dict) or not isinstance(value.get("roots"), dict):
        raise MaintenanceError("inventory_policy_invalid")
    return value


def _root(policy: dict, key: str) -> Path | None:
    configured = _inventory(policy)["roots"].get(key)
    if not configured:
        return None
    path = Path(configured)
    if not path.is_absolute() or len(path.parts) < 3 or path.resolve() != path or path.is_symlink():
        raise MaintenanceError("inventory_root_invalid")
    if not path.is_dir():
        raise MaintenanceError("inventory_root_unavailable")
    return path


def _flatten(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for child in value for item in _flatten(child)]
    return []


def _protected_paths(policy: dict) -> set[Path]:
    inv = _inventory(policy)
    raw = _flatten(inv.get("pinned_paths", [])) + _flatten(inv.get("coherent_keep_paths", []))
    result = set()
    for value in raw:
        path = Path(value)
        if not path.is_absolute():
            raise MaintenanceError("protected_path_invalid")
        result.add(path)
        result.add(path.resolve())
    for key in ("manager_releases", "work_tg_releases", "personal_tg_releases", "tg_runtimes"):
        root = _root(policy, key)
        if root and (root / "current").is_symlink():
            result.add((root / "current").resolve())
    # Personal Telegram is never a candidate class.
    root = _root(policy, "personal_tg_releases")
    if root:
        result.add(root)
    return result


def _overlaps(path: Path, protected: set[Path]) -> bool:
    return any(path == item or path.is_relative_to(item) or item.is_relative_to(path) for item in protected)


def _mounted_paths() -> set[Path]:
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError as exc:
        raise MaintenanceError("mount_inventory_unavailable") from exc
    return {Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), line.split()[4])) for line in lines}


def _tree_identity(path: Path, *, allow_links: bool = False, allow_hardlinks: bool = False) -> dict:
    """Fingerprint lstat metadata without following any directory symlink."""
    base = _identity(path)
    device = path.lstat().st_dev
    mounts = _mounted_paths()
    records = []
    for parent, directories, files in os.walk(path, followlinks=False):
        for name in [".", *sorted(directories), *sorted(files)]:
            child = Path(parent) if name == "." else Path(parent) / name
            info = child.lstat()
            if child in mounts or info.st_dev != device:
                raise MaintenanceError("candidate_mount_or_device_crossing")
            if stat.S_ISLNK(info.st_mode):
                if not allow_links:
                    raise MaintenanceError("candidate_symlink")
                link = hashlib.sha256(os.readlink(child).encode()).hexdigest()
            elif stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode):
                link = ""
            else:
                raise MaintenanceError("candidate_special_file")
            if (
                stat.S_ISREG(info.st_mode)
                and info.st_nlink != 1
                and not (allow_hardlinks and child.relative_to(path).as_posix() == "store.dump")
            ):
                raise MaintenanceError("candidate_hardlink")
            if not stat.S_ISLNK(info.st_mode) and (info.st_uid != OWNER_UID or info.st_mode & 0o022):
                raise MaintenanceError("candidate_tree_owner_or_mode_invalid")
            records.append(
                [
                    child.relative_to(path).as_posix(),
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                    info.st_nlink,
                    link,
                ]
            )
    return {**base, "tree_fingerprint": _hash(sorted(records))}


def _guard(path: Path, root: Path, *, allow_links: bool = False, allow_hardlinks: bool = False) -> dict:
    if path.parent != root or path.is_symlink() or path.resolve() != path:
        raise MaintenanceError("candidate_path_invalid")
    info = path.lstat()
    if info.st_dev != root.stat().st_dev or path in _mounted_paths():
        raise MaintenanceError("candidate_mount_or_device_crossing")
    if info.st_uid != OWNER_UID or info.st_mode & 0o022:
        raise MaintenanceError("candidate_owner_or_mode_invalid")
    if path.is_dir():
        return _tree_identity(path, allow_links=allow_links, allow_hardlinks=allow_hardlinks)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise MaintenanceError("candidate_file_invalid")
    return _identity(path)


def _regular(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise MaintenanceError("metadata_file_unavailable") from exc
    if (
        path.is_symlink()
        or path.resolve() != path
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != OWNER_UID
        or info.st_mode & 0o022
    ):
        raise MaintenanceError("metadata_file_invalid")


def _json(path: Path) -> dict:
    _regular(path)
    if path.stat().st_size > 8 * 1024 * 1024:
        raise MaintenanceError("metadata_too_large")
    try:
        value = json.loads(path.read_text())
    except (ValueError, UnicodeError) as exc:
        raise MaintenanceError("metadata_json_invalid") from exc
    if not isinstance(value, dict):
        raise MaintenanceError("metadata_json_invalid")
    return value


def _env(path: Path) -> dict[str, str]:
    _regular(path)
    if path.stat().st_size > 65536:
        raise MaintenanceError("metadata_too_large")
    result = {}
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if key in ENV_KEYS and separator:
            if key in result or any(char in value for char in ("\x00", "\n", "\r")):
                raise MaintenanceError("metadata_env_invalid")
            result[key] = value
    return result


def _member(directory: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if not name or relative.is_absolute() or ".." in relative.parts or str(relative) != name:
        raise MaintenanceError("artifact_path_invalid")
    path = directory / name
    _regular(path)
    if not path.resolve().is_relative_to(directory):
        raise MaintenanceError("artifact_path_invalid")
    return path


def _manifest_artifacts(directory: Path, *, full: bool) -> dict[str, dict]:
    manifest = _json(directory / "manifest.json")
    if full:
        if manifest.get("format") != "autostop_complete_backup_v1" or manifest.get("id") != directory.name:
            raise MaintenanceError("full_manifest_invalid")
    elif (
        manifest.get("schema") not in {"autostop-agent-release-backup.v4", "autostop-agent-release-backup.v5"}
        or manifest.get("backup_id") != directory.name
    ):
        raise MaintenanceError("crm_manifest_invalid")
    if manifest.get("complete") is not True or not isinstance(manifest.get("artifacts"), dict):
        raise MaintenanceError("backup_incomplete")
    try:
        created = datetime.fromisoformat(manifest["created_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MaintenanceError("backup_timestamp_invalid") from exc
    if created.tzinfo is None or created.utcoffset() != UTC.utcoffset(created):
        raise MaintenanceError("backup_timestamp_invalid")
    result = {}
    for key, entry in manifest["artifacts"].items():
        if entry is None and not full:
            continue
        if not isinstance(entry, dict):
            raise MaintenanceError("artifact_metadata_invalid")
        name = key if full else entry.get("name")
        if not isinstance(name, str) or (not full and CRM_ARTIFACTS.get(key) != name):
            raise MaintenanceError("artifact_metadata_invalid")
        size = entry.get("bytes" if full else "size_bytes")
        digest = entry.get("sha256")
        path = _member(directory, name)
        if (
            type(size) is not int
            or size < 0
            or not isinstance(digest, str)
            or not HASH.fullmatch(digest)
            or path.stat().st_size != size
        ):
            raise MaintenanceError("artifact_metadata_invalid")
        result[name] = {"bytes": size, "sha256": digest}
    required = FULL_REQUIRED if full else {"state.json", "autostop_manager.sqlite3"}
    if not required.issubset(result):
        raise MaintenanceError("artifact_coverage_missing")
    return result


def _store_artifacts(directory: Path) -> dict[str, dict]:
    metadata = _env(directory / "metadata.env")
    match = STORE_NAME.fullmatch(directory.name)
    if not match or metadata.get("incoming_git_sha") != match[2]:
        raise MaintenanceError("store_metadata_invalid")
    for key in ("previous_image_id", "previous_db_image_id"):
        if not IMAGE.fullmatch(metadata.get(key, "")):
            raise MaintenanceError("store_image_identity_invalid")
    if (
        metadata.get("rollback_image_tag") != "autostop-app:rollback-" + match[1]
        or metadata.get("rollback_db_image_tag") != "autostop-db:rollback-" + match[1]
    ):
        raise MaintenanceError("store_tag_binding_invalid")
    checksum = directory / "SHA256SUMS"
    _regular(checksum)
    result = {}
    for line in checksum.read_text().splitlines():
        matched = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)
        if not matched or matched[2] in result:
            raise MaintenanceError("checksum_manifest_invalid")
        path = _member(directory, matched[2])
        result[matched[2]] = {"bytes": path.stat().st_size, "sha256": matched[1]}
    if not STORE_REQUIRED.issubset(result):
        raise MaintenanceError("artifact_coverage_missing")
    return result


def _runtime(policy: dict) -> dict:
    inv = _inventory(policy)
    ids = checked_run(["docker", "ps", "-aq"], timeout=20).split()
    containers = []
    template = '{"id":{{json .Id}},"image_id":{{json .Image}},"name":{{json .Name}},"running":{{json .State.Running}},"started_at":{{json .State.StartedAt}}}'
    if ids:
        for line in checked_run(["docker", "inspect", "--format", template, *ids], timeout=20).splitlines():
            data = json.loads(line)
            if policy.get("operation") != "pg-retain" or data["name"].lstrip("/") == inv.get(
                "pg_container", "autostop-db"
            ):
                containers.append({key: data[key] for key in ("id", "image_id", "name", "running", "started_at")})
    units = []
    for unit in [] if policy.get("operation") == "pg-retain" else inv.get("units", []):
        output = checked_run(
            [
                "systemctl",
                "show",
                unit,
                "--property=Id,MainPID,NRestarts,ExecMainStartTimestamp,WorkingDirectory,ActiveState,SubState",
            ],
            timeout=10,
        )
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        values["actual_cwd"] = ""
        if values.get("MainPID", "0") != "0":
            try:
                values["actual_cwd"] = os.readlink("/proc/" + values["MainPID"] + "/cwd")
            except OSError as exc:
                raise MaintenanceError("runtime_process_unavailable") from exc
        units.append(values)
    configs = (
        {}
        if policy.get("operation") == "pg-retain"
        else {str(path): sha256_file(Path(path)) for path in inv.get("config_paths", [])}
    )
    git = {}
    for path in [] if policy.get("operation") == "pg-retain" else inv.get("git_roots", []):
        git[path] = {
            "head": _git_read(path, "rev-parse", "HEAD").strip(),
            "status": _git_read(path, "status", "--porcelain").splitlines(),
            "stashes": _git_read(path, "stash", "list", "--format=%gd").splitlines(),
        }
    return {"containers": sorted(containers, key=lambda row: row["id"]), "units": units, "configs": configs, "git": git}


def _git_read(path: str, *arguments: str) -> str:
    directory = Path(path)
    if not directory.is_absolute() or ".." in directory.parts:
        raise MaintenanceError("git_inventory_path_invalid")
    return checked_run(
        [
            "git",
            "--no-optional-locks",
            "-c",
            "safe.directory=" + path,
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            path,
            *arguments,
        ],
        timeout=10,
    )


def snapshot(policy: dict) -> dict:
    result = _runtime(policy)
    keys = ("pg_dumps", "full_bundles") if policy.get("operation") == "pg-retain" else _inventory(policy)["roots"]
    result["root_devices"] = {key: _root(policy, key).stat().st_dev for key in keys if _root(policy, key)}
    result["protected_bindings"] = (
        [] if policy.get("operation") == "pg-retain" else sorted(map(str, _protected_paths(policy)))
    )
    marker = _inventory(policy).get("store_deploy_marker")
    if marker and policy.get("operation") != "pg-retain":
        result["store_marker"] = _env(Path(marker))
    return result


def _allocated(path: Path, *, allow_links: bool = False, allow_hardlinks: bool = False) -> int:
    if not allow_links and not allow_hardlinks:
        return unique_allocated(path)
    seen, total = set(), 0
    for parent, directories, files in os.walk(path, followlinks=False):
        for child in [Path(parent), *(Path(parent) / name for name in directories + files)]:
            info = child.lstat()
            identity = info.st_dev, info.st_ino
            if identity not in seen and not (stat.S_ISREG(info.st_mode) and info.st_nlink > 1):
                seen.add(identity)
                total += info.st_blocks * 512
    return total


def _candidate(
    path: Path,
    root: Path,
    kind: str,
    component: str,
    reason: str,
    proof: dict,
    *,
    allow_links: bool = False,
    allow_hardlinks: bool = False,
) -> dict:
    proof = {**proof, "requires_ci": component == "store" or kind == "cache_directory"}
    return {
        "kind": kind,
        "path": str(path),
        "component": component,
        "identity": _guard(path, root, allow_links=allow_links, allow_hardlinks=allow_hardlinks),
        "estimated_reclaim_bytes": _allocated(path, allow_links=allow_links, allow_hardlinks=allow_hardlinks),
        "reason": reason,
        "proof": proof,
    }


def _recoveries(policy: dict, candidates: list, protected: list, skipped: list) -> tuple[dict, dict]:
    keepers = {}
    store_meta = {}
    for key, pattern, full, component in [
        ("full_bundles", FULL_NAME, True, "full"),
        ("crm_recovery", CRM_NAME, False, "crm"),
        ("store_recovery", STORE_NAME, False, "store"),
    ]:
        if policy.get("operation") == "pg-retain" and component != "full":
            continue
        root = _root(policy, key)
        if root is None:
            continue
        valid = []
        for child in sorted(root.iterdir()):
            if not pattern.fullmatch(child.name):
                skipped.append({"path": str(child), "reason": "unrecognized_recovery_name"})
                continue
            try:
                _guard(child, root, allow_hardlinks=full)
                if component == "store":
                    _store_artifacts(child)
                else:
                    _manifest_artifacts(child, full=full)
                valid.append(child)
            except (MaintenanceError, OSError, ValueError):
                skipped.append({"path": str(child), "reason": "invalid_recovery_metadata"})
        if not valid:
            raise MaintenanceError("preserved_recovery_missing")
        if component == "store":
            marker = _env(Path(_inventory(policy)["store_deploy_marker"]))
            keeper = Path(marker.get("backup_dir", ""))
            if keeper not in valid or _env(keeper / "metadata.env")["incoming_git_sha"] != marker.get("git_sha"):
                raise MaintenanceError("store_current_recovery_binding_invalid")
            store_meta = _env(keeper / "metadata.env")
        else:
            keeper = max(valid, key=lambda item: item.name)
        keepers[component] = str(keeper)
        protected.append({"path": str(keeper), "component": component, "reason": "latest_recovery_set"})
        for path in valid:
            if path == keeper:
                continue
            if full:
                skipped.append({"path": str(path), "reason": "full_retention_not_enabled"})
                continue
            if policy.get("operation") != "pg-retain":
                candidates.append(
                    _candidate(
                        path,
                        root,
                        "recovery_directory",
                        component,
                        "older_validated_recovery",
                        {
                            "metadata_valid": True,
                            "keeper": str(keeper),
                            "root_key": key,
                            "allow_hardlinks": full,
                            "metadata_sha256": sha256_file(
                                path / ("SHA256SUMS" if component == "store" else "manifest.json")
                            ),
                        },
                        allow_hardlinks=full,
                    )
                )
    return keepers, store_meta


def _pg_inventory(policy: dict, full_keeper: str | None, candidates: list, protected: list, skipped: list) -> None:
    root = _root(policy, "pg_dumps")
    if root is None:
        return
    timezone = ZoneInfo(policy.get("timezone", "Asia/Krasnoyarsk"))
    days = defaultdict(list)
    pin = None
    if full_keeper:
        data = _json(Path(full_keeper) / "manifest.json")
        name = data.get("components", {}).get("store", {}).get("canonical_dump")
        if not isinstance(name, str) or not PG_NAME.fullmatch(name):
            raise MaintenanceError("full_pg_reference_invalid")
        pin = root / name
        if not pin.exists() or not os.path.samefile(pin, Path(full_keeper) / "store.dump"):
            raise MaintenanceError("full_pg_reference_unbound")
    for path in sorted(root.iterdir()):
        match = PG_NAME.fullmatch(path.name)
        if not match or path.is_symlink() or not path.is_file():
            skipped.append({"path": str(path), "reason": "unrecognized_pg_dump"})
            continue
        try:
            stamp = datetime.strptime(match[1], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
            _regular(path)
            with path.open("rb") as handle:
                if handle.read(5) != b"PGDMP":
                    raise MaintenanceError("pg_header_invalid")
        except (OSError, ValueError, MaintenanceError):
            skipped.append({"path": str(path), "reason": "invalid_pg_metadata"})
            continue
        days[stamp.astimezone(timezone).date()].append((stamp, path))
    keep = {max(days[day])[1] for day in sorted(days, reverse=True)[: policy.get("pg_keep_days", 3)]}
    if pin:
        keep.add(pin)
    if len(days) < policy.get("pg_keep_days", 3):
        raise MaintenanceError("preserved_pg_days_missing")
    for entries in days.values():
        for _, path in entries:
            if path in keep:
                protected.append(
                    {
                        "path": str(path),
                        "component": "pg",
                        "reason": "full_pg_pin" if path == pin else "latest_distinct_local_day",
                    }
                )
            else:
                try:
                    candidates.append(
                        _candidate(
                            path,
                            root,
                            "pg_dump",
                            "pg",
                            "older_pg_restore_point",
                            {"metadata_valid": True, "root_key": "pg_dumps"},
                        )
                    )
                except MaintenanceError:
                    skipped.append({"path": str(path), "reason": "unsafe_pg_candidate"})


def _versioned(policy: dict, candidates: list, protected: list, skipped: list) -> None:
    inv = _inventory(policy)
    keep = _protected_paths(policy)
    coherent = inv.get("coherent_keep_paths", [])
    current_tuple = _flatten(coherent[0]) if isinstance(coherent, list) and coherent else []
    refreshed = bool(current_tuple)
    for key in ("manager_releases", "work_tg_releases"):
        root = _root(policy, key)
        if root and (root / "current").is_symlink() and str((root / "current").resolve()) not in current_tuple:
            refreshed = False
    for key, pattern, component in [
        ("manager_releases", MANAGER_NAME, "manager"),
        ("work_tg_releases", TELEGRAM_NAME, "work_telegram"),
        ("tg_runtimes", SHA, "work_telegram_runtime"),
    ]:
        root = _root(policy, key)
        if root is None:
            continue
        for path in sorted(root.iterdir()):
            if path.name == "current":
                continue
            if not refreshed:
                protected.append({"path": str(path), "component": component, "reason": "policy_refresh_required"})
                continue
            if _overlaps(path, keep):
                protected.append({"path": str(path), "component": component, "reason": "coherent_current_or_previous"})
                continue
            if not pattern.fullmatch(path.name):
                skipped.append({"path": str(path), "reason": "unrecognized_release"})
                continue
            try:
                if component.endswith("runtime"):
                    marker = path / ".dependencies-ready"
                    _regular(marker)
                    if marker.stat().st_uid != OWNER_UID or stat.S_IMODE(marker.stat().st_mode) != 0o600:
                        raise MaintenanceError("runtime_marker_invalid")
                elif component == "manager":
                    revision = (path / "REVISION").read_text().strip()
                    if not SHA.fullmatch(revision) or not path.name.endswith(revision[:12]):
                        raise MaintenanceError("release_revision_invalid")
                    _regular(path / "MANIFEST.sha256")
                else:
                    for name in (
                        "autostop_manager/telegram_bridge.py",
                        "deploy/systemd/autostop-work-telegram.service",
                        "deploy/telegram/faster-whisper-small.sha256",
                    ):
                        _regular(path / name)
                candidates.append(
                    _candidate(
                        path,
                        root,
                        "release_directory",
                        component,
                        "older_owned_release",
                        {"metadata_valid": True, "root_key": key, "allow_leaf_symlinks": component.endswith("runtime")},
                        allow_links=component.endswith("runtime"),
                    )
                )
            except (MaintenanceError, OSError):
                skipped.append({"path": str(path), "reason": "invalid_release_metadata"})


def _images(
    policy: dict, runtime: dict, store_meta: dict, keepers: dict, candidates: list, protected: list, skipped: list
) -> None:
    inv = _inventory(policy)
    rows = [
        json.loads(line)
        for line in checked_run(
            ["docker", "image", "ls", "--no-trunc", "--format", "{{json .}}"], timeout=20
        ).splitlines()
        if line
    ]
    images = defaultdict(set)
    for row in rows:
        image = row.get("ID", "")
        if not IMAGE.fullmatch(image):
            raise MaintenanceError("docker_image_identity_invalid")
        repository, tag = row.get("Repository", ""), row.get("Tag", "")
        images[image].add(f"{repository}:{tag}")
    used = {row["image_id"] for row in runtime["containers"]}
    keep = used | set(inv.get("protected_image_ids", [])) | set(inv.get("keep_image_ids", []))
    keep.update(store_meta.get(key, "") for key in ("previous_image_id", "previous_db_image_id"))
    expected = inv.get("expected_container_images", {})
    refresh = any(
        expected.get(row["name"].lstrip("/"), row["image_id"]) != row["image_id"] for row in runtime["containers"]
    )
    if "crm" in keepers:
        tag = "autostopcrm-rollback:" + Path(keepers["crm"]).name
        matches = [image for image, tags in images.items() if tag in tags]
        if len(matches) != 1:
            raise MaintenanceError("crm_previous_image_missing")
        keep.update(matches)
    if store_meta and any(store_meta.get(key) not in images for key in ("previous_image_id", "previous_db_image_id")):
        raise MaintenanceError("store_previous_image_missing")
    # Conservative exclusive layer bytes. Shared layers/cache are never added.
    sizes = {}
    data = run(
        [
            "curl",
            "--silent",
            "--show-error",
            "--max-time",
            "15",
            "--unix-socket",
            inv.get("docker_socket", "/var/run/docker.sock"),
            "http://localhost/system/df",
        ],
        timeout=20,
    )
    if data.returncode == 0:
        try:
            sizes = {
                row["Id"]: max(0, row["Size"] - max(0, row["SharedSize"]))
                for row in json.loads(data.stdout).get("Images", [])
            }
        except (KeyError, ValueError, TypeError):
            sizes = {}
    for image, tags in sorted(images.items()):
        if image in keep or refresh:
            protected.append(
                {
                    "image_id": image,
                    "tags": sorted(tags),
                    "reason": "policy_refresh_required" if refresh else "current_container_or_closest_previous",
                }
            )
            continue
        crm = all(CRM_TAG.fullmatch(tag) for tag in tags)
        store = all(STORE_TAG.fullmatch(tag) for tag in tags)
        if not (crm or store):
            skipped.append({"image_id": image, "reason": "unknown_or_mixed_image_tags"})
            continue
        candidates.append(
            {
                "kind": "docker_image",
                "image_id": image,
                "tags": sorted(tags),
                "component": "crm" if crm else "store",
                "identity": {"image_id": image, "tags": sorted(tags)},
                "estimated_reclaim_bytes": sizes.get(image, 0),
                "reason": "older_unreferenced_owned_image",
                "proof": {"metadata_valid": True, "all_container_references_checked": True, "requires_ci": True},
            }
        )


def collect(policy: dict) -> dict:
    baseline = snapshot(policy)
    candidates, protected, skipped = [], [], []
    keepers, store_meta = _recoveries(policy, candidates, protected, skipped)
    _pg_inventory(policy, keepers.get("full"), candidates, protected, skipped)
    if policy.get("operation") != "pg-retain":
        _versioned(policy, candidates, protected, skipped)
        _images(policy, baseline, store_meta, keepers, candidates, protected, skipped)
    pinned = set() if policy.get("operation") == "pg-retain" else _protected_paths(policy)
    for raw in [] if policy.get("operation") == "pg-retain" else _inventory(policy).get("cache_roots", []):
        path = Path(raw)
        if not path.exists():
            continue
        if path.name not in {"htmlcov", ".mypy_cache", ".ruff_cache", "__pycache__", "http-v2", "wheel"} or _overlaps(
            path, pinned
        ):
            skipped.append({"path": str(path), "reason": "cache_not_owned_or_protected"})
            continue
        try:
            candidates.append(
                _candidate(
                    path,
                    path.parent,
                    "cache_directory",
                    "technical_cache",
                    "reproducible_completed_cache",
                    {"metadata_valid": True, "cache_root": str(path)},
                )
            )
        except MaintenanceError:
            skipped.append({"path": str(path), "reason": "unsafe_cache_candidate"})
    guarded = []
    paths = [Path(row["path"]) for row in candidates if "path" in row]
    processes = process_references(paths) if paths else {}
    units = unit_references(paths) if paths else {}
    for candidate in candidates:
        if "path" in candidate and _overlaps(Path(candidate["path"]), pinned):
            protected.append({"path": candidate["path"], "reason": "explicit_or_runtime_pin"})
        elif "path" in candidate and (processes.get(candidate["path"]) or units.get(candidate["path"])):
            protected.append({"path": candidate["path"], "reason": "active_process_or_unit_reference"})
        else:
            guarded.append(candidate)
    return {
        "candidates": guarded,
        "protected": protected,
        "skipped": skipped,
        "fingerprint": _hash(baseline),
        "keepers": keepers,
    }


def _archive_check(path: Path) -> None:
    try:
        with gzip.open(path, "rb") as source:
            while source.read(1024 * 1024):
                pass
        with tarfile.open(path, "r|gz") as archive:
            for member in archive:
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or ".." in relative.parts or member.isdev() or member.isfifo():
                    raise MaintenanceError("preserved_archive_structure_invalid")
                if member.issym() or member.islnk():
                    target = PurePosixPath(member.linkname)
                    if target.is_absolute() or ".." in target.parts:
                        raise MaintenanceError("preserved_archive_link_invalid")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise MaintenanceError("preserved_archive_invalid") from exc


def _pg_check(path: Path, policy: dict) -> None:
    container = _inventory(policy).get("pg_container", "autostop-db")
    for arguments in (["--list"], ["--file=/dev/null", "--no-owner", "--no-privileges"]):
        result = run(["docker", "exec", "-i", container, "pg_restore", *arguments], timeout=180, input_file=path)
        if result.returncode:
            raise MaintenanceError("preserved_pg_invalid")


def _verify_artifacts(directory: Path, artifacts: dict, policy: dict) -> None:
    for name, metadata in artifacts.items():
        path = _member(directory, name)
        if sha256_file(path) != metadata["sha256"]:
            raise MaintenanceError("preserved_hash_mismatch")
        if name.endswith(".tar.gz"):
            _archive_check(path)
        elif name.endswith((".sqlite3", ".sqlite")):
            try:
                with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
                    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                        raise MaintenanceError("preserved_sqlite_invalid")
            except sqlite3.Error as exc:
                raise MaintenanceError("preserved_sqlite_invalid") from exc
        elif name.endswith(".dump"):
            _pg_check(path, policy)


def verify_protected(policy: dict) -> dict:
    inventory = collect(policy)
    receipts = []
    for component, value in inventory["keepers"].items():
        directory = Path(value)
        artifacts = (
            _store_artifacts(directory)
            if component == "store"
            else _manifest_artifacts(directory, full=component == "full")
        )
        _verify_artifacts(directory, artifacts, policy)
        receipts.append({"component": component, "path": value, "ok": True, "artifact_count": len(artifacts)})
    for entry in inventory["protected"]:
        if entry.get("component") == "pg":
            _pg_check(Path(entry["path"]), policy)
            receipts.append({"component": "pg", "path": entry["path"], "ok": True})
    if policy.get("operation") != "pg-retain":
        for path in _protected_paths(policy):
            if not path.exists():
                raise MaintenanceError("protected_path_unavailable")
        for raw in _flatten(_inventory(policy).get("coherent_keep_paths", [])):
            path = Path(raw)
            if (path / "REVISION").is_file():
                revision = (path / "REVISION").read_text().strip()
                if not SHA.fullmatch(revision) or not path.name.endswith(revision[:12]):
                    raise MaintenanceError("preserved_revision_invalid")
                _verify_checksum_list(path, path / "MANIFEST.sha256")
            elif SHA.fullmatch(path.name):
                _regular(path / ".dependencies-ready")
                _regular(path / ".model-ready")
                _verify_checksum_list(path / "model", path / "faster-whisper-small.sha256")
            elif TELEGRAM_NAME.fullmatch(path.name):
                _verify_work_release(path, policy)
            receipts.append({"component": "coherent_runtime", "path": str(path), "ok": True})
    return {"ok": True, "receipts": receipts, "fingerprint": inventory["fingerprint"]}


def _checksum_rows(manifest: Path) -> dict[str, str]:
    _regular(manifest)
    lines = [line for line in manifest.read_text().splitlines() if line.strip() and not line.startswith("#")]
    if not lines:
        raise MaintenanceError("preserved_checksums_empty")
    result = {}
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)
        if not match:
            raise MaintenanceError("preserved_checksums_invalid")
        name = match[2].removeprefix("./")
        if name in result:
            raise MaintenanceError("preserved_checksums_invalid")
        result[name] = match[1]
    return result


def _verify_checksum_list(directory: Path, manifest: Path) -> None:
    for name, digest in _checksum_rows(manifest).items():
        if sha256_file(_member(directory, name)) != digest:
            raise MaintenanceError("preserved_hash_mismatch")


def _verify_work_release(path: Path, policy: dict) -> str:
    match = TELEGRAM_NAME.fullmatch(path.name)
    managers = _root(policy, "manager_releases")
    if not match or managers is None:
        raise MaintenanceError("work_release_identity_invalid")
    matches = [
        child
        for child in managers.iterdir()
        if MANAGER_NAME.fullmatch(child.name) and child.name.endswith("-manager-" + match[1])
    ]
    if len(matches) != 1:
        raise MaintenanceError("work_release_manager_binding_invalid")
    manager = matches[0]
    revision = (manager / "REVISION").read_text().strip()
    if not SHA.fullmatch(revision) or revision[:12] != match[1]:
        raise MaintenanceError("work_release_revision_invalid")
    _verify_checksum_list(manager, manager / "MANIFEST.sha256")
    trusted = _checksum_rows(manager / "MANIFEST.sha256")
    # Native work releases are git archives and have no REVISION/MANIFEST.
    # Compare the complete executable Python package and its launch/config
    # files to the matching trusted Manager manifest; never import the code.
    names = [str(child.relative_to(path)) for child in (path / "autostop_manager").rglob("*.py")]
    names += [
        name
        for name in (
            "deploy/systemd/autostop-work-telegram.service",
            "deploy/telegram/faster-whisper-small.sha256",
            "scripts/run-work-telegram-media.sh",
        )
        if (path / name).exists()
    ]
    if "autostop_manager/telegram_bridge.py" not in names:
        raise MaintenanceError("work_release_coverage_missing")
    for name in names:
        if name not in trusted or sha256_file(_member(path, name)) != trusted[name]:
            raise MaintenanceError("work_release_source_mismatch")
    return revision


def collect_current_tuple(policy: dict) -> dict:
    """Discover a cold-verifiable current tuple, never guess a prior by mtime."""
    roots = [_root(policy, key) for key in ("manager_releases", "work_tg_releases", "tg_runtimes")]
    if any(root is None for root in roots):
        raise MaintenanceError("current_tuple_roots_missing")
    manager, telegram = [(root / "current").resolve(strict=True) for root in roots[:2]]
    revision = (manager / "REVISION").read_text().strip()
    if not SHA.fullmatch(revision) or _verify_work_release(telegram, policy) != revision:
        raise MaintenanceError("current_tuple_revision_mismatch")
    runtime = roots[2] / revision
    _verify_checksum_list(manager, manager / "MANIFEST.sha256")
    _regular(runtime / ".dependencies-ready")
    _regular(runtime / ".model-ready")
    _verify_checksum_list(runtime / "model", runtime / "faster-whisper-small.sha256")
    paths = list(map(str, [manager, telegram, runtime]))
    coherent = _inventory(policy).get("coherent_keep_paths", [])
    previous = _flatten(coherent[0]) if coherent else []
    return {
        "ok": True,
        "revision": revision,
        "current_tuple": paths,
        "previous_known_current_tuple": previous,
        "policy_refresh_required": paths != previous,
    }


def revalidate(candidate: dict, policy: dict) -> None:
    """Recompute allowlists and the candidate's identity; never mutate a target."""
    refreshed = collect(policy)
    key = "image_id" if candidate.get("kind") == "docker_image" else "path"
    current = next(
        (
            row
            for row in refreshed["candidates"]
            if row.get(key) == candidate.get(key) and row.get("kind") == candidate.get("kind")
        ),
        None,
    )
    if (
        current is None
        or current.get("identity") != candidate.get("identity")
        or current.get("proof") != candidate.get("proof")
    ):
        raise MaintenanceError("candidate_identity_or_policy_changed")
    if key == "path":
        paths = [Path(candidate["path"])]
        if any(process_references(paths).values()) or any(unit_references(paths).values()):
            raise MaintenanceError("candidate_in_use")
