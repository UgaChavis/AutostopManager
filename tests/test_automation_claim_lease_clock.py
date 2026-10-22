"""A claim's lease starts after its preparation and writer-lock delay.

Only the module clock advances virtually. The second case holds a real SQLite
writer lock briefly; neither case claims to simulate a 61-second busy timeout.
Public commands model an already-dispatched service worker, without a socket,
daemon, executor, provider or timer operation.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from autostop_manager import automation_registry as registry
from autostop_manager.automation_control import AUTOMATION_CONTROL_PROTOCOL, AutomationControlService


START = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
OWNER = "synthetic-claim-clock-worker"
RUN_KEY = "claim-clock-manual-0001"


class NoTimers:
    def __getattr__(self, name):
        raise AssertionError(f"Timer/service operation forbidden: {name}")


def command(service, operation, payload, key, revision=None):
    request = {
        "protocol": AUTOMATION_CONTROL_PROTOCOL,
        "request_id": str(uuid4()),
        "operation": operation,
        "actor": {"kind": "codex", "id": "synthetic-claim-clock-owner", "is_admin": True},
        "payload": payload,
        "idempotency_key": key,
    }
    if revision is not None:
        request["expected_revision"] = revision
    return service.handle(request)


def snapshot(store):
    with closing(sqlite3.connect(store.path)) as connection:
        connection.row_factory = sqlite3.Row
        return {
            "runs": [dict(row) for row in connection.execute("SELECT * FROM manager_automation_runs ORDER BY rowid")],
            "runtime": dict(connection.execute("SELECT * FROM manager_automation_runtime").fetchone()),
        }


@dataclass
class Case:
    store: registry.AutomationStore
    control: AutomationControlService
    clock: list[datetime]
    job: dict

    def request_run(self):
        return command(self.control, "run_now", {"job_id": self.job["job_id"]}, RUN_KEY, self.job["revision"])


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
        "claim-clock-create-0001",
    )["job"]
    command(
        service,
        "set_enabled",
        {"job_id": created["job_id"], "enabled": True},
        "claim-clock-enable-0001",
        created["revision"],
    )
    applied = store.reconcile_next_job()
    assert applied is not None and applied["applied"] is True
    assert applied["job"]["next_run_at"] == "2026-10-21T12:20:00Z"
    assert snapshot(store)["runs"] == []
    # A separate supported service/store must not enter the claim's test gate.
    control = AutomationControlService(registry.AutomationStore(store.path), timer_controller=NoTimers())
    return Case(store, control, clock, applied["job"])


@dataclass
class Work:
    initialized: threading.Event = field(default_factory=threading.Event)
    resume: threading.Event = field(default_factory=threading.Event)
    begin: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    claims: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    statements: list = field(default_factory=list)
    open_connections: int = 0


@contextmanager
def claim_worker(case, monkeypatch):
    work = Work()
    initialize = registry.AutomationStore.initialize
    connect = registry.AutomationStore.connect

    def after_real_initialize(self):
        initialize(self)
        if self is case.store:
            work.initialized.set()
            assert work.resume.wait(3), "claim preparation gate was not released"

    def trace(statement):
        work.statements.append(statement)
        if statement.strip().upper() == "BEGIN IMMEDIATE":
            work.begin.set()

    @contextmanager
    def traced_connect(self):
        with connect(self) as connection:
            if self is not case.store:
                yield connection
                return
            work.open_connections += 1
            try:
                if work.initialized.is_set():
                    connection.set_trace_callback(trace)
                yield connection
            finally:
                work.open_connections -= 1

    def claim():
        try:
            work.claims.append(case.store.claim_next_run(owner=OWNER))  # Default TTL60.
        except Exception as exc:  # noqa: BLE001 - propagate owned worker failures to the test.
            work.errors.append(exc)
        finally:
            work.done.set()

    with monkeypatch.context() as patch:
        patch.setattr(registry.AutomationStore, "initialize", after_real_initialize)
        patch.setattr(registry.AutomationStore, "connect", traced_connect)
        thread = threading.Thread(target=claim, name="synthetic-claim-clock", daemon=True)
        thread.start()
        try:
            assert work.initialized.wait(3), "real initialize did not complete"
            yield work
        finally:
            work.resume.set()
            thread.join(3)
            assert not thread.is_alive(), "owned claim worker did not stop"
            assert work.open_connections == 0
            assert work.errors == []


def assert_usable_claim(case, work, accepted):
    assert work.done.is_set() and len(work.claims) == 1
    claim = work.claims[0]
    assert claim is not None and claim["run_id"] == accepted["run_id"]
    state = snapshot(case.store)
    assert len(state["runs"]) == 1 and state["runs"][0]["status"] == "running"
    assert state["runtime"]["fencing_token"] == claim["fencing_token"] == state["runs"][0]["fencing_token"]
    assert state["runtime"]["lease_until"] == claim["lease_until"]
    assert registry.parse_time(claim["lease_until"]) > case.clock[0]
    assert (
        case.store.run_claim_is_current(
            job_id=claim["job_id"],
            run_id=claim["run_id"],
            owner=OWNER,
            fencing_token=claim["fencing_token"],
            claimed_revision=claim["claimed_revision"],
        )
        is True
    )
    assert (
        case.store.heartbeat_run(
            job_id=claim["job_id"], run_id=claim["run_id"], owner=OWNER, fencing_token=claim["fencing_token"]
        )
        is True
    )
    return claim, state


def test_fresh_public_request_after_preparation_gets_a_current_claim(case, monkeypatch):
    with claim_worker(case, monkeypatch) as work:
        assert snapshot(case.store)["runs"] == []
        case.clock[0] += timedelta(seconds=61)
        accepted = case.request_run()
        queued = snapshot(case.store)
        assert accepted["queued"] is True
        assert queued["runs"][0]["status"] == "queued"
        assert queued["runtime"]["lease_until"] is None
        work.resume.set()
        assert work.done.wait(3), "claim did not finish after preparation"
        claim, state = assert_usable_claim(case, work, accepted)
        assert registry.parse_time(state["runs"][0]["started_at"]) >= registry.parse_time(
            queued["runs"][0]["requested_at"]
        )
        assert registry.parse_time(claim["lease_until"]) >= case.clock[0] + timedelta(seconds=60)
        assert case.request_run() == {**accepted, "idempotent_replay": True}
        assert snapshot(case.store) == state


def test_real_competing_writer_does_not_spend_the_new_claim_lease(case, monkeypatch):
    accepted = case.request_run()
    with claim_worker(case, monkeypatch) as work:
        with closing(sqlite3.connect(case.store.path)) as writer, writer:
            writer.execute("BEGIN IMMEDIATE")
            work.resume.set()
            assert work.begin.wait(3), "claim did not attempt its real BEGIN"
            # A negative completion check while SQLite holds a real writer lock;
            # it does not drive expiry or stand in for a long wall-clock wait.
            assert not work.done.wait(0.05), "claim completed while the writer lock was held"
            assert writer.in_transaction
            assert len(work.statements) == 1 and work.statements[0].strip().upper() == "BEGIN IMMEDIATE"
            assert snapshot(case.store)["runs"][0]["status"] == "queued"
            case.clock[0] += timedelta(seconds=20)
            writer.commit()
        assert work.done.wait(3), "claim did not finish after writer release"
        claim, state = assert_usable_claim(case, work, accepted)
        assert registry.parse_time(claim["lease_until"]) >= case.clock[0] + timedelta(seconds=60)
        assert registry.parse_time(state["runs"][0]["started_at"]) >= case.clock[0]
