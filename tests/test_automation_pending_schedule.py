"""Pending desired schedules must not reuse the previously applied due time."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from autostop_manager import automation_registry
from autostop_manager.automation_control import AUTOMATION_CONTROL_PROTOCOL, AutomationControlService
from autostop_manager.automation_registry import AutomationStore


START = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
OWNER = "synthetic-pending-schedule-worker"


class NoTimers:
    def __getattr__(self, name):
        raise AssertionError(f"Unexpected timer operation: {name}")


def command(service, operation, payload, key, revision=None):
    request = {
        "protocol": AUTOMATION_CONTROL_PROTOCOL,
        "request_id": str(uuid4()),
        "operation": operation,
        "actor": {"kind": "codex", "id": "synthetic-schedule-review", "is_admin": True},
        "payload": payload,
        "idempotency_key": key,
    }
    if revision is not None:
        request["expected_revision"] = revision
    return service.handle(request)


def enabled_job(case, key, *, template_id="crm_digest_v1"):
    created = command(
        case.control,
        "create_from_template",
        {"template_id": template_id, "schedule": {"every_minutes": 20, "timezone": "UTC"}},
        f"pending-create-{key}",
    )["job"]
    command(
        case.control,
        "set_enabled",
        {"job_id": created["job_id"], "enabled": True},
        f"pending-enable-{key}",
        created["revision"],
    )
    applied = case.store.reconcile_next_job()
    assert applied is not None and applied["applied"] is True
    assert applied["job"]["job_id"] == created["job_id"]
    return applied["job"]


@pytest.fixture
def case(tmp_path, monkeypatch):
    clock = SimpleNamespace(value=START)
    monkeypatch.setattr(automation_registry, "utc_now", lambda: clock.value)
    path = tmp_path / "manager.sqlite3"
    # The control command commits through a separate store/connection, just as
    # it can between the daemon's reconciliation and claim transactions.
    result = SimpleNamespace(
        store=AutomationStore(path),
        control=AutomationControlService(AutomationStore(path), timer_controller=NoTimers()),
        clock=clock,
    )
    result.job = enabled_job(result, "first")
    assert result.job["next_run_at"] == "2026-10-21T12:20:00Z"
    return result


def pending_schedule(case, schedule):
    assert case.store.reconcile_next_job() is None
    changed = command(
        case.control,
        "set_schedule",
        {"job_id": case.job["job_id"], "schedule": schedule},
        "pending-schedule-change-0001",
        case.job["revision"],
    )["job"]
    assert changed["revision"] == case.job["revision"] + 1
    assert changed["applied_revision"] == case.job["revision"]
    assert changed["reconcile_state"] == "pending"
    assert changed["next_run_at"] == case.job["next_run_at"]
    return changed


def execution_snapshot(store, job_id):
    # Independent SQL readback only; all fixture writes use supported commands
    # and reconciliation, rather than injecting due times or revisions.
    with closing(sqlite3.connect(store.path)) as connection:
        connection.row_factory = sqlite3.Row
        return {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} WHERE job_id = ?", (job_id,))]
            for table in (
                "manager_automation_runtime",
                "manager_automation_runs",
                "manager_automation_outbox",
            )
        }


def assert_current(store, claim):
    assert store.run_claim_is_current(
        job_id=claim["job_id"],
        run_id=claim["run_id"],
        owner=OWNER,
        fencing_token=claim["fencing_token"],
        claimed_revision=claim["claimed_revision"],
    )


def test_pending_interval_waits_for_reconciliation_then_claims_at_new_due_time(case):
    case.clock.value = START + timedelta(minutes=20)
    changed = pending_schedule(case, {"every_minutes": 60})
    before = execution_snapshot(case.store, case.job["job_id"])

    assert case.store.claim_next_run(owner=OWNER) is None
    assert execution_snapshot(case.store, case.job["job_id"]) == before

    applied = case.store.reconcile_next_job()
    assert applied is not None and applied["applied"] is True
    assert applied["job"]["reconcile_state"] == "in_sync"
    assert applied["job"]["applied_revision"] == changed["revision"]
    assert applied["job"]["next_run_at"] == "2026-10-21T13:20:00Z"
    case.clock.value = START + timedelta(minutes=80, seconds=-1)
    before_due = execution_snapshot(case.store, case.job["job_id"])
    assert case.store.claim_next_run(owner=OWNER) is None
    assert execution_snapshot(case.store, case.job["job_id"]) == before_due

    case.clock.value += timedelta(seconds=1)
    claim = case.store.claim_next_run(owner=OWNER)
    assert claim is not None
    assert claim["job_id"] == case.job["job_id"]
    assert claim["trigger_kind"] == "schedule"
    assert claim["claimed_revision"] == changed["revision"]
    assert claim["fencing_token"] == before["manager_automation_runtime"][0]["fencing_token"] + 1
    assert_current(case.store, claim)
    runs = execution_snapshot(case.store, case.job["job_id"])["manager_automation_runs"]
    assert len(runs) == 1
    assert runs[0]["status"] == "running"
    assert runs[0]["scheduled_for"] == runs[0]["started_at"] == "2026-10-21T13:20:00Z"


def test_pending_earlier_due_job_does_not_block_an_applied_due_job(case, monkeypatch):
    # The production catalog currently has one singleton. Add a local synthetic
    # template so this selection test can create both jobs through the API.
    template_id = "synthetic_schedule_v1"
    monkeypatch.setitem(
        automation_registry.AUTOMATION_TEMPLATES,
        template_id,
        replace(automation_registry.AUTOMATION_TEMPLATES["crm_digest_v1"], template_id=template_id, singleton=False),
    )
    case.clock.value = START + timedelta(minutes=1)
    other = enabled_job(case, "other", template_id=template_id)
    assert other["next_run_at"] == "2026-10-21T12:21:00Z"
    case.clock.value = START + timedelta(minutes=21)
    pending_schedule(case, {"every_minutes": 60})
    before = execution_snapshot(case.store, case.job["job_id"])

    claim = case.store.claim_next_run(owner=OWNER)

    assert claim is not None and claim["job_id"] == other["job_id"]
    assert claim["trigger_kind"] == "schedule"
    assert claim["claimed_revision"] == other["applied_revision"]
    assert_current(case.store, claim)
    assert execution_snapshot(case.store, case.job["job_id"]) == before
    assert case.store.claim_next_run(owner=OWNER) is None
    assert execution_snapshot(case.store, case.job["job_id"]) == before


def test_pending_active_window_does_not_defer_runtime_before_reconciliation(case):
    case.clock.value = START + timedelta(minutes=20)
    changed = pending_schedule(case, {"active_window": {"start": "14:00", "end": "16:00"}})
    before = execution_snapshot(case.store, case.job["job_id"])

    assert case.store.claim_next_run(owner=OWNER) is None
    assert execution_snapshot(case.store, case.job["job_id"]) == before
    assert case.store.status(job_id=case.job["job_id"])["jobs"][0]["next_run_at"] == "2026-10-21T12:20:00Z"

    applied = case.store.reconcile_next_job()
    assert applied is not None and applied["applied"] is True
    assert applied["job"]["applied_revision"] == changed["revision"]
    assert applied["job"]["next_run_at"] == "2026-10-21T14:00:00Z"


def test_manual_run_now_remains_claimable_while_schedule_revision_is_pending(case):
    case.clock.value = START + timedelta(minutes=20)
    changed = pending_schedule(case, {"every_minutes": 60})
    queued = command(
        case.control,
        "run_now",
        {"job_id": case.job["job_id"]},
        "pending-manual-run-0001",
        changed["revision"],
    )
    assert queued["queued"] is True
    assert queued["coalesced"] is False

    claim = case.store.claim_next_run(owner=OWNER)

    assert claim is not None and claim["run_id"] == queued["run_id"]
    assert claim["job_id"] == case.job["job_id"]
    assert claim["trigger_kind"] == "manual"
    assert claim["claimed_revision"] == changed["revision"]
    assert_current(case.store, claim)
    observed = case.store.status(job_id=case.job["job_id"])["jobs"][0]
    assert observed["reconcile_state"] == "pending"
    assert observed["applied_revision"] == changed["applied_revision"]
    assert observed["next_run_at"] == changed["next_run_at"]
