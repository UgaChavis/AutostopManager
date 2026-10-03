from __future__ import annotations

import copy
import importlib.util
import json
import os
import stat
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts.disk_maintenance import core, util
from scripts.disk_maintenance.util import MaintenanceError


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "ROOT_UID", os.geteuid())
    monkeypatch.setattr(util, "ROOT_UID", os.geteuid())
    policy = copy.deepcopy(core.DEFAULT_POLICY)
    policy["automatic"] = True
    policy["pg_retention_enabled"] = True
    policy["filesystem_root"] = str(tmp_path)
    state = tmp_path / "private"
    state.mkdir(mode=0o700)
    policy["state_root"] = str(state)
    roots = {}
    for key in policy["inventory"]["roots"]:
        path = tmp_path / key
        path.mkdir(mode=0o700)
        roots[key] = str(path)
    policy["inventory"]["roots"] = roots
    for key in policy["locks"]:
        path = tmp_path / (key + ".lock")
        path.touch(mode=0o600)
        policy["locks"][key] = str(path)
    baseline = {"containers": [{"id": "stable"}], "units": [{"pid": 42}]}
    candidates = []
    calls = []
    monkeypatch.setattr(core, "health", lambda value: {"ok": True})
    monkeypatch.setattr(core, "available_bytes", lambda value: 10_000_000_000)
    monkeypatch.setattr(core.inventory, "snapshot", lambda value: copy.deepcopy(baseline))
    monkeypatch.setattr(
        core.inventory,
        "collect",
        lambda value: {
            "fingerprint": core.digest(baseline),
            "candidates": copy.deepcopy(candidates),
            "protected": [],
            "skipped": [],
        },
    )
    monkeypatch.setattr(
        core.inventory,
        "verify_protected",
        lambda value: {
            "ok": True,
            "receipts": [
                {"ok": True, "component": component} for component in ("full", "crm", "store", "pg", "coherent_runtime")
            ],
            "fingerprint": core.digest(baseline),
        },
    )
    monkeypatch.setattr(core.inventory, "revalidate", lambda candidate, value: calls.append("revalidate"))

    def pause(value, path, **kwargs):
        calls.append("pause")
        util.atomic_json(path, {"phase": "paused"})
        return {"phase": "paused"}

    def resume(value, path):
        calls.append("resume")
        util.atomic_json(path, {"phase": "restored"})
        return {"phase": "restored"}

    monkeypatch.setattr(core.ci, "pause", pause)
    monkeypatch.setattr(core.ci, "resume", resume)
    return policy, candidates, baseline, calls


def candidate(path: Path, kind="recovery_directory", component="crm", **proof):
    return {
        "kind": kind,
        "path": str(path),
        "component": component,
        "identity": util.file_identity(path),
        "estimated_reclaim_bytes": 4096,
        "reason": "older_owned_backup",
        "proof": {"metadata_valid": True, **proof},
    }


def data_directory(policy, name="old", root="crm_recovery"):
    path = Path(policy["inventory"]["roots"][root]) / name
    path.mkdir(mode=0o700)
    (path / "artifact").write_bytes(b"synthetic")
    return path


def test_maintain_deletes_only_owned_candidate_and_reports_deficit(setup):
    policy, candidates, _, calls = setup
    old = data_directory(policy)
    untouched = data_directory(policy, "unrecognized")
    candidates.append(candidate(old))
    result = core.maintain(policy)
    assert not old.exists() and untouched.exists()
    assert result["ok"] and result["ci_restored"] and not result["target_reached"]
    assert result["deficit_bytes"] == 10_000_000_000
    assert calls == ["pause", "revalidate", "resume"]
    state = Path(policy["state_root"])
    for path in state.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert util.read_json(state / "last-result.json", private=True)["deleted"] == result["deleted"]


def test_missing_preserved_cold_receipt_keeps_old_archives(setup, monkeypatch):
    policy, candidates, _, calls = setup
    old = data_directory(policy)
    candidates.append(candidate(old))
    monkeypatch.setattr(core.inventory, "verify_protected", lambda value: {"ok": True, "receipts": []})
    result = core.maintain(policy)
    assert old.exists() and result["errors"] == ["preserved_verification_missing"]
    assert calls == ["pause", "resume"]


