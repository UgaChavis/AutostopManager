#!/usr/bin/env python3
"""Plan/apply a local runtime-alias workspace; never deploy or publish anything.

Only clean tracked root entries are replaced. Untracked descendants block the
conversion; untracked root entries, data and the common Git directory stay put.
Git control files remain regular sealed bytes because Git ignores their symlinks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

BRANCH = "refs/heads/ops/runtime-workspace"
CONTROLS = {".gitignore", ".gitattributes"}


class Refused(RuntimeError):
    pass


def _git(workspace, *args, data=None, index=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1")
    env.update(GIT_AUTHOR_NAME="AutoStop workspace", GIT_AUTHOR_EMAIL="runtime@localhost")
    env.update(GIT_COMMITTER_NAME="AutoStop workspace", GIT_COMMITTER_EMAIL="runtime@localhost")
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-C", str(workspace), *args],
        input=data,
        capture_output=True,
        env=env,
        timeout=20,
    )
    if result.returncode:
        raise Refused("git_operation_failed:" + args[0])
    return result.stdout


def _owned(path, *, directory=False):
    info = path.lstat()
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise Refused("unsafe_owned_path")
    return info


def _hash(content):
    return hashlib.sha256(content).hexdigest()


def _identity(path):
    info = path.lstat()
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid]


def _preserved(workspace, replaced):
    result = {".git": _identity(workspace / ".git")}
    for path in sorted(workspace.iterdir()):
        if path.name in replaced or path.name in {".git", ".venv"}:
            continue
        if path.name == "data":
            result[path.name] = _identity(path)
            continue
        rows = []
        for item in [path, *sorted(path.rglob("*"))] if path.is_dir() and not path.is_symlink() else [path]:
            info = item.lstat()
            rows.append([str(item.relative_to(workspace)), *_identity(item), info.st_size, info.st_mtime_ns])
        result[path.name] = rows
    return result


def _service_python():
    result = subprocess.run(
        ["systemctl", "show", "autostop-manager-mcp.service", "--property=ExecStart", "--value"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    match = re.match(r"^\{\s+path=(/[^;\s]+)\s+;", result.stdout)
    if result.returncode or not match or " -m autostop_manager.mcp_server" not in result.stdout:
        raise Refused("effective_manager_python_unavailable")
    return Path(match[1])


def _runtime(python, release, workspace):
    python = python.absolute()
    if python != _service_python().absolute():
        raise Refused("effective_runtime_changed")
    venv = python.parent.parent.resolve(strict=True)
    if venv.is_relative_to(workspace) or not (venv / "pyvenv.cfg").is_file():
        raise Refused("runtime_venv_invalid")
    _owned(venv, directory=True)
    _owned(venv / "pyvenv.cfg")
    _owned(python.resolve(strict=True))
    code = (
        "import json,sys;sys.path.insert(0,sys.argv[1]);import autostop_manager;"
        "print(json.dumps({'prefix':sys.prefix,'module':autostop_manager.__file__}))"
    )
    proc = subprocess.run(
        [str(python), "-I", "-B", "-c", code, str(release)], cwd="/tmp", capture_output=True, text=True, timeout=10
    )
    if proc.returncode:
        raise Refused("runtime_import_failed")
    probe = json.loads(proc.stdout)
    if Path(probe["prefix"]).resolve() != venv or not Path(probe["module"]).resolve().is_relative_to(release):
        raise Refused("runtime_import_mismatch")
    return {
        "venv": str(venv),
        "venv_identity": _identity(venv),
        "python_identity": _identity(python),
        "binary_identity": _identity(python.resolve()),
        "pyvenv_sha256": _hash((venv / "pyvenv.cfg").read_bytes()),
    }


def _release(link, workspace):
    if not link.is_symlink():
        raise Refused("release_link_required")
    root = link.resolve(strict=True)
    _owned(root, directory=True)
    if root.is_relative_to(workspace):
        raise Refused("release_inside_workspace")
    revision = root / "REVISION"
    manifest = root / "MANIFEST.sha256"
    _owned(revision)
    _owned(manifest)
    sha = revision.read_text().strip()
    if not re.fullmatch("[0-9a-f]{40}", sha):
        raise Refused("release_revision_invalid")
    listed = set()
    for line in manifest.read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        relative = Path(name.removeprefix("*").removeprefix("./"))
        if not re.fullmatch("[0-9a-f]{64}", digest) or relative.is_absolute() or ".." in relative.parts:
            raise Refused("manifest_path_invalid")
        path = root / relative
        _owned(path)
        if path.resolve() != path or str(relative) in listed or _hash(path.read_bytes()) != digest:
            raise Refused("manifest_mismatch")
        listed.add(str(relative))
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise Refused("release_symlink")
        if path.is_file() and path != manifest:
            actual.add(str(path.relative_to(root)))
    if actual != listed or "REVISION" not in listed:
        raise Refused("manifest_coverage_mismatch")
    return {
        "root": str(root),
        "revision": sha,
        "manifest_sha256": _hash(manifest.read_bytes()),
        "link_target": os.readlink(link),
        "link_identity": _identity(link),
    }


def _records(workspace, head):
    records = []
    for row in _git(workspace, "ls-tree", "-r", "-z", head).split(b"\0"):
        if row:
            header, name = row.split(b"\t", 1)
            mode, kind, oid = header.decode().split()
            if kind != "blob":
                raise Refused("submodules_not_supported")
            records.append((mode, oid, os.fsdecode(name)))
    return records


def _tracked_state(base, names, records, algorithm):
    expected = {name: (mode, oid) for mode, oid, name in records if Path(name).parts[0] in names}
    directories = {str(p) for name in expected for p in Path(name).parents if str(p) != "."}
    rows = []

    def visit(path):
        name = str(path.relative_to(base))
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            if name not in directories:
                raise Refused("untracked_descendant")
            for child in sorted(path.iterdir()):
                visit(child)
        else:
            if name not in expected:
                raise Refused("untracked_descendant")
            mode, oid = expected[name]
            if mode == "120000" and path.is_symlink():
                content = os.fsencode(os.readlink(path))
            elif mode in {"100644", "100755"} and stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                content = path.read_bytes()
            else:
                raise Refused("tracked_type_or_hardlink_changed")
            blob = b"blob " + str(len(content)).encode() + b"\0" + content
            if hashlib.new(algorithm, blob).hexdigest() != oid:
                raise Refused("tracked_blob_changed")
        rows.append([name, *_identity(path), info.st_size, info.st_mtime_ns, info.st_ctime_ns])

    for name in names:
        visit(base / name)
    return _hash(json.dumps(rows).encode())


def _references(paths):
    """Only path metadata; no session/config/message contents are read."""
    hits = []
    namespaces = set()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            candidates = [os.readlink(proc / name) for name in ("cwd", "exe")]
            candidates += [os.fsdecode(x) for x in (proc / "cmdline").read_bytes().split(b"\0") if x]
            candidates += [
                line.split(maxsplit=5)[5]
                for line in (proc / "maps").read_text().splitlines()
                if len(line.split(maxsplit=5)) == 6
            ]
            candidates += [os.readlink(fd) for fd in (proc / "fd").iterdir()]
            namespace = os.readlink(proc / "ns/mnt")
            if namespace not in namespaces:
                namespaces.add(namespace)
                for line in (proc / "mountinfo").read_text().splitlines():
                    candidates += line.split()[3:5]
            for path in paths:
                if any(value == str(path) or value.startswith(str(path) + "/") for value in candidates):
                    hits.append({"pid": int(proc.name), "path": str(path)})
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            raise Refused("process_reference_inspection_incomplete") from None
    return hits


def build_plan(workspace, release_link, runtime_python):
    workspace = workspace.absolute()
    release_link = release_link.absolute()
    if workspace.resolve() != workspace:
        raise Refused("workspace_symlink")
    _owned(workspace, directory=True)
    _owned(workspace / ".git", directory=True)
    if (workspace / ".git/index.lock").exists():
        raise Refused("index_locked")
    common = Path(os.fsdecode(_git(workspace, "rev-parse", "--git-common-dir")).strip())
    if (workspace / common).resolve() != workspace / ".git":
        raise Refused("main_checkout_required")
    head = _git(workspace, "rev-parse", "HEAD").decode().strip()
    head_ref = _git(workspace, "rev-parse", "--symbolic-full-name", "HEAD").decode().strip()
    refs = _git(workspace, "for-each-ref", "--format=%(refname) %(objectname)").decode().splitlines()
    existing = next((line.split()[1] for line in refs if line.startswith(BRANCH + " ")), None)
    if existing is not None and (head_ref != BRANCH or existing != head):
        raise Refused("ops_branch_already_owned")
    if _git(workspace, "status", "--porcelain", "--untracked-files=no"):
        raise Refused("tracked_changes_present")
    release = _release(release_link, workspace)
    root = Path(release["root"])
    runtime = _runtime(runtime_python, root, workspace)
    records = _records(workspace, head)
    old_names = sorted({Path(row[2]).parts[0] for row in records} - {"data", ".venv"})
    names = sorted(set(old_names) | {name for name in CONTROLS if (root / name).is_file()})
    if any((workspace / name).exists() or (workspace / name).is_symlink() for name in set(names) - set(old_names)):
        raise Refused("new_control_would_overwrite_untracked")
    if not names or any(not (root / name).exists() for name in names):
        raise Refused("release_root_entry_missing")
    algorithm = _git(workspace, "rev-parse", "--show-object-format").decode().strip()
    tracked = _tracked_state(workspace, old_names, records, algorithm)
    venv = workspace / ".venv"
    physical = venv.exists() and not venv.is_symlink()
    if physical:
        _owned(venv, directory=True)
        _owned(venv / "pyvenv.cfg")
    protected = _preserved(workspace, names)
    return {
        "schema": "autostop.manager.runtime-workspace-plan.v1",
        "workspace": str(workspace),
        "old_head": head,
        "old_head_ref": head_ref,
        "old_refs": refs,
        "head_file": (workspace / ".git/HEAD").read_text(),
        "index_sha256": _hash((workspace / ".git/index").read_bytes()),
        "release_link": str(release_link),
        "release": release,
        "runtime_python": str(runtime_python.absolute()),
        "runtime_venv": runtime["venv"],
        "runtime": runtime,
        "old_entries": old_names,
        "entries": names,
        "tracked_fingerprint": tracked,
        "preserved": protected,
        "legacy_venv_identity": _identity(venv) if venv.exists() or venv.is_symlink() else None,
        "remove_legacy_venv_required": physical,
        "active_references": _references([workspace / n for n in [*names, ".venv"]]),
    }


def _private_json(path, payload):
    parent = _owned(path.parent, directory=True)
    if parent.st_mode & 0o077:
        raise Refused("receipt_parent_must_be_private")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_fd(fd, content):
    with os.fdopen(os.dup(fd), "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _lexists(path):
    return path.exists() or path.is_symlink()


class Conversion:
    """Move-only transaction; deletion starts after the verified Git commit."""

    def __init__(self, plan, receipt_dir):
        self.plan = plan
        self.workspace = Path(plan["workspace"])
        self.root = Path(plan["release"]["root"])
        self.gitdir = self.workspace / ".git"
        self.receipt_dir = receipt_dir
        self.index_before = (self.gitdir / "index").read_bytes()
        self.stage = None
        self.locks = {}
        self.moved = []
        self.installed = {}
        self.control_bytes = {}
        self.commit = None
        self.ref_published = False
        self.published_files = {}
        self.converted_verified = False
        self.new_index = None
        self.result = {"ok": False, "workspace": str(self.workspace), "old_head": plan["old_head"]}

    def _lock(self, name, *, key=None):
        path = self.gitdir / (name + ".lock")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.locks[key or name] = (path, fd, os.fstat(fd).st_ino)
        return fd

    def prepare(self):
        self._lock("index")
        self._lock("HEAD")
        if _hash((self.gitdir / "index").read_bytes()) != self.plan["index_sha256"]:
            raise Refused("index_changed")
        self.stage = Path(tempfile.mkdtemp(prefix=".manager-workspace-", dir=self.workspace.parent))
        entries = []
        for name in [*self.plan["entries"], ".venv"]:
            content = (self.root / name).read_bytes() if name in CONTROLS else os.fsencode(self.target(name))
            oid = _git(self.workspace, "hash-object", "-w", "--stdin", data=content).decode().strip()
            entries.append(("100644" if name in CONTROLS else "120000", "blob", oid, name))
        for row in _git(self.workspace, "ls-tree", "-z", self.plan["old_head"]).split(b"\0"):
            if row and row.split(b"\t", 1)[1] == b"data":
                header, name = row.split(b"\t", 1)
                mode, kind, oid = header.decode().split()
                entries.append((mode, kind, oid, os.fsdecode(name)))
        tree = (
            _git(
                self.workspace,
                "mktree",
                "-z",
                data=b"".join(
                    f"{mode} {kind} {oid}\t".encode() + os.fsencode(name) + b"\0"
                    for mode, kind, oid, name in sorted(entries, key=lambda row: os.fsencode(row[3]))
                ),
            )
            .decode()
            .strip()
        )
        self.commit = (
            _git(
                self.workspace,
                "commit-tree",
                tree,
                "-p",
                self.plan["old_head"],
                data=b"Local operational runtime workspace aliases\n",
            )
            .decode()
            .strip()
        )
        _git(self.workspace, "read-tree", self.commit, index=self.stage / "new-index")
        self.new_index = (self.stage / "new-index").read_bytes()

    def target(self, name):
        return self.plan["runtime_venv"] if name == ".venv" else str(Path(self.plan["release_link"]) / name)

    def install_entries(self):
        for name in [*self.plan["entries"], ".venv"]:
            path = self.workspace / name
            if _lexists(path):
                path.rename(self.stage / name)
                self.moved.append(name)
            if name in CONTROLS:
                self.control_bytes[name] = (self.root / name).read_bytes()
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(self.control_bytes[name])
            else:
                path.symlink_to(self.target(name))
            self.installed[name] = _identity(path)

    def check_fences(self):
        if _preserved(self.workspace, self.plan["entries"]) != self.plan["preserved"]:
            raise Refused("preserved_metadata_changed")
        if _release(Path(self.plan["release_link"]), self.workspace) != self.plan["release"]:
            raise Refused("release_changed_during_conversion")
        if _runtime(Path(self.plan["runtime_python"]), self.root, self.workspace) != self.plan["runtime"]:
            raise Refused("runtime_changed_during_conversion")
        if (
            _git(self.workspace, "for-each-ref", "--format=%(refname) %(objectname)").decode().splitlines()
            != self.plan["old_refs"]
        ):
            raise Refused("refs_changed_during_conversion")
        if (
            (self.gitdir / "HEAD").read_text() != self.plan["head_file"]
            or _git(self.workspace, "rev-parse", "HEAD").decode().strip() != self.plan["old_head"]
            or _git(self.workspace, "rev-parse", "--symbolic-full-name", "HEAD").decode().strip()
            != self.plan["old_head_ref"]
        ):
            raise Refused("head_changed_during_conversion")

    def update_ref(self, *args):
        # A detached administrative view avoids Git also locking the real HEAD
        # when updating its attached branch. The shared ref still uses Git CAS.
        with tempfile.TemporaryDirectory(prefix="ref-operation-", dir=self.stage) as directory:
            view = Path(directory)
            (view / "commondir").write_text(str(self.gitdir) + "\n")
            (view / "HEAD").write_text(self.plan["old_head"] + "\n")
            _git(self.workspace, "--git-dir", str(view), "update-ref", *args)

    def publish(self):
        old = self.plan["old_head"] if self.plan["old_head_ref"] == BRANCH else "0" * len(self.plan["old_head"])
        self.update_ref(BRANCH, self.commit, old)
        self.ref_published = True
        for name, content in [("HEAD", ("ref: " + BRANCH + "\n").encode()), ("index", self.new_index)]:
            lock, fd, _ = self.locks[name]
            _write_fd(fd, content)
            os.replace(lock, self.gitdir / name)
            self.published_files[name] = (content, _identity(self.gitdir / name))

    def verify_converted(self):
        if _release(Path(self.plan["release_link"]), self.workspace) != self.plan["release"]:
            raise Refused("release_changed_before_cleanup")
        if _git(self.workspace, "status", "--porcelain", "--untracked-files=no"):
            raise Refused("converted_tracked_state_dirty")
        ignored = (
            _git(
                self.workspace,
                "check-ignore",
                "--no-index",
                "--stdin",
                data=b".env\n.env.local\n.crm-mcp.env\ndata/private-check\n",
            )
            .decode()
            .splitlines()
        )
        if len(ignored) != 4:
            raise Refused("protected_ignore_rules_missing")
        if _preserved(self.workspace, self.plan["entries"]) != self.plan["preserved"]:
            raise Refused("preserved_metadata_changed")
        records = _records(self.workspace, self.plan["old_head"])
        algorithm = _git(self.workspace, "rev-parse", "--show-object-format").decode().strip()
        _tracked_state(self.stage, self.plan["old_entries"], records, algorithm)
        if _references([self.stage / name for name in self.moved]):
            raise Refused("staged_entries_in_use")
        self.result.update(converted=True, new_head=self.commit, branch=BRANCH, preserved_verified=True)
        _private_json(
            self.receipt_dir / "committed.json",
            {**self.result, "staging": str(self.stage), "index_sha256": _hash(self.new_index)},
        )
        self.converted_verified = True

    def cleanup(self):
        for name in self.moved:
            path = self.stage / name
            if path.is_dir() and not path.is_symlink():
                if not shutil.rmtree.avoids_symlink_attacks:
                    raise Refused("safe_rmtree_required")
                shutil.rmtree(path)
            else:
                path.unlink()
        (self.stage / "new-index").unlink()
        self.stage.rmdir()
        self.result.update(ok=True, legacy_venv_removed=self.plan["remove_legacy_venv_required"], staging_removed=True)

    def _ours(self, name):
        path = self.workspace / name
        if not _lexists(path) or _identity(path) != self.installed[name]:
            return False
        if name in CONTROLS:
            return not path.is_symlink() and path.read_bytes() == self.control_bytes[name]
        return path.is_symlink() and os.readlink(path) == self.target(name)

    def _git_rollback_conflicts(self, *, ignore_owned_locks=False):
        conflicts = []
        for name, (content, identity) in self.published_files.items():
            path = self.gitdir / name
            lock = self.gitdir / (name + ".lock")
            own = self.locks.get("restore-" + name) if ignore_owned_locks else None
            try:
                locked = _lexists(lock) and not (own and lock.lstat().st_ino == own[2])
                changed = path.read_bytes() != content or _identity(path) != identity
            except OSError:
                changed, locked = True, True
            if changed or locked:
                conflicts.append(name)
        if (
            self.ref_published
            and _git(self.workspace, "rev-parse", "--verify", "--quiet", BRANCH).decode().strip() != self.commit
        ):
            conflicts.append(BRANCH)
        return conflicts

    def _restore_git(self):
        for name, content in [("HEAD", self.plan["head_file"].encode()), ("index", self.index_before)]:
            if name in self.published_files:
                path, fd, _ = self.locks["restore-" + name]
                _write_fd(fd, content)
                os.replace(path, self.gitdir / name)

    def _rollback_ref(self):
        if self.ref_published:
            if self.plan["old_head_ref"] == BRANCH:
                self.update_ref(BRANCH, self.plan["old_head"], self.commit)
            else:
                self.update_ref("-d", BRANCH, self.commit)

    def rollback(self):
        conflicts = self._git_rollback_conflicts()
        if not conflicts:
            for name in self.published_files:
                self._lock(name, key="restore-" + name)
            conflicts = self._git_rollback_conflicts(ignore_owned_locks=True)
        if not conflicts:
            self._rollback_ref()
            for name in reversed(self.installed):
                if self._ours(name):
                    (self.workspace / name).unlink()
                else:
                    conflicts.append(name)
            for name in reversed(self.moved):
                source = self.stage / name
                if _lexists(source):
                    if _lexists(self.workspace / name):
                        if name not in conflicts:
                            conflicts.append(name)
                    else:
                        source.rename(self.workspace / name)
            self._restore_git()
        if self.stage is not None and not conflicts:
            (self.stage / "new-index").unlink(missing_ok=True)
            try:
                self.stage.rmdir()
            except OSError:
                conflicts.append("staging")
        self.result.update(
            rollback_complete=not conflicts,
            conflicts=conflicts,
            retained_staging=str(self.stage) if self.stage is not None and self.stage.exists() else None,
        )

    def close_locks(self):
        for path, fd, inode in self.locks.values():
            try:
                if path.exists() and (path.lstat().st_dev, path.lstat().st_ino) == (os.fstat(fd).st_dev, inode):
                    path.unlink()
            finally:
                os.close(fd)

    def run(self):
        try:
            self.prepare()
            self.install_entries()
            self.check_fences()
            self.publish()
            self.verify_converted()
            self.cleanup()
        except BaseException as exc:
            if self.converted_verified:
                self.result.update(
                    cleanup_pending=True, retained_staging=str(self.stage), reason="postcommit_cleanup_failed"
                )
            else:
                try:
                    self.rollback()
                except (OSError, Refused) as rollback_exc:
                    self.result.update(
                        rollback_complete=False,
                        rollback_error=type(rollback_exc).__name__,
                        retained_staging=str(self.stage) if self.stage else None,
                    )
                self.result["reason"] = str(exc) if isinstance(exc, Refused) else "conversion_failed"
            self.result["error"] = type(exc).__name__
            raise
        finally:
            self.close_locks()
            _private_json(self.receipt_dir / "after.json", self.result)
        return self.result


def apply_plan(plan, expected_old_head, receipt_dir, *, remove_legacy_venv=False):
    workspace = Path(plan["workspace"])
    fresh = build_plan(workspace, Path(plan["release_link"]), Path(plan["runtime_python"]))
    if plan != fresh or expected_old_head != plan["old_head"]:
        raise Refused("plan_or_expected_head_changed")
    if plan["active_references"]:
        raise Refused("workspace_entries_in_use")
    if plan["remove_legacy_venv_required"] and not remove_legacy_venv:
        raise Refused("legacy_venv_requires_explicit_removal_flag")
    parent = _owned(receipt_dir.parent, directory=True)
    if parent.st_mode & 0o077 or receipt_dir.parent.resolve() != receipt_dir.parent.absolute():
        raise Refused("receipt_parent_must_be_private")
    if any(
        receipt_dir.absolute().is_relative_to(path)
        for path in [workspace, Path(plan["release"]["root"]), Path(plan["runtime_venv"])]
    ):
        raise Refused("receipt_inside_protected_resource")
    receipt_dir.mkdir(mode=0o700)
    _private_json(receipt_dir / "before.json", plan)
    return Conversion(plan, receipt_dir).run()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--workspace", type=Path, default=Path("/opt/AutostopManager"))
    plan.add_argument("--release-link", type=Path, default=Path("/opt/autostop-manager-releases/current"))
    plan.add_argument("--output", type=Path, required=True)
    apply = sub.add_parser("apply")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--expected-old-head", required=True)
    apply.add_argument("--receipt-dir", type=Path, required=True)
    apply.add_argument("--remove-legacy-venv", action="store_true")
    args = parser.parse_args()
    try:
        if args.mode == "plan":
            payload = build_plan(args.workspace, args.release_link, _service_python())
            _private_json(args.output, payload)
            result = {"ok": True, "plan": str(args.output), "plan_sha256": _hash(args.output.read_bytes())}
        else:
            _owned(args.plan)
            result = apply_plan(
                json.loads(args.plan.read_text()),
                args.expected_old_head,
                args.receipt_dir,
                remove_legacy_venv=args.remove_legacy_venv,
            )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, Refused) as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, Refused) else type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
