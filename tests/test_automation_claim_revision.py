"""Exact run revision provenance and lossless version6 migration, offline."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from autostop_manager import automation_registry as registry
from autostop_manager.automation_control import AUTOMATION_CONTROL_PROTOCOL, AutomationControlService


START = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
OWNER = "synthetic-revision-worker"
ACTOR = {"kind": "codex", "id": "synthetic-revision-owner", "is_admin": True}
LEGACY_JOB = "auto_" + "1" * 24
LEGACY_RUNNING = "run_" + "1" * 32
LEGACY_TERMINAL = "run_" + "2" * 32
LEGACY_QUEUED = "run_" + "3" * 32
LEGACY_KEY = "revision-legacy-running-0001"
TABLES = (
    "manager_automations",
    "manager_automation_runtime",
    "manager_automation_runs",
    "manager_automation_commands",
    "manager_automation_cursors",
    "manager_automation_outbox",
    "manager_automation_controller_runtime",
    "manager_automation_schema",
    "manager_automation_system_timers",
)
# Frozen pre-v7 table layout: this fixture must not be made by initialize(),
# nor pretend a current table with NULL values is an old schema.
V6_RUNS_TABLE = """
CREATE TABLE manager_automation_runs (
    run_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    command_id TEXT,
    scheduled_for TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    lease_owner_hash TEXT,
    fencing_token INTEGER,
    result_code TEXT,
    coalesced_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(job_id) REFERENCES manager_automations(job_id) ON DELETE CASCADE,
    FOREIGN KEY(command_id) REFERENCES manager_automation_commands(command_id) ON DELETE SET NULL
)
"""


class NoTimers:
    def __getattr__(self, name):
        raise AssertionError(f"Timer/service operation forbidden: {name}")


def command(service, operation, payload, key, revision=None):
    message = {
        "protocol": AUTOMATION_CONTROL_PROTOCOL,
        "request_id": str(uuid4()),
        "operation": operation,
        "actor": ACTOR,
        "payload": payload,
        "idempotency_key": key,
    }
    if revision is not None:
        message["expected_revision"] = revision
    return service.handle(message)


def run_now(service, job, key):
    return command(service, "run_now", {"job_id": job["job_id"]}, key, job["revision"])


def snapshot(store):
    with closing(sqlite3.connect(store.path)) as connection:
        connection.row_factory = sqlite3.Row
        return {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in TABLES
        }


def run_row(store, run_id):
    return next(row for row in snapshot(store)["manager_automation_runs"] if row["run_id"] == run_id)


def current(store, claim):
    return store.run_claim_is_current(
        job_id=claim["job_id"],
        run_id=claim["run_id"],
        owner=OWNER,
        fencing_token=claim["fencing_token"],
        claimed_revision=claim["claimed_revision"],
    )


@dataclass
class Case:
    store: registry.AutomationStore
    service: AutomationControlService
    clock: list[datetime]
    job: dict


@pytest.fixture
def case(tmp_path, monkeypatch):
    clock = [START]
    monkeypatch.setattr(registry, "utc_now", lambda: clock[0])
    store = registry.AutomationStore(tmp_path / "manager.sqlite3")
    service = AutomationControlService(store, timer_controller=NoTimers())
    created = command(
        service,
        "create_from_template",
        {"template_id": "crm_digest_v1", "schedule": {"every_minutes": 20, "timezone": "UTC"}},
        "revision-create-0001",
    )["job"]
    job = command(
        service,
        "set_enabled",
        {"job_id": created["job_id"], "enabled": True},
        "revision-enable-0001",
        created["revision"],
    )["job"]
    return Case(store, service, clock, job)


def test_public_run_now_after_revision_change_survives_old_claim_cancellation(case):
    assert case.store.reconcile_next_job()["applied"] is True
    first = run_now(case.service, case.job, "revision-first-run-0001")
    original = case.store.claim_next_run(owner=OWNER, lease_seconds=600)
    assert original is not None and original["run_id"] == first["run_id"]
    assert current(case.store, original) is True
    case.clock[0] += timedelta(seconds=1)
    changed = command(
        case.service,
        "set_schedule",
        {"job_id": case.job["job_id"], "schedule": {"every_minutes": 30}},
        "revision-change-schedule-0001",
        case.job["revision"],
    )["job"]
    assert changed["revision"] == original["claimed_revision"] + 1
    assert case.store.reconcile_next_job()["applied"] is False
    assert current(case.store, original) is False
    before = run_row(case.store, original["run_id"])

    accepted = run_now(case.service, changed, "revision-new-run-0001")

    # This assertion fails on v6 for the public behavior, before any v7 field read.
    assert accepted["queued"] is True and accepted["coalesced"] is False
    assert accepted["run_id"] != original["run_id"]
    assert run_row(case.store, original["run_id"]) == before
    assert before["claimed_revision"] == original["claimed_revision"]
    assert run_row(case.store, accepted["run_id"])["claimed_revision"] is None
    reopened = registry.AutomationStore(case.store.path)
    service = AutomationControlService(reopened, timer_controller=NoTimers())
    pending = snapshot(reopened)
    assert run_now(service, changed, "revision-new-run-0001") == {**accepted, "idempotent_replay": True}
    assert snapshot(reopened) == pending
    assert (
        reopened.cancel_run_claim(
            job_id=original["job_id"], run_id=original["run_id"], owner=OWNER, fencing_token=original["fencing_token"]
        )
        is True
    )

    replacement = reopened.claim_next_run(owner=OWNER)

    assert replacement is not None and replacement["run_id"] == accepted["run_id"]
    assert replacement["claimed_revision"] == changed["revision"]
    assert run_row(reopened, replacement["run_id"])["claimed_revision"] == changed["revision"]
    historical = run_row(reopened, original["run_id"])
    assert historical["status"] == "cancelled"
    assert historical["claimed_revision"] == original["claimed_revision"]
    assert current(reopened, replacement) is True
    after_claim = snapshot(reopened)
    assert run_now(service, changed, "revision-new-run-0001") == {**accepted, "idempotent_replay": True}
    assert snapshot(reopened) == after_claim


def test_healthy_manual_claim_before_reconcile_keeps_exact_revision_and_coalescing(case):
    state = snapshot(case.store)
    assert state["manager_automation_runtime"][0]["applied_revision"] != case.job["revision"]
    first = run_now(case.service, case.job, "revision-current-first-0001")
    assert run_row(case.store, first["run_id"])["claimed_revision"] is None

    claim = case.store.claim_next_run(owner=OWNER)

    assert claim is not None and claim["run_id"] == first["run_id"]
    assert claim["claimed_revision"] == case.job["revision"]
    assert run_row(case.store, claim["run_id"])["claimed_revision"] == claim["claimed_revision"]
    assert current(case.store, claim) is True
    assert snapshot(case.store)["manager_automation_runtime"][0]["applied_revision"] != claim["claimed_revision"]
    assert run_now(case.service, case.job, "revision-current-repeat-0001") == {
        "queued": False,
        "coalesced": True,
        "run_id": claim["run_id"],
    }


def test_scheduled_claim_persists_returned_revision_through_completion(case):
    applied = case.store.reconcile_next_job()
    assert applied is not None and applied["applied"] is True
    case.clock[0] = registry.parse_time(applied["job"]["next_run_at"])

    claim = case.store.claim_next_run(owner=OWNER)

    assert claim is not None and claim["trigger_kind"] == "schedule"
    assert claim["claimed_revision"] == case.job["revision"]
    assert run_row(case.store, claim["run_id"])["claimed_revision"] == claim["claimed_revision"]
    assert (
        case.store.finish_run(
            job_id=claim["job_id"],
            run_id=claim["run_id"],
            owner=OWNER,
            fencing_token=claim["fencing_token"],
            succeeded=True,
            result_code="ok",
        )
        is True
    )
    stored = run_row(registry.AutomationStore(case.store.path), claim["run_id"])
    assert stored["status"] == "succeeded" and stored["claimed_revision"] == claim["claimed_revision"]


def insert(connection, table, **values):
    connection.execute(
        f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )


@pytest.fixture
def legacy_v6(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "utc_now", lambda: START)

    def make(*, queued=True):
        path = tmp_path / "legacy-v6.sqlite3"
        at = registry.isoformat(START)
        owner_hash = hashlib.sha256(OWNER.encode()).hexdigest()
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA user_version = 37")
            for statement in registry.SCHEMA_STATEMENTS:
                if "CREATE TABLE IF NOT EXISTS manager_automation_runs (" in statement:
                    connection.execute(V6_RUNS_TABLE)
                else:
                    connection.execute(statement)
            assert "claimed_revision" not in {
                row[1] for row in connection.execute("PRAGMA table_info(manager_automation_runs)")
            }
            insert(connection, "manager_automation_schema", component="automation_center", version=6, upgraded_at=at)
            insert(
                connection,
                "manager_automations",
                job_id=LEGACY_JOB,
                template_id="crm_digest_v1",
                name="Synthetic legacy job",
                desired_state="on",
                schedule_kind="interval",
                every_seconds=1200,
                timezone="UTC",
                schedule_timezone="UTC",
                active_window_json='"24/7"',
                revision=3,
                created_actor_hash="synthetic-owner",
                created_at=at,
                updated_at=at,
            )
            insert(
                connection,
                "manager_automation_runtime",
                job_id=LEGACY_JOB,
                actual_state="running",
                next_run_at=registry.isoformat(START + timedelta(minutes=20)),
                heartbeat_at=at,
                lease_owner_hash=owner_hash,
                lease_until=registry.isoformat(START + timedelta(minutes=10)),
                fencing_token=7,
                applied_revision=3,
                applied_at=at,
                updated_at=at,
            )
            insert(
                connection,
                "manager_automation_controller_runtime",
                singleton=1,
                state="active",
                heartbeat_at=at,
                owner_hash="synthetic-controller",
                started_at=at,
                updated_at=at,
            )
            rows = [
                (LEGACY_RUNNING, "running", LEGACY_KEY, 7),
                (LEGACY_TERMINAL, "succeeded", "revision-legacy-terminal-0001", 6),
            ]
            if queued:
                rows.append((LEGACY_QUEUED, "queued", "revision-legacy-queued-0001", None))
            for index, (run_id, status, key, fence) in enumerate(rows):
                command_id = f"cmd_legacy_{index}"
                actor_hash = registry.technical_actor_hash(ACTOR["kind"], ACTOR["id"])
                request_hash = registry.technical_actor_hash(
                    "automation-command",
                    registry.canonical_json(
                        {
                            "operation": "run_now",
                            "payload": {"job_id": LEGACY_JOB},
                            "expected_revision": 3,
                            "actor_hash": actor_hash,
                        }
                    ),
                )
                insert(
                    connection,
                    "manager_automation_commands",
                    command_id=command_id,
                    source="codex",
                    idempotency_key=key,
                    operation="run_now",
                    request_hash=request_hash,
                    actor_hash=actor_hash,
                    response_json=registry.canonical_json({"queued": True, "coalesced": False, "run_id": run_id}),
                    created_at=at,
                )
                insert(
                    connection,
                    "manager_automation_runs",
                    run_id=run_id,
                    job_id=LEGACY_JOB,
                    trigger_kind="manual",
                    status=status,
                    command_id=command_id,
                    scheduled_for=at,
                    requested_at=at,
                    started_at=None if status == "queued" else at,
                    finished_at=at if status == "succeeded" else None,
                    lease_owner_hash=None if status == "queued" else owner_hash,
                    fencing_token=fence,
                    result_code="ok" if status == "succeeded" else None,
                    coalesced_count=index,
                )
            insert(
                connection,
                "manager_automation_cursors",
                job_id=LEGACY_JOB,
                cursor_name="crm_changes",
                cursor_value="synthetic-technical-cursor",
                revision=9,
                updated_at=at,
            )
            insert(
                connection,
                "manager_automation_outbox",
                outbox_id="out_legacy_sent",
                job_id=LEGACY_JOB,
                run_id=LEGACY_TERMINAL,
                kind="notification_test",
                status="sent",
                idempotency_key="revision-legacy-receipt-0001",
                payload_json="{}",
                attempts=1,
                next_attempt_at=at,
                fencing_token=5,
                result_code="ok",
                created_at=at,
                updated_at=at,
            )
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        path.chmod(0o600)
        return registry.AutomationStore(path)

    return make


def test_real_v6_migration_preserves_rows_leases_replays_and_null_provenance(legacy_v6):
    store = legacy_v6()
    before = snapshot(store)

    store.initialize()

    after = snapshot(store)
    for table in TABLES:
        if table == "manager_automation_schema":
            assert after[table][0]["version"] == registry.AUTOMATION_SCHEMA_VERSION
        elif table == "manager_automation_runs":
            assert [
                {key: value for key, value in row.items() if key != "claimed_revision"} for row in after[table]
            ] == before[table]
            assert all(row["claimed_revision"] is None for row in after[table])
        else:
            assert after[table] == before[table]
    with closing(sqlite3.connect(store.path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (37,)
        column = next(
            row
            for row in connection.execute("PRAGMA table_info(manager_automation_runs)")
            if row[1] == "claimed_revision"
        )
        assert column[2:5] == ("INTEGER", 0, None)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    store.initialize()
    assert snapshot(store) == after
    assert store.cancel_run_claim(job_id=LEGACY_JOB, run_id=LEGACY_RUNNING, owner=OWNER, fencing_token=7) is True
    claim = store.claim_next_run(owner=OWNER)
    assert claim is not None and claim["run_id"] == LEGACY_QUEUED
    assert claim["claimed_revision"] == 3
    assert run_row(store, LEGACY_QUEUED)["claimed_revision"] == 3
    assert run_row(store, LEGACY_RUNNING)["claimed_revision"] is None


def test_legacy_null_queues_fresh_intent_but_old_completion_and_replay_still_work(legacy_v6):
    store = legacy_v6(queued=False)
    store.initialize()
    service = AutomationControlService(store, timer_controller=NoTimers())
    job = {"job_id": LEGACY_JOB, "revision": 3}
    before = snapshot(store)
    old = run_row(store, LEGACY_RUNNING)
    assert old["claimed_revision"] is None
    assert before["manager_automation_runtime"][0]["applied_revision"] == job["revision"]

    accepted = run_now(service, job, "revision-legacy-new-request-0001")

    assert accepted["queued"] is True and accepted["coalesced"] is False
    assert accepted["run_id"] != LEGACY_RUNNING
    assert run_row(store, LEGACY_RUNNING) == old
    pending = snapshot(store)
    original_response = {"queued": True, "coalesced": False, "run_id": LEGACY_RUNNING, "idempotent_replay": True}
    assert run_now(service, job, LEGACY_KEY) == original_response
    assert snapshot(store) == pending
    original = {"job_id": LEGACY_JOB, "run_id": LEGACY_RUNNING, "fencing_token": 7, "claimed_revision": 3}
    assert current(store, original) is True  # NULL alone does not cancel an old in-flight claim.
    assert store.heartbeat_run(job_id=LEGACY_JOB, run_id=LEGACY_RUNNING, owner=OWNER, fencing_token=7) is True
    assert (
        store.finish_run(
            job_id=LEGACY_JOB, run_id=LEGACY_RUNNING, owner=OWNER, fencing_token=7, succeeded=True, result_code="ok"
        )
        is True
    )
    completed = run_row(store, LEGACY_RUNNING)
    assert completed["status"] == "succeeded" and completed["claimed_revision"] is None
    assert run_row(store, accepted["run_id"])["status"] == "queued"
    replacement = store.claim_next_run(owner=OWNER)
    assert replacement is not None and replacement["run_id"] == accepted["run_id"]
    assert run_row(store, replacement["run_id"])["claimed_revision"] == 3
    after_claim = snapshot(store)
    assert run_now(service, job, LEGACY_KEY) == original_response
    assert snapshot(store) == after_claim
