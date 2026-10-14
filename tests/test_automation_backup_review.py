from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import gc
import importlib.util
import os
from pathlib import Path
import sqlite3
import stat
import threading

import pytest


@pytest.fixture
def backup_module():
    path = Path(
        os.environ.get(
            "PKG007_BACKUP_SOURCE",
            str(Path(__file__).resolve().parents[1] / "scripts/backup-manager-automation-state.py"),
        )
    )
    specification = importlib.util.spec_from_file_location("private_backup_review", path)
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _database(path, *, marker="synthetic-requested", version=6):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE manager_automation_schema (component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO manager_automation_schema VALUES (?, ?)", ("automation_center", version))
        connection.execute("CREATE TABLE snapshot_origin (marker TEXT NOT NULL)")
        connection.execute("INSERT INTO snapshot_origin VALUES (?)", (marker,))
    path.chmod(0o600)


def _marker(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT marker FROM snapshot_origin").fetchone()[0]


def _output(tmp_path):
    directory = tmp_path / "outputs"
    directory.mkdir(mode=0o700)
    return directory / "snapshot.sqlite3"


@pytest.mark.parametrize(
    "name,shadow",
    [
        ("registry.sqlite3", None),
        ("registry?demo.sqlite3", "registry"),
        ("registry#demo.sqlite3", "registry"),
        ("registry%2e.sqlite3", "registry..sqlite3"),
        ("registry%25.sqlite3", "registry%.sqlite3"),
        ("\u0420\u0435\u0435\u0441\u0442\u0440.sqlite3", None),
        ("registry with space.sqlite3", None),
        ("registry+plus.sqlite3", None),
    ],
)
def test_backup_reads_exact_filename_and_keeps_source_read_only(backup_module, tmp_path, monkeypatch, name, shadow):
    source = tmp_path / name
    _database(source)
    if shadow is not None:
        _database(tmp_path / shadow, marker="synthetic-shadow", version=5)
    output = _output(tmp_path)
    original_connect = backup_module.sqlite3.connect
    source_read_only = []

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        if kwargs.get("uri") is True:
            try:
                connection.execute("UPDATE manager_automation_schema SET version=version")
            except sqlite3.OperationalError as error:
                source_read_only.append("readonly" in str(error).replace(" ", "").lower())
            else:
                source_read_only.append(False)
                connection.rollback()
        return connection

    monkeypatch.setattr(backup_module.sqlite3, "connect", connect)
    report = backup_module.backup(source, output)
    assert report["schema_version"] == 6 and _marker(output) == "synthetic-requested"
    assert report["ok"] is True and source_read_only == [True]
    assert output.stat().st_mode & 0o777 == 0o600
    assert not list(output.parent.glob(".automation-registry.*.tmp"))


def test_backup_handles_uri_characters_in_source_directory(backup_module, tmp_path):
    directory = tmp_path / "source?scope"
    directory.mkdir(mode=0o700)
    source = directory / "registry.sqlite3"
    _database(source)
    _database(tmp_path / "source", marker="synthetic-shadow", version=5)
    output = _output(tmp_path)
    report = backup_module.backup(source, output)
    assert report["schema_version"] == 6 and _marker(output) == "synthetic-requested"


def test_backup_retains_direct_relative_source_helper_behavior(backup_module, tmp_path, monkeypatch):
    source = tmp_path / "registry.sqlite3"
    _database(source)
    output = _output(tmp_path)
    monkeypatch.chdir(tmp_path)
    report = backup_module.backup(Path("registry.sqlite3"), output)
    assert report["ok"] is True and _marker(output) == "synthetic-requested"


def test_backup_preserves_existing_output(backup_module, tmp_path):
    source = tmp_path / "registry.sqlite3"
    _database(source)
    output = _output(tmp_path)
    _database(output, marker="synthetic-existing")
    with pytest.raises(ValueError, match=r"^automation_backup_output_exists$"):
        backup_module.backup(source, output)
    assert _marker(output) == "synthetic-existing"


def test_backup_rejects_symlink_source(backup_module, tmp_path):
    source = tmp_path / "registry.sqlite3"
    _database(source)
    link = tmp_path / "alias.sqlite3"
    link.symlink_to(source)
    output = _output(tmp_path)
    with pytest.raises(ValueError, match=r"^automation_backup_source_invalid$"):
        backup_module.backup(link, output)
    assert not output.exists() and not list(output.parent.glob(".automation-registry.*.tmp"))


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o660])
def test_backup_rejects_a_source_readable_by_other_users(backup_module, tmp_path, mode):
    source = tmp_path / "registry.sqlite3"
    _database(source)
    source.chmod(mode)
    output = _output(tmp_path)
    with pytest.raises(ValueError, match=r"^automation_backup_source_invalid$"):
        backup_module.backup(source, output)
    assert not output.exists() and not list(output.parent.glob(".automation-registry.*.tmp"))


