from __future__ import annotations

import ast
from contextlib import closing
import inspect
import sqlite3
from textwrap import dedent
from types import SimpleNamespace

import pytest

from autostop_manager import storage


class _LookupConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lookups = []
        self.lookup_steps = 0
        self.lookup_active = False
        self.set_trace_callback(self._trace)
        self.set_progress_handler(self._progress, 1)

    def _trace(self, sql):
        normalized = " ".join(sql.split())
        self.lookup_active = normalized.startswith("SELECT * FROM manager_runs WHERE idempotency_key =")
        if self.lookup_active:
            self.lookups.append(sql)

    def _progress(self):
        if self.lookup_active:
            self.lookup_steps += 1
        return 0


def _generic_schema():
    # Extract the existing generic tables/indexes without executing initialize's
    # other schema or migration work.
    tree = ast.parse(dedent(inspect.getsource(storage.StoreState.initialize)))
    prefixes = (
        "CREATE TABLE IF NOT EXISTS manager_runs (",
        "CREATE TABLE IF NOT EXISTS manager_run_events (",
        "CREATE INDEX IF NOT EXISTS idx_manager_runs_status ",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_manager_runs_idempotency ",
    )
    statements = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"execute", "executescript"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            statements.extend(
                statement
                for statement in node.args[0].value.split(";")
                if " ".join(statement.split()).startswith(prefixes)
            )
    assert len(statements) == 4
    return ";".join(statements)


def _snapshot(path):
    with closing(sqlite3.connect(path)) as connection:
        return (
            connection.execute("SELECT * FROM manager_runs ORDER BY id").fetchall(),
            connection.execute("SELECT * FROM manager_run_events ORDER BY id").fetchall(),
            connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall(),
        )


@pytest.fixture
def history(tmp_path, monkeypatch):
    schema = _generic_schema()
    paths = []
    now = "2026-01-01T00:00:00Z"

    def forbidden(*_args, **_kwargs):
        pytest.fail("generic lookup tests must not use a default DB or Store cleanup")

    monkeypatch.setattr(storage.StoreState, "initialize", lambda _self: None)
    monkeypatch.setattr(storage, "get_db_path", forbidden)
    monkeypatch.setattr(storage, "_cleanup_store_owner_runs", forbidden)
    monkeypatch.setattr(storage, "_now", lambda: now)

    def make(count):
        path = tmp_path / f"manager-{len(paths)}.sqlite3"
        paths.append(path)
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(schema)
            connection.executemany(
                "INSERT INTO manager_runs(workflow_id, intent, idempotency_key, status, started_at, updated_at) "
                "VALUES (?, ?, ?, 'completed', ?, ?)",
                [
                    (
                        "synthetic_workflow" if index == count - 1 else "unrelated_workflow",
                        "synthetic_intent",
                        "target-key" if index == count - 1 else ("" if index % 2 == 0 else f"unrelated-{index}"),
                        now,
                        now,
                    )
                    for index in range(count)
                ],
            )
            connection.commit()
            assert connection.execute("SELECT COUNT(*) FROM manager_runs WHERE idempotency_key = ''").fetchone() == (
                count // 2,
            )
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            assert set(tables) == {("manager_runs",), ("manager_run_events",)}
        connections = []

        def connect(actual_path):
            assert actual_path == path
            connection = sqlite3.connect(path, factory=_LookupConnection)
            connections.append(connection)
            return connection

        # Keep shared sqlite3 untouched and exercise the real connect/start body.
        monkeypatch.setattr(storage, "sqlite3", SimpleNamespace(connect=connect, Row=sqlite3.Row))
        return SimpleNamespace(path=path, store=storage.StoreState(path), connections=connections)

    return make


@pytest.mark.parametrize("mode", ["replay", "new", "conflict", "normalized"])
def test_start_lookup_work_does_not_scale_with_unrelated_history(history, mode):
    steps = []
    plans = []
    for count in (500, 5000):
        state = history(count)
        before = _snapshot(state.path)
        key = "new-key" if mode == "new" else " \t target-key\n " if mode == "normalized" else "target-key"
        arguments = {
            "workflow_id": "different_workflow" if mode == "conflict" else "synthetic_workflow",
            "intent": "synthetic_intent",
            "idempotency_key": key,
            "request_id": "synthetic-request",
            "correlation_id": "synthetic-correlation",
        }
        result = state.store.start_workflow_run(**arguments)
        after = _snapshot(state.path)
        assert after[2] == before[2]
        assert len(state.connections) == 1
        measured = state.connections[0]
        assert len(measured.lookups) == 1
        steps.append(measured.lookup_steps)
        with closing(sqlite3.connect(state.path)) as connection:
            plans.append(connection.execute("EXPLAIN QUERY PLAN " + measured.lookups[0]).fetchall())

        if mode == "new":
            assert result["ok"] and result["deduplicated"] is False
            assert result["status"] == "planned" and result["id"] == count + 1
            assert after[0][:-1] == before[0]
            assert len(after[1]) == 1 and after[1][0][1:3] == (result["id"], "workflow_started")
            replay = state.store.start_workflow_run(**arguments)
            assert replay["ok"] and replay["deduplicated"] and replay["id"] == result["id"]
            assert _snapshot(state.path) == after
        elif mode == "conflict":
            assert result == {
                "ok": False,
                "error": "idempotency_key_conflict",
                "id": count,
                "status": "completed",
                "conflict_fields": ["workflow_id"],
            }
            assert after == before
        else:
            assert result["ok"] and result["deduplicated"]
            assert result["id"] == count and result["idempotency_key"] == "target-key"
            assert result["status"] == "completed"
            assert after == before

    assert max(steps) < 200, steps
    assert steps[1] <= steps[0] + 50, steps
    for plan in plans:
        assert any("SEARCH manager_runs USING INDEX idx_manager_runs_idempotency" in row[3] for row in plan), plan


@pytest.mark.parametrize("key", ["", " \t\n "])
def test_blank_keys_are_rejected_before_lookup_with_legacy_empty_rows(history, key):
    state = history(4)
    before = _snapshot(state.path)
    result = state.store.start_workflow_run(
        workflow_id="synthetic_workflow", intent="synthetic_intent", idempotency_key=key
    )
    assert result == {"ok": False, "error": "idempotency_key is required"}
    assert state.connections == []
    assert _snapshot(state.path) == before
