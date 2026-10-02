from __future__ import annotations

import importlib.util
import json
import itertools
import os
import stat
import subprocess
import sqlite3
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/autostop-complete-backup.py"
SPEC = importlib.util.spec_from_file_location("complete_backup", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
backup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = backup
SPEC.loader.exec_module(backup)


class FakePostgres(backup.Postgres):
    sequence = itertools.count()

    def __init__(self, mutate=None, *, invalid=False):
        super().__init__()
        self.mutate, self.invalid = mutate, invalid
        self.verified = 0
        self.helper_calls = 0

    def check(self, layout):
        return {
            "autostopcrm": {"image": "synthetic-crm", "revision": "a" * 40},
            "autostop-app": {"image": "synthetic-store"},
            "autostop-db": {"image": "synthetic-db"},
            "store_source_revision": "c" * 40,
        }

    def helper(self, layout, guard):
        self.helper_calls += 1
        self.canonical_attempted = True
        guard()
        destination = layout.daily / ("autostop24-20261003T" + f"{next(self.sequence):06d}" + "Z.dump")
        destination.write_bytes(b"synthetic-pg-dump")
        destination.chmod(0o600)
        self.canonical_succeeded = True
        if self.mutate:
            self.mutate()

    def verify(self, dump):
        self.verified += 1
        if self.invalid or dump.read_bytes() != b"synthetic-pg-dump":
            raise backup.BackupError("backup_postgres_command_failed")


@pytest.fixture
def layout(tmp_path):
    root = tmp_path / "bundles"
    crm = tmp_path / "crm"
    manager = tmp_path / "manager.sqlite3"
    scheduler = tmp_path / "registry.sqlite3"
    roles, uploads, photos, daily = [tmp_path / name for name in ["roles", "uploads", "photos", "daily"]]
    for directory in [crm, roles, uploads, photos, daily]:
        directory.mkdir(mode=0o700)
    for name in ["attachments", "shared-files", "repair-orders"]:
        directory = crm / name
        directory.mkdir()
        (directory / "synthetic.txt").write_text(name)
    (crm / "state.json").write_text(json.dumps({"cards": []}))
    (crm / "shared_files_index.json").write_text(json.dumps({"files": []}))
    (crm / "state.lock").touch(mode=0o644)
    (uploads / "synthetic.txt").write_text("store upload")
    (roles / "current-state.md").write_text("synthetic technical state")
    (crm / "state.json").write_text(json.dumps({"cards": [], "auth": "secret-not-for-output"}))
    baseline = daily / "autostop24-20000101T000000Z.dump"
    baseline.write_bytes(b"baseline")
    baseline.chmod(0o600)
    for path in [manager, scheduler, crm / "change_feed.sqlite3"]:
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE fixture(value TEXT)")
            connection.execute("INSERT INTO fixture VALUES ('synthetic')")
        path.chmod(0o600)
    revision = tmp_path / "REVISION"
    revision.write_text("a" * 40)
    return backup.Layout(
        root, crm, manager, scheduler, roles, uploads, photos, daily, tmp_path / "postgres.lock", revision
    )


def test_complete_backup_covers_files_sqlites_and_empty_store_volume(layout):
    backend = FakePostgres()
    result = backup.Backup(layout, reserve=0, postgres=backend).create()
    directory = Path(result["backup"])
    manifest = backup.verify(directory, backend)
    assert manifest["global_atomic_snapshot"] is False
    assert manifest["components"]["crm"]["sqlite_restore_metadata"]["change_feed.sqlite3"] == backup.restore_metadata(
        layout.crm / "change_feed.sqlite3"
    )
    assert manifest["components"]["manager"]["sqlite_restore_metadata"] == backup.restore_metadata(layout.manager)
    assert manifest["components"]["scheduler"]["sqlite_restore_metadata"] == backup.restore_metadata(layout.scheduler)
    assert backup.REQUIRED_ARTIFACTS <= manifest["artifacts"].keys()
    assert {
        "attachments/synthetic.txt",
        "shared-files/synthetic.txt",
        "repair-orders/synthetic.txt",
    } <= backup.archive_hashes(directory / "crm-files.tar.gz").keys()
    assert backup.archive_hashes(directory / "photos.tar.gz") == {}
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in directory.rglob("*") if path.is_file())
    assert directory.stat().st_mode & 0o777 == 0o700
    assert not list(layout.root.glob(".partial-*"))
    assert "secret-not-for-output" not in json.dumps(result)
    assert "secret-not-for-output" not in json.dumps(manifest)


