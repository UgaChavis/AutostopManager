from __future__ import annotations

import copy
from contextlib import nullcontext
import hashlib
import io
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from disk_maintenance import cold_cas as cas
from disk_maintenance import cold_registry
from disk_maintenance import cold_restore
from disk_maintenance import core
from disk_maintenance import inventory, util
from disk_maintenance.util import MaintenanceError


@pytest.fixture(autouse=True)
def synthetic_owner(monkeypatch):
    # Hosted CI is unprivileged; the installed production defaults remain root.
    for module, name in (
        (cas, "OWNER_UID"),
        (cold_restore, "OWNER_UID"),
        (inventory, "OWNER_UID"),
        (util, "ROOT_UID"),
        (core, "ROOT_UID"),
    ):
        monkeypatch.setattr(module, name, os.getuid())


def digest(data):
    return hashlib.sha256(data).hexdigest()


def packed(objects, *, duplicate=False, unsafe=None, trailing=b""):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for key, data in objects.items():
            for _ in range(2 if duplicate else 1):
                entry = tarfile.TarInfo("objects/" + key)
                entry.size = len(data)
                if unsafe:
                    entry.name = unsafe
                archive.addfile(entry, io.BytesIO(data))
    return subprocess.run(
        ["zstd", "-q", "-T1", "-3", "--stdout"], input=output.getvalue() + trailing, capture_output=True, check=True
    ).stdout


def entry(path, kind="file", **extra):
    return {
        "path": path,
        "kind": kind,
        "uid": os.getuid(),
        "gid": os.getgid(),
        "mode": 0o700 if kind == "directory" else 0o777 if kind == "symlink" else 0o600,
        "mtime_ns": 1234567891234567890,
        **extra,
    }


@pytest.fixture
def bundle(tmp_path):
    directory = tmp_path / "bundle"
    directory.mkdir(mode=0o700)
    data = b"public synthetic package\n"
    key = digest(data)
    members = [
        entry(".", "directory"),
        entry("bin", "directory"),
        entry("bin/a", sha256=key, bytes=len(data), group="source-inode-a"),
        entry("bin/b", sha256=key, bytes=len(data), group="source-inode-b"),
        entry("bin/hard", sha256=key, bytes=len(data), group="source-inode-a"),
        entry("bin/link", "symlink", target="a"),
    ]
    root = {
        "id": "fixture",
        "kind": "tg_runtime",
        "source_path": "/opt/fixture/" + "a" * 40,
        "revision": "a" * 40,
        "provenance": {"packages": "synthetic"},
        "external_links": {},
        "entries": members,
    }
    archive = packed({key: data})
    manifest = {
        "schema": cas.SCHEMA,
        "platform": cas.current_platform(),
        "roots": [root],
        "objects": {key: len(data)},
        "carrier": {"bytes": len(archive), "sha256": digest(archive)},
    }
    (directory / "objects.tar.zst").write_bytes(archive)
    (directory / "objects.tar.zst").chmod(0o600)
    return directory, manifest, {key: data}


def seal(directory, manifest):
    raw = cas.canonical(manifest)
    (directory / "manifest.json").write_bytes(raw)
    (directory / "manifest.json").chmod(0o600)
    return digest(raw)


