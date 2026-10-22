#!/usr/bin/env python3
"""Bound closed OS logs and disposable download/build caches; never application data."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

GIB = 1024**3
DOWNLOAD_ROOTS = (
    Path("/root/.cache/pip/http-v2"),
    Path("/root/.npm/_cacache"),
    Path("/home/runner/.cache/pip/http-v2"),
    Path("/home/runner/.npm/_cacache"),
)
LOG_ROOTS = tuple(
    map(
        Path,
        (
            "/var/log",
            "/var/log/nginx",
            "/var/log/apt",
            "/var/log/unattended-upgrades",
            "/var/log/letsencrypt",
            "/var/log/sysstat",
            "/var/log/autostop-server-maintenance",
        ),
    )
)
BUSY = re.compile(
    r"(?:^|[ /])(?:pip(?:3)?|npm|npx|uv|apt(?:-get)?|dpkg|Runner\.Worker|buildctl)(?:$|[ /])|docker(?:\s+buildx)?\s+build(?:\s|$)"
)
PIP_ENTRY = re.compile(r"pip(?:\d+(?:\.\d+)*)?\Z")
ROTATED = re.compile(
    r"(?:syslog|mail\.(?:log|err)|kern\.log|auth\.log|user\.log|cron\.log|ufw\.log|fail2ban\.log|dpkg\.log|alternatives\.log|apport\.log|cloud-init(?:-output)?\.log|boot\.log|ubuntu-advantage[^/]*\.log|dmesg)\.\d+(?:\.(?:gz|xz|bz2))?\Z"
)


@dataclass(frozen=True)
class Entry:
    path: Path
    device: int
    inode: int
    size: int
    allocated: int
    mtime_ns: int
    atime_ns: int
    ctime_ns: int
    links: int

    @classmethod
    def read(cls, path: Path) -> Entry | None:
        try:
            value = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(value.st_mode):
            return None
        return cls(
            path,
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_blocks * 512,
            value.st_mtime_ns,
            value.st_atime_ns,
            value.st_ctime_ns,
            value.st_nlink,
        )

    @property
    def last_use_ns(self) -> int:
        return max(self.mtime_ns, self.atime_ns, self.ctime_ns)


def regular_files(root: Path, recursive: bool = True) -> list[Entry]:
    if not root.is_dir() or root.resolve() != root:
        return []
    result = []
    for base, directories, files in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if not (Path(base) / name).is_symlink()] if recursive else []
        for name in files:
            entry = Entry.read(Path(base) / name)
            if entry is not None:
                result.append(entry)
    return result


def is_rotated_log(path: Path) -> bool:
    if path.parent == Path("/var/log"):
        return bool(ROTATED.fullmatch(path.name))
    if path.parent == Path("/var/log/sysstat"):
        return bool(re.fullmatch(r"sa(?:r)?\d{2}(?:\.(?:gz|xz|bz2))?", path.name))
    if path.parent == Path("/var/log/autostop-server-maintenance"):
        return bool(re.fullmatch(r"maintenance-\d{8}T\d{6}Z\.log", path.name))
    return path.parent in LOG_ROOTS and bool(re.fullmatch(r"[^/]+\.log\.\d+(?:\.(?:gz|xz|bz2))?", path.name))


def python_runs_pip(arguments: list[str]) -> bool:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "-m":
            return index + 1 < len(arguments) and arguments[index + 1] == "pip"
        if argument.startswith("-m"):
            return argument[2:] == "pip"
        if argument == "--":
            return index + 1 < len(arguments) and bool(PIP_ENTRY.fullmatch(Path(arguments[index + 1]).name))
        if argument.startswith("-") and len(argument) > 1 and set(argument[1:]) <= set("bBdEiIOPqsSuvx"):
            index += 1
        elif argument in {"-W", "-X", "--check-hash-based-pycs"}:
            index += 2
        elif argument.startswith(("-W", "-X")):
            index += 1
        else:
            # -c, stdin, other scripts and unknown options stop operand parsing.
            return not argument.startswith("-") and bool(PIP_ENTRY.fullmatch(Path(argument).name))
    return False


def busy_argv(arguments: list[str]) -> bool:
    if not arguments:
        return False
    executable = Path(arguments[0]).name
    if BUSY.fullmatch(executable) or PIP_ENTRY.fullmatch(executable) or "Runner.Worker" in executable:
        return True
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable):
        return python_runs_pip(arguments[1:])
    if executable == "docker":
        return arguments[1:2] == ["build"] or arguments[1:3] == ["buildx", "build"]
    return executable == "node" and len(arguments) > 1 and Path(arguments[1]).name in {"npm-cli.js", "npx-cli.js"}


def usage(proc: Path = Path("/proc")) -> tuple[set[tuple[int, int]], bool]:
    """No process arguments, credentials or file contents are returned or recorded."""
    opened: set[tuple[int, int]] = set()
    busy = False
    for process in proc.iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            arguments = process.joinpath("cmdline").read_bytes().split(b"\0")
            if arguments[-1] == b"":
                arguments.pop()
            # Preserve argv boundaries, including empty operands and paths with spaces.
            busy |= busy_argv([argument.decode(errors="replace") for argument in arguments])
            for descriptor in process.joinpath("fd").iterdir():
                try:
                    value = descriptor.stat()
                    opened.add((value.st_dev, value.st_ino))
                except (FileNotFoundError, ProcessLookupError):
                    pass
            for line in process.joinpath("maps").read_text().splitlines():
                fields = line.split(maxsplit=5)
                if len(fields) == 6 and fields[5].startswith("/"):
                    try:
                        value = Path(fields[5]).stat()
                        opened.add((value.st_dev, value.st_ino))
                    except (FileNotFoundError, ProcessLookupError):
                        pass
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            # Unknown users/processes mean no destructive actions this cycle.
            return opened, True
    return opened, busy


def select_cache(
    entries: list[Entry], cutoff_ns: int, budget: int, opened: set[tuple[int, int]]
) -> tuple[list[Entry], int]:
    remaining = sum(entry.allocated for entry in entries)
    chosen = []
    for entry in sorted(entries, key=lambda item: item.last_use_ns):
        if entry.links != 1 or (entry.device, entry.inode) in opened:
            continue
        if entry.last_use_ns < cutoff_ns or remaining > budget:
            chosen.append(entry)
            remaining -= entry.allocated
    return chosen, remaining


def remove_exact(entry: Entry) -> bool:
    # Do not follow a replaced parent/symlink or delete a file used since inventory.
    if entry.path.parent.resolve() != entry.path.parent or Entry.read(entry.path) != entry:
        return False
    opened, busy = usage()
    if busy or (entry.device, entry.inode) in opened or entry.links != 1:
        return False
    if Entry.read(entry.path) != entry:
        return False
    # Resolve every parent with O_NOFOLLOW; a replaced cache-directory symlink
    # must not redirect this privileged unlink into an application data tree.
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in entry.path.parent.parts[1:]:
            next_descriptor = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        current = os.stat(entry.path.name, dir_fd=descriptor, follow_symlinks=False)
        if (
            (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
                current.st_ctime_ns,
                current.st_atime_ns,
            )
            != (entry.device, entry.inode, entry.size, entry.mtime_ns, entry.ctime_ns, entry.atime_ns)
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
        ):
            return False
        os.unlink(entry.path.name, dir_fd=descriptor)
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    finally:
        os.close(descriptor)


def retain(days: int, budget: int, apply: bool, docker: bool) -> dict:
    cutoff = time.time_ns() - days * 86400 * 10**9
    opened, busy = usage()
    logs = [
        entry
        for root in LOG_ROOTS
        for entry in regular_files(root, recursive=False)
        if is_rotated_log(entry.path)
        and entry.mtime_ns < cutoff
        and entry.links == 1
        and (entry.device, entry.inode) not in opened
    ]
    caches = [entry for root in DOWNLOAD_ROOTS for entry in regular_files(root)]
    cache_plan, projected = select_cache(caches, cutoff, budget, opened)
    result = {
        "apply": apply,
        "keep_days": days,
        "busy_or_unknown_usage": busy,
        "closed_log_candidates": len(logs),
        "closed_log_bytes": sum(item.allocated for item in logs),
        "download_before_bytes": sum(item.allocated for item in caches),
        "download_budget_bytes": budget,
        "download_candidate_files": len(cache_plan),
        "download_projected_bytes": projected,
        "removed_log_files": 0,
        "removed_download_files": 0,
        "removed_bytes": 0,
        "docker_budget_bytes": GIB,
        "docker_status": "not_requested",
        "application_data_touched": False,
    }
    if apply and not busy:
        for category, entries in (("log", logs), ("download", cache_plan)):
            for entry in entries:
                if remove_exact(entry):
                    result[f"removed_{category}_files"] += 1
                    result["removed_bytes"] += entry.allocated
        if docker:
            result["docker_status"] = "skipped_busy" if usage()[1] else "pending"
            if result["docker_status"] == "pending":
                for arguments in (
                    ("--filter", f"until={days * 24}h"),
                    ("--max-used-space", "1GiB", "--reserved-space", "0"),
                ):
                    if usage()[1]:
                        result["docker_status"] = "skipped_busy"
                        break
                    command = ["docker", "buildx", "prune", "--builder", "default", "--force", *arguments]
                    try:
                        completed = subprocess.run(command, capture_output=True, timeout=120, check=False)
                    except (OSError, subprocess.TimeoutExpired):
                        result["docker_status"] = "unverified_error"
                        break
                    if completed.returncode:
                        result["docker_status"] = "unverified_error"
                        break
                    result["docker_status"] = "pruned_unused_default_builder"
    elif docker:
        result["docker_status"] = "skipped_busy" if busy else "dry_run"
    result["download_after_bytes"] = sum(entry.allocated for root in DOWNLOAD_ROOTS for entry in regular_files(root))
    result["download_within_budget"] = result["download_after_bytes"] <= budget
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--download-budget", type=int, default=GIB)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--docker", action="store_true")
    args = parser.parse_args()
    if args.days < 1 or args.download_budget < 1:
        parser.error("positive retention days and download budget required")
    if args.apply:
        if os.geteuid() != 0:
            parser.error("apply requires root")
        descriptor = os.open(
            "/run/lock/autostop-technical-retention.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(json.dumps({"status": "skipped_already_running"}))
                return 0
            result = retain(args.days, args.download_budget, True, args.docker)
    else:
        result = retain(args.days, args.download_budget, False, args.docker)
    print(json.dumps(result, sort_keys=True))
    return 1 if result["docker_status"] == "unverified_error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