def test_changed_store_file_across_dump_prevents_publish_and_preserves_old_bundle(layout):
    first = backup.Backup(layout, reserve=0, postgres=FakePostgres()).create()
    backend = FakePostgres(lambda: (layout.uploads / "synthetic.txt").write_text("changed during dump"))
    with pytest.raises(backup.BackupError, match="backup_store_files_changed_during_dump"):
        backup.Backup(layout, reserve=0, retention=1, postgres=backend).create()
    assert Path(first["backup"]).exists()
    assert len([p for p in layout.root.iterdir() if backup.OWNED_ID.fullmatch(p.name)]) == 1
    assert not list(layout.root.glob(".partial-*"))


def test_crm_shared_file_changes_during_sqlite_copy_prevent_publish(layout, monkeypatch):
    original = backup.sqlite_backup

    def mutate(source, destination, guard):
        original(source, destination, guard)
        if source == layout.crm / "change_feed.sqlite3":
            (layout.crm / "shared-files/synthetic.txt").write_text("independent writer changed file")

    monkeypatch.setattr(backup, "sqlite_backup", mutate)
    with pytest.raises(backup.BackupError, match="backup_crm_files_changed_during_sqlite_snapshot"):
        backup.Backup(layout, reserve=0, postgres=FakePostgres()).create()
    assert not list(layout.root.glob(".partial-*"))
    assert not any(backup.OWNED_ID.fullmatch(p.name) for p in layout.root.iterdir())


def test_capacity_failure_does_not_create_or_prune_backup_root(layout):
    with pytest.raises(backup.BackupError, match="backup_capacity_exhausted"):
        backup.Backup(layout, reserve=10**18, postgres=FakePostgres()).create()
    assert not layout.root.exists()


def test_invalid_pg_dump_preserves_prior_backups(layout):
    first = backup.Backup(layout, reserve=0, postgres=FakePostgres()).create()
    with pytest.raises(backup.BackupError, match="backup_postgres_command_failed"):
        backup.Backup(layout, reserve=0, retention=1, postgres=FakePostgres(invalid=True)).create()
    assert Path(first["backup"]).exists()
    assert not list(layout.root.glob(".partial-*"))


def test_retention_prunes_only_verified_owned_bundles_after_success(layout):
    runner = backup.Backup(layout, reserve=0, retention=1, postgres=FakePostgres())
    first = runner.create()
    foreign = layout.root / "manual-preserve"
    foreign.mkdir()
    unknown = layout.root / "20000101T000000Z-aaaaaaaaaaaa"
    unknown.mkdir(mode=0o700)
    (unknown / "manifest.json").write_text("unknown format")
    result = runner.create()
    assert not Path(first["backup"]).exists()
    assert Path(result["backup"]).exists()
    assert foreign.exists() and unknown.exists()


@pytest.mark.parametrize("manifest", [[], {}, {"format": backup.FORMAT, "id": 42}])
def test_retention_preserves_malformed_foreign_manifests_without_failing_new_backup(layout, manifest):
    runner = backup.Backup(layout, reserve=0, retention=1, postgres=FakePostgres())
    runner.create()
    unknown = layout.root / "20000101T000000Z-bbbbbbbbbbbb"
    unknown.mkdir(mode=0o700)
    path = unknown / "manifest.json"
    path.write_text(json.dumps(manifest))
    path.chmod(0o600)
    result = runner.create()
    assert Path(result["backup"]).exists() and unknown.exists()


