"""Fixed root-only native-role claim. No application import before qualification.

The caller is the reviewed component controller's fixed systemd unit. A launch
reservation is consumed once; any crash/unknown leaves its durable ledger entry.
This is not an installer, permission issuer or arbitrary command launcher.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import time
from uuid import UUID


IDENTITIES = {
    "manager-mcp": (0, 0, []),
    "j1": (0, 0, []),
    "scheduler": (0, 10001, [10001]),
    "work-telegram": (987, 982, [982, 10001]),
    "personal-telegram": (993, 985, [985]),
    "wake": (0, 982, [982]),
}
FORBIDDEN_ENV = {"LD_PRELOAD", "LD_AUDIT", "LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME", "BASH_ENV", "ENV"}
MAX_SOURCE_BYTES = 64 * 1024 * 1024


def require(value, code):
    if not value:
        raise RuntimeError("component_bootstrap_" + code)


def version(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
    )


def parent_fd(path):
    require(path.is_absolute() and str(PurePosixPath(str(path))) == str(path) and ".." not in path.parts, "path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for name in path.parts[1:-1]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022, "directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


def protected_bytes(path, limit=2 * 1024 * 1024, *, with_version=False):
    parent = parent_fd(path)
    fd = None
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        before = os.fstat(fd)
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == 0
            and before.st_nlink == 1
            and not before.st_mode & 0o022
            and before.st_size <= limit,
            "file",
        )
        chunks, size = [], 0
        while chunk := os.read(fd, min(65536, limit + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            require(size <= limit, "file_limit")
        require(
            version(before)
            == version(os.fstat(fd))
            == version(os.stat(path.name, dir_fd=parent, follow_symlinks=False)),
            "file_changed",
        )
        raw = b"".join(chunks)
        return (raw, version(before)) if with_version else raw
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def document(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    def nonfinite(_value):
        raise RuntimeError("component_bootstrap_nonfinite_json")

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
    require(type(value) is dict, "document")
    return value


def source_root():
    script = Path(__file__).absolute()
    root = script.parents[1]
    require(
        script == script.resolve()
        and re.fullmatch(r"/opt/autostop-manager-cold-releases/[0-9a-f]{40}/source", str(root)),
        "source",
    )
    return root


def qualified_source(source, reference, revision):
    require(
        type(reference) is dict
        and set(reference) == {"path", "sha256"}
        and reference["path"] == str(source / "MANIFEST.sha256"),
        "source_manifest",
    )
    raw, manifest_version = protected_bytes(source / "MANIFEST.sha256", with_version=True)
    require(hashlib.sha256(raw).hexdigest() == reference["sha256"], "source_manifest_hash")
    rows = {}
    for line in raw.decode("utf-8").splitlines():
        found = re.fullmatch(r"([a-f0-9]{64})  (.+)", line)
        require(found, "manifest_line")
        name = found[2]
        path = PurePosixPath(name)
        require(
            not path.is_absolute()
            and ".." not in path.parts
            and str(path) == name
            and "\\" not in name
            and not any(ord(char) < 32 for char in name)
            and name not in rows
            and name != "MANIFEST.sha256",
            "manifest_path",
        )
        rows[name] = found[1]
    require(
        0 < len(rows) <= 2048
        and "REVISION" in rows
        and "scripts/component-role-bootstrap.py" in rows
        and "scripts/cold-role-start.py" in rows,
        "manifest_inventory",
    )
    actual, directories = set(), {}
    for count, path in enumerate(source.rglob("*"), 1):
        require(count <= 8192, "source_entries")
        info = path.lstat()
        require(not stat.S_ISLNK(info.st_mode), "source_symlink")
        if stat.S_ISREG(info.st_mode):
            actual.add(str(path.relative_to(source)))
        else:
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022, "source_special")
            directories[str(path.relative_to(source))] = version(info)
    require(actual == set(rows) | {"MANIFEST.sha256"}, "source_inventory_changed")
    total, versions = 0, {}
    for name, digest in rows.items():
        data, observed = protected_bytes(source / name, MAX_SOURCE_BYTES - total, with_version=True)
        total += len(data)
        require(total <= MAX_SOURCE_BYTES and hashlib.sha256(data).hexdigest() == digest, "source_hash")
        versions[name] = observed
    require(protected_bytes(source / "REVISION") == (revision + "\n").encode(), "revision")
    versions["MANIFEST.sha256"] = manifest_version
    return {"files": versions, "directories": directories}


def recheck_source(source, versions):
    actual, directories = {}, {}
    for count, path in enumerate(source.rglob("*"), 1):
        require(count <= 8192, "source_entries")
        info = path.lstat()
        require(not stat.S_ISLNK(info.st_mode), "source_symlink")
        if stat.S_ISREG(info.st_mode):
            actual[str(path.relative_to(source))] = version(info)
        else:
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022, "source_special")
            directories[str(path.relative_to(source))] = version(info)
    require(actual == versions["files"] and directories == versions["directories"], "source_changed_after_claim")


def write_once(path, raw, *, gid=0, mode=0o600):
    parent = parent_fd(path)
    fd = None
    try:
        fd = os.open(
            path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=parent
        )
        os.fchown(fd, 0, gid)
        os.fchmod(fd, mode)
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            require(written > 0, "short_write")
            view = view[written:]
        os.fsync(fd)
        os.fsync(parent)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def not_revoked(root):
    try:
        (root / "revoked.json").lstat()
    except FileNotFoundError:
        return
    require(False, "revoked")


def context(launch, source, component, cold):
    require(os.geteuid() == 0 and os.getegid() == 0, "root_required")
    require(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode, "interpreter_flags")
    require(not FORBIDDEN_ENV.intersection(os.environ), "inherited_loader_environment")
    require(cold._boot_id() == launch["boot_id"], "boot_changed")
    require(launch["source_revision"] == source.parent.name, "source_revision")
    role = launch["role"]
    expected = f"/system.slice/autostop-manager-component-{launch['generation']}-{role}.service"
    require(Path("/proc/self/cgroup").read_text().strip() == "0::" + expected, "unit_context")
    uid, gid, groups = IDENTITIES[role]
    identity = launch["identity"]
    require(
        type(identity) is dict
        and set(identity) == {"uid", "gid", "supplementary_groups"}
        and type(identity["uid"]) is int
        and type(identity["gid"]) is int
        and type(identity["supplementary_groups"]) is list
        and all(type(group) is int for group in identity["supplementary_groups"])
        and identity == {"uid": uid, "gid": gid, "supplementary_groups": groups},
        "role_identity",
    )
    require(launch["not_before_ns"] <= time.monotonic_ns() < launch["expires_at_ns"], "expired")
    executable, physical, actual = component.runtime_identity(Path(launch["runtime"]["path"]))
    require(
        executable == physical == Path(launch["runtime"]["path"]) and actual == launch["runtime"]["version"],
        "runtime_identity",
    )
    require(
        hashlib.sha256(protected_bytes(physical, 64 * 1024 * 1024)).hexdigest() == launch["runtime"]["sha256"],
        "runtime_hash",
    )


def claim(launch, launch_ref, source, component, cold):
    template = launch["binding_template"]
    require(type(template) is dict and "claim_ref" not in template, "binding_template")
    for key in (
        "mode",
        "source_revision",
        "attempt_id",
        "boot_id",
        "generation",
        "epoch_id",
        "role",
        "instance_id",
        "runtime",
        "not_before_ns",
        "expires_at_ns",
    ):
        require(template.get(key) == launch[key], "binding_template_changed")
    role, epoch = launch["role"], launch["epoch_id"]
    claim_path = f"/etc/autostop-manager-cold/{epoch}/{role}/claim.json"
    binding_path = f"/etc/autostop-manager-cold/{epoch}/{role}/binding.json"
    identities = {key: launch[key] for key in ("mode", "attempt_id", "boot_id", "generation", "epoch_id")}
    claim_document = {
        "schema": "autostop.manager.component-claim.v1",
        **identities,
        **{key: launch[key] for key in ("source_revision", "role", "instance_id", "runtime")},
        **component.process_identity(),
        "claimed_ns": time.monotonic_ns(),
    }
    claim_raw = cold.canonical(claim_document)
    claim_ref = {"path": claim_path, "sha256": hashlib.sha256(claim_raw).hexdigest()}
    binding_document = {**template, "claim_ref": claim_ref}
    current = cold.validate_binding_document(binding_document)
    binding_raw = cold.canonical(binding_document)
    binding_ref = {"path": binding_path, "sha256": hashlib.sha256(binding_raw).hexdigest()}
    root = Path(component.control_root(current))
    with cold._dispatch_state(current, exclusive=True) as state:
        not_revoked(root)
        component.ensure_not_revoked(current)
        context(launch, source, component, cold)
        component._typed(
            state, current, "dispatch-state", {"state", "admission_ref", "commit_ref", "store_effects_ref"}
        )
        require(
            state["state"] == "closed" and state["commit_ref"] is None and state["store_effects_ref"] is None,
            "claim_state",
        )
        # Consume before either public file: an interrupted publication stays unknown.
        ledger = {
            "schema": "autostop.manager.component-launch-claim.v1",
            **identities,
            "role": role,
            "instance_id": launch["instance_id"],
            "launch_ref": launch_ref,
            "claim_ref": claim_ref,
            "binding_ref": binding_ref,
            **component.process_identity(),
        }
        write_once(root / "claims" / (role + ".json"), cold.canonical(ledger))
        write_once(Path(claim_path), claim_raw, gid=IDENTITIES[role][1], mode=0o640)
        write_once(Path(binding_path), binding_raw, gid=IDENTITIES[role][1], mode=0o640)
        require(launch["not_before_ns"] <= time.monotonic_ns() < launch["expires_at_ns"], "claim_expired")
    return current, binding_ref


def execute(current, binding_ref, source_versions, component, cold):
    root = Path(component.control_root(current))
    # Parent publishes the exact six/seven actual binding hashes after claims.
    # Never hold the dispatch lock while waiting for that publication.
    while True:
        not_revoked(root)
        require(
            current.document["not_before_ns"] <= time.monotonic_ns() < current.document["expires_at_ns"], "wait_expired"
        )
        with cold._dispatch_state(current) as state:
            require(state.get("state") == "closed" and state.get("commit_ref") is None, "wait_state")
            ready = state.get("admission_ref") is not None
        if ready:
            break
        time.sleep(0.025)
    uid, gid, groups = IDENTITIES[current.role]
    role_state = f"/var/lib/autostop-manager-cold/{current.document['epoch_id']}/{current.role}"
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": role_state,
        "XDG_CACHE_HOME": role_state + "/xdg-cache",
        "XDG_CONFIG_HOME": role_state + "/xdg-config",
        "CODEX_HOME": role_state + "/codex",
        cold.BINDING_ENV: binding_ref["path"],
        cold.SHA_ENV: binding_ref["sha256"],
        "AUTOSTOP_MANAGER_ENV_FILE": current.document["paths"]["env_file"],
    }
    os.environ.clear()
    os.environ.update(environment)
    with cold._dispatch_state(current) as state:
        component.admission(current, state)
        require(state["state"] == "closed" and state["commit_ref"] is None, "start_state")
        not_revoked(root)
        recheck_source(Path(current.document["paths"]["source_root"]), source_versions)
        os.chdir(current.document["paths"]["source_root"])
        os.umask(0o077)
        os.setgroups(groups)
        os.setgid(gid)
        os.setuid(uid)
        require((os.geteuid(), os.getegid(), sorted(os.getgroups())) == (uid, gid, groups), "identity_drop")
        component.validate_process_claim(current, starting=True)
    runtime = current.document["runtime"]["path"]
    os.execve(
        runtime,
        [
            runtime,
            "-I",
            "-B",
            str(Path(current.document["paths"]["source_root"]) / "scripts/cold-role-start.py"),
            "--role",
            current.role,
        ],
        environment,
    )
    raise RuntimeError("component_bootstrap_exec_returned")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=tuple(IDENTITIES), required=True)
    parser.add_argument("--launch", required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args(argv)
    require(re.fullmatch(r"[a-f0-9]{64}", args.sha256), "launch_hash")
    path = Path(args.launch)
    require(
        re.fullmatch(r"/var/lib/autostop-component-release/[a-f0-9-]{36}/launch/" + args.role + r"\.json", str(path)),
        "launch_path",
    )
    attempt = path.parents[1].name
    require(str(UUID(attempt)) == attempt, "attempt")
    raw = protected_bytes(path)
    require(hashlib.sha256(raw).hexdigest() == args.sha256, "launch_hash")
    launch = document(raw)
    fields = {
        "schema",
        "mode",
        "source_revision",
        "attempt_id",
        "boot_id",
        "generation",
        "epoch_id",
        "role",
        "instance_id",
        "runtime",
        "identity",
        "source_manifest_ref",
        "not_before_ns",
        "expires_at_ns",
        "binding_template",
    }
    require(
        set(launch) == fields
        and launch["schema"] == "autostop.manager.component-launch.v1"
        and launch["attempt_id"] == attempt
        and launch["role"] == args.role,
        "launch_document",
    )
    source = source_root()
    source_versions = qualified_source(source, launch["source_manifest_ref"], launch["source_revision"])
    sys.path.insert(0, str(source))
    cold = importlib.import_module("autostop_manager.cold_runtime")
    component = importlib.import_module("autostop_manager.component_runtime")
    require(component.NATIVE_IDENTITIES == IDENTITIES, "identity_policy_changed")
    context(launch, source, component, cold)
    current, binding_ref = claim(launch, {"path": str(path), "sha256": args.sha256}, source, component, cold)
    execute(current, binding_ref, source_versions, component, cold)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, TypeError):
        print("component_bootstrap_refused", file=sys.stderr)
        raise SystemExit(1) from None
