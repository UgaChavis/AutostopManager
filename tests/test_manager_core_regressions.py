from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Event, current_thread

import pytest

from autostop_manager.storage import StoreState


def _run(state):
    started = state.start_workflow_run(
        workflow_id="synthetic_review", intent="synthetic_review", idempotency_key="synthetic-review"
    )
    assert state.transition_workflow_run(started["id"], status="executing")["ok"]
    return started["id"]


@pytest.mark.parametrize(
    ("verification", "paths"),
    [
        ({"verification": [{"ok": False}]}, ["verification[0].ok"]),
        ({"readback": [{"status": "failed"}]}, ["readback[0].status"]),
        ({"execution": [[{"checks": [{"success": False}]}]]}, ["execution[0][0].checks[0].success"]),
        ({"readback": [{"ok": True}, {"nested": [{"passed": False}]}]}, ["readback[1].nested[0].passed"]),
    ],
)
def test_nested_array_failures_reject_completion_without_ledger_writes(tmp_path, verification, paths):
    state = StoreState(tmp_path / "manager.sqlite3")
    run_id = _run(state)
    assert state.transition_workflow_run(run_id, status="verifying")["ok"]
    before = state.get_manager_run(run_id)["item"]
    rejected = state.transition_workflow_run(run_id, status="completed", verification=verification)
    assert rejected["ok"] is False
    assert rejected["error"] == "verification_failed_before_completion"
    assert rejected["failure_paths"] == paths
    assert state.get_manager_run(run_id)["item"] == before


def test_nested_positive_technical_refs_still_complete_and_replay(tmp_path):
    state = StoreState(tmp_path / "manager.sqlite3")
    run_id = _run(state)
    assert state.transition_workflow_run(run_id, status="verifying")["ok"]
    verification = {
        "verification": [{"ok": True, "message_id": "synthetic-message"}],
        "readback": [[{"status": "completed", "external_ref": "synthetic-ref"}]],
        "optional_metadata": [{"ok": False}],
    }
    completed = state.transition_workflow_run(run_id, status="completed", verification=verification)
    assert completed["ok"]
    before = state.get_manager_run(run_id)["item"]
    assert before["verification"] == verification
    assert state.transition_workflow_run(run_id, status="completed", verification=verification)["deduplicated"]
    assert state.get_manager_run(run_id)["item"] == before


@pytest.mark.parametrize("array", [False, True])
@pytest.mark.parametrize("context", ["readback", "executor", "verification"])
@pytest.mark.parametrize("success_marker", ["ok", "success", "passed", "verified"])
def test_successful_technical_node_preserves_domain_failure_state(tmp_path, array, context, success_marker):
    state = StoreState(tmp_path / "manager.sqlite3")
    run_id = _run(state)
    assert state.transition_workflow_run(run_id, status="verifying")["ok"]
    before = state.get_manager_run(run_id)["item"]
    node = {
        success_marker: True,
        "business": {"status": "failed", "available": False, "paid": False, "ok": False},
        "nested": [{"state": "rejected", "success": False}],
        "external_ref": "synthetic-business-ref",
    }
    verification = {"executor_ok": True, context: [node] if array else node}
    completed = state.transition_workflow_run(run_id, status="completed", verification=verification)
    assert completed["ok"]
    after = state.get_manager_run(run_id)["item"]
    assert after["status"] == "completed"
    assert after["state_version"] == before["state_version"] + 1
    assert len(after["events"]) == len(before["events"]) + 1
    assert after["verification"] == verification
    assert state.transition_workflow_run(run_id, status="completed", verification=verification)["deduplicated"]
    assert state.get_manager_run(run_id)["item"] == after


