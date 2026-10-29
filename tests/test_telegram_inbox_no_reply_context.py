"""No-reply decisions need current context; delivered results and pauses keep their proof."""

from __future__ import annotations

import pytest

from autostop_manager.telegram_inbox import InboxError, TelegramInbox
from test_telegram_inbox import Clock, guard, receipt, running


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "unused.sqlite3"))
    with TelegramInbox(tmp_path / "inbox" / "inbound.sqlite3", clock=Clock()) as result:
        result.duty_set(True)
        yield result


@pytest.mark.parametrize("reason", ["duplicate", "spam", "reaction_only", "not_actionable"])
def test_generic_no_reply_rejects_edited_context_until_reread_and_refresh(inbox, reason):
    event = inbox.record(42, 100)
    job = running(inbox)
    inbox.invalidate_context(42)  # The bridge invalidates, but creates no inbound, for an edit.
    before = inbox.read_job(job["job_id"])
    generation = inbox.generation
    with pytest.raises(InboxError, match=r"^inbox_context_stale$"):
        inbox.result(**guard(job), outcome="no_reply_needed", reason_code=reason, event_ids=[event], receipt_keys=[])
    assert inbox.read_job(job["job_id"]) == before
    assert inbox.generation == generation
    assert inbox.event_state(event) == "open"

    # A caller may classify the reread message again only after acknowledging its current context.
    refreshed = inbox.refresh_job(**guard(job), event_ids=[event])
    done = inbox.result(
        **guard(refreshed), outcome="no_reply_needed", reason_code=reason, event_ids=[event], receipt_keys=[]
    )
    assert done["state"] == "completed" and inbox.event_state(event) == "no_reply_needed"


@pytest.mark.parametrize("action,reason", [("pause", "paused"), ("takeover", "human_takeover")])
def test_waiting_control_keeps_unread_request_open_without_refresh(inbox, action, reason):
    event = inbox.record(42, 100)
    job = running(inbox)
    inbox.set_client_control(42, action=action, expected_revision=0)
    done = inbox.result(**guard(job), outcome="no_reply_needed", reason_code=reason, event_ids=[event], receipt_keys=[])
    assert done["state"] == "waiting_control"
    assert inbox.event_state(event) == "open"
    assert done["client_control"]["state"] == reason


@pytest.mark.parametrize("kind", ["result", "handoff"])
def test_late_pause_preserves_verified_answer_or_handoff_without_new_send(inbox, kind):
    event = inbox.record(42, 100)
    job = running(inbox)
    key = receipt(inbox, job, kind=kind)
    inbox.set_client_control(42, action="pause", expected_revision=0)
    done = inbox.result(
        **guard(job), outcome="no_reply_needed", reason_code="already_answered", event_ids=[event], receipt_keys=[key]
    )
    assert done["state"] == "completed"
    assert done["client_control"]["state"] == "paused"
    assert [effect["receipt_key"] for effect in done["effects"]] == [key]


def test_late_pause_does_not_allow_already_answered_without_verified_receipt(inbox):
    event = inbox.record(42, 100)
    job = running(inbox)
    inbox.set_client_control(42, action="pause", expected_revision=0)
    with pytest.raises(InboxError, match=r"^inbox_result_receipt_required$"):
        inbox.result(
            **guard(job), outcome="no_reply_needed", reason_code="already_answered", event_ids=[event], receipt_keys=[]
        )
    assert inbox.event_state(event) == "open"


def test_late_pause_keeps_delivered_clarification_receipt_valid(inbox):
    event = inbox.record(42, 100)
    job = running(inbox)
    key = receipt(inbox, job, kind="clarification")
    inbox.set_client_control(42, action="pause", expected_revision=0)
    done = inbox.result(**guard(job), outcome="awaiting_client", event_ids=[event], receipt_keys=[key])
    assert done["state"] == "completed" and done["outcome"] == "awaiting_client"
    assert done["client_control"]["state"] == "paused"
    assert [effect["receipt_key"] for effect in done["effects"]] == [key]


def test_human_answer_still_requires_current_context_before_completion(inbox):
    event = inbox.record(42, 100)
    job = running(inbox)
    assert inbox.observe_outgoing(42, 101) == "human"
    job = inbox.refresh_job(**guard(job), event_ids=[event])
    inbox.invalidate_context(42)
    result = {
        "outcome": "no_reply_needed",
        "reason_code": "human_answered",
        "event_ids": [event],
        "receipt_keys": [],
        "human_message_ids": [101],
    }
    with pytest.raises(InboxError, match=r"^inbox_context_stale$"):
        inbox.result(**guard(job), **result)
    assert inbox.event_state(event) == "open"
    refreshed = inbox.refresh_job(**guard(job), event_ids=[event])
    assert inbox.result(**guard(refreshed), **result)["state"] == "completed"
