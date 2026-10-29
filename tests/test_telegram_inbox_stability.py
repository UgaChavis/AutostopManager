"""Durable serial execution and separate client contexts, without transports."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from autostop_manager.telegram_inbox import InboxError, TelegramInbox

ROOT = "/opt/synthetic-instructions"
REVISION = "a" * 40
THREAD = "00000000-0000-4000-8000-000000000001"


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 22, 9, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def inbox(tmp_path: Path, clock: Clock):
    with TelegramInbox(tmp_path / "inbox" / "registry.sqlite3", clock=clock) as queue:
        queue.duty_set(True)
        yield queue


def guard(job):
    return {"job_id": job["job_id"], "lease": job["lease"], "expected_revision": job["revision"]}


def claim(queue):
    job = queue.claim(instruction_root=ROOT, instruction_revision=REVISION)
    assert job is not None
    return job


def running(queue):
    job = claim(queue)
    # The baseline has no context registry. Both setups exercise the real
    # claim/start/bind lifecycle; only the new version requires this binding.
    if hasattr(queue, "prepare_peer_thread"):
        mapping = queue.prepare_peer_thread(**guard(job))
        queue.bind_peer_thread(**guard(job), creation_key=mapping["creation_key"], thread_id=THREAD)
    job = queue.start_intent(**guard(job), thread_id=THREAD)
    return queue.bind_turn(**guard(job), turn_id="owned-turn")


def test_verified_business_result_does_not_release_execution(inbox):
    first = inbox.record(1, 1)
    inbox.record(2, 1)
    job = running(inbox)
    key = inbox.reserve_effect(**guard(job), kind="result", text_sha256="a" * 64, peer_id=1, reply_to_message_id=1)
    inbox.verify_effect(effect_key=key, receipt_key=key, message_id=100)
    done = inbox.result(**guard(job), outcome="answered", event_ids=[first], receipt_keys=[key])
    assert done["state"] == "completed"
    assert inbox.claim(instruction_root=ROOT, instruction_revision=REVISION) is None
    proved = terminal(inbox, done)
    assert proved["state"] == "completed" and inbox.execution_slot() is None
    assert claim(inbox)["peer_id"] == 2


def test_pause_revokes_authority_before_an_effect_is_reserved(inbox):
    inbox.record(1, 1)
    job = running(inbox)
    inbox.set_execution_paused(paused=True, expected_revision=inbox.status()["execution_revision"])
    with pytest.raises(InboxError, match=r"inbox_execution_(paused|stale)"):
        inbox.reserve_effect(**guard(job), kind="result", text_sha256="a" * 64, peer_id=1, reply_to_message_id=1)


def terminal(queue, job, status="completed"):
    return queue.finish_execution(
        **guard(job),
        proof_kind="terminal",
        thread_id=job["thread_id"],
        turn_id=job["turn_id"],
        terminal_status=status,
        local_tasks_done=True,
    )


def pause(queue, value=True):
    queue.set_execution_paused(paused=value, expected_revision=queue.status()["execution_revision"])


@pytest.mark.parametrize("change", ["deadline", "pause_roundtrip", "duty_off", "duty_roundtrip", "refresh"])
def test_awaited_work_loses_its_original_write_authority(inbox, clock, change):
    inbox.record(1, 1)
    job = running(inbox)
    original = inbox.execution_authority(**guard(job))
    if change == "deadline":
        clock.advance(1200)
    elif change == "pause_roundtrip":
        pause(inbox)
        pause(inbox, False)
    elif change == "duty_off":
        inbox.duty_set(False)
    elif change == "duty_roundtrip":
        inbox.duty_set(False)
        inbox.duty_set(True)
    else:
        inbox.refresh_job(**guard(job))
    with pytest.raises(InboxError):
        inbox.check_execution_authority(original)
    assert inbox.execution_slot()["job_id"] == job["job_id"]


def test_claim_deadline_and_exact_binding_survive_reopen(tmp_path, clock):
    path = tmp_path / "inbox" / "state.sqlite3"
    with TelegramInbox(path, clock=clock) as queue:
        queue.duty_set(True)
        queue.record(1, 1)
        job = running(queue)
        deadline = (clock.value + timedelta(seconds=1200)).isoformat(timespec="microseconds")
        assert job["deadline_at"] == deadline
        clock.advance(1199)
        queue.check_execution_authority(queue.execution_authority(**guard(job)))
    with TelegramInbox(path, clock=clock) as queue:
        queue.recover_on_startup()
        restored = queue.read_job(job["job_id"])
        assert restored["deadline_at"] == deadline
        assert restored["thread_id"] == THREAD and restored["turn_id"] == "owned-turn"
        assert queue.execution_slot()["job_id"] == job["job_id"]
        queue.record(2, 1)
        assert queue.claim(instruction_root=ROOT, instruction_revision=REVISION) is None
        restored = terminal(queue, restored, "interrupted")
        assert restored["state"] == "needs_review"
        assert claim(queue)["peer_id"] == 2


def test_late_turn_binding_never_releases_an_unknown_execution(inbox):
    inbox.record(1, 1)
    inbox.record(2, 1)
    job = claim(inbox)
    context = inbox.prepare_peer_thread(**guard(job))
    inbox.bind_peer_thread(**guard(job), creation_key=context["creation_key"], thread_id=THREAD)
    job = inbox.start_intent(**guard(job), thread_id=THREAD)
    job = inbox.needs_review(**guard(job), reason_code="codex_start_outcome_unknown")
    with pytest.raises(InboxError, match="inbox_execution_proof_invalid"):
        inbox.finish_execution(**guard(job), proof_kind="not_started", local_tasks_done=True)
    assert inbox.claim(instruction_root=ROOT, instruction_revision=REVISION) is None
    job = inbox.bind_turn(**guard(job), turn_id="late-exact-turn")
    with pytest.raises(InboxError, match="inbox_execution_proof_invalid"):
        inbox.finish_execution(
            **guard(job),
            proof_kind="terminal",
            thread_id=THREAD,
            turn_id="late-exact-turn",
            terminal_status="interrupted",
            local_tasks_done=False,
        )
    with pytest.raises(InboxError, match="inbox_execution_proof_invalid"):
        inbox.finish_execution(
            **guard(job),
            proof_kind="terminal",
            thread_id=THREAD,
            turn_id="another-turn",
            terminal_status="interrupted",
            local_tasks_done=True,
        )
    assert inbox.claim(instruction_root=ROOT, instruction_revision=REVISION) is None
    proved = terminal(inbox, job, "interrupted")
    generation = inbox.generation
    assert terminal(inbox, job, "interrupted") == proved
    assert inbox.generation == generation
    assert claim(inbox)["peer_id"] == 2


def test_terminal_peer_quarantine_allows_other_peers_in_order(inbox):
    inbox.record(1, 1)
    inbox.record(2, 1)
    inbox.record(3, 1)
    job = running(inbox)
    inbox.record(1, 2)
    job = inbox.needs_review(**guard(job), reason_code="codex_turn_failed")
    job = terminal(inbox, job, "failed")
    assert inbox.status()["held_peers"] == 1
    second = claim(inbox)
    assert second["peer_id"] == 2
    inbox.release_job(**guard(second))
    # A release requeues B; its original timestamp keeps it ahead of C.
    assert claim(inbox)["peer_id"] == 2
    assert inbox.read_job(job["job_id"])["state"] == "needs_review"


def test_unknown_creation_is_durable_and_manually_recoverable(tmp_path, clock):
    path = tmp_path / "inbox" / "state.sqlite3"
    with TelegramInbox(path, clock=clock) as queue:
        queue.duty_set(True)
        queue.record(1, 1)
        job = claim(queue)
        creation = queue.prepare_peer_thread(**guard(job))
        assert creation["created"] is True
        assert queue.prepare_peer_thread(**guard(job))["created"] is False
        queue.mark_peer_thread_unknown(**guard(job), creation_key=creation["creation_key"])
    with TelegramInbox(path, clock=clock) as queue:
        queue.recover_on_startup()
        job = queue.read_job(job["job_id"])
        assert job["state"] == "needs_review" and job["execution_proof"] == "not_started"
        assert queue.read_peer_thread(job["job_id"])["state"] == "creation_unknown"
        assert queue.execution_slot() is None
        with pytest.raises(InboxError, match="inbox_peer_thread_unknown"):
            queue.bind_peer_thread(**guard(job), creation_key=creation["creation_key"], thread_id=THREAD)
        with pytest.raises(InboxError, match="inbox_peer_thread_recovery_required"):
            queue.bind_peer_thread(
                **guard(job),
                creation_key=creation["creation_key"],
                thread_id=THREAD,
                effects_reviewed=True,
            )
        pause(queue)
        queue.bind_peer_thread(
            **guard(job),
            creation_key=creation["creation_key"],
            thread_id=THREAD,
            effects_reviewed=True,
        )
        released = queue.resolve_job(**guard(job), decision="retry_no_effects", effects_reviewed=True)
        assert released["lease"] != job["lease"]
        pause(queue, False)
        fresh = claim(queue)
        binding = queue.prepare_peer_thread(**guard(fresh))
        assert binding["state"] == "ready" and binding["thread_id"] == THREAD
        assert binding["created"] is False


def test_manual_recovery_rotates_authority_and_preserves_automatic_deadline(inbox, clock):
    inbox.record(1, 1)
    job = running(inbox)
    deadline = job["deadline_at"]
    job = inbox.needs_review(**guard(job), reason_code="codex_turn_failed")
    job = terminal(inbox, job, "failed")
    clock.advance(1300)
    pause(inbox)
    manual = inbox.resolve_job(**guard(job), decision="continue_bound_turn", effects_reviewed=True)
    assert manual["lease"] != job["lease"] and manual["deadline_at"] == deadline
    snapshot = inbox.execution_authority(**guard(manual))
    assert snapshot["manual_recovery"] is True
    with pytest.raises(InboxError, match="inbox_lease_mismatch"):
        inbox.execution_authority(**guard(job))
    inbox.check_execution_authority(snapshot)
    key = inbox.reserve_effect(**guard(manual), kind="result", text_sha256="a" * 64, peer_id=1, reply_to_message_id=1)
    inbox.mark_effect_intent(key)
    inbox.verify_effect(effect_key=key, receipt_key=key, message_id=100)
    inbox.result(**guard(manual), outcome="answered", event_ids=manual["event_ids"], receipt_keys=[key])
    assert inbox.execution_slot() is None


def test_unknown_delivery_is_not_retried_even_after_execution_is_terminal(inbox):
    inbox.record(1, 1)
    job = running(inbox)
    key = inbox.reserve_effect(**guard(job), kind="result", text_sha256="a" * 64, peer_id=1, reply_to_message_id=1)
    inbox.mark_effect_intent(key)
    job = inbox.needs_review(**guard(job), reason_code="send_outcome_unknown")
    job = terminal(inbox, job)
    pause(inbox)
    with pytest.raises(InboxError, match="inbox_effect_outcome_unknown"):
        inbox.resolve_job(**guard(job), decision="continue_bound_turn", effects_reviewed=True)
    # A late independently verified receipt remains admissible under pause.
    inbox.verify_effect(effect_key=key, receipt_key=key, message_id=100)
    manual = inbox.resolve_job(**guard(job), decision="continue_bound_turn", effects_reviewed=True)
    permit = inbox.execution_authority(**guard(manual))
    before = inbox.status()
    with pytest.raises(InboxError, match="inbox_manual_recovery_active"):
        pause(inbox, False)
    assert inbox.status()["execution_revision"] == before["execution_revision"]
    assert inbox.status()["execution_paused"] is True
    assert inbox.execution_slot()["slot_phase"] == "manual"
    inbox.check_execution_authority(permit)


def test_stopping_manual_recovery_holds_peer_and_allows_a_new_reviewed_grant(inbox):
    inbox.record(1, 1)
    job = running(inbox)
    job = inbox.needs_review(**guard(job), reason_code="codex_turn_failed")
    job = terminal(inbox, job, "failed")
    pause(inbox)
    manual = inbox.resolve_job(**guard(job), decision="continue_bound_turn", effects_reviewed=True)
    permit = inbox.execution_authority(**guard(manual))
    inbox.record(2, 1)

    stopped = terminal(inbox, manual, "failed")
    assert stopped["state"] == "needs_review"
    assert stopped["reason_code"] == "manual_execution_stopped"
    assert not stopped["manual_recovery"]
    assert inbox.execution_slot() is None
    with pytest.raises(InboxError, match="inbox_revision_conflict"):
        inbox.check_execution_authority(permit)

    pause(inbox, False)
    following = claim(inbox)
    assert following["peer_id"] == 2
    inbox.release_job(**guard(following))
    pause(inbox)
    recovered = inbox.resolve_job(**guard(stopped), decision="continue_bound_turn", effects_reviewed=True)
    assert recovered["lease"] != manual["lease"]
    assert inbox.execution_authority(**guard(recovered))["manual_recovery"] is True


def test_repeated_pause_revokes_only_the_current_manual_grant(inbox):
    inbox.record(1, 1)
    job = running(inbox)
    job = inbox.needs_review(**guard(job), reason_code="codex_turn_failed")
    job = terminal(inbox, job, "failed")
    pause(inbox)
    manual = inbox.resolve_job(**guard(job), decision="continue_bound_turn", effects_reviewed=True)
    permit = inbox.execution_authority(**guard(manual))
    before = inbox.status()

    pause(inbox)
    revoked = inbox.status()
    assert revoked["execution_revision"] == before["execution_revision"] + 1
    with pytest.raises(InboxError, match="inbox_execution_stale"):
        inbox.check_execution_authority(permit)
    assert inbox.read_job(manual["job_id"])["lease"] == manual["lease"]
    assert inbox.execution_slot()["slot_phase"] == "manual"
    pause(inbox)
    assert inbox.status()["execution_revision"] == revoked["execution_revision"]
    with pytest.raises(InboxError, match="inbox_manual_recovery_active"):
        pause(inbox, False)

    # The old execution is stopped, not restarted, and its stop proof can still
    # retire the slot after the write grant has been revoked.
    held = terminal(inbox, manual, "failed")
    assert held["state"] == "needs_review" and inbox.execution_slot() is None
    assert inbox.claim(instruction_root=ROOT, instruction_revision=REVISION) is None


def test_releasing_context_creation_keeps_exact_manual_recovery_possible(inbox):
    inbox.record(1, 1)
    job = claim(inbox)
    mapping = inbox.prepare_peer_thread(**guard(job))
    held = inbox.release_job(**guard(job))
    assert held["state"] == "needs_review"
    assert held["execution_proof"] == "not_started"
    assert inbox.read_peer_thread(job["job_id"])["state"] == "creation_unknown"
    assert inbox.execution_slot() is None
    assert inbox.claim(instruction_root=ROOT, instruction_revision=REVISION) is None

    pause(inbox)
    inbox.bind_peer_thread(**guard(held), creation_key=mapping["creation_key"], thread_id=THREAD, effects_reviewed=True)
    inbox.resolve_job(**guard(held), decision="retry_no_effects", effects_reviewed=True)
    pause(inbox, False)
    recovered = claim(inbox)
    bound = inbox.prepare_peer_thread(**guard(recovered))
    assert recovered["peer_id"] == 1
    assert bound["state"] == "ready" and bound["thread_id"] == THREAD
    assert bound["creation_key"] == mapping["creation_key"] and not bound["created"]


@pytest.mark.parametrize("started", [False, True])
def test_direct_execution_proof_holds_unfinished_business_job(inbox, started):
    inbox.record(1, 1)
    job = running(inbox) if started else claim(inbox)
    if started:
        held = terminal(inbox, job)
    else:
        held = inbox.finish_execution(**guard(job), proof_kind="not_started", local_tasks_done=True)
    assert held["state"] == "needs_review"
    assert held["reason_code"] == "execution_stopped"
    assert inbox.execution_slot() is None
    with pytest.raises(InboxError, match="inbox_revision_conflict"):
        inbox.needs_review(**guard(job), reason_code="obsolete_owner")
    assert inbox.claim(instruction_root=ROOT, instruction_revision=REVISION) is None
    inbox.record(2, 1)
    assert claim(inbox)["peer_id"] == 2


def legacy_database(path, version):
    path.parent.mkdir(mode=0o700)
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE peers (peer_id INTEGER PRIMARY KEY,context_revision INTEGER NOT NULL DEFAULT0);
            CREATE TABLE duty_windows (
                window_id INTEGER PRIMARY KEY AUTOINCREMENT,opened_at TEXT NOT NULL,closed_at TEXT,recovered_at TEXT
            );
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,peer_id INTEGER NOT NULL REFERENCES peers(peer_id),state TEXT NOT NULL,
                revision INTEGER NOT NULL,lease TEXT NOT NULL,context_revision INTEGER NOT NULL,
                instruction_root TEXT NOT NULL,instruction_revision TEXT NOT NULL,thread_id TEXT,turn_id TEXT,
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL,outcome TEXT,reason_code TEXT
            );
            CREATE TABLE events (
                sequence TEXT PRIMARY KEY,peer_id INTEGER NOT NULL REFERENCES peers(peer_id),message_id INTEGER NOT NULL,
                received_at TEXT NOT NULL,state TEXT NOT NULL,context_revision INTEGER NOT NULL,
                job_id TEXT REFERENCES jobs(job_id),completed_at TEXT,UNIQUE(peer_id,message_id)
            );
            CREATE TABLE effects (
                effect_key TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                kind TEXT NOT NULL,context_revision INTEGER NOT NULL,effect_slot INTEGER NOT NULL,text_sha256 TEXT,
                peer_id INTEGER NOT NULL,reply_to_message_id INTEGER NOT NULL,receipt_key TEXT UNIQUE,
                sent_message_id INTEGER,verified INTEGER NOT NULL DEFAULT0,attempted INTEGER NOT NULL DEFAULT0,
                created_at TEXT NOT NULL,verified_at TEXT,UNIQUE(job_id,kind,context_revision,effect_slot)
            );
            """.replace("DEFAULT0", "DEFAULT 0")
        )
        db.executemany(
            "INSERT INTO metadata VALUES (?,?)",
            [("schema_version", str(version)), ("generation", "7"), ("duty_enabled", "1")],
        )
        if version == 2:
            db.executemany(
                "INSERT INTO metadata VALUES (?,?)", [("execution_paused", "1"), ("execution_revision", "9")]
            )
        db.execute("INSERT INTO peers VALUES(1,3)")
        job_id = "job-" + "1" * 32
        at = "2026-10-22T09:00:00.000000+00:00"
        db.execute("INSERT INTO duty_windows(opened_at) VALUES (?)", (at,))
        db.execute(
            "INSERT INTO jobs VALUES (?,1,'running',3,'old-lease',3,?,?,'mixed-thread','old-turn',?,?,NULL,NULL)",
            (job_id, ROOT, REVISION, at, at),
        )
        db.execute("INSERT INTO events VALUES('100',1,7,?,'open',3,?,NULL)", (at, job_id))
        db.execute(
            "INSERT INTO effects VALUES('old-effect',?,'result',3,0,?,1,7,NULL,NULL,0,1,?,NULL)",
            (job_id, "a" * 64, at),
        )
    return job_id


