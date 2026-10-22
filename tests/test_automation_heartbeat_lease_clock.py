from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from autostop_manager import automation_registry
from autostop_manager.automation_registry import AutomationStore, isoformat


START = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
OWNER = "synthetic-heartbeat-worker"


@pytest.fixture
def heartbeat_state(tmp_path, monkeypatch):
    clock = [START]
    monkeypatch.setattr(automation_registry, "utc_now", lambda: clock[0])
    store = AutomationStore(tmp_path / "registry.sqlite3")

    def bootstrap(connection, command_id):
        job = store.create_from_template(
            connection,
            template_id="crm_digest_v1",
            payload={"template_id": "crm_digest_v1"},
            actor_hash="synthetic-actor",
        )["job"]
        enabled = store.set_enabled(connection, job_id=job["job_id"], enabled=True, expected_revision=job["revision"])[
            "job"
        ]
        return store.enqueue_run(
            connection,
            job_id=job["job_id"],
            command_id=command_id,
            expected_revision=enabled["revision"],
        )

    queued = store.execute_command(
        source="codex",
        idempotency_key="synthetic-heartbeat-bootstrap-0001",
        operation="run_now",
        request_hash="synthetic-heartbeat-request",
        actor_hash="synthetic-actor",
        mutation=bootstrap,
    )
    claim = store.claim_next_run(owner=OWNER, lease_seconds=10)
    assert claim and claim["run_id"] == queued["run_id"]
    assert claim["lease_until"] == isoformat(START + timedelta(seconds=10))
    return store, claim, clock


def _heartbeat(store, claim):
    return store.heartbeat_run(
        job_id=claim["job_id"],
        run_id=claim["run_id"],
        owner=OWNER,
        fencing_token=claim["fencing_token"],
        lease_seconds=60,
    )


def _snapshot(store, claim):
    with store.connect() as connection:
        runtime = connection.execute(
            "SELECT * FROM manager_automation_runtime WHERE job_id = ?", (claim["job_id"],)
        ).fetchone()
        run = connection.execute(
            "SELECT * FROM manager_automation_runs WHERE run_id = ?", (claim["run_id"],)
        ).fetchone()
    return {"runtime": dict(runtime), "run": dict(run)}


def test_healthy_heartbeat_renews_lease_without_changing_run(heartbeat_state):
    store, claim, clock = heartbeat_state
    before = _snapshot(store, claim)
    clock[0] = START + timedelta(seconds=5)

    assert _heartbeat(store, claim) is True

    assert _snapshot(store, claim) == {
        "runtime": {
            **before["runtime"],
            "heartbeat_at": isoformat(clock[0]),
            "lease_until": isoformat(clock[0] + timedelta(seconds=60)),
            "updated_at": isoformat(clock[0]),
        },
        "run": before["run"],
    }


def test_heartbeat_started_after_expiry_does_not_mutate_state(heartbeat_state):
    store, claim, clock = heartbeat_state
    before = _snapshot(store, claim)
    clock[0] = START + timedelta(seconds=11)

    assert _heartbeat(store, claim) is False

    assert _snapshot(store, claim) == before


def test_heartbeat_waiting_for_writer_cannot_renew_expired_lease(heartbeat_state, monkeypatch):
    store, claim, clock = heartbeat_state
    before = _snapshot(store, claim)
    fd_before = len(list(Path("/proc/self/fd").iterdir()))
    clock[0] = START + timedelta(seconds=9)
    connect = AutomationStore.connect
    begin_seen = threading.Event()
    done = threading.Event()
    statements = []
    outcomes = []
    failures = []

    @contextmanager
    def traced_connect(self):
        with connect(self) as connection:
            if threading.current_thread() is worker:

                def trace(statement):
                    statements.append((statement, clock[0]))
                    if statement.strip().upper() == "BEGIN IMMEDIATE":
                        begin_seen.set()

                connection.set_trace_callback(trace)
            yield connection

    def heartbeat_worker():
        try:
            outcomes.append(_heartbeat(store, claim))
        except BaseException as exc:  # noqa: BLE001 - propagate thread failures to the test.
            failures.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=heartbeat_worker, name="synthetic-heartbeat", daemon=True)
    monkeypatch.setattr(AutomationStore, "connect", traced_connect)
    try:
        with connect(store) as writer:
            writer.execute("BEGIN IMMEDIATE")
            try:
                worker.start()
                assert begin_seen.wait(timeout=3), "heartbeat did not attempt its write transaction"
                # Hold a real SQLite writer lock briefly; virtual time controls expiry.
                assert not done.wait(timeout=0.05), "heartbeat completed while the writer lock was held"
                assert writer.in_transaction
                assert len(statements) == 1 and statements[0][0].strip().upper() == "BEGIN IMMEDIATE"
                clock[0] = START + timedelta(seconds=11)
            finally:
                writer.commit()
        assert done.wait(timeout=3), "heartbeat did not finish after the writer lock was released"
    finally:
        if worker.ident is not None:
            worker.join(timeout=3)

    assert not worker.is_alive(), "heartbeat thread leaked"
    assert failures == []
    after = _snapshot(store, claim)
    assert len(list(Path("/proc/self/fd").iterdir())) == fd_before
    updates = [at for sql, at in statements if sql.lstrip().upper().startswith("UPDATE MANAGER_AUTOMATION_RUNTIME")]
    assert updates == [START + timedelta(seconds=11)]
    assert outcomes == [False], (
        f"expired heartbeat returned {outcomes}: old lease={before['runtime']['lease_until']}, "
        f"heartbeat_at={after['runtime']['heartbeat_at']}, new lease={after['runtime']['lease_until']}"
    )
    assert after == before