def test_copy_hydration_preserves_metadata_and_original_groups_without_cas_hardlinks(bundle, tmp_path):
    directory, manifest, _ = bundle
    pin = seal(directory, manifest)
    assert cas.verify_bundle(directory, pin)["ok"]
    restored = tmp_path / "restored"
    result = cas.restore_root(directory, pin, "fixture", restored, expected_revision="a" * 40)
    assert result["ok"] and result["independent_file_groups"] == 2
    assert (restored / "bin/a").stat().st_ino == (restored / "bin/hard").stat().st_ino
    assert (restored / "bin/a").stat().st_ino != (restored / "bin/b").stat().st_ino
    assert (restored / "bin/a").stat().st_nlink == 2
    assert os.readlink(restored / "bin/link") == "a"
    assert stat.S_IMODE(restored.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "change,error",
    [
        (lambda m: m.update(schema="wrong"), "cold_schema_invalid"),
        (lambda m: m["platform"].update(machine="foreign"), "cold_platform_mismatch"),
        (lambda m: m["roots"][0].update(revision="short"), "cold_revision_invalid"),
        (lambda m: m["roots"].append(copy.deepcopy(m["roots"][0])), "cold_duplicate_root"),
        (lambda m: m["roots"][0]["entries"].append(copy.deepcopy(m["roots"][0]["entries"][2])), "cold_duplicate_path"),
        (lambda m: m["roots"][0]["entries"][2].update(path="../escape"), "cold_path_invalid"),
        (lambda m: m["roots"][0]["entries"][2].update(mode=0o666), "cold_metadata_invalid"),
        (lambda m: m["roots"][0]["entries"][2].update(uid=1), "cold_metadata_invalid"),
        (lambda m: m["roots"][0]["entries"][2].update(bytes=True), "cold_object_invalid"),
        (lambda m: m["roots"][0]["entries"][4].update(mtime_ns=1), "cold_group_invalid"),
        (lambda m: m["roots"][0]["entries"][5].update(target="../../../outside"), "cold_link_invalid"),
        (lambda m: m["roots"][0]["entries"][5].update(target="/etc/passwd"), "cold_link_invalid"),
        (lambda m: m["roots"][0]["entries"][1].update(kind="symlink", target="a"), "cold_parent_invalid"),
    ],
)
def test_manifest_negative_guards_before_hydration(bundle, tmp_path, change, error):
    directory, manifest, _ = bundle
    change(manifest)
    pin = seal(directory, manifest)
    with pytest.raises(MaintenanceError, match=error):
        cas.restore_root(directory, pin, "fixture", tmp_path / "rejected", expected_revision="a" * 40)
    assert not (tmp_path / "rejected").exists()


@pytest.mark.parametrize("malformation", ["payload", "missing", "duplicate", "escape", "trailer"])
def test_attested_carrier_is_fully_verified_and_failed_restore_removed_only_private_destination(
    bundle, tmp_path, malformation
):
    directory, manifest, objects = bundle
    key = next(iter(objects))
    if malformation == "payload":
        archive = packed({key: b"x" * len(objects[key])})
    elif malformation == "missing":
        archive = packed({})
    elif malformation == "duplicate":
        archive = packed(objects, duplicate=True)
    elif malformation == "escape":
        archive = packed(objects, unsafe="../escape")
    else:
        archive = packed(objects, trailing=b"untrusted trailing data")
    (directory / "objects.tar.zst").write_bytes(archive)
    manifest["carrier"] = {"bytes": len(archive), "sha256": digest(archive)}
    pin = seal(directory, manifest)
    with pytest.raises(MaintenanceError):
        cas.restore_root(directory, pin, "fixture", tmp_path / "rejected", expected_revision="a" * 40)
    assert not (tmp_path / "rejected").exists()
    assert directory.exists() and not (tmp_path / "escape").exists()


def test_wrong_source_revision_bad_manifest_pin_and_existing_destination_stop_before_writes(bundle, tmp_path):
    directory, manifest, _ = bundle
    pin = seal(directory, manifest)
    with pytest.raises(MaintenanceError, match="cold_root_revision_mismatch"):
        cas.restore_root(directory, pin, "fixture", tmp_path / "new", expected_revision="b" * 40)
    with pytest.raises(MaintenanceError, match="cold_manifest_hash_mismatch"):
        cas.verify_bundle(directory, "0" * 64)
    assert not (tmp_path / "new").exists()
    with pytest.raises(MaintenanceError, match="cold_destination_exists"):
        cas.restore_root(directory, pin, "fixture", tmp_path, expected_revision="a" * 40)


def test_changed_carrier_without_resealing_and_duplicate_json_keys_rejected(bundle):
    directory, manifest, _ = bundle
    pin = seal(directory, manifest)
    with (directory / "objects.tar.zst").open("ab") as output:
        output.write(b"changed")
    with pytest.raises(MaintenanceError, match="cold_carrier_size_mismatch"):
        cas.verify_bundle(directory, pin)
    raw = cas.canonical(manifest).replace(b'{"carrier"', b'{"schema":"duplicate","carrier"', 1)
    (directory / "manifest.json").write_bytes(raw)
    with pytest.raises(MaintenanceError, match="cold_manifest_not_canonical"):
        cas.load_bundle(directory, digest(raw))


def test_external_hardlink_added_after_restore_rejected(bundle, tmp_path):
    directory, manifest, _ = bundle
    pin = seal(directory, manifest)
    destination = tmp_path / "restored"
    cas.restore_root(directory, pin, "fixture", destination, expected_revision="a" * 40)
    os.link(destination / "bin/b", tmp_path / "foreign")
    with pytest.raises(MaintenanceError, match="cold_external_hardlink"):
        cas.verify_tree(destination, manifest["roots"][0])


