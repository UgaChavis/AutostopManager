from datetime import UTC, datetime, timedelta

import pytest

from autostop_manager.telegram_inbox import InboxError, TelegramInbox
from autostop_manager import telegram_inbox as inbox_module


ROOT = "/opt/test-runtime"
REVISION = "a" * 40


def test_older_schema_reader_refuses_new_routing_state(tmp_path, monkeypatch):
    path = tmp_path / "inbox" / "events.sqlite3"
    with TelegramInbox(path) as inbox:
        assert inbox.status()["schema_version"] == 9
    monkeypatch.setattr(inbox_module, "SUPPORTED_INBOX_SCHEMA_VERSIONS", tuple(range(1, 9)))
    with pytest.raises(InboxError, match="inbox_open_failed"):
        TelegramInbox(path)


@pytest.fixture
def queue(tmp_path):
    with TelegramInbox(tmp_path / "inbox" / "events.sqlite3") as inbox:
        inbox.duty_set(True)
        yield inbox


def claim(inbox):
    return inbox.claim(instruction_root=ROOT, instruction_revision=REVISION)


def test_nonclients_are_durable_monitorable_and_hidden_from_client_access(tmp_path):
    path = tmp_path / "inbox" / "events.sqlite3"
    with TelegramInbox(path) as inbox:
        inbox.duty_set(True)
        bot = inbox.record(1, 1, source_kind="bot")
        unknown = inbox.record(1, 2, source_kind="unknown")
        client = inbox.record(1, 3)
        assert [row["state"] for row in inbox.events()] == ["bot_pending", "identity_pending", "open"]
        status = inbox.status()
        assert (status["bot_pending_events"], status["identity_pending_events"], status["unfinished_events"]) == (
            1,
            1,
            3,
        )
        assert inbox.resolve_identity_pending(unknown).message_id == 2
        assert inbox.resolve_bot_notification(bot).message_id == 1
        with pytest.raises(InboxError, match="inbound_identity_not_pending"):
            inbox.resolve_identity_pending(bot)
        for event in (unknown, client):
            with pytest.raises(InboxError, match="inbound_event_not_bot_pending"):
                inbox.resolve_bot_notification(event)
        assert [row["event_id"] for row in inbox.context(client)] == [client]
        for event in (bot, unknown):
            with pytest.raises(InboxError, match="inbound_event_not_client"):
                inbox.resolve(event)
            with pytest.raises(InboxError, match="inbound_event_not_client"):
                inbox.context(event)
            with pytest.raises(InboxError):
                inbox.require_open(event)
        assert claim(inbox)["event_ids"] == [client]
    with TelegramInbox(path) as restored:
        assert restored.event_state(bot) == "bot_pending"
        assert restored.event_state(unknown) == "identity_pending"
        assert restored.status()["unfinished_events"] == 3
        assert [row["event_id"] for row in restored.events(limit=1, after_event_id=bot)] == [unknown]


def test_acknowledge_bot_is_transactional_idempotent_and_keeps_identity(queue):
    bot = queue.record(1, 1, source_kind="bot")
    unknown = queue.record(2, 1, source_kind="unknown")
    client = queue.record(3, 1)
    for event in (unknown, client):
        with pytest.raises(InboxError, match="inbound_event_not_bot"):
            queue.acknowledge_bot_notification(event)
    queue._db.execute("CREATE TRIGGER fail_ack BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'test'); END")
    generation = queue.generation
    with pytest.raises(InboxError, match="inbox_storage_failed"):
        queue.acknowledge_bot_notification(bot)
    assert queue.event_state(bot) == "bot_pending" and queue.generation == generation
    queue._db.execute("DROP TRIGGER fail_ack")
    assert queue.acknowledge_bot_notification(bot) is None
    row = queue._event_row(bot)
    assert row["state"] == "no_reply_needed" and row["completed_at"] is not None and row["job_id"] is None
    generation = queue.generation
    assert queue.acknowledge_bot_notification(bot) is None
    assert queue.generation == generation and queue._event_row(bot)["completed_at"] == row["completed_at"]
    with pytest.raises(InboxError, match="inbound_event_not_bot_pending"):
        queue.resolve_bot_notification(bot)
    with pytest.raises(InboxError, match="inbound_source_kind_conflict"):
        queue.record(1, 1, source_kind="client")
    with pytest.raises(InboxError, match="inbound_event_not_client"):
        queue.resolve(bot)


