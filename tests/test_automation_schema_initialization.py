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
        connection.commit()
    assert _schema_row(store.path) == (AUTOMATION_SCHEMA_VERSION, "2026-01-01T00:00:00Z")

    clock[0] += timedelta(days=1)
    store.initialize()
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
        with pytest.raises(AutomationError, match="automation_schema_newer_than_runtime"):
            store.initialize()
        assert _snapshot(path) == before
        assert _schema_row(path) == (version, "2025-01-01T00:00:00Z")
    else:
        store.initialize()
        assert _schema_row(path) == (AUTOMATION_SCHEMA_VERSION, "2026-01-02T00:00:00Z")
        assert _snapshot(path)[1] == 23
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            assert connection.execute("SELECT value FROM synthetic_marker").fetchone() == ("replacement-data",)
        upgraded = _snapshot(path)
        clock[0] += timedelta(days=1)
        store.initialize()
        assert _snapshot(path) == upgraded
