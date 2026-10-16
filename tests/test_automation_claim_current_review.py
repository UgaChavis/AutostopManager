"""Regressions for AutomationStore.run_claim_is_current.

Uses real SQLite, real control commands and a synthetic clock. Delays occur at
connection entry or immediately before the real claim SELECT. The target store
is frozen, so connect is wrapped on its class with an instance/one-shot guard.
SQL and cursor results are never replaced. No sleep, executors or external IO.

The accepted regression has no valid linearization point: hold is enabled at
call entry and is released only after the original lease expires. A simple
unchanged-row delay is deliberately not treated as a separate defect. These
tests impose no new contract on claim_next_run or cancellation.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from autostop_manager import automation_registry
from autostop_manager.automation_control import AUTOMATION_CONTROL_PROTOCOL, AutomationControlService
from autostop_manager.automation_registry import AutomationStore


START = datetime(2026, 10, 16, 9, 0, 0, tzinfo=UTC)
OWNER = "synthetic-current-check-worker"
HOLD_ATTEMPT = "b" * 64
HOLD_ACTOR = {"kind": "system", "id": "synthetic-current-check-release", "is_admin": True}
STATE_TABLES = (
    "manager_automations",
    "manager_automation_runtime",
    "manager_automation_runs",
    "manager_automation_cursors",
    "manager_automation_outbox",
    "manager_automation_controller_runtime",
    "manager_automation_commands",
)


@dataclass
class Clock:
    value: datetime = START

    def __call__(self) -> datetime:
        return self.value


@dataclass
class Case:
    store: AutomationStore
    control: AutomationControlService
    clock: Clock
    claim: dict[str, Any]


def command(service, operation, payload, key, *, expected_revision=None, actor=None):
    request = {
        "protocol": AUTOMATION_CONTROL_PROTOCOL,
        "request_id": str(uuid4()),
        "operation": operation,
        "actor": actor or {"kind": "codex", "id": "synthetic-current-check-owner", "is_admin": True},
        "payload": payload,
        "idempotency_key": key,
    }
    if expected_revision is not None:
        request["expected_revision"] = expected_revision
    return service.handle(request)


@pytest.fixture
def case(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(automation_registry, "utc_now", clock)
    store = AutomationStore(tmp_path / "claim-current" / "manager.sqlite3")
    service = AutomationControlService(store)
    created = command(service, "create_from_template", {"template_id": "crm_digest_v1"}, "current-check-create-0001")[
        "job"
    ]
    command(
        service,
        "set_enabled",
        {"job_id": created["job_id"], "enabled": True},
        "current-check-enable-0001",
        expected_revision=created["revision"],
    )
    reconciled = store.reconcile_next_job()
    assert reconciled is not None and reconciled["applied"] is True
    job = reconciled["job"]
    queued = command(
        service,
        "run_now",
        {"job_id": job["job_id"]},
        "current-check-run-0001",
        expected_revision=job["revision"],
    )
    assert queued["queued"] is True and queued["coalesced"] is False
    claim = store.claim_next_run(owner=OWNER, lease_seconds=10)
    assert claim is not None and claim["run_id"] == queued["run_id"]
    # Independent supported control connection, not direct mutation of SQL rows.
    control = AutomationControlService(AutomationStore(store.path))
    return Case(store, control, clock, claim)


def current_args(case):
    return {
        "job_id": case.claim["job_id"],
        "run_id": case.claim["run_id"],
        "owner": OWNER,
        "fencing_token": case.claim["fencing_token"],
        "claimed_revision": case.claim["claimed_revision"],
    }


def snapshot(store):
    with store.connect() as connection:
        return {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in STATE_TABLES
        }


def lease_deadline(case):
    return datetime.fromisoformat(case.claim["lease_until"].replace("Z", "+00:00"))


def set_hold(case, enabled, expected_revision):
    return command(
        case.control,
        "set_global_hold",
        {"enabled": enabled, "reason": "release" if enabled else None, "attempt_hash": HOLD_ATTEMPT},
        "current-check-hold-0001" if enabled else "current-check-release-hold-0001",
        expected_revision=expected_revision,
        actor=HOLD_ACTOR,
    )["global_hold"]


def assert_read_check(case, expected, **overrides):
    arguments = {**current_args(case), **overrides}
    before = snapshot(case.store)
    current = case.store.run_claim_is_current(**arguments)
    assert snapshot(case.store) == before
    assert current is expected


@pytest.mark.parametrize("delay_point", ["connect_entry", "before_select"])
@pytest.mark.parametrize(
    ("seconds_from_expiry", "expected"),
    [pytest.param(1, False, id="hold-released-after-expiry"), pytest.param(-1, True, id="hold-released-before-expiry")],
)
def test_current_uses_lease_time_consistent_with_legal_hold_release(
    case, monkeypatch, delay_point, seconds_from_expiry, expected
):
    held = set_hold(case, True, expected_revision=0)
    assert held["enabled"] is True and held["revision"] == 1
    assert START < lease_deadline(case)
    assert_read_check(case, False)
    store_class = type(case.store)
    real_connect = store_class.connect
    injected = False
    select_calls = 0
    expected_after_control = None
    observations = [("call_entry", case.clock(), True)]

    def release_hold(connection):
        nonlocal injected, expected_after_control
        assert not injected
        injected = True
        # No read snapshot/transaction has begun, so the ordinary control write
        # below can commit before this call's real SELECT on either boundary.
        assert connection.in_transaction is False
        case.clock.value = lease_deadline(case) + timedelta(seconds=seconds_from_expiry)
        with case.control.store.connect() as check:
            row = check.execute(
                "SELECT hold_enabled FROM manager_automation_controller_runtime WHERE singleton=1"
            ).fetchone()
            assert row is not None and row[0] == 1
        observations.append(("before_release", case.clock(), True))
        released = set_hold(case, False, expected_revision=held["revision"])
        assert released["enabled"] is False and released["revision"] == 2
        observations.append(("after_release", case.clock(), False))
        # Only legal control changes are permitted during the check. Capture
        # those before SELECT so subsequent read-check writes cannot hide in it.
        expected_after_control = snapshot(case.control.store)

    class SelectBoundary:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args, **kwargs):
            nonlocal select_calls
            normalized = " ".join(sql.split()).casefold()
            if normalized.startswith("select ") and "from manager_automation_runs" in normalized:
                select_calls += 1
                if delay_point == "before_select" and not injected:
                    release_hold(self.connection)
            return self.connection.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    @contextmanager
    def connection_with_boundary(self):
        with real_connect(self) as connection:
            # Class patch accommodates frozen dataclass; control's second store
            # must pass through unchanged and cannot recurse into injection.
            if self is not case.store:
                yield connection
                return
            if delay_point == "connect_entry" and not injected:
                release_hold(connection)
            yield SelectBoundary(connection)

    with monkeypatch.context() as patch:
        patch.setattr(store_class, "connect", connection_with_boundary)
        current = case.store.run_claim_is_current(**current_args(case))

    release_time = lease_deadline(case) + timedelta(seconds=seconds_from_expiry)
    assert injected and select_calls == 1
    assert observations == [
        ("call_entry", START, True),
        ("before_release", release_time, True),
        ("after_release", release_time, False),
    ]
    assert expected_after_control is not None
    assert snapshot(case.store) == expected_after_control
    assert current is expected, {
        "delay_point": delay_point,
        "lease_until": case.claim["lease_until"],
        "observations": observations,
        "current": current,
    }


@pytest.mark.parametrize(
    ("clock_position", "expected"),
    [
        pytest.param("start", True, id="active-claim"),
        pytest.param("before", True, id="before-expiry"),
        pytest.param("exact", False, id="exact-expiry"),
    ],
)
def test_current_lease_boundary_is_read_only(case, clock_position, expected):
    if clock_position != "start":
        case.clock.value = lease_deadline(case) - timedelta(seconds=clock_position == "before")
    assert_read_check(case, expected)


@pytest.mark.parametrize("wrong_field", ["owner", "fencing_token", "claimed_revision", "run_id"])
def test_current_rejects_foreign_claim_identity_without_writes(case, wrong_field):
    replacements = {
        "owner": "synthetic-other-worker",
        "fencing_token": case.claim["fencing_token"] + 1,
        "claimed_revision": case.claim["claimed_revision"] + 1,
        "run_id": "run_" + "0" * 32,
    }
    assert_read_check(case, False, **{wrong_field: replacements[wrong_field]})


def test_current_rejects_enabled_hold_without_writes(case):
    held = set_hold(case, True, expected_revision=0)
    assert held["enabled"] is True
    assert_read_check(case, False)
