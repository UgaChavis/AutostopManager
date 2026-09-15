#!/usr/bin/env python3
"""Explicit future migration only. Default is dry-run; no services are restarted."""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import stat
import tempfile
import tomllib
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

COMMAND = "/opt/autostop-managed-hooks/run_learning_hook.sh"


def learning_hook(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") == "command" and value.get("command") == COMMAND


def toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(k)} = {toml_value(v)}" for k, v in value.items()) + " }"
    raise ValueError("unsupported_hook_value")


def without_learning(text: str) -> tuple[str, int]:
    original = tomllib.loads(text)
    expected = copy.deepcopy(original)
    removed = 0
    for event, groups in list(expected.get("hooks", {}).items()):
        if not isinstance(groups, list):
            continue
        kept = []
        for group in groups:
            hooks = group.get("hooks", [])
            filtered = [h for h in hooks if not learning_hook(h)]
            removed += len(hooks) - len(filtered)
            if filtered or filtered == hooks:
                if filtered != hooks:
                    group["hooks"] = filtered
                kept.append(group)
        if kept:
            expected["hooks"][event] = kept
        else:
            del expected["hooks"][event]
    if not removed:
        return text, 0
    # Rewrite only affected array-table blocks. Keep every other section byte-for-byte.
    headers = list(re.finditer(r"(?m)^\s*(\[\[?[^\n]+?\]\]?)\s*(?:#.*)?$", text))
    edits = []
    for i, match in enumerate(headers):
        event = re.fullmatch(r"\[\[hooks\.([A-Za-z][A-Za-z0-9_]*)\]\]", match.group(1))
        if not event:
            continue
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block = text[match.start() : end]
        group = tomllib.loads(block)["hooks"][event.group(1)][0]
        hooks = group.get("hooks", [])
        if not any(learning_hook(h) for h in hooks):
            continue
        group["hooks"] = [h for h in hooks if not learning_hook(h)]
        replacement = ""
        if group["hooks"]:
            replacement = (
                match.group(1)
                + "\n"
                + "\n".join(f"{json.dumps(k)} = {toml_value(v)}" for k, v in group.items())
                + "\n\n"
            )
        edits.append((match.start(), end, replacement))
    for start, end, replacement in reversed(edits):
        text = text[:start] + replacement + text[end:]
    if tomllib.loads(text) != expected:
        raise ValueError("unsupported_hook_layout")
    return text, removed


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checked_file(path: Path) -> tuple[bytes, os.stat_result]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("regular_file_required")
    return path.read_bytes(), info


def atomic_replace(path: Path, previous: bytes, replacement: bytes, info: os.stat_result) -> None:
    fd, name = tempfile.mkstemp(prefix=".autostop-hook-change-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            os.fchmod(file.fileno(), stat.S_IMODE(info.st_mode))
            os.fchown(file.fileno(), info.st_uid, info.st_gid)
            file.write(replacement)
            file.flush()
            os.fsync(file.fileno())
        current, current_info = checked_file(path)
        if current != previous or current_info.st_ino != info.st_ino:
            raise ValueError("configuration_changed")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def migrate(config: Path, *, apply: bool = False, restore: Path | None = None) -> dict[str, Any]:
    before, info = checked_file(config)
    backup_name = None
    if restore:
        backup_data, backup_info = checked_file(restore)
        if stat.S_IMODE(backup_info.st_mode) & 0o077 or backup_info.st_uid != os.geteuid():
            raise ValueError("backup_permissions")
        backup = json.loads(backup_data)
        if backup["config"] != str(config.absolute()):
            raise ValueError("backup_target_mismatch")
        after = base64.b64decode(backup["original"], validate=True)
        if before == after:
            return {"ok": True, "changed": False}
        if digest(before) != backup["replacement_sha256"]:
            raise ValueError("configuration_changed_since_migration")
        if [stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid] != backup["permissions"]:
            raise ValueError("configuration_permissions_changed")
    else:
        clean, count = without_learning(before.decode("utf-8"))
        after = clean.encode("utf-8")
        if not count:
            return {"ok": True, "changed": False, "removed_hooks": 0}
    if apply:
        if not restore:
            fd, backup_name = tempfile.mkstemp(prefix="autostop-learning-backup-", suffix=".json", dir=config.parent)
            with os.fdopen(fd, "w") as file:
                json.dump(
                    {
                        "config": str(config.absolute()),
                        "original": base64.b64encode(before).decode(),
                        "replacement_sha256": digest(after),
                        "permissions": [stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid],
                    },
                    file,
                )
                file.flush()
                os.fsync(file.fileno())
        atomic_replace(config, before, after, info)
    return {"ok": True, "changed": apply, "change_needed": True, "backup": backup_name}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/etc/codex/requirements.toml"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restore", type=Path)
    args = parser.parse_args()
    try:
        result = migrate(args.config, apply=args.apply, restore=args.restore)
    except (OSError, ValueError, KeyError, TypeError):
        result = {"ok": False, "error": "hook_migration_failed_no_service_action"}
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