@pytest.mark.parametrize("mode", [0o750, 0o755])
def test_backup_rejects_an_output_directory_accessible_to_other_users(backup_module, tmp_path, mode):
    source = tmp_path / "registry.sqlite3"
    _database(source)
    output = _output(tmp_path)
    output.parent.chmod(mode)
    with pytest.raises(ValueError, match=r"^automation_backup_directory_invalid$"):
        backup_module.backup(source, output)
    assert not output.exists() and not list(output.parent.glob(".automation-registry.*.tmp"))


def test_backup_preserves_a_dangling_output_symlink(backup_module, tmp_path):
    source = tmp_path / "registry.sqlite3"
    _database(source)
    output = _output(tmp_path)
    missing = tmp_path / "missing.sqlite3"
    output.symlink_to(missing)
    with pytest.raises(ValueError, match=r"^automation_backup_output_exists$"):
        backup_module.backup(source, output)
    assert output.is_symlink() and output.readlink() == missing and not missing.exists()
    assert not list(output.parent.glob(".automation-registry.*.tmp"))


def test_backup_does_not_publish_failed_verification(backup_module, tmp_path):
    source = tmp_path / "registry.sqlite3"
    _database(source, version="synthetic-invalid")
    output = _output(tmp_path)
    with pytest.raises(ValueError, match=r"^automation_backup_verification_failed$"):
        backup_module.backup(source, output)
    assert not output.exists() and not list(output.parent.glob(".automation-registry.*.tmp"))


def test_verified_backup_syncs_file_before_publication_and_parent_afterward(backup_module, tmp_path, monkeypatch):
    source = tmp_path / "registry.sqlite3"
    _database(source)
    output = _output(tmp_path)
    original_fsync = backup_module.os.fsync
    observed = []

    def sync(descriptor):
        info = os.fstat(descriptor)
        target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        assert target.is_relative_to(tmp_path)
        if stat.S_ISREG(info.st_mode):
            assert target.parent == output.parent and not output.exists()
            observed.append("verified_file")
        else:
            assert stat.S_ISDIR(info.st_mode) and target == output.parent
            assert _marker(output) == "synthetic-requested"
            observed.append("published_directory")
        return original_fsync(descriptor)

    monkeypatch.setattr(backup_module.os, "fsync", sync)
    assert backup_module.backup(source, output)["ok"] is True
    assert observed == ["verified_file", "published_directory"]
    assert output.stat().st_mode & 0o777 == 0o600
    assert not list(output.parent.glob(".automation-registry.*.tmp"))


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="Linux descriptor control")
def test_failed_file_sync_keeps_output_absent_and_closes_resources(backup_module, tmp_path, monkeypatch):
    source = tmp_path / "registry.sqlite3"
    _database(source)
    output = _output(tmp_path)
    gc.collect()
    baseline = _private_handles(tmp_path)
    enabled = gc.isenabled()
    gc.disable()

    def fail_sync(_descriptor):
        raise OSError("synthetic file sync failure")

    monkeypatch.setattr(backup_module.os, "fsync", fail_sync)
    try:
        with pytest.raises(OSError, match="synthetic file sync failure"):
            backup_module.backup(source, output)
        assert not output.exists() and not list(output.parent.glob(".automation-registry.*.tmp"))
        assert _private_handles(tmp_path) == baseline
    finally:
        gc.collect()
        if enabled:
            gc.enable()