def test_unknown_reclassification_after_checkpoint_restart_and_duty_change(tmp_path):
    now = [datetime(2026, 11, 12, 12, 0, 0, 500000, tzinfo=UTC)]

    def clock():
        return now[0]

    path = tmp_path / "inbox" / "events.sqlite3"
    original = now[0].replace(microsecond=0).isoformat()
    with TelegramInbox(path, clock=clock) as inbox:
        inbox.duty_set(True)
        event = inbox.record(1, 10, original, timestamp_precision="second", source_kind="unknown")
        inbox.checkpoint(1, 100)
        inbox.duty_set(False)
    now[0] += timedelta(days=1)
    with TelegramInbox(path, clock=clock) as restored:
        assert restored.record(1, 10, now[0].isoformat(), source_kind="client") == event
        assert restored.resolve(event).received_at == "2026-11-12T12:00:00.000000+00:00"
        assert restored.event_state(event) == "open"
        assert restored.status()["needs_review_events"] == 1
        assert restored.gaps()[0]["reason_code"] == "boundary_instruction_unavailable"
        restored.duty_set(True)
        assert claim(restored) is None
        held = restored.active_jobs()[0]
        assert held["event_ids"] == [event] and held["reason_code"] == "duty_boundary_ambiguous"


def test_unknown_can_become_bot_once_and_never_client(queue):
    event = queue.record(1, 1, source_kind="unknown")
    queue.checkpoint(1, 10)
    assert queue.record(1, 1, source_kind="bot") == event
    assert queue.event_state(event) == "bot_pending"
    generation = queue.generation
    assert queue.record(1, 1, source_kind="unknown") == event
    assert queue.generation == generation and claim(queue) is None
    with pytest.raises(InboxError, match="inbound_source_kind_conflict"):
        queue.record(1, 1)


def test_acknowledged_bot_remains_protected_after_restart(tmp_path):
    path = tmp_path / "inbox" / "events.sqlite3"
    with TelegramInbox(path) as inbox:
        inbox.duty_set(True)
        event = inbox.record(1, 1, source_kind="bot")
        inbox.acknowledge_bot_notification(event)
    with TelegramInbox(path) as restored:
        assert restored.event_state(event) == "no_reply_needed"
        assert restored.acknowledge_bot_notification(event) is None
        with pytest.raises(InboxError, match="inbound_source_kind_conflict"):
            restored.record(1, 1)
        with pytest.raises(InboxError, match="inbound_event_not_client"):
            restored.resolve(event)


def test_capacity_includes_every_unfinished_event_and_import(queue):
    queue._max_open_events = 2
    bot = queue.record(1, 1, source_kind="bot")
    queue.record(2, 1, source_kind="unknown")
    with pytest.raises(InboxError, match="inbound_monitor_full"):
        queue.record(3, 1)
    with pytest.raises(InboxError, match="inbound_monitor_full"):
        queue.import_event(
            "inbound-100", 4, 1, datetime.now(UTC).isoformat(), instruction_root=ROOT, instruction_revision=REVISION
        )
    queue.acknowledge_bot_notification(bot)
    assert queue.record(3, 1)
    assert queue.status()["unfinished_events"] == 2


def test_client_reclassification_respects_existing_peer_hold(queue):
    first = queue.record(1, 1)
    running = claim(queue)
    assert running["event_ids"] == [first]
    unknown = queue.record(1, 2, source_kind="unknown")
    assert queue.record(1, 2, source_kind="client") == unknown
    assert claim(queue) is None
    assert queue._event_row(unknown)["job_id"] is None


@pytest.mark.parametrize("kind", ["bot", "unknown"])
def test_new_nonclient_input_invalidates_active_peer_context(queue, kind):
    first = queue.record(1, 1)
    job = claim(queue)
    guard = {"job_id": job["job_id"], "lease": job["lease"], "expected_revision": job["revision"]}
    mapping = queue.prepare_peer_thread(**guard)
    queue.bind_peer_thread(**guard, creation_key=mapping["creation_key"], thread_id="thread-1")
    job = queue.start_intent(**guard, thread_id="thread-1")
    guard["expected_revision"] = job["revision"]
    job = queue.bind_turn(**guard, turn_id="turn-1")
    guard["expected_revision"] = job["revision"]
    authority = queue.execution_authority(**guard)
    queue.require_current_context(job["job_id"])
    event = queue.record(1, 2, source_kind=kind)
    with pytest.raises(InboxError, match="inbox_context_stale"):
        queue.require_current_context(job["job_id"])
    with pytest.raises(InboxError, match="inbox_context_stale"):
        queue.check_execution_authority(authority)
    assert queue.read_job(job["job_id"])["event_ids"] == [first]
    assert queue._event_row(event)["job_id"] is None
    revision = queue._peer_revision(1)
    assert queue.record(1, 2, source_kind=kind) == event
    assert queue._peer_revision(1) == revision


