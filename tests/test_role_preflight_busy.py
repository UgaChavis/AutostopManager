from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
import sqlite3
from types import SimpleNamespace

import pytest

from autostop_manager.automation_daemon import AutomationDaemon
from autostop_manager.automation_registry import AutomationStore
from autostop_manager.role_coordination import RoleStore
from autostop_manager import role_executor as module


REAL_CONNECT = sqlite3.connect


def sqlite_error(code):
    error = sqlite3.OperationalError("synthetic database failure")
    if code is not None:
        error.sqlite_errorcode = code
    return error


def no_rpc():
    pytest.fail("preflight must not open RPC")


class PreflightStore:
    def __init__(self, error, phase):
        self.error = error
        self.phase = phase
        self.projects = 0
        self.reads = 0

    def project_automation_jobs(self):
        self.projects += 1
        if self.phase == "project" and self.error is not None:
            error, self.error = self.error, None
            raise error

    def dispatch_state(self, _role):
        self.reads += 1
        if self.phase == "read" and self.error is not None:
            error, self.error = self.error, None
            raise error
        return {"thread_id": None}, None, None


def daemon_for(executor):
    return AutomationDaemon(store=object(), timer_controller=object(), control_server=object(), role_executor=executor)


@pytest.mark.parametrize("phase", ["project", "read"])
@pytest.mark.parametrize(
    "code",
    [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY_SNAPSHOT, sqlite3.SQLITE_LOCKED_SHAREDCACHE],
)
def test_only_preflight_busy_is_deferred(phase, code):
    store = PreflightStore(sqlite_error(code), phase)
    executor = module.RoleExecutor(store, rpc_factory=no_rpc)

    async def scenario():
        await executor.tick()
        assert store.projects == 1
        await executor.tick()
        assert store.projects == 2
        assert store.reads == (2 if phase == "project" else 3)

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["project", "read"])
@pytest.mark.parametrize("failure", ["unknown", "no_code", "schema", "io", "cancel"])
def test_other_preflight_failures_terminate_without_retry(phase, failure):
    error = {
        "unknown": RuntimeError("unknown outcome"),
        "no_code": sqlite_error(None),
        "schema": sqlite_error(sqlite3.SQLITE_ERROR),
        "io": sqlite_error(sqlite3.SQLITE_IOERR),
        "cancel": asyncio.CancelledError(),
    }[failure]
    store = PreflightStore(error, phase)
    executor = module.RoleExecutor(store, rpc_factory=no_rpc)

    async def scenario():
        task = asyncio.create_task(daemon_for(executor).role_loop())
        with pytest.raises(type(error)) as caught:
            await task
        assert caught.value is error
        await executor.request_reconcile(None)
        await asyncio.sleep(0)
        assert task.done()
        assert store.projects == 1

    asyncio.run(scenario())


@pytest.fixture
def local_store(tmp_path, monkeypatch):
    """Real SQLite/projection, with only AutomationStore's POSIX path gate replaced."""
    connections = []

    class TrackedConnection(sqlite3.Connection):
        closed = False
        rollbacks = 0
        commits = 0
        before_commit = None
        transaction_at_close = None

        def commit(self):
            self.commits += 1
            if self.before_commit is not None:
                self.before_commit(self.commits)
            return super().commit()

        def rollback(self):
            self.rollbacks += 1
            return super().rollback()

        def close(self):
            self.transaction_at_close = self.in_transaction
            super().close()
            self.closed = True

    def connect(*args, **kwargs):
        kwargs.update(timeout=0.01, factory=TrackedConnection)
        db = REAL_CONNECT(*args, **kwargs)
        connections.append(db)
        return db

    @contextmanager
    def local_connection(store):
        db = connect(store.path)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(AutomationStore, "connect", local_connection)
    store = RoleStore(tmp_path / "registry.sqlite3")
    store.project_automation_jobs()
    assert all(db.closed for db in connections)
    connections.clear()
    return store, connections


def assert_connections_closed(connections):
    assert connections
    assert all(db.closed and db.transaction_at_close is False for db in connections)