@pytest.mark.parametrize("changed", ["host", "policy", "runtime", "expiry"])
def test_manual_approval_checks_precede_ci_mutation(setup, changed):
    policy, _, baseline, calls = setup
    manifest = core.make_plan(policy)
    path = Path(policy["state_root"]) / "approval.json"
    if changed == "host":
        manifest["host"]["boot_id"] = "another"
    elif changed == "policy":
        manifest["policy_sha256"] = "0" * 64
    elif changed == "runtime":
        baseline["units"][0]["pid"] = 43
    else:
        manifest["expires_at"] = time.time() - 1
    util.atomic_json(path, manifest)
    result = core.apply(policy, path, util.sha256_file(path))
    assert not result["ok"] and "pause" not in calls


def test_approval_sha_and_private_mode_guard(setup):
    policy, _, _, calls = setup
    path = Path(policy["state_root"]) / "approval.json"
    util.atomic_json(path, core.make_plan(policy))
    with pytest.raises(MaintenanceError, match="manifest_approval_invalid"):
        core.apply(policy, path, "0" * 64)
    path.chmod(0o644)
    with pytest.raises(MaintenanceError, match="private_file_invalid"):
        core.apply(policy, path, util.sha256_file(path))
    assert not calls


def test_maintain_regenerates_manifest_after_ci_wait(setup, monkeypatch):
    policy, _, _, calls = setup
    clock = [1_000_000.0]
    original_pause = core.ci.pause
    monkeypatch.setattr(core.time, "time", lambda: clock[0])

    def delayed_pause(*args, **kwargs):
        original_pause(*args, **kwargs)
        clock[0] += 1800

    monkeypatch.setattr(core.ci, "pause", delayed_pause)
    result = core.maintain(policy)
    manifest = util.read_json(Path(policy["state_root"]) / "last-plan.json", private=True)
    assert result["ok"] and manifest["created_at"] == clock[0]
    assert calls == ["pause", "resume"]


def test_manual_wait_expiry_restores_ci_without_deleting(setup, monkeypatch):
    policy, candidates, _, calls = setup
    old = data_directory(policy)
    candidates.append(candidate(old))
    clock = [1_000_000.0]
    monkeypatch.setattr(core.time, "time", lambda: clock[0])
    path = Path(policy["state_root"]) / "approval.json"
    util.atomic_json(path, core.make_plan(policy))
    original_pause = core.ci.pause

    def delayed_pause(*args, **kwargs):
        original_pause(*args, **kwargs)
        clock[0] += 1800

    monkeypatch.setattr(core.ci, "pause", delayed_pause)
    result = core.apply(policy, path, util.sha256_file(path))
    assert old.exists() and result["errors"] == ["manifest_expired"]
    assert calls == ["pause", "resume"]


def test_ci_unavailable_skips_store_but_cleans_native_crm(setup, monkeypatch):
    policy, candidates, _, calls = setup
    store = data_directory(policy, root="store_recovery")
    crm = data_directory(policy)
    candidates.extend([candidate(store, component="store"), candidate(crm)])

    def failed_pause(*args, **kwargs):
        raise MaintenanceError("ci_api_auth_failed")

    monkeypatch.setattr(core.ci, "pause", failed_pause)
    result = core.maintain(policy)
    assert store.exists() and not crm.exists() and result["ok"]
    assert result["skipped"] == [
        {"reason": "ci_api_auth_failed"},
        {"component": "store", "reason": "ci_fence_unavailable"},
    ]
    assert calls == ["revalidate"]


def test_changed_candidate_during_window_aborts_and_restores(setup, monkeypatch):
    policy, candidates, _, calls = setup
    old = data_directory(policy)
    candidates.append(candidate(old))

    def reject(candidate, policy):
        raise MaintenanceError("candidate_identity_or_policy_changed")

    monkeypatch.setattr(core.inventory, "revalidate", reject)
    result = core.maintain(policy)
    assert old.exists() and result["errors"] == ["candidate_identity_or_policy_changed"]
    assert calls == ["pause", "resume"]


