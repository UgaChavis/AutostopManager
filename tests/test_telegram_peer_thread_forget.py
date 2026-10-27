"""Jobless maintenance forget uses only private, synthetic SQLite ledgers."""

from __future__ import annotations

import struct
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_inbox import InboxError, TelegramInbox


def pause(inbox, value=True):
    inbox.set_execution_paused(paused=value, expected_revision=inbox.status()["execution_revision"])


def guard(job):
    return {"job_id": job["job_id"], "lease": job["lease"], "expected_revision": job["revision"]}


@pytest.fixture
def idle(tmp_path):
    with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
        inbox.duty_set(True)
        # No job/event is manufactured to authorize the operation under test.
        with inbox._write():
            for peer, thread, key in ((123, "old-thread", "old-key"), (456, "other-thread", "other-key")):
                inbox._db.execute("INSERT INTO peers(peer_id) VALUES (?)", (peer,))
                inbox._db.execute("INSERT INTO peer_threads VALUES (?,?,?,'ready',3)", (peer, thread, key))
        pause(inbox)
        yield inbox


def arguments(inbox, **overrides):
    values = {
        "peer_id": 123,
        "expected_thread_id": "old-thread",
        "expected_creation_key": "old-key",
        "expected_peer_thread_revision": 3,
        "expected_execution_revision": inbox.status()["execution_revision"],
        "missing_thread_reviewed": True,
        "effects_reviewed": True,
    }
    return values | overrides


def forget(inbox, **overrides):
    return inbox.forget_missing_peer_thread(**arguments(inbox, **overrides))


def state(inbox):
    return tuple(inbox._db.iterdump())


def held_job(inbox, peer=123):
    pause(inbox, False)
    event = inbox.record(peer, 11)
    job = inbox.claim(instruction_root="/opt/synthetic-runtime", instruction_revision="a" * 40)
    job = inbox.finish_execution(**guard(job), proof_kind="not_started", local_tasks_done=True)
    pause(inbox)
    return job, event


def test_forget_needs_no_job_and_preserves_unrelated_ledger(idle):
    before = idle.status()
    other = dict(idle._db.execute("SELECT * FROM peer_threads WHERE peer_id=456").fetchone())
    schema = tuple(idle._db.execute("SELECT name,sql FROM sqlite_master ORDER BY name"))
    assert forget(idle) is True
    assert idle.read_peer_thread_by_peer(123) is None
    assert idle.read_peer_thread_by_peer(456) == other
    assert idle.status() | {"generation": before["generation"]} == before
    assert tuple(idle._db.execute("SELECT name,sql FROM sqlite_master ORDER BY name")) == schema
    for table in ("jobs", "events", "effects"):
        assert idle._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_next_real_input_creates_fresh_binding_only_when_claimed(idle):
    assert forget(idle) is True
    pause(idle, False)
    event = idle.record(123, 12)
    assert idle.read_peer_thread_by_peer(123) is None
    job = idle.claim(instruction_root="/opt/synthetic-runtime", instruction_revision="b" * 40)
    assert job["event_ids"] == [event]
    mapping = idle.prepare_peer_thread(**guard(job))
    assert mapping["state"] == "creating" and mapping["thread_id"] is None
    assert mapping["creation_key"] != "old-key"


@pytest.mark.parametrize("peer", [123, 456])
def test_any_unattached_open_event_blocks_forget_and_is_preserved(idle, peer):
    event = idle.record(peer, 12)
    before = state(idle)
    with pytest.raises(InboxError, match=r"^inbox_peer_thread_forget_held$"):
        forget(idle)
    assert state(idle) == before
    assert idle.event_state(event) == "open"


def test_bound_open_event_blocks_forget(idle):
    job, event = held_job(idle)
    before = state(idle)
    with pytest.raises(InboxError, match=r"^inbox_peer_thread_forget_held$"):
        forget(idle)
    assert state(idle) == before and idle.read_job(job["job_id"])["event_ids"] == [event]


@pytest.mark.parametrize("job_state", ["claimed", "start_intent", "running", "needs_review"])
def test_any_active_job_blocks_even_without_open_event_or_effect(idle, job_state):
    job, _event = held_job(idle, peer=456)
    # Deliberately incomplete private ledgers must also fail closed.
    with idle._write():
        idle._db.execute("UPDATE events SET state='no_reply_needed'")
        idle._db.execute("UPDATE jobs SET state=? WHERE job_id=?", (job_state, job["job_id"]))
    assert idle.execution_slot() is None and idle.read_job(job["job_id"])["effects"] == []
    before = state(idle)
    with pytest.raises(InboxError, match=r"^inbox_peer_thread_forget_held$"):
        forget(idle)
    assert state(idle) == before


def test_other_global_slot_blocks_forget(idle):
    pause(idle, False)
    idle.record(456, 12)
    job = idle.claim(instruction_root="/opt/synthetic-runtime", instruction_revision="a" * 40)
    pause(idle)
    before = state(idle)
    with pytest.raises(InboxError, match=r"^inbox_peer_thread_forget_held$"):
        forget(idle)
    assert state(idle) == before and idle.execution_slot()["job_id"] == job["job_id"]


