from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from autostop_manager.storage import ManagerMemoryStore


def test_manager_run_ledger_records_events_and_finish_state(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    started = store.start_manager_run(
        intent="board_cleanup",
        query="Приберись",
        dry_run=True,
        source="test",
        metadata={"scope": "active_board"},
    )
    assert started["ok"] is True

    event = store.record_manager_run_event(
        started["id"],
        event_type="planned_action",
        message="Update card summaries without moving cards",
        target_type="card",
        target_id="C-1",
        payload={"write": "description"},
    )
    assert event["ok"] is True

    finished = store.finish_manager_run(
        started["id"],
        status="completed",
        summary="Checked 1 card",
        verification={"cards_moved": 0},
    )
    assert finished["ok"] is True

    listed = store.list_manager_runs(limit=5, include_events=True)

    assert listed["ok"] is True
    assert listed["items"][0]["id"] == started["id"]
    assert listed["items"][0]["status"] == "completed"
    assert listed["items"][0]["dry_run"] is True
    assert listed["items"][0]["events"][0]["event_type"] == "planned_action"
    assert listed["items"][0]["verification"]["cards_moved"] == 0


def test_v2_workflow_is_idempotent_resumable_and_keeps_external_steps_refs_only(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    started = store.start_workflow_run(
        workflow_id="crm_gmail_workflow",
        intent="crm_gmail_workflow",
        query="ответь клиенту по карточке CRM",
        idempotency_key="crm-gmail-c1-v1",
        scope={"card_id": "C-1"},
        selected_ids=["C-1"],
    )
    duplicate = store.start_workflow_run(
        workflow_id="crm_gmail_workflow",
        intent="crm_gmail_workflow",
        query="ответь клиенту по карточке CRM",
        idempotency_key="crm-gmail-c1-v1",
    )
    assert duplicate["id"] == started["id"]
    assert duplicate["deduplicated"] is True

    assert store.transition_workflow_run(started["id"], status="executing")["ok"] is True
    checkpoint = store.checkpoint_workflow_run(
        started["id"],
        checkpoint={"phase": "crm_verified", "next_action": "send Gmail reply"},
    )
    assert checkpoint["ok"] is True

    rejected = store.register_external_step(
        started["id"],
        step_id="gmail-send-1",
        connector="gmail",
        action="send",
        request_refs={"thread_id": "thread-1", "body": "must never persist"},
    )
    assert rejected["ok"] is False
    assert rejected["error"] == "raw_external_body_not_allowed_in_manager_ledger"

    waiting = store.register_external_step(
        started["id"],
        step_id="gmail-send-1",
        connector="gmail",
        action="send",
        request_refs={"thread_id": "thread-1", "recipient_count": 1},
    )
    assert waiting["workflow_status"] == "external_wait"
    assert store.resume_workflow_run(started["id"])["error"] == "external_steps_pending"

    completed = store.complete_external_step(
        started["id"],
        step_id="gmail-send-1",
        result_refs={"message_id": "message-9", "thread_id": "thread-1", "status": "sent"},
    )
    assert completed["ok"] is True
    assert (
        store.complete_external_step(
            started["id"],
            step_id="gmail-send-1",
            result_refs={"message_id": "message-9", "thread_id": "thread-1", "status": "sent"},
        )["deduplicated"]
        is True
    )
    assert store.resume_workflow_run(started["id"])["status"] == "executing"
    assert store.transition_workflow_run(started["id"], status="verifying")["ok"] is True
    assert (
        store.transition_workflow_run(
            started["id"],
            status="completed",
            verification={"crm_readback": True, "gmail_result_ref": True},
        )["ok"]
        is True
    )

    raw_db = (tmp_path / "memory.sqlite3").read_bytes()
    assert b"must never persist" not in raw_db
    status = store.get_manager_run(started["id"], include_events=True, include_external_steps=True)
    assert status["item"]["status"] == "completed"
    assert status["item"]["external_steps"][0]["result_refs"]["message_id"] == "message-9"


def test_v2_idempotency_rejects_changed_scope_selected_ids_or_mode(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    first = store.start_workflow_run(
        workflow_id="finance:create_cash_transaction",
        intent="finance_create_cash_transaction",
        idempotency_key="finance-key-1",
        scope={"operation": "create_cash_transaction", "request_fingerprint": "aaa"},
        selected_ids=["cashbox-1"],
        dry_run=False,
    )

    changed_scope = store.start_workflow_run(
        workflow_id="finance:create_cash_transaction",
        intent="finance_create_cash_transaction",
        idempotency_key="finance-key-1",
        scope={"operation": "create_cash_transaction", "request_fingerprint": "bbb"},
        selected_ids=["cashbox-1"],
        dry_run=False,
    )
    changed_ids = store.start_workflow_run(
        workflow_id="finance:create_cash_transaction",
        intent="finance_create_cash_transaction",
        idempotency_key="finance-key-1",
        scope={"operation": "create_cash_transaction", "request_fingerprint": "aaa"},
        selected_ids=["cashbox-2"],
        dry_run=False,
    )
    changed_mode = store.start_workflow_run(
        workflow_id="finance:create_cash_transaction",
        intent="finance_create_cash_transaction",
        idempotency_key="finance-key-1",
        scope={"operation": "create_cash_transaction", "request_fingerprint": "aaa"},
        selected_ids=["cashbox-1"],
        dry_run=True,
    )

    assert first["ok"] is True
    assert changed_scope["ok"] is False
    assert changed_scope["conflict_fields"] == ["scope"]
    assert changed_ids["ok"] is False
    assert changed_ids["conflict_fields"] == ["selected_ids"]
    assert changed_mode["ok"] is False
    assert changed_mode["conflict_fields"] == ["dry_run"]


def test_v2_concurrent_idempotent_starts_deduplicate_without_integrity_errors(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    store.initialize()
    workers = 24
    barrier = Barrier(workers)

    def start(_index):
        barrier.wait()
        return store.start_workflow_run(
            workflow_id="crm_gmail_workflow",
            intent="crm_gmail_workflow",
            query="ответь клиенту",
            idempotency_key="concurrent-same-key",
            scope={"card_id": "C-1"},
            selected_ids=["C-1"],
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(start, range(workers)))

    assert all(result["ok"] is True for result in results)
    assert len({result["id"] for result in results}) == 1
    assert sum(result["deduplicated"] is False for result in results) == 1


def test_v2_rejects_invalid_state_transition(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    started = store.start_workflow_run(
        workflow_id="timer_floor_control",
        intent="timer_floor",
        query="подними таймеры",
        idempotency_key="timer-floor-v2",
    )

    result = store.transition_workflow_run(started["id"], status="completed")

    assert result["ok"] is False
    assert result["error"] == "invalid_workflow_transition"
    assert result["allowed"] == ["cancelled", "executing", "failed"]


def test_v2_completed_rejects_explicit_executor_or_verification_failure(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    failure_evidence = [
        ({"executor_ok": False, "passed": True}, ["executor_ok"]),
        ({"executor": False, "passed": True}, ["executor"]),
        ({"executor": "failed", "passed": True}, ["executor"]),
        ({"verification": False}, ["verification"]),
        ({"verification_passed": False}, ["verification_passed"]),
        ({"verification": {"passed": False}}, ["verification.passed"]),
        ({"verification": {"status": "failed"}}, ["verification.status"]),
    ]

    for index, (verification, expected_paths) in enumerate(failure_evidence):
        started = store.start_workflow_run(
            workflow_id="board",
            intent="board_write",
            idempotency_key=f"completion-failure-{index}",
        )
        executing = store.transition_workflow_run(started["id"], status="executing", expected_state_version=1)
        verifying = store.transition_workflow_run(
            started["id"],
            status="verifying",
            expected_state_version=executing["state_version"],
        )

        rejected = store.transition_workflow_run(
            started["id"],
            status="completed",
            verification=verification,
            expected_state_version=verifying["state_version"],
        )

        assert rejected["ok"] is False
        assert rejected["error"] == "verification_failed_before_completion"
        assert rejected["failure_paths"] == expected_paths
        status = store.get_manager_run(started["id"], include_events=False)
        assert status["item"]["status"] == "verifying"
        assert status["item"]["state_version"] == verifying["state_version"]


def test_v2_completed_dedup_still_rejects_new_failed_evidence(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    started = store.start_workflow_run(
        workflow_id="board",
        intent="board_write",
        idempotency_key="completed-dedup-verification",
    )
    executing = store.transition_workflow_run(started["id"], status="executing")
    verifying = store.transition_workflow_run(started["id"], status="verifying")
    completed = store.transition_workflow_run(
        started["id"],
        status="completed",
        verification={"executor_ok": True, "verification_passed": True},
        expected_state_version=verifying["state_version"],
    )
    assert completed["ok"] is True

    rejected = store.transition_workflow_run(
        started["id"],
        status="completed",
        verification={"executor_ok": False},
        expected_state_version=completed["state_version"],
    )

    assert rejected["ok"] is False
    assert rejected["error"] == "verification_failed_before_completion"
    assert rejected["failure_paths"] == ["executor_ok"]
    assert executing["state_version"] == 2


def test_v2_mutable_lifecycle_calls_enforce_expected_state_version_cas(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")
    started = store.start_workflow_run(
        workflow_id="crm_gmail_workflow",
        intent="crm_gmail_workflow",
        idempotency_key="workflow-cas-all-mutations",
    )

    stale_transition = store.transition_workflow_run(started["id"], status="executing", expected_state_version=0)
    assert stale_transition == {
        "ok": False,
        "error": "workflow_state_conflict",
        "run_id": started["id"],
        "expected_state_version": 0,
        "current_state_version": 1,
    }
    executing = store.transition_workflow_run(started["id"], status="executing", expected_state_version=1)
    assert executing["state_version"] == 2

    stale_checkpoint = store.checkpoint_workflow_run(
        started["id"], checkpoint={"phase": "stale"}, expected_state_version=1
    )
    assert stale_checkpoint["error"] == "workflow_state_conflict"
    assert stale_checkpoint["current_state_version"] == 2
    checkpoint = store.checkpoint_workflow_run(started["id"], checkpoint={"phase": "ready"}, expected_state_version=2)
    assert checkpoint["state_version"] == 3

    stale_wait = store.register_external_step(
        started["id"],
        step_id="gmail-send-cas",
        connector="gmail",
        action="send",
        request_refs={"thread_id": "thread-cas"},
        expected_state_version=2,
    )
    assert stale_wait["error"] == "workflow_state_conflict"
    waiting = store.register_external_step(
        started["id"],
        step_id="gmail-send-cas",
        connector="gmail",
        action="send",
        request_refs={"thread_id": "thread-cas"},
        expected_state_version=3,
    )
    assert waiting["state_version"] == 4

    stale_complete = store.complete_external_step(
        started["id"],
        step_id="gmail-send-cas",
        result_refs={"message_id": "message-cas"},
        expected_state_version=3,
    )
    assert stale_complete["error"] == "workflow_state_conflict"
    completed_step = store.complete_external_step(
        started["id"],
        step_id="gmail-send-cas",
        result_refs={"message_id": "message-cas"},
        expected_state_version=4,
    )
    assert completed_step["state_version"] == 5

    stale_resume = store.resume_workflow_run(started["id"], expected_state_version=4)
    assert stale_resume["error"] == "workflow_state_conflict"
    resumed = store.resume_workflow_run(started["id"], expected_state_version=5)
    assert resumed["status"] == "executing"
    assert resumed["state_version"] == 6

    stale_cancel = store.cancel_workflow_run(started["id"], reason="stale", expected_state_version=5)
    assert stale_cancel["error"] == "workflow_state_conflict"
    cancelled = store.cancel_workflow_run(started["id"], reason="done", expected_state_version=6)
    assert cancelled["status"] == "cancelled"
    assert cancelled["state_version"] == 7


def test_initialize_migrates_current_manager_run_schema_without_losing_legacy_rows(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE manager_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent TEXT NOT NULL DEFAULT '',
                query TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'running',
                dry_run INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'codex',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                summary TEXT NOT NULL DEFAULT '',
                verification_json TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL,
                finished_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE manager_run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                target_type TEXT NOT NULL DEFAULT '',
                target_id TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            INSERT INTO manager_runs (
                intent, query, status, dry_run, source, metadata_json,
                summary, verification_json, started_at, updated_at
            ) VALUES (
                'legacy', 'old run', 'running', 0, 'codex', '{}', '', '{}',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
            );
            """
        )

    store = ManagerMemoryStore(db_path)
    store.initialize()

    with sqlite3.connect(db_path) as conn:
        run_columns = {row[1] for row in conn.execute("PRAGMA table_info(manager_runs)")}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "workflow_id",
        "request_id",
        "idempotency_key",
        "correlation_id",
        "actor",
        "scope_json",
        "selected_ids_json",
        "checkpoint_json",
        "compensation_json",
        "state_version",
    }.issubset(run_columns)
    assert "manager_run_external_steps" in tables

    legacy = store.get_manager_run(1, include_events=False, include_external_steps=True)
    assert legacy["item"]["status"] == "running"
    assert legacy["item"]["checkpoint"] == {}
    assert legacy["item"]["external_steps"] == []
