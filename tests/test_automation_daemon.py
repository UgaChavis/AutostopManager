from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path
from uuid import uuid4

from autostop_manager.automation_control import AUTOMATION_CONTROL_PROTOCOL, AutomationControlService
from autostop_manager.automation_daemon import (
    AutomationDaemon,
    TEST_NOTIFICATION_TEXT,
    notification_readiness,
)
from autostop_manager.automation_jobs import AutomationJobError
from autostop_manager.automation_registry import AutomationStore


def command(service, operation, payload, idempotency_key, expected_revision=None):
    request = {
        "protocol": AUTOMATION_CONTROL_PROTOCOL,
        "request_id": str(uuid4()),
        "operation": operation,
        "actor": {"kind": "codex", "id": "test-owner", "is_admin": True},
        "payload": payload,
        "idempotency_key": idempotency_key,
    }
    if expected_revision is not None:
        request["expected_revision"] = expected_revision
    return service.handle(request)


def create_digest(service):
    created = command(
        service,
        "create_from_template",
        {"template_id": "crm_digest_v1"},
        "create-digest-0001",
    )["job"]
    enabled = command(
        service,
        "set_enabled",
        {"job_id": created["job_id"], "enabled": True},
        "enable-digest-0001",
        created["revision"],
    )["job"]
    reconciled = service.store.reconcile_next_job()
    assert reconciled and reconciled["applied"] is True
    return {**enabled, **reconciled["job"]}


def test_daemon_executes_claim_under_fence_and_records_result(tmp_path: Path):
    path = tmp_path / "manager.sqlite3"
    store = AutomationStore(path)
    service = AutomationControlService(store)
    job = create_digest(service)
    queued = command(
        service,
        "run_now",
        {"job_id": job["job_id"]},
        "run-digest-0001",
        job["revision"],
    )
    observed = []

    def executor(claim):
        observed.append(dict(claim))
        return {"ok": True, "result_code": "ok"}

    daemon = AutomationDaemon(store=store, executors={"crm_digest_v1": executor})
    claim = store.claim_next_run(owner=daemon.owner, lease_seconds=60)
    assert claim

    asyncio.run(daemon._execute_claim(claim))

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT status, result_code, fencing_token FROM manager_automation_runs WHERE run_id = ?",
            (queued["run_id"],),
        ).fetchone()
    assert row == ("succeeded", "ok", claim["fencing_token"])
    assert observed[0]["run_id"] == queued["run_id"]


def test_daemon_without_executor_fails_closed_without_external_effect(tmp_path: Path):
    path = tmp_path / "manager.sqlite3"
    store = AutomationStore(path)
    service = AutomationControlService(store)
    job = create_digest(service)
    queued = command(
        service,
        "run_now",
        {"job_id": job["job_id"]},
        "run-digest-0001",
        job["revision"],
    )
    daemon = AutomationDaemon(store=store)
    claim = store.claim_next_run(owner=daemon.owner, lease_seconds=60)
    assert claim

    asyncio.run(daemon._execute_claim(claim))

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT status, result_code FROM manager_automation_runs WHERE run_id = ?",
            (queued["run_id"],),
        ).fetchone()
    assert row == ("failed", "automation_executor_unavailable")


class FakeNotifier:
    def __init__(self, *, uncertain: bool = False, lookup_result=None):
        self.uncertain = uncertain
        self.lookup_result = lookup_result
        self.calls = []

    def preview(self, text):
        self.calls.append(("preview", text))
        return {
            "mode": "dry_run",
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "contract_token": "test-token",
        }

    def apply(self, text, *, contract_token, idempotency_key):
        self.calls.append(("apply", text, contract_token, idempotency_key))
        if self.uncertain:
            raise AutomationJobError("automation_telegram_outcome_unknown", outcome_uncertain=True)
        return {"verified": True, "message_id": 71}

    def readback(self, *, message_id, text_sha256):
        raise AssertionError("test notification relies on the bridge's verified apply")

    def lookup(self, *, idempotency_key, text_sha256):
        self.calls.append(("lookup", idempotency_key, text_sha256))
        return self.lookup_result or {
            "ok": True,
            "outcome": "not_found",
            "found": False,
            "verified": False,
        }


