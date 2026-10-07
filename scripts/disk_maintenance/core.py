"""Root-owned, bounded cleanup with private approvals and durable CI recovery."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import ci, inventory
from .util import (
    MaintenanceError,
    atomic_json,
    no_symlink_ancestors,
    private_directory,
    read_json,
    run,
    sha256_file,
)

POLICY_SCHEMA = "autostop_disk_policy_v1"
MANIFEST_SCHEMA = "autostop_disk_manifest_v1"
ROOT_UID = 0
DEFAULT_POLICY_PATH = Path("/etc/autostop-maintenance/policy.json")
DEFAULT_POLICY = {
    "schema": POLICY_SCHEMA,
    "automatic": False,
    "pg_retention_enabled": False,
    "state_root": "/var/lib/autostop-manager/private/disk-maintenance",
    "filesystem_root": "/",
    "goal_bytes": 20_000_000_000,
    "manifest_ttl_seconds": 900,
    "delete_budget_seconds": 900,
    "timezone": "Asia/Krasnoyarsk",
    "pg_keep_days": 3,
    "locks": {
        "cleanup": "/run/lock/autostop-disk-maintenance.lock",
        "full": "/var/backups/autostop-complete/.backup.lock",
        "pg": "/run/lock/autostop24-db-backup.lock",
        "crm": "/opt/autostopcrm/.autostop-deploy.lock",
        "tg": "/run/autostop-work-telegram-control.lock",
    },
    "ci": {
        "repository": "AutoStopKrsk/AutoStop-App",
        "runner_unit": "actions.runner.AutoStopKrsk-AutoStop-App.vps26457.service",
        "runner_root": "/opt/actions-runner",
        "poll_seconds": 5,
    },
    "health": {"units": [], "containers": [], "http": []},
    "inventory": {
        "roots": {
            "pg_dumps": "/var/backups/autostop24/database",
            "full_bundles": "/var/backups/autostop-complete",
            "crm_recovery": "/root/autostopcrm-backups/agent-gateway-v2",
            "store_recovery": "/opt/autostop-app-backups",
            "manager_releases": "/opt/autostop-manager-releases",
            "work_tg_releases": "/opt/autostop-work-telegram-releases",
            "personal_tg_releases": "/opt/autostop-telegram-releases",
            "tg_runtimes": "/opt/autostop-work-telegram-runtimes",
        },
        "store_deploy_marker": "/opt/autostop-app-deploys/current.env",
        "pinned_paths": [],
        "coherent_keep_paths": [],
        "cache_roots": [],
        "containers": [],
        "units": [],
        "git_roots": [],
        "config_paths": [],
        "keep_image_ids": [],
        "protected_image_ids": [],
    },
}
KINDS = {"pg_dump", "recovery_directory", "release_directory", "cache_directory", "docker_image"}
PATH_ROOTS = {
    "pg_dump": {"pg_dumps"},
    "recovery_directory": {"full_bundles", "crm_recovery", "store_recovery"},
    "release_directory": {"manager_releases", "work_tg_releases", "tg_runtimes"},
}
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,95}")


def digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _absolute(value) -> Path:
    if not isinstance(value, str):
        raise MaintenanceError("policy_path_invalid")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value:
        raise MaintenanceError("policy_path_invalid")
    return path


def validate_policy(policy: dict) -> dict:
    if policy.get("schema") != POLICY_SCHEMA:
        raise MaintenanceError("policy_schema_invalid")
    for name in ("automatic", "pg_retention_enabled"):
        if type(policy.get(name)) is not bool:
            raise MaintenanceError("policy_boolean_invalid")
    ranges = {"goal_bytes": (1, 10**15), "manifest_ttl_seconds": (1, 900), "delete_budget_seconds": (1, 900)}
    for name, (low, high) in ranges.items():
        if type(policy.get(name)) is not int or not low <= policy[name] <= high:
            raise MaintenanceError("policy_budget_invalid")
    if policy.get("pg_keep_days") != 3 or policy.get("timezone") != "Asia/Krasnoyarsk":
        raise MaintenanceError("policy_retention_invalid")
    try:
        ZoneInfo(policy["timezone"])
    except ZoneInfoNotFoundError as exc:
        raise MaintenanceError("timezone_unavailable") from exc
    _absolute(policy.get("filesystem_root"))
    if len(_absolute(policy.get("state_root")).parts) < 3:
        raise MaintenanceError("policy_state_root_invalid")
    locks = policy.get("locks")
    if not isinstance(locks, dict) or set(locks) != {"cleanup", "full", "pg", "crm", "tg"}:
        raise MaintenanceError("policy_locks_invalid")
    if len({str(_absolute(value)) for value in locks.values()}) != len(locks):
        raise MaintenanceError("policy_locks_invalid")
    inv = policy.get("inventory")
    if not isinstance(inv, dict) or not isinstance(inv.get("roots"), dict):
        raise MaintenanceError("inventory_policy_invalid")
    if set(inv["roots"]) - set(DEFAULT_POLICY["inventory"]["roots"]):
        raise MaintenanceError("inventory_root_unknown")
    inventory.cold_registry.records(policy)
    for value in [*inv["roots"].values(), *inv.get("cache_roots", [])]:
        if len(_absolute(value).parts) < 3:
            raise MaintenanceError("inventory_root_too_broad")
    for name in (
        "cache_roots",
        "pinned_paths",
        "coherent_keep_paths",
        "units",
        "containers",
        "git_roots",
        "config_paths",
        "keep_image_ids",
        "protected_image_ids",
    ):
        if not isinstance(inv.get(name, []), list):
            raise MaintenanceError("inventory_list_invalid")
        if name != "coherent_keep_paths" and any(not isinstance(item, str) for item in inv.get(name, [])):
            raise MaintenanceError("inventory_list_invalid")
    if not isinstance(policy.get("ci"), dict):
        raise MaintenanceError("ci_policy_invalid")
    _validate_health(policy.get("health"))
    runner = policy.get("ci", {}).get("runner_unit", DEFAULT_POLICY["ci"]["runner_unit"])
    if runner in inv.get("units", []) or runner in policy["health"]["units"]:
        raise MaintenanceError("ci_runner_in_application_baseline")
    return policy


def _validate_health(settings) -> None:
    if not isinstance(settings, dict):
        raise MaintenanceError("health_policy_invalid")
    for key in ("units", "containers", "http"):
        if not isinstance(settings.get(key), list):
            raise MaintenanceError("health_policy_invalid")
        if key != "http" and any(not isinstance(item, str) or item.startswith("-") for item in settings[key]):
            raise MaintenanceError("health_policy_invalid")
    for entry in settings["http"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("url"), str) or type(entry.get("status")) is not int:
            raise MaintenanceError("health_policy_invalid")
        parsed = urllib.parse.urlsplit(entry["url"])
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise MaintenanceError("health_policy_invalid")


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> dict:
    return validate_policy(read_json(Path(path), private=True))


def require_root() -> None:
    if os.geteuid() != ROOT_UID:
        raise MaintenanceError("root_required")


def state_root(policy: dict) -> Path:
    path = Path(policy["state_root"])
    private_directory(path)
    return path


def host_identity(policy: dict) -> dict:
    root = Path(policy["filesystem_root"])
    no_symlink_ancestors(root)
    if root.is_symlink() or not root.is_dir():
        raise MaintenanceError("filesystem_root_invalid")
    return {
        "hostname": os.uname().nodename,
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "root_dev": root.stat().st_dev,
    }


def available_bytes(policy: dict) -> int:
    info = os.statvfs(policy["filesystem_root"])
    return info.f_bavail * info.f_frsize


def health(policy: dict) -> dict:
    """Only technical status is retained; HTTP response bodies are never read."""
    settings = policy["health"]
    for unit in settings["units"]:
        result = run(["systemctl", "is-active", unit], timeout=10)
        if result.returncode or result.stdout.strip() != "active":
            raise MaintenanceError("health_unit_failed")
    for name in settings["containers"]:
        result = run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                name,
            ],
            timeout=10,
        )
        if result.returncode or result.stdout.strip() not in {"true healthy", "true none"}:
            raise MaintenanceError("health_container_failed")
    for entry in settings["http"]:
        try:
            with urllib.request.urlopen(entry["url"], timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except (OSError, urllib.error.URLError) as exc:
            raise MaintenanceError("health_http_failed") from exc
        if status != entry["status"]:
            raise MaintenanceError("health_http_failed")
    return {
        "ok": True,
        "unit_count": len(settings["units"]),
        "container_count": len(settings["containers"]),
        "http_count": len(settings["http"]),
    }


@contextmanager
def locked(path: Path, *, create: bool = False):
    """The native lock inode is never created or replaced by this collector."""
    no_symlink_ancestors(path)
    flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != ROOT_UID or info.st_mode & 0o022 or info.st_nlink != 1:
            raise MaintenanceError("lock_file_invalid")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MaintenanceError("native_lock_busy") from exc
        if (path.lstat().st_dev, path.lstat().st_ino) != (info.st_dev, info.st_ino):
            raise MaintenanceError("lock_inode_changed")
        yield
    finally:
        os.close(fd)


@contextmanager
def native_locks(policy: dict):
    names = ["full", "pg"] if policy.get("operation") == "pg-retain" else ["full", "pg", "crm", "tg"]
    with ExitStack() as stack:
        for name in names:
            stack.enter_context(locked(Path(policy["locks"][name])))
        yield


def make_plan(policy: dict, *, now: float | None = None) -> dict:
    require_root()
    health(policy)
    baseline = inventory.snapshot(policy)
    collected = inventory.collect(policy)
    if digest(baseline) != collected["fingerprint"] or inventory.snapshot(policy) != baseline:
        raise MaintenanceError("inventory_changed")
    timestamp = time.time() if now is None else now
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "operation_id": uuid.uuid4().hex,
        "created_at": timestamp,
        "expires_at": timestamp + policy["manifest_ttl_seconds"],
        "policy_sha256": digest(policy),
        "host": host_identity(policy),
        "baseline": baseline,
        "baseline_fingerprint": collected["fingerprint"],
        "free_bytes": available_bytes(policy),
        "goal_bytes": policy["goal_bytes"],
        "protected": collected["protected"],
        "candidates": collected["candidates"],
        "skipped": collected["skipped"],
    }
    _validate_candidates(manifest, policy)
    return manifest


def save_plan(policy: dict, output: Path) -> dict:
    manifest = make_plan(policy)
    atomic_json(output, manifest)
    return {
        "ok": True,
        "manifest_sha256": sha256_file(output),
        "candidate_count": len(manifest["candidates"]),
        "free_bytes": manifest["free_bytes"],
        "goal_bytes": policy["goal_bytes"],
    }


def _protected_paths(manifest: dict, policy: dict) -> list[Path]:
    values = [
        entry["path"]
        for entry in manifest["protected"]
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    ]
    # Inventory and execution must share the same narrow attested-cold mapping.
    # This still includes all explicit pins, hot tuples and cold carrier proofs.
    values.extend(map(str, inventory._protected_paths(policy)))
    return [_absolute(value) for value in values]


def _candidate_path(candidate: dict, policy: dict) -> Path:
    path = _absolute(candidate.get("path"))
    kind = candidate["kind"]
    inv = policy["inventory"]
    if kind == "cache_directory":
        allowed = path in map(Path, inv.get("cache_roots", []))
    else:
        allowed = any(path.parent == Path(inv["roots"][key]) for key in PATH_ROOTS.get(kind, []) if key in inv["roots"])
    if (
        not allowed
        or path.name in {"current", "previous", ".", ".."}
        or (kind != "cache_directory" and path.name.startswith("."))
    ):
        raise MaintenanceError("candidate_outside_owned_roots")
    return path


def _validate_candidates(manifest: dict, policy: dict) -> None:
    if not isinstance(manifest.get("candidates"), list) or not isinstance(manifest.get("protected"), list):
        raise MaintenanceError("manifest_candidates_invalid")
    protected = _protected_paths(manifest, policy)
    seen = set()
    for candidate in manifest["candidates"]:
        if (
            not isinstance(candidate, dict)
            or candidate.get("kind") not in KINDS
            or not isinstance(candidate.get("identity"), dict)
        ):
            raise MaintenanceError("manifest_candidate_invalid")
        if type(candidate.get("estimated_reclaim_bytes")) is not int or candidate["estimated_reclaim_bytes"] < 0:
            raise MaintenanceError("manifest_candidate_invalid")
        if not isinstance(candidate.get("proof"), dict) or candidate["proof"].get("metadata_valid") is not True:
            raise MaintenanceError("manifest_candidate_unproven")
        if candidate["kind"] == "docker_image":
            target = candidate.get("image_id")
            if not isinstance(target, str) or not IMAGE_ID.fullmatch(target):
                raise MaintenanceError("manifest_image_invalid")
        else:
            path = _candidate_path(candidate, policy)
            if any(path == pin or path.is_relative_to(pin) or pin.is_relative_to(path) for pin in protected):
                raise MaintenanceError("candidate_protected_overlap")
            target = str(path)
        if target in seen:
            raise MaintenanceError("manifest_candidate_duplicate")
        seen.add(target)


def validate_manifest(manifest: dict, policy: dict, *, now: float | None = None) -> None:
    timestamp = time.time() if now is None else now
    if manifest.get("schema") != MANIFEST_SCHEMA or manifest.get("policy_sha256") != digest(policy):
        raise MaintenanceError("manifest_policy_changed")
    created, expires = manifest.get("created_at"), manifest.get("expires_at")
    if (
        type(created) not in {int, float}
        or type(expires) not in {int, float}
        or not created <= timestamp < expires
        or expires - created > policy["manifest_ttl_seconds"]
    ):
        raise MaintenanceError("manifest_expired")
    if manifest.get("host") != host_identity(policy):
        raise MaintenanceError("manifest_host_changed")
    if digest(manifest.get("baseline", {})) != manifest.get("baseline_fingerprint"):
        raise MaintenanceError("manifest_baseline_invalid")
    if inventory.snapshot(policy) != manifest["baseline"]:
        raise MaintenanceError("runtime_changed")
    _validate_candidates(manifest, policy)
    health(policy)


def requires_ci(candidate: dict) -> bool:
    proof = candidate.get("proof", {})
    return (
        proof.get("requires_ci") is True
        or candidate.get("component") == "store"
        or candidate.get("kind") in {"docker_image", "cache_directory"}
    )


def _mount_id(fd: int) -> str:
    for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines():
        if line.startswith("mnt_id:"):
            return line.split()[1]
    raise MaintenanceError("mount_identity_unavailable")


def _open_directory(path: Path) -> int:
    """Walk every path component with nofollow; ancestor swaps cannot redirect us."""
    no_symlink_ancestors(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError as exc:
        os.close(fd)
        raise MaintenanceError("candidate_parent_invalid") from exc
    except BaseException:
        os.close(fd)
        raise


def _deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise MaintenanceError("deletion_budget_exceeded")


def _walk_guard(fd: int, device: int, mount: str, allow_links: bool, deadline: float) -> None:
    _deadline(deadline)
    if os.fstat(fd).st_dev != device or _mount_id(fd) != mount:
        raise MaintenanceError("candidate_mount_escape")
    for name in os.listdir(fd):
        _deadline(deadline)
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                opened = os.fstat(child)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise MaintenanceError("candidate_inode_changed")
                _walk_guard(child, device, mount, allow_links, deadline)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode):
            if info.st_dev != device or info.st_nlink != 1:
                raise MaintenanceError("candidate_hardlink_or_device_escape")
        elif not (allow_links and stat.S_ISLNK(info.st_mode)):
            raise MaintenanceError("candidate_special_file")


def _remove_contents(fd: int, device: int, mount: str, allow_links: bool, deadline: float) -> None:
    _walk_guard(fd, device, mount, allow_links, deadline)
    for name in os.listdir(fd):
        _deadline(deadline)
        before = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                opened = os.fstat(child)
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise MaintenanceError("candidate_inode_changed")
                _remove_contents(child, device, mount, allow_links, deadline)
                current = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                    raise MaintenanceError("candidate_inode_changed")
                os.rmdir(name, dir_fd=fd)
            finally:
                os.close(child)
        else:
            current = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino, current.st_mode, current.st_nlink) != (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_nlink,
            ):
                raise MaintenanceError("candidate_inode_changed")
            if stat.S_ISREG(current.st_mode) and current.st_nlink != 1:
                raise MaintenanceError("candidate_hardlink_or_device_escape")
            if not (stat.S_ISREG(current.st_mode) or (allow_links and stat.S_ISLNK(current.st_mode))):
                raise MaintenanceError("candidate_special_file")
            if stat.S_ISREG(current.st_mode):
                leaf = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if _mount_id(leaf) != mount or os.fstat(leaf).st_ino != current.st_ino:
                        raise MaintenanceError("candidate_mount_escape")
                finally:
                    os.close(leaf)
            os.unlink(name, dir_fd=fd)


def secure_delete(candidate: dict, policy: dict, deadline: float) -> None:
    path = _candidate_path(candidate, policy)
    parent = _open_directory(path.parent)
    try:
        parent_info = os.fstat(parent)
        if parent_info.st_uid != ROOT_UID or parent_info.st_mode & 0o022:
            raise MaintenanceError("candidate_parent_unsafe")
        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        identity = candidate["identity"]
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
            identity.get("dev"),
            identity.get("ino"),
            identity.get("size"),
            identity.get("mtime_ns"),
        ):
            raise MaintenanceError("candidate_inode_changed")
        if info.st_uid != ROOT_UID or info.st_mode & 0o022 or info.st_dev != parent_info.st_dev:
            raise MaintenanceError("candidate_owner_or_device_invalid")
        _deadline(deadline)
        if stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise MaintenanceError("candidate_hardlink_or_device_escape")
            leaf = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                if _mount_id(leaf) != _mount_id(parent) or os.fstat(leaf).st_ino != info.st_ino:
                    raise MaintenanceError("candidate_mount_escape")
            finally:
                os.close(leaf)
            os.unlink(path.name, dir_fd=parent)
        elif stat.S_ISDIR(info.st_mode):
            child = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                if os.fstat(child).st_ino != info.st_ino or _mount_id(child) != _mount_id(parent):
                    raise MaintenanceError("candidate_mount_escape")
                runtime_root = policy["inventory"]["roots"].get("tg_runtimes")
                allow_links = bool(
                    runtime_root
                    and path.parent == Path(runtime_root)
                    and candidate["proof"].get("allow_leaf_symlinks") is True
                )
                _remove_contents(child, info.st_dev, _mount_id(parent), allow_links, deadline)
                current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise MaintenanceError("candidate_inode_changed")
                os.rmdir(path.name, dir_fd=parent)
            finally:
                os.close(child)
        else:
            raise MaintenanceError("candidate_special_file")
    finally:
        os.close(parent)


def delete_image(candidate: dict, deadline: float) -> None:
    _deadline(deadline)
    image_id = candidate["image_id"]
    tags = candidate["identity"].get("tags")
    if not isinstance(tags, list) or any(
        not isinstance(tag, str) or tag.startswith("-") or not re.fullmatch(r"[a-zA-Z0-9_./:-]+", tag) for tag in tags
    ):
        raise MaintenanceError("candidate_image_tags_invalid")
    remaining = set(tags)
    for target in tags or [image_id]:
        _deadline(deadline)
        inspected = run(
            ["docker", "image", "inspect", "--format", "{{json .Id}} {{json .RepoTags}}", image_id], timeout=15
        )
        try:
            actual_id, actual_tags = inspected.stdout.strip().split(" ", 1)
            matches = json.loads(actual_id) == image_id and set(json.loads(actual_tags) or []) == remaining
        except (ValueError, TypeError):
            matches = False
        if inspected.returncode or not matches:
            raise MaintenanceError("candidate_image_identity_changed")
        result = run(["docker", "image", "rm", target], timeout=min(60, max(1, deadline - time.monotonic())))
        if result.returncode:
            raise MaintenanceError("image_delete_failed")
        remaining.discard(target)


def _receipt(policy: dict, result: dict) -> None:
    name = {"recover-ci": "last-recovery.json", "pg-retain": "last-retention.json"}.get(
        result["operation"], "last-result.json"
    )
    atomic_json(state_root(policy) / name, result)


def _new_result(policy: dict, operation: str) -> dict:
    return {
        "schema": "autostop_disk_result_v1",
        "operation": operation,
        "started_at": datetime.now(UTC).isoformat(),
        "free_before": available_bytes(policy),
        "goal_bytes": policy["goal_bytes"],
        "deleted": [],
        "skipped": [],
        "errors": [],
        "ci_restored": True,
    }


@contextmanager
def cache_fence(candidate: dict):
    """APT uses POSIX record locks. A busy package job is skipped, never stopped."""
    if candidate["kind"] != "cache_directory" or not Path(candidate["path"]).is_relative_to("/var/cache/apt"):
        yield
        return
    descriptors = []
    try:
        for value in (
            "/var/lib/dpkg/lock-frontend",
            "/var/lib/dpkg/lock",
            "/var/lib/apt/lists/lock",
            "/var/cache/apt/archives/lock",
        ):
            path = Path(value)
            no_symlink_ancestors(path)
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
            descriptors.append(fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != ROOT_UID:
                raise MaintenanceError("apt_lock_invalid")
            try:
                fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MaintenanceError("apt_cache_busy") from exc
        yield
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _apply_locked(manifest: dict, policy: dict, result: dict, *, ci_ready: bool) -> None:
    validate_manifest(manifest, policy)
    proof = inventory.verify_protected(policy)
    if (
        proof.get("ok") is not True
        or not proof.get("receipts")
        or proof.get("fingerprint") != manifest["baseline_fingerprint"]
        or any(receipt.get("ok") is not True for receipt in proof["receipts"])
    ):
        raise MaintenanceError("preserved_verification_missing")
    present = {receipt.get("component") for receipt in proof["receipts"]}
    required = _required_receipts(manifest["candidates"], ci_ready=ci_ready)
    if not required.issubset(present):
        raise MaintenanceError("preserved_component_verification_missing")
    result["preserved_receipts"] = proof["receipts"]
    deadline = time.monotonic() + policy["delete_budget_seconds"]
    for candidate in manifest["candidates"]:
        _deadline(deadline)
        if time.time() >= manifest["expires_at"]:
            raise MaintenanceError("manifest_expired")
        if requires_ci(candidate) and not ci_ready:
            result["skipped"].append({"component": candidate.get("component"), "reason": "ci_fence_unavailable"})
            continue
        if inventory.snapshot(policy) != manifest["baseline"]:
            raise MaintenanceError("runtime_changed")
        health(policy)
        inventory.revalidate(candidate, policy)
        try:
            with cache_fence(candidate):
                if candidate["kind"] == "docker_image":
                    delete_image(candidate, deadline)
                else:
                    secure_delete(candidate, policy, deadline)
        except MaintenanceError as exc:
            if exc.code not in {"apt_cache_busy", "apt_lock_invalid"}:
                raise
            result["skipped"].append({"component": candidate.get("component"), "reason": exc.code})
            continue
        result["deleted"].append(
            {
                "kind": candidate["kind"],
                "component": candidate.get("component"),
                "estimated_bytes": candidate["estimated_reclaim_bytes"],
            }
        )
        _receipt(policy, result)
        health(policy)
        if inventory.snapshot(policy) != manifest["baseline"]:
            raise MaintenanceError("runtime_changed")
        result["free_after"] = available_bytes(policy)


def _required_receipts(candidates: list[dict], *, ci_ready: bool) -> set[str]:
    """A reduced selector may never remove the evidence for its own rollback."""
    required = set()
    for candidate in candidates:
        if requires_ci(candidate) and not ci_ready:
            continue
        kind = candidate["kind"]
        if kind == "pg_dump":
            required.update({"full", "pg"})
        elif kind == "recovery_directory":
            component = candidate.get("component")
            if component not in {"full", "crm", "store"}:
                raise MaintenanceError("candidate_component_invalid")
            required.update({"full", component})
        elif kind == "docker_image":
            required.update({"full", "crm", "store"})
        elif kind == "release_directory":
            required.add("coherent_runtime")
    return required


def _resume(policy: dict, result: dict) -> None:
    path = state_root(policy) / "ci-state.json"
    if path.exists() or path.is_symlink():
        try:
            restored = ci.resume(policy, path)
            if restored.get("warnings"):
                result["ci_warnings"] = sorted(set(result.get("ci_warnings", []) + restored["warnings"]))
            result["ci_restored"] = restored.get("phase") in {"restored", "absent"}
            if not result["ci_restored"]:
                result["errors"].append("ci_recovery_required")
        except (MaintenanceError, OSError):
            result["ci_restored"] = False
            result["errors"].append("ci_recovery_required")


def _finish(policy: dict, result: dict) -> dict:
    result["free_after"] = available_bytes(policy)
    result["deficit_bytes"] = max(0, policy["goal_bytes"] - result["free_after"])
    result["target_reached"] = result["deficit_bytes"] == 0
    result["ok"] = not result["errors"] and result["ci_restored"]
    result["finished_at"] = datetime.now(UTC).isoformat()
    _receipt(policy, result)
    return result


def _error_code(exc: BaseException) -> str:
    code = exc.code if isinstance(exc, MaintenanceError) else "operation_failed"
    return code if SAFE_CODE.fullmatch(code) else "operation_failed"


def execute(policy: dict, *, operation: str, manifest: dict | None = None) -> dict:
    require_root()
    state_root(policy)
    result = _new_result(policy, operation)
    with locked(Path(policy["locks"]["cleanup"]), create=True):
        if operation != "pg-retain":
            _resume(policy, result)
        if not result["ci_restored"]:
            return _finish(policy, result)
        try:
            if manifest is not None:
                validate_manifest(manifest, policy)
            ci_ready = False
            if operation != "pg-retain":
                try:
                    ci.pause(policy, state_root(policy) / "ci-state.json", wait_seconds=1800)
                    ci_ready = True
                except (MaintenanceError, OSError) as exc:
                    result["skipped"].append({"reason": _error_code(exc)})
                    _resume(policy, result)
                    if not result["ci_restored"]:
                        raise MaintenanceError("ci_recovery_required") from exc
            with native_locks(policy):
                fresh = make_plan(policy) if manifest is None else manifest
                if manifest is None:
                    atomic_json(state_root(policy) / "last-plan.json", fresh)
                _apply_locked(fresh, policy, result, ci_ready=ci_ready)
        except (MaintenanceError, OSError, ValueError) as exc:
            result["errors"].append(_error_code(exc))
        finally:
            if operation != "pg-retain":
                _resume(policy, result)
    return _finish(policy, result)


def apply(policy: dict, manifest_path: Path, approval: str) -> dict:
    require_root()
    manifest = read_json(manifest_path, private=True)
    if not re.fullmatch(r"[0-9a-f]{64}", approval) or sha256_file(manifest_path) != approval:
        raise MaintenanceError("manifest_approval_invalid")
    # Confirm the exact bytes across the parse as well as the approval check.
    if read_json(manifest_path, private=True) != manifest:
        raise MaintenanceError("manifest_approval_invalid")
    return execute(policy, operation="apply", manifest=manifest)


def maintain(policy: dict) -> dict:
    if policy.get("automatic") is not True:
        raise MaintenanceError("automatic_policy_disabled")
    return execute(policy, operation="maintain")


def pg_retain(policy: dict) -> dict:
    if policy.get("pg_retention_enabled") is not True:
        raise MaintenanceError("pg_retention_policy_disabled")
    scoped = copy.deepcopy(policy)
    scoped["operation"] = "pg-retain"
    scoped["health"] = {"units": [], "containers": [], "http": []}
    try:
        result = execute(scoped, operation="pg-retain")
    except (MaintenanceError, OSError) as exc:
        result = _new_result(scoped, "pg-retain")
        result["errors"].append(_error_code(exc))
        _finish(scoped, result)
    result["retention_pending"] = bool(result["errors"])
    _receipt(scoped, result)
    return result


def recover_ci(policy: dict) -> dict:
    require_root()
    result = _new_result(policy, "recover-ci")
    try:
        with locked(Path(policy["locks"]["cleanup"]), create=True):
            _resume(policy, result)
    except MaintenanceError as exc:
        if exc.code != "native_lock_busy":
            raise
        # A running main job owns this state. The recovery timer must leave it
        # untouched, and ExecStopPost will retry after the main process exits.
        return {"ok": True, "busy": True}
    return _finish(policy, result)
