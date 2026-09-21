from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from autostop_manager import automation_daemon
from autostop_manager.automation_control import AUTOMATION_CONTROL_PROTOCOL, AutomationControlService
from autostop_manager.automation_daemon import (
    AutomationDaemon,
    TEST_NOTIFICATION_TEXT,
    crm_change_feed_readiness,
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


def test_crm_change_feed_readiness_is_non_mutating_and_fail_closed():
    class Source:
        def __init__(self, result=None, error=None):
            self.result = result
            self.error = error
            self.calls = 0

        def readiness(self):
            self.calls += 1
            if self.error is not None:
                raise self.error
            return self.result

    ready = Source({"pending_publish": False})
    pending = Source({"pending_publish": True})
    unavailable = Source(error=AutomationJobError("automation_crm_unavailable"))

    assert crm_change_feed_readiness(None) == "not_configured"
    assert crm_change_feed_readiness(ready) == "ready"
    assert crm_change_feed_readiness(pending) == "pending"
    assert crm_change_feed_readiness(unavailable) == "unavailable"
    assert ready.calls == pending.calls == unavailable.calls == 1


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


def test_systemd_notification_supports_filesystem_and_abstract_sockets(monkeypatch):
    observed = []

    class Datagram:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def connect(self, target):
            observed.append(("connect", target))

        def sendall(self, payload):
            observed.append(("send", payload))

    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    monkeypatch.setattr(
        automation_daemon.socket,
        "socket",
        lambda *_args: pytest.fail("socket must not be opened without NOTIFY_SOCKET"),
    )
    automation_daemon.notify_systemd("READY=1")

    monkeypatch.setattr(automation_daemon.socket, "socket", lambda *_args: Datagram())
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/test-notify.sock")
    automation_daemon.notify_systemd("READY=1")
    monkeypatch.setenv("NOTIFY_SOCKET", "@test-notify")
    automation_daemon.notify_systemd("STOPPING=1")

    assert observed == [
        ("connect", "/run/test-notify.sock"),
        ("send", b"READY=1"),
        ("connect", "\0test-notify"),
        ("send", b"STOPPING=1"),
    ]


def test_production_wires_digest_and_reports_live_dependency_readiness(monkeypatch, tmp_path):
    config = SimpleNamespace(configured=True, error_code=None)
    sources = []

    class Source:
        def __init__(self, received_config, *, timeout_seconds=10):
            assert received_config is config
            self.timeout_seconds = timeout_seconds
            sources.append(self)

        def readiness(self):
            return {"pending_publish": False}

    class Notifier:
        def __init__(self, path):
            self.path = path

        def status(self):
            return {"transport_ready": True, "owner_notification_configured": True}

    monkeypatch.setattr(automation_daemon, "get_automation_crm_connection_config", lambda: config)
    monkeypatch.setattr(automation_daemon, "get_work_telegram_socket_path", lambda: tmp_path / "telegram.sock")
    monkeypatch.setattr(automation_daemon, "HttpCrmDigestSource", Source)
    monkeypatch.setattr(automation_daemon, "TelegramOwnerNotifier", Notifier)
    monkeypatch.setattr(automation_daemon, "CrmDigestExecutor", lambda **_kwargs: lambda _claim: {"ok": True})

    daemon = AutomationDaemon.production(store=AutomationStore(tmp_path / "manager.sqlite3"))

    assert set(daemon.executors) == {"crm_digest_v1"}
    assert [source.timeout_seconds for source in sources] == [10, 2]
    assert daemon.readiness_provider() == {
        "ready": True,
        "checks": {
            "crm_digest_executor": "ready",
            "crm_change_feed": "ready",
            "notification_transport": "ready",
            "owner_notification_target": "ready",
        },
    }


def test_production_reports_invalid_crm_configuration_without_building_executor(monkeypatch, tmp_path):
    config = SimpleNamespace(configured=False, error_code="automation_crm_config_invalid")

    class Notifier:
        def __init__(self, _path):
            pass

        def status(self):
            return {"transport_ready": True, "owner_notification_configured": True}

    monkeypatch.setattr(automation_daemon, "get_automation_crm_connection_config", lambda: config)
    monkeypatch.setattr(automation_daemon, "get_work_telegram_socket_path", lambda: tmp_path / "telegram.sock")
    monkeypatch.setattr(automation_daemon, "TelegramOwnerNotifier", Notifier)
    monkeypatch.setattr(
        automation_daemon,
        "HttpCrmDigestSource",
        lambda *_args, **_kwargs: pytest.fail("CRM source must not be built for invalid configuration"),
    )

    daemon = AutomationDaemon.production(store=AutomationStore(tmp_path / "manager.sqlite3"))

    assert daemon.executors == {}
    assert daemon.readiness_provider()["checks"]["crm_change_feed"] == "automation_crm_config_invalid"
    assert daemon.readiness_provider()["ready"] is False


class RecordingRunStore:
    def __init__(self):
        self.finished = []
        self.cancelled = []

    def finish_run(self, **values):
        self.finished.append(values)

    def cancel_run_claim(self, **values):
        self.cancelled.append(values)


def _claim():
    return {
        "template_id": "crm_digest_v1",
        "job_id": "auto_" + "1" * 24,
        "run_id": "run_" + "2" * 32,
        "fencing_token": 3,
    }


def _unit_daemon(store, *, executors=None, notifier=None):
    return AutomationDaemon(
        store=store,
        executors=executors,
        notifier=notifier,
        timer_controller=object(),
        control_server=object(),
    )


def test_claim_supports_async_executor_and_cancels_superseded_result():
    store = RecordingRunStore()

    async def executor(invocation):
        assert invocation["lease_owner"].startswith("automation-daemon-")
        return {"ok": False, "result_code": "automation_claim_superseded"}

    daemon = _unit_daemon(store, executors={"crm_digest_v1": executor})
    asyncio.run(daemon._execute_claim(_claim()))

    assert store.finished == []
    assert store.cancelled[0]["fencing_token"] == 3


def test_claim_awaits_deferred_sync_result_and_preserves_retry_delay():
    store = RecordingRunStore()

    async def result():
        return {"ok": False, "result_code": "automation_retry", "next_delay_seconds": 17}

    def executor(_invocation):
        return result()

    daemon = _unit_daemon(store, executors={"crm_digest_v1": executor})
    asyncio.run(daemon._execute_claim(_claim()))

    assert store.finished[0]["result_code"] == "automation_retry"
    assert store.finished[0]["next_delay_seconds"] == 17


def test_claim_exception_is_reduced_to_safe_failure_code():
    store = RecordingRunStore()

    def executor(_invocation):
        raise RuntimeError("private executor detail")

    daemon = _unit_daemon(store, executors={"crm_digest_v1": executor})
    asyncio.run(daemon._execute_claim(_claim()))

    assert store.finished[0]["succeeded"] is False
    assert store.finished[0]["result_code"] == "automation_executor_failed"


class RecordingOutboxStore:
    def __init__(self, current_results=()):
        self.current_results = iter(current_results)
        self.finished = []
        self.cancelled = []
        self.blocked = []

    def finish_outbox(self, **values):
        self.finished.append(values)

    def cancel_outbox_claim(self, **values):
        self.cancelled.append(values)

    def block_outbox_claim(self, **values):
        self.blocked.append(values)

    def outbox_claim_is_current(self, **_values):
        return next(self.current_results)


def _outbox_claim(*, kind="notification_test", payload=None, recovery_only=False):
    if payload is None:
        payload = {"template": "automation_notification_test_v1", "command_id": "cmd_" + "1" * 32}
    return {
        "outbox_id": "out_" + "2" * 32,
        "kind": kind,
        "payload": payload,
        "fencing_token": 4,
        "claimed_revision": 1,
        "idempotency_key": "outbox-test-key",
        "recovery_only": recovery_only,
    }


def test_outbox_rejects_invalid_payload_and_missing_notifier():
    invalid_store = RecordingOutboxStore()
    asyncio.run(_unit_daemon(invalid_store)._execute_outbox(_outbox_claim(payload={"template": "bad"})))
    assert invalid_store.finished[0]["result_code"] == "automation_outbox_payload_invalid"

    unavailable_store = RecordingOutboxStore()
    asyncio.run(_unit_daemon(unavailable_store)._execute_outbox(_outbox_claim()))
    assert unavailable_store.finished[0]["result_code"] == "automation_telegram_unavailable"


@pytest.mark.parametrize(
    ("kind", "transition", "expected_text"),
    [
        ("job_error_alert", "error", "перешёл в ERROR"),
        ("job_recovery_alert", "recovery", "снова работает"),
    ],
)
def test_incident_outbox_renders_allowlisted_messages(kind, transition, expected_text):
    store = RecordingOutboxStore([True, True])
    notifier = FakeNotifier()
    payload = {
        "template": "automation_job_incident_v1",
        "template_id": "crm_digest_v1",
        "transition": transition,
        "incident_id": "inc_" + "a" * 32,
        "error_code": "automation_executor_failed",
    }

    asyncio.run(_unit_daemon(store, notifier=notifier)._execute_outbox(_outbox_claim(kind=kind, payload=payload)))

    assert store.finished[0]["result_code"] == "ok"
    assert expected_text in notifier.calls[0][1]


def test_outbox_recovery_lookup_error_blocks_without_resend():
    store = RecordingOutboxStore()

    class Notifier(FakeNotifier):
        def lookup(self, **_kwargs):
            raise AutomationJobError("automation_telegram_unavailable")

    notifier = Notifier()
    asyncio.run(_unit_daemon(store, notifier=notifier)._execute_outbox(_outbox_claim(recovery_only=True)))

    assert store.blocked and store.finished == []
    assert notifier.calls == []


def test_outbox_invalid_preview_and_deterministic_apply_error_are_recorded():
    preview_store = RecordingOutboxStore([True])

    class InvalidPreview(FakeNotifier):
        def preview(self, _text):
            return {"mode": "dry_run", "text_sha256": "invalid", "contract_token": "proof"}

    asyncio.run(_unit_daemon(preview_store, notifier=InvalidPreview())._execute_outbox(_outbox_claim()))
    assert preview_store.finished[0]["result_code"] == "automation_notification_preview_invalid"

    apply_store = RecordingOutboxStore([True, True])

    class RejectedApply(FakeNotifier):
        def apply(self, *_args, **_kwargs):
            raise AutomationJobError("automation_telegram_rejected")

    asyncio.run(_unit_daemon(apply_store, notifier=RejectedApply())._execute_outbox(_outbox_claim()))
    assert apply_store.finished[0]["result_code"] == "automation_telegram_rejected"


def test_outbox_rechecks_fence_before_apply_and_holds_unknown_result():
    stale_store = RecordingOutboxStore([True, False])
    stale_notifier = FakeNotifier()
    asyncio.run(_unit_daemon(stale_store, notifier=stale_notifier)._execute_outbox(_outbox_claim()))
    assert stale_store.cancelled and all(call[0] != "apply" for call in stale_notifier.calls)

    uncertain_store = RecordingOutboxStore([True, True])

    class UnknownApply(FakeNotifier):
        def apply(self, *_args, **_kwargs):
            return {"verified": False, "message_id": 0}

    asyncio.run(_unit_daemon(uncertain_store, notifier=UnknownApply())._execute_outbox(_outbox_claim()))
    assert uncertain_store.finished == []
    assert uncertain_store.cancelled == []


@pytest.mark.parametrize("mode", ["reconcile", "outbox", "run", "error"])
def test_scheduler_loop_prioritizes_each_work_class_and_recovers_errors(monkeypatch, mode):
    events = []

    class Store:
        def controller_heartbeat(self, **values):
            events.append(("heartbeat", values))
            if mode == "error" and "state" not in values:
                raise RuntimeError("transient")
            if mode == "error":
                daemon.stopping.set()

        def reconcile_next_job(self):
            if mode == "reconcile":
                daemon.stopping.set()
                return {"applied": True}
            return None

        def claim_outbox(self, **_values):
            if mode == "outbox":
                daemon.stopping.set()
                return {"outbox_id": "out"}
            return None

        def claim_next_run(self, **_values):
            if mode == "run":
                daemon.stopping.set()
                return {"run_id": "run"}
            daemon.stopping.set()
            return None

    store = Store()
    daemon = _unit_daemon(store)

    async def execute_outbox(claim):
        events.append(("outbox", claim))

    async def execute_claim(claim):
        events.append(("run", claim))

    monkeypatch.setattr(daemon, "_execute_outbox", execute_outbox)
    monkeypatch.setattr(daemon, "_execute_claim", execute_claim)
    asyncio.run(daemon.scheduler_loop())

    if mode == "outbox":
        assert ("outbox", {"outbox_id": "out"}) in events
    elif mode == "run":
        assert ("run", {"run_id": "run"}) in events
    elif mode == "error":
        assert any(name == "heartbeat" and values.get("state") == "error" for name, values in events)


def test_daemon_run_initializes_adopts_serves_and_stops_cleanly(monkeypatch):
    events = []

    class Store:
        def initialize(self):
            events.append("initialize")

        def seed_defaults(self):
            events.append("seed")

        def controller_heartbeat(self, **values):
            events.append(("heartbeat", values))

    class Timers:
        def adopt_all_current(self):
            events.append("adopt")

    class Server:
        async def start(self):
            events.append("start")

        async def close(self):
            events.append("close")

    daemon = AutomationDaemon(store=Store(), timer_controller=Timers(), control_server=Server())

    async def scheduler_loop():
        events.append("scheduler")
        daemon.stopping.set()

    monkeypatch.setattr(daemon, "scheduler_loop", scheduler_loop)
    monkeypatch.setattr(automation_daemon, "notify_systemd", lambda message: events.append(message))

    asyncio.run(daemon.run())

    assert events[:3] == ["initialize", "seed", "adopt"]
    assert events[3][0] == "heartbeat" and events[3][1]["started"] is True
    assert events[4] == "start"
    assert "READY=1" in events
    assert "STOPPING=1" in events
    assert events[-1] == "close"


def test_daemon_main_loads_runtime_and_runs_production_daemon(monkeypatch):
    events = []

    class Daemon:
        async def run(self):
            events.append("run")

    monkeypatch.setattr(automation_daemon, "load_runtime_env", lambda: events.append("env"))
    monkeypatch.setattr(automation_daemon.AutomationDaemon, "production", lambda: Daemon())

    assert automation_daemon.main([]) == 0
    assert events == ["env", "run"]
