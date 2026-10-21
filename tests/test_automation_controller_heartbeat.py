"""Controller liveness during real temporary claims and gated synthetic work."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from autostop_manager import automation_daemon, automation_registry
from autostop_manager.automation_control import AUTOMATION_CONTROL_PROTOCOL, AutomationControlService
from autostop_manager.automation_daemon import AutomationDaemon
from autostop_manager.automation_registry import AutomationStore, isoformat


START = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
PERIOD = 0.05
WATCHDOG = 3


class Clock:
    value = START

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class NoTimers:
    def adopt_all_current(self):
        return []


class NoServer:
    closed = False

    async def start(self):
        pass

    async def close(self):
        self.closed = True


def command(service, operation, payload, key, revision=None):
    request = {
        "protocol": AUTOMATION_CONTROL_PROTOCOL,
        "request_id": str(uuid4()),
        "operation": operation,
        "actor": {"kind": "codex", "id": "synthetic-heartbeat-review", "is_admin": True},
        "payload": payload,
        "idempotency_key": key,
    }
    if revision is not None:
        request["expected_revision"] = revision
    return service.handle(request)


def observe_heartbeats(monkeypatch, store, clock, *, fail_once=False):
    observed = SimpleNamespace(pulse=asyncio.Event(), error=asyncio.Event(), failures=[], writes=[])
    real_heartbeat = AutomationStore.controller_heartbeat

    def heartbeat(target, **values):
        state = values.get("state", "active")
        if target is store and fail_once and clock.value > START and state == "active" and not observed.failures:
            observed.failures.append(clock.value)
            raise OSError("synthetic transient controller-heartbeat write failure")
        real_heartbeat(target, **values)
        if target is store:
            observed.writes.append((clock.value, state))
            if clock.value > START:
                observed.pulse.set()
            if state == "error":
                observed.error.set()

    monkeypatch.setattr(AutomationStore, "controller_heartbeat", heartbeat)
    return observed


async def observe_next_pulse(observed):
    # Baseline has no periodic writer during pending work. After a bounded
    # observation window, read durable status and fail on its actual state.
    with suppress(TimeoutError):
        await asyncio.wait_for(observed.pulse.wait(), WATCHDOG)


class Work:
    def __init__(self, store):
        self.store = store
        self.loop = asyncio.get_running_loop()
        self.started = asyncio.Event()
        self.progressed = asyncio.Event()
        self.pulse_async = asyncio.Event()
        self.release_async = asyncio.Event()
        self.pulse_thread = threading.Event()
        self.release_thread = threading.Event()
        self.claim = None

    def renew_run(self):
        if self.claim is not None:
            assert self.store.heartbeat_run(
                job_id=self.claim["job_id"],
                run_id=self.claim["run_id"],
                owner=self.claim["lease_owner"],
                fencing_token=self.claim["fencing_token"],
            )

    async def async_run(self, claim):
        self.claim = claim
        self.started.set()
        await self.pulse_async.wait()
        self.renew_run()
        self.progressed.set()
        await self.release_async.wait()
        return {"ok": True, "result_code": "ok"}

    def sync_run(self, claim=None):
        self.claim = claim
        self.loop.call_soon_threadsafe(self.started.set)
        # Only the test's finally block releases these gates on failure.
        self.pulse_thread.wait()
        self.renew_run()
        self.loop.call_soon_threadsafe(self.progressed.set)
        self.release_thread.wait()
        return {"ok": True, "result_code": "ok"}

    def pulse(self):
        self.pulse_thread.set()
        self.pulse_async.set()

    def release(self):
        self.pulse()
        self.release_thread.set()
        self.release_async.set()


def isolate_daemon(monkeypatch, clock, stopped):
    monkeypatch.setattr(automation_registry, "utc_now", lambda: clock.value)
    monkeypatch.setattr(automation_daemon, "CONTROLLER_HEARTBEAT_SECONDS", PERIOD, raising=False)
    monkeypatch.setattr(automation_daemon, "reject_obsolete_runtime_config", lambda: None)
    monkeypatch.setattr(
        automation_daemon, "notify_systemd", lambda message: stopped.set() if message == "STOPPING=1" else None
    )
    # This loop belongs to asyncio.run in this test; install no process signal handlers.
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *_args: None)


@pytest.mark.parametrize("kind", ["run_async", "run_thread", "outbox", "transient_write_failure"])
def test_healthy_busy_controller_stays_fresh_and_shutdown_drains_work(monkeypatch, tmp_path, kind):
    async def scenario():
        clock = Clock()
        stopping_reported = asyncio.Event()
        isolate_daemon(monkeypatch, clock, stopping_reported)
        store = AutomationStore(tmp_path / "registry.sqlite3")
        service = AutomationControlService(store, timer_controller=NoTimers())
        job = command(service, "create_from_template", {"template_id": "crm_digest_v1"}, "heartbeat-create-0001")["job"]
        job = command(
            service,
            "set_enabled",
            {"job_id": job["job_id"], "enabled": True},
            "heartbeat-enable-0001",
            job["revision"],
        )["job"]
        assert store.reconcile_next_job()["applied"] is True
        outbox = kind == "outbox"
        queued = command(
            service,
            "test_notification" if outbox else "run_now",
            {"job_id": job["job_id"]},
            "heartbeat-work-0001",
            job["revision"],
        )
        work = Work(store)

        class Notifier:
            def preview(self, text):
                return {
                    "mode": "dry_run",
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "contract_token": "synthetic-token",
                }

            def apply(self, *_args, **_kwargs):
                work.sync_run()
                return {"verified": True, "message_id": 71}

        server = NoServer()
        daemon = AutomationDaemon(
            store=store,
            timer_controller=NoTimers(),
            control_server=server,
            executors={"crm_digest_v1": work.async_run if kind == "run_async" else work.sync_run},
            notifier=Notifier(),
        )
        observed = observe_heartbeats(monkeypatch, store, clock, fail_once=kind == "transient_write_failure")
        original_tasks = asyncio.all_tasks()
        running = asyncio.create_task(daemon.run())
        try:
            await asyncio.wait_for(work.started.wait(), WATCHDOG)
            clock.advance(31)
            work.pulse()
            await asyncio.wait_for(work.progressed.wait(), WATCHDOG)
            await observe_next_pulse(observed)
            status = store.status(job_id=job["job_id"])
            assert not running.done() and not server.closed
            if outbox:
                with store.connect() as connection:
                    delivery = connection.execute(
                        "SELECT status, fencing_token FROM manager_automation_outbox WHERE outbox_id = ?",
                        (queued["outbox_id"],),
                    ).fetchone()
                assert delivery["status"] == "sending"
                assert store.outbox_claim_is_current(
                    outbox_id=queued["outbox_id"],
                    owner=daemon.owner,
                    fencing_token=delivery["fencing_token"],
                    claimed_revision=job["revision"],
                )
            else:
                assert status["jobs"][0]["actual_state"] == "running"
                assert status["jobs"][0]["heartbeat_at"] == isoformat(clock.value)
                assert store.run_claim_is_current(
                    job_id=job["job_id"],
                    run_id=queued["run_id"],
                    owner=daemon.owner,
                    fencing_token=work.claim["fencing_token"],
                    claimed_revision=job["revision"],
                )
            assert status["controller"]["state"] == "active"
            assert status["controller"]["heartbeat_at"] == isoformat(clock.value)
            assert status["readiness"]["ready"] is True
            if kind == "transient_write_failure":
                assert len(observed.failures) == 1

            daemon.stopping.set()
            await asyncio.wait_for(stopping_reported.wait(), WATCHDOG)
            assert not running.done() and not server.closed
            observed.pulse.clear()
            clock.advance(1)
            await observe_next_pulse(observed)
            assert store.status()["controller"]["state"] == "stopping"
            assert not running.done(), "shutdown returned before pending work completed"
        finally:
            work.release()
            daemon.stopping.set()
            await asyncio.wait_for(running, WATCHDOG)

        assert server.closed
        stopped = store.status()["controller"]
        assert stopped["state"] == "stopped"
        assert not (asyncio.all_tasks() - original_tasks), "daemon left background tasks alive"
        writes_after_shutdown = len(observed.writes)
        clock.advance(31)
        await asyncio.sleep(0)
        assert len(observed.writes) == writes_after_shutdown
        assert store.status()["controller"] == stopped
        with store.connect() as connection:
            table, reference = (
                ("manager_automation_outbox", "outbox_id") if outbox else ("manager_automation_runs", "run_id")
            )
            result = connection.execute(
                f"SELECT status, result_code FROM {table} WHERE {reference} = ?", (queued[reference],)
            ).fetchone()
        assert tuple(result) == ("sent" if outbox else "succeeded", "ok")

    asyncio.run(scenario())


def test_periodic_heartbeat_preserves_reconciliation_error_until_retry(monkeypatch, tmp_path):
    async def scenario():
        clock = Clock()
        isolate_daemon(monkeypatch, clock, asyncio.Event())

        class FailingReconciliation(AutomationStore):
            def reconcile_next_job(self):
                raise RuntimeError("synthetic reconcile failure")

        store = FailingReconciliation(tmp_path / "registry.sqlite3")
        observed = observe_heartbeats(monkeypatch, store, clock)
        daemon = AutomationDaemon(store=store, timer_controller=NoTimers(), control_server=NoServer(), tick_seconds=10)
        original_tasks = asyncio.all_tasks()
        running = asyncio.create_task(daemon.run())
        try:
            await asyncio.wait_for(observed.error.wait(), WATCHDOG)
            clock.advance(31)
            await observe_next_pulse(observed)
            controller = store.status()["controller"]
            assert controller["state"] == "error"
            assert controller["heartbeat_at"] == isoformat(clock.value)
        finally:
            daemon.stopping.set()
            await asyncio.wait_for(running, WATCHDOG)
        assert not (asyncio.all_tasks() - original_tasks)

    asyncio.run(scenario())


def test_inactive_controller_still_becomes_stale_after_thirty_seconds(monkeypatch, tmp_path):
    clock = Clock()
    monkeypatch.setattr(automation_registry, "utc_now", lambda: clock.value)
    store = AutomationStore(tmp_path / "registry.sqlite3")
    store.initialize()
    store.controller_heartbeat(owner="inactive-synthetic-controller")
    assert store.status()["controller"]["state"] == "active"
    clock.advance(31)
    status = store.status()
    assert status["controller"]["state"] == "stale"
    assert status["readiness"]["ready"] is False
