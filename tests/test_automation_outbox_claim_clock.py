from __future__ import annotations

import asyncio
import hashlib
import threading
from concurrent.futures import Future
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from autostop_manager import automation_registry as registry
from autostop_manager.automation_daemon import AutomationDaemon, TEST_NOTIFICATION_TEXT

OWNER = "synthetic-outbox-clock-owner"
START = datetime(2030, 1, 1, tzinfo=UTC)


def _due_notification(path: Path) -> tuple[registry.AutomationStore, str]:
    store = registry.AutomationStore(path)
    actor_hash = registry.technical_actor_hash("codex", "synthetic-outbox-clock-test")
    created = store.execute_command(
        source="codex",
        idempotency_key="clock-create-digest-0001",
        operation="create_from_template",
        request_hash=hashlib.sha256(b"create").hexdigest(),
        actor_hash=actor_hash,
        mutation=lambda connection, _command_id: store.create_from_template(
            connection,
            template_id="crm_digest_v1",
            payload={"template_id": "crm_digest_v1"},
            actor_hash=actor_hash,
        ),
    )["job"]
    queued = store.execute_command(
        source="codex",
        idempotency_key="clock-notification-test-0001",
        operation="test_notification",
        request_hash=hashlib.sha256(b"notification").hexdigest(),
        actor_hash=actor_hash,
        mutation=lambda connection, command_id: store.enqueue_notification_test(
            connection,
            job_id=created["job_id"],
            command_id=command_id,
            expected_revision=created["revision"],
            idempotency_key="clock-notification-test-0001",
        ),
    )
    with store.connect() as connection:
        row = connection.execute(
            "SELECT status, attempts, next_attempt_at FROM manager_automation_outbox WHERE outbox_id = ?",
            (queued["outbox_id"],),
        ).fetchone()
    assert tuple(row) == ("queued", 0, registry.isoformat(START))
    return store, queued["outbox_id"]


def _start_claim(store: registry.AutomationStore) -> tuple[threading.Thread, Future]:
    future: Future = Future()

    def claim():
        try:
            future.set_result(store.claim_outbox(owner=OWNER))
        except Exception as error:  # noqa: BLE001 - Propagate worker failures to the test thread.
            future.set_exception(error)

    worker = threading.Thread(target=claim, name="synthetic-outbox-claim")
    worker.start()
    return worker, future


class SyntheticNotifier:
    def __init__(self, idempotency_key: str):
        self.idempotency_key = idempotency_key
        self.calls: list[str] = []

    def preview(self, text: str) -> dict[str, Any]:
        assert text == TEST_NOTIFICATION_TEXT
        self.calls.append("preview")
        return {
            "mode": "dry_run",
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "contract_token": "synthetic-outbox-token",
        }

    def apply(self, text: str, *, contract_token: str, idempotency_key: str) -> dict[str, Any]:
        assert text == TEST_NOTIFICATION_TEXT
        assert contract_token == "synthetic-outbox-token"
        assert idempotency_key == self.idempotency_key
        self.calls.append("apply")
        return {"verified": True, "message_id": 123}

    def lookup(self, **_kwargs):
        self.calls.append("lookup")
        raise AssertionError("A fresh unsent intent must not enter unknown-outcome recovery")

    def readback(self, **_kwargs):
        self.calls.append("readback")
        raise AssertionError("The synthetic notification uses a verified apply receipt")


def test_outbox_claim_after_pre_begin_pause_can_complete_without_unknown_recovery(tmp_path: Path, monkeypatch):
    clock = [START]
    monkeypatch.setattr(registry, "utc_now", lambda: clock[0])
    store, outbox_id = _due_notification(tmp_path / "registry.sqlite3")
    initialized = threading.Event()
    release = threading.Event()
    original_initialize = registry.AutomationStore.initialize

    def pause_after_initialize(self):
        original_initialize(self)
        if self is store:
            initialized.set()
            assert release.wait(timeout=3)

    monkeypatch.setattr(registry.AutomationStore, "initialize", pause_after_initialize)
    worker, future = _start_claim(store)
    try:
        assert initialized.wait(timeout=3)
        clock[0] += timedelta(seconds=61)
    finally:
        release.set()
        worker.join(timeout=3)
    assert not worker.is_alive()
    claim = future.result(timeout=0)
    assert claim is not None and claim["outbox_id"] == outbox_id
    assert registry.parse_time(claim["lease_until"]) == clock[0] + timedelta(seconds=60)
    assert claim["recovery_only"] is False
    assert store.outbox_claim_is_current(
        outbox_id=outbox_id,
        owner=OWNER,
        fencing_token=claim["fencing_token"],
        claimed_revision=claim["claimed_revision"],
    )

    notifier = SyntheticNotifier(claim["idempotency_key"])
    inert_daemon = SimpleNamespace(store=store, owner=OWNER, notifier=notifier)

    async def execute():
        await asyncio.wait_for(AutomationDaemon._execute_outbox(inert_daemon, claim), timeout=2)

    asyncio.run(execute())
    assert notifier.calls == ["preview", "apply"]
    with store.connect() as connection:
        row = connection.execute(
            "SELECT status, attempts, result_code, lease_owner_hash, lease_until "
            "FROM manager_automation_outbox WHERE outbox_id = ?",
            (outbox_id,),
        ).fetchone()
    assert tuple(row) == ("sent", 1, "ok", None, None)


def test_outbox_claim_gets_full_lease_after_waiting_for_sqlite_writer(tmp_path: Path, monkeypatch):
    clock = [START]
    monkeypatch.setattr(registry, "utc_now", lambda: clock[0])
    store, outbox_id = _due_notification(tmp_path / "registry.sqlite3")
    initialized = threading.Event()
    release = threading.Event()
    begin_attempted = threading.Event()
    original_initialize = registry.AutomationStore.initialize
    original_connect = registry.AutomationStore.connect

    def pause_after_initialize(self):
        original_initialize(self)
        if self is store:
            initialized.set()
            assert release.wait(timeout=3)

    @contextmanager
    def observed_connect(self):
        with original_connect(self) as connection:
            if self is store and initialized.is_set() and threading.current_thread().name == "synthetic-outbox-claim":
                connection.set_trace_callback(lambda sql: begin_attempted.set() if sql == "BEGIN IMMEDIATE" else None)
            yield connection

    monkeypatch.setattr(registry.AutomationStore, "initialize", pause_after_initialize)
    monkeypatch.setattr(registry.AutomationStore, "connect", observed_connect)
    worker, future = _start_claim(store)
    try:
        assert initialized.wait(timeout=3)
        with original_connect(store) as writer:
            writer.execute("BEGIN IMMEDIATE")
            try:
                release.set()
                assert begin_attempted.wait(timeout=3)
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.05)
                clock[0] += timedelta(seconds=20)
            finally:
                writer.rollback()
    finally:
        release.set()
        worker.join(timeout=3)
    assert not worker.is_alive()
    claim = future.result(timeout=0)
    assert claim is not None and claim["outbox_id"] == outbox_id
    assert registry.parse_time(claim["lease_until"]) == clock[0] + timedelta(seconds=60)
    with store.connect() as connection:
        row = connection.execute(
            "SELECT status, attempts, lease_until FROM manager_automation_outbox WHERE outbox_id = ?",
            (outbox_id,),
        ).fetchone()
    assert tuple(row) == ("sending", 1, claim["lease_until"])
    assert store.outbox_claim_is_current(
        outbox_id=outbox_id,
        owner=OWNER,
        fencing_token=claim["fencing_token"],
        claimed_revision=claim["claimed_revision"],
    )