def test_backup_preserves_a_competing_output_created_at_publication(backup_module, tmp_path, monkeypatch):
    source = tmp_path / "registry.sqlite3"
    _database(source)
    output = _output(tmp_path)
    original_replace, original_link = backup_module.os.replace, backup_module.os.link

    def commit(operation, temporary, target, *args, **kwargs):
        assert Path(target) == output and Path(temporary).parent == output.parent
        _database(output, marker="synthetic-competing")
        return operation(temporary, target, *args, **kwargs)

    monkeypatch.setattr(backup_module.os, "replace", lambda *args, **kwargs: commit(original_replace, *args, **kwargs))
    monkeypatch.setattr(backup_module.os, "link", lambda *args, **kwargs: commit(original_link, *args, **kwargs))
    with pytest.raises(ValueError, match=r"^automation_backup_output_exists$"):
        backup_module.backup(source, output)
    assert _marker(output) == "synthetic-competing" and output.stat().st_mode & 0o777 == 0o600
    assert not list(output.parent.glob(".automation-registry.*.tmp"))


def test_backup_preserves_a_competing_output_symlink_created_at_publication(backup_module, tmp_path, monkeypatch):
    source = tmp_path / "registry.sqlite3"
    _database(source)
    output = _output(tmp_path)
    target = tmp_path / "competing.sqlite3"
    _database(target, marker="synthetic-symlink-target")
    original_link = backup_module.os.link

    def commit(temporary, destination, *args, **kwargs):
        assert Path(destination) == output and Path(temporary).parent == output.parent
        output.symlink_to(target)
        return original_link(temporary, destination, *args, **kwargs)

    monkeypatch.setattr(backup_module.os, "link", commit)
    with pytest.raises(ValueError, match=r"^automation_backup_output_exists$"):
        backup_module.backup(source, output)
    assert output.is_symlink() and output.readlink() == target
    assert _marker(target) == "synthetic-symlink-target"
    assert not list(output.parent.glob(".automation-registry.*.tmp"))


def test_only_one_concurrent_backup_can_publish_to_the_new_path(backup_module, tmp_path, monkeypatch):
    first, second = tmp_path / "first.sqlite3", tmp_path / "second.sqlite3"
    _database(first, marker="synthetic-first")
    _database(second, marker="synthetic-second")
    output = _output(tmp_path)
    original_replace, original_link = backup_module.os.replace, backup_module.os.link
    barrier = threading.Barrier(2)

    def commit(operation, temporary, target, *args, **kwargs):
        assert Path(target) == output and Path(temporary).parent == output.parent
        barrier.wait(timeout=5)
        return operation(temporary, target, *args, **kwargs)

    monkeypatch.setattr(backup_module.os, "replace", lambda *args, **kwargs: commit(original_replace, *args, **kwargs))
    monkeypatch.setattr(backup_module.os, "link", lambda *args, **kwargs: commit(original_link, *args, **kwargs))

    def backup(source):
        try:
            return backup_module.backup(source, output)
        except ValueError as error:
            return {"ok": False, "error": str(error)}

    with ThreadPoolExecutor(max_workers=2) as executor:
        reports = list(executor.map(backup, (first, second)))
    assert sum(report["ok"] is True for report in reports) == 1
    assert [report["error"] for report in reports if not report["ok"]] == ["automation_backup_output_exists"]
    assert _marker(output) in ("synthetic-first", "synthetic-second")
    assert output.stat().st_mode & 0o777 == 0o600 and not list(output.parent.glob(".automation-registry.*.tmp"))


def _private_handles(root):
    handles = 0
    for name in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink("/proc/self/fd/" + name)
        except FileNotFoundError:
            continue
        if target.startswith(str(root) + "/"):
            handles += 1
    return handles


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="Linux descriptor control")
@pytest.mark.parametrize("valid", [True, False])
def test_backup_closes_connections_on_success_and_verification_failure(backup_module, tmp_path, valid):
    source = tmp_path / "registry.sqlite3"
    _database(source, version=6 if valid else "synthetic-invalid")
    output = _output(tmp_path)
    gc.collect()
    baseline = _private_handles(tmp_path)
    enabled = gc.isenabled()
    gc.disable()
    try:
        if valid:
            assert backup_module.backup(source, output)["ok"] is True
        else:
            with pytest.raises(ValueError, match=r"^automation_backup_verification_failed$"):
                backup_module.backup(source, output)
        assert _private_handles(tmp_path) == baseline
        assert not list(output.parent.glob(".automation-registry.*.tmp"))
    finally:
        gc.collect()
        if enabled:
            gc.enable()