def registry_fixture(bundle, tmp_path):
    directory, manifest, _ = bundle
    root = manifest["roots"][0]
    runtime_root = tmp_path / "runtime-fixtures"
    runtime_root.mkdir(mode=0o700)
    root["source_path"] = str(runtime_root / root["revision"])
    pin = seal(directory, manifest)
    checks = dict.fromkeys(
        (
            "full_tree_sha_metadata",
            "separate_inode_groups",
            "package_pins",
            "python_abi",
            "model_manifest",
            "native_readiness",
            "network_isolated",
            "originals_unchanged",
        ),
        True,
    )
    receipt = {
        "schema": "autostop.tg-cold-rehearsal.v1",
        "ok": True,
        "root_id": root["id"],
        "revision": root["revision"],
        "manifest_sha256": pin,
        "source_path": root["source_path"],
        "platform": manifest["platform"],
        "provenance_sha256": digest(cas.canonical(root["provenance"])),
        "checks": checks,
    }
    path = tmp_path / "rehearsal.json"
    path.write_bytes(cas.canonical(receipt))
    path.chmod(0o600)
    record = {
        "path": root["source_path"],
        "revision": root["revision"],
        "root_id": root["id"],
        "bundle_dir": str(directory),
        "manifest_sha256": pin,
        "rehearsal_path": str(path),
        "rehearsal_sha256": digest(path.read_bytes()),
    }
    current = [str(tmp_path / "manager-current"), str(tmp_path / "telegram-current"), str(runtime_root / ("c" * 40))]
    previous = [str(tmp_path / "manager-previous"), str(tmp_path / "telegram-previous"), current[2]]
    historical = [str(tmp_path / "manager-old"), str(tmp_path / "telegram-old"), root["source_path"]]
    policy = {
        "inventory": {
            "roots": {"tg_runtimes": str(runtime_root)},
            "pinned_paths": [],
            "coherent_keep_paths": [current, historical, previous],
            "hot_coherent_keep_paths": [current, previous],
            "cold_tg_runtimes": [record],
        }
    }
    return policy, record, receipt


def test_native_cold_keeper_preserves_historical_tuples_and_hot_current_previous(bundle, tmp_path):
    policy, record, _ = registry_fixture(bundle, tmp_path)
    proof = cold_registry.verify(policy, full=True)
    assert proof[0]["ok"] and proof[0]["storage"] == "attested_cold"
    protected = inventory._protected_paths(policy)
    assert Path(record["path"]) not in protected
    assert Path(record["bundle_dir"]) in protected
    assert Path(record["rehearsal_path"]) in protected
    for item in policy["inventory"]["hot_coherent_keep_paths"]:
        assert all(Path(value) in protected for value in item)
    # An explicit owner pin always wins over a historical cold mapping.
    policy["inventory"]["pinned_paths"].append(record["path"])
    assert Path(record["path"]) in inventory._protected_paths(policy)


@pytest.mark.parametrize("kind", ["broken_symlink", "directory_symlink", "file"])
def test_cold_keeper_rejects_present_non_directory_before_tree_walk(bundle, tmp_path, monkeypatch, kind):
    policy, record, _ = registry_fixture(bundle, tmp_path)
    historical = Path(record["path"])
    other = tmp_path / "other-runtime"
    if kind == "file":
        historical.write_bytes(b"synthetic regular file")
    else:
        if kind == "directory_symlink":
            other.mkdir(mode=0o700)
            (other / "sentinel").write_bytes(b"synthetic other runtime")
        historical.symlink_to(other, target_is_directory=True)

    def forbidden_tree_walk(*_args, **_kwargs):
        pytest.fail("A present non-directory must be rejected without following its target")

    monkeypatch.setattr(cas, "verify_tree", forbidden_tree_walk)
    with pytest.raises(MaintenanceError, match="cold_tree_kind_mismatch"):
        cold_registry.verify(policy, full=True)
    assert historical.lstat()
    if kind == "directory_symlink":
        assert (other / "sentinel").read_bytes() == b"synthetic other runtime"