def test_notification_readiness_requires_bridge_transport_and_owner_target():
    class StatusNotifier(FakeNotifier):
        def __init__(self, status):
            super().__init__()
            self._status = status

        def status(self):
            return self._status

    assert notification_readiness(StatusNotifier({"transport_ready": True, "owner_notification_configured": True})) == {
        "notification_transport": "ready",
        "owner_notification_target": "ready",
    }
    assert notification_readiness(
        StatusNotifier({"transport_ready": False, "owner_notification_configured": True})
    ) == {"notification_transport": "unavailable", "owner_notification_target": "ready"}
    assert notification_readiness(
        StatusNotifier({"transport_ready": True, "owner_notification_configured": False})
    ) == {"notification_transport": "ready", "owner_notification_target": "not_configured"}


def test_notification_readiness_fails_closed_when_bridge_status_is_unavailable():
    class UnavailableNotifier(FakeNotifier):
        def status(self):
            raise AutomationJobError("automation_telegram_unavailable")

    assert notification_readiness(UnavailableNotifier()) == {
        "notification_transport": "unavailable",
        "owner_notification_target": "not_configured",
    }


def test_daemon_delivers_test_notification_once_through_verified_owner_target(tmp_path: Path):
    path = tmp_path / "manager.sqlite3"
    store = AutomationStore(path)
    service = AutomationControlService(store)
    job = create_digest(service)
    queued = command(
        service,
        "test_notification",
        {"job_id": job["job_id"]},
        "notify-test-0001",
        job["revision"],
    )
    notifier = FakeNotifier()
    daemon = AutomationDaemon(store=store, notifier=notifier)
    claim = store.claim_outbox(owner=daemon.owner)
    assert claim

    asyncio.run(daemon._execute_outbox(claim))

    with sqlite3.connect(path) as connection:
        state = connection.execute(
            "SELECT status, result_code FROM manager_automation_outbox WHERE outbox_id = ?",
            (queued["outbox_id"],),
        ).fetchone()
    assert state == ("sent", "ok")
    assert notifier.calls == [
        ("preview", TEST_NOTIFICATION_TEXT),
        ("apply", TEST_NOTIFICATION_TEXT, "test-token", "notification-test:notify-test-0001"),
    ]


def test_uncertain_test_notification_is_held_without_blind_retry(tmp_path: Path):
    path = tmp_path / "manager.sqlite3"
    store = AutomationStore(path)
    service = AutomationControlService(store)
    job = create_digest(service)
    queued = command(
        service,
        "test_notification",
        {"job_id": job["job_id"]},
        "notify-test-0001",
        job["revision"],
    )
    notifier = FakeNotifier(uncertain=True)
    daemon = AutomationDaemon(store=store, notifier=notifier)
    claim = store.claim_outbox(owner=daemon.owner)
    assert claim

    asyncio.run(daemon._execute_outbox(claim))

    with sqlite3.connect(path) as connection:
        state = connection.execute(
            "SELECT status, result_code FROM manager_automation_outbox WHERE outbox_id = ?",
            (queued["outbox_id"],),
        ).fetchone()
    assert state == ("sending", None)
    assert store.claim_outbox(owner="other-worker") is None


