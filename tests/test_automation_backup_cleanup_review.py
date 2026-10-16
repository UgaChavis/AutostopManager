"""Backup resource ownership and errors before/after publication, using private SQLite files."""

from __future__ import annotations

from contextlib import closing
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace

import pytest

import autostop_manager


@pytest.fixture
def backup_module():
    script = Path(autostop_manager.__file__).resolve().parents[1] / "scripts/backup-manager-automation-state.py"
    specification = importlib.util.spec_from_file_location("automation_backup_cleanup_review", script)
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.fixture
def case(backup_module, tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir(mode=0o700)
    source = source_dir / "registry.sqlite3"
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute(
            "CREATE TABLE manager_automation_schema (component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO manager_automation_schema VALUES (?, ?)", ("automation_center", 6))
        connection.execute("CREATE TABLE snapshot_origin (marker TEXT NOT NULL)")
        connection.execute("INSERT INTO snapshot_origin VALUES (?)", ("synthetic-requested",))
    source.chmod(0o600)
    output_dir = tmp_path / "outputs"
    output_dir.mkdir(mode=0o700)
    return SimpleNamespace(
        module=backup_module,
        source=source,
        source_bytes=source.read_bytes(),
        output=output_dir / "snapshot.sqlite3",
    )


def _identity(info):
    return info.st_dev, info.st_ino


def _descriptor_identity(descriptor):
    try:
        return _identity(os.fstat(descriptor))
    except OSError as error:
        assert error.errno == errno.EBADF
        return None


@pytest.fixture
def temporary_files(case, monkeypatch):
    original = case.module.tempfile.mkstemp
    captured = []

    def create(*args, **kwargs):
        descriptor, raw_path = original(*args, **kwargs)
        if kwargs.get("dir") == case.output.parent and kwargs.get("prefix") == ".automation-registry.":
            captured.append((descriptor, Path(raw_path), _identity(os.fstat(descriptor))))
        return descriptor, raw_path

    monkeypatch.setattr(case.module.tempfile, "mkstemp", create)
    try:
        yield captured
    finally:
        # On the failing baseline, release only the exact descriptor/inode and
        # temporary name created by this test. Never close a reused descriptor.
        for descriptor, temporary, identity in captured:
            if _descriptor_identity(descriptor) == identity:
                os.close(descriptor)
            try:
                info = temporary.lstat()
            except FileNotFoundError:
                continue
            if _identity(info) == identity:
                temporary.unlink()


def _assert_released(case, captured):
    assert len(captured) == 1
    descriptor, temporary, identity = captured[0]
    leaked = (_descriptor_identity(descriptor) == identity, temporary.exists())
    assert leaked == (False, False), {"descriptor_open": leaked[0], "temporary_exists": leaked[1]}
    assert not list(case.output.parent.glob(".automation-registry.*.tmp"))
    assert case.source.read_bytes() == case.source_bytes


def _assert_exact_output(case):
    info = case.output.lstat()
    assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_uid == os.geteuid()
    uri = case.output.absolute().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT version FROM manager_automation_schema WHERE component='automation_center'"
        ).fetchone() == (6,)
        assert connection.execute("SELECT marker FROM snapshot_origin").fetchall() == [("synthetic-requested",)]
    return _identity(info), hashlib.sha256(case.output.read_bytes()).hexdigest()


def _cli(case, capsys, expected_error):
    assert case.module.main(["--source", str(case.source), "--output", str(case.output)]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": False, "error": expected_error}
    assert captured.err == ""


def test_fchmod_failure_closes_descriptor_and_removes_private_temporary(case, temporary_files, monkeypatch, capsys):
    original = case.module.os.fchmod
    failures = []

    def fail_owned(descriptor, mode):
        if temporary_files and descriptor == temporary_files[0][0]:
            assert _descriptor_identity(descriptor) == temporary_files[0][2]
            failures.append(descriptor)
            raise OSError(errno.EIO, "synthetic fchmod failure")
        return original(descriptor, mode)

    monkeypatch.setattr(case.module.os, "fchmod", fail_owned)
    _cli(case, capsys, "automation_backup_failed")
    assert len(failures) == 1
    assert not case.output.exists()
    _assert_released(case, temporary_files)


def test_successful_backup_releases_temporary_and_publishes_exact_private_snapshot(case, temporary_files):
    report = case.module.backup(case.source, case.output)
    assert report == {
        "ok": True,
        "format": "autostop_manager_automation_backup_v1",
        "schema_version": 6,
        "output": str(case.output),
    }
    _assert_released(case, temporary_files)
    _assert_exact_output(case)


def test_file_fsync_failure_cleans_temporary_before_publication(case, temporary_files, monkeypatch, capsys):
    original = case.module.os.fsync
    failed_descriptors = []

    def fail_owned_file(descriptor):
        if temporary_files and _descriptor_identity(descriptor) == temporary_files[0][2]:
            assert stat.S_ISREG(os.fstat(descriptor).st_mode)
            assert not case.output.exists()
            failed_descriptors.append(descriptor)
            raise OSError(errno.EIO, "synthetic file fsync failure")
        return original(descriptor)

    monkeypatch.setattr(case.module.os, "fsync", fail_owned_file)
    _cli(case, capsys, "automation_backup_failed")
    assert len(failed_descriptors) == 1
    assert _descriptor_identity(failed_descriptors[0]) is None
    assert not case.output.exists()
    _assert_released(case, temporary_files)


def test_directory_fsync_error_preserves_published_snapshot_and_exact_retry(case, temporary_files, monkeypatch, capsys):
    original = case.module.os.fsync
    directory_identity = _identity(case.output.parent.stat())
    syncs = []
    directory_descriptors = []

    def fail_owned_directory(descriptor):
        identity = _descriptor_identity(descriptor)
        if identity == directory_identity:
            assert stat.S_ISDIR(os.fstat(descriptor).st_mode)
            assert case.output.exists()
            syncs.append("published_directory")
            directory_descriptors.append(descriptor)
            raise OSError(errno.EIO, "synthetic directory fsync failure")
        if temporary_files and identity == temporary_files[0][2]:
            assert not case.output.exists()
            syncs.append("unpublished_file")
        return original(descriptor)

    monkeypatch.setattr(case.module.os, "fsync", fail_owned_directory)
    _cli(case, capsys, "automation_backup_failed")
    assert syncs == ["unpublished_file", "published_directory"]
    assert len(directory_descriptors) == 1
    assert _descriptor_identity(directory_descriptors[0]) is None
    _assert_released(case, temporary_files)
    # Failure after link does not imply absence of the output or durable success.
    # Read the exact published path before any retry; retain its identity/content.
    published = _assert_exact_output(case)
    with closing(sqlite3.connect(case.source)) as connection, connection:
        connection.execute("UPDATE snapshot_origin SET marker=?", ("synthetic-newer",))
    _cli(case, capsys, "automation_backup_output_exists")
    assert _assert_exact_output(case) == published
    assert len(temporary_files) == 1
    assert syncs == ["unpublished_file", "published_directory"]
    assert not list(case.output.parent.glob(".automation-registry.*.tmp"))