@pytest.mark.parametrize("array", [False, True])
@pytest.mark.parametrize(
    ("node", "failure"),
    [
        ({"ok": False}, "ok"),
        ({"ok": True, "status": "failed"}, "status"),
        ({"ok": True, "success": False}, "success"),
        ({"ok": True, "checks": [{"ok": False}]}, "checks[0].ok"),
        ({"ok": True, "check": {"status": "failed"}}, "check.status"),
        ({"ok": True, "executor": {"ok": False}}, "executor.ok"),
        ({"ok": True, "business": {"verification": {"status": "failed"}}}, "business.verification.status"),
        ({"ok": True, "business": {"execution_ok": False}}, "business.execution_ok"),
        (
            {"ok": True, "business": {"nested": [{"readback": [{"ok": False}]}]}},
            "business.nested[0].readback[0].ok",
        ),
    ],
)
def test_successful_parent_still_rejects_direct_or_explicit_technical_child_failure(tmp_path, array, node, failure):
    state = StoreState(tmp_path / "manager.sqlite3")
    run_id = _run(state)
    assert state.transition_workflow_run(run_id, status="verifying")["ok"]
    before = state.get_manager_run(run_id)["item"]
    verification = {"executor_ok": True, "readback": [node] if array else node}
    rejected = state.transition_workflow_run(run_id, status="completed", verification=verification)
    assert rejected["ok"] is False
    assert rejected["error"] == "verification_failed_before_completion"
    assert rejected["failure_paths"] == [f"readback{'[0]' if array else ''}.{failure}"]
    assert state.get_manager_run(run_id)["item"] == before


@pytest.mark.parametrize("array", [False, True])
def test_completed_replay_rejects_contradictory_success_and_failed_readback(tmp_path, array):
    state = StoreState(tmp_path / "manager.sqlite3")
    run_id = _run(state)
    assert state.transition_workflow_run(run_id, status="verifying")["ok"]
    assert state.transition_workflow_run(run_id, status="completed", verification={"executor_ok": True})["ok"]
    before = state.get_manager_run(run_id)["item"]
    node = {"ok": True, "status": "failed"}
    rejected = state.transition_workflow_run(
        run_id, status="completed", verification={"readback": [node] if array else node}
    )
    assert rejected["error"] == "verification_failed_before_completion"
    assert rejected["failure_paths"] == [f"readback{'[0]' if array else ''}.status"]
    assert state.get_manager_run(run_id)["item"] == before


@pytest.mark.parametrize("target", ["verifying", "completed", "replay"])
@pytest.mark.parametrize("body_key", ["body", "raw", "raw_body", "message_body_text"])
def test_generic_transition_rejects_nested_raw_body_before_write_or_replay(tmp_path, target, body_key):
    state = StoreState(tmp_path / "manager.sqlite3")
    run_id = _run(state)
    if target != "verifying":
        assert state.transition_workflow_run(run_id, status="verifying")["ok"]
    if target == "replay":
        assert state.transition_workflow_run(run_id, status="completed", verification={"readback": {"ok": True}})["ok"]
    before = state.get_manager_run(run_id)["item"]
    payload = {"readback": [{"ok": True, body_key: "SYNTHETIC_RAW_BODY_MUST_NOT_PERSIST"}]}
    checkpoint = state.checkpoint_workflow_run(run_id, checkpoint=payload)
    assert checkpoint["error"] == "raw_external_body_not_allowed_in_manager_ledger"
    rejected = state.transition_workflow_run(
        run_id, status="completed" if target == "replay" else target, verification=payload
    )
    assert rejected["ok"] is False
    assert rejected["error"] == checkpoint["error"]
    assert rejected["forbidden_keys"] == [f"readback[0].{body_key}"]
    assert state.get_manager_run(run_id)["item"] == before
    assert b"SYNTHETIC_RAW_BODY_MUST_NOT_PERSIST" not in state.path.read_bytes()


@pytest.mark.parametrize(
    ("operation", "include_events", "include_steps"),
    [("get", True, True), ("get", False, True), ("get", True, False), ("get", False, False), ("list", True, False)],
)
def test_parent_events_and_steps_use_one_wal_read_snapshot(
    tmp_path, monkeypatch, operation, include_events, include_steps
):
    state = StoreState(tmp_path / "manager.sqlite3")
    run_id = _run(state)
    waiting = state.register_external_step(run_id, step_id="synthetic-step", connector="synthetic", action="synthetic")
    with sqlite3.connect(state.path) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"

    def read():
        if operation == "get":
            return state.get_manager_run(run_id, include_events=include_events, include_external_steps=include_steps)[
                "item"
            ]
        return state.list_manager_runs(include_events=include_events)["items"][0]

    before = read()
    parent_read, release_reader = Event(), Event()
    real_connect = sqlite3.connect
    transactions = []

    class FrozenRows:
        def __init__(self, rows):
            self.rows = rows

        def fetchone(self):
            return self.rows[0]

        def fetchall(self):
            return self.rows

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            cursor = super().execute(sql, *args, **kwargs)
            if current_thread().name == "snapshot-reader" and sql.startswith("SELECT * FROM manager_runs"):
                rows = cursor.fetchall()
                cursor.close()
                transactions.append(self.in_transaction)
                parent_read.set()
                assert release_reader.wait(3)
                return FrozenRows(rows)
            return cursor

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=ObservedConnection)

    def read_in_thread():
        current_thread().name = "snapshot-reader"
        return read()

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            reader = pool.submit(read_in_thread)
            assert parent_read.wait(3)
            writer = pool.submit(
                state.complete_external_step,
                run_id,
                step_id="synthetic-step",
                result_refs={"external_ref": "synthetic-result"},
            )
            written = writer.result(timeout=3)
            release_reader.set()
            observed = reader.result(timeout=3)
    finally:
        release_reader.set()
    assert written["ok"]
    assert written["state_version"] == waiting["state_version"] + 1
    assert observed == before
    if include_events or include_steps:
        assert transactions == [True]
    assert read()["state_version"] == written["state_version"]
    assert ("events" in observed) is include_events
    assert ("external_steps" in observed) is include_steps