def test_outbox_revision_change_is_cancelled_before_telegram_preview(tmp_path: Path):
    path = tmp_path / "manager.sqlite3"
    store = AutomationStore(path)
    service = AutomationControlService(store)
    job = create_digest(service)
    queued = command(
        service,
        "test_notification",
        {"job_id": job["job_id"]},
        "notify-revision-0001",
        job["revision"],
    )
    notifier = FakeNotifier()
    daemon = AutomationDaemon(store=store, notifier=notifier)
    claim = store.claim_outbox(owner=daemon.owner)
    assert claim
    command(
        service,
        "set_schedule",
        {"job_id": job["job_id"], "schedule": {"every_minutes": 30}},
        "schedule-after-outbox-claim-0001",
        job["revision"],
    )

    asyncio.run(daemon._execute_outbox(claim))

    with sqlite3.connect(path) as connection:
        state = connection.execute(
            "SELECT status, result_code FROM manager_automation_outbox WHERE outbox_id = ?",
            (queued["outbox_id"],),
        ).fetchone()
    assert state == ("cancelled", "automation_claim_superseded")
    assert notifier.calls == []


def test_expired_uncertain_outbox_uses_lookup_without_resend(tmp_path: Path):
    path = tmp_path / "manager.sqlite3"
    store = AutomationStore(path)
    service = AutomationControlService(store)
    job = create_digest(service)
    queued = command(
        service,
        "test_notification",
        {"job_id": job["job_id"]},
        "notify-unknown-lookup-0001",
        job["revision"],
    )
    first_daemon = AutomationDaemon(store=store, notifier=FakeNotifier(uncertain=True))
    first_claim = store.claim_outbox(owner=first_daemon.owner)
    assert first_claim
    asyncio.run(first_daemon._execute_outbox(first_claim))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE manager_automation_outbox SET lease_until = '2000-01-01T00:00:00Z' WHERE outbox_id = ?",
            (queued["outbox_id"],),
        )
    expected_hash = hashlib.sha256(TEST_NOTIFICATION_TEXT.encode()).hexdigest()
    verifier = FakeNotifier(
        lookup_result={
            "ok": True,
            "outcome": "verified",
            "found": True,
            "verified": True,
            "message_id": 91,
            "text_sha256": expected_hash,
        }
    )
    second_daemon = AutomationDaemon(store=store, notifier=verifier)
    recovery_claim = store.claim_outbox(owner=second_daemon.owner)
    assert recovery_claim and recovery_claim["recovery_only"] is True

    asyncio.run(second_daemon._execute_outbox(recovery_claim))

    with sqlite3.connect(path) as connection:
        state = connection.execute(
            "SELECT status, result_code FROM manager_automation_outbox WHERE outbox_id = ?",
            (queued["outbox_id"],),
        ).fetchone()
    assert state == ("sent", "ok")
    assert verifier.calls == [("lookup", "notification-test:notify-unknown-lookup-0001", expected_hash)]


def test_expired_uncertain_outbox_not_found_is_visibly_blocked(tmp_path: Path):
    path = tmp_path / "manager.sqlite3"
    store = AutomationStore(path)
    service = AutomationControlService(store)
    job = create_digest(service)
    queued = command(
        service,
        "test_notification",
        {"job_id": job["job_id"]},
        "notify-unknown-block-0001",
        job["revision"],
    )
    first_daemon = AutomationDaemon(store=store, notifier=FakeNotifier(uncertain=True))
    first_claim = store.claim_outbox(owner=first_daemon.owner)
    assert first_claim
    asyncio.run(first_daemon._execute_outbox(first_claim))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE manager_automation_outbox SET lease_until = '2000-01-01T00:00:00Z' WHERE outbox_id = ?",
            (queued["outbox_id"],),
        )
    second_daemon = AutomationDaemon(store=store, notifier=FakeNotifier())
    recovery_claim = store.claim_outbox(owner=second_daemon.owner)
    assert recovery_claim and recovery_claim["recovery_only"] is True

    asyncio.run(second_daemon._execute_outbox(recovery_claim))

    with sqlite3.connect(path) as connection:
        state = connection.execute(
            "SELECT status, result_code, lease_until FROM manager_automation_outbox WHERE outbox_id = ?",
            (queued["outbox_id"],),
        ).fetchone()
    assert state == ("sending", "automation_delivery_manual_reconciliation_required", None)
    assert store.claim_outbox(owner="must-not-resend") is None
