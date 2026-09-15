from __future__ import annotations

import json
import sqlite3

import pytest

from autostop_manager import storage as storage_module
from autostop_manager.storage import StoreState


def _insert_store_conductor_run(
    store: StoreState,
    *,
    status: str,
    checkpoint: dict | str,
    external_step_status: str | None = None,
) -> None:
    store.initialize()
    now = storage_module._now()
    checkpoint_json = checkpoint if isinstance(checkpoint, str) else json.dumps(checkpoint)
    with store.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO manager_runs (intent, workflow_id, status, checkpoint_json, started_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("store_quote_conductor", "store_quote_conductor", status, checkpoint_json, now, now),
        )
        if external_step_status is not None:
            conn.execute(
                """
                INSERT INTO manager_run_external_steps
                    (run_id, step_id, connector, action, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (cursor.lastrowid, "legacy-step", "telegram", "send", external_step_status, now, now),
            )


def test_store_quote_conductor_release_readiness_is_aggregate_and_read_only(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    store = StoreState(db_path)
    store.initialize()
    before = db_path.read_bytes()

    ready = store.store_quote_conductor_release_readiness()

    assert ready == {
        "ok": True,
        "format": "store_quote_conductor_release_readiness_v1",
        "read_only": True,
        "active_total": 0,
        "legacy_active_total": 0,
        "active_by_status": {},
        "active_by_phase": {},
        "legacy_by_phase": {},
        "runs_with_external_steps": 0,
        "pending_external_steps": 0,
        "legacy_checkpoint_count": 0,
        "blocking_reasons": [],
    }
    assert db_path.read_bytes() == before


def test_store_quote_conductor_release_readiness_blocks_active_legacy_state(tmp_path):
    store = StoreState(tmp_path / "memory.sqlite3")
    _insert_store_conductor_run(
        store,
        status="external_wait",
        checkpoint={"phase": "clarifying", "telegram_context_hash": "a" * 64},
        external_step_status="pending",
    )

    result = store.store_quote_conductor_release_readiness()

    assert result["ok"] is False
    assert result["active_total"] == result["legacy_active_total"] == 1
    assert result["active_by_status"] == {"external_wait": 1}
    assert result["legacy_by_phase"] == {"clarifying": 1}
    assert result["runs_with_external_steps"] == result["pending_external_steps"] == 1
    assert result["legacy_checkpoint_count"] == 1
    assert result["blocking_reasons"] == [
        "legacy_store_quote_conductor_active_runs",
        "legacy_store_quote_conductor_external_wait_runs",
        "legacy_store_quote_conductor_checkpoints",
        "legacy_store_quote_conductor_external_steps",
    ]


def test_store_quote_conductor_release_readiness_accepts_current_and_terminal_legacy_runs(tmp_path):
    store = StoreState(tmp_path / "memory.sqlite3")
    _insert_store_conductor_run(store, status="planned", checkpoint={"phase": "new"})
    _insert_store_conductor_run(store, status="completed", checkpoint={"phase": "clarifying"})

    result = store.store_quote_conductor_release_readiness()

    assert result["ok"] is True
    assert result["active_total"] == 1
    assert result["active_by_phase"] == {"new": 1}


def test_store_quote_conductor_release_readiness_blocks_a_malformed_active_checkpoint(tmp_path):
    store = StoreState(tmp_path / "memory.sqlite3")
    _insert_store_conductor_run(store, status="executing", checkpoint="not-json")

    result = store.store_quote_conductor_release_readiness()

    assert result["ok"] is False
    assert result["legacy_by_phase"] == {"missing": 1}
    assert result["legacy_checkpoint_count"] == 1
    assert "legacy_store_quote_conductor_checkpoints" in result["blocking_reasons"]


def test_store_quote_conductor_release_readiness_fails_closed_without_a_database(tmp_path):
    path = tmp_path / "missing.sqlite3"

    result = StoreState(path).store_quote_conductor_release_readiness()

    assert result == {
        "ok": False,
        "read_only": True,
        "error": "store_quote_conductor_release_database_unavailable",
        "blocking_reasons": ["store_quote_conductor_release_database_unavailable"],
    }
    assert not path.exists()


def test_store_connect_context_closes_connection(tmp_path):
    store = StoreState(tmp_path / "memory.sqlite3")

    with store.connect() as conn:
        conn.execute("SELECT 1").fetchone()

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