def test_two_real_connections_serialize_legacy_column_migration_and_preserve_data(tmp_path, monkeypatch):
    state = StoreState(tmp_path / "legacy.sqlite3")
    run_id = _run(state)
    before = state.get_manager_run(run_id)["item"]
    with sqlite3.connect(state.path) as conn:
        conn.execute("ALTER TABLE manager_runs DROP COLUMN state_version")
    first_read, second_attempt, release_first, release_second = Event(), Event(), Event(), Event()
    real_connect = sqlite3.connect
    transactions = []

    class FrozenRows:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            name = current_thread().name
            if name == "migration-second" and sql == "BEGIN IMMEDIATE":
                second_attempt.set()
            cursor = super().execute(sql, *args, **kwargs)
            if name.startswith("migration-") and sql == "PRAGMA table_info(manager_runs)":
                rows = cursor.fetchall()
                cursor.close()
                transactions.append(self.in_transaction)
                if name == "migration-first":
                    first_read.set()
                    assert release_first.wait(3)
                else:
                    second_attempt.set()
                    assert release_second.wait(3)
                return FrozenRows(rows)
            return cursor

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=ObservedConnection)

    def migrate(name):
        current_thread().name = name
        state.initialize()

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(migrate, "migration-first")
            assert first_read.wait(3)
            second = pool.submit(migrate, "migration-second")
            assert second_attempt.wait(3)
            release_first.set()
            first.result(timeout=3)
            release_second.set()
            second.result(timeout=3)
    finally:
        release_first.set()
        release_second.set()
    after = state.get_manager_run(run_id)["item"]
    assert after == {**before, "state_version": 1}
    assert transactions == [True, True]
    with sqlite3.connect(state.path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert len([row for row in conn.execute("PRAGMA table_info(manager_runs)") if row[1] == "state_version"]) == 1


def test_failed_legacy_migration_rolls_back_all_added_columns_and_keeps_rows(tmp_path, monkeypatch):
    state = StoreState(tmp_path / "legacy.sqlite3")
    run_id = _run(state)
    with sqlite3.connect(state.path) as conn:
        conn.execute("ALTER TABLE manager_runs DROP COLUMN workflow_id")
        conn.execute("ALTER TABLE manager_runs DROP COLUMN state_version")
        before_columns = list(conn.execute("PRAGMA table_info(manager_runs)"))
        before_rows = list(conn.execute("SELECT * FROM manager_runs"))
        before_events = list(conn.execute("SELECT * FROM manager_run_events"))
    real_connect = sqlite3.connect

    class FailedMigrationConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith("ALTER TABLE manager_runs ADD COLUMN state_version"):
                raise sqlite3.OperationalError("synthetic_migration_failure")
            return super().execute(sql, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(
            sqlite3, "connect", lambda *args, **kwargs: real_connect(*args, **kwargs, factory=FailedMigrationConnection)
        )
        with pytest.raises(sqlite3.OperationalError, match="synthetic_migration_failure"):
            state.initialize()
    with sqlite3.connect(state.path) as conn:
        assert list(conn.execute("PRAGMA table_info(manager_runs)")) == before_columns
        assert list(conn.execute("SELECT * FROM manager_runs")) == before_rows
        assert list(conn.execute("SELECT * FROM manager_run_events")) == before_events
    state.initialize()
    readback = state.get_manager_run(run_id)["item"]
    assert readback["workflow_id"] == "" and readback["state_version"] == 1
    assert readback["status"] == "executing"