def test_real_sqlite_busy_recovers_in_existing_role_loop(local_store, monkeypatch):
    store, connections = local_store
    original = store.project_automation_jobs
    attempts = 0

    async def scenario():
        first, recovered = asyncio.Event(), asyncio.Event()

        def project():
            nonlocal attempts
            attempts += 1
            try:
                original()
            finally:
                (first if attempts == 1 else recovered).set()

        monkeypatch.setattr(store, "project_automation_jobs", project)
        executor = module.RoleExecutor(store, rpc_factory=no_rpc)
        blocker = REAL_CONNECT(store.path)
        blocker.execute("BEGIN IMMEDIATE")
        task = asyncio.create_task(daemon_for(executor).role_loop())
        try:
            await asyncio.wait_for(first.wait(), timeout=1)
            assert not task.done()
            assert attempts == 1
            assert_connections_closed(connections)
            assert not store.path.with_suffix(".dispatch.lock").exists()
            blocker.rollback()
            await executor.request_reconcile(None)
            await asyncio.wait_for(recovered.wait(), timeout=1)
            assert attempts == 2
            assert not task.done()
            assert_connections_closed(connections)
        finally:
            blocker.close()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


def test_role_store_busy_during_setup_closes_connection(local_store):
    store, connections = local_store
    blocker = REAL_CONNECT(store.path)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as caught, store.connection(write=True):
            pytest.fail("write transaction must not enter while database is locked")
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert connections[-1].rollbacks == 1
        assert_connections_closed(connections)
    finally:
        blocker.close()


@pytest.mark.parametrize("commit_number", [1, 2])
def test_role_store_busy_commit_rolls_back_and_releases_writer(local_store, monkeypatch, commit_number):
    store, connections = local_store
    connect = sqlite3.connect
    reader = REAL_CONNECT(store.path)

    def hold_reader(number):
        if number == commit_number:
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM role_state").fetchall()

    def connect_with_reader(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.before_commit = hold_reader
        return db

    monkeypatch.setattr(sqlite3, "connect", connect_with_reader)
    try:
        with pytest.raises(sqlite3.OperationalError) as caught, store.connection(write=True) as db:
            db.execute("UPDATE role_state SET context='uncommitted' WHERE role='M1'")
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert connections[-1].commits == commit_number
        assert connections[-1].rollbacks == 1
        assert_connections_closed(connections)
        # The original reader still holds its shared lock. A fresh reserved lock
        # proves the failed writer was released independently of that reader.
        probe = REAL_CONNECT(store.path, timeout=0.01)
        try:
            probe.execute("BEGIN IMMEDIATE")
            assert probe.execute("SELECT context FROM role_state WHERE role='M1'").fetchone()[0] == ""
        finally:
            probe.close()
    finally:
        reader.close()


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_role_store_body_failure_rolls_back_and_closes(local_store, failure):
    store, connections = local_store
    with pytest.raises(failure), store.connection(write=True) as db:
        db.execute("UPDATE role_state SET context='uncommitted' WHERE role='M1'")
        raise failure()
    assert connections[-1].rollbacks == 1
    assert_connections_closed(connections)
    assert store.role_state("M1")["context"] == ""


@pytest.mark.parametrize("failure", ["busy", "unknown", "cancel"])
def test_post_effect_projection_failure_is_not_retried(tmp_path, monkeypatch, failure):
    error = {
        "busy": sqlite_error(sqlite3.SQLITE_BUSY),
        "unknown": RuntimeError("unknown outcome"),
        "cancel": asyncio.CancelledError(),
    }[failure]
    events = []
    store = PreflightStore(None, "project")
    store.path = tmp_path / "registry.sqlite3"

    def project():
        store.projects += 1
        if store.projects == 2:
            raise error

    @asynccontextmanager
    async def rpc():
        events.append("rpc_open")
        try:
            yield object()
        finally:
            events.append("rpc_close")

    async def effect(_role, _rpc):
        events.append("effect")

    monkeypatch.setattr(store, "project_automation_jobs", project)
    # Model the POSIX dispatch lock only in this synthetic post-effect test.
    monkeypatch.setattr(
        module,
        "os",
        SimpleNamespace(
            O_RDWR=2,
            O_CREAT=64,
            O_NOFOLLOW=0,
            open=lambda *_args: events.append("lock_open") or 99,
            close=lambda _fd: events.append("lock_close"),
        ),
    )
    monkeypatch.setattr(module, "fcntl", SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=lambda *_args: None))
    executor = module.RoleExecutor(store, rpc_factory=rpc)
    monkeypatch.setattr(executor, "_needed", lambda role: role == "M1")
    monkeypatch.setattr(executor, "_role_tick", effect)

    async def scenario():
        task = asyncio.create_task(daemon_for(executor).role_loop())
        with pytest.raises(type(error)) as caught:
            await task
        assert caught.value is error
        await executor.request_reconcile(None)
        await asyncio.sleep(0)
        assert store.projects == 2
        assert events == ["lock_open", "rpc_open", "effect", "rpc_close", "lock_close"]

    asyncio.run(scenario())
