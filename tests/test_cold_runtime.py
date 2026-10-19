"""Cold configuration and actual dispatch refusal, entirely artificial I/O."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from autostop_manager import cold_runtime as cold

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Cold native admission uses Linux file locks")


EPOCH = "11111111-1111-4111-8111-111111111111"
ATTEMPT = "22222222-2222-4222-8222-222222222222"
BOOT = "33333333-3333-4333-8333-333333333333"
ORIGIN = "44444444-4444-4444-8444-444444444444"
REVISION = "a" * 40
COMMON = {"attempt_id": ATTEMPT, "boot_id": BOOT, "generation": EPOCH, "epoch_id": EPOCH}


def bootstrap_module():
    path = Path(__file__).parents[1] / "scripts/cold-role-start.py"
    spec = importlib.util.spec_from_file_location("cold_bootstrap_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("role", "module", "arguments"),
    [
        ("manager-mcp", "mcp_server", []),
        ("j1", "j1_research", ["worker"]),
        ("scheduler", "automation_daemon", ["daemon"]),
        ("work-telegram", "telegram_bridge", ["--account", "work", "daemon"]),
        ("personal-telegram", "telegram_bridge", ["--account", "personal", "daemon"]),
        ("wake", "telegram_wake", ["daemon"]),
    ],
)
def test_fixed_bootstrap_validates_before_consumer_import(monkeypatch, role, module, arguments):
    bootstrap = bootstrap_module()
    source = Path(f"/opt/autostop-manager-cold-releases/{REVISION}/source")
    events = []
    current = SimpleNamespace(document={"paths": {"source_root": str(source)}})
    fake_cold = SimpleNamespace(
        binding=lambda: events.append("binding") or current,
        require_role=lambda selected: events.append(("role", selected)),
    )

    def imported(name):
        events.append(("import", name))
        if name == "autostop_manager.cold_runtime":
            return fake_cold
        assert name == "autostop_manager." + module
        assert events[-3:-1] == ["binding", ("role", role)]
        return SimpleNamespace(main=lambda: events.append("consumer"))

    monkeypatch.setattr(bootstrap, "_source_root", lambda: source)
    monkeypatch.setattr(bootstrap, "importlib", SimpleNamespace(import_module=imported))
    monkeypatch.setattr(bootstrap, "sys", SimpleNamespace(modules={}, path=[], argv=[]))
    assert bootstrap.main(["--role", role]) == 0
    assert bootstrap.sys.path == [str(source)]
    assert bootstrap.sys.argv == [str(source / "scripts/cold-role-start.py"), *arguments]
    assert events[-1] == "consumer"


@pytest.mark.parametrize("failure", ["missing", "role", "source", "loaded", "arbitrary"])
def test_fixed_bootstrap_denies_before_application_import(monkeypatch, failure):
    bootstrap = bootstrap_module()
    source = Path(f"/opt/autostop-manager-cold-releases/{REVISION}/source")
    imports = []

    def require_role(_role):
        if failure == "role":
            raise cold.ColdRuntimeDenied("cold_entrypoint_role_changed")

    def imported(name):
        imports.append(name)
        assert name == "autostop_manager.cold_runtime"
        return SimpleNamespace(
            binding=lambda: (
                None
                if failure == "missing"
                else SimpleNamespace(
                    document={"paths": {"source_root": "/old" if failure == "source" else str(source)}}
                )
            ),
            require_role=require_role,
        )

    monkeypatch.setattr(bootstrap, "_source_root", lambda: source)
    monkeypatch.setattr(bootstrap, "importlib", SimpleNamespace(import_module=imported))
    loaded = {"autostop_manager": SimpleNamespace(__file__="/old/autostop_manager/__init__.py")}
    monkeypatch.setattr(
        bootstrap, "sys", SimpleNamespace(modules=loaded if failure == "loaded" else {}, path=[], argv=[])
    )
    argv = ["--role", "manager-mcp", *(["--module", "arbitrary"] if failure == "arbitrary" else [])]
    with pytest.raises((SystemExit, cold.ColdRuntimeDenied)):
        bootstrap.main(argv)
    assert not imports or imports == ["autostop_manager.cold_runtime"]


def test_fixed_bootstrap_refuses_development_source():
    with pytest.raises(SystemExit, match="cold_bootstrap_source_invalid"):
        bootstrap_module()._source_root()


@pytest.fixture
def epoch(monkeypatch, tmp_path, request):
    files = {}

    def put(path, value):
        raw = cold.canonical(value)
        files[str(path)] = raw
        return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}

    etc = f"/etc/autostop-manager-cold/{EPOCH}/dispatch"
    root = f"/var/lib/autostop-manager-cold/{EPOCH}/dispatch"
    lock = tmp_path / "dispatch.lock"
    lock.write_bytes(b"")
    metadata = lock.stat()
    fence = {
        "schema": "autostop.j1.cold-dispatch-fence.v1",
        **COMMON,
        "state_path": root + "/state.json",
        "dispatch_lock_path": root + "/dispatch.lock",
        "dispatch_lock_identity": {
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "ctime_ns": metadata.st_ctime_ns,
        },
    }
    fence_ref = put(etc + "/fence.json", fence)
    tombstones = {
        "schema": "autostop.j1.cold-dispatch-tombstones.v1",
        **COMMON,
        "origin_attempt_id": ORIGIN,
        "rows": [{"op_id": "old-unknown", "payload_sha256": "e" * 64, "outcome": "unknown", "retry": "forbidden"}],
    }
    fingerprint_kind = getattr(request, "param", None)
    if fingerprint_kind is not None:
        tombstones["schema"] = "autostop.j1.cold-dispatch-tombstones.v2"
        row = tombstones["rows"][0]
        row["fingerprint"] = {"kind": fingerprint_kind, "sha256": row.pop("payload_sha256")}
    tombstone_ref = put(etc + "/tombstones.json", tombstones)
    documents, refs = {}, {}
    for role in cold.ROLES:
        doc = {
            "schema": cold.SCHEMA,
            **COMMON,
            "source_revision": REVISION,
            "role": role,
            "paths": cold.expected_paths(EPOCH, REVISION, role),
            "tombstones_ref": tombstone_ref,
            "ingress_fence_ref": fence_ref,
        }
        documents[role] = doc
        refs[role] = put(f"/etc/autostop-manager-cold/{EPOCH}/{role}/binding.json", doc)
    epoch_doc = {"schema": "autostop.j1.cold-dispatch-epoch.v1", **COMMON, "runtime_bindings": refs}
    epoch_ref = put(etc + "/epoch.json", epoch_doc)
    plan = {
        "schema": "autostop.j1.crm-manager-cold-forward-plan.v1",
        "attempt_id": ATTEMPT,
        "candidate_manager_sha": REVISION,
        "dispatch_epoch_ref": epoch_ref,
    }
    plan_ref = put(f"/var/lib/autostop-crm-forward/attempts/{ATTEMPT}/plan.json", plan)
    commit = {
        "schema": "autostop.j1.crm-forward-commit.v1",
        "plan": plan,
        "proofs": {},
        "previous_sha256": "0" * 64,
        "sequence": 1,
    }
    commit_ref = put(cold.STATE_ROOT / "history/00000000000000000001.json", commit)
    head = {"schema": "autostop.j1.crm-forward-head.v1", "sequence": 1, "sha256": commit_ref["sha256"]}
    head_ref = put(
        cold.STATE_ROOT / "HEAD.json",
        head,
    )
    proof_root = root + "/proofs"
    projection = {
        name: put(proof_root + f"/{name}.json", document)
        for name, document in (("plan", plan), ("commit", commit), ("head", head), ("epoch", epoch_doc))
    }
    admission = {
        "schema": "autostop.j1.cold-dispatch-admission.v1",
        **COMMON,
        "source_refs": {"plan": plan_ref, "commit": commit_ref, "head": head_ref, "epoch": epoch_ref},
        "projected_refs": projection,
    }
    admission_ref = put(proof_root + "/admission.json", admission)
    state = {
        "schema": "autostop.j1.cold-dispatch-state.v1",
        **COMMON,
        "state": "open",
        "plan_ref": plan_ref,
        "commit_ref": commit_ref,
        "admission_ref": admission_ref,
    }
    put(root + "/state.json", state)
    source = Path(documents["manager-mcp"]["paths"]["source_root"])
    files[str(source / "REVISION")] = (REVISION + "\n").encode()

    def select(role):
        monkeypatch.setenv(cold.BINDING_ENV, refs[role]["path"])
        monkeypatch.setenv(cold.SHA_ENV, refs[role]["sha256"])

    monkeypatch.setattr(cold, "SOURCE_ROOT", source)
    monkeypatch.setattr(cold, "_boot_id", lambda: BOOT)
    monkeypatch.setattr(cold, "_read_protected", lambda path: files[str(path)])
    monkeypatch.setattr(cold, "_open_protected", lambda path: os.open(lock, os.O_RDONLY))
    monkeypatch.setattr(cold, "_lock_link", lambda path: lock.stat())
    select("manager-mcp")
    return {
        "documents": documents,
        "refs": refs,
        "files": files,
        "put": put,
        "state": state,
        "admission": admission,
        "proof_root": proof_root,
        "state_path": root + "/state.json",
        "select": select,
        "lock": lock,
        "fence": fence,
        "tombstones": tombstones,
    }


def test_exact_seven_roles_have_no_legacy_paths(epoch):
    assert len(cold.NATIVE_ROLES) == 6 and len(cold.ROLES) == 7
    for role, document in epoch["documents"].items():
        accepted = cold.validate_binding_document(document)
        assert accepted.role == role
        assert all(EPOCH in value or REVISION in value for value in document["paths"].values())
    assert cold.runtime_path("work-telegram", "socket_path", "/run/old.sock") == Path(
        f"/run/autostop-manager-cold/{EPOCH}/work-telegram/bridge.sock"
    )


@pytest.mark.parametrize(
    "mutation", ["extra", "old_path", "missing_path", "cross_epoch", "role", "source", "shared_ref"]
)
def test_binding_rejects_incomplete_or_substituted_configuration(epoch, mutation):
    doc = deepcopy(epoch["documents"]["manager-mcp"])
    if mutation == "extra":
        doc["permit"] = True
    elif mutation == "old_path":
        doc["paths"]["manager_db"] = "/opt/AutostopManager/data/autostop_manager.sqlite3"
    elif mutation == "missing_path":
        doc["paths"].pop("env_file")
    elif mutation == "cross_epoch":
        doc["epoch_id"] = ORIGIN
    elif mutation == "role":
        doc["role"] = "autostop-codex-start"
    elif mutation == "source":
        doc["source_revision"] = "main"
    else:
        doc["tombstones_ref"]["path"] = "/tmp/copied-tombstones.json"
    with pytest.raises(cold.ColdRuntimeDenied):
        cold.validate_binding_document(doc)


def test_legacy_absence_preserves_defaults_without_proof_reads(monkeypatch):
    monkeypatch.delenv(cold.BINDING_ENV, raising=False)
    monkeypatch.delenv(cold.SHA_ENV, raising=False)
    monkeypatch.setattr(cold, "_read_protected", lambda _: pytest.fail("legacy read cold proof"))
    assert cold.runtime_path("wake", "socket_path", "/run/legacy.sock") == Path("/run/legacy.sock")
    with cold.dispatch_guard(None, "legacy"):
        pass


def test_cold_source_cannot_fall_back_by_omitting_manifest(monkeypatch):
    monkeypatch.delenv(cold.BINDING_ENV, raising=False)
    monkeypatch.delenv(cold.SHA_ENV, raising=False)
    monkeypatch.setattr(cold, "SOURCE_ROOT", Path(f"/opt/autostop-manager-cold-releases/{REVISION}/source"))
    with pytest.raises(cold.ColdRuntimeDenied, match="binding_required_for_cold_source"):
        cold.binding()


def test_loader_checks_source_boot_hash_and_environment(epoch, monkeypatch):
    assert cold.binding().role == "manager-mcp"
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", "/var/lib/old.sqlite3")
    with pytest.raises(cold.ColdRuntimeDenied, match="environment_path_conflict"):
        cold.configured_path("AUTOSTOP_MANAGER_DB", "manager-mcp", "manager_db", "/old")
    monkeypatch.setattr(cold, "_boot_id", lambda: ORIGIN)
    with pytest.raises(cold.ColdRuntimeDenied, match="boot_changed"):
        cold.binding()


def test_cold_env_loader_uses_only_protected_role_environment(epoch, monkeypatch):
    from autostop_manager import config

    monkeypatch.setattr(config.os, "environ", dict(config.os.environ))
    path = epoch["documents"]["manager-mcp"]["paths"]["env_file"]
    epoch["files"][path] = b"COLD_SYNTHETIC_MARKER=current-epoch\n"
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", path)
    monkeypatch.delenv("COLD_SYNTHETIC_MARKER", raising=False)
    monkeypatch.setattr(config, "_ENV_LOADED", False)
    assert config.load_runtime_env(force=True) == [Path(path)]
    assert os.environ["COLD_SYNTHETIC_MARKER"] == "current-epoch"


def test_manifest_hash_or_duplicate_json_key_cannot_configure(epoch):
    ref = epoch["refs"]["manager-mcp"]
    epoch["files"][ref["path"]] += b" "
    with pytest.raises(cold.ColdRuntimeDenied, match="hash_changed"):
        cold.binding()
    with pytest.raises(cold.ColdRuntimeDenied, match="duplicate_json_key"):
        cold._document(b'{"state":"closed","state":"open"}')


def test_closed_epoch_never_calls_effect(epoch):
    state = {**epoch["state"], "state": "closed", "plan_ref": None, "commit_ref": None, "admission_ref": None}
    epoch["put"](epoch["state_path"], state)
    called = []

    @cold.guarded_effect("manager-store", lambda args: args["operation_id"])
    def effect(operation_id):
        called.append(operation_id)

    with pytest.raises(cold.ColdRuntimeDenied, match="dispatch_closed"):
        effect("current-action")
    assert called == []


def test_open_requires_projected_commit_head_match(epoch):
    admission = deepcopy(epoch["admission"])
    projected = epoch["put"](
        epoch["proof_root"] + "/head.json",
        {"schema": "autostop.j1.crm-forward-head.v1", "sequence": 2, "sha256": "f" * 64},
    )
    admission["projected_refs"]["head"] = projected
    admission["source_refs"]["head"]["sha256"] = projected["sha256"]
    admission_ref = epoch["put"](epoch["proof_root"] + "/admission.json", admission)
    epoch["put"](epoch["state_path"], {**epoch["state"], "admission_ref": admission_ref})
    with pytest.raises(cold.ColdRuntimeDenied, match="not_current_head"):
        with cold.dispatch_guard("current-action", "manager-store"):
            pytest.fail("effect executed")


@pytest.mark.parametrize("invalid_sequence", [True, 1.0])
def test_head_sequence_has_strict_integer_type(epoch, invalid_sequence):
    admission = deepcopy(epoch["admission"])
    projected = epoch["put"](
        epoch["proof_root"] + "/head.json",
        {
            "schema": "autostop.j1.crm-forward-head.v1",
            "sequence": invalid_sequence,
            "sha256": epoch["state"]["commit_ref"]["sha256"],
        },
    )
    admission["projected_refs"]["head"] = projected
    admission["source_refs"]["head"]["sha256"] = projected["sha256"]
    admission_ref = epoch["put"](epoch["proof_root"] + "/admission.json", admission)
    epoch["put"](epoch["state_path"], {**epoch["state"], "admission_ref": admission_ref})
    with pytest.raises(cold.ColdRuntimeDenied, match="not_current_head"):
        with cold.dispatch_guard("current-action", "manager-store"):
            pytest.fail("malformed HEAD admitted an effect")


def test_unknown_old_operation_cannot_be_retagged_with_new_payload(epoch):
    with pytest.raises(cold.ColdRuntimeDenied, match="old_operation_forbidden"):
        with cold.dispatch_guard("old-unknown", "manager-store", payload_sha256="a" * 64):
            pytest.fail("old operation replayed")


@pytest.mark.parametrize("epoch", ["payload_sha256", "observed_record_sha256"], indirect=True)
def test_v2_fingerprint_preserves_id_only_quarantine_and_new_dispatch(epoch):
    called = []
    for payload in (None, "a" * 64, "e" * 64):
        with pytest.raises(cold.ColdRuntimeDenied, match="old_operation_forbidden"):
            with cold.dispatch_guard("old-unknown", "manager-store", payload_sha256=payload):
                called.append("old")
    with cold.dispatch_guard("new-operation", "manager-store", payload_sha256="e" * 64):
        called.append("new")
    assert called == ["new"]


@pytest.mark.parametrize(
    "mutation",
    [
        "kind",
        "kind_type",
        "digest",
        "extra",
        "missing",
        "mixed",
        "legacy_extra",
        "duplicate",
        "retry",
        "schema",
        "observed_outcome",
    ],
)
def test_tombstone_fingerprint_is_strict_and_never_relabels_observation(epoch, mutation):
    doc = deepcopy(epoch["tombstones"])
    row = doc["rows"][0]
    doc["schema"] = "autostop.j1.cold-dispatch-tombstones.v2"
    row["fingerprint"] = {"kind": "observed_record_sha256", "sha256": row.pop("payload_sha256")}
    if mutation == "kind":
        row["fingerprint"]["kind"] = "inferred_payload_sha256"
    elif mutation == "kind_type":
        row["fingerprint"]["kind"] = {"value": "payload_sha256"}
    elif mutation == "digest":
        row["fingerprint"]["sha256"] = True
    elif mutation == "extra":
        row["fingerprint"]["accepted"] = True
    elif mutation == "missing":
        row["fingerprint"].pop("kind")
    elif mutation == "mixed":
        row["payload_sha256"] = "a" * 64
    elif mutation == "legacy_extra":
        doc["schema"] = "autostop.j1.cold-dispatch-tombstones.v1"
    elif mutation == "duplicate":
        doc["rows"].append(deepcopy(row))
    elif mutation == "retry":
        row["retry"] = "allowed"
    elif mutation == "observed_outcome":
        row["outcome"] = "applied"
    else:
        doc["schema"] = "autostop.j1.cold-dispatch-tombstones.v3"
    with pytest.raises(cold.ColdRuntimeDenied):
        cold._blocked_operation_ids(doc, cold.binding())


def test_cold_browser_never_reads_old_release_or_connects_to_renderer(epoch, monkeypatch):
    from autostop_manager import j1_browser

    def forbidden(*_args, **_kwargs):
        pytest.fail("cold role used the denied legacy browser")

    monkeypatch.setattr(j1_browser, "_read_root_owned_file", forbidden)
    monkeypatch.setattr(j1_browser.socket, "socket", forbidden)
    monkeypatch.setattr(j1_browser, "_socket_ready", forbidden)
    assert j1_browser.active_release_revision() == ("", "browser_cold_role_denied")
    assert j1_browser.browser_status()["browser_ready"] is False
    result = j1_browser.render_page("https://example.org/article", socket_path="/old/renderer.sock")
    assert result["error"] == "browser_cold_role_denied"
    assert result["retryable"] is False and result["safe_disabled"] is True


def test_real_shared_lock_excludes_close_through_actual_effect(epoch):
    import fcntl

    other = os.open(epoch["lock"], os.O_RDONLY)
    try:
        with cold.dispatch_guard("current-action", "manager-store"):
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(other)


def test_exclusive_close_or_replaced_lock_prevents_admission(epoch):
    import fcntl

    other = os.open(epoch["lock"], os.O_RDONLY)
    try:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(cold.ColdRuntimeDenied, match="serialization_busy"):
            with cold.dispatch_guard("current-action", "manager-store"):
                pytest.fail("effect executed")
    finally:
        os.close(other)
    epoch["lock"].unlink()
    epoch["lock"].write_bytes(b"new inode")
    with pytest.raises(cold.ColdRuntimeDenied, match="lock_replaced"):
        with cold.dispatch_guard("current-action", "manager-store"):
            pytest.fail("effect executed")


def test_async_guard_keeps_lock_across_await_and_cancellation(epoch):
    import fcntl

    entered, released = [], []

    @cold.guarded_effect("manager-store", lambda args: args["operation_id"])
    async def effect(operation_id):
        other = os.open(epoch["lock"], os.O_RDONLY)
        try:
            await asyncio.sleep(0)
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            entered.append(operation_id)
            raise asyncio.CancelledError
        finally:
            os.close(other)
            released.append(True)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(effect("current-action"))
    assert entered == ["current-action"] and released == [True]
    other = os.open(epoch["lock"], os.O_RDONLY)
    try:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(other)


def test_unknown_effect_exception_is_preserved_without_reexecution(epoch):
    called = []

    @cold.guarded_effect("manager-store", lambda args: args["operation_id"])
    def effect(operation_id):
        called.append(operation_id)
        raise ConnectionError("synthetic unknown outcome")

    with pytest.raises(ConnectionError, match="unknown outcome"):
        effect("current-action")
    assert called == ["current-action"]


@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: -1)() != 0,
    reason="Real protected-file checks require a root-owned disposable source tree",
)
def test_real_protected_file_rejects_symlink_hardlink_permissions_and_fifo():
    from tempfile import TemporaryDirectory

    # /tmp itself is writable and correctly rejected. Use only a newly created
    # temporary child of this root-owned development checkout, then remove it.
    with TemporaryDirectory(prefix=".cold-reader-test-", dir=Path(__file__).resolve().parents[1]) as name:
        parent = Path(name)
        path = parent / "binding.json"
        path.write_bytes(b'{"fixture":true}')
        path.chmod(0o600)
        assert cold._read_protected(path) == b'{"fixture":true}'
        path.chmod(0o660)
        with pytest.raises(cold.ColdRuntimeDenied):
            cold._read_protected(path)
        path.chmod(0o600)
        hardlink = parent / "hardlink"
        os.link(path, hardlink)
        with pytest.raises(cold.ColdRuntimeDenied):
            cold._read_protected(path)
        hardlink.unlink()
        symlink = parent / "symlink"
        symlink.symlink_to(path)
        with pytest.raises(cold.ColdRuntimeDenied):
            cold._read_protected(symlink)
        fifo = parent / "fifo"
        os.mkfifo(fifo, mode=0o600)
        with pytest.raises(cold.ColdRuntimeDenied):
            cold._read_protected(fifo)
        parent.chmod(0o770)
        with pytest.raises(cold.ColdRuntimeDenied):
            cold._read_protected(path)
        parent.chmod(0o700)


def test_channel_role_and_missing_identity_fail_closed(epoch):
    for operation_id, channel in [(None, "manager-store"), ("", "manager-store"), ("current-action", "wake")]:
        with pytest.raises(cold.ColdRuntimeDenied):
            with cold.dispatch_guard(operation_id, channel):
                pytest.fail("effect executed")
    epoch["select"]("crm-embedded")
    with cold.dispatch_guard("current-action", "manager-store"):
        pass


def close_epoch(epoch):
    epoch["put"](
        epoch["state_path"],
        {**epoch["state"], "state": "closed", "plan_ref": None, "commit_ref": None, "admission_ref": None},
    )


def test_dispatch_reader_never_accesses_private_controller_graph(epoch):
    for path in list(epoch["files"]):
        if path.startswith("/var/lib/autostop-crm-forward/"):
            del epoch["files"][path]
    with cold.dispatch_guard("current-action", "manager-store"):
        pass


@pytest.mark.parametrize(
    "mutation",
    ["extra", "generation", "projection_path", "projection_hash", "source_plan", "source_head", "missing_epoch"],
)
def test_incomplete_or_substituted_projection_never_admits_effect(epoch, mutation):
    admission = deepcopy(epoch["admission"])
    if mutation == "extra":
        admission["permission"] = True
    elif mutation == "generation":
        admission["generation"] = ORIGIN
    elif mutation == "projection_path":
        admission["projected_refs"]["head"]["path"] = "/tmp/head.json"
    elif mutation == "projection_hash":
        admission["projected_refs"]["head"]["sha256"] = "f" * 64
    elif mutation == "source_plan":
        admission["source_refs"]["plan"]["path"] = "/tmp/plan.json"
    elif mutation == "source_head":
        admission["source_refs"]["head"]["path"] = "/tmp/HEAD.json"
    else:
        admission["projected_refs"].pop("epoch")
    ref = epoch["put"](epoch["proof_root"] + "/admission.json", admission)
    epoch["put"](epoch["state_path"], {**epoch["state"], "admission_ref": ref})
    with pytest.raises(cold.ColdRuntimeDenied):
        with cold.dispatch_guard("current-action", "manager-store"):
            pytest.fail("effect executed")


def test_closed_epoch_blocks_real_store_http_consumer_before_transport(epoch, monkeypatch):
    from autostop_manager import store_api

    close_epoch(epoch)
    monkeypatch.setattr(store_api, "_open_response", lambda *a, **k: pytest.fail("Store transport dispatched"))
    client = store_api.StoreApiClient(
        api_url="http://127.0.0.1:8000/internal/agent/v1", read_token="fixture-read", manage_token="fixture-manage"
    )
    with pytest.raises(cold.ColdRuntimeDenied, match="dispatch_closed"):
        client.management_action(
            operation="update_quote_request_comment",
            target_id="fixture",
            expected_updated_at="2026-10-19T00:00:00Z",
            owner_intent="fixture",
            idempotency_key="existing-request",
            correlation_id="fixture-correlation",
            mode="apply",
            planned_changes={"internal_comment": "fixture"},
        )


def test_closed_epoch_blocks_telegram_before_actual_send(epoch):
    from autostop_manager import telegram_bridge as bridge

    epoch["select"]("work-telegram")
    close_epoch(epoch)
    with pytest.raises(cold.ColdRuntimeDenied, match="dispatch_closed"):
        asyncio.run(
            bridge._handle_send_text_to_entity(
                object(),
                object(),
                {},
                entity=object(),
                target={"kind": "private", "id": 1},
                text="fixture",
                mode="apply",
                idempotency_key="existing-request",
            )
        )


def test_closed_epoch_blocks_scheduler_claim_and_outbox_consumers(epoch):
    from autostop_manager.automation_daemon import AutomationDaemon

    epoch["select"]("scheduler")
    close_epoch(epoch)
    for function, claim in [
        (AutomationDaemon._execute_claim, {"run_id": "existing-run"}),
        (AutomationDaemon._execute_outbox, {"idempotency_key": "existing-outbox"}),
    ]:
        with pytest.raises(cold.ColdRuntimeDenied, match="dispatch_closed"):
            asyncio.run(function(object(), claim))


def test_closed_epoch_blocks_j1_before_database_work(epoch, monkeypatch):
    from autostop_manager import j1_research

    epoch["select"]("j1")
    close_epoch(epoch)
    monkeypatch.setattr(j1_research, "_db", lambda **k: pytest.fail("job consumer entered"))
    with pytest.raises(cold.ColdRuntimeDenied, match="dispatch_closed"):
        j1_research._work_job("existing-job")


def test_closed_epoch_blocks_direct_turn_start_rpc(epoch):
    from autostop_manager.telegram_wake import AppServer

    epoch["select"]("wake")
    close_epoch(epoch)
    app = object.__new__(AppServer)
    app.active_event_id = "existing-event"
    with pytest.raises(cold.ColdRuntimeDenied, match="dispatch_closed"):
        asyncio.run(app.request("turn/start", {"threadId": "fixture-thread"}))


def test_cold_scheduler_cannot_adopt_legacy_timers(epoch):
    from autostop_manager.automation_timers import SystemTimerController

    epoch["select"]("scheduler")
    controller = SystemTimerController(runner=lambda *a: pytest.fail("legacy systemctl invoked"))
    assert controller.policies == {}
    assert controller.adopt_all_current() == []


def test_before_import_paths_and_default_arguments_follow_bound_epoch(epoch):
    # A separate interpreter proves bootstrap order; fake proof bytes are supplied
    # before any consumer module is imported. No runtime files or RPC are used.
    import json
    import subprocess

    fixture = {
        "files": {key: value.decode() for key, value in epoch["files"].items()},
        "ref": epoch["refs"]["wake"],
        "boot": BOOT,
        "source": epoch["documents"]["wake"]["paths"]["source_root"],
    }
    program = """
