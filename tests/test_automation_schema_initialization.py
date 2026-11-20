"""Schema validation stays active without refreshing a version's install time."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from autostop_manager import automation_registry
from autostop_manager.automation_control import AUTOMATION_CONTROL_PROTOCOL, AutomationControlService
from autostop_manager.automation_registry import AUTOMATION_SCHEMA_VERSION, AutomationError, AutomationStore


@pytest.fixture
def clock(monkeypatch):
    current = [datetime(2026, 1, 1, tzinfo=UTC)]
    monkeypatch.setattr(automation_registry, "utc_now", lambda: current[0])
    return current


def _schema_row(path: Path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        return connection.execute(
            "SELECT version, upgraded_at FROM manager_automation_schema WHERE component = 'automation_center'"
        ).fetchone()


def _snapshot(path: Path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        return tuple(connection.iterdump()), connection.execute("PRAGMA user_version").fetchone()[0]


def _control_read(service: AutomationControlService, operation: str):
    return service.handle(
        {
            "protocol": AUTOMATION_CONTROL_PROTOCOL,
            "request_id": str(uuid4()),
            "operation": operation,
            "actor": {"kind": "codex", "id": "synthetic-schema-test", "is_admin": True},
        }
    )


def _add_pending_update(connection: sqlite3.Connection, job_id: str) -> None:
    connection.execute("UPDATE manager_automations SET revision = 2 WHERE job_id = ?", (job_id,))
    connection.execute("UPDATE role_state SET revision = 2 WHERE role = 'M1'")
    connection.execute(
        """
        INSERT INTO manager_automation_outbox(
            outbox_id, job_id, kind, status, idempotency_key, next_attempt_at, created_at, updated_at
        ) VALUES('synthetic', ?, 'test_notification', 'queued', 'synthetic-test', '', '', '')
        """,
        (job_id,),
    )


@pytest.mark.parametrize("operation", ["status", "readiness"])
def test_initialized_diagnostic_reads_committed_state_during_writer_transaction(tmp_path: Path, operation):
    store = AutomationStore(tmp_path / "registry.sqlite3")
    job = store.seed_defaults()["job"]
    service = AutomationControlService(store, timer_controller=SimpleNamespace(list_status=list))
    before = store.path.stat()

    with closing(sqlite3.connect(store.path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        _add_pending_update(writer, job["job_id"])
        result = _control_read(service, operation)
        packet = (result["readiness"] if operation == "status" else result)["execution_packet"]
        assert packet["jobs"][0]["revision"] == job["revision"]
        assert packet["outbox"]["total_count"] == 0
        after = store.path.stat()
        assert (after.st_ctime_ns, after.st_mtime_ns, after.st_mode) == (
            before.st_ctime_ns,
            before.st_mtime_ns,
            before.st_mode,
        )
        writer.commit()

    result = _control_read(service, operation)
    packet = (result["readiness"] if operation == "status" else result)["execution_packet"]
    assert packet["jobs"][0]["revision"] == 2
    assert packet["outbox"]["total_count"] == 1


def test_status_packet_and_roles_share_one_snapshot_across_writer_commit(tmp_path: Path, monkeypatch):
    store = AutomationStore(tmp_path / "registry.sqlite3")
    job = store.seed_defaults()["job"]
    service = AutomationControlService(store, timer_controller=SimpleNamespace(list_status=list))
    original = AutomationStore._technical_execution_state

    with closing(sqlite3.connect(store.path)) as keeper:
        assert keeper.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"

        def count_after_writer_commit(connection, *, job_id):
            with closing(sqlite3.connect(store.path)) as writer:
                writer.execute("BEGIN IMMEDIATE")
                _add_pending_update(writer, job["job_id"])
                writer.commit()
            return original(connection, job_id=job_id)

        with monkeypatch.context() as patch:
            patch.setattr(AutomationStore, "_technical_execution_state", staticmethod(count_after_writer_commit))
            status = _control_read(service, "status")
        updated = _control_read(service, "status")

    for result, revision, role_revision, pending in ((status, 1, 0, 0), (updated, 2, 2, 1)):
        assert result["jobs"][0]["revision"] == revision
        packet = result["readiness"]["execution_packet"]
        assert packet["jobs"][0]["revision"] == revision
        assert packet["outbox"]["total_count"] == pending
        assert result["roles"][0]["role"] == "M1"
        assert result["roles"][0]["revision"] == role_revision


def test_status_initializes_missing_registry(tmp_path: Path, clock):
    store = AutomationStore(tmp_path / "missing" / "registry.sqlite3")
    assert not store.path.parent.exists()
    assert store.status()["jobs"] == []
    assert _schema_row(store.path) == (AUTOMATION_SCHEMA_VERSION, "2026-01-01T00:00:00Z")


@pytest.mark.parametrize(
    "statement,error",
    [
        ("UPDATE manager_automation_schema SET version = 'invalid'", "automation_schema_invalid"),
        ("UPDATE role_registry_schema SET version = version + 1", "role_schema_unsupported"),
    ],
)
def test_status_does_not_repair_invalid_schema_versions(tmp_path: Path, statement: str, error: str):
    store = AutomationStore(tmp_path / "registry.sqlite3")
    store.initialize()
    with store.connect() as connection:
        connection.execute(statement)
    before = store.path.stat()
    with pytest.raises(AutomationError, match=error):
        store.status()
    with pytest.raises(AutomationError, match=error):
        store.readiness()
    assert store.path.stat().st_ctime_ns == before.st_ctime_ns


def test_version_timestamp_changes_only_on_creation_or_version_transition(tmp_path: Path, clock):
    path = tmp_path / "registry.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA user_version = 23")
    store = AutomationStore(path)
    store.initialize()
    assert _schema_row(path) == (AUTOMATION_SCHEMA_VERSION, "2026-01-01T00:00:00Z")
    original = _snapshot(path)

    clock[0] += timedelta(days=1)
    store.initialize()
    assert _snapshot(path) == original

    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "UPDATE manager_automation_schema SET version = ? WHERE component = 'automation_center'",
            (AUTOMATION_SCHEMA_VERSION - 1,),
        )
        connection.commit()
    clock[0] += timedelta(days=1)
    store.initialize()
    assert _schema_row(path) == (AUTOMATION_SCHEMA_VERSION, "2026-01-03T00:00:00Z")
    upgraded = _snapshot(path)
    assert upgraded[1] == 23
    clock[0] += timedelta(days=1)
    store.initialize()
    assert _snapshot(path) == upgraded


def test_same_version_repairs_columns_backfills_and_controller_without_retimestamping(tmp_path: Path, clock):
    store = AutomationStore(tmp_path / "registry.sqlite3")
    service = AutomationControlService(store, timer_controller=SimpleNamespace(list_status=list))
    job = service.handle(
        {
            "protocol": AUTOMATION_CONTROL_PROTOCOL,
            "request_id": str(uuid4()),
            "operation": "create_from_template",
            "actor": {"kind": "codex", "id": "synthetic-schema-test", "is_admin": True},
            "payload": {"template_id": "crm_digest_v1"},
            "idempotency_key": "schema-repair-create-0001",
        }
    )["job"]
    with closing(sqlite3.connect(store.path)) as connection:
        legacy_timezone = connection.execute("SELECT timezone FROM manager_automations").fetchone()[0]
        runtime_updated = connection.execute("SELECT updated_at FROM manager_automation_runtime").fetchone()[0]
        connection.execute("ALTER TABLE manager_automations DROP COLUMN schedule_timezone")
        connection.execute("ALTER TABLE manager_automation_runtime DROP COLUMN applied_at")
        connection.execute("DELETE FROM manager_automation_controller_runtime")
        connection.execute("DROP INDEX idx_manager_automation_outbox_queue")
        connection.commit()
    assert _schema_row(store.path) == (AUTOMATION_SCHEMA_VERSION, "2026-01-01T00:00:00Z")

    clock[0] += timedelta(days=1)
    assert store.status()["controller"]["state"] == "stopped"
    with closing(sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True)) as connection:
        assert connection.execute(
            "SELECT job_id, desired_state, revision, schedule_timezone FROM manager_automations"
        ).fetchone() == (job["job_id"], "off", job["revision"], legacy_timezone)
        assert connection.execute("SELECT applied_at, updated_at FROM manager_automation_runtime").fetchone() == (
            runtime_updated,
            runtime_updated,
        )
        assert connection.execute(
            "SELECT state, hold_enabled FROM manager_automation_controller_runtime WHERE singleton = 1"
        ).fetchone() == ("stopped", 0)
        assert connection.execute("SELECT COUNT(*) FROM manager_automation_runs").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM manager_automation_outbox").fetchone() == (0,)
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name = 'idx_manager_automation_outbox_queue'"
        ).fetchone() == (1,)
    assert _schema_row(store.path) == (AUTOMATION_SCHEMA_VERSION, "2026-01-01T00:00:00Z")


@pytest.mark.parametrize("replacement_kind", ["older", "newer"])
def test_same_store_revalidates_replaced_database(tmp_path: Path, clock, replacement_kind):
    path = tmp_path / "registry.sqlite3"
    store = AutomationStore(path)
    store.initialize()
    previous_inode = path.stat().st_ino
    replacement = tmp_path / "replacement.sqlite3"
    version = AUTOMATION_SCHEMA_VERSION + (1 if replacement_kind == "newer" else -1)
    with closing(sqlite3.connect(replacement)) as connection:
        connection.execute("PRAGMA user_version = 23")
        connection.execute(
            "CREATE TABLE manager_automation_schema(component TEXT PRIMARY KEY, version INTEGER, upgraded_at TEXT)"
        )
        connection.execute(
            "INSERT INTO manager_automation_schema VALUES('automation_center', ?, '2025-01-01T00:00:00Z')",
            (version,),
        )
        connection.execute("CREATE TABLE synthetic_marker(value TEXT)")
        connection.execute("INSERT INTO synthetic_marker VALUES('replacement-data')")
        connection.commit()
    replacement.chmod(0o600)
    os.replace(replacement, path)
    assert path.stat().st_ino != previous_inode
    before = _snapshot(path)
    clock[0] += timedelta(days=1)

    if replacement_kind == "newer":
        before_stat = path.stat()
        with pytest.raises(AutomationError, match="automation_schema_newer_than_runtime"):
            store.status()
        assert path.stat().st_ctime_ns == before_stat.st_ctime_ns
        assert _snapshot(path) == before
        assert _schema_row(path) == (version, "2025-01-01T00:00:00Z")
    else:
        assert store.status()["schema_version"] == AUTOMATION_SCHEMA_VERSION
        assert _schema_row(path) == (AUTOMATION_SCHEMA_VERSION, "2026-01-02T00:00:00Z")
        assert _snapshot(path)[1] == 23
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            assert connection.execute("SELECT value FROM synthetic_marker").fetchone() == ("replacement-data",)
        upgraded = _snapshot(path)
        clock[0] += timedelta(days=1)
        store.initialize()
        assert _snapshot(path) == upgraded
