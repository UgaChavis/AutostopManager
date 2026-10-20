"""Opt-in cold epoch paths and serialized dispatch admission.

A path binding is configuration, never permission. Dispatch additionally needs
the controller's protected admission projection, an open serialized epoch and
the existing operation identity. Private release journals are not exposed to
workers. Nothing here creates state, changes a hold or retries an effect.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import Any
from uuid import UUID


SCHEMA = "autostop.manager.cold-runtime-binding.v1"
NATIVE_ROLES = frozenset({"manager-mcp", "j1", "scheduler", "work-telegram", "personal-telegram", "wake"})
ROLES = NATIVE_ROLES | {"crm-embedded"}
SOURCE_ROOT = Path(__file__).resolve().parents[1]
STATE_ROOT = Path("/var/lib/autostop-crm-forward/state")
BINDING_ENV = "AUTOSTOP_COLD_RUNTIME_BINDING"
SHA_ENV = "AUTOSTOP_COLD_RUNTIME_BINDING_SHA256"
MAX_BYTES = 2 * 1024 * 1024
_fcntl = importlib.import_module("fcntl") if sys.platform == "linux" else None


class ColdRuntimeDenied(RuntimeError):
    """Fixed technical refusal; never echo paths, operation content or secrets."""


def _require(value: Any, code: str) -> None:
    if not value:
        raise ColdRuntimeDenied(code)


def _uuid(value: Any) -> str:
    _require(type(value) is str, "cold_uuid_invalid")
    try:
        _require(str(UUID(value)) == value, "cold_uuid_invalid")
    except ValueError as exc:
        raise ColdRuntimeDenied("cold_uuid_invalid") from exc
    return value


def _fields(value: Any, fields: set[str], code: str) -> dict[str, Any]:
    _require(type(value) is dict and set(value) == fields, code)
    return value


def _sha(value: Any) -> str:
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value), "cold_sha256_invalid")
    return value


def _path(value: Any) -> Path:
    _require(type(value) is str and value.startswith("/") and "\\" not in value, "cold_path_invalid")
    _require(str(PurePosixPath(value)) == value and ".." not in PurePosixPath(value).parts, "cold_path_invalid")
    _require(not any(ord(char) < 32 for char in value), "cold_path_invalid")
    return Path(value)


def _reference(value: Any) -> dict[str, str]:
    result = _fields(value, {"path", "sha256"}, "cold_reference_fields")
    _path(result["path"])
    _sha(result["sha256"])
    return result


def canonical(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def _document(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            _require(key not in result, "cold_duplicate_json_key")
            result[key] = value
        return result

    def nonfinite(_value: str) -> None:
        raise ColdRuntimeDenied("cold_nonfinite_json")

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ColdRuntimeDenied("cold_json_invalid") from exc
    _require(type(value) is dict, "cold_document_required")
    return value


def _open_protected(path: Path) -> int:
    """Walk root-owned nonwritable ancestors by descriptor, without symlinks."""
    _path(str(path))
    parent = os.open("/", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for name in path.parts[1:-1]:
            child = os.open(name, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            os.close(parent)
            parent = child
            info = os.fstat(parent)
            _require(
                stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                "cold_directory_unprotected",
            )
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        try:
            info = os.fstat(descriptor)
            _require(
                stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and not info.st_mode & 0o022,
                "cold_file_unprotected",
            )
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor
    except OSError as exc:
        raise ColdRuntimeDenied("cold_protected_file_unavailable") from exc
    finally:
        os.close(parent)


def _read_protected(path: Path) -> bytes:
    descriptor = _open_protected(path)
    try:
        before = os.fstat(descriptor)
        _require(before.st_size <= MAX_BYTES, "cold_file_too_large")
        chunks = []
        remaining = MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        _require(
            len(raw) <= MAX_BYTES
            and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
            "cold_file_changed_or_oversized",
        )
        return raw
    finally:
        os.close(descriptor)


def _read_ref(reference: Any) -> dict[str, Any]:
    ref = _reference(reference)
    raw = _read_protected(_path(ref["path"]))
    _require(hashlib.sha256(raw).hexdigest() == ref["sha256"], "cold_reference_hash_changed")
    return _document(raw)


def expected_paths(epoch: str, revision: str, role: str) -> dict[str, str]:
    _uuid(epoch)
    _require(type(revision) is str and re.fullmatch(r"[0-9a-f]{40}", revision), "cold_source_revision")
    _require(role in ROLES, "cold_role_invalid")
    etc = f"/etc/autostop-manager-cold/{epoch}/{role}"
    state = f"/var/lib/autostop-manager-cold/{epoch}/{role}"
    run = f"/run/autostop-manager-cold/{epoch}/{role}"
    source = f"/opt/autostop-manager-cold-releases/{revision}/source"
    result = {"source_root": source, "env_file": etc + "/runtime.env"}
    if role in {"manager-mcp", "crm-embedded"}:
        result.update(manager_db=state + "/autostop_manager.sqlite3")
    elif role == "j1":
        result.update(cache_dir=state + "/cache", vin_cache_dir=run + "/vin")
    elif role == "scheduler":
        result.update(registry_db=state + "/registry.sqlite3", socket_path=run + "/control.sock")
    elif role in {"work-telegram", "personal-telegram"}:
        result.update(
            credentials_path=etc + "/credentials",
            session_path=state + "/account",
            state_dir=state,
            socket_path=run + "/bridge.sock",
            model_dir=state + "/models/faster-whisper-small",
        )
        if role == "work-telegram":
            result["owner_config_path"] = etc + "/owner.json"
    else:
        result.update(
            config_path=etc + "/wake.json",
            socket_path=run + "/wake.sock",
            app_socket=run + "/app-server.sock",
            instruction_root=source,
        )
    return result


@dataclass(frozen=True)
class ColdBinding:
    document: dict[str, Any]

    @property
    def role(self) -> str:
        return str(self.document["role"])

    def path(self, role: str, name: str) -> Path:
        paths = expected_paths(self.document["epoch_id"], self.document["source_revision"], role)
        _require(name in paths, "cold_path_key_invalid")
        return Path(paths[name])

    def same_epoch(self, document: Mapping[str, Any]) -> None:
        _require(
            all(
                document.get(name) == self.document[name]
                for name in ("attempt_id", "boot_id", "generation", "epoch_id")
            ),
            "cold_epoch_binding_changed",
        )


def validate_binding_document(document: Any) -> ColdBinding:
    if type(document) is dict and document.get("schema") == "autostop.manager.component-runtime-binding.v1":
        from .component_runtime import validate_binding_document as component_binding

        return component_binding(document)
    value = _fields(
        document,
        {
            "schema",
            "source_revision",
            "attempt_id",
            "boot_id",
            "generation",
            "epoch_id",
            "role",
            "paths",
            "tombstones_ref",
            "ingress_fence_ref",
        },
        "cold_binding_fields",
    )
    _require(value["schema"] == SCHEMA, "cold_binding_schema")
    _require(
        type(value["source_revision"]) is str and re.fullmatch(r"[0-9a-f]{40}", value["source_revision"]),
        "cold_source_revision",
    )
    for name in ("attempt_id", "boot_id", "generation", "epoch_id"):
        _uuid(value[name])
    _require(value["generation"] == value["epoch_id"], "cold_epoch_generation")
    role = value["role"]
    _require(type(role) is str and role in ROLES, "cold_role_invalid")
    _require(
        type(value["paths"]) is dict
        and value["paths"] == expected_paths(value["epoch_id"], value["source_revision"], role),
        "cold_paths_not_exact",
    )
    shared = f"/etc/autostop-manager-cold/{value['epoch_id']}/dispatch"
    for field, name in (("tombstones_ref", "tombstones.json"), ("ingress_fence_ref", "fence.json")):
        ref = _reference(value[field])
        _require(ref["path"] == shared + "/" + name, "cold_shared_reference_path")
    return ColdBinding(json.loads(json.dumps(value)))


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _load_binding() -> ColdBinding | None:
    path, digest = os.environ.get(BINDING_ENV), os.environ.get(SHA_ENV)
    if path is None and digest is None:
        _require(
            not str(SOURCE_ROOT).startswith("/opt/autostop-manager-cold-releases/"),
            "cold_binding_required_for_cold_source",
        )
        return None
    _require(sys.platform == "linux", "cold_linux_required")
    _require(bool(path) and bool(digest), "cold_binding_environment_incomplete")
    _sha(digest)
    raw = _read_protected(_path(path))
    _require(hashlib.sha256(raw).hexdigest() == digest, "cold_binding_hash_changed")
    result = validate_binding_document(_document(raw))
    expected = f"/etc/autostop-manager-cold/{result.document['epoch_id']}/{result.role}/binding.json"
    _require(path == expected, "cold_binding_path_not_exact")
    source = result.path(result.role, "source_root")
    _require(SOURCE_ROOT == source, "cold_source_root_changed")
    _require(
        _read_protected(source / "REVISION").decode().strip() == result.document["source_revision"],
        "cold_source_revision_changed",
    )
    _require(_boot_id() == result.document["boot_id"], "cold_boot_changed")
    return result


def binding() -> ColdBinding | None:
    current = _load_binding()
    if current is not None and current.document["schema"] != SCHEMA:
        from .component_runtime import validate_process_claim

        validate_process_claim(current)
    return current


def claim_runtime_start() -> ColdBinding | None:
    """Check a controller-owned one-shot claim before importing the consumer."""
    current = _load_binding()
    if current is not None and current.document["schema"] != SCHEMA:
        from .component_runtime import starting

        with starting(current):
            pass
    return current


def runtime_path(role: str, name: str, legacy: str | Path) -> Path:
    current = binding()
    return current.path(role, name) if current is not None else Path(legacy)


def require_role(role: str) -> None:
    current = binding()
    _require(current is None or current.role == role, "cold_entrypoint_role_changed")


def configured_path(environment: str, role: str, name: str, legacy: str | Path) -> Path:
    current = binding()
    if current is None:
        return Path(os.environ.get(environment, str(legacy)))
    expected = current.path(role, name)
    _require(
        environment not in os.environ or os.environ[environment] == str(expected), "cold_environment_path_conflict"
    )
    return expected


def _lock_link(path: str) -> os.stat_result:
    return os.stat(path, follow_symlinks=False)


def _committed(current: ColdBinding, state: dict[str, Any]) -> None:
    plan_ref, commit_ref = _reference(state["plan_ref"]), _reference(state["commit_ref"])
    attempt = current.document["attempt_id"]
    proof_root = f"/var/lib/autostop-manager-cold/{current.document['epoch_id']}/dispatch/proofs"
    admission_ref = _reference(state["admission_ref"])
    _require(admission_ref["path"] == proof_root + "/admission.json", "cold_admission_path")
    admission = _read_ref(admission_ref)
    _fields(
        admission,
        {"schema", "attempt_id", "boot_id", "generation", "epoch_id", "source_refs", "projected_refs"},
        "cold_admission_fields",
    )
    _require(admission["schema"] == "autostop.j1.cold-dispatch-admission.v1", "cold_admission_schema")
    current.same_epoch(admission)
    source_refs = _fields(admission["source_refs"], {"plan", "commit", "head", "epoch"}, "cold_admission_source_fields")
    projected_refs = _fields(
        admission["projected_refs"], {"plan", "commit", "head", "epoch"}, "cold_admission_projected_fields"
    )
    projected = {}
    for name in ("plan", "commit", "head", "epoch"):
        source_ref, projected_ref = _reference(source_refs[name]), _reference(projected_refs[name])
        _require(
            projected_ref["path"] == proof_root + f"/{name}.json" and projected_ref["sha256"] == source_ref["sha256"],
            "cold_projection_reference",
        )
        document = _read_ref(projected_ref)
        _require(
            hashlib.sha256(canonical(document)).hexdigest() == projected_ref["sha256"], "cold_projection_not_canonical"
        )
        projected[name] = document
    _require(
        source_refs["plan"] == plan_ref
        and source_refs["commit"] == commit_ref
        and source_refs["head"]["path"] == str(STATE_ROOT / "HEAD.json"),
        "cold_admission_source_binding",
    )
    _require(plan_ref["path"] == f"/var/lib/autostop-crm-forward/attempts/{attempt}/plan.json", "cold_plan_path")
    plan = projected["plan"]
    _require(
        plan.get("schema") == "autostop.j1.crm-manager-cold-forward-plan.v1"
        and plan.get("attempt_id") == attempt
        and plan.get("candidate_manager_sha") == current.document["source_revision"],
        "cold_committed_plan_binding",
    )
    commit = projected["commit"]
    _require(
        commit.get("schema") in {"autostop.j1.crm-forward-commit.v1", "autostop.j1.crm-forward-commit.v2"}
        and commit.get("plan") == plan,
        "cold_commit_record_binding",
    )
    _require(hashlib.sha256(canonical(commit["plan"])).hexdigest() == plan_ref["sha256"], "cold_commit_plan_sha256")
    sequence = commit.get("sequence")
    _require(
        type(sequence) is int
        and sequence > 0
        and commit_ref["path"] == str(STATE_ROOT / "history" / f"{sequence:020d}.json"),
        "cold_commit_record_path",
    )
    head = projected["head"]
    _require(
        type(head.get("sequence")) is int
        and head == {"schema": "autostop.j1.crm-forward-head.v1", "sequence": sequence, "sha256": commit_ref["sha256"]},
        "cold_commit_not_current_head",
    )
    _require(source_refs["epoch"] == plan.get("dispatch_epoch_ref"), "cold_admission_epoch_binding")
    epoch = projected["epoch"]
    current.same_epoch(epoch)
    _require(epoch.get("schema") == "autostop.j1.cold-dispatch-epoch.v1", "cold_dispatch_epoch_schema")
    refs = epoch["runtime_bindings"]
    _require(type(refs) is dict and set(refs) == ROLES, "cold_dispatch_epoch_roles")
    for role, ref in refs.items():
        _reference(ref)
        _require(
            ref["path"] == f"/etc/autostop-manager-cold/{current.document['epoch_id']}/{role}/binding.json",
            "cold_dispatch_epoch_manifest_path",
        )
    _require(
        refs[current.role] == {"path": os.environ[BINDING_ENV], "sha256": os.environ[SHA_ENV]},
        "cold_dispatch_epoch_manifest",
    )


def _blocked_operation_ids(tombstones: Any, current: ColdBinding) -> set[str]:
    _fields(
        tombstones,
        {"schema", "attempt_id", "boot_id", "generation", "epoch_id", "origin_attempt_id", "rows"},
        "cold_tombstones_fields",
    )
    current.same_epoch(tombstones)
    version = tombstones["schema"]
    _require(
        type(version) is str
        and version in {"autostop.j1.cold-dispatch-tombstones.v1", "autostop.j1.cold-dispatch-tombstones.v2"},
        "cold_tombstones_schema",
    )
    legacy = version == "autostop.j1.cold-dispatch-tombstones.v1"
    _uuid(tombstones["origin_attempt_id"])
    rows = tombstones["rows"]
    _require(type(rows) is list and len(rows) <= 8192, "cold_tombstones_limit")
    seen: set[str] = set()
    for row in rows:
        fingerprint_field = "payload_sha256" if legacy else "fingerprint"
        _fields(row, {"op_id", fingerprint_field, "outcome", "retry"}, "cold_tombstone_fields")
        _require(
            type(row["op_id"]) is str and 1 <= len(row["op_id"]) <= 200 and row["op_id"] not in seen,
            "cold_tombstone_identity",
        )
        seen.add(row["op_id"])
        if legacy:
            _sha(row["payload_sha256"])
        else:
            fingerprint = _fields(row["fingerprint"], {"kind", "sha256"}, "cold_tombstone_fingerprint_fields")
            _require(
                type(fingerprint["kind"]) is str
                and fingerprint["kind"] in {"payload_sha256", "observed_record_sha256"},
                "cold_tombstone_fingerprint_kind",
            )
            _sha(fingerprint["sha256"])
            _require(
                fingerprint["kind"] != "observed_record_sha256" or row["outcome"] == "unknown",
                "cold_tombstone_observed_outcome",
            )
        _require(
            row["outcome"] in {"unknown", "known_not_applied", "applied", "needs_review"}
            and row["retry"] == "forbidden",
            "cold_tombstone_outcome",
        )
    return seen


@contextmanager
def _dispatch_state(current: ColdBinding, *, exclusive: bool = False) -> Iterator[dict[str, Any]]:
    """Read the current state while holding the same protected dispatch inode."""
    fence = _read_ref(current.document["ingress_fence_ref"])
    _fields(
        fence,
        {
            "schema",
            "attempt_id",
            "boot_id",
            "generation",
            "epoch_id",
            "state_path",
            "dispatch_lock_path",
            "dispatch_lock_identity",
        },
        "cold_fence_fields",
    )
    current.same_epoch(fence)
    _require(fence["schema"] == "autostop.j1.cold-dispatch-fence.v1", "cold_fence_schema")
    root = f"/var/lib/autostop-manager-cold/{current.document['epoch_id']}/dispatch"
    _require(
        fence["state_path"] == root + "/state.json" and fence["dispatch_lock_path"] == root + "/dispatch.lock",
        "cold_fence_paths",
    )
    identity = _fields(fence["dispatch_lock_identity"], {"device", "inode", "ctime_ns"}, "cold_lock_identity_fields")
    _require(
        type(identity["device"]) is int
        and identity["device"] >= 0
        and type(identity["inode"]) is int
        and identity["inode"] > 0,
        "cold_lock_identity_invalid",
    )
    _require(type(identity["ctime_ns"]) is int and identity["ctime_ns"] > 0, "cold_lock_identity_invalid")
    descriptor = _open_protected(Path(fence["dispatch_lock_path"]))
    try:
        info = os.fstat(descriptor)
        _require(
            (info.st_dev, info.st_ino, info.st_ctime_ns)
            == (identity["device"], identity["inode"], identity["ctime_ns"]),
            "cold_lock_replaced",
        )
        try:
            flock_module = _fcntl
            if flock_module is None:
                raise ColdRuntimeDenied("cold_linux_required")
            operation = flock_module.LOCK_EX if exclusive else flock_module.LOCK_SH
            flock_module.flock(descriptor, operation | flock_module.LOCK_NB)
        except BlockingIOError as exc:
            raise ColdRuntimeDenied("cold_dispatch_serialization_busy") from exc
        linked = _lock_link(fence["dispatch_lock_path"])
        _require(
            (linked.st_dev, linked.st_ino, linked.st_ctime_ns) == (info.st_dev, info.st_ino, info.st_ctime_ns),
            "cold_lock_replaced",
        )
        state = _document(_read_protected(Path(fence["state_path"])))
        current.same_epoch(state)
        yield state
    finally:
        os.close(descriptor)


@contextmanager
def dispatch_guard(
    operation_id: Any, channel: str, payload_sha256: str | None = None, *, operation: str | None = None
) -> Iterator[None]:
    current = binding()
    if current is None:
        yield
        return
    _require(
        type(operation_id) is str and 1 <= len(operation_id) <= 200 and not any(ord(c) < 32 for c in operation_id),
        "cold_dispatch_existing_identity_required",
    )
    channels = {
        "manager-store": {"manager-mcp", "crm-embedded"},
        "manager-store-owner": {"manager-mcp", "crm-embedded"},
        "manager-crm": {"manager-mcp", "crm-embedded"},
        "j1": {"j1"},
        "scheduler": {"scheduler"},
        "scheduler-outbox": {"scheduler"},
        "work-telegram": {"work-telegram"},
        "personal-telegram": {"personal-telegram"},
        "wake": {"wake"},
    }
    _require(current.role in channels.get(channel, set()), "cold_dispatch_channel_role")
    if payload_sha256 is not None:
        _sha(payload_sha256)
    with _dispatch_state(current) as state:
        if current.document["schema"] != SCHEMA:
            from .component_runtime import admit_effect

            admit_effect(current, state, channel, operation)
            seen = _blocked_operation_ids(_read_ref(current.document["tombstones_ref"]), current)
            _require(operation_id not in seen, "cold_old_operation_forbidden")
            yield
            return
        _fields(
            state,
            {
                "schema",
                "attempt_id",
                "boot_id",
                "generation",
                "epoch_id",
                "state",
                "commit_ref",
                "plan_ref",
                "admission_ref",
            },
            "cold_dispatch_state_fields",
        )
        current.same_epoch(state)
        _require(
            state["schema"] == "autostop.j1.cold-dispatch-state.v1" and state["state"] == "open", "cold_dispatch_closed"
        )
        _committed(current, state)
        seen = _blocked_operation_ids(_read_ref(current.document["tombstones_ref"]), current)
        _require(operation_id not in seen, "cold_old_operation_forbidden")
        yield


def guarded_effect(channel: str, identity: Any, *, applies: Any = None, operation: Any = None) -> Any:
    """Keep the epoch's shared lock through a synchronous or async consumer.

    Callers select an existing domain identity from bound arguments. This helper
    neither generates an identity nor retries a denied or uncertain invocation.
    """

    def decorate(function: Any) -> Any:
        signature = inspect.signature(function)

        def guard(args: Any, kwargs: Any) -> Any:
            current = binding()
            if current is None:
                from contextlib import nullcontext

                return nullcontext()
            arguments = signature.bind(*args, **kwargs)
            arguments.apply_defaults()
            enabled = applies is None or applies(arguments.arguments)
            if not enabled:
                from contextlib import nullcontext

                return nullcontext()
            resolved = current.role if channel == "telegram" else channel
            return dispatch_guard(
                identity(arguments.arguments),
                resolved,
                operation=operation(arguments.arguments) if operation is not None else None,
            )

        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def asynchronous(*args: Any, **kwargs: Any) -> Any:
                with guard(args, kwargs):
                    return await function(*args, **kwargs)

            return asynchronous

        @wraps(function)
        def synchronous(*args: Any, **kwargs: Any) -> Any:
            with guard(args, kwargs):
                return function(*args, **kwargs)

        return synchronous

    return decorate