@pytest.mark.parametrize("source", ["crm", "uploads", "root"])
def test_source_or_output_symlink_is_rejected(layout, source, tmp_path):
    target = getattr(layout, source)
    if target.exists():
        moved = tmp_path / (source + "-real")
        target.rename(moved)
    else:
        moved = tmp_path / "external-backups"
        moved.mkdir()
    target.symlink_to(moved, target_is_directory=True)
    with pytest.raises(backup.BackupError):
        backup.Backup(layout, reserve=0, postgres=FakePostgres()).create()
    assert not (moved / ".backup.lock").exists()


def test_corruption_is_detected_independently(layout):
    backend = FakePostgres()
    result = backup.Backup(layout, reserve=0, postgres=backend).create()
    directory = Path(result["backup"])
    (directory / "scheduler.sqlite3").write_bytes(b"corrupt")
    with pytest.raises(backup.BackupError, match="backup_hash_mismatch"):
        backup.verify(directory, backend)


def test_pending_crm_deploy_aborts_before_backup(layout):
    (layout.crm / ".agent-gateway-maintenance").touch()
    with pytest.raises(backup.BackupError, match="backup_deploy_in_progress"):
        backup.Backup(layout, reserve=0, postgres=FakePostgres()).create()
    assert not layout.root.exists()


def test_sqlite_backup_captures_committed_wal_without_copying_live_sidecars(tmp_path):
    source, destination = tmp_path / "live.sqlite3", tmp_path / "snapshot.sqlite3"
    with sqlite3.connect(source) as live:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("CREATE TABLE fixture(value TEXT)")
        live.execute("INSERT INTO fixture VALUES ('committed-in-wal')")
        live.commit()
        backup.sqlite_backup(source, destination)
        with sqlite3.connect(destination) as restored:
            assert restored.execute("SELECT value FROM fixture").fetchone() == ("committed-in-wal",)
    assert not destination.with_name(destination.name + "-wal").exists()


def test_lock_contention_aborts_with_bounded_timeout(tmp_path):
    path = tmp_path / "state.lock"
    with backup.lock(path):
        with pytest.raises(backup.BackupError, match="backup_lock_busy"):
            with backup.lock(path, timeout=0.01):
                pytest.fail("should not acquire another process file lock")


def test_runtime_release_change_during_capture_preserves_old_backup(layout):
    first = backup.Backup(layout, reserve=0, postgres=FakePostgres()).create()
    backend = FakePostgres(lambda: layout.revision.write_text("b" * 40))
    with pytest.raises(backup.BackupError, match="backup_runtime_revision_changed"):
        backup.Backup(layout, reserve=0, retention=1, postgres=backend).create()
    assert Path(first["backup"]).exists()
    assert not list(layout.root.glob(".partial-*"))


def test_missing_live_crm_lock_is_not_created_by_root(layout):
    path = layout.crm / "state.lock"
    path.unlink()
    with pytest.raises(FileNotFoundError):
        backup.Backup(layout, reserve=0, postgres=FakePostgres()).create()
    assert not path.exists()
    assert not list(layout.root.glob(".partial-*"))


def test_crm_lock_budget_aborts_and_releases_live_lock(layout, monkeypatch):
    clock = iter(range(0, 10000, 2))
    monkeypatch.setattr(backup.time, "monotonic", lambda: next(clock))
    with pytest.raises(backup.BackupError, match="backup_crm_lock_budget_exceeded"):
        backup.Backup(layout, reserve=0, postgres=FakePostgres()).create()
    with backup.lock(layout.crm / "state.lock", timeout=0, create=False):
        pass
    assert not list(layout.root.glob(".partial-*"))


