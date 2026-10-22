"""A source-lock budget must not become a deadline for a healthy SQLite copy."""

from __future__ import annotations

from contextlib import closing
import hashlib
import importlib.util
import os
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

import autostop_manager


@pytest.fixture
def case(tmp_path, monkeypatch):
    script = Path(autostop_manager.__file__).resolve().parents[1] / "scripts/backup-manager-automation-state.py"
    specification = importlib.util.spec_from_file_location("automation_backup_busy_review", script)
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    monkeypatch.setattr(module, "SOURCE_BUSY_TIMEOUT_SECONDS", 0.1, raising=False)
    source = tmp_path / "registry.sqlite3"
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("CREATE TABLE manager_automation_schema(component TEXT PRIMARY KEY, version INTEGER)")
        connection.execute("INSERT INTO manager_automation_schema VALUES ('automation_center', 6)")
        connection.execute("CREATE TABLE synthetic_origin(marker TEXT)")
        connection.execute("INSERT INTO synthetic_origin VALUES ('synthetic-manager-registry')")
    source.chmod(0o600)
    output_dir = tmp_path / "outputs"
    output_dir.mkdir(mode=0o700)
    return SimpleNamespace(
        module=module, source=source, output=output_dir / "snapshot.sqlite3", connect=sqlite3.connect
    )


def observe_progress(case, monkeypatch, observer):
    class ObservedConnection(sqlite3.Connection):
        def backup(self, target, **kwargs):
            callback = kwargs.pop("progress", None)

            def progress(status, remaining, total):
                observer(status, remaining, total)
                if callback is not None:
                    callback(status, remaining, total)

            return super().backup(target, progress=progress, **kwargs)

    def connect(database, **kwargs):
        if kwargs.get("uri") is True:
            kwargs["factory"] = ObservedConnection
        return case.connect(database, **kwargs)

    monkeypatch.setattr(case.module.sqlite3, "connect", connect)


def start_backup(case):
    done = threading.Event()
    outcome = {}

    def run():
        try:
            outcome["result"] = case.module.backup(case.source, case.output)
        except (OSError, ValueError, sqlite3.Error) as exc:
            outcome["error"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return worker, done, outcome


def assert_snapshot(case):
    with closing(case.connect(case.output)) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute("SELECT marker FROM synthetic_origin").fetchall() == [("synthetic-manager-registry",)]
    assert case.output.stat().st_mode & 0o777 == 0o600
    assert not list(case.output.parent.glob(".automation-registry.*.tmp"))


def private_handles(directory):
    result = []
    for entry in Path("/proc/self/fd").iterdir():
        try:
            target = os.readlink(entry)
        except FileNotFoundError:
            continue
        if target.startswith(str(directory) + "/"):
            result.append(target)
    return result


def test_persistent_exclusive_source_lock_aborts_and_releases_unpublished_backup(case, monkeypatch):
    busy = threading.Event()
    observe_progress(
        case, monkeypatch, lambda status, _remaining, _total: busy.set() if status == sqlite3.SQLITE_BUSY else None
    )
    original_hash = hashlib.sha256(case.source.read_bytes()).hexdigest()
    writer = case.connect(case.source)
    writer.execute("BEGIN EXCLUSIVE")
    worker, done, outcome = start_backup(case)
    try:
        assert busy.wait(15), outcome
        stopped_under_lock = done.wait(2)
        output_while_locked = case.output.exists()
        temporaries_while_locked = list(case.output.parent.glob(".automation-registry.*.tmp"))
    finally:
        writer.rollback()
        writer.close()
        worker.join(5)
    assert not worker.is_alive(), outcome
    assert stopped_under_lock, outcome
    assert isinstance(outcome.get("error"), ValueError), outcome
    assert str(outcome["error"]) == "automation_backup_source_busy"
    assert output_while_locked is False
    assert temporaries_while_locked == []
    assert not case.output.exists()
    assert not list(case.output.parent.glob(".automation-registry.*.tmp"))
    assert hashlib.sha256(case.source.read_bytes()).hexdigest() == original_hash
    assert private_handles(case.source.parent) == []


def test_transient_exclusive_source_lock_can_resume_and_publish(case, monkeypatch):
    busy = threading.Event()
    released = threading.Event()

    def progress(status, _remaining, _total):
        if status == sqlite3.SQLITE_BUSY:
            busy.set()
            assert released.wait(5), "synthetic source lock was not released"

    observe_progress(case, monkeypatch, progress)
    original_hash = hashlib.sha256(case.source.read_bytes()).hexdigest()
    writer = case.connect(case.source)
    writer.execute("BEGIN EXCLUSIVE")
    worker, _done, outcome = start_backup(case)
    try:
        assert busy.wait(15), outcome
        assert not case.output.exists()
    finally:
        writer.rollback()
        writer.close()
        released.set()
        worker.join(5)
    assert not worker.is_alive(), outcome
    assert outcome.get("result", {}).get("ok") is True, outcome
    assert_snapshot(case)
    assert hashlib.sha256(case.source.read_bytes()).hexdigest() == original_hash
    assert private_handles(case.source.parent) == []


def test_healthy_copy_progress_may_exceed_busy_budget_in_total(case, monkeypatch):
    with closing(case.connect(case.source)) as connection, connection:
        connection.execute("CREATE TABLE synthetic_payload(value BLOB)")
        connection.executemany("INSERT INTO synthetic_payload VALUES (?)", [(b"x" * 8192,)] * 512)
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
    assert pages > 256
    original_hash = hashlib.sha256(case.source.read_bytes()).hexdigest()
    clock = SimpleNamespace(value=0.0)
    monkeypatch.setattr(case.module, "time", SimpleNamespace(monotonic=lambda: clock.value), raising=False)

    def progress(status, _remaining, _total):
        if status in {sqlite3.SQLITE_OK, sqlite3.SQLITE_DONE}:
            clock.value += 1.0

    observe_progress(case, monkeypatch, progress)

    assert case.module.backup(case.source, case.output)["ok"] is True

    assert clock.value > case.module.SOURCE_BUSY_TIMEOUT_SECONDS
    assert_snapshot(case)
    with closing(case.connect(case.output)) as connection:
        assert connection.execute("SELECT COUNT(*), SUM(length(value)) FROM synthetic_payload").fetchone() == (
            512,
            512 * 8192,
        )
    assert hashlib.sha256(case.source.read_bytes()).hexdigest() == original_hash
    assert private_handles(case.source.parent) == []
