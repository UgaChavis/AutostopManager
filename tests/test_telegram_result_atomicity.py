"""Keep terminal rollback distinct from an error after a durable job result.

Private SQLite fixtures, real leases/effects/commit, and narrow fault injection.
No external send is performed. Closing/reopening uses the existing single owner
contract; a post-commit error must not be mistaken for permission to replay.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from autostop_manager.telegram_inbox import InboxError, TelegramInbox


NOW = datetime(2026, 10, 16, 9, tzinfo=UTC)
ROOT = "/opt/synthetic-result-review"
REVISION = "a" * 40


def _guard(job):
    return {"job_id": job["job_id"], "lease": job["lease"], "expected_revision": job["revision"]}


def _snapshot(inbox: TelegramInbox) -> dict[str, Any]:
    return {
        "generation": inbox.generation,
        "jobs": [tuple(row) for row in inbox._db.execute("SELECT * FROM jobs ORDER BY rowid")],
        "events": [tuple(row) for row in inbox._db.execute("SELECT * FROM events ORDER BY rowid")],
        "effects": [tuple(row) for row in inbox._db.execute("SELECT * FROM effects ORDER BY rowid")],
        "peers": [tuple(row) for row in inbox._db.execute("SELECT * FROM peers ORDER BY peer_id")],
        "peer_threads": [tuple(row) for row in inbox._db.execute("SELECT * FROM peer_threads ORDER BY peer_id")],
        "global_slot": [tuple(row) for row in inbox._db.execute("SELECT * FROM global_slot")],
    }


@pytest.fixture
def case(tmp_path: Path):
    inbox = TelegramInbox(tmp_path / "result" / "inbound.sqlite3", clock=lambda: NOW)
    try:
        inbox.duty_set(True)
        event_ids = [inbox.record(25, 1), inbox.record(25, 2)]
        assert all(isinstance(value, str) for value in event_ids)
        job = inbox.claim(instruction_root=ROOT, instruction_revision=REVISION)
        assert job is not None and job["event_ids"] == event_ids
        mapping = inbox.prepare_peer_thread(**_guard(job))
        inbox.bind_peer_thread(**_guard(job), creation_key=mapping["creation_key"], thread_id="synthetic-thread")
        job = inbox.start_intent(**_guard(job), thread_id="synthetic-thread")
        job = inbox.bind_turn(**_guard(job), turn_id="synthetic-turn")
        key = inbox.reserve_effect(
            **_guard(job), kind="result", text_sha256="b" * 64, peer_id=25, reply_to_message_id=1
        )
        inbox.mark_effect_intent(key)
        inbox.verify_effect(effect_key=key, receipt_key=key, message_id=101)
        job = inbox.read_job(job["job_id"])
        # A new event outside this job must remain open and unbound.
        unrelated = inbox.record(26, 1)
        assert isinstance(unrelated, str)
        yield SimpleNamespace(
            inbox=inbox, path=inbox.path, job=job, key=key, unrelated=unrelated, before=_snapshot(inbox)
        )
    finally:
        inbox.close()


def _result(inbox, case, job=None):
    return inbox.result(
        **_guard(case.job if job is None else job),
        outcome="answered",
        event_ids=case.job["event_ids"],
        receipt_keys=[case.key],
    )


def _assert_completed_once(inbox, case):
    current = inbox.read_job(case.job["job_id"])
    assert inbox._db.in_transaction is False
    assert current == {
        **case.job,
        "state": "completed",
        "revision": case.job["revision"] + 1,
        "outcome": "answered",
        "reason_code": None,
        "updated_at": NOW.isoformat(timespec="microseconds"),
    }
    assert inbox.generation == case.before["generation"] + 1
    assert all(inbox.event_state(event_id) == "reply_verified" for event_id in case.job["event_ids"])
    unrelated = inbox._event_row(case.unrelated)
    assert unrelated["state"] == "open" and unrelated["job_id"] is None and unrelated["completed_at"] is None
    assert _snapshot(inbox)["effects"] == case.before["effects"]
    assert _snapshot(inbox)["peers"] == case.before["peers"]
    terminal = _snapshot(inbox)
    for guard_job, code in ((case.job, "inbox_revision_conflict"), (current, "inbox_job_not_active")):
        with pytest.raises(InboxError, match=f"^{code}$"):
            _result(inbox, case, guard_job)
        assert _snapshot(inbox) == terminal


def _trace_terminal_updates(inbox):
    attempted = []
    # The observer records attempts, without performing SQL writes. Its Python
    # list survives rollback so the second-event fault cannot pass vacuously.
    inbox._db.create_function("synthetic_terminal_update", 1, attempted.append)
    return attempted


def _reopen_after_rollback(case):
    case.inbox.close()
    with TelegramInbox(case.path, clock=lambda: NOW) as reopened:
        assert _snapshot(reopened) == case.before
        assert reopened.read_job(case.job["job_id"])["state"] == "running"
        assert all(reopened.event_state(event_id) == "open" for event_id in case.job["event_ids"])
        # After exact readback and removal of the synthetic fault, the original
        # guard is still valid. This is the first committed terminal transition.
        assert _result(reopened, case)["state"] == "completed"
        _assert_completed_once(reopened, case)


def test_second_terminal_event_update_failure_rolls_back_whole_result(case):
    inbox = case.inbox
    sequences = [value.removeprefix("inbound-") for value in case.job["event_ids"]]
    assert all(value.isascii() and value.isdigit() for value in sequences)
    attempted = _trace_terminal_updates(inbox)
    inbox._db.execute(
        "CREATE TRIGGER synthetic_terminal_second BEFORE UPDATE OF state ON events "
        "WHEN NEW.state='reply_verified' BEGIN SELECT synthetic_terminal_update(OLD.sequence); "
        f"SELECT RAISE(ABORT, 'synthetic_second_terminal') WHERE OLD.sequence='{sequences[1]}'; END"
    )
    inbox._db.commit()
    with pytest.raises(InboxError, match=r"^inbox_storage_failed$"):
        _result(inbox, case)
    assert attempted == sequences
    assert inbox._db.in_transaction is False and _snapshot(inbox) == case.before
    inbox._db.execute("DROP TRIGGER synthetic_terminal_second")
    inbox._db.commit()
    _reopen_after_rollback(case)


def test_generation_failure_rolls_back_job_outcome_events_and_context(case):
    inbox = case.inbox
    attempted = _trace_terminal_updates(inbox)
    inbox._db.execute(
        "CREATE TRIGGER synthetic_terminal_trace BEFORE UPDATE OF state ON events "
        "WHEN NEW.state='reply_verified' BEGIN SELECT synthetic_terminal_update(OLD.sequence); END"
    )
    inbox._db.execute(
        "CREATE TRIGGER synthetic_terminal_generation BEFORE UPDATE OF value ON metadata "
        "WHEN OLD.key='generation' BEGIN SELECT RAISE(ABORT, 'synthetic_generation'); END"
    )
    inbox._db.commit()
    with pytest.raises(InboxError, match=r"^inbox_storage_failed$"):
        _result(inbox, case)
    assert attempted == [value.removeprefix("inbound-") for value in case.job["event_ids"]]
    assert inbox._db.in_transaction is False and _snapshot(inbox) == case.before
    inbox._db.execute("DROP TRIGGER synthetic_terminal_generation")
    inbox._db.execute("DROP TRIGGER synthetic_terminal_trace")
    inbox._db.commit()
    _reopen_after_rollback(case)


def test_secure_files_error_after_commit_preserves_completed_result_on_reopen(case, monkeypatch):
    inbox = case.inbox
    failures = []

    def fail_after_commit():
        assert inbox._db.in_transaction is False
        assert inbox.generation == case.before["generation"] + 1
        failures.append(inbox.read_job(case.job["job_id"])["state"])
        raise InboxError("inbox_file_invalid")

    with monkeypatch.context() as patch:
        patch.setattr(inbox, "_secure_files", fail_after_commit)
        with pytest.raises(InboxError, match=r"^inbox_file_invalid$"):
            _result(inbox, case)
    assert failures == ["completed"]
    inbox.close()
    with TelegramInbox(case.path, clock=lambda: NOW) as reopened:
        _assert_completed_once(reopened, case)


def test_final_read_error_after_commit_preserves_completed_result_on_reopen(case, monkeypatch):
    inbox = case.inbox
    original = inbox.read_job
    failures = []

    def fail_terminal_read(job_id):
        job = original(job_id)
        if job_id == case.job["job_id"] and job["state"] == "completed":
            assert inbox._db.in_transaction is False
            assert inbox.generation == case.before["generation"] + 1
            failures.append(job_id)
            raise InboxError("inbox_storage_failed")
        return job

    with monkeypatch.context() as patch:
        patch.setattr(inbox, "read_job", fail_terminal_read)
        with pytest.raises(InboxError, match=r"^inbox_storage_failed$"):
            _result(inbox, case)
    assert failures == [case.job["job_id"]]
    inbox.close()
    with TelegramInbox(case.path, clock=lambda: NOW) as reopened:
        _assert_completed_once(reopened, case)