def test_verifier_rejects_missing_coverage_and_artifact_path_escape(layout):
    backend = FakePostgres()
    result = backup.Backup(layout, reserve=0, postgres=backend).create()
    directory = Path(result["backup"])
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["artifacts"].pop("photos.tar.gz")
    path.write_text(json.dumps(manifest))
    with pytest.raises(backup.BackupError, match="backup_manifest_coverage_missing"):
        backup.verify(directory, backend)
    manifest["artifacts"]["photos.tar.gz"] = {}
    manifest["artifacts"] = {"../outside": {"bytes": 0, "sha256": "a" * 64}, **manifest["artifacts"]}
    path.write_text(json.dumps(manifest))
    with pytest.raises(backup.BackupError, match="backup_manifest_invalid"):
        backup.verify(directory, backend)


def test_crm_state_is_required_for_valid_coverage(layout):
    (layout.crm / "state.json").unlink()
    with pytest.raises(backup.BackupError, match="backup_manifest_coverage_missing"):
        backup.Backup(layout, reserve=0, postgres=FakePostgres()).create()
    assert not list(layout.root.glob(".partial-*"))


def test_canonical_dump_is_fresh_hardlink_and_helper_can_take_its_own_lock(layout):
    class CheckLock(FakePostgres):
        def helper(self, layout, guard):
            with backup.lock(layout.pg_lock, timeout=0):
                super().helper(layout, guard)

    backend = CheckLock()
    result = backup.Backup(layout, reserve=0, postgres=backend).create()
    bundle = Path(result["backup"])
    manifest = backup.verify(bundle, backend)
    source = layout.daily / manifest["components"]["store"]["canonical_dump"]
    assert os.path.samefile(source, bundle / "store.dump")
    assert source.stat().st_nlink == 2
    assert result["non_postgres_bytes"] == result["bytes"] - source.stat().st_size
    assert (layout.daily / "autostop24-20000101T000000Z.dump").exists()


@pytest.mark.parametrize("fresh_count", [0, 2])
def test_stale_or_ambiguous_canonical_dump_is_never_selected(layout, fresh_count):
    class Ambiguous(FakePostgres):
        def helper(self, layout, guard):
            for _ in range(fresh_count):
                super().helper(layout, guard)

    with pytest.raises(backup.BackupError, match="backup_fresh_canonical_dump_ambiguous"):
        backup.Backup(layout, reserve=0, postgres=Ambiguous()).create()
    assert not list(layout.root.glob(".partial-*"))


def test_canonical_helper_failure_preserves_previous_bundle(layout):
    first = backup.Backup(layout, reserve=0, postgres=FakePostgres()).create()

    class Failed(FakePostgres):
        def helper(self, layout, guard):
            raise backup.BackupError("backup_postgres_command_failed")

    with pytest.raises(backup.BackupError, match="backup_postgres_command_failed"):
        backup.Backup(layout, reserve=0, retention=1, postgres=Failed()).create()
    assert Path(first["backup"]).exists()


def test_hardlink_is_created_under_canonical_lock(layout, monkeypatch):
    original = backup.os.link

    def assert_locked(source, destination):
        with pytest.raises(backup.BackupError, match="backup_lock_busy"):
            with backup.lock(layout.pg_lock, timeout=0):
                pass
        original(source, destination)

    monkeypatch.setattr(backup.os, "link", assert_locked)
    backup.Backup(layout, reserve=0, postgres=FakePostgres()).create()


def test_container_change_during_capture_rejects_bundle(layout):
    class Changed(FakePostgres):
        checks = 0

        def check(self, layout):
            self.checks += 1
            result = super().check(layout)
            result["autostopcrm"]["image"] = f"revision-{self.checks}"
            return result

    with pytest.raises(backup.BackupError, match="backup_container_identity_changed"):
        backup.Backup(layout, reserve=0, postgres=Changed()).create()
    assert not list(layout.root.glob(".partial-*"))