@pytest.mark.parametrize(
    "change,error",
    [
        (lambda p, r: p["inventory"].pop("hot_coherent_keep_paths"), "cold_hot_tuples_invalid"),
        (lambda p, r: p["inventory"]["hot_coherent_keep_paths"].reverse(), "cold_hot_tuples_invalid"),
        (
            lambda p, r: r.update(path=p["inventory"]["hot_coherent_keep_paths"][0][2], revision="c" * 40),
            "cold_hot_runtime_forbidden",
        ),
        (lambda p, r: p["inventory"]["cold_tg_runtimes"].append(copy.deepcopy(r)), "cold_hot_runtime_forbidden"),
        (lambda p, r: r.update(revision="b" * 40), "cold_runtime_binding_invalid"),
        (lambda p, r: r.update(path="/outside/" + "a" * 40), "cold_runtime_path_invalid"),
    ],
)
def test_cold_registry_cannot_demote_hot_paths_or_accept_unbound_revision(bundle, tmp_path, change, error):
    policy, record, _ = registry_fixture(bundle, tmp_path)
    change(policy, record)
    with pytest.raises(MaintenanceError, match=error):
        cold_registry.records(policy)


@pytest.mark.parametrize(
    "change,error",
    [
        (lambda r: r.update(manifest_sha256="0" * 64), "cold_rehearsal_binding_invalid"),
        (lambda r: r.update(revision="b" * 40), "cold_rehearsal_binding_invalid"),
        (lambda r: r["checks"].update(native_readiness=False), "cold_rehearsal_incomplete"),
        (lambda r: r.update(provenance_sha256="0" * 64), "cold_rehearsal_provenance_invalid"),
    ],
)
def test_rehearsal_receipt_must_bind_exact_runtime_provenance_and_all_checks(bundle, tmp_path, change, error):
    _, record, receipt = registry_fixture(bundle, tmp_path)
    change(receipt)
    path = Path(record["rehearsal_path"])
    path.write_bytes(cas.canonical(receipt))
    record["rehearsal_sha256"] = digest(path.read_bytes())
    with pytest.raises(MaintenanceError, match=error):
        cold_registry.attest(record)


def test_cold_runtime_real_plan_subset_approval_and_apply_preserve_hot_tuples(bundle, tmp_path, monkeypatch):
    directory, manifest, objects = bundle
    root = manifest["roots"][0]
    # Native runtime deletion retains its single-link guard. This fixture has
    # independently copied equal bytes, like the actual four historical trees.
    root["entries"][4]["group"] = "third-independent-group"
    key = digest(b"")
    for name in (".dependencies-ready", ".model-ready"):
        root["entries"].append(entry(name, sha256=key, bytes=0, group=name))
    objects[key] = b""
    manifest["objects"][key] = 0
    data = packed(objects)
    (directory / "objects.tar.zst").write_bytes(data)
    manifest["carrier"] = {"bytes": len(data), "sha256": digest(data)}
    policy, record, _ = registry_fixture(bundle, tmp_path)
    cas.restore_root(
        directory, record["manifest_sha256"], root["id"], Path(record["path"]), expected_revision=root["revision"]
    )
    hot = Path(policy["inventory"]["hot_coherent_keep_paths"][0][2])
    hot.mkdir(mode=0o700)
    (hot / "sentinel").write_bytes(b"current and previous must remain hot")
    state = tmp_path / "maintenance-state"
    state.mkdir(mode=0o700)
    complete = copy.deepcopy(core.DEFAULT_POLICY)
    complete.update(filesystem_root=str(tmp_path), state_root=str(state), automatic=True)
    complete["inventory"] = policy["inventory"]
    for key in complete["locks"]:
        complete["locks"][key] = str(tmp_path / (key + ".lock"))
        Path(complete["locks"][key]).touch(mode=0o600)
    baseline = {"technical_synthetic_host": "constant"}
    monkeypatch.setattr(core, "health", lambda _: {"ok": True})
    monkeypatch.setattr(core, "available_bytes", lambda _: 10**10)
    monkeypatch.setattr(inventory, "snapshot", lambda _: copy.deepcopy(baseline))
    monkeypatch.setattr(inventory, "process_references", lambda paths: {str(p): [] for p in paths})
    monkeypatch.setattr(inventory, "unit_references", lambda paths: {str(p): [] for p in paths})

    def collect(value):
        candidates, protected, skipped = [], [], []
        inventory._versioned(value, candidates, protected, skipped)
        return {
            "candidates": candidates,
            "protected": protected,
            "skipped": skipped,
            "fingerprint": core.digest(baseline),
        }

    monkeypatch.setattr(inventory, "collect", collect)
    monkeypatch.setattr(
        inventory,
        "verify_protected",
        lambda value: {
            "ok": True,
            "receipts": cold_registry.verify(value, full=True),
            "fingerprint": core.digest(baseline),
        },
    )
    monkeypatch.setattr(core.ci, "pause", lambda *a, **k: {"phase": "paused"})
    monkeypatch.setattr(core.ci, "resume", lambda *a, **k: {"phase": "restored"})
    monkeypatch.setattr(core, "native_locks", lambda _: nullcontext())
    plan = core.make_plan(complete)
    assert [row["path"] for row in plan["candidates"]] == [record["path"]]
    # Explicit pins still prohibit an invented hot candidate in a subset plan.
    malicious = copy.deepcopy(plan)
    malicious["candidates"][0]["path"] = str(hot)
    with pytest.raises(MaintenanceError, match="candidate_protected_overlap"):
        core._validate_candidates(malicious, complete)
    approval = state / "approved.json"
    core.atomic_json(approval, plan)
    result = core.apply(complete, approval, core.sha256_file(approval))
    assert result["ok"] and len(result["deleted"]) == 1
    assert not Path(record["path"]).exists()
    assert (hot / "sentinel").read_bytes() == b"current and previous must remain hot"
    assert cas.verify_bundle(directory, record["manifest_sha256"])["ok"]
    assert cold_registry.verify(complete, full=True)[0]["ok"]


