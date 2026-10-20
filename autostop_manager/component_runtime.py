"""Two fixed component admissions; protected configuration never issues a grant.

The controller owns the one-shot process claim and all authority publication.
Workers only read its protected projection. This module neither retires an old
process nor changes a hold, starts a child, repairs scopes or retries an effect.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any

from . import cold_runtime as cold


SCHEMA = "autostop.manager.component-runtime-binding.v1"
EXTERNAL = "manager_external"
CRM = "crm"
MODE_ROLES = {EXTERNAL: cold.NATIVE_ROLES, CRM: cold.ROLES}
MAX_WINDOW_NS = 600_000_000_000
EMBEDDED_REVISION = "89af29082e0a6f72c5d85ae448acb6d4deec5d94"
PAYMENT_SCOPE = "store:write:order_payment"
STORE_REVISION = "54eecff7243576a18a47759085323794783d1f9e"
MANAGE_PRINCIPAL = "00000000-0000-4000-8000-000000000102"
NATIVE_IDENTITIES = {
    "manager-mcp": (0, 0, []),
    "j1": (0, 0, []),
    "scheduler": (0, 10001, [10001]),
    "work-telegram": (987, 982, [982, 10001]),
    "personal-telegram": (993, 985, [985]),
    "wake": (0, 982, [982]),
}
HEADER = {"schema", "mode", "attempt_id", "boot_id", "generation", "epoch_id"}
BINDING_EXTRA = {"mode", "instance_id", "runtime", "claim_ref", "not_before_ns", "expires_at_ns"}
MODE_EXTRA = {EXTERNAL: {"retained_crm_ref"}, CRM: {"external_commit_ref", "candidate_crm_sha"}}
ORIGINAL_UNITS = frozenset(
    {
        "autostop-manager-mcp.service",
        "autostop-j1.service",
        "autostop-manager-scheduler.service",
        "autostop-work-telegram.service",
        "autostop-telegram.service",
        "autostop-codex-wake.service",
        "autostop-codex-start.service",
        "autostop-j1-browser.service",
    }
)


def _require(value: Any, code: str) -> None:
    cold._require(value, "component_" + code)


def _integer(value: Any, *, positive: bool = False) -> int:
    _require(type(value) is int and value >= int(positive), "integer_invalid")
    return value


def _revision(value: Any) -> str:
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{40}", value), "revision_invalid")
    return value


def control_root(current: cold.ColdBinding) -> str:
    return f"/var/lib/autostop-component-release/{current.document['attempt_id']}"


def proof_root(current: cold.ColdBinding) -> str:
    return f"/var/lib/autostop-manager-cold/{current.document['epoch_id']}/dispatch/proofs"


def _typed(document: Any, current: cold.ColdBinding, kind: str, fields: set[str]) -> dict[str, Any]:
    value = cold._fields(document, HEADER | fields, "component_" + kind + "_fields")
    current.same_epoch(value)
    _require(value["schema"] == f"autostop.manager.component-{kind}.v1", kind + "_schema")
    _require(value["mode"] == current.document["mode"], "mode_changed")
    return value


def _read(reference: Any, expected: str) -> dict[str, Any]:
    ref = cold._reference(reference)
    _require(ref["path"] == expected, "reference_path")
    value = cold._read_ref(ref)
    _require(hashlib.sha256(cold.canonical(value)).hexdigest() == ref["sha256"], "reference_not_canonical")
    return value


def _window(value: dict[str, Any]) -> None:
    start, end = _integer(value["not_before_ns"]), _integer(value["expires_at_ns"])
    _require(0 < end - start <= MAX_WINDOW_NS, "window_invalid")


def validate_binding_document(document: Any) -> cold.ColdBinding:
    _require(
        type(document) is dict and type(document.get("mode")) is str and document["mode"] in MODE_ROLES, "mode_invalid"
    )
    mode = document["mode"]
    extra = BINDING_EXTRA | MODE_EXTRA[mode]
    cold._fields(
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
        }
        | extra,
        "component_binding_fields",
    )
    base = {key: value for key, value in document.items() if key not in extra}
    _require(base.get("schema") == SCHEMA, "binding_schema")
    base["schema"] = cold.SCHEMA
    accepted = cold.validate_binding_document(base)
    _require(accepted.role in MODE_ROLES[mode], "role_invalid")
    cold._uuid(document["instance_id"])
    _window(document)
    runtime = cold._fields(document["runtime"], {"path", "sha256", "version"}, "component_runtime_fields")
    path = cold._path(runtime["path"])
    cold._sha(runtime["sha256"])
    version = runtime["version"]
    _require(type(version) is list and len(version) == 9, "runtime_version")
    for item in version:
        _integer(item)
    _require(
        stat.S_ISREG(version[5]) and not version[5] & 0o022 and version[6] == 0 and version[8] == 1,
        "runtime_unprotected",
    )
    if accepted.role in cold.NATIVE_ROLES:
        _require(path == Path(document["paths"]["source_root"]).parent / "venv/bin/python3.12", "runtime_path")
    current = cold.ColdBinding({**accepted.document, **{key: document[key] for key in extra}, "schema": SCHEMA})
    _require(
        cold._reference(document["claim_ref"])["path"]
        == f"/etc/autostop-manager-cold/{document['epoch_id']}/{current.role}/claim.json",
        "claim_path",
    )
    if mode == EXTERNAL:
        _require(
            cold._reference(document["retained_crm_ref"])["path"] == control_root(current) + "/retained-crm.json",
            "retained_crm_path",
        )
    else:
        _revision(document["candidate_crm_sha"])
        reference = cold._reference(document["external_commit_ref"])
        _require(
            re.fullmatch(r"/var/lib/autostop-component-release/[0-9a-f-]{36}/commit.json", reference["path"]),
            "external_commit_path",
        )
        cold._uuid(Path(reference["path"]).parent.name)
        _require(Path(reference["path"]).parent.name != document["attempt_id"], "external_commit_cycle")
    return cold.ColdBinding(json.loads(json.dumps(current.document)))


def _version(info: os.stat_result) -> list[int]:
    return [
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
    ]


def process_identity() -> dict[str, int]:
    # Only our own process; namespace identity distinguishes container PID 1.
    fields = Path("/proc/self/stat").read_text().rsplit(") ", 1)[1].split()
    return {"pid": os.getpid(), "start_ticks": int(fields[19]), "pid_namespace": os.stat("/proc/self/ns/pid").st_ino}


def runtime_identity(path: Path) -> tuple[Path, Path, list[int]]:
    descriptor = cold._open_protected(path)
    try:
        before = _version(os.fstat(descriptor))
        result = (Path(sys.executable).resolve(), Path("/proc/self/exe").resolve(), before)
        _require(
            before == _version(os.fstat(descriptor)) == _version(os.stat(path, follow_symlinks=False)),
            "runtime_changed",
        )
        return result
    finally:
        os.close(descriptor)


def validate_process_claim(current: cold.ColdBinding, *, starting: bool = False) -> None:
    validate_role_identity(current)
    value = current.document
    claim = _read(value["claim_ref"], value["claim_ref"]["path"])
    _typed(
        claim,
        current,
        "claim",
        {
            "source_revision",
            "role",
            "instance_id",
            "runtime",
            "pid",
            "start_ticks",
            "pid_namespace",
            "claimed_ns",
        },
    )
    for name in ("source_revision", "role", "instance_id", "runtime"):
        _require(claim[name] == value[name], "claim_changed")
    actual = process_identity()
    _require(all(_integer(claim[name], positive=True) == actual[name] for name in actual), "process_changed")
    executable, physical, version = runtime_identity(Path(value["runtime"]["path"]))
    _require(version == value["runtime"]["version"], "runtime_changed")
    _require(executable == physical == Path(value["runtime"]["path"]), "runtime_executable_changed")
    now = time.monotonic_ns()
    claimed = _integer(claim["claimed_ns"])
    _require(value["not_before_ns"] <= claimed < value["expires_at_ns"] and claimed <= now, "claim_time")
    if starting:
        _require(value["not_before_ns"] <= now < value["expires_at_ns"], "start_expired")


def validate_role_identity(current: cold.ColdBinding) -> None:
    actual = (os.geteuid(), os.getegid(), sorted(os.getgroups()))
    if current.role in cold.NATIVE_ROLES:
        _require(actual == NATIVE_IDENTITIES[current.role], "process_credentials_changed")
    else:
        _require(actual[:2] == (10001, 10001) and set(actual[2]) <= {10001}, "process_credentials_changed")


def _retained_crm(admission: dict[str, Any], current: cold.ColdBinding) -> None:
    reference = admission["retained_crm_ref"]
    _require(reference == current.document["retained_crm_ref"], "retained_crm_changed")
    value = _read(
        {"path": proof_root(current) + "/retained-crm.json", "sha256": reference["sha256"]},
        proof_root(current) + "/retained-crm.json",
    )
    _typed(
        value,
        current,
        "retained-crm",
        {
            "container_id",
            "image_id",
            "app_revision",
            "embedded_revision",
            "manager_source_root",
            "manager_source_sha256",
            "manager_mount",
            "environment_sha256",
            "owned_paths",
        },
    )
    _require(
        type(value["container_id"]) is str and re.fullmatch(r"[0-9a-f]{64}", value["container_id"]), "crm_container"
    )
    _require(type(value["image_id"]) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", value["image_id"]), "crm_image")
    _revision(value["app_revision"])
    _require(value["embedded_revision"] == EMBEDDED_REVISION, "embedded_revision_changed")
    for name in ("manager_source_sha256", "environment_sha256"):
        cold._sha(value[name])
    source = str(cold._path(value["manager_source_root"]))
    _require(source != current.document["paths"]["source_root"], "embedded_source_changed")
    mount = cold._fields(
        value["manager_mount"],
        {"source", "destination", "read_only", "data_source", "data_destination"},
        "component_mount_fields",
    )
    _require(
        mount["source"] == source and mount["destination"] == "/opt/AutostopManager" and mount["read_only"] is True,
        "embedded_mount_changed",
    )
    cold._path(mount["data_source"])
    _require(mount["data_destination"] == "/opt/AutostopManager/data", "embedded_data_mount")
    paths = value["owned_paths"]
    _require(
        type(paths) is list and 0 < len(paths) <= 64 and all(type(path) is str for path in paths), "crm_owned_paths"
    )
    _require(paths == sorted(set(paths)) and mount["data_source"] in paths, "crm_owned_paths")
    for path in paths:
        retained = cold._path(path)
        for prefix in ("/var/lib/autostop-manager-cold", "/run/autostop-manager-cold"):
            candidate = Path(prefix)
            _require(
                not (retained == candidate or retained in candidate.parents or candidate in retained.parents),
                "crm_state_overlap",
            )


def _ownership(reference: Any, current: cold.ColdBinding) -> None:
    value = _read(reference, proof_root(current) + "/ownership.json")
    _typed(
        value,
        current,
        "ownership",
        {
            "source_revision",
            "roles",
            "original_units_denied",
            "hold_ref",
            "retirement_ref",
            "retained_operator_ref",
            "old_writer_allowed",
            "candidate_may_have_written",
        },
    )
    _require(value["source_revision"] == current.document["source_revision"], "ownership_source")
    _require(value["roles"] == sorted(MODE_ROLES[current.document["mode"]]), "ownership_roles")
    _require(value["original_units_denied"] == sorted(ORIGINAL_UNITS), "original_units_not_denied")
    _require(value["old_writer_allowed"] is False and value["candidate_may_have_written"] is True, "writer_ownership")
    # These are controller-private evidence refs, not worker-readable files.
    # Admission is published only after the controller checks their actual facts.
    for name, filename in (("hold_ref", "hold.json"), ("retirement_ref", "retirement.json")):
        _require(
            cold._reference(value[name])["path"] == control_root(current) + "/" + filename, "ownership_reference_path"
        )
    if value["retained_operator_ref"] is not None:
        _require(
            cold._reference(value["retained_operator_ref"])["path"] == control_root(current) + "/operator.json",
            "operator_reference_path",
        )


def _urls(value: Any, mode: str) -> None:
    from urllib.parse import urlsplit

    urls = cold._fields(value, {"board", "mcp"}, "component_crm_url_fields")
    import ipaddress

    for name, url in urls.items():
        _require(type(url) is str, "crm_url")
        try:
            parsed = urlsplit(url)
            host, port = parsed.hostname or "", parsed.port
            _require(ipaddress.ip_address(host).is_loopback, "crm_url_host")
        except ValueError as exc:
            raise cold.ColdRuntimeDenied("component_crm_url") from exc
        _require(
            parsed.scheme == "http"
            and port == (8000 if name == "board" else 8001)
            and not (parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment),
            "crm_url",
        )
        expected = (
            ("" if name == "board" else "/mcp")
            if mode == EXTERNAL
            else (r"/__cold/[0-9a-f]{64}" if name == "board" else r"/mcp/__cold/[0-9a-f]{64}/mcp")
        )
        _require(re.fullmatch(expected, parsed.path), "crm_url_path")
        _require(not any(ord(char) <= 32 for char in url) and "\\" not in url, "crm_url")


def ensure_not_revoked(current: cold.ColdBinding) -> None:
    path = Path(proof_root(current)).parent / "revoked.json"
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise cold.ColdRuntimeDenied("component_revocation_unavailable") from exc
    raise cold.ColdRuntimeDenied("component_revoked")


def admission(current: cold.ColdBinding, state: dict[str, Any]) -> dict[str, Any]:
    ensure_not_revoked(current)
    _typed(state, current, "dispatch-state", {"state", "admission_ref", "commit_ref", "store_effects_ref"})
    _require(state["state"] in {"closed", "open"}, "dispatch_state")
    value = _read(state["admission_ref"], proof_root(current) + "/admission.json")
    _typed(
        value,
        current,
        "admission",
        {
            "source_revision",
            "not_before_ns",
            "expires_at_ns",
            "runtime_bindings",
            "crm_urls",
            "ownership_ref",
        }
        | MODE_EXTRA[current.document["mode"]],
    )
    for key in ("source_revision", "not_before_ns", "expires_at_ns"):
        _require(value[key] == current.document[key], "admission_binding_changed")
    refs = value["runtime_bindings"]
    _require(type(refs) is dict and set(refs) == MODE_ROLES[current.document["mode"]], "admission_roles")
    for role, reference in refs.items():
        _require(
            cold._reference(reference)["path"]
            == f"/etc/autostop-manager-cold/{current.document['epoch_id']}/{role}/binding.json",
            "admission_binding_path",
        )
    _require(
        refs[current.role] == {"path": os.environ[cold.BINDING_ENV], "sha256": os.environ[cold.SHA_ENV]},
        "admission_instance",
    )
    _urls(value["crm_urls"], current.document["mode"])
    _ownership(value["ownership_ref"], current)
    if current.document["mode"] == EXTERNAL:
        # Original control refs stay private; workers read byte-identical projections.
        _retained_crm(value, current)
    else:
        _require(value["candidate_crm_sha"] == current.document["candidate_crm_sha"], "crm_candidate_changed")
        _require(value["external_commit_ref"] == current.document["external_commit_ref"], "external_commit_changed")
        external = _read(
            {"path": proof_root(current) + "/external-commit.json", "sha256": value["external_commit_ref"]["sha256"]},
            proof_root(current) + "/external-commit.json",
        )
        cold._fields(
            external,
            HEADER | {"source_revision", "admission_ref", "claim_refs", "instances", "committed_ns"},
            "component_external_commit_fields",
        )
        _require(
            external["schema"] == "autostop.manager.component-commit.v1" and external["mode"] == EXTERNAL,
            "external_commit_schema",
        )
        for name in ("attempt_id", "boot_id", "generation", "epoch_id"):
            cold._uuid(external[name])
        _require(
            external["attempt_id"] == Path(value["external_commit_ref"]["path"]).parent.name
            and external["epoch_id"] == external["generation"],
            "external_commit_identity",
        )
        _require(
            external.get("source_revision") == current.document["source_revision"]
            and external.get("boot_id") == current.document["boot_id"],
            "external_commit_binding",
        )
        _require(
            type(external.get("claim_refs")) is dict and set(external["claim_refs"]) == cold.NATIVE_ROLES,
            "external_commit_roles",
        )
        for role, ref in external["claim_refs"].items():
            _require(
                cold._reference(ref)["path"] == f"/etc/autostop-manager-cold/{external['epoch_id']}/{role}/claim.json",
                "external_claim_path",
            )
        _require(
            cold._reference(external["admission_ref"])["path"]
            == str(Path(value["external_commit_ref"]["path"]).parent / "admission.json"),
            "external_admission_path",
        )
        _require(_integer(external["committed_ns"]) <= time.monotonic_ns(), "external_commit_future")
        _instances(external["instances"], cold.NATIVE_ROLES)
    return value


def _commit(current: cold.ColdBinding, state: dict[str, Any]) -> None:
    value = _read(state["commit_ref"], proof_root(current) + "/commit.json")
    _typed(value, current, "commit", {"source_revision", "admission_ref", "claim_refs", "instances", "committed_ns"})
    _require(value["source_revision"] == current.document["source_revision"], "commit_source")
    _require(
        value["admission_ref"]
        == {"path": control_root(current) + "/admission.json", "sha256": state["admission_ref"]["sha256"]},
        "commit_admission",
    )
    when = _integer(value["committed_ns"])
    _require(
        current.document["not_before_ns"] <= when < current.document["expires_at_ns"] and when <= time.monotonic_ns(),
        "commit_expired",
    )
    refs = value["claim_refs"]
    _require(type(refs) is dict and set(refs) == MODE_ROLES[current.document["mode"]], "commit_roles")
    for role, reference in refs.items():
        _require(
            cold._reference(reference)["path"]
            == f"/etc/autostop-manager-cold/{current.document['epoch_id']}/{role}/claim.json",
            "commit_claim_path",
        )
    _require(refs[current.role] == current.document["claim_ref"], "commit_claim_changed")
    _instances(value["instances"], MODE_ROLES[current.document["mode"]], current)


def _instances(value: Any, roles: frozenset[str], current: cold.ColdBinding | None = None) -> None:
    _require(type(value) is dict and set(value) == roles, "instance_roles")
    instances, processes = set(), set()
    for role, row in value.items():
        cold._fields(
            row, {"instance_id", "pid", "start_ticks", "pid_namespace", "runtime_sha256"}, "component_instance_fields"
        )
        cold._uuid(row["instance_id"])
        cold._sha(row["runtime_sha256"])
        for name in ("pid", "start_ticks", "pid_namespace"):
            _integer(row[name], positive=True)
        _require(
            row["instance_id"] not in instances and (row["pid_namespace"], row["pid"]) not in processes,
            "duplicate_instance",
        )
        instances.add(row["instance_id"])
        processes.add((row["pid_namespace"], row["pid"]))
        if current is not None and role == current.role:
            _require(
                row
                == {
                    "instance_id": current.document["instance_id"],
                    "runtime_sha256": current.document["runtime"]["sha256"],
                    **process_identity(),
                },
                "commit_process_changed",
            )


def _store_effect(current: cold.ColdBinding, state: dict[str, Any], channel: str, operation: Any) -> None:
    _require(type(operation) is str and operation, "store_operation_required")
    value = _read(state["store_effects_ref"], proof_root(current) + "/store-effects.json")
    _typed(
        value, current, "store-effects", {"source_revision", "admission_ref", "allowed_operations", "payment_scope_ref"}
    )
    _require(
        value["source_revision"] == current.document["source_revision"]
        and value["admission_ref"]
        == {"path": control_root(current) + "/admission.json", "sha256": state["admission_ref"]["sha256"]},
        "store_effect_binding",
    )
    actions = cold._fields(
        value["allowed_operations"], {"manager-store", "manager-store-owner"}, "component_store_operations"
    )
    for entries in actions.values():
        _require(
            type(entries) is list
            and len(entries) <= 1024
            and all(type(item) is str and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", item) for item in entries),
            "store_operations",
        )
        _require(entries == sorted(set(entries)), "store_operations")
    _require(operation in actions[channel], "store_operation_not_admitted")
    if operation == "set_order_payment_status" or channel == "manager-store-owner":
        scope = _read(value["payment_scope_ref"], proof_root(current) + "/store-scope.json")
        _typed(
            scope,
            current,
            "store-scope",
            {"source_revision", "store_revision", "principal_id", "scope", "amendment_receipt_ref"},
        )
        _require(
            scope["source_revision"] == current.document["source_revision"] and scope["scope"] == PAYMENT_SCOPE,
            "payment_scope",
        )
        _revision(scope["store_revision"])
        cold._uuid(scope["principal_id"])
        _require(
            scope["store_revision"] == STORE_REVISION and scope["principal_id"] == MANAGE_PRINCIPAL,
            "payment_principal_changed",
        )
        reference = cold._reference(scope["amendment_receipt_ref"])
        _require(reference["path"].startswith("/var/lib/autostop-component-release/"), "scope_receipt_path")


def admit_effect(current: cold.ColdBinding, state: dict[str, Any], channel: str, operation: Any) -> None:
    admission(current, state)
    _require(state["state"] == "open", "dispatch_closed")
    _commit(current, state)
    if channel in {"manager-store", "manager-store-owner"}:
        _store_effect(current, state, channel, operation)


@contextmanager
def starting(current: cold.ColdBinding):
    validate_process_claim(current, starting=True)
    with cold._dispatch_state(current) as state:
        admission(current, state)
        _require(state["state"] == "closed" and state["commit_ref"] is None, "start_after_commit")
        yield


def crm_urls(current: cold.ColdBinding) -> dict[str, str]:
    with cold._dispatch_state(current) as state:
        return dict(admission(current, state)["crm_urls"])
