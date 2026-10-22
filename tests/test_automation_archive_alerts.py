"""Archive resolves unsent incident alerts without discarding protected deliveries."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from autostop_manager import automation_registry as registry
from autostop_manager.automation_control import AUTOMATION_CONTROL_PROTOCOL, AutomationControlService


class NoTimers:
    def __getattr__(self, name):
        raise AssertionError(f"Unexpected timer operation: {name}")


def command(service, operation, payload, revision=None):
    request = {
        "protocol": AUTOMATION_CONTROL_PROTOCOL,
        "request_id": str(uuid4()),
        "operation": operation,
        "actor": {"kind": "codex", "id": "synthetic-archive-review", "is_admin": True},
        "payload": payload,
        "idempotency_key": f"archive-test-{uuid4().hex}",
    }
    if revision is not None:
        request["expected_revision"] = revision
    return service.handle(request)


@pytest.fixture
def case(monkeypatch, tmp_path):
    clock = SimpleNamespace(value=datetime(2026, 10, 21, 12, 0, tzinfo=UTC))
    monkeypatch.setattr(registry, "utc_now", lambda: clock.value)
    store = registry.AutomationStore(tmp_path / "registry.sqlite3")
    control = AutomationControlService(store, timer_controller=NoTimers())
    created = command(control, "create_from_template", {"template_id": "crm_digest_v1"})["job"]
    command(control, "set_enabled", {"job_id": created["job_id"], "enabled": True}, created["revision"])
    reconciled = store.reconcile_next_job()
    assert reconciled["applied"] is True
    return SimpleNamespace(store=store, control=control, job=reconciled["job"], clock=clock)


def finish_job(case, *, succeeded):
    queued = command(case.control, "run_now", {"job_id": case.job["job_id"]}, case.job["revision"])
    claim = case.store.claim_next_run(owner="synthetic-archive-runner")
    assert claim["run_id"] == queued["run_id"]
    assert case.store.finish_run(
        job_id=case.job["job_id"],
        run_id=claim["run_id"],
        owner="synthetic-archive-runner",
        fencing_token=claim["fencing_token"],
        succeeded=succeeded,
        result_code="ok" if succeeded else "synthetic_run_failure",
    )


def disable_and_reopen(case):
    command(case.control, "set_enabled", {"job_id": case.job["job_id"], "enabled": False}, case.job["revision"])
    reconciled = case.store.reconcile_next_job()
    assert reconciled["applied"] is True
    case.job = reconciled["job"]
    case.store = registry.AutomationStore(case.store.path)
    case.control = AutomationControlService(case.store, timer_controller=NoTimers())
    assert case.store.reconcile_next_job() is None
    assert case.job["desired_state"] == "off"
    assert case.job["reconcile_state"] == "in_sync"


def read_state(case):
    # Independent SQLite reads; fixture writes use control, claim and completion APIs.
    with closing(sqlite3.connect(case.store.path)) as connection:
        connection.row_factory = sqlite3.Row
        return {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}")]
            for table, order in (
                ("manager_automations", "job_id"),
                ("manager_automation_runtime", "job_id"),
                ("manager_automation_commands", "command_id"),
                ("manager_automation_outbox", "outbox_id"),
            )
        }


@pytest.mark.parametrize("recovered", [False, True], ids=["error-alert", "error-and-recovery-alerts"])
@pytest.mark.parametrize("delivery_state", ["queued", "failed"])
def test_archive_resolves_existing_off_incident_alerts(case, recovered, delivery_state):
    finish_job(case, succeeded=False)
    if recovered:
        finish_job(case, succeeded=True)
    if delivery_state == "failed":
        while (claim := case.store.claim_outbox(owner="synthetic-preview-worker")) is not None:
            assert claim["recovery_only"] is False
            assert case.store.finish_outbox(
                outbox_id=claim["outbox_id"],
                owner="synthetic-preview-worker",
                fencing_token=claim["fencing_token"],
                sent=False,
                result_code="synthetic_preview_unavailable",
            )
    disable_and_reopen(case)
    case.clock.value += timedelta(seconds=61)
    assert case.store.claim_outbox(owner="must-not-send-disabled-alerts") is None
    before = read_state(case)["manager_automation_outbox"]
    assert {row["kind"] for row in before} == (
        {"job_error_alert", "job_recovery_alert"} if recovered else {"job_error_alert"}
    )
    assert all(row["status"] == delivery_state for row in before)

    result = command(case.control, "archive", {"job_id": case.job["job_id"]}, case.job["revision"])

    assert result["archived"] is True
    after = read_state(case)["manager_automation_outbox"]
    assert len(after) == len(before)
    for previous, current in zip(before, after, strict=True):
        assert current["status"] == "cancelled"
        assert current["result_code"] == "automation_archived"
        assert current["lease_until"] is None
        for field in (
            "outbox_id",
            "job_id",
            "run_id",
            "kind",
            "idempotency_key",
            "payload_json",
            "attempts",
            "fencing_token",
        ):
            assert current[field] == previous[field]
    archived = case.store.status(job_id=case.job["job_id"])["jobs"][0]
    assert archived["actual_state"] == "archived"
    assert archived["revision"] == case.job["revision"] + 1
    assert case.store.claim_outbox(owner="must-not-send-archived-alerts") is None


@pytest.mark.parametrize("protected", ["notification_test", "unknown_sending"])
def test_rejected_archive_rolls_back_safe_alert_changes(case, protected):
    finish_job(case, succeeded=False)
    if protected == "notification_test":
        command(case.control, "test_notification", {"job_id": case.job["job_id"]}, case.job["revision"])
    else:
        sending = case.store.claim_outbox(owner="synthetic-unknown-sender")
        assert sending and sending["recovery_only"] is False
        finish_job(case, succeeded=True)
        case.clock.value += timedelta(seconds=61)
        recovered = case.store.claim_outbox(owner="synthetic-recovery-worker")
        assert recovered["outbox_id"] == sending["outbox_id"]
        assert recovered["recovery_only"] is True
        assert case.store.block_outbox_claim(
            outbox_id=recovered["outbox_id"],
            owner="synthetic-recovery-worker",
            fencing_token=recovered["fencing_token"],
        )
    disable_and_reopen(case)
    before = read_state(case)
    deliveries = before["manager_automation_outbox"]
    assert len(deliveries) == 2
    assert any(
        row["kind"] in {"job_error_alert", "job_recovery_alert"} and row["status"] == "queued" for row in deliveries
    )
    if protected == "unknown_sending":
        assert any(row["status"] == "sending" and row["lease_until"] is None for row in deliveries)

    with pytest.raises(registry.AutomationError, match="automation_delivery_pending"):
        command(case.control, "archive", {"job_id": case.job["job_id"]}, case.job["revision"])

    assert read_state(case) == before