def test_health_failure_stops_next_batch_and_restores(setup, monkeypatch):
    policy, candidates, _, calls = setup
    first = data_directory(policy, "first")
    second = data_directory(policy, "second")
    candidates.extend([candidate(first), candidate(second)])

    def guard(value):
        if not first.exists():
            raise MaintenanceError("health_http_failed")
        return {"ok": True}

    monkeypatch.setattr(core, "health", guard)
    result = core.maintain(policy)
    assert not first.exists() and second.exists()
    assert result["errors"] == ["health_http_failed"] and calls[-1] == "resume"


def test_post_delete_runtime_change_stops_second_batch(setup, monkeypatch):
    policy, candidates, baseline, _ = setup
    first = data_directory(policy, "first")
    second = data_directory(policy, "second")
    candidates.extend([candidate(first), candidate(second)])
    original = core.secure_delete

    def changed(*args):
        original(*args)
        baseline["containers"][0]["id"] = "restarted"

    monkeypatch.setattr(core, "secure_delete", changed)
    result = core.maintain(policy)
    assert not first.exists() and second.exists() and result["errors"] == ["runtime_changed"]


def test_keyboard_interrupt_still_restores_saved_ci(setup, monkeypatch):
    policy, candidates, _, calls = setup
    old = data_directory(policy)
    candidates.append(candidate(old))
    monkeypatch.setattr(core, "secure_delete", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        core.maintain(policy)
    assert calls[-1] == "resume" and old.exists()


def test_failed_resume_is_durable_then_recovery_retries(setup, monkeypatch):
    policy, _, _, calls = setup
    original = core.ci.resume
    monkeypatch.setattr(
        core.ci, "resume", lambda *args: (_ for _ in ()).throw(MaintenanceError("ci_resume_incomplete"))
    )
    result = core.maintain(policy)
    assert not result["ok"] and not result["ci_restored"]
    assert util.read_json(Path(policy["state_root"]) / "ci-state.json", private=True)["phase"] == "paused"
    monkeypatch.setattr(core.ci, "resume", original)
    assert core.recover_ci(policy)["ci_restored"]
    assert calls == ["pause", "resume"]
    assert util.read_json(Path(policy["state_root"]) / "last-result.json", private=True) == result


def test_recovery_timer_never_resumes_running_main(setup):
    policy, _, _, calls = setup
    with core.locked(Path(policy["locks"]["cleanup"])):
        assert core.recover_ci(policy) == {"ok": True, "busy": True}
    assert not calls and not (Path(policy["state_root"]) / "last-recovery.json").exists()


def test_pg_retention_never_reads_pending_ci_or_calls_gh(setup, monkeypatch):
    policy, candidates, _, calls = setup
    path = Path(policy["inventory"]["roots"]["pg_dumps"]) / "obsolete.dump"
    path.write_bytes(b"synthetic")
    candidates.append(candidate(path, "pg_dump", "pg"))
    # Deliberately malformed pending CI state cannot affect the independent hook.
    ci_state = Path(policy["state_root"]) / "ci-state.json"
    ci_state.write_text("malformed secret-like synthetic value")
    ci_state.chmod(0o600)
    monkeypatch.setattr(core.ci, "pause", lambda *a, **k: pytest.fail("PG must not touch CI"))
    monkeypatch.setattr(core.ci, "resume", lambda *a, **k: pytest.fail("PG must not touch CI"))
    result = core.pg_retain(policy)
    assert result["ok"] and not result["retention_pending"] and not path.exists()
    assert calls == ["revalidate"] and ci_state.read_text().startswith("malformed")
    assert (Path(policy["state_root"]) / "last-retention.json").exists()


def test_ci_head_change_is_visible_after_successful_restoration(setup, monkeypatch):
    policy, candidates, _, _ = setup
    candidates.append(candidate(data_directory(policy)))

    def resume(value, path):
        util.atomic_json(path, {"phase": "restored"})
        return {"phase": "restored", "warnings": ["ci_repository_head_changed"]}

    monkeypatch.setattr(core.ci, "resume", resume)
    result = core.maintain(policy)
    assert result["ok"] and result["ci_restored"]
    assert result["ci_warnings"] == ["ci_repository_head_changed"]
    receipt = json.loads((Path(policy["state_root"]) / "last-result.json").read_text())
    assert receipt["ci_warnings"] == result["ci_warnings"]


def test_pg_busy_lock_preserves_points_and_reports_pending(setup):
    policy, _, _, calls = setup
    with core.locked(Path(policy["locks"]["pg"])):
        result = core.pg_retain(policy)
    assert result["retention_pending"] and result["errors"] == ["native_lock_busy"]
    assert not calls


def test_native_lock_order_and_release_when_later_busy(setup, monkeypatch):
    policy, _, _, _ = setup
    acquired = []
    released = []

    @contextmanager
    def fake(path, **kwargs):
        name = next(key for key, value in policy["locks"].items() if str(path) == value)
        if name == "tg":
            raise MaintenanceError("native_lock_busy")
        acquired.append(name)
        try:
            yield
        finally:
            released.append(name)

    monkeypatch.setattr(core, "locked", fake)
    with pytest.raises(MaintenanceError, match="native_lock_busy"), core.native_locks(policy):
        pytest.fail("must not enter")
    assert acquired == ["full", "pg", "crm"] and released == ["crm", "pg", "full"]


def test_native_lock_missing_is_never_created(setup):
    policy, _, _, _ = setup
    path = Path(policy["locks"]["tg"])
    path.unlink()
    with pytest.raises(FileNotFoundError), core.native_locks(policy):
        pytest.fail("must not enter")
    assert not path.exists()


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "special", "parent_symlink", "mount"])
def test_secure_delete_rejects_escape_before_any_removal(setup, tmp_path, monkeypatch, attack):
    policy, _, _, _ = setup
    old = data_directory(policy)
    outside = tmp_path / "keep"
    outside.write_bytes(b"protected")
    if attack == "symlink":
        (old / "escape").symlink_to(outside)
    elif attack == "hardlink":
        os.link(outside, old / "escape")
    elif attack == "special":
        os.mkfifo(old / "escape")
    elif attack == "parent_symlink":
        parent = old.parent
        renamed = parent.with_name(parent.name + "-real")
        parent.rename(renamed)
        parent.symlink_to(renamed, target_is_directory=True)
    else:
        original = core._mount_id
        monkeypatch.setattr(
            core,
            "_mount_id",
            lambda fd: "different" if os.readlink(f"/proc/self/fd/{fd}") == str(old) else original(fd),
        )
    planned = candidate(old)
    with pytest.raises(MaintenanceError):
        core.secure_delete(planned, policy, time.monotonic() + 20)
    assert outside.read_bytes() == b"protected" and (old / "artifact").exists()