def test_locked_hydration_approval_and_atomic_activation_never_replace_existing_runtime(bundle, tmp_path, monkeypatch):
    policy, record, _ = registry_fixture(bundle, tmp_path)
    state = tmp_path / "restore-state"
    state.mkdir(mode=0o700)
    policy.update(state_root=str(state), locks={"cleanup": str(tmp_path / "cleanup.lock")})
    monkeypatch.setattr(core, "locked", lambda _: nullcontext())
    monkeypatch.setattr(core, "native_locks", lambda _: nullcontext())
    monkeypatch.setattr(core, "health", lambda _: {"ok": True})
    with pytest.raises(MaintenanceError, match="cold_restore_approval_invalid"):
        cold_restore.restore(policy, record["revision"], "0" * 64, output=None, activate=True)
    result = cold_restore.restore(policy, record["revision"], record["manifest_sha256"], output=None, activate=True)
    assert result["ok"] and result["activated_original_path"] and not result["application_restart"]
    original = Path(record["path"])
    assert cas.verify_tree(
        original, cas.load_bundle(Path(record["bundle_dir"]), record["manifest_sha256"])["roots"][0]
    )["ok"]
    with pytest.raises(MaintenanceError, match="cold_destination_exists"):
        cold_restore.restore(policy, record["revision"], record["manifest_sha256"], output=None, activate=True)
    raced = state / "raced-directory"
    raced.mkdir(mode=0o700)
    (raced / "sentinel").write_bytes(b"existing target")
    staging = state / "staged-directory"
    staging.mkdir(mode=0o700)
    with pytest.raises(MaintenanceError, match="cold_activation_refused"):
        cold_restore._publish(staging, raced)
    assert (raced / "sentinel").read_bytes() == b"existing target" and staging.exists()


@pytest.fixture
def locked_restore(bundle, tmp_path, monkeypatch):
    policy, record, _ = registry_fixture(bundle, tmp_path)
    state = tmp_path / "restore-state"
    state.mkdir(mode=0o700)
    policy.update(state_root=str(state), locks={"cleanup": str(tmp_path / "cleanup.lock")})
    monkeypatch.setattr(core, "locked", lambda _: nullcontext())
    monkeypatch.setattr(core, "native_locks", lambda _: nullcontext())
    monkeypatch.setattr(core, "health", lambda _: {"ok": True})
    return policy, record, state


