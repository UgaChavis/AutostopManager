import fcntl
import importlib.util
import os
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "crm_fence_coord", Path(__file__).parents[1] / "scripts/crm_backup_coordinator.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def populate(root, uid, gid):
    root.mkdir(mode=0o700)
    if os.geteuid() == 0:
        os.chown(root, uid, gid)
    for relative in m.HOST_WRITER_LOCKS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent != root and os.geteuid() == 0:
            os.chown(path.parent, uid, gid)
        path.touch(mode=0o600)
        if os.geteuid() == 0:
            os.chown(path, uid, gid)
    for name in m.AGENT_SEED_FILES:
        path = root / "agent" / name
        path.touch(mode=0o600)
        if os.geteuid() == 0:
            os.chown(path, uid, gid)
    database = root / "change_feed.sqlite3"
    db = sqlite3.connect(database)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE synthetic(v INTEGER)")
        db.execute("INSERT INTO synthetic VALUES (7)")
        db.commit()
    finally:
        db.close()
    database.chmod(0o600)
    if os.geteuid() == 0:
        os.chown(database, uid, gid)
    return database


@pytest.fixture
def synthetic(tmp_path):
    root = tmp_path / "data"
    return root, populate(root, os.geteuid(), os.getegid())


def test_native_locks_and_sqlite_writer_are_fenced_but_backup_reads(synthetic, tmp_path):
    root, database = synthetic
    with m.host_writer_fence(root, uid=os.geteuid(), gid=os.getegid()):
        for relative in m.HOST_WRITER_LOCKS:
            fd = os.open(root / relative, os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
        with sqlite3.connect(database, timeout=0.05, isolation_level=None) as writer:
            with pytest.raises(sqlite3.OperationalError):
                writer.execute("BEGIN IMMEDIATE")
        source = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
        target = sqlite3.connect(tmp_path / "restored.sqlite")
        try:
            source.backup(target)
            assert target.execute("SELECT COUNT(*) FROM synthetic").fetchone() == (1,)
        finally:
            source.close()
            target.close()
    with sqlite3.connect(database, timeout=0.2, isolation_level=None) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.rollback()


def test_missing_lock_is_not_created_and_earlier_locks_release(synthetic):
    root, _ = synthetic
    missing = root / m.HOST_WRITER_LOCKS[-1]
    missing.unlink()
    with pytest.raises(FileNotFoundError):
        with m.host_writer_fence(root, uid=os.geteuid(), gid=os.getegid()):
            pass
    assert not missing.exists()
    fd = os.open(root / m.HOST_WRITER_LOCKS[0], os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_busy_lock_fails_with_bounded_wait_and_releases_previous(synthetic):
    root, _ = synthetic
    fd = os.open(root / m.HOST_WRITER_LOCKS[1], os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        with pytest.raises(m.Uncertain, match="lock busy"):
            with m.host_writer_fence(root, uid=os.geteuid(), gid=os.getegid(), timeout=0.1):
                pass
        assert time.monotonic() - started < 2
        first = os.open(root / m.HOST_WRITER_LOCKS[0], os.O_RDWR)
        try:
            fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(first)
    finally:
        os.close(fd)


def test_writer_already_in_transaction_aborts_with_bounded_wait(synthetic):
    root, database = synthetic
    with sqlite3.connect(database, timeout=0.1, isolation_level=None) as writer:
        writer.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(m.Uncertain):
            with m.host_writer_fence(root, uid=os.geteuid(), gid=os.getegid(), timeout=0.3):
                pass
        assert time.monotonic() - started < 2
        writer.rollback()


def test_replaced_lock_invalidates_snapshot(synthetic):
    root, _ = synthetic
    path = root / m.HOST_WRITER_LOCKS[0]
    with pytest.raises(m.Uncertain, match="lock changed"):
        with m.host_writer_fence(root, uid=os.geteuid(), gid=os.getegid()):
            path.unlink()
            path.touch(mode=0o600)


def test_missing_or_replaced_agent_seed_invalidates_snapshot(synthetic):
    root, _ = synthetic
    path = root / "agent" / m.AGENT_SEED_FILES[0]
    path.unlink()
    with pytest.raises(FileNotFoundError):
        with m.host_writer_fence(root, uid=os.geteuid(), gid=os.getegid()):
            pass
    assert not path.exists()
    path.touch(mode=0o600)
    with pytest.raises(m.Uncertain, match="agent state changed"):
        with m.host_writer_fence(root, uid=os.geteuid(), gid=os.getegid()):
            path.unlink()
            path.write_bytes(b"synthetic")


def test_lost_sqlite_fence_invalidates_snapshot(synthetic, monkeypatch):
    root, _ = synthetic
    original = m.subprocess.Popen
    children = []

    def start(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(m.subprocess, "Popen", start)
    with pytest.raises(m.Uncertain):
        with m.host_writer_fence(root, uid=os.geteuid(), gid=os.getegid()):
            children[-1].kill()
            children[-1].wait()
    fd = os.open(root / m.HOST_WRITER_LOCKS[0], os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


@pytest.mark.skipif(os.geteuid() != 0, reason="requires root to emulate the CRM service UID")
def test_sqlite_sidecars_are_created_as_crm_uid():
    with tempfile.TemporaryDirectory(prefix="crm-fence-uid-", dir="/tmp") as d:
        root = Path(d) / "data"
        Path(d).chmod(0o711)
        database = populate(root, 10001, 10001)
        with m.host_writer_fence(root):
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(database) + suffix)
                assert sidecar.is_file()
                assert sidecar.stat().st_uid == 10001
