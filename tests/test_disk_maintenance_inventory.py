from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from disk_maintenance import inventory as inv


@pytest.fixture(autouse=True)
def synthetic_owner(monkeypatch):
    monkeypatch.setattr(inv, "OWNER_UID", os.geteuid())


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    path.chmod(0o600)


def metadata(path):
    return {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def archive(path, name="synthetic.txt"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as out:
        header = tarfile.TarInfo(name)
        header.size = 7
        out.addfile(header, io.BytesIO(b"fixture"))
    path.chmod(0o600)


def database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE technical_fixture(value TEXT)")
    path.chmod(0o600)


def checksums(directory, names, filename="SHA256SUMS", *, comments=False):
    text = "# Technical fixture\n" if comments else ""
    text += "".join(f"{metadata(directory / name)['sha256']}  ./{name}\n" for name in names)
    write(directory / filename, text)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    roots = {}
    for name in ("pg_dumps", "full_bundles", "crm_recovery", "store_recovery"):
        path = tmp_path / name
        path.mkdir(mode=0o700)
        roots[name] = str(path)
    pg = Path(roots["pg_dumps"])
    dumps = {}
    for stamp in (
        "20261001T033000",
        "20261002T033000",
        "20261003T033000",
        "20261002T223627",
        "20261002T010000",
        "20261002T020000",
        "20261003T010000",
    ):
        path = pg / f"autostop24-{stamp}Z.dump"
        write(path, b"PGDMPsynthetic opaque dump")
        dumps[stamp] = path
    full = Path(roots["full_bundles"]) / ("20261002T223627Z-" + "f" * 12)
    full.mkdir(mode=0o700)
    os.link(dumps["20261002T223627"], full / "store.dump")
    for name in sorted(inv.FULL_REQUIRED - {"store.dump"}):
        if name.endswith(".tar.gz"):
            archive(full / name)
        else:
            database(full / name)
    write(
        full / "manifest.json",
        json.dumps(
            {
                "format": "autostop_complete_backup_v1",
                "id": full.name,
                "created_at": "2026-10-02T22:36:27+00:00",
                "complete": True,
                "components": {"store": {"canonical_dump": dumps["20261002T223627"].name}},
                "artifacts": {name: metadata(full / name) for name in inv.FULL_REQUIRED},
            }
        ),
    )
    crm_root = Path(roots["crm_recovery"])
    crm = []
    for date in ("20261001T143848Z", "20261002T164351Z"):
        directory = crm_root / f"{date}-{'a' * 12}-100"
        directory.mkdir(mode=0o700)
        write(directory / "state.json", '{"opaque_business_fixture":"do-not-interpret"}')
        database(directory / "autostop_manager.sqlite3")
        artifacts = dict.fromkeys(inv.CRM_ARTIFACTS)
        for key in ("state", "manager_sqlite"):
            name = inv.CRM_ARTIFACTS[key]
            item = metadata(directory / name)
            artifacts[key] = {"name": name, "size_bytes": item["bytes"], "sha256": item["sha256"]}
        write(
            directory / "manifest.json",
            json.dumps(
                {
                    "schema": "autostop-agent-release-backup.v5",
                    "backup_id": directory.name,
                    "created_at": "2026-10-02T16:50:00+00:00",
                    "complete": True,
                    "artifacts": artifacts,
                }
            ),
        )
        crm.append(directory)

    def image(char):
        return "sha256:" + char * 64

    crm_current, crm_previous, crm_old, store_current, store_previous, store_old, pg_image = map(image, "1234567")
    store = []
    for date, sha, old in [("20261001-084344", "b" * 40, store_old), ("20261001-165100", "c" * 40, store_previous)]:
        directory = Path(roots["store_recovery"]) / f"{date}-{sha}"
        directory.mkdir(mode=0o700)
        write(
            directory / "metadata.env",
            f"incoming_git_sha={sha}\nprevious_image_id={old}\nrollback_image_tag=autostop-app:rollback-{date}\nprevious_db_image_id={pg_image}\nrollback_db_image_tag=autostop-db:rollback-{date}\nSECRET_TOKEN=private-fixture-token\n",
        )
        for name in inv.STORE_REQUIRED - {"metadata.env"}:
            if name.endswith(".tar.gz"):
                archive(directory / name)
            elif name.endswith(".dump"):
                write(directory / name, b"PGDMPfixture")
            else:
                write(directory / name, "opaque fixture\n")
        # Store's native sha256sum files have no leading './'.
        write(
            directory / "SHA256SUMS",
            "".join(f"{metadata(directory / name)['sha256']}  {name}\n" for name in sorted(inv.STORE_REQUIRED)),
        )
        store.append(directory)
    marker = tmp_path / "current.env"
    write(
        marker,
        f"git_sha={'c' * 40}\nimage_id={store_current}\nbackup_dir={store[-1]}\nSECRET_TOKEN=private-fixture-token\n",
    )
    containers = [
        {
            "id": "container-crm",
            "image_id": crm_current,
            "name": "/autostopcrm",
            "running": True,
            "started_at": "immutable",
        },
        {
            "id": "container-store",
            "image_id": store_current,
            "name": "/autostop-app",
            "running": True,
            "started_at": "immutable",
        },
        {
            "id": "container-pg",
            "image_id": pg_image,
            "name": "/autostop-db",
            "running": True,
            "started_at": "immutable",
        },
    ]
    rows = [
        {"ID": crm_current, "Repository": "autostopcrm", "Tag": "a" * 12},
        {"ID": crm_previous, "Repository": "autostopcrm-rollback", "Tag": crm[-1].name},
        {"ID": crm_old, "Repository": "autostopcrm", "Tag": "crm-only-" + "e" * 12},
        {"ID": crm_old, "Repository": "autostopcrm-rollback", "Tag": crm[0].name},
        {"ID": store_current, "Repository": "autostop-app-app", "Tag": "latest"},
        {"ID": store_previous, "Repository": "autostop-app", "Tag": "rollback-20261001-165100"},
        {"ID": store_old, "Repository": "autostop-app", "Tag": "rollback-20261001-084344"},
        {"ID": pg_image, "Repository": "postgres", "Tag": "16"},
    ]
    calls = []

    def fake_checked(argv, **_kwargs):
        calls.append(argv)
        if argv[:3] == ["docker", "ps", "-aq"]:
            return "\n".join(row["id"] for row in containers)
        if argv[:2] == ["docker", "inspect"]:
            return "\n".join(json.dumps(row) for row in containers)
        if argv[:3] == ["docker", "image", "ls"]:
            return "\n".join(json.dumps(row) for row in rows)
        raise AssertionError(f"unexpected synthetic command: {argv[:3]}")

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        if argv[0] == "curl":
            result = {"Images": [{"Id": row["ID"], "Size": 10000, "SharedSize": 2000} for row in rows]}
            return subprocess.CompletedProcess(argv, 0, json.dumps(result), "")
        assert argv[:3] == ["docker", "exec", "-i"] and "pg_restore" in argv
        return subprocess.CompletedProcess(argv, 0, "technical-object-metadata", "")

    monkeypatch.setattr(inv, "checked_run", fake_checked)
    monkeypatch.setattr(inv, "run", fake_run)
    monkeypatch.setattr(inv, "process_references", lambda paths: {str(path): [] for path in paths})
    monkeypatch.setattr(inv, "unit_references", lambda paths: {str(path): [] for path in paths})
    policy = {
        "timezone": "Asia/Krasnoyarsk",
        "pg_keep_days": 3,
        "inventory": {
            "roots": roots,
            "store_deploy_marker": str(marker),
            "pinned_paths": [],
            "coherent_keep_paths": [],
            "units": [],
            "config_paths": [],
            "git_roots": [],
            "cache_roots": [],
            "expected_container_images": {row["name"].lstrip("/"): row["image_id"] for row in containers},
        },
    }
    return {
        "policy": policy,
        "dumps": dumps,
        "full": full,
        "crm": crm,
        "store": store,
        "rows": rows,
        "containers": containers,
        "calls": calls,
        "crm_old": crm_old,
        "pg_image": pg_image,
    }


def test_collect_keeps_distinct_local_days_plus_full_pin_and_latest_recoveries(lab):
    result = inv.collect(lab["policy"])
    dumps = {Path(row["path"]).name for row in result["protected"] if row.get("component") == "pg"}
    assert dumps == {
        f"autostop24-{date}Z.dump"
        for date in ("20261001T033000", "20261002T033000", "20261003T033000", "20261002T223627")
    }
    assert sum(row["kind"] == "pg_dump" for row in result["candidates"]) == 3
    assert result["keepers"]["crm"] == str(lab["crm"][-1])
    assert result["keepers"]["store"] == str(lab["store"][-1])
    assert "private-fixture-token" not in json.dumps(result)


def test_docker_inventory_groups_all_owned_aliases_and_protects_stopped_references(lab):
    result = inv.collect(lab["policy"])
    candidate = next(row for row in result["candidates"] if row.get("image_id") == lab["crm_old"])
    assert len(candidate["tags"]) == 2
    assert candidate["estimated_reclaim_bytes"] == 8000
    assert candidate["proof"]["requires_ci"] is True
    lab["containers"].append(
        {
            "id": "stopped",
            "image_id": lab["crm_old"],
            "name": "/stopped-container",
            "running": False,
            "started_at": "past",
        }
    )
    assert all(row.get("image_id") != lab["crm_old"] for row in inv.collect(lab["policy"])["candidates"])


def test_unknown_image_alias_prevents_entire_image_deletion(lab):
    lab["rows"].append({"ID": lab["crm_old"], "Repository": "protected-foreign", "Tag": "latest"})
    result = inv.collect(lab["policy"])
    assert all(row.get("image_id") != lab["crm_old"] for row in result["candidates"])
    assert any(row.get("reason") == "unknown_or_mixed_image_tags" for row in result["skipped"])


def test_cold_keeper_verification_never_interprets_business_json(lab):
    proof = inv.verify_protected(lab["policy"])
    assert proof["ok"] is True
    assert sum(row["component"] == "pg" for row in proof["receipts"]) == 4
    assert "private-fixture-token" not in json.dumps(proof)
    assert any("--file=/dev/null" in argv for argv in lab["calls"])


def test_keeper_corruption_blocks_cold_proof(lab):
    write(lab["full"] / "crm-files.tar.gz", b"corrupted-preserved-data")
    with pytest.raises(inv.MaintenanceError):
        inv.verify_protected(lab["policy"])


def test_pg_only_scopes_var_roots_and_never_reads_home_or_store(lab):
    lab["policy"]["operation"] = "pg-retain"
    lab["policy"]["inventory"]["roots"]["crm_recovery"] = "/unavailable/root/home"
    lab["policy"]["inventory"]["store_deploy_marker"] = "/unavailable/secret.env"
    lab["policy"]["inventory"]["pinned_paths"] = ["/unavailable/root/venv"]
    result = inv.collect(lab["policy"])
    assert all(row["kind"] == "pg_dump" for row in result["candidates"])
    assert inv.verify_protected(lab["policy"])["ok"] is True
    assert not any(argv[:3] == ["docker", "image", "ls"] for argv in lab["calls"])


@pytest.mark.parametrize("mutation", ["replace", "modify_restored_mtime"])
def test_revalidate_detects_pg_identity_change_even_with_restored_mtime(lab, mutation):
    row = next(row for row in inv.collect(lab["policy"])["candidates"] if row["kind"] == "pg_dump")
    path = Path(row["path"])
    before = path.stat()
    if mutation == "replace":
        path.unlink()
    write(path, b"PGDMPmutated opaque dump!")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(inv.MaintenanceError, match="candidate_identity_or_policy_changed"):
        inv.revalidate(row, lab["policy"])


def test_revalidate_checks_empty_mapping_correctly_and_blocks_fresh_reference(lab, monkeypatch):
    row = next(row for row in inv.collect(lab["policy"])["candidates"] if row["kind"] == "pg_dump")
    inv.revalidate(row, lab["policy"])
    monkeypatch.setattr(inv, "unit_references", lambda paths: {str(path): ["synthetic.service"] for path in paths})
    with pytest.raises(inv.MaintenanceError):
        inv.revalidate(row, lab["policy"])


@pytest.mark.parametrize("invalid", ["symlink", "hardlink", "nested_symlink", "mount"])
def test_candidate_tree_guards_symlinks_hardlinks_and_mounts(tmp_path, monkeypatch, invalid):
    root = tmp_path / "owned"
    root.mkdir()
    candidate = root / "candidate"
    candidate.mkdir()
    write(candidate / "payload", "opaque")
    if invalid == "symlink":
        candidate.rename(root / "actual")
        candidate.symlink_to(root / "actual", target_is_directory=True)
    elif invalid == "hardlink":
        os.link(candidate / "payload", root / "external-link")
    elif invalid == "nested_symlink":
        (candidate / "escape").symlink_to(tmp_path, target_is_directory=True)
    else:
        monkeypatch.setattr(inv, "_mounted_paths", lambda: {candidate / "payload"})
    with pytest.raises(inv.MaintenanceError):
        inv._guard(candidate, root)


def test_runtime_leaf_links_are_fingerprinted_without_following(tmp_path):
    root = tmp_path / "runtimes"
    root.mkdir()
    runtime = root / ("e" * 40)
    runtime.mkdir()
    target = tmp_path / "protected"
    write(target, "central interpreter")
    (runtime / "python").symlink_to(target)
    before = inv._guard(runtime, root, allow_links=True)
    assert inv._allocated(runtime, allow_links=True) < target.stat().st_blocks * 512 + 8192
    (runtime / "python").unlink()
    (runtime / "python").symlink_to(tmp_path / "other")
    assert inv._guard(runtime, root, allow_links=True) != before
    assert target.read_text() == "central interpreter"


def test_changed_current_image_requires_policy_refresh_and_protects_all_images(lab):
    lab["policy"]["inventory"]["expected_container_images"]["autostopcrm"] = "sha256:" + "f" * 64
    result = inv.collect(lab["policy"])
    assert not any(row["kind"] == "docker_image" for row in result["candidates"])
    assert any(row["reason"] == "policy_refresh_required" for row in result["protected"])


@pytest.mark.parametrize("name", ["../outside", "/absolute", "env/../../outside"])
def test_checksum_paths_cannot_escape_recovery_root(tmp_path, name):
    path = tmp_path / "recovery"
    path.mkdir()
    with pytest.raises(inv.MaintenanceError, match="artifact_path_invalid"):
        inv._member(path, name)


def test_model_checksums_support_native_comments_and_dot_slash(tmp_path):
    write(tmp_path / "model.bin", "synthetic model")
    checksums(tmp_path, ["model.bin"], comments=True)
    inv._verify_checksum_list(tmp_path, tmp_path / "SHA256SUMS")
    write(tmp_path / "model.bin", "mutated model")
    with pytest.raises(inv.MaintenanceError, match="preserved_hash_mismatch"):
        inv._verify_checksum_list(tmp_path, tmp_path / "SHA256SUMS")


def test_archive_verification_rejects_traversal_without_extracting(tmp_path):
    path = tmp_path / "unsafe.tar.gz"
    archive(path, "../escaped")
    with pytest.raises(inv.MaintenanceError, match="preserved_archive_structure_invalid"):
        inv._archive_check(path)
    assert not (tmp_path.parent / "escaped").exists()


def test_read_only_current_tuple_verifies_gitarchive_without_inventing_manifest(tmp_path):
    revision = "a" * 40
    manager_root, telegram_root, runtime_root = [tmp_path / name for name in ("managers", "telegrams", "runtimes")]
    for root in (manager_root, telegram_root, runtime_root):
        root.mkdir()
    manager = manager_root / f"20261002T164351Z-{'b' * 12}-100-manager-{revision[:12]}"
    telegram = telegram_root / f"20261002T165049Z-{revision[:12]}"
    runtime = runtime_root / revision
    manager.mkdir()
    telegram.mkdir()
    runtime.mkdir()
    names = [
        "autostop_manager/telegram_bridge.py",
        "deploy/systemd/autostop-work-telegram.service",
        "deploy/telegram/faster-whisper-small.sha256",
    ]
    for name in names:
        write(manager / name, "trusted synthetic source")
        write(telegram / name, "trusted synthetic source")
    write(manager / "REVISION", revision)
    checksums(manager, names, "MANIFEST.sha256")
    for marker in (".dependencies-ready", ".model-ready"):
        write(runtime / marker, "")
    write(runtime / "model/model.bin", "trusted offline model")
    checksums(runtime / "model", ["model.bin"], comments=True)
    (runtime / "model/SHA256SUMS").rename(runtime / "faster-whisper-small.sha256")
    (manager_root / "current").symlink_to(manager, target_is_directory=True)
    (telegram_root / "current").symlink_to(telegram, target_is_directory=True)
    policy = {
        "inventory": {
            "roots": {
                "manager_releases": str(manager_root),
                "work_tg_releases": str(telegram_root),
                "tg_runtimes": str(runtime_root),
            },
            "coherent_keep_paths": [[str(manager), str(telegram), str(runtime)]],
        }
    }
    result = inv.collect_current_tuple(policy)
    assert result["ok"] is True and result["policy_refresh_required"] is False
    assert not (telegram / "REVISION").exists() and not (telegram / "MANIFEST.sha256").exists()
    write(telegram / "autostop_manager/telegram_bridge.py", "untrusted source change")
    with pytest.raises(inv.MaintenanceError, match="work_release_source_mismatch"):
        inv.collect_current_tuple(policy)


def test_versioned_cleanup_skips_after_current_binding_changes(tmp_path):
    root = tmp_path / "releases"
    root.mkdir()
    current = root / ("20261002T164351Z-" + "a" * 12 + "-100-manager-" + "b" * 12)
    old = root / ("20261001T164351Z-" + "a" * 12 + "-100-manager-" + "c" * 12)
    current.mkdir()
    old.mkdir()
    (root / "current").symlink_to(current, target_is_directory=True)
    policy = {
        "inventory": {"roots": {"manager_releases": str(root)}, "coherent_keep_paths": [[str(old)], [str(current)]]}
    }
    candidates, protected, skipped = [], [], []
    inv._versioned(policy, candidates, protected, skipped)
    assert candidates == []
    assert all(row["reason"] == "policy_refresh_required" for row in protected)


def test_metadata_env_never_executes_shell_syntax(tmp_path):
    target = tmp_path / "should-not-exist"
    path = tmp_path / "metadata.env"
    write(path, f"SECRET=$(touch {target})\ngit_sha={'a' * 40}\n")
    assert inv._env(path) == {"git_sha": "a" * 40}
    assert not target.exists()


def test_native_process_reference_mapping_tracks_real_synthetic_open_fd(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("Native cross-process reference scan requires root")
    if not Path("/proc/self/fd").is_dir():
        pytest.skip("Linux procfs required")
    write(tmp_path / "technical-fixture", "synthetic")
    with (tmp_path / "technical-fixture").open("rb"):
        mapping = inv.process_references([tmp_path])
    assert "pid:" + str(os.getpid()) in mapping[str(tmp_path)]


def test_candidate_owner_guard_rejects_other_uid(tmp_path, monkeypatch):
    path = tmp_path / "owned-technical-fixture"
    write(path, "synthetic")
    monkeypatch.setattr(inv, "OWNER_UID", os.geteuid() + 1)
    with pytest.raises(inv.MaintenanceError, match="candidate_owner_or_mode_invalid"):
        inv._guard(path, tmp_path)


def test_native_unit_reference_mapping_uses_actual_shared_signature(tmp_path, monkeypatch):
    from disk_maintenance import util

    def checked(argv, **_kwargs):
        if argv[1] == "list-units":
            return "synthetic.service loaded active running Synthetic\n"
        return f"Id=synthetic.service\nWorkingDirectory={tmp_path}\nExecStart=/usr/bin/true\nEnvironmentFiles=\n"

    monkeypatch.setattr(util, "checked_run", checked)
    assert inv.unit_references([tmp_path]) == {str(tmp_path): ["synthetic.service"]}


def test_git_readback_is_command_locally_trusted_and_cannot_run_repository_fsmonitor(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(inv, "checked_run", lambda argv, **_kwargs: commands.append(argv) or "synthetic")
    assert inv._git_read(str(tmp_path), "status", "--porcelain") == "synthetic"
    command = commands[0]
    assert "--no-optional-locks" in command
    assert "safe.directory=" + str(tmp_path) in command
    assert "core.fsmonitor=false" in command
    assert "core.hooksPath=/dev/null" in command
    assert "--global" not in command
