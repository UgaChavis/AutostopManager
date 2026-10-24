from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from autostop_manager.automation_control import AUTOMATION_CONTROL_PROTOCOL, AutomationControlService
from autostop_manager.automation_registry import AUTOMATION_SCHEMA_VERSION, AutomationError, AutomationStore

NOW = "2026-10-24T00:00:00Z"
TARGET = "auto_" + "1" * 24
ARCHIVED = "auto_" + "2" * 24
TABLES = ("manager_automation_outbox", "manager_automation_runs")
INDEXES = ("idx_manager_automation_outbox_job_status", "idx_manager_automation_runs_job_status")


def seed_jobs(store):
    store.initialize()
    with store.connect() as connection:
        for job_id, archived_at in ((TARGET, None), (ARCHIVED, NOW)):
            connection.execute(
                """
                INSERT INTO manager_automations(
                    job_id, template_id, name, desired_state, schedule_kind,
                    every_seconds, timezone, revision, created_actor_hash,
                    created_at, updated_at, archived_at)
                VALUES (?, 'crm_digest_v1', 'Synthetic counters', 'off', 'interval',
                        1200, 'UTC', 1, 'synthetic', ?, ?, ?)
                """,
                (job_id, NOW, NOW, archived_at),
            )
            connection.execute(
                "INSERT INTO manager_automation_runtime(job_id, actual_state, updated_at) VALUES (?, 'disabled', ?)",
                (job_id, NOW),
            )
        for index, status in enumerate(("queued", "sending", "sent", "failed", "cancelled", "sending")):
            connection.execute(
                """
                INSERT INTO manager_automation_outbox(
                    outbox_id, job_id, kind, status, idempotency_key,
                    next_attempt_at, created_at, updated_at, lease_until)
                VALUES (?, ?, 'synthetic', ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"target-out-{index}",
                    TARGET,
                    status,
                    f"target-key-{index}",
                    NOW,
                    NOW,
                    NOW,
                    NOW if index == 5 else None,
                ),
            )
        for index, status in enumerate(("queued", "running", "succeeded", "failed", "cancelled")):
            connection.execute(
                """INSERT INTO manager_automation_runs(
                       run_id, job_id, trigger_kind, status, scheduled_for, requested_at)
                   VALUES (?, ?, 'manual', ?, ?, ?)""",
                (f"target-run-{index}", TARGET, status, NOW, NOW),
            )


def append_history(store, start, stop):
    with store.connect() as connection:
        connection.executemany(
            """INSERT INTO manager_automation_outbox(
                   outbox_id, job_id, kind, status, idempotency_key,
                   next_attempt_at, created_at, updated_at)
               VALUES (?, ?, 'synthetic', 'sent', ?, ?, ?, ?)""",
            [(f"archive-out-{i}", ARCHIVED, f"archive-key-{i}", NOW, NOW, NOW) for i in range(start, stop)],
        )
        connection.executemany(
            """INSERT INTO manager_automation_runs(
                   run_id, job_id, trigger_kind, status, scheduled_for, requested_at)
               VALUES (?, ?, 'manual', 'succeeded', ?, ?)""",
            [(f"archive-run-{i}", ARCHIVED, NOW, NOW) for i in range(start, stop)],
        )


def measure_packet(store, service, operation, monkeypatch):
    steps = dict.fromkeys(TABLES, 0)
    queries = {}
    original_connect = AutomationStore.connect

    @contextmanager
    def measured_connect(target):
        with original_connect(target) as connection:
            current = [None]

            def trace(statement):
                sql = " ".join(statement.split())
                current[0] = None
                if sql.startswith("SELECT status, COUNT(*) AS item_count"):
                    for table in TABLES:
                        if f"FROM {table} " in sql:
                            queries[table] = sql
                            current[0] = table

            def progress():
                if current[0] is not None:
                    steps[current[0]] += 1
                return 0

            connection.set_trace_callback(trace)
            connection.set_progress_handler(progress, 1)
            yield connection

    with monkeypatch.context() as patch:
        patch.setattr(AutomationStore, "connect", measured_connect)
        response = service.handle(
            {
                "protocol": AUTOMATION_CONTROL_PROTOCOL,
                "request_id": str(uuid4()),
                "operation": operation,
                "actor": {"kind": "codex", "id": "synthetic-counters", "is_admin": True},
                "payload": {},
            }
        )
    packet = response["readiness"]["execution_packet"] if operation == "status" else response["execution_packet"]
    return packet, steps, queries


def assert_job_searches(store, queries):
    assert set(queries) == set(TABLES)
    with store.connect() as connection:
        for sql in queries.values():
            details = [str(row[3]) for row in connection.execute("EXPLAIN QUERY PLAN " + sql)]
            assert any("SEARCH" in row and "COVERING INDEX" in row and "job_id=?" in row for row in details)


@pytest.mark.parametrize("operation", ["status", "readiness"])
def test_job_packet_work_does_not_grow_with_unrelated_archived_history(tmp_path, monkeypatch, operation):
    store = AutomationStore(tmp_path / "registry.sqlite3")
    seed_jobs(store)
    service = AutomationControlService(store, timer_controller=SimpleNamespace(list_status=list))
    samples = []
    for start, stop in ((0, 500), (500, 5000)):
        append_history(store, start, stop)
        packet, steps, queries = measure_packet(store, service, operation, monkeypatch)
        assert [job["job_id"] for job in packet["jobs"]] == [TARGET]
        assert packet["outbox"] == {
            "by_status": {"queued": 1, "sending": 2, "sent": 1, "failed": 1, "cancelled": 1},
            "total_count": 6,
            "pending_count": 4,
            "blocked_count": 1,
        }
        assert packet["runs"] == {
            "by_status": {"queued": 1, "running": 1, "succeeded": 1, "failed": 1, "cancelled": 1},
            "total_count": 5,
        }
        assert all(value > 0 for value in steps.values())
        samples.append((steps, queries))
    for table in TABLES:
        assert samples[1][0][table] <= samples[0][0][table] * 2 + 100
    assert_job_searches(store, samples[-1][1])
    global_state = store.technical_execution_state()
    assert global_state["outbox"]["total_count"] == 5006
    assert global_state["runs"]["total_count"] == 5005
    archived_state = store.technical_execution_state(job_id=ARCHIVED)
    assert archived_state["outbox"]["by_status"] == {"sent": 5000}
    assert archived_state["runs"]["by_status"] == {"succeeded": 5000}


def snapshot(store):
    with store.connect() as connection:
        return (
            tuple(line for line in connection.iterdump() if line.startswith("INSERT INTO")),
            connection.execute("PRAGMA user_version").fetchone()[0],
        )


def remove_counter_indexes(store):
    with store.connect() as connection:
        for name in INDEXES:
            connection.execute(f"DROP INDEX IF EXISTS {name}")


def test_populated_same_version_database_gains_indexes_without_changing_data(tmp_path):
    store = AutomationStore(tmp_path / "registry.sqlite3")
    seed_jobs(store)
    append_history(store, 0, 30)
    remove_counter_indexes(store)
    with store.connect() as connection:
        connection.execute("PRAGMA user_version = 23")
        connection.execute("UPDATE manager_automation_schema SET upgraded_at = ?", (NOW,))
    before = snapshot(store)
    for _attempt in range(2):
        store.initialize()
        assert snapshot(store) == before
        assert store.schema_version() == AUTOMATION_SCHEMA_VERSION
        assert_job_searches(
            store,
            {
                table: f"SELECT status, COUNT(*) FROM {table} WHERE job_id = '{TARGET}' GROUP BY status"
                for table in TABLES
            },
        )
        remove_counter_indexes(store)


def test_newer_database_is_rejected_before_creating_counter_indexes(tmp_path):
    store = AutomationStore(tmp_path / "registry.sqlite3")
    seed_jobs(store)
    remove_counter_indexes(store)
    with store.connect() as connection:
        connection.execute("UPDATE manager_automation_schema SET version = ?", (AUTOMATION_SCHEMA_VERSION + 1,))
    before = snapshot(store)

    with pytest.raises(AutomationError, match="automation_schema_newer_than_runtime"):
        store.initialize()

    assert snapshot(store) == before
    with sqlite3.connect(store.path) as connection:
        indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert not indexes.intersection(INDEXES)