def test_client_reclassification_keeps_owner_boundary_hold(queue):
    queue.configure_instructions(ROOT, REVISION)
    event = queue.record(1, 1, source_kind="unknown")
    queue.configure_owner_route(1)
    assert queue.record(1, 1) == event
    held = queue.active_jobs()[0]
    assert held["event_ids"] == [event] and held["reason_code"] == "owner_role_transition_review_required"
    assert claim(queue) is None


def test_source_validation_and_duty_exclusion(queue):
    for invalid in (None, "Bot", "", 1, []):
        with pytest.raises(InboxError, match="inbound_source_kind_invalid"):
            queue.record(1, 1, source_kind=invalid)
    old = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    for kind in ("client", "bot", "unknown"):
        assert queue.record(1, 1, old, source_kind=kind) is None
    client = queue.record(1, 1)
    with pytest.raises(InboxError, match="inbound_source_kind_conflict"):
        queue.record(1, 1, source_kind="bot")
    assert queue.record(1, 1, source_kind="unknown") == client


def test_schema_eight_bot_reclassification_preserves_review_history(tmp_path):
    path = tmp_path / "inbox" / "events.sqlite3"
    with TelegramInbox(path) as inbox:
        inbox.duty_set(True)
        held = inbox.record(1, 1)
        pending = inbox.record(1, 2)
        client = inbox.record(2, 1)
        with inbox._write():
            inbox._hold_refs([held.removeprefix("inbound-")], 1, ROOT, REVISION, reason_code="previous_review")
        original_job = inbox.active_jobs()[0]
        with inbox._db:
            inbox._db.execute("UPDATE metadata SET value='8' WHERE key='schema_version'")
            inbox._db.execute("ALTER TABLE events DROP COLUMN sender_kind_verified")

    with TelegramInbox(path) as inbox:
        assert inbox.record(1, 1, source_kind="bot") == held
        assert inbox.record(1, 2, source_kind="bot") == pending
        assert inbox.event_state(held) == "open"
        assert inbox.event_state(pending) == "bot_pending"
        assert inbox.read_job(original_job["job_id"]) == original_job
        assert inbox.status()["needs_review_jobs"] == 1
        assert claim(inbox)["event_ids"] == [client]
        with pytest.raises(InboxError, match="inbound_event_not_client"):
            inbox.resolve(held)
        generation = inbox.generation
        assert inbox.record(1, 1, source_kind="bot") == held
        assert inbox.generation == generation

    with TelegramInbox(path) as inbox:
        assert inbox.record(1, 1, source_kind="bot") == held
        assert inbox.read_job(original_job["job_id"]) == original_job
        # A later recovery may detach an old event from its holder. Its
        # verified bot classification still excludes it from client claims.
        with inbox._db:
            inbox._db.execute("UPDATE events SET job_id=NULL WHERE peer_id=1")
            inbox._db.execute("UPDATE jobs SET state='retired' WHERE peer_id=1")
        assert claim(inbox) is None


@pytest.mark.parametrize("schema", [8, 9])
def test_verified_historical_clients_keep_source_conflict_guard(tmp_path, schema):
    path = tmp_path / "inbox" / "events.sqlite3"
    with TelegramInbox(path) as inbox:
        inbox.duty_set(True)
        event = inbox.record(1, 1)
        with inbox._db:
            inbox._db.execute("UPDATE metadata SET value=? WHERE key='schema_version'", (str(schema),))
            inbox._db.execute("ALTER TABLE events DROP COLUMN sender_kind_verified")
    with TelegramInbox(path) as inbox:
        if schema == 8:
            assert inbox.record(1, 1, source_kind="client") == event
        with pytest.raises(InboxError, match="inbound_source_kind_conflict"):
            inbox.record(1, 1, source_kind="bot")
