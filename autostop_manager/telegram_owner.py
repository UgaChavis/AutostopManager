"""Root-controlled owner identity, kept outside source and Telegram history."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
import secrets
import stat


WORK_OWNER_CONFIG_PATH = Path("/etc/autostop-work-telegram/owner.json")
MAX_OWNER_PEER_ID = (1 << 52) - 1


class OwnerConfigError(ValueError):
    pass


def _validate_peer_id(value: object) -> int:
    if type(value) is not int or not 0 < value <= MAX_OWNER_PEER_ID:
        raise OwnerConfigError("owner_peer_id_invalid")
    return value


def _open_parent(path: Path, *, for_sync: bool = False) -> int:
    if not path.is_absolute():
        raise OwnerConfigError("owner_config_path_invalid")
    # The service has traverse-only access to /etc/autostop-work-telegram.
    # O_PATH permits openat/fstat without granting directory listing; root
    # enrollment needs a readable descriptor for the final directory fsync.
    access = os.O_RDONLY if for_sync else os.O_PATH
    fd = os.open(path.parent, access | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW)
    metadata = os.fstat(fd)
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
        os.close(fd)
        raise OwnerConfigError("owner_config_directory_untrusted")
    return fd


def load_owner_peer_id(path: Path | None) -> int | None:
    if path is None:
        return None
    parent_fd = file_fd = -1
    try:
        parent_fd = _open_parent(path)
        file_fd = os.open(path.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o027:
            raise OwnerConfigError("owner_config_untrusted")
        raw = os.read(file_fd, 4097)
        if len(raw) > 4096:
            raise OwnerConfigError("owner_config_invalid")
        payload = json.loads(raw)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "owner_peer_id", "kind"}
            or type(payload["schema_version"]) is not int
            or payload["schema_version"] != 1
            or payload["kind"] != "private"
        ):
            raise OwnerConfigError("owner_config_invalid")
        return _validate_peer_id(payload["owner_peer_id"])
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OwnerConfigError("owner_config_unreadable") from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def configure_owner_peer_id(path: Path, peer_id: int, *, reader_gid: int, replace: bool = False) -> None:
    """Enroll an independently verified private peer; never resolve identity by name."""

    if os.geteuid() != 0:
        raise OwnerConfigError("owner_config_requires_root")
    _validate_peer_id(peer_id)
    parent_fd = file_fd = -1
    temporary_name = f".owner-{secrets.token_hex(12)}.tmp"
    try:
        parent_fd = _open_parent(path, for_sync=True)
        file_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        os.fchown(file_fd, 0, reader_gid)
        os.fchmod(file_fd, 0o640)
        payload = {"schema_version": 1, "owner_peer_id": peer_id, "kind": "private"}
        os.write(file_fd, (json.dumps(payload) + "\n").encode("ascii"))
        os.fsync(file_fd)
        if replace:
            os.replace(temporary_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        else:
            os.link(temporary_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
            os.unlink(temporary_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileExistsError as exc:
        raise OwnerConfigError("owner_config_already_exists") from exc
    except OSError as exc:
        raise OwnerConfigError("owner_config_write_failed") from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if parent_fd >= 0:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=parent_fd)
            os.close(parent_fd)