def test_runtime_leaf_symlink_is_unlinked_without_follow(setup, tmp_path):
    policy, _, _, _ = setup
    old = data_directory(policy, root="tg_runtimes")
    outside = tmp_path / "protected-runtime"
    outside.mkdir()
    (outside / "keep").write_text("protected")
    (old / "python").symlink_to(outside, target_is_directory=True)
    planned = candidate(old, "release_directory", "work_telegram_runtime", allow_leaf_symlinks=True)
    core.secure_delete(planned, policy, time.monotonic() + 20)
    assert not old.exists() and (outside / "keep").read_text() == "protected"


def test_runtime_link_permission_cannot_be_used_for_crm_archive(setup, tmp_path):
    policy, _, _, _ = setup
    old = data_directory(policy)
    (old / "escape").symlink_to(tmp_path)
    with pytest.raises(MaintenanceError, match="candidate_special_file"):
        core.secure_delete(candidate(old, allow_leaf_symlinks=True), policy, time.monotonic() + 20)
    assert (old / "artifact").exists()


def test_source_replacement_race_rejected(setup):
    policy, _, _, _ = setup
    old = data_directory(policy)
    planned = candidate(old)
    old.rename(old.with_name("saved"))
    replacement = data_directory(policy)
    with pytest.raises(MaintenanceError, match="candidate_inode_changed"):
        core.secure_delete(planned, policy, time.monotonic() + 20)
    assert (replacement / "artifact").exists()


def test_delete_budget_prevents_first_mutation(setup):
    policy, _, _, _ = setup
    old = data_directory(policy)
    with pytest.raises(MaintenanceError, match="deletion_budget_exceeded"):
        core.secure_delete(candidate(old), policy, time.monotonic() - 1)
    assert (old / "artifact").exists()


