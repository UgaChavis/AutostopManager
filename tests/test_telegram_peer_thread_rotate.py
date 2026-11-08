"""Rotation keeps the same durable job while reserving one fresh peer thread."""

from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_inbox import InboxError, TelegramInbox


def guard(job):
    return {"job_id": job["job_id"], "lease": job["lease"], "expected_revision": job["revision"]}


@pytest.fixture
def case(tmp_path):
    with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
        inbox.duty_set(True)
        first_event = inbox.record(123, 1)
        first = inbox.claim(instruction_root="/opt/synthetic", instruction_revision="a" * 40)
        initial = inbox.prepare_peer_thread(**guard(first))
        inbox.bind_peer_thread(**guard(first), creation_key=initial["creation_key"], thread_id="old-thread")
        first = inbox.start_intent(**guard(first), thread_id="old-thread")
        first = inbox.bind_turn(**guard(first), turn_id="old-turn")
        first = inbox.result(
            **guard(first),
            outcome="no_reply_needed",
            event_ids=[first_event],
            receipt_keys=[],
            reason_code="not_actionable",
        )
        inbox.finish_execution(
            **guard(first),
            proof_kind="terminal",
            thread_id="old-thread",
            turn_id="old-turn",
            terminal_status="completed",
            local_tasks_done=True,
        )
        second_event = inbox.record(123, 2)
        job = inbox.claim(instruction_root="/opt/synthetic", instruction_revision="a" * 40)
        assert job["event_ids"] == [second_event]
        mapping = inbox.read_peer_thread(job["job_id"])
        yield inbox, first, job, mapping


def rotate(inbox, job, mapping, **overrides):
    return inbox.rotate_peer_thread(
        **(
            guard(job)
            | {"expected_thread_id": mapping["thread_id"], "expected_peer_thread_revision": mapping["revision"]}
            | overrides
        )
    )


def test_rotation_reserves_one_new_thread_without_changing_jobs(case):
    inbox, first, job, mapping = case
    before_first = inbox.read_job(first["job_id"])
    before_job = inbox.read_job(job["job_id"])
    rotated = rotate(inbox, job, mapping)
    assert rotated["state"] == "creating" and rotated["thread_id"] is None and rotated["created"] is True
    assert rotated["revision"] == mapping["revision"] + 1
    assert rotated["creation_key"] != mapping["creation_key"]
    assert inbox.read_job(first["job_id"]) == before_first
    assert inbox.read_job(job["job_id"]) == before_job
    assert inbox.execution_slot()["job_id"] == job["job_id"]
    bound = inbox.bind_peer_thread(**guard(job), creation_key=rotated["creation_key"], thread_id="new-thread")
    assert bound["state"] == "ready" and bound["thread_id"] == "new-thread"
    assert inbox.start_intent(**guard(job), thread_id="new-thread")["state"] == "start_intent"


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_thread_id": "other-thread"},
        {"expected_peer_thread_revision": 99},
        {"lease": "wrong-lease"},
        {"expected_revision": 99},
    ],
)
def test_rotation_rejects_stale_binding_or_authority_without_changes(case, changes):
    inbox, _, job, mapping = case
    before = tuple(inbox._db.iterdump())
    with pytest.raises(InboxError):
        rotate(inbox, job, mapping, **changes)
    assert tuple(inbox._db.iterdump()) == before


def test_unknown_outcome_and_client_pause_prevent_rotation(case):
    inbox, first, job, mapping = case
    with inbox._write():
        inbox._db.execute(
            "INSERT INTO effects(effect_key,job_id,kind,context_revision,effect_slot,peer_id,reply_to_message_id,"
            "attempted,verified,created_at) VALUES ('unknown',?,'result',1,1,123,1,1,0,?)",
            (first["job_id"], inbox._now()),
        )
    with pytest.raises(InboxError, match="inbox_effect_outcome_unknown"):
        rotate(inbox, job, mapping)
    assert inbox.read_peer_thread(job["job_id"]) == mapping
    with inbox._write():
        inbox._db.execute("DELETE FROM effects WHERE effect_key='unknown'")
    inbox.set_client_control(123, action="pause", expected_revision=0)
    with pytest.raises(InboxError, match="inbox_client_paused"):
        rotate(inbox, job, mapping)
    assert inbox.read_peer_thread(job["job_id"]) == mapping


def test_restarted_reservation_is_held_instead_of_creating_a_second_thread(case):
    inbox, _, job, mapping = case
    rotated = rotate(inbox, job, mapping)
    inbox.recover_on_startup()
    recovered = inbox.read_peer_thread(job["job_id"])
    assert recovered["state"] == "creation_unknown" and recovered["creation_key"] == rotated["creation_key"]
    assert inbox.read_job(job["job_id"])["state"] == "needs_review"


def test_bridge_internal_rotation_checks_native_ledger(case, monkeypatch):
    inbox, _, job, mapping = case
    changed = []
    monitor = SimpleNamespace(inbox=inbox, config=None, changed=lambda: changed.append(True))
    request = {
        "operation": "job_peer_thread_rotate",
        **guard(job),
        "expected_thread_id": "old-thread",
        "expected_peer_thread_revision": mapping["revision"],
    }
    monkeypatch.setattr(
        bridge,
        "_require_native_outcome_known",
        lambda *_: (_ for _ in ()).throw(bridge.BridgeError("send_outcome_uncertain")),
    )
    with pytest.raises(bridge.BridgeError, match="send_outcome_uncertain"):
        bridge._job_execution_control(request, monitor)
    assert inbox.read_peer_thread(job["job_id"]) == mapping and changed == []
    monkeypatch.setattr(bridge, "_require_native_outcome_known", lambda *_: None)
    result = bridge._job_execution_control(request, monitor)
    assert result["ok"] and result["peer_thread"]["state"] == "creating" and changed == [True]