def test_exact_canonical_helper_identity_and_permissions_are_required(tmp_path, monkeypatch):
    helper = tmp_path / "canonical-helper"
    helper.write_text("#!/bin/sh\nexit 0\n")
    helper.chmod(0o750)
    layout = backup.Layout(helper=helper)
    monkeypatch.setattr(backup, "HELPER_SHA256", backup.digest(helper))
    backend = backup.Postgres()
    if os.geteuid() != 0:
        with pytest.raises(backup.BackupError, match="backup_canonical_helper_unsafe"):
            backend.check_helper(layout)
        original = Path.lstat

        def root_owned(path):
            info = original(path)
            if path == helper:
                fields = list(info)
                fields[stat.ST_UID] = 0
                return os.stat_result(fields)
            return info

        monkeypatch.setattr(Path, "lstat", root_owned)
    backend.check_helper(layout)
    helper.write_text("#!/bin/sh\nexit 1\n")
    with pytest.raises(backup.BackupError, match="backup_canonical_helper_changed"):
        backend.check_helper(layout)
    helper.chmod(0o770)
    with pytest.raises(backup.BackupError, match="backup_canonical_helper_unsafe"):
        backend.check_helper(layout)


def test_daily_preserves_canonical_backup_despite_full_capacity_failure(layout):
    backend = FakePostgres()
    result = backup.Backup(layout, reserve=10**18, postgres=backend).daily()
    assert result == {
        "ok": False,
        "error": "backup_capacity_exhausted",
        "canonical_attempted": True,
        "canonical_succeeded": True,
    }
    assert backend.helper_calls == 1
    assert not layout.root.exists()


def test_daily_later_crm_failure_does_not_repeat_successful_canonical_helper(layout):
    first = backup.Backup(layout, reserve=0, postgres=FakePostgres()).create()
    (layout.crm / "state.lock").unlink()
    backend = FakePostgres()
    result = backup.Backup(layout, reserve=0, retention=1, postgres=backend).daily()
    assert result["ok"] is False and result["canonical_succeeded"] is True
    assert backend.helper_calls == 1
    assert Path(first["backup"]).exists()


def test_daily_failed_canonical_helper_is_never_retried(layout):
    class Failed(FakePostgres):
        def helper(self, layout, guard):
            self.helper_calls += 1
            self.canonical_attempted = True
            raise backup.BackupError("backup_postgres_command_failed")

    backend = Failed()
    result = backup.Backup(layout, reserve=0, postgres=backend).daily()
    assert result["ok"] is False and result["canonical_succeeded"] is False
    assert result["canonical_attempted"] is True and backend.helper_calls == 1


def test_daily_unsupported_full_runtime_still_runs_canonical_once(layout):
    backend = FakePostgres()
    result = backup.Backup(layout, reserve=0, postgres=backend).daily(runtime_supported=False)
    assert result["error"] == "backup_supported_sqlite_runtime_required"
    assert result["canonical_succeeded"] is True and backend.helper_calls == 1


def test_payload_filenames_are_opaque_in_every_application_file_tree(layout):
    names = ["payload.lock", "payload-wal", "payload-shm", "payload-journal", "payload.db", "payload.sqlite3"]
    directories = [
        layout.crm / "shared-files",
        layout.crm / "attachments",
        layout.crm / "repair-orders",
        layout.uploads,
        layout.photos,
    ]
    for directory in directories:
        for name in names:
            (directory / name).write_bytes(b"opaque synthetic payload, not a SQLite database")
    (layout.crm / "change_feed.sqlite3-shm").write_bytes(b"native sidecar excluded")
    backend = FakePostgres()
    result = backup.Backup(layout, reserve=0, postgres=backend).create()
    bundle = Path(result["backup"])
    backup.verify(bundle, backend)
    crm_names = backup.archive_hashes(bundle / "crm-files.tar.gz")
    for directory in directories[:3]:
        assert {directory.name + "/" + name for name in names} <= crm_names.keys()
    assert "change_feed.sqlite3-shm" not in crm_names
    assert "state.lock" not in crm_names
    assert set(names) <= backup.archive_hashes(bundle / "uploads.tar.gz").keys()
    assert set(names) <= backup.archive_hashes(bundle / "photos.tar.gz").keys()