@pytest.mark.parametrize("verified", [False, True])
def test_unknown_effect_blocks_but_verified_history_is_preserved(idle, verified):
    job, event = held_job(idle, peer=456)
    idle.result(
        **guard(job), outcome="no_reply_needed", event_ids=[event], receipt_keys=[], reason_code="not_actionable"
    )
    with idle._write():
        idle._db.execute(
            "INSERT INTO effects(effect_key,job_id,kind,context_revision,effect_slot,peer_id,reply_to_message_id,"
            "attempted,verified,created_at) VALUES ('synthetic-effect',?,'result',1,1,456,11,1,?,?)",
            (job["job_id"], int(verified), idle._now()),
        )
    before_job = idle.read_job(job["job_id"])
    before = state(idle)
    if verified:
        assert forget(idle) is True
    else:
        with pytest.raises(InboxError, match=r"^inbox_peer_thread_forget_held$"):
            forget(idle)
        assert state(idle) == before
    assert idle.read_job(job["job_id"]) == before_job
    assert idle.resolve(event).peer_id == 456


@pytest.mark.parametrize("peer", [True, "123", 0, -1, 2**63])
def test_untrusted_peer_shapes_are_rejected_before_mutation(idle, peer):
    before = state(idle)
    with pytest.raises(InboxError, match=r"^inbound_peer_invalid$"):
        forget(idle, peer_id=peer)
    with pytest.raises(InboxError, match=r"^inbound_peer_invalid$"):
        idle.read_peer_thread_by_peer(peer)
    assert state(idle) == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"peer_id": 456},
        {"peer_id": 789},
        {"expected_thread_id": "foreign-thread"},
        {"expected_creation_key": "different-key"},
        {"expected_peer_thread_revision": 4},
    ],
)
def test_exact_old_binding_cas_rejects_foreign_or_stale_inputs(idle, overrides):
    before = state(idle)
    with pytest.raises(InboxError, match=r"^inbox_peer_thread_conflict$"):
        forget(idle, **overrides)
    assert state(idle) == before


@pytest.mark.parametrize(
    "overrides", [{"missing_thread_reviewed": False}, {"effects_reviewed": False}, {"effects_reviewed": 1}]
)
def test_explicit_reviews_are_required(idle, overrides):
    before = state(idle)
    with pytest.raises(InboxError, match=r"^inbox_peer_thread_forget_review_required$"):
        forget(idle, **overrides)
    assert state(idle) == before


def test_pause_and_fresh_execution_revision_are_required(idle):
    stale = arguments(idle)
    pause(idle, False)
    before = state(idle)
    with pytest.raises(InboxError, match=r"^inbox_peer_thread_forget_held$"):
        forget(idle)
    assert state(idle) == before
    pause(idle)
    before = state(idle)
    with pytest.raises(InboxError, match=r"^inbox_execution_conflict$"):
        idle.forget_missing_peer_thread(**stale)
    assert state(idle) == before


def test_lost_response_and_replay_with_another_key_cannot_remove_future_binding(idle):
    old = arguments(idle)
    assert idle.forget_missing_peer_thread(**old) is True
    assert idle.read_peer_thread_by_peer(123) is None  # Exact readback after a lost response.
    for key in ("old-key", "another-key"):
        before = state(idle)
        with pytest.raises(InboxError, match=r"^inbox_peer_thread_conflict$"):
            idle.forget_missing_peer_thread(**(old | {"expected_creation_key": key}))
        assert state(idle) == before
    with idle._write():
        idle._db.execute("INSERT INTO peer_threads VALUES (123,'new-thread','fresh-key','ready',3)")
    before = state(idle)
    for key in ("old-key", "another-key"):
        with pytest.raises(InboxError, match=r"^inbox_peer_thread_conflict$"):
            idle.forget_missing_peer_thread(**(old | {"expected_creation_key": key}))
        assert state(idle) == before


def test_cli_and_root_only_control_use_exact_peer_and_cas_without_job(idle):
    argv = ["--account", "work", "job-peer-thread-forget-missing", "--peer-id", "123"]
    for field, value in arguments(idle).items():
        if field == "peer_id":
            continue
        argv.append("--" + field.replace("_", "-"))
        if value is not True:
            argv.append(str(value))
    request = bridge._job_cli_request(bridge.build_parser().parse_args(argv))
    assert request == {"operation": "job_peer_thread_forget_missing", **arguments(idle)}
    changes = []
    monitor = SimpleNamespace(inbox=idle, changed=lambda: changes.append(True))
    assert bridge._job_execution_control(request, monitor) == {"ok": True, "forgotten": True}
    read = bridge._job_cli_request(
        bridge.build_parser().parse_args(["--account", "work", "job-peer-thread-read-peer", "--peer-id", "123"])
    )
    assert bridge._job_execution_control(read, monitor) == {"ok": True, "peer_thread": None}
    assert changes == [True]
    # Both operations use the existing job_* root-only RPC boundary.
    socket = SimpleNamespace(getsockopt=lambda *_args: struct.pack("3i", 1, 1000, 1000))
    writer = SimpleNamespace(get_extra_info=lambda _name: socket)
    with pytest.raises(bridge.BridgeError, match=r"^job_control_permission_denied$"):
        bridge._require_root_client(writer)