@pytest.mark.parametrize("failure", ["attest", "verify", "publish"])
def test_failed_activation_removes_only_owned_unpublished_hydration(locked_restore, monkeypatch, failure):
    policy, record, state = locked_restore
    target = Path(record["path"])
    error = "cold_activation_refused" if failure == "publish" else "synthetic_posthydrate_failure"
    if failure == "publish":
        publish = cold_restore._publish

        def raced_publish(staging, destination):
            destination.mkdir(mode=0o700)
            (destination / "sentinel").write_bytes(b"raced target must survive")
            publish(staging, destination)

        monkeypatch.setattr(cold_restore, "_publish", raced_publish)
    else:
        calls = 0
        module, name = (cold_registry, "attest") if failure == "attest" else (cas, "verify_tree")
        original = getattr(module, name)

        def fail_after_hydration(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise MaintenanceError(error)
            return original(*args, **kwargs)

        monkeypatch.setattr(module, name, fail_after_hydration)

    with pytest.raises(MaintenanceError, match=error):
        cold_restore.restore(policy, record["revision"], record["manifest_sha256"], output=None, activate=True)
    assert list(state.iterdir()) == []
    assert cas.verify_bundle(Path(record["bundle_dir"]), record["manifest_sha256"])["ok"]
    if failure == "publish":
        assert (target / "sentinel").read_bytes() == b"raced target must survive"
    else:
        assert not target.exists()


@pytest.mark.parametrize("activate", [False, True])
def test_posthydrate_health_failure_preserves_user_output_or_published_runtime(
    locked_restore, tmp_path, monkeypatch, activate
):
    policy, record, state = locked_restore
    checks = 0

    def fail_health_after_restore(_policy):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise MaintenanceError("synthetic_postrestore_health_failure")
        return {"ok": True}

    monkeypatch.setattr(core, "health", fail_health_after_restore)
    destination = Path(record["path"]) if activate else tmp_path / "owner-output"
    with pytest.raises(MaintenanceError, match="synthetic_postrestore_health_failure"):
        cold_restore.restore(
            policy,
            record["revision"],
            record["manifest_sha256"],
            output=None if activate else destination,
            activate=activate,
        )
    root = cas.load_bundle(Path(record["bundle_dir"]), record["manifest_sha256"])["roots"][0]
    assert cas.verify_tree(destination, root)["ok"]
    assert list(state.iterdir()) == []


def test_publication_error_after_rename_preserves_published_runtime(locked_restore, monkeypatch):
    policy, record, state = locked_restore
    publish = cold_restore._publish

    def fail_after_rename(staging, destination):
        publish(staging, destination)
        raise MaintenanceError("synthetic_postrename_failure")

    monkeypatch.setattr(cold_restore, "_publish", fail_after_rename)
    with pytest.raises(MaintenanceError, match="synthetic_postrename_failure"):
        cold_restore.restore(policy, record["revision"], record["manifest_sha256"], output=None, activate=True)
    root = cas.load_bundle(Path(record["bundle_dir"]), record["manifest_sha256"])["roots"][0]
    assert cas.verify_tree(Path(record["path"]), root)["ok"]
    assert list(state.iterdir()) == []


@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_staging_cleanup_refuses_replaced_directory_or_symlink(tmp_path, replacement):
    staging = tmp_path / "owned-staging"
    staging.mkdir(mode=0o700)
    before = staging.lstat()
    retained = tmp_path / "retained-staging"
    staging.rename(retained)
    if replacement == "directory":
        staging.mkdir(mode=0o700)
        (staging / "sentinel").write_bytes(b"replacement must survive")
    else:
        staging.symlink_to(retained, target_is_directory=True)
    with pytest.raises(MaintenanceError, match="cold_staging_changed"):
        cold_restore._remove_staging(staging, (before.st_dev, before.st_ino))
    assert retained.is_dir()
    if replacement == "symlink":
        assert staging.is_symlink()
    else:
        assert (staging / "sentinel").read_bytes() == b"replacement must survive"


def test_staging_cleanup_failure_preserves_original_error_and_retains_copy(locked_restore, monkeypatch):
    policy, record, state = locked_restore
    attest = cold_registry.attest
    calls = 0

    def fail_after_hydration(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise MaintenanceError("synthetic_posthydrate_failure")
        return attest(*args, **kwargs)

    def fail_cleanup(*_args):
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(cold_registry, "attest", fail_after_hydration)
    monkeypatch.setattr(cold_restore, "_remove_staging", fail_cleanup)
    with pytest.raises(MaintenanceError, match="synthetic_posthydrate_failure") as caught:
        cold_restore.restore(policy, record["revision"], record["manifest_sha256"], output=None, activate=True)
    assert isinstance(caught.value.__cause__, OSError)
    assert caught.value.__notes__ == ["cold_staging_cleanup_failed"]
    assert len(list(state.iterdir())) == 1
    assert not Path(record["path"]).exists()
