"""Manual requests must coalesce only with runnable pending work.

Uses real temporary SQLite, supported control commands and a virtual clock.
Only the stale-identity guard injects owner/fence disagreement into a run row;
no executor, notifier or system timer is used.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from autostop_manager import automation_registry
from autostop_manager.automation_control import AUTOMATION_CONTROL_PROTOCOL, AutomationControlService
from autostop_manager.automation_registry import AutomationStore


START = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
OWNER = "synthetic-coalescing-worker"
NEW_KEY = "coalescing-new-request-0001"


class NoTimers:
    def __getattr__(self, name):
        raise AssertionError(f"Timer/service operation forbidden: {name}")


def command(service, operation, payload, *, key, revision=None):
    message = {
        "protocol": AUTOMATION_CONTROL_PROTOCOL,
        "request_id": str(uuid4()),
        "operation": operation,
        "actor": {"kind": "codex", "id": "synthetic-coalescing-owner", "is_admin": True},
        "payload": payload,
        "idempotency_key": key,
    }
    if revision is not None:
        message["expected_revision"] = revision
    return service.handle(message)


def run_now(service, job, key=NEW_KEY):
    return command(service, "run_now", {"job_id": job["job_id"]}, key=key, revision=job["revision"])


def snapshot(store):
    with closing(sqlite3.connect(store.path)) as connection:
        connection.row_factory = sqlite3.Row
        return {
            "runs": {row["run_id"]: dict(row) for row in connection.execute("SELECT * FROM manager_automation_runs")},
            "runtime": dict(connection.execute("SELECT * FROM manager_automation_runtime").fetchone()),
            "outbox": [
                dict(row) for row in connection.execute("SELECT * FROM manager_automation_outbox ORDER BY rowid")
            ],
        }


def is_current(store, claim):
    return store.run_claim_is_current(
        job_id=claim["job_id"],
        run_id=claim["run_id"],
        owner=OWNER,
        fencing_token=claim["fencing_token"],
        claimed_revision=claim["claimed_revision"],
    )


@dataclass
class Case:
    store: AutomationStore
    service: AutomationControlService
    clock: list[datetime]
    job: dict
    first: dict


@pytest.fixture
def case(tmp_path, monkeypatch):
    clock = [START]
    monkeypatch.setattr(automation_registry, "utc_now", lambda: clock[0])
    store = AutomationStore(tmp_path / "manager.sqlite3")
    service = AutomationControlService(store, timer_controller=NoTimers())
    created = command(
        service,
        "create_from_template",
        {"template_id": "crm_digest_v1", "schedule": {"every_minutes": 20, "timezone": "UTC"}},
        key="coalescing-create-0001",
    )["job"]
    command(
        service,
        "set_enabled",
        {"job_id": created["job_id"], "enabled": True},
        key="coalescing-enable-0001",
        revision=created["revision"],
    )
    applied = store.reconcile_next_job()
    assert applied is not None and applied["applied"] is True
    job = applied["job"]
    assert job["next_run_at"] == "2026-10-21T12:20:00Z"
    first = run_now(service, job, key="coalescing-first-request-0001")
    assert first["queued"] is True
    return Case(store, service, clock, job, first)


@pytest.mark.parametrize("seconds", [pytest.param(10, id="exact-expiry"), pytest.param(11, id="expired")])
def test_new_manual_intent_survives_expiry_before_recovery_and_replay(case, seconds):
    original = case.store.claim_next_run(owner=OWNER, lease_seconds=10)
    assert original is not None and original["run_id"] == case.first["run_id"]
    case.clock[0] += timedelta(seconds=seconds)
    assert is_current(case.store, original) is False
    before = snapshot(case.store)

    accepted = run_now(case.service, case.job)

    assert accepted["queued"] is True and accepted["coalesced"] is False
    assert accepted["run_id"] != original["run_id"]
    pending = snapshot(case.store)
    assert pending["runs"][original["run_id"]] == before["runs"][original["run_id"]]
    assert pending["runs"][accepted["run_id"]]["status"] == "queued"
    assert pending["outbox"] == before["outbox"]  # enqueue_run does not perform lease recovery.
    for field in ("lease_owner_hash", "lease_until", "fencing_token", "next_run_at"):
        assert pending["runtime"][field] == before["runtime"][field]

    # A new store/service must replay the persisted result without changing it.
    reopened = AutomationStore(case.store.path)
    control = AutomationControlService(reopened, timer_controller=NoTimers())
    assert run_now(control, case.job) == {**accepted, "idempotent_replay": True}
    assert snapshot(reopened) == pending

    replacement = reopened.claim_next_run(owner=OWNER)

    assert replacement is not None and replacement["run_id"] == accepted["run_id"]
    assert replacement["trigger_kind"] == "manual"
    assert replacement["fencing_token"] > original["fencing_token"]
    assert is_current(reopened, replacement) is True
    recovered = snapshot(reopened)
    old = recovered["runs"][original["run_id"]]
    assert old["status"] == "failed" and old["result_code"] == "automation_lease_expired"
    assert recovered["runs"][accepted["run_id"]]["status"] == "running"
    assert is_current(reopened, original) is False
    assert (
        reopened.heartbeat_run(
            job_id=original["job_id"], run_id=original["run_id"], owner=OWNER, fencing_token=original["fencing_token"]
        )
        is False
    )
    assert (
        reopened.finish_run(
            job_id=original["job_id"],
            run_id=original["run_id"],
            owner=OWNER,
            fencing_token=original["fencing_token"],
            succeeded=True,
            result_code="ok",
        )
        is False
    )
    assert run_now(control, case.job) == {**accepted, "idempotent_replay": True}
    assert snapshot(reopened) == recovered


@pytest.mark.parametrize("pending_kind", ["queued", "running"])
def test_new_manual_intent_still_coalesces_with_runnable_pending_work(case, pending_kind):
    if pending_kind == "running":
        claim = case.store.claim_next_run(owner=OWNER, lease_seconds=10)
        assert claim is not None
        case.clock[0] += timedelta(seconds=9)
        assert is_current(case.store, claim) is True
    before = snapshot(case.store)

    accepted = run_now(case.service, case.job)

    assert accepted == {"queued": False, "coalesced": True, "run_id": case.first["run_id"]}
    after = snapshot(case.store)
    assert len(after["runs"]) == 1
    assert after["runs"][case.first["run_id"]]["coalesced_count"] == 1
    assert after["runtime"] == before["runtime"]
    assert after["outbox"] == before["outbox"]
    claimed = case.store.claim_next_run(owner=OWNER)
    if pending_kind == "queued":
        assert claimed is not None and claimed["run_id"] == case.first["run_id"]
    else:
        assert claimed is None  # The live worker retains its exclusive claim.


@pytest.mark.parametrize("field", ["lease_owner_hash", "fencing_token"])
def test_live_runtime_does_not_make_a_foreign_running_row_a_coalescing_target(case, field):
    original = case.store.claim_next_run(owner=OWNER, lease_seconds=60)
    assert original is not None
    # Synthetic persisted identity disagreement, with the runtime lease still live.
    value = (
        hashlib.sha256(b"synthetic-foreign-worker").hexdigest()
        if field == "lease_owner_hash"
        else original["fencing_token"] + 1
    )
    with case.store.connect() as connection:
        connection.execute(
            f"UPDATE manager_automation_runs SET {field} = ? WHERE run_id = ?",
            (value, original["run_id"]),
        )
    before = snapshot(case.store)
    assert automation_registry.parse_time(before["runtime"]["lease_until"]) > case.clock[0]
    assert is_current(case.store, original) is False

    accepted = run_now(case.service, case.job)

    assert accepted["queued"] is True and accepted["coalesced"] is False
    assert accepted["run_id"] != original["run_id"]
    after = snapshot(case.store)
    assert after["runs"][original["run_id"]] == before["runs"][original["run_id"]]
    assert after["runs"][accepted["run_id"]]["status"] == "queued"
    for lease_field in ("lease_owner_hash", "lease_until", "fencing_token"):
        assert after["runtime"][lease_field] == before["runtime"][lease_field]
    assert after["outbox"] == before["outbox"]
    assert case.store.claim_next_run(owner=OWNER) is None  # Never bypass the live runtime lease.