import json,os,sys
from pathlib import Path
from autostop_manager import cold_runtime as c
f=json.load(sys.stdin)
c.SOURCE_ROOT=Path(f['source'])
c._boot_id=lambda:f['boot']
c._read_protected=lambda p:f['files'][str(p)].encode()
os.environ[c.BINDING_ENV]=f['ref']['path']
os.environ[c.SHA_ENV]=f['ref']['sha256']
from autostop_manager import telegram_owner as o, telegram_bridge as b, telegram_wake as w
assert o.WORK_OWNER_CONFIG_PATH==c.runtime_path('work-telegram','owner_config_path','/old')
assert b.ACCOUNT_PATHS['work'].socket_path==c.runtime_path('work-telegram','socket_path','/old')
assert b.TelegramConfig.__dataclass_fields__['session_path'].default==c.runtime_path('personal-telegram','session_path','/old')
assert w.CONFIG_PATH==c.runtime_path('wake','config_path','/old')
assert w.WakeConfig.load.__func__.__defaults__==(w.CONFIG_PATH,)
assert w.APP_SOCKET==str(c.runtime_path('wake','app_socket','/old'))
assert w.PROJECT_DIR==f['source']
print('cold_preimport_defaults_ok=true')
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", program], input=json.dumps(fixture), text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "cold_preimport_defaults_ok=true"
