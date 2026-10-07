"""Freeze a tested maintenance revision and adopt its explicitly approved timers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from .util import MaintenanceError, atomic_json, no_symlink_ancestors, private_file

INSTALL_SOURCE = ("scripts/install-autostop-disk-maintenance.py", "scripts/disk_maintenance/install.py")

PAYLOAD = (
    "scripts/autostop-disk-maintenance.py",
    "scripts/disk_maintenance/__init__.py",
    "scripts/disk_maintenance/util.py",
    "scripts/disk_maintenance/core.py",
    "scripts/disk_maintenance/inventory.py",
    "scripts/disk_maintenance/ci.py",
    "scripts/disk_maintenance/cold_cas.py",
    "scripts/disk_maintenance/cold_registry.py",
    "scripts/disk_maintenance/cold_restore.py",
)
UNITS = (
    "autostop-disk-maintenance.service",
    "autostop-disk-maintenance.timer",
    "autostop-disk-maintenance-recovery.service",
    "autostop-disk-maintenance-recovery.timer",
    "autostop24-db-backup.service.d/disk-retention.conf",
)
TIMERS = ("autostop-disk-maintenance.timer", "autostop-disk-maintenance-recovery.timer")
BASE = Path("/usr/local/libexec/autostop-disk-maintenance")
POLICY = Path("/etc/autostop-maintenance/policy.json")
STATE = Path("/var/lib/autostop-manager/private/disk-maintenance")
WRAPPER = Path("/usr/local/sbin/autostop-disk-maintenance")
SYSTEMD = Path("/etc/systemd/system")
WRAPPER_BYTES = (
    b"#!/bin/sh\nset -eu\numask 077\n"
    b"export LD_LIBRARY_PATH=/opt/autostop-sqlite/3.51.3/lib\n"
    b'exec /usr/bin/python3 -I /usr/local/libexec/autostop-disk-maintenance/current/autostop-disk-maintenance.py "$@"\n'
)


def _command(argv: list[str], *, allow_failure: bool = False) -> str:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        raise MaintenanceError("install_command_unavailable") from None
    if result.returncode and not allow_failure:
        raise MaintenanceError("install_command_failed")
    return result.stdout.strip()


def _directory(path: Path, mode: int) -> None:
    no_symlink_ancestors(path)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise MaintenanceError("install_directory_invalid")
    else:
        path.mkdir(mode=mode)
    if mode == 0o700:
        path.chmod(mode)


def _write(path: Path, data: bytes, mode: int, *, gid: int = 0) -> None:
    no_symlink_ancestors(path)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
            raise MaintenanceError("install_file_invalid")
    fd, temporary = tempfile.mkstemp(prefix=".disk-install-", dir=path.parent)
    try:
        os.fchown(fd, 0, gid)
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _payload(source: Path, revision: str) -> tuple[dict[str, bytes], str]:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise MaintenanceError("install_revision_invalid")
    if _command(["git", "-C", str(source), "rev-parse", "HEAD"]) != revision:
        raise MaintenanceError("install_source_revision_changed")
    paths = (*PAYLOAD, *INSTALL_SOURCE, *("deploy/systemd/" + name for name in UNITS))
    if _command(["git", "-C", str(source), "status", "--porcelain", "--", *paths]):
        raise MaintenanceError("install_source_dirty")
    result = {}
    for name in paths:
        _command(["git", "-C", str(source), "ls-files", "--error-unmatch", name])
        path = source / name
        no_symlink_ancestors(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise MaintenanceError("install_source_invalid")
        result[name] = path.read_bytes()
    digest = hashlib.sha256()
    for name in PAYLOAD:
        digest.update(name.encode() + b"\0" + result[name] + b"\0")
    return result, digest.hexdigest()


def _snapshot(paths: list[Path], directory: Path) -> list[dict]:
    result = []
    for index, path in enumerate(paths):
        no_symlink_ancestors(path)
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
                raise MaintenanceError("install_previous_file_invalid")
            backup = directory / f"previous-{index:02d}"
            data = path.read_bytes()
            _write(backup, data, 0o600)
            result.append(
                {
                    "path": str(path),
                    "backup": str(backup),
                    "mode": stat.S_IMODE(info.st_mode),
                    "gid": info.st_gid,
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        else:
            result.append({"path": str(path), "backup": None})
    return result


def _current_target() -> str | None:
    current = BASE / "current"
    if current.exists() or current.is_symlink():
        if not current.is_symlink() or current.lstat().st_uid != 0:
            raise MaintenanceError("install_current_invalid")
        if not re.fullmatch(r"releases/[0-9a-f]{64}", os.readlink(current)):
            raise MaintenanceError("install_current_invalid")
        return os.readlink(current)
    return None


def _activate(target: str | None) -> None:
    current = BASE / "current"
    _current_target()
    if target is None:
        current.unlink(missing_ok=True)
    else:
        _trusted_package(BASE / target)
        temporary = BASE / ".current-new"
        if temporary.exists() or temporary.is_symlink():
            raise MaintenanceError("install_activation_pending")
        os.symlink(target, temporary)
        os.replace(temporary, current)
    _sync_directory(BASE)
    if _current_target() != target:
        raise MaintenanceError("install_activation_readback_failed")


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _approved_policy(policy_file: Path) -> bytes:
    from .ci import _settings
    from .core import validate_policy

    private_file(policy_file)
    policy_bytes = policy_file.read_bytes()
    try:
        policy = json.loads(policy_bytes)
    except (ValueError, UnicodeError) as exc:
        raise MaintenanceError("install_policy_invalid") from exc
    if (
        not isinstance(policy, dict)
        or policy.get("schema") != "autostop_disk_policy_v1"
        or policy.get("state_root") != str(STATE)
    ):
        raise MaintenanceError("install_policy_invalid")
    validate_policy(policy)
    _settings(policy)
    return policy_bytes


def _trusted_directory(path: Path) -> None:
    no_symlink_ancestors(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise MaintenanceError("install_package_directory_invalid")


def _trusted_package(package: Path) -> None:
    _trusted_directory(package)
    _trusted_directory(package / "disk_maintenance")


def _install_package(package: Path, contents: dict[str, bytes]) -> None:
    if package.exists() or package.is_symlink():
        _trusted_package(package)
    else:
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=BASE / "releases"))
        try:
            _trusted_directory(staging)
            staging.chmod(0o750)
            (staging / "disk_maintenance").mkdir(mode=0o750)
            for name in PAYLOAD:
                relative = name.removeprefix("scripts/")
                _write(staging / relative, contents[name], 0o640)
            _trusted_package(staging)
            if package.exists() or package.is_symlink():
                raise MaintenanceError("install_package_appeared")
            os.rename(staging, package)
            _sync_directory(package.parent)
        finally:
            if staging.exists() and not staging.is_symlink():
                _trusted_directory(staging)
                if (staging / "disk_maintenance").exists() or (staging / "disk_maintenance").is_symlink():
                    _trusted_directory(staging / "disk_maintenance")
                shutil.rmtree(staging)
    _trusted_package(package)
    for name in PAYLOAD:
        path = package / name.removeprefix("scripts/")
        no_symlink_ancestors(path)
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_mode & 0o022
            or info.st_nlink != 1
            or path.read_bytes() != contents[name]
        ):
            raise MaintenanceError("install_payload_readback_failed")


def _timer_enabled(name: str) -> str:
    value = _command(["systemctl", "is-enabled", name], allow_failure=True) or "not-found"
    if value not in {"enabled", "enabled-runtime", "disabled", "not-found"}:
        raise MaintenanceError("install_timer_lifecycle_unsupported")
    return value


def _timer_active(name: str) -> str:
    value = _command(["systemctl", "is-active", name], allow_failure=True)
    if value not in {"active", "inactive"}:
        raise MaintenanceError("install_timer_lifecycle_unsupported")
    return value


def _stop_timer(name: str) -> None:
    if _timer_active(name) == "active":
        _command(["systemctl", "stop", name])
    if _timer_active(name) != "inactive":
        raise MaintenanceError("install_timer_stop_unconfirmed")


def _disable_timer(name: str) -> None:
    if _timer_enabled(name) in {"enabled", "enabled-runtime"}:
        _command(["systemctl", "disable", name])
    if _timer_enabled(name) not in {"disabled", "not-found"}:
        raise MaintenanceError("install_timer_disable_unconfirmed")


def _restore_file(item: dict) -> None:
    path = Path(item["path"])
    no_symlink_ancestors(path)
    if item["backup"]:
        backup = Path(item["backup"])
        private_file(backup)
        data = backup.read_bytes()
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise MaintenanceError("install_backup_changed")
        _write(path, data, item["mode"], gid=item["gid"])
        info = path.lstat()
        if (
            info.st_uid != 0
            or info.st_gid != item["gid"]
            or stat.S_IMODE(info.st_mode) != item["mode"]
            or path.read_bytes() != data
        ):
            raise MaintenanceError("install_restore_readback_failed")
    else:
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
                raise MaintenanceError("install_restore_target_invalid")
            path.unlink()
            _sync_directory(path.parent)
        if path.exists() or path.is_symlink():
            raise MaintenanceError("install_restore_readback_failed")


def _restore_enabled(name: str, original: str) -> None:
    current = _timer_enabled(name)
    if current != original:
        _disable_timer(name)
        if original == "enabled":
            _command(["systemctl", "enable", name])
        elif original == "enabled-runtime":
            _command(["systemctl", "enable", "--runtime", name])
    if _timer_enabled(name) != original:
        raise MaintenanceError("install_timer_enable_restore_unconfirmed")


def _restore_active(name: str, original: str) -> None:
    if _timer_active(name) != original:
        _command(["systemctl", "start" if original == "active" else "stop", name])
    if _timer_active(name) != original:
        raise MaintenanceError("install_timer_active_restore_unconfirmed")


def _attempt(errors: list[str], code: str, function, *args) -> None:
    try:
        function(*args)
    except (OSError, subprocess.SubprocessError, MaintenanceError):
        errors.append(code)


def _rollback(receipt_path: Path, receipt: dict) -> None:
    errors: list[str] = []
    receipt["status"], receipt["rollback_errors"] = "rollback_required", errors
    _attempt(errors, "rollback_receipt_failed", atomic_json, receipt_path, receipt)
    for name in TIMERS:
        _attempt(errors, "rollback_timer_stop_failed", _stop_timer, name)
        _attempt(errors, "rollback_timer_disable_failed", _disable_timer, name)
    configuration_start = len(errors)
    _attempt(errors, "rollback_current_failed", _activate, receipt["previous_current"])
    for item in receipt["previous_files"]:
        _attempt(errors, "rollback_file_failed", _restore_file, item)
    _attempt(errors, "rollback_reload_failed", _command, ["systemctl", "daemon-reload"])
    configuration_restored = len(errors) == configuration_start
    for name in TIMERS:
        _attempt(errors, "rollback_timer_enable_failed", _restore_enabled, name, receipt["previous_enabled"][name])
        if configuration_restored:
            _attempt(errors, "rollback_timer_active_failed", _restore_active, name, receipt["previous_active"][name])
        else:
            _attempt(errors, "rollback_timer_stop_failed", _stop_timer, name)
            errors.append("rollback_activation_skipped")
    receipt["status"] = "rollback_required" if errors else "rolled_back"
    _attempt(errors, "rollback_receipt_failed", atomic_json, receipt_path, receipt)
    if errors:
        raise MaintenanceError("install_rollback_incomplete")
    raise MaintenanceError("install_failed_rolled_back")


def install(source: Path, policy_file: Path, revision: str, receipt_path: Path) -> dict:
    if os.geteuid() != 0:
        raise MaintenanceError("install_root_required")
    if receipt_path.exists() or receipt_path.is_symlink():
        raise MaintenanceError("install_receipt_already_exists")
    contents, digest = _payload(source, revision)
    policy_bytes = _approved_policy(policy_file)
    _directory(BASE, 0o750)
    _directory(BASE / "releases", 0o750)
    _directory(POLICY.parent, 0o700)
    _directory(STATE, 0o700)
    _directory(receipt_path.parent, 0o700)
    _install_package(BASE / "releases" / digest, contents)
    unit_paths = [SYSTEMD / name for name in UNITS]
    for path in unit_paths:
        _directory(path.parent, 0o755)
    previous_current = _current_target()
    if previous_current is not None:
        _trusted_package(BASE / previous_current)
    prior = _snapshot([POLICY, WRAPPER, *unit_paths], receipt_path.parent)
    enabled = {name: _timer_enabled(name) for name in TIMERS}
    active = {name: _timer_active(name) for name in TIMERS}
    receipt = {
        "schema": "autostop_disk_install_v1",
        "status": "prepared",
        "revision": revision,
        "payload_sha256": digest,
        "previous_current": previous_current,
        "previous_files": prior,
        "previous_enabled": enabled,
        "previous_active": active,
        "policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
    }
    atomic_json(receipt_path, receipt)
    try:
        _write(POLICY, policy_bytes, 0o600)
        _write(WRAPPER, WRAPPER_BYTES, 0o750)
        for name, path in zip(UNITS, unit_paths, strict=True):
            _write(path, contents["deploy/systemd/" + name], 0o644)
        _activate("releases/" + digest)
        _command([str(WRAPPER), "--policy", str(POLICY), "--help"])
        _command(["systemd-analyze", "verify", *(str(path) for path in unit_paths[:4])])
        _command(["systemctl", "daemon-reload"])
        for name in TIMERS:
            _command(["systemctl", "enable", "--now", name])
            if (
                _command(["systemctl", "is-enabled", name]) != "enabled"
                or _command(["systemctl", "is-active", name]) != "active"
            ):
                raise MaintenanceError("install_timer_readback_failed")
        receipt["status"] = "installed"
        atomic_json(receipt_path, receipt)
    except (OSError, subprocess.SubprocessError, MaintenanceError):
        _rollback(receipt_path, receipt)
    return receipt