@pytest.mark.parametrize("version", [1, 2])
def test_real_legacy_schema_upgrade_preserves_unknowns_without_mapping_mixed_history(tmp_path, clock, version):
    path = tmp_path / "legacy" / "state.sqlite3"
    job_id = legacy_database(path, version)
    with TelegramInbox(path, clock=clock) as queue:
        old = queue.read_job(job_id)
        assert queue.status()["schema_version"] == 7 and queue.status()["worker_protocol"] == 2
        assert queue.generation == 7 and old["revision"] == 3 and old["lease"] == "old-lease"
        assert old["context_revision"] == 3 and old["turn_id"] == "old-turn"
        assert old["context_mode"] == "legacy" and old["deadline_at"] is None
        assert queue.read_peer_thread(job_id) is None and queue.status()["compatibility_hold"]
        assert queue.status()["execution_paused"] is (version == 2)
        assert queue.status()["execution_revision"] == (9 if version == 2 else 1)
    with TelegramInbox(path, clock=clock) as queue:
        assert queue.read_job(job_id) == old
        queue.recover_on_startup()
        queue.record(2, 1)
        if version == 2:
            pause(queue, False)
        assert queue.claim(instruction_root=ROOT, instruction_revision=REVISION) is None
        old = queue.read_job(job_id)
        old = terminal(queue, old, "interrupted")
        assert old["state"] == "needs_review" and not queue.status()["compatibility_hold"]
        assert queue.read_effect("old-effect")["attempted"] == 1
        assert queue.read_effect("old-effect")["verified"] == 0
        assert claim(queue)["peer_id"] == 2


