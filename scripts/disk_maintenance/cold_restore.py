"""Explicit locked hydration; publication never replaces an existing runtime."""

from __future__ import annotations

import ctypes
import os
import shutil
import stat
import uuid
from pathlib import Path

from . import cold_cas, cold_registry
from .util import MaintenanceError, no_symlink_ancestors, private_directory


OWNER_UID = 0


def _remove_staging(staging: Path, identity: tuple[int, int]) -> None:
    private_directory(staging.parent)
    try:
        info = staging.lstat()
    except FileNotFoundError:
        # A successful rename, including one followed by an fsync error, has
        # already moved this path. Never remove its published destination.
        return
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != OWNER_UID or (info.st_dev, info.st_ino) != identity:
        raise MaintenanceError("cold_staging_changed")
    if not shutil.rmtree.avoids_symlink_attacks:
        raise MaintenanceError("cold_staging_cleanup_unsupported")
    shutil.rmtree(staging)


def _publish(staging: Path, target: Path) -> None:
    no_symlink_ancestors(target)
    parent = target.parent.lstat()
    if parent.st_uid != OWNER_UID or parent.st_mode & 0o022 or parent.st_dev != staging.lstat().st_dev:
        raise MaintenanceError("cold_activation_parent_invalid")
    # Linux renameat2 provides atomic RENAME_NOREPLACE, including a raced-in dir.
    library = ctypes.CDLL(None, use_errno=True)
    function = library.renameat2
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(-100, os.fsencode(staging), -100, os.fsencode(target), 1):
        raise MaintenanceError("cold_activation_refused")
    descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def restore(policy: dict, revision: str, approval: str, *, output: Path | None, activate: bool) -> dict:
    # Import lazily to avoid the inventory/core import cycle.
    from . import core

    matches = [row for row in cold_registry.records(policy).values() if row["revision"] == revision]
    if len(matches) != 1 or approval != matches[0]["manifest_sha256"]:
        raise MaintenanceError("cold_restore_approval_invalid")
    record = matches[0]
    if activate == (output is not None):
        raise MaintenanceError("cold_restore_mode_invalid")
    with core.locked(Path(policy["locks"]["cleanup"])), core.native_locks(policy):
        cold_registry.attest(record, full=True)
        core.health(policy)
        target = Path(record["path"])
        if activate and (target.exists() or target.is_symlink()):
            raise MaintenanceError("cold_destination_exists")
        destination: Path | None
        if activate:
            parent = Path(policy["state_root"])
            private_directory(parent)
            destination = parent / ("tg-cold-restore-" + uuid.uuid4().hex)
        else:
            destination = output
        if destination is None:
            raise MaintenanceError("cold_restore_mode_invalid")
        result = cold_cas.restore_root(
            Path(record["bundle_dir"]),
            approval,
            record["root_id"],
            destination,
            expected_revision=revision,
            floor_bytes=6 * 1024**3,
        )
        staging_identity = None
        if activate:
            info = destination.lstat()
            staging_identity = (info.st_dev, info.st_ino)
        try:
            # Originals, aliases and services are unchanged in private-output mode.
            _, root = cold_registry.attest(record)
            cold_cas.verify_tree(destination, root)
            if activate:
                _publish(destination, target)
                cold_cas.verify_tree(target, root)
            core.health(policy)
        except BaseException as error:
            if staging_identity is not None:
                try:
                    _remove_staging(destination, staging_identity)
                except (MaintenanceError, OSError) as cleanup_error:
                    error.add_note("cold_staging_cleanup_failed")
                    raise error from cleanup_error
            raise
        return {**result, "activated_original_path": activate, "application_restart": False}
