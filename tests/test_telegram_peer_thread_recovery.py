"""Explicit recovery of a missing peer uses only private synthetic inboxes."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_inbox import InboxError, TelegramInbox


def guard(job):
    return {"job_id": job["job_id"], "lease": job["lease"], "expected_revision": job["revision"]}


def pause(inbox, value=True):
    inbox.set_execution_paused(paused=value, expected_revision=inbox.status()["execution_revision"])


@pytest.fixture
def held(tmp_path):
    with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
        inbox.duty_set(True)
        event = inbox.record(123, 11)
        job = inbox.claim(instruction_root="/opt/synthetic-runtime", instruction_revision="a" * 40)
        mapping = inbox.prepare_peer_thread(**guard(job))
        mapping = inbox.bind_peer_thread(**guard(job), creation_key=mapping["creation_key"], thread_id="old-thread")
        job = inbox.finish_execution(**guard(job), proof_kind="not_started", local_tasks_done=True)
        pause(inbox)
        yield SimpleNamespace(inbox=inbox, job=job, mapping=mapping, event=event)


@pytest.fixture
def unmaterialized(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    with TelegramInbox(path) as inbox:
        inbox.duty_set(True)
        event = inbox.record(123, 11)
        job = inbox.claim(instruction_root="/opt/synthetic-runtime", instruction_revision="a" * 40)
        mapping = inbox.prepare_peer_thread(**guard(job))
        inbox.bind_peer_thread(
            **guard(job), creation_key=mapping["creation_key"], thread_id="old-thread", materialized=False
        )
    with TelegramInbox(path) as inbox:
        inbox.recover_on_startup()
        job = inbox.read_job(job["job_id"])
        mapping = inbox.read_peer_thread(job["job_id"])
        assert mapping["state"] == "creation_unknown" and mapping["thread_id"] == "old-thread"
        assert job["state"] == "needs_review" and job["execution_proof"] == "not_started"
        pause(inbox)
        yield SimpleNamespace(inbox=inbox, job=job, mapping=mapping, event=event)


def replacement(case, **overrides):
    arguments = {
        **guard(case.job),
        "expected_thread_id": "old-thread",
        "expected_peer_thread_revision": case.mapping["revision"],
        "thread_id": "replacement-thread",
        "missing_thread_reviewed": True,
        "effects_reviewed": True,
    }
    arguments.update(overrides)
    return case.inbox.replace_missing_peer_thread(**arguments)


def state(inbox):
    return tuple(inbox._db.iterdump())


def test_missing_peer_can_be_rebound_without_rewriting_job_or_events(held):
    before_job = held.inbox.read_job(held.job["job_id"])
    before_event = held.inbox.resolve(held.event)
    before_status = held.inbox.status()
    changed = replacement(held)
    assert changed["peer_id"] == held.mapping["peer_id"]
    assert changed["thread_id"] == "replacement-thread"
    assert changed["state"] == "ready"
    assert changed["revision"] == held.mapping["revision"] + 1
    assert changed["creation_key"] != held.mapping["creation_key"]
    assert held.inbox.read_job(held.job["job_id"]) == before_job
    assert held.inbox.resolve(held.event) == before_event
    assert held.inbox.execution_slot() is None
    assert held.inbox.status()["execution_paused"] is True
    assert held.inbox.status()["execution_revision"] == before_status["execution_revision"]
    assert held.inbox.read_peer_thread(held.job["job_id"]) == changed


def test_replacement_requires_separate_retry_before_any_turn(held):
    replacement(held)
    assert held.inbox.claim(instruction_root="/opt/synthetic-runtime", instruction_revision="b" * 40) is None
    released = held.inbox.resolve_job(**guard(held.job), decision="retry_no_effects", effects_reviewed=True)
    assert released["state"] == "released"
    pause(held.inbox, False)
    next_job = held.inbox.claim(instruction_root="/opt/synthetic-runtime", instruction_revision="b" * 40)
    assert next_job["event_ids"] == [held.event]
    assert held.inbox.resolve(held.event).message_id == 11
    assert held.inbox.read_peer_thread(next_job["job_id"])["thread_id"] == "replacement-thread"
    started = held.inbox.start_intent(**guard(next_job), thread_id="replacement-thread")
    running = held.inbox.bind_turn(**guard(started), turn_id="new-turn")
    assert running["state"] == "running" and running["effects"] == []
    assert held.inbox.read_job(held.job["job_id"])["instruction_revision"] == "a" * 40
    assert running["instruction_revision"] == "b" * 40


def test_missing_unmaterialized_peer_recovers_after_restart_without_unblocking_early(unmaterialized):
    case = unmaterialized
    before_job = case.inbox.read_job(case.job["job_id"])
    before_event = case.inbox.resolve(case.event)
    changed = replacement(case, materialized=True)
    assert changed["state"] == "ready" and changed["thread_id"] == "replacement-thread"
    assert changed["revision"] == case.mapping["revision"] + 1
    assert changed["creation_key"] != case.mapping["creation_key"]
    assert case.inbox.read_job(case.job["job_id"]) == before_job
    assert case.inbox.resolve(case.event) == before_event
    assert case.inbox.execution_slot() is None
    assert case.inbox.claim(instruction_root="/opt/synthetic-runtime", instruction_revision="b" * 40) is None
    path = case.inbox.path
    case.inbox.close()
    with TelegramInbox(path) as reopened:
        reopened.recover_on_startup()
        assert reopened.read_peer_thread(case.job["job_id"]) == changed
        assert reopened.status()["execution_paused"] is True
        assert reopened.read_job(case.job["job_id"]) == before_job
        reopened.resolve_job(**guard(case.job), decision="retry_no_effects", effects_reviewed=True)
        pause(reopened, False)
        next_job = reopened.claim(instruction_root="/opt/synthetic-runtime", instruction_revision="b" * 40)
        assert next_job["event_ids"] == [case.event]
        assert reopened.read_peer_thread(next_job["job_id"]) == changed
        started = reopened.start_intent(**guard(next_job), thread_id="replacement-thread")
        assert reopened.bind_turn(**guard(started), turn_id="new-turn")["state"] == "running"


@pytest.mark.parametrize(
    "materialized,code",
    [
        (False, "inbox_peer_thread_conflict"),
        (1, "inbox_peer_thread_state_invalid"),
        ("true", "inbox_peer_thread_state_invalid"),
    ],
)
def test_unknown_binding_requires_explicit_replacement_materialization(unmaterialized, materialized, code):
    before = state(unmaterialized.inbox)
    with pytest.raises(InboxError, match=f"^{code}$"):
        replacement(unmaterialized, materialized=materialized)
    assert state(unmaterialized.inbox) == before


def test_normal_bind_cannot_replace_unmaterialized_peer(unmaterialized):
    before = state(unmaterialized.inbox)
    with pytest.raises(InboxError, match=r"^inbox_peer_thread_conflict$"):
        unmaterialized.inbox.bind_peer_thread(
            **guard(unmaterialized.job),
            creation_key=unmaterialized.mapping["creation_key"],
            thread_id="replacement-thread",
            effects_reviewed=True,
        )
    assert state(unmaterialized.inbox) == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_thread_id": "foreign-thread"},
        {"expected_peer_thread_revision": 99},
        {"thread_id": "old-thread"},
    ],
)
def test_unknown_replacement_preserves_exact_cas(unmaterialized, overrides):
    before = state(unmaterialized.inbox)
    with pytest.raises(InboxError, match=r"^inbox_peer_thread_conflict$"):
        replacement(unmaterialized, materialized=True, **overrides)
    assert state(unmaterialized.inbox) == before


def test_creation_in_progress_cannot_be_replaced(unmaterialized):
    with unmaterialized.inbox._db:
        unmaterialized.inbox._db.execute("UPDATE peer_threads SET state='creating'")
    before = state(unmaterialized.inbox)
    with pytest.raises(InboxError, match=r"^inbox_peer_thread_conflict$"):
        replacement(unmaterialized, materialized=True)
    assert state(unmaterialized.inbox) == before


@pytest.mark.parametrize("changed_guard", ["pause", "proof", "effect", "slot"])
def test_unknown_replacement_does_not_bypass_execution_guards(unmaterialized, changed_guard):
    case = unmaterialized
    if changed_guard == "pause":
        pause(case.inbox, False)
    elif changed_guard == "proof":
        case.inbox._db.execute("UPDATE jobs SET execution_proof='failed' WHERE job_id=?", (case.job["job_id"],))
    elif changed_guard == "effect":
        case.inbox._db.execute(
            "INSERT INTO effects(effect_key,job_id,kind,context_revision,effect_slot,peer_id,reply_to_message_id,"
            "attempted,verified,created_at) VALUES ('synthetic-effect',?,'result',1,1,123,11,0,0,?)",
            (case.job["job_id"], case.inbox._now()),
        )
    else:
        pause(case.inbox, False)
        case.inbox.record(456, 22)
        other = case.inbox.claim(instruction_root="/opt/synthetic-runtime", instruction_revision="a" * 40)
        assert other["peer_id"] == 456
        pause(case.inbox)
    before = state(case.inbox)
    with pytest.raises(InboxError, match=r"^inbox_peer_thread_recovery_required$"):
        replacement(case, materialized=True)
    assert state(case.inbox) == before


@pytest.mark.parametrize("review", ["missing_thread_reviewed", "effects_reviewed"])
def test_unknown_replacement_requires_both_explicit_reviews(unmaterialized, review):
    before = state(unmaterialized.inbox)
    with pytest.raises(InboxError, match=r"^inbox_peer_thread_recovery_review_required$"):
        replacement(unmaterialized, materialized=True, **{review: False})
    assert state(unmaterialized.inbox) == before


def test_normal_bind_cannot_replace_ready_peer(held):
    before = state(held.inbox)
    with pytest.raises(InboxError, match=r"^inbox_peer_thread_conflict$"):
        held.inbox.bind_peer_thread(
            **guard(held.job),
            creation_key=held.mapping["creation_key"],
            thread_id="replacement-thread",
            effects_reviewed=True,
        )
    assert state(held.inbox) == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"missing_thread_reviewed": False},
        {"effects_reviewed": False},
        {"missing_thread_reviewed": 1},
        {"effects_reviewed": 1},
    ],
)
def test_explicit_reviews_are_required(held, overrides):
    before = state(held.inbox)
    with pytest.raises(InboxError, match=r"^inbox_peer_thread_recovery_review_required$"):
        replacement(held, **overrides)
    assert state(held.inbox) == before


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"expected_thread_id": "foreign-thread"}, "inbox_peer_thread_conflict"),
        ({"expected_peer_thread_revision": 99}, "inbox_peer_thread_conflict"),
        ({"expected_revision": 99}, "inbox_revision_conflict"),
        ({"lease": "wrong-lease"}, "inbox_lease_mismatch"),
        ({"thread_id": "old-thread"}, "inbox_peer_thread_conflict"),
    ],
)
def test_exact_cas_rejects_stale_or_foreign_binding(held, overrides, code):
    before = state(held.inbox)
    with pytest.raises(InboxError, match=f"^{code}$"):
        replacement(held, **overrides)
    assert state(held.inbox) == before


def test_repeated_old_cas_does_not_overwrite_replacement(held):
    changed = replacement(held)
    before = state(held.inbox)
    with pytest.raises(InboxError, match=r"^inbox_peer_thread_conflict$"):
        replacement(held, thread_id="another-thread")
    assert held.inbox.read_peer_thread(held.job["job_id"]) == changed
    assert state(held.inbox) == before


def test_lost_response_uses_preserved_old_binding_and_exact_readback(held):
    old = held.inbox.read_peer_thread(held.job["job_id"])
    prior_job = held.inbox.read_job(held.job["job_id"])
    receipt = held.inbox.path.parent / "recovery-before.json"
    backup = {"job_id": held.job["job_id"], "old_binding": old, "new_thread_id": "replacement-thread"}
    descriptor = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, json.dumps(backup).encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    assert receipt.stat().st_mode & 0o777 == 0o600

    def lost_response():
        replacement(held)
        raise TimeoutError("synthetic response lost after commit")

    with pytest.raises(TimeoutError):
        lost_response()
    readback = held.inbox.read_peer_thread(held.job["job_id"])
    assert readback["thread_id"] == backup["new_thread_id"]
    assert readback["revision"] == old["revision"] + 1
    assert readback["creation_key"] != old["creation_key"]
    assert held.inbox.read_job(held.job["job_id"]) == prior_job
    assert held.inbox.status()["execution_paused"] is True
    assert json.loads(receipt.read_bytes()) == backup
    assert "lease" not in backup
    before = state(held.inbox)
    with pytest.raises(InboxError, match=r"^inbox_peer_thread_conflict$"):
        replacement(held)
    assert state(held.inbox) == before


def test_pause_is_required(held):
    pause(held.inbox, False)
    before = state(held.inbox)
    with pytest.raises(InboxError, match=r"^inbox_peer_thread_recovery_required$"):
        replacement(held)
    assert state(held.inbox) == before


def test_other_claimed_slot_blocks_replacement(held):
    pause(held.inbox, False)
    held.inbox.record(456, 22)
    other = held.inbox.claim(instruction_root="/opt/synthetic-runtime", instruction_revision="a" * 40)
    pause(held.inbox)
    before = state(held.inbox)
    with pytest.raises(InboxError, match=r"^inbox_peer_thread_recovery_required$"):
        replacement(held)
    assert held.inbox.execution_slot()["job_id"] == other["job_id"]
    assert state(held.inbox) == before


def test_thread_already_bound_to_another_peer_is_preserved(held):
    pause(held.inbox, False)
    held.inbox.record(456, 22)
    other = held.inbox.claim(instruction_root="/opt/synthetic-runtime", instruction_revision="a" * 40)
    mapping = held.inbox.prepare_peer_thread(**guard(other))
    held.inbox.bind_peer_thread(**guard(other), creation_key=mapping["creation_key"], thread_id="replacement-thread")
    held.inbox.finish_execution(**guard(other), proof_kind="not_started", local_tasks_done=True)
    pause(held.inbox)
    before = state(held.inbox)
    with pytest.raises(InboxError, match=r"^inbox_peer_thread_conflict$"):
        replacement(held)
    assert state(held.inbox) == before


@pytest.mark.parametrize(
    "column,value",
    [
        ("execution_proof", None),
        ("execution_proof", "failed"),
        ("thread_id", "old-thread"),
        ("turn_id", "old-turn"),
        ("context_mode", "legacy"),
        ("state", "running"),
    ],
)
def test_recovery_does_not_authorize_unknown_or_started_execution(held, column, value):
    # Private inconsistent-ledger controls must also fail closed without repair.
    held.inbox._db.execute(f"UPDATE jobs SET {column}=? WHERE job_id=?", (value, held.job["job_id"]))
    before = state(held.inbox)
    with pytest.raises(InboxError, match=r"^inbox_peer_thread_recovery_required$"):
        replacement(held)
    assert state(held.inbox) == before


@pytest.mark.parametrize("attempted,verified", [(0, 0), (1, 0), (1, 1)])
def test_any_existing_effect_blocks_replacement(held, attempted, verified):
    held.inbox._db.execute(
        "INSERT INTO effects(effect_key,job_id,kind,context_revision,effect_slot,peer_id,reply_to_message_id,"
        "attempted,verified,created_at) VALUES ('synthetic-effect',?,'result',1,1,123,11,?,?,?)",
        (held.job["job_id"], attempted, verified, held.inbox._now()),
    )
    before = state(held.inbox)
    with pytest.raises(InboxError, match=r"^inbox_peer_thread_recovery_required$"):
        replacement(held)
    assert state(held.inbox) == before


def test_replacement_and_event_locators_survive_reopen(held):
    changed = replacement(held)
    path = held.inbox.path
    held.inbox.close()
    with TelegramInbox(path) as reopened:
        assert reopened.read_peer_thread(held.job["job_id"]) == changed
        assert reopened.resolve(held.event).peer_id == 123
        assert reopened.resolve(held.event).message_id == 11
        assert reopened.read_job(held.job["job_id"])["state"] == "needs_review"
        assert reopened.status()["execution_paused"] is True


@pytest.mark.parametrize("materialized", [False, True])
def test_cli_preserves_both_cas_and_review_fields(materialized):
    parser = bridge.build_parser()
    args = parser.parse_args(
        [
            "--account",
            "work",
            "job-peer-thread-replace-missing",
            "--job-id",
            "job-synthetic",
            "--lease",
            "lease-synthetic",
            "--expected-revision",
            "2",
            "--expected-thread-id",
            "old-thread",
            "--expected-peer-thread-revision",
            "2",
            "--thread-id",
            "replacement-thread",
            "--missing-thread-reviewed",
            "--effects-reviewed",
        ]
        + (["--materialized"] if materialized else [])
    )
    request = bridge._job_cli_request(args)
    assert request["operation"] == "job_peer_thread_replace_missing"
    assert request["expected_thread_id"] == "old-thread"
    assert request["expected_peer_thread_revision"] == 2
    assert request["missing_thread_reviewed"] is True and request["effects_reviewed"] is True
    assert request["materialized"] is materialized


def test_control_router_is_local_and_returns_only_the_binding(held):
    changes = []
    monitor = SimpleNamespace(inbox=held.inbox, changed=lambda: changes.append(True))
    request = {
        "operation": "job_peer_thread_replace_missing",
        **guard(held.job),
        "expected_thread_id": "old-thread",
        "expected_peer_thread_revision": held.mapping["revision"],
        "thread_id": "replacement-thread",
        "missing_thread_reviewed": True,
        "effects_reviewed": True,
    }
    response = bridge._job_execution_control(request, monitor)
    assert response["ok"] is True and response["peer_thread"]["thread_id"] == "replacement-thread"
    assert set(response) == {"ok", "peer_thread"} and changes == [True]


def test_control_router_preserves_explicit_materialization_for_unknown_peer(unmaterialized):
    monitor = SimpleNamespace(inbox=unmaterialized.inbox, changed=lambda: None)
    request = {
        "operation": "job_peer_thread_replace_missing",
        **guard(unmaterialized.job),
        "expected_thread_id": "old-thread",
        "expected_peer_thread_revision": unmaterialized.mapping["revision"],
        "thread_id": "replacement-thread",
        "missing_thread_reviewed": True,
        "effects_reviewed": True,
        "materialized": True,
    }
    response = bridge._job_execution_control(request, monitor)
    assert response["ok"] is True
    assert response["peer_thread"]["state"] == "ready"
    assert response["peer_thread"]["thread_id"] == "replacement-thread"