@pytest.mark.parametrize("crm_binding", ["correct", "wrong_source", "wrong_destination"])
def test_crm_live_mount_must_match_backup_source(layout, monkeypatch, crm_binding):
    backend = backup.Postgres()
    monkeypatch.setattr(backend, "check_helper", lambda _: None)

    def observe(args, **kwargs):
        if args[0] == "git":
            output = "c" * 40
        elif args[-2] == "{{.State.Health.Status}}":
            output = "healthy"
        else:
            name = args[-1]
            mounts = []
            if name == "autostopcrm":
                mounts = [
                    {
                        "Type": "bind",
                        "Source": str(layout.crm) if crm_binding != "wrong_source" else "/wrong-data",
                        "Destination": "/home/autostop/.minimal-kanban"
                        if crm_binding != "wrong_destination"
                        else "/app/data",
                    }
                ]
            elif name == "autostop-app":
                mounts = [
                    {"Type": "volume", "Source": str(layout.uploads), "Destination": "/var/data/uploads"},
                    {
                        "Type": "volume",
                        "Source": str(layout.photos),
                        "Destination": "/var/data/quote-request-vin-photos",
                    },
                ]
            output = json.dumps(
                {
                    "id": name,
                    "image": "synthetic",
                    "started_at": "2026-10-03",
                    "running": True,
                    "mounts": mounts,
                    "revision": "a" * 40,
                }
            )
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(backup.subprocess, "run", observe)
    if crm_binding == "correct":
        assert backend.check(layout)["autostopcrm"]["running"] is True
    else:
        with pytest.raises(backup.BackupError, match="backup_crm_mount_invalid"):
            backend.check(layout)


@pytest.mark.skipif(os.geteuid() != 0, reason="real effective-identity integration requires root")
def test_online_snapshot_does_not_create_root_owned_sidecars_and_restores_identity(tmp_path):
    source_dir, target_dir = tmp_path / "source", tmp_path / "target"
    source_dir.mkdir(mode=0o700)
    target_dir.mkdir(mode=0o700)
    source, target = source_dir / "fixture.sqlite3", target_dir / "snapshot.sqlite3"
    connection = sqlite3.connect(source)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE fixture(value TEXT)")
    connection.execute("INSERT INTO fixture VALUES ('committed-only-in-wal')")
    connection.commit()
    # Keep all source ancestors accessible to the application UID while the
    # independent destination remains root-only. Fixtures contain no live data.
    os.chown(tmp_path, 10001, 10001)
    for ancestor in tmp_path.parents:
        if ancestor.name.startswith("pytest-"):
            ancestor.chmod(0o711)
    os.chown(source_dir, 10001, 10001)
    for path in source_dir.iterdir():
        os.chown(path, 10001, 10001)
    before = os.geteuid(), os.getegid()
    backup.sqlite_backup(source, target)
    assert (os.geteuid(), os.getegid()) == before
    assert target.stat().st_uid == 0 and target.stat().st_mode & 0o777 == 0o600
    assert all(
        path.stat().st_uid == 10001 and path.stat().st_gid == 10001
        for path in source_dir.iterdir()
        if path.name.endswith(("-wal", "-shm"))
    )
    with sqlite3.connect(f"file:{target}?immutable=1", uri=True) as restored:
        assert restored.execute("SELECT value FROM fixture").fetchone() == ("committed-only-in-wal",)
    connection.close()
    assert not source.with_name(source.name + "-wal").exists()
    backup.sqlite_backup(source, target_dir / "second.sqlite3")
    sidecars = [path for path in source_dir.iterdir() if path.name.endswith(("-wal", "-shm"))]
    assert sidecars and all(path.stat().st_uid == 10001 and path.stat().st_gid == 10001 for path in sidecars)

    def fail():
        raise backup.BackupError("synthetic_guard_failure")

    with pytest.raises(backup.BackupError, match="synthetic_guard_failure"):
        backup.sqlite_backup(source, target_dir / "aborted.sqlite3", fail)
    assert (os.geteuid(), os.getegid()) == before
