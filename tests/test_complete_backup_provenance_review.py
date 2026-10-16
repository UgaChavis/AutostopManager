"""Reconcile complete-backup provenance with the selected deployment policy.

Use private synthetic paths, the real Postgres.check and Backup.create, real
SQLite/tar/hash/fsync/link/rename, and a closed subprocess observation boundary.
A layout adapter supplies the future receipt field even on the pre-port source;
missing constructor fields therefore cannot masquerade as policy failures.
The synthetic Git boundary returns a valid, different checkout SHA so baseline
failures concern policy, not a missing executable or malformed fixture.

Seventeen runs cover fourteen receipt/legacy checks and three create paths:
sixteen distinct provenance situations plus one healthy publication repetition.
No live marker, environment, Docker, helper, archive or network is accessed.
The canonical helper's root ownership is normalized only for that synthetic
leaf on non-root test hosts; receipt ownership and permissions remain real.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
from types import SimpleNamespace

import autostop_manager
import pytest


SCRIPT = Path(autostop_manager.__file__).resolve().parents[1] / "scripts/autostop-complete-backup.py"
SPEC = importlib.util.spec_from_file_location("complete_backup_provenance_review", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
backup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = backup
SPEC.loader.exec_module(backup)

DEPLOY_SHA = "a" * 40
CHECKOUT_SHA = "c" * 40
IMAGE = "sha256:" + "1" * 64
DUMP = b"synthetic-postgres-dump-for-provenance"
PRIVATE_VALUE = "synthetic-receipt-only-value"
BINDING_ERROR = "backup_store_deploy_binding_invalid"
MARKER_ERROR = "backup_store_deploy_marker_invalid"
PRIVATE_ERROR = "backup_private_path_invalid"


@dataclass(frozen=True)
class ReceiptLayout(backup.Layout):
    """Supply the future field without changing the product Layout or its checks."""

    store_deploy_marker: Path | None = None


def write_marker(path, *, revision=DEPLOY_SHA, image=IMAGE):
    path.write_text(
        f"git_sha={revision}\nimage_id={image}\nbackup_dir={PRIVATE_VALUE}\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


class SyntheticPostgres(backup.Postgres):
    """Only produce synthetic canonical dump bytes; check/dump/verify stay real."""

    def __init__(self):
        super().__init__()
        self.helper_calls = 0
        self.after_dump = None

    def helper(self, layout, guard):
        self.check_helper(layout)
        self.canonical_attempted = True
        self.helper_calls += 1
        guard()
        path = layout.daily / f"autostop24-20000101T{self.helper_calls:06d}Z.dump"
        path.write_bytes(DUMP)
        path.chmod(0o600)
        self.canonical_succeeded = True
        if self.after_dump is not None:
            self.after_dump()


def root_owned_helper_only(monkeypatch, helper):
    """Match the existing test_complete_backup helper-authentication fixture."""
    if os.geteuid() == 0:
        return
    original = Path.lstat

    def lstat(path):
        info = original(path)
        if path == helper:
            fields = list(info)
            fields[stat.ST_UID] = 0
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", lstat)


def make_layout(tmp_path):
    crm = tmp_path / "crm"
    roles, uploads, photos, daily, source = [
        tmp_path / name for name in ("roles", "uploads", "photos", "daily", "archive-source")
    ]
    for directory in (crm, roles, uploads, photos, daily, source):
        directory.mkdir(mode=0o700)
    agent = crm / "agent"
    agent.mkdir(mode=0o700)
    (agent / "agent.lock").touch(mode=0o600)
    for name in backup.CRM_AGENT_FILES:
        (agent / name).write_text("synthetic component data", encoding="utf-8")
    (crm / "state.lock").touch(mode=0o600)
    (crm / "state.json").write_text('{"cards": []}', encoding="utf-8")
    (uploads / "synthetic.txt").write_text("synthetic upload", encoding="utf-8")
    (roles / "synthetic.md").write_text("synthetic technical state", encoding="utf-8")
    (source / "synthetic.txt").write_text("deployed archive has no .git", encoding="utf-8")
    manager, scheduler = tmp_path / "manager.sqlite3", tmp_path / "scheduler.sqlite3"
    for path in (manager, scheduler, crm / "change_feed.sqlite3"):
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE fixture(value TEXT)")
            connection.execute("INSERT INTO fixture VALUES ('synthetic')")
            connection.commit()
        finally:
            connection.close()
        path.chmod(0o600)
    baseline = daily / "autostop24-20000101T000000Z.dump"
    baseline.write_bytes(DUMP)
    baseline.chmod(0o600)
    revision, helper, marker = (tmp_path / name for name in ("REVISION", "canonical-helper", "receipt.env"))
    revision.write_text("b" * 40, encoding="utf-8")
    helper.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    helper.chmod(0o750)
    write_marker(marker)
    return ReceiptLayout(
        root=tmp_path / "bundles",
        crm=crm,
        manager=manager,
        scheduler=scheduler,
        roles=roles,
        uploads=uploads,
        photos=photos,
        daily=daily,
        pg_lock=tmp_path / "postgres.lock",
        revision=revision,
        helper=helper,
        store_source=source,
        store_deploy_marker=marker,
    )


def container_records(layout):
    def mount(source, destination, kind):
        return {"Type": kind, "Source": str(source), "Destination": destination}

    records = {
        "autostopcrm": {
            "mounts": [mount(layout.crm, "/home/autostop/.minimal-kanban", "bind")],
        },
        "autostop-app": {
            "mounts": [
                mount(layout.uploads, "/var/data/uploads", "volume"),
                mount(layout.photos, "/var/data/quote-request-vin-photos", "volume"),
            ],
        },
        "autostop-db": {"mounts": []},
    }
    for index, (name, record) in enumerate(records.items(), start=2):
        record.update(
            id=f"synthetic-{name}",
            image="sha256:" + str(index) * 64,
            started_at="2026-10-17T00:00:00Z",
            revision="d" * 40,
            running=True,
        )
    records["autostop-app"]["image"] = IMAGE
    return records


@pytest.fixture
def provenance(tmp_path, monkeypatch):
    layout = make_layout(tmp_path)
    root_owned_helper_only(monkeypatch, layout.helper)
    monkeypatch.setattr(backup, "HELPER_SHA256", hashlib.sha256(layout.helper.read_bytes()).hexdigest())
    records, calls = container_records(layout), []

    def observe(args, **kwargs):
        calls.append(tuple(args))
        if args == ["git", "-C", str(layout.store_source), "rev-parse", "HEAD"]:
            assert kwargs["timeout"] == 15
            return subprocess.CompletedProcess(args, 0, CHECKOUT_SHA + "\n")
        if len(args) == 5 and args[:3] == ["docker", "inspect", "--format"] and args[-1] in records:
            assert kwargs["timeout"] == 15
            if args[3] == "{{.State.Health.Status}}":
                assert args[-1] == "autostop-db"
                return subprocess.CompletedProcess(args, 0, "healthy\n")
            return subprocess.CompletedProcess(args, 0, json.dumps(records[args[-1]]))
        if args[:5] == ["docker", "exec", "-i", "autostop-db", "pg_restore"]:
            assert args[5:] in (["--list"], ["--file=/dev/null", "--no-owner", "--no-privileges"])
            assert kwargs["stdin"].read() == DUMP
            return subprocess.CompletedProcess(args, 0)
        raise AssertionError(f"Unapproved synthetic subprocess boundary: {args!r}")

    def forbidden_process(*_args, **_kwargs):
        raise AssertionError("The provenance fixture must not start a process")

    monkeypatch.setattr(backup.subprocess, "run", observe)
    monkeypatch.setattr(backup.subprocess, "Popen", forbidden_process)
    return SimpleNamespace(layout=layout, backend=SyntheticPostgres(), calls=calls)


def assert_no_git(calls):
    assert not [args for args in calls if args[0] == "git"], calls


def test_archive_receipt_binds_exact_deployed_revision_without_git(provenance):
    layout = provenance.layout
    assert not (layout.store_source / ".git").exists()
    result = provenance.backend.check(layout)
    assert result["store_source_revision"] == DEPLOY_SHA
    assert result["autostop-app"]["image"] == IMAGE
    assert PRIVATE_VALUE not in json.dumps(result)
    assert_no_git(provenance.calls)


@pytest.mark.parametrize(
    "case,expected_code",
    [
        pytest.param("missing", None, id="missing-receipt"),
        pytest.param("empty", BINDING_ERROR, id="empty-receipt"),
        pytest.param("missing-image", BINDING_ERROR, id="missing-image-key"),
        pytest.param("duplicate-sha", MARKER_ERROR, id="duplicate-sha-key"),
        pytest.param("duplicate-image", MARKER_ERROR, id="duplicate-image-key"),
        pytest.param("invalid-sha", BINDING_ERROR, id="short-sha"),
        pytest.param("invalid-image", BINDING_ERROR, id="mutable-image-tag"),
        pytest.param("mismatch", BINDING_ERROR, id="different-immutable-image"),
        pytest.param("public-mode", PRIVATE_ERROR, id="world-readable-receipt"),
        pytest.param("oversized", MARKER_ERROR, id="over-65536-bytes"),
        pytest.param("symlink", PRIVATE_ERROR, id="receipt-symlink-inside-fixture"),
        pytest.param("directory", PRIVATE_ERROR, id="receipt-directory"),
    ],
)
def test_invalid_deployment_evidence_never_falls_back_to_git(provenance, case, expected_code):
    marker = provenance.layout.store_deploy_marker
    if case == "missing":
        marker.unlink()
    elif case == "empty":
        marker.write_text("", encoding="utf-8")
    elif case == "missing-image":
        marker.write_text(f"git_sha={DEPLOY_SHA}\n", encoding="utf-8")
    elif case == "duplicate-sha":
        marker.write_text(marker.read_text(encoding="utf-8") + f"git_sha={DEPLOY_SHA}\n", encoding="utf-8")
    elif case == "duplicate-image":
        marker.write_text(marker.read_text(encoding="utf-8") + f"image_id={IMAGE}\n", encoding="utf-8")
    elif case == "invalid-sha":
        write_marker(marker, revision="a" * 39)
    elif case == "invalid-image":
        write_marker(marker, image="autostop-app:latest")
    elif case == "mismatch":
        write_marker(marker, image="sha256:" + "9" * 64)
    elif case == "public-mode":
        marker.chmod(0o644)
    elif case == "oversized":
        marker.write_text(marker.read_text(encoding="utf-8") + "x" * 65537, encoding="utf-8")
    elif case == "symlink":
        target = marker.with_name("valid-receipt-target.env")
        marker.rename(target)
        marker.symlink_to(target)
    elif case == "directory":
        marker.unlink()
        marker.mkdir(mode=0o700)
    else:
        raise AssertionError(case)

    with pytest.raises((backup.BackupError, FileNotFoundError)) as caught:
        provenance.backend.check(provenance.layout)
    if expected_code is None:
        assert isinstance(caught.value, FileNotFoundError)
        assert caught.value.filename == str(marker)
    else:
        assert isinstance(caught.value, backup.BackupError)
        assert str(caught.value) == expected_code
    assert_no_git(provenance.calls)


def test_only_explicit_none_preserves_legacy_checkout_policy(provenance):
    layout = replace(provenance.layout, store_deploy_marker=None)
    result = provenance.backend.check(layout)
    assert result["store_source_revision"] == CHECKOUT_SHA
    assert [args for args in provenance.calls if args[0] == "git"] == [
        ("git", "-C", str(provenance.layout.store_source), "rev-parse", "HEAD")
    ]


def tree_hashes(directory):
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_healthy_create_publishes_only_selected_provenance(provenance):
    result = backup.Backup(provenance.layout, reserve=0, postgres=provenance.backend).create()
    assert result["ok"] is True
    published = Path(result["backup"])
    manifest = json.loads((published / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["store_source_revision"] == DEPLOY_SHA
    assert manifest["container_images"]["autostop-app"] == IMAGE
    assert manifest["global_atomic_snapshot"] is False
    assert PRIVATE_VALUE not in json.dumps(manifest)
    assert provenance.backend.helper_calls == 1
    assert not list(provenance.layout.root.glob(".partial-*"))
    assert_no_git(provenance.calls)


@pytest.mark.parametrize(
    "change", ["revision", "missing"], ids=["sha-changed-after-dump", "receipt-removed-after-dump"]
)
def test_create_rechecks_provenance_before_publishing_and_preserves_old_bundle(provenance, change):
    runner = backup.Backup(provenance.layout, reserve=0, postgres=provenance.backend)
    old = Path(runner.create()["backup"])
    old_hashes = tree_hashes(old)
    marker = provenance.layout.store_deploy_marker

    def mutate_receipt():
        if change == "revision":
            write_marker(marker, revision="e" * 40)
        else:
            marker.unlink()

    provenance.backend.after_dump = mutate_receipt
    with pytest.raises((backup.BackupError, FileNotFoundError)) as caught:
        runner.create()
    if change == "revision":
        assert isinstance(caught.value, backup.BackupError)
        assert str(caught.value) == "backup_container_identity_changed"
    else:
        assert isinstance(caught.value, FileNotFoundError)
        assert caught.value.filename == str(marker)
    assert provenance.backend.helper_calls == 2
    assert tree_hashes(old) == old_hashes
    assert [path for path in provenance.layout.root.iterdir() if backup.OWNED_ID.fullmatch(path.name)] == [old]
    assert not list(provenance.layout.root.glob(".partial-*"))
    assert_no_git(provenance.calls)