def test_failed_legacy_upgrade_is_atomic(tmp_path, clock):
    path = tmp_path / "legacy" / "state.sqlite3"
    legacy_database(path, 2)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TRIGGER reject_upgrade BEFORE UPDATE ON metadata WHEN NEW.key='schema_version' "
            "BEGIN SELECT RAISE(ABORT,'synthetic upgrade failure'); END"
        )
        before = list(db.iterdump())
    with pytest.raises(InboxError, match="inbox_open_failed"):
        TelegramInbox(path, clock=clock)
    with sqlite3.connect(path) as db:
        assert list(db.iterdump()) == before


def test_active_slot_protects_completed_job_from_retention(inbox, clock):
    event = inbox.record(1, 1)
    job = running(inbox)
    done = inbox.result(
        **guard(job),
        outcome="no_reply_needed",
        event_ids=[event],
        receipt_keys=[],
        reason_code="not_actionable",
    )
    inbox.advance_checkpoint(1, 1)
    clock.advance(8 * 24 * 3600)
    assert inbox.prune() == 0
    assert inbox.resolve(event).message_id == 1 and inbox.read_job(done["job_id"])["state"] == "completed"
    terminal(inbox, done)
    assert inbox.prune() == 1


def test_each_peer_keeps_its_own_thread_across_jobs_and_reopen(tmp_path, clock):
    path = tmp_path / "inbox" / "state.sqlite3"
    other_thread = "00000000-0000-4000-8000-000000000002"
    with TelegramInbox(path, clock=clock) as queue:
        queue.duty_set(True)
        queue.record(1, 1)
        queue.record(2, 1)
        first = running(queue)
        first_mapping = queue.read_peer_thread(first["job_id"])
        first = queue.result(
            **guard(first),
            outcome="no_reply_needed",
            event_ids=first["event_ids"],
            receipt_keys=[],
            reason_code="not_actionable",
        )
        terminal(queue, first)
    with TelegramInbox(path, clock=clock) as queue:
        second = claim(queue)
        assert second["peer_id"] == 2
        mapping = queue.prepare_peer_thread(**guard(second))
        with pytest.raises(InboxError, match="inbox_peer_thread_conflict"):
            queue.bind_peer_thread(**guard(second), creation_key=mapping["creation_key"], thread_id=THREAD)
        queue.bind_peer_thread(**guard(second), creation_key=mapping["creation_key"], thread_id=other_thread)
        second = queue.start_intent(**guard(second), thread_id=other_thread)
        second = queue.bind_turn(**guard(second), turn_id="second-client-turn")
        second = queue.result(
            **guard(second),
            outcome="no_reply_needed",
            event_ids=second["event_ids"],
            receipt_keys=[],
            reason_code="not_actionable",
        )
        terminal(queue, second)
        queue.record(1, 2)
        later = claim(queue)
        assert later["peer_id"] == 1
        reused = queue.prepare_peer_thread(**guard(later))
        assert reused["thread_id"] == THREAD and reused["creation_key"] == first_mapping["creation_key"]
        assert reused["created"] is False
        generation = queue.generation
        queue.bind_peer_thread(**guard(later), creation_key=reused["creation_key"], thread_id=THREAD)
        queue.mark_peer_thread_unknown(**guard(later), creation_key=reused["creation_key"])
        assert queue.generation == generation
        assert queue.read_peer_thread(later["job_id"])["state"] == "ready"