def test_protected_overlap_and_broad_roots_are_rejected(setup):
    policy, candidates, _, _ = setup
    old = data_directory(policy)
    policy["inventory"]["pinned_paths"] = [str(old / "artifact")]
    candidates.append(candidate(old))
    with pytest.raises(MaintenanceError, match="candidate_protected_overlap"):
        core.make_plan(policy)
    policy["inventory"]["roots"]["crm_recovery"] = "/root"
    with pytest.raises(MaintenanceError, match="inventory_root_too_broad"):
        core.validate_policy(policy)


def test_own_runner_excluded_from_runtime_and_health(setup):
    policy, _, _, _ = setup
    policy["inventory"]["units"] = [policy["ci"]["runner_unit"]]
    with pytest.raises(MaintenanceError, match="ci_runner_in_application_baseline"):
        core.validate_policy(policy)


def test_policy_owner_and_mode_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(util, "ROOT_UID", os.geteuid())
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "policy.json"
    util.atomic_json(path, core.DEFAULT_POLICY)
    assert core.load_policy(path)["schema"] == core.POLICY_SCHEMA
    directory.chmod(0o755)
    with pytest.raises(MaintenanceError, match="private_directory_invalid"):
        core.load_policy(path)
    directory.chmod(0o700)
    monkeypatch.setattr(util, "ROOT_UID", os.geteuid() + 1)
    with pytest.raises(MaintenanceError, match="private_directory_invalid"):
        core.load_policy(path)


def test_f_bavail_is_used_without_reserved_blocks(monkeypatch):
    class Space:
        f_bavail = 20
        f_bfree = 400
        f_frsize = 4096

    monkeypatch.setattr(core.os, "statvfs", lambda value: Space())
    assert core.available_bytes(core.DEFAULT_POLICY) == 20 * 4096


def test_docker_delete_no_force_exact_tags_and_identity_drift(monkeypatch):
    image = "sha256:" + "a" * 64
    tags = {"autostopcrm:aaaaaaaaaaaa", "autostopcrm:bbbbbbbbbbbb"}
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        if "inspect" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps(image) + " " + json.dumps(sorted(tags)), "")
        tags.remove(argv[-1])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(core, "run", command)
    planned = {"image_id": image, "identity": {"tags": sorted(tags)}}
    core.delete_image(planned, time.monotonic() + 30)
    assert not tags and all("--force" not in argv and "prune" not in argv for argv in calls)
    tags.add("unknown:latest")
    with pytest.raises(MaintenanceError, match="candidate_image_identity_changed"):
        core.delete_image(planned, time.monotonic() + 30)
    assert tags == {"unknown:latest"}


def test_cli_redacts_raw_external_error(setup, monkeypatch, capsys):
    policy, _, _, _ = setup
    entry_path = Path(__file__).parents[1] / "scripts" / "autostop-disk-maintenance.py"
    spec = importlib.util.spec_from_file_location("disk_maintenance_entry", entry_path)
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    monkeypatch.setattr(entry.core, "load_policy", lambda path: policy)
    monkeypatch.setattr(
        entry.core,
        "maintain",
        lambda value: (_ for _ in ()).throw(entry.MaintenanceError("SYNTHETIC_SECRET raw stderr")),
    )
    assert entry.main(["maintain"]) == 2
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "operation_failed"}


