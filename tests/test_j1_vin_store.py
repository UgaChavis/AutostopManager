"""Storage security regressions using disposable tmpfs and synthetic VINs only."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import time
from typing import Any

import pytest

from autostop_manager import j1_research as general
from autostop_manager import j1_vin_research as api
from autostop_manager import j1_vin_store as store

VIN = "Z94K241BBKR000000"
OTHER = "1HGCM82673A000000"


@pytest.fixture
def private_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    root = Path(tempfile.mkdtemp(prefix="autostop-vin-store-test-", dir="/dev/shm"))
    disk = tmp_path / "isolated-general"
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(disk))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(root))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "1")
    assert store._is_tmpfs(root)
    assert root.stat().st_mode & 0o777 == 0o700
    try:
        yield root, disk
    finally:
        # Remove only this test's private tree, including its own tmpfs outputs.
        shutil.rmtree(root)
        assert not root.exists()


def start(key: str = "synthetic-start-key", vin: str = VIN) -> str:
    result = api.j1_research_vin(vin, key)
    assert result["ok"], result
    return result["job_id"]


def queue_rows() -> list[dict[str, Any]]:
    with general._db(readonly=True) as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM jobs ORDER BY id")]


def assert_disk_deidentified(disk: Path) -> None:
    needles = (VIN.encode(), OTHER.encode(), hashlib.sha256(VIN.encode()).hexdigest().encode())
    for path in disk.rglob("*"):
        if path.is_file():
            body = path.read_bytes()
            assert all(needle not in body for needle in needles), path.name
    with general._db(readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM queries").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0] == 0


def test_private_tmpfs_state_and_persistent_queue_stub_are_separate(private_stores) -> None:
    root, disk = private_stores
    job_id = start()
    database = root / job_id / "research.sqlite3"
    assert database.stat().st_mode & 0o777 == 0o600
    with store.connect(job_id) as connection:
        current = store.metadata(connection)
        assert current["vin"] == VIN
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("PRAGMA temp_store").fetchone()[0] == 2
        assert connection.execute("PRAGMA secure_delete").fetchone()[0] == 1
    rows = queue_rows()
    assert len(rows) == 1 and rows[0]["profile"] == "vin"
    assert rows[0]["objective"] == "Independent VIN public research"
    assert rows[0]["automotive_context"] == ""
    assert_disk_deidentified(disk)


def test_same_key_reuses_one_job_even_when_concurrent(private_stores) -> None:
    _root, disk = private_stores
    with ThreadPoolExecutor(max_workers=2) as executor:
        ids = list(executor.map(lambda _number: start(), range(2)))
    assert ids[0] == ids[1]
    assert len(queue_rows()) == 1
    assert_disk_deidentified(disk)


def test_same_key_different_vin_conflicts_without_mutating_original(private_stores) -> None:
    root, disk = private_stores
    original = start()
    conflict = api.j1_research_vin(OTHER, "synthetic-start-key")
    assert conflict["error"]["code"] == "idempotency_conflict"
    with store.connect(original) as connection:
        assert store.metadata(connection)["vin"] == VIN
    assert [path.name for path in root.iterdir() if store.IDENTIFIER.fullmatch(path.name)] == [original]
    assert len(queue_rows()) == 1
    assert_disk_deidentified(disk)


def test_new_start_honors_general_queue_busy_slot(private_stores) -> None:
    root, disk = private_stores
    original = start()
    result = api.j1_research_vin(OTHER, "another-synthetic-key")
    assert result["error"]["code"] == "j1_busy"
    assert [path.name for path in root.iterdir() if store.IDENTIFIER.fullmatch(path.name)] == [original]
    assert_disk_deidentified(disk)


def test_initialization_failure_rolls_back_queue_and_removes_partial_files(private_stores, monkeypatch) -> None:
    root, disk = private_stores

    def fail(path: Path, *_args: object) -> None:
        (path / "partial").write_text(VIN)
        raise store.VinJobError("initialization_failed")

    monkeypatch.setattr(store, "initialize", fail)
    result = api.j1_research_vin(VIN, "synthetic-start-key")
    assert result["error"]["code"] == "initialization_failed"
    assert not any(store.IDENTIFIER.fullmatch(path.name) for path in root.iterdir())
    assert queue_rows() == []
    assert_disk_deidentified(disk)


def test_queue_commit_failure_removes_initialized_private_state(private_stores, monkeypatch) -> None:
    root, disk = private_stores
    original = general._db

    class FailCommit:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

        def commit(self) -> None:
            raise sqlite3.OperationalError("synthetic_commit_failure")

    @contextmanager
    def failed_db(*, readonly: bool = False):
        with original(readonly=readonly) as connection:
            yield connection if readonly else FailCommit(connection)

    monkeypatch.setattr(general, "_db", failed_db)
    result = api.j1_research_vin(VIN, "synthetic-start-key")
    assert result["error"]["code"] == "vin_store_unavailable"
    assert not any(store.IDENTIFIER.fullmatch(path.name) for path in root.iterdir())
    assert queue_rows() == []
    assert_disk_deidentified(disk)


def test_add_queries_queue_commit_failure_keeps_completed_private_state(private_stores, monkeypatch) -> None:
    _root, disk = private_stores
    job_id = start()
    with store.connect(job_id, transaction=True) as connection:
        current = store.metadata(connection)
        current.update(collection_status="completed", analysis_status="ready_partial", stop_reason="completed_test")
        store.write_metadata(connection, current)
        connection.execute("UPDATE queries SET status='done'")
    store.set_stub(job_id, "completed")
    with store.connect(job_id) as connection:
        metadata_before = store.metadata(connection)
        queries_before = [dict(row) for row in connection.execute("SELECT * FROM queries ORDER BY id")]
    queue_before = queue_rows()
    original = general._db
    commits = []

    class FailCommit:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

        def commit(self) -> None:
            commits.append(True)
            raise sqlite3.OperationalError("synthetic_commit_failure " + VIN)

    @contextmanager
    def failed_db(*, readonly: bool = False):
        with original(readonly=readonly) as connection:
            yield connection if readonly else FailCommit(connection)

    monkeypatch.setattr(general, "_db", failed_db)
    result = api.research_add_queries(job_id, ["Synthetic extra source"])
    assert result == store.error("vin_store_unavailable", job_id)
    assert commits == [True]
    with store.connect(job_id) as connection:
        assert store.metadata(connection) == metadata_before
        assert [dict(row) for row in connection.execute("SELECT * FROM queries ORDER BY id")] == queries_before
    assert queue_rows() == queue_before
    assert_disk_deidentified(disk)


def test_disk_cache_is_rejected_as_private_vin_storage(private_stores, monkeypatch, tmp_path) -> None:
    invalid = tmp_path / "persistent-vin"
    invalid.mkdir(mode=0o700)
    if store._is_tmpfs(invalid):
        pytest.skip("pytest disk fixture is unexpectedly on tmpfs")
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(invalid))
    result = api.j1_research_vin(VIN, "synthetic-start-key")
    assert result["error"]["code"] == "vin_tmpfs_required"
    assert list(invalid.iterdir()) == []


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o707])
def test_private_root_permissions_are_required(private_stores, mode) -> None:
    root, _disk = private_stores
    root.chmod(mode)
    try:
        result = api.j1_research_vin(VIN, "synthetic-start-key")
        assert result["error"]["code"] == "vin_store_permissions_invalid"
        assert list(root.iterdir()) == []
    finally:
        root.chmod(0o700)


def test_root_and_ancestor_symlinks_are_rejected(private_stores, monkeypatch) -> None:
    root, _disk = private_stores
    actual = root / "actual"
    actual.mkdir(mode=0o700)
    child = actual / "child"
    child.mkdir(mode=0o700)
    link = root / "link"
    link.symlink_to(actual, target_is_directory=True)
    for target in (link, link / "child"):
        monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(target))
        result = api.j1_research_vin(VIN, "synthetic-start-key")
        assert result["error"]["code"] == "vin_store_permissions_invalid"
    assert list(child.iterdir()) == []


def test_wrong_root_owner_is_rejected(private_stores, monkeypatch) -> None:
    root, _disk = private_stores
    uid = os.geteuid()
    monkeypatch.setattr(store.os, "geteuid", lambda: uid + 1)
    result = api.j1_research_vin(VIN, "synthetic-start-key")
    assert result["error"]["code"] == "vin_store_permissions_invalid"
    assert list(root.iterdir()) == []


def test_lock_symlink_never_writes_target(private_stores) -> None:
    root, _disk = private_stores
    target = root / "sentinel"
    target.write_text("unchanged")
    (root / "start.lock").symlink_to(target)
    result = api.j1_research_vin(VIN, "synthetic-start-key")
    assert result["ok"] is False
    assert target.read_text() == "unchanged"


@pytest.mark.parametrize("mode", [0o644, 0o640])
def test_database_read_rejects_nonprivate_permissions(private_stores, mode) -> None:
    root, _disk = private_stores
    job_id = start()
    database = root / job_id / "research.sqlite3"
    database.chmod(mode)
    try:
        result = api.research_status(job_id)
        assert result["error"]["code"] == "vin_store_permissions_invalid"
    finally:
        database.chmod(0o600)


def test_database_symlink_is_rejected_without_touching_target(private_stores) -> None:
    root, _disk = private_stores
    job_id = start()
    database = root / job_id / "research.sqlite3"
    actual = root / "database-sentinel"
    database.rename(actual)
    before = actual.read_bytes()
    database.symlink_to(actual)
    result = api.research_status(job_id)
    assert result["error"]["code"] == "vin_store_permissions_invalid"
    assert actual.read_bytes() == before


def test_lost_private_directory_cannot_resume_from_persistent_stub(private_stores) -> None:
    root, disk = private_stores
    job_id = start()
    store._delete_job(root, job_id)
    result = api.research_status(job_id)
    assert result["error"]["code"] == "vin_ephemeral_state_lost"
    row = queue_rows()[0]
    assert (row["status"], row["error"]) == ("failed", "vin_ephemeral_state_lost")
    assert not (root / job_id).exists()
    assert_disk_deidentified(disk)


def test_lost_database_does_not_create_empty_replacement(private_stores) -> None:
    root, disk = private_stores
    job_id = start()
    database = root / job_id / "research.sqlite3"
    database.unlink()
    result = api.research_status(job_id)
    assert result["error"]["code"] == "vin_ephemeral_state_lost"
    assert not database.exists()
    assert queue_rows()[0]["status"] == "failed"
    assert_disk_deidentified(disk)


def test_expiry_removes_database_fts_journals_and_nested_outputs(private_stores) -> None:
    root, disk = private_stores
    job_id = start()
    job_path = root / job_id
    with store.connect(job_id, transaction=True) as connection:
        current = store.metadata(connection)
        document_id = "a" * 32
        connection.execute(
            "INSERT INTO documents(id,url,source_class,source_tier,source_basis) VALUES(?,?,?,?,?)",
            (document_id, "https://example.org/" + VIN, "unknown", "unclassified", "test"),
        )
        api.ingest_pages(connection, current, document_id, [{"page": 1, "text": VIN + " synthetic source"}])
        current["expires_at"] = time.time() - 1
        store.write_metadata(connection, current)
    for name in ("research.sqlite3-wal", "research.sqlite3-shm", "research.sqlite3-journal", "source.pdf"):
        path = job_path / name
        path.write_bytes(VIN.encode())
        path.chmod(0o600)
    nested = job_path / "parser-temp"
    nested.mkdir(mode=0o700)
    (nested / "page.png").write_bytes(b"synthetic output")
    store.prune()
    assert not job_path.exists()
    assert queue_rows()[0]["error"] == "vin_job_expired"
    assert_disk_deidentified(disk)


def test_prune_closes_sqlite_connections_even_for_live_job(private_stores, monkeypatch) -> None:
    job_id = start()
    opened: list[Any] = []
    original = store._database

    class Tracked:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection
            self.closed = False

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

        def __enter__(self) -> Any:
            return self.connection.__enter__()

        def __exit__(self, *_args: object) -> Any:
            return self.connection.__exit__(*_args)

        def close(self) -> None:
            self.closed = True
            self.connection.close()

    def tracked_database(path: Path, **kwargs: Any) -> Any:
        tracked = Tracked(original(path, **kwargs))
        opened.append(tracked)
        return tracked

    monkeypatch.setattr(store, "_database", tracked_database)
    try:
        store.prune()
        assert len(opened) == 1
        assert opened[0].closed is True
    finally:
        for connection in opened:
            connection.close()
    assert api.research_status(job_id)["ok"] is True


def test_transaction_error_rolls_back_private_mutation(private_stores) -> None:
    job_id = start()
    with pytest.raises(RuntimeError, match="rollback-test"):
        with store.connect(job_id, transaction=True) as connection:
            current = store.metadata(connection)
            current["revision"] = 999
            store.write_metadata(connection, current)
            raise RuntimeError("rollback-test")
    with store.connect(job_id) as connection:
        assert store.metadata(connection)["revision"] == 0


def test_capacity_failure_cannot_commit_partial_private_state(private_stores, monkeypatch) -> None:
    job_id = start()

    def exhausted(*_args: object) -> None:
        raise store.VinJobError("vin_storage_limit_reached")

    monkeypatch.setattr(store, "check_capacity", exhausted)
    with pytest.raises(store.VinJobError, match="vin_storage_limit_reached"):
        with store.connect(job_id, transaction=True) as connection:
            current = store.metadata(connection)
            current["revision"] = 123
            store.write_metadata(connection, current)
    with store.connect(job_id) as connection:
        assert store.metadata(connection)["revision"] == 0


def test_disabled_start_does_not_create_private_state(private_stores, monkeypatch) -> None:
    root, disk = private_stores
    monkeypatch.setenv("AUTOSTOP_J1_VIN_RESEARCH_ENABLED", "0")
    result = api.j1_research_vin(VIN, "synthetic-start-key")
    assert result["error"]["code"] == "vin_research_disabled"
    assert list(root.iterdir()) == []
    assert not disk.exists()


def test_tmpfs_connection_is_closed_after_normal_context(private_stores) -> None:
    job_id = start()
    with store.connect(job_id) as connection:
        assert store.metadata(connection)["vin"] == VIN
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")
    root, _disk = private_stores
    assert stat.S_ISDIR((root / job_id).stat().st_mode)


def test_expiry_during_transaction_prevents_commit(private_stores, monkeypatch) -> None:
    clock = [time.time()]
    monkeypatch.setattr(store.time, "time", lambda: clock[0])
    job_id = start()
    with store.connect(job_id) as connection:
        expiry = store.metadata(connection)["expires_at"]
    with pytest.raises(store.VinJobError, match="vin_job_expired"):
        with store.connect(job_id, transaction=True) as connection:
            current = store.metadata(connection)
            current["revision"] = 987
            store.write_metadata(connection, current)
            clock[0] = expiry + 1
    clock[0] = expiry - 60
    with store.connect(job_id) as connection:
        assert store.metadata(connection)["revision"] == 0


def test_corrupt_metadata_cannot_retain_expired_private_artifacts(private_stores) -> None:
    root, disk = private_stores
    job_id = start()
    job_path = root / job_id
    connection = sqlite3.connect(job_path / "research.sqlite3")
    try:
        connection.execute("UPDATE metadata SET payload='{' WHERE id=1")
        connection.commit()
    finally:
        connection.close()
    stale = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds")
    with general._db() as connection:
        connection.execute("UPDATE jobs SET created_at=? WHERE id=?", (stale, job_id))
        connection.commit()
    (job_path / "remaining.pdf").write_bytes(VIN.encode())
    store.prune()
    assert not job_path.exists()
    assert queue_rows()[0]["status"] == "failed"
    assert_disk_deidentified(disk)


@pytest.mark.parametrize("expiry", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_expiry_is_lost_state_and_uses_stale_cleanup(private_stores, expiry) -> None:
    root, disk = private_stores
    job_id = start()
    path = root / job_id
    connection = sqlite3.connect(path / "research.sqlite3")
    try:
        current = json.loads(connection.execute("SELECT payload FROM metadata WHERE id=1").fetchone()[0])
        current["expires_at"] = expiry
        connection.execute("UPDATE metadata SET payload=? WHERE id=1", (json.dumps(current),))
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(store.VinJobError, match="vin_ephemeral_state_lost"):
        with store.connect(job_id):
            pass
    stale = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds")
    with general._db() as connection:
        connection.execute("UPDATE jobs SET created_at=? WHERE id=?", (stale, job_id))
        connection.commit()
    (path / "remaining.pdf").write_bytes(VIN.encode())
    store.prune()
    assert not path.exists()
    assert_disk_deidentified(disk)


def test_unrelated_lost_private_state_does_not_block_new_start(private_stores) -> None:
    root, disk = private_stores
    lost_job = start()
    (root / lost_job / "research.sqlite3").unlink()
    result = api.research_status(lost_job)
    assert result["error"]["code"] == "vin_ephemeral_state_lost"
    next_job = api.j1_research_vin(OTHER, "synthetic-fresh-start")
    assert next_job["ok"] is True, next_job
    assert next_job["job_id"] != lost_job
    rows = {row["id"]: row for row in queue_rows()}
    assert rows[lost_job]["status"] == "failed"
    assert rows[next_job["job_id"]]["status"] == "queued"
    assert_disk_deidentified(disk)
