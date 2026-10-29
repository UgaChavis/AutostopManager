"""Terminal receipts cover the exact inbound content, even within one context revision."""

from __future__ import annotations

import pytest

from autostop_manager.telegram_inbox import InboxError, TelegramInbox
from test_telegram_inbox import Clock, guard, receipt, running

OUTCOMES = [("answered", "result"), ("awaiting_client", "clarification"), ("handoff", "handoff")]


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "unused.sqlite3"))
    with TelegramInbox(tmp_path / "inbox" / "inbound.sqlite3", clock=Clock()) as result:
        result.duty_set(True)
        yield result


@pytest.mark.parametrize("outcome,kind", OUTCOMES)
def test_receipt_cannot_close_later_attached_input_at_the_same_context_revision(inbox, outcome, kind):
    first = inbox.record(42, 100)
    job = running(inbox)
    second = inbox.record(42, 101)
    job = inbox.refresh_job(**guard(job), event_ids=[first])
    key = receipt(inbox, job, kind=kind)
    job = inbox.refresh_job(**guard(job), event_ids=[first, second])
    assert job["effects"][0]["context_revision"] == job["context_revision"]
    before = inbox.read_job(job["job_id"])
    generation = inbox.generation

    with pytest.raises(InboxError, match=r"^inbox_context_stale$"):
        inbox.result(**guard(job), outcome=outcome, event_ids=[first, second], receipt_keys=[key])

    assert inbox.read_job(job["job_id"]) == before
    assert inbox.generation == generation
    assert inbox.event_state(first) == inbox.event_state(second) == "open"


@pytest.mark.parametrize("outcome,kind", OUTCOMES)
def test_receipt_for_all_attached_input_can_complete(inbox, outcome, kind):
    first = inbox.record(42, 100)
    job = running(inbox)
    second = inbox.record(42, 101)
    job = inbox.refresh_job(**guard(job), event_ids=[first, second])
    key = receipt(inbox, job, kind=kind)

    done = inbox.result(**guard(job), outcome=outcome, event_ids=[first, second], receipt_keys=[key])

    assert done["state"] == "completed"
    assert inbox.event_state(first) == inbox.event_state(second) == "reply_verified"


@pytest.mark.parametrize("outcome,kind", OUTCOMES)
def test_legacy_receipt_without_content_proof_cannot_close_input(inbox, outcome, kind):
    event = inbox.record(42, 100)
    job = running(inbox)
    key = receipt(inbox, job, kind=kind)
    inbox._db.execute("UPDATE effects SET event_coverage_sha256=NULL WHERE receipt_key=?", (key,))
    inbox._db.commit()

    with pytest.raises(InboxError, match=r"^inbox_context_stale$"):
        inbox.result(**guard(job), outcome=outcome, event_ids=[event], receipt_keys=[key])

    assert inbox.event_state(event) == "open"


def test_coverage_and_kind_must_belong_to_the_same_receipt(inbox):
    first = inbox.record(42, 100)
    job = running(inbox)
    second = inbox.record(42, 101)
    job = inbox.refresh_job(**guard(job), event_ids=[first])
    old_result = receipt(inbox, job)
    job = inbox.refresh_job(**guard(job), event_ids=[first, second])
    current_question = receipt(inbox, job, kind="clarification")

    with pytest.raises(InboxError, match=r"^inbox_context_stale$"):
        inbox.result(
            **guard(job),
            outcome="answered",
            event_ids=[first, second],
            receipt_keys=[old_result, current_question],
        )

    assert inbox.event_state(second) == "open"