def test_run_drops_inherited_notify_and_rejects_input_symlink(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "synthetic")
    monkeypatch.setenv("WATCHDOG_USEC", "100")
    monkeypatch.setenv("OTHER_SAFE_ENV", "retained")
    seen = []

    def process(argv, **kwargs):
        seen.append(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(util.subprocess, "run", process)
    util.run(["synthetic"])
    assert "NOTIFY_SOCKET" not in seen[0] and "WATCHDOG_USEC" not in seen[0]
    assert seen[0]["OTHER_SAFE_ENV"] == "retained"
    source = tmp_path / "data"
    source.write_bytes(b"synthetic")
    alias = tmp_path / "alias"
    alias.symlink_to(source)
    with pytest.raises(MaintenanceError, match="command_unavailable"):
        util.run(["synthetic"], input_file=alias)
    assert len(seen) == 1


def test_apt_busy_skips_cache_and_keeps_other_native_cleanup(setup, monkeypatch):
    policy, candidates, _, _ = setup
    cache = Path(policy["state_root"]).parent / ".ruff_cache"
    cache.mkdir(mode=0o700)
    (cache / "owned-cache").write_bytes(b"synthetic")
    policy["inventory"]["cache_roots"] = [str(cache)]
    crm = data_directory(policy)
    candidates.extend([candidate(cache, "cache_directory", "technical_cache"), candidate(crm)])
    original = core.cache_fence

    @contextmanager
    def busy(value):
        if value["kind"] == "cache_directory":
            raise MaintenanceError("apt_cache_busy")
        with original(value):
            yield

    monkeypatch.setattr(core, "cache_fence", busy)
    result = core.maintain(policy)
    assert result["ok"] and cache.exists() and not crm.exists()
    assert result["skipped"] == [{"component": "technical_cache", "reason": "apt_cache_busy"}]


def test_invalid_health_arguments_fail_closed(setup):
    policy, _, _, _ = setup
    policy["health"]["units"] = ["--all"]
    with pytest.raises(MaintenanceError, match="health_policy_invalid"):
        core.validate_policy(policy)


@pytest.mark.parametrize("missing", ["full", "crm", "store", "coherent_runtime", "pg"])
def test_missing_required_keeper_receipt_blocks_all_deletion(setup, monkeypatch, missing):
    policy, candidates, baseline, _ = setup
    first = data_directory(policy)
    candidates.append(candidate(first))
    if missing == "store":
        path = data_directory(policy, root="store_recovery")
        candidates.append(candidate(path, component="store"))
    elif missing == "coherent_runtime":
        path = data_directory(policy, root="manager_releases")
        candidates.append(candidate(path, "release_directory", "manager"))
    elif missing == "pg":
        path = Path(policy["inventory"]["roots"]["pg_dumps"]) / "synthetic.dump"
        path.write_bytes(b"synthetic")
        candidates.append(candidate(path, "pg_dump", "pg"))
    components = {"full", "crm", "store", "coherent_runtime", "pg"} - {missing}
    monkeypatch.setattr(
        core.inventory,
        "verify_protected",
        lambda value: {
            "ok": True,
            "receipts": [{"component": component, "ok": True} for component in components],
            "fingerprint": core.digest(baseline),
        },
    )
    result = core.maintain(policy)
    assert first.exists() and result["deleted"] == []
    assert result["errors"] == ["preserved_component_verification_missing"]


def test_missing_ci_only_keeper_receipt_does_not_block_native_crm(setup, monkeypatch):
    policy, candidates, baseline, _ = setup
    crm = data_directory(policy)
    store = data_directory(policy, root="store_recovery")
    candidates.extend([candidate(crm), candidate(store, component="store")])
    monkeypatch.setattr(
        core.inventory,
        "verify_protected",
        lambda value: {
            "ok": True,
            "receipts": [{"component": name, "ok": True} for name in ("full", "crm")],
            "fingerprint": core.digest(baseline),
        },
    )
    monkeypatch.setattr(
        core.ci, "pause", lambda *args, **kwargs: (_ for _ in ()).throw(MaintenanceError("ci_api_auth_failed"))
    )
    result = core.maintain(policy)
    assert result["ok"] and not crm.exists() and store.exists()


def test_pg_outer_cleanup_busy_records_private_pending(setup):
    policy, _, _, calls = setup
    with core.locked(Path(policy["locks"]["cleanup"])):
        result = core.pg_retain(policy)
    assert result["retention_pending"] and result["errors"] == ["native_lock_busy"]
    saved = util.read_json(Path(policy["state_root"]) / "last-retention.json", private=True)
    assert saved["retention_pending"] and not calls


def test_image_requires_both_application_keeper_receipts():
    image = {"kind": "docker_image", "proof": {"requires_ci": True}}
    assert core._required_receipts([image], ci_ready=True) == {"full", "crm", "store"}
    assert core._required_receipts([image], ci_ready=False) == set()
