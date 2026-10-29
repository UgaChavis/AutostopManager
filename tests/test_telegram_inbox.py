from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from autostop_manager.telegram_inbox import InboxError, TelegramInbox

ROOT = "/opt/test-runtime"
REVISION = "a" * 40


@dataclass
class Clock:
    value: datetime = datetime(2026, 10, 12, 9, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def inbox(tmp_path: Path, clock: Clock):
    with TelegramInbox(tmp_path / "inbox" / "inbound.sqlite3", clock=clock) as result:
        result.duty_set(True)
        yield result


def claim(inbox: TelegramInbox, **kwargs: Any) -> dict[str, Any]:
    job = inbox.claim(instruction_root=ROOT, instruction_revision=REVISION, **kwargs)
    assert job is not None
    return job


def guard(job: dict[str, Any]) -> dict[str, Any]:
    return {"job_id": job["job_id"], "lease": job["lease"], "expected_revision": job["revision"]}


def running(inbox: TelegramInbox) -> dict[str, Any]:
    job = claim(inbox)
    mapping = inbox.prepare_peer_thread(**guard(job))
    thread_id = mapping["thread_id"] or f"thread-{job['peer_id']}"
    inbox.bind_peer_thread(**guard(job), creation_key=mapping["creation_key"], thread_id=thread_id)
    job = inbox.start_intent(**guard(job), thread_id=thread_id)
    return inbox.bind_turn(**guard(job), turn_id="turn-1")


def receipt(inbox: TelegramInbox, job: dict[str, Any], kind: str = "result", *, digest: str = "b" * 64) -> str:
    event = inbox.resolve(job["event_ids"][0])
    key = inbox.reserve_effect(
        **guard(job),
        kind=kind,
        text_sha256=digest,
        peer_id=event.peer_id,
        reply_to_message_id=event.message_id,
    )
    inbox.verify_effect(effect_key=key, receipt_key=key, message_id=event.message_id + 100)
    return key


def finish(inbox: TelegramInbox, job: dict[str, Any], key: str, outcome: str = "answered") -> dict[str, Any]:
    done = inbox.result(**guard(job), outcome=outcome, event_ids=job["event_ids"], receipt_keys=[key])
    # This helper models the complete workflow, including ended model work.
    return inbox.finish_execution(
        **guard(done),
        proof_kind="terminal",
        thread_id=done["thread_id"],
        turn_id=done["turn_id"],
        terminal_status="completed",
        local_tasks_done=True,
    )


def review_stopped(inbox: TelegramInbox, job: dict[str, Any]) -> dict[str, Any]:
    stopped = inbox.finish_execution(
        **guard(job),
        proof_kind="terminal",
        thread_id=job["thread_id"],
        turn_id=job["turn_id"],
        terminal_status="failed",
        local_tasks_done=True,
    )
    inbox.set_execution_paused(paused=True, expected_revision=inbox.status()["execution_revision"])
    return stopped


def test_sqlite_storage_is_private_durable_and_one_writer(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "inbox" / "inbound.sqlite3"
    first = TelegramInbox(path, clock=clock)
    first.duty_set(True)
    event_id = first.record(1, 11)
    assert event_id
    generation = first.generation
    assert first._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert first._db.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert first._meta("schema_version") == "7"
    assert path.parent.stat().st_mode & 0o777 == 0o700
    for suffix in ("", "-wal", "-shm", ".lock"):
        assert Path(str(path) + suffix).stat().st_mode & 0o777 == 0o600
    with pytest.raises(InboxError, match="inbox_writer_active"):
        TelegramInbox(path)
    first.close()
    with TelegramInbox(path, clock=clock) as restored:
        assert restored.resolve(event_id).message_id == 11
        assert restored.record(1, 11) == event_id
        assert restored.generation == generation
        assert restored.status()["open_events"] == 1


@pytest.mark.parametrize("pause", [None, False, True])
def test_schema_v1_upgrade_preserves_pending_identity_effects_context_and_pause(
    tmp_path: Path, clock: Clock, pause: bool | None
) -> None:
    path = tmp_path / "inbox" / "inbound.sqlite3"
    with TelegramInbox(path, clock=clock) as old:
        old.duty_set(True)
        first = old.record(1, 1)
        job = running(old)
        known = receipt(old, job, "ack")
        unknown = old.reserve_effect(
            **guard(job), kind="result", text_sha256="c" * 64, peer_id=1, reply_to_message_id=1
        )
        old.mark_effect_intent(unknown)
        pending = old.record(1, 2)
        job = old.needs_review(**guard(job), reason_code="codex_turn_failed")
        old.advance_checkpoint(1, 2)
        if pause is True:
            old.set_execution_paused(paused=True, expected_revision=1)
        if pause is None:
            # Original v1 registries predate persistent executor control.
            old._db.execute("DELETE FROM metadata WHERE key IN ('execution_paused','execution_revision')")
        old._db.execute("UPDATE metadata SET value='1' WHERE key='schema_version'")
        old._db.commit()
        before_job = old.read_job(job["job_id"])
        before_events = old.events()
        generation = old.generation
        peer_context = old._peer_revision(1)
        windows = old.duty_windows()
    with TelegramInbox(path, clock=clock) as upgraded:
        assert upgraded._meta("schema_version") == "7"
        assert upgraded.read_job(job["job_id"]) == before_job
        assert upgraded.events() == before_events
        assert upgraded.require_open(first).message_id == 1
        assert upgraded.require_open(pending).message_id == 2
        assert upgraded.read_effect(known)["verified"] == 1
        assert upgraded.read_effect(unknown)["attempted"] == 1
        assert upgraded.read_effect(unknown)["verified"] == 0
        assert upgraded.generation == generation and upgraded._peer_revision(1) == peer_context
        assert upgraded.checkpoint(1) == 2 and upgraded.duty_windows() == windows
        status = upgraded.status()
        assert status["execution_paused"] is (pause is True)
        assert status["execution_revision"] == (2 if pause is True else 1)


def test_unknown_schema_is_rejected_before_registry_or_journal_mutations(tmp_path: Path) -> None:
    path = tmp_path / "inbox" / "inbound.sqlite3"
    path.parent.mkdir()
    with sqlite3.connect(path) as future:
        future.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        future.execute("INSERT INTO metadata VALUES ('schema_version','8')")
        future.execute("CREATE TABLE future_state (value TEXT)")
        future.execute("INSERT INTO future_state VALUES ('synthetic')")
        before = list(future.iterdump())
        assert future.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    with pytest.raises(InboxError, match="inbox_open_failed"):
        TelegramInbox(path)
    with sqlite3.connect(path) as unchanged:
        assert list(unchanged.iterdump()) == before
        assert unchanged.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_failed_v1_upgrade_rolls_back_defaults_and_schema_version(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "inbox" / "inbound.sqlite3"
    with TelegramInbox(path, clock=clock) as old:
        old.duty_set(True)
        event = old.record(1, 1)
        old._db.execute("DELETE FROM metadata WHERE key IN ('execution_paused','execution_revision')")
        old._db.execute("UPDATE metadata SET value='1' WHERE key='schema_version'")
        old._db.execute(
            "CREATE TRIGGER fail_upgrade BEFORE UPDATE ON metadata WHEN NEW.key='schema_version' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic'); END"
        )
        old._db.commit()
        before = list(old._db.iterdump())
    with pytest.raises(InboxError, match="inbox_open_failed"):
        TelegramInbox(path, clock=clock)
    with sqlite3.connect(path) as unchanged:
        assert list(unchanged.iterdump()) == before
        unchanged.execute("DROP TRIGGER fail_upgrade")
    with TelegramInbox(path, clock=clock) as recovered:
        assert recovered._meta("schema_version") == "7"
        assert recovered.require_open(event).message_id == 1
        assert recovered.status()["execution_paused"] is False


def test_duplicate_notification_is_safe_and_generation_does_not_change(inbox: TelegramInbox) -> None:
    event_id = inbox.record(2, 22)
    generation = inbox.generation
    assert inbox.record(2, 22) == event_id
    assert inbox.generation == generation
    assert inbox.status()["open_events"] == 1


def test_duty_windows_exclude_old_tests_and_disabled_periods(inbox: TelegramInbox, clock: Clock) -> None:
    assert inbox.record(1, 1, (clock.value - timedelta(seconds=1)).isoformat()) is None
    enabled_at = clock.value
    event_id = inbox.record(1, 2)
    clock.advance(10)
    inbox.duty_set(False)
    assert inbox.record(1, 3) is None
    clock.advance(10)
    disabled_at = clock.value
    clock.advance(10)
    inbox.duty_set(True)
    assert inbox.record(1, 4, disabled_at.isoformat()) is None
    assert inbox.record(2, 5, enabled_at.isoformat()) is not None
    assert inbox.record(1, 6) is not None
    assert inbox.require_open(event_id).message_id == 2
    windows = inbox.recovery_windows()
    assert len(windows) == 2
    assert windows[0]["ended_at"] == windows[0]["closed_at"]
    inbox.mark_window_recovered(windows[0]["window_id"])
    assert len(inbox.recovery_windows()) == 1


def test_subsecond_duty_enable_holds_coarse_first_second(tmp_path: Path, clock: Clock) -> None:
    message_at = clock.value.isoformat()
    clock.value += timedelta(microseconds=500000)
    with TelegramInbox(tmp_path / "inbox" / "inbound.sqlite3", clock=clock) as queue:
        queue.configure_instructions(ROOT, REVISION)
        queue.duty_set(True)
        assert queue.recovery_allowed(message_at, timestamp_precision="second")
        assert queue.duty_classification(message_at, timestamp_precision="second") == "ambiguous"
        event_id = queue.record(1, 1, message_at, timestamp_precision="second")
        assert event_id
        assert queue.resolve(event_id).message_id == 1
        held = queue.active_jobs()[0]
        assert held["state"] == "needs_review"
        assert held["reason_code"] == "duty_boundary_ambiguous"
        assert held["instruction_root"] == ROOT
        assert queue.claim(instruction_root=ROOT, instruction_revision=REVISION) is None
        # A known precise timestamp after enable is allowed; it does not make
        # the coarse first-second locator safe to execute automatically.
        assert queue.duty_classification((clock.value + timedelta(microseconds=300000)).isoformat()) == "allowed"


def test_subsecond_duty_disable_holds_last_second_and_excludes_whole_off(tmp_path: Path, clock: Clock) -> None:
    with TelegramInbox(tmp_path / "inbox" / "inbound.sqlite3", clock=clock) as queue:
        queue.configure_instructions(ROOT, REVISION)
        queue.duty_set(True)
        clock.value += timedelta(seconds=1, microseconds=500000)
        queue.duty_set(False)
        last_second = clock.value.replace(microsecond=0).isoformat()
        assert queue.duty_classification(last_second, timestamp_precision="second") == "ambiguous"
        event_id = queue.record(1, 1, last_second, timestamp_precision="second")
        assert event_id and queue.active_jobs()[0]["event_ids"] == [event_id]
        off_second = (clock.value + timedelta(seconds=1)).replace(microsecond=0).isoformat()
        assert queue.duty_classification(off_second, timestamp_precision="second") == "excluded"
        assert queue.record(1, 2, off_second, timestamp_precision="second") is None


def test_whole_second_boundaries_do_not_create_false_ambiguity(tmp_path: Path, clock: Clock) -> None:
    with TelegramInbox(tmp_path / "inbox" / "inbound.sqlite3", clock=clock) as queue:
        start = clock.value
        queue.duty_set(True)
        clock.advance(2)
        queue.duty_set(False)
        assert queue.duty_classification(start.isoformat(), timestamp_precision="second") == "allowed"
        assert (
            queue.duty_classification((start + timedelta(seconds=1)).isoformat(), timestamp_precision="second")
            == "allowed"
        )
        assert queue.duty_classification(clock.value.isoformat(), timestamp_precision="second") == "excluded"
        event_id = queue.record(1, 1, start.isoformat(), timestamp_precision="second")
        assert event_id
        queue.duty_set(True)
        assert claim(queue)["event_ids"] == [event_id]


def test_same_second_off_on_gap_requires_review_even_when_both_edges_on(tmp_path: Path, clock: Clock) -> None:
    with TelegramInbox(tmp_path / "inbox" / "inbound.sqlite3", clock=clock) as queue:
        queue.configure_instructions(ROOT, REVISION)
        start = clock.value
        queue.duty_set(True)
        clock.value = start + timedelta(microseconds=200000)
        queue.duty_set(False)
        clock.value = start + timedelta(microseconds=800000)
        queue.duty_set(True)
        assert queue.duty_classification(start.isoformat(), timestamp_precision="second") == "ambiguous"
        assert queue.record(1, 1, start.isoformat(), timestamp_precision="second")
        assert queue.status()["needs_review_events"] == 1


def test_ambiguous_locator_survives_missing_pin_and_restart_until_review(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "inbox" / "inbound.sqlite3"
    message_at = clock.value.isoformat()
    clock.value += timedelta(microseconds=500000)
    with TelegramInbox(path, clock=clock) as first:
        first.duty_set(True)
        event_id = first.record(1, 1, message_at, timestamp_precision="second")
        assert event_id
        assert first.status()["needs_review_events"] == 1
        assert first.gaps()[0]["reason_code"] == "boundary_instruction_unavailable"
        assert first.active_jobs() == []
    with TelegramInbox(path, clock=clock) as restored:
        # Supplying an authentic snapshot can attach the lease, never replay.
        assert restored.claim(instruction_root=ROOT, instruction_revision=REVISION) is None
        held = restored.active_jobs()[0]
        assert held["event_ids"] == [event_id] and held["state"] == "needs_review"
        restored.recover_on_startup()
        assert restored.claim(instruction_root=ROOT, instruction_revision=REVISION) is None
        with pytest.raises(InboxError, match="inbox_resolution_review_required"):
            restored.resolve_job(**guard(held), decision="retry_no_effects")
        restored.set_execution_paused(paused=True, expected_revision=restored.status()["execution_revision"])
        restored.resolve_job(**guard(held), decision="retry_no_effects", effects_reviewed=True)
        restored.set_execution_paused(paused=False, expected_revision=restored.status()["execution_revision"])
        assert claim(restored)["event_ids"] == [event_id]


def test_claim_batches_only_same_peer_and_five_minute_window(inbox: TelegramInbox, clock: Clock) -> None:
    first = inbox.record(1, 1)
    for message in range(2, 8):
        clock.advance(1)
        inbox.record(1, message)
    inbox.record(2, 1)
    clock.advance(301)
    late = inbox.record(1, 9)
    job = claim(inbox)
    assert job["event_ids"][0] == first
    assert len(job["event_ids"]) == 5
    assert late not in job["event_ids"]
    assert all(inbox.resolve(ref).peer_id == 1 for ref in job["event_ids"])
    assert inbox.claim(instruction_root=ROOT, instruction_revision=REVISION) is None


def test_disabled_duty_retains_pending_event_until_explicit_enable(inbox: TelegramInbox, clock: Clock) -> None:
    event = inbox.record(1, 1)
    clock.advance(1)
    inbox.duty_set(False)
    generation = inbox.generation
    assert inbox.claim(instruction_root=ROOT, instruction_revision=REVISION) is None
    assert inbox.generation == generation and inbox.require_open(event)
    assert inbox.record(1, 2) is None
    inbox.duty_set(True)
    assert claim(inbox)["event_ids"] == [event]


def test_explicit_executor_pause_survives_restart_and_preserves_intake(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "inbox" / "inbound.sqlite3"
    with TelegramInbox(path, clock=clock) as first:
        first.duty_set(True)
        original = first.record(1, 1)
        assert first.status()["execution_paused"] is False
        assert first.status()["execution_revision"] == 1
        paused = first.set_execution_paused(paused=True, expected_revision=1)
        assert paused["execution_paused"] is True and paused["execution_revision"] == 2
        later = first.record(1, 2)
        assert later and first.status()["open_events"] == 2
        assert first.status()["execution_revision"] == 2
    with TelegramInbox(path, clock=clock) as restored:
        restored.recover_on_startup()
        status = restored.status()
        assert status["enabled"] and status["execution_paused"] and status["execution_revision"] == 2
        assert restored.claim(instruction_root=ROOT, instruction_revision=REVISION) is None
        assert restored.require_open(original) and restored.require_open(later)
        # Changing intake duty does not implicitly clear an explicit pause.
        restored.duty_set(False)
        restored.duty_set(True)
        assert restored.claim(instruction_root=ROOT, instruction_revision=REVISION) is None
        restored.set_execution_paused(paused=False, expected_revision=2)
        assert claim(restored)["event_ids"] == [original, later]


def test_executor_revision_is_independent_of_incoming_events(inbox: TelegramInbox) -> None:
    generation = inbox.generation
    inbox.record(1, 1)
    assert inbox.generation > generation and inbox.status()["execution_revision"] == 1
    inbox.set_execution_paused(paused=True, expected_revision=1)
    generation = inbox.generation
    inbox.record(1, 2)
    assert inbox.generation > generation and inbox.status()["execution_revision"] == 2
    resumed = inbox.set_execution_paused(paused=False, expected_revision=2)
    assert resumed["execution_paused"] is False and resumed["execution_revision"] == 3


def test_executor_control_validates_stale_cas_even_when_desired_state_matches(inbox: TelegramInbox) -> None:
    initial = inbox.set_execution_paused(paused=False, expected_revision=1)
    assert initial["execution_revision"] == 1
    paused = inbox.set_execution_paused(paused=True, expected_revision=1)
    generation = inbox.generation
    for desired in (False, True):
        with pytest.raises(InboxError, match="inbox_execution_revision_conflict"):
            inbox.set_execution_paused(paused=desired, expected_revision=1)
    same = inbox.set_execution_paused(paused=True, expected_revision=2)
    assert same == paused and inbox.generation == generation
    assert inbox.status()["execution_paused"] is True


@pytest.mark.parametrize("paused", [0, 1, "true", None, []])
def test_executor_control_requires_boolean_pause(inbox: TelegramInbox, paused: Any) -> None:
    with pytest.raises(InboxError, match="inbox_execution_invalid"):
        inbox.set_execution_paused(paused=paused, expected_revision=1)
    assert inbox.status()["execution_revision"] == 1


@pytest.mark.parametrize("revision", [True, 0, -1, "1", None])
def test_executor_control_requires_positive_integer_revision(inbox: TelegramInbox, revision: Any) -> None:
    with pytest.raises(InboxError, match="inbox_execution_revision_invalid"):
        inbox.set_execution_paused(paused=True, expected_revision=revision)
    assert inbox.status()["execution_paused"] is False


def test_executor_pause_and_revision_roll_back_together_on_storage_failure(inbox: TelegramInbox) -> None:
    before = inbox.status()
    inbox._db.execute(
        "CREATE TRIGGER fail_pause BEFORE UPDATE ON metadata WHEN NEW.key='execution_revision' "
        "BEGIN SELECT RAISE(ABORT, 'synthetic'); END"
    )
    inbox._db.commit()
    with pytest.raises(InboxError, match="inbox_storage_failed"):
        inbox.set_execution_paused(paused=True, expected_revision=1)
    assert inbox.status() == before


def test_claim_lease_and_revision_prevent_stale_or_unauthorized_transition(inbox: TelegramInbox) -> None:
    inbox.record(1, 1)
    job = claim(inbox)
    with pytest.raises(InboxError, match="inbox_lease_mismatch"):
        inbox.start_intent(job_id=job["job_id"], lease="wrong", expected_revision=job["revision"], thread_id="thread-1")
    mapping = inbox.prepare_peer_thread(**guard(job))
    inbox.bind_peer_thread(**guard(job), creation_key=mapping["creation_key"], thread_id="thread-1")
    new = inbox.start_intent(**guard(job), thread_id="thread-1")
    with pytest.raises(InboxError, match="inbox_revision_conflict"):
        inbox.bind_turn(**guard(job), turn_id="turn-1")
    assert inbox.read_job(job["job_id"])["revision"] == new["revision"]


@pytest.mark.parametrize("started", [False, True])
def test_restart_releases_only_proven_unstarted_claims(tmp_path: Path, clock: Clock, started: bool) -> None:
    path = tmp_path / "inbox" / "inbound.sqlite3"
    with TelegramInbox(path, clock=clock) as first:
        first.duty_set(True)
        event_id = first.record(1, 1)
        job = claim(first)
        if started:
            mapping = first.prepare_peer_thread(**guard(job))
            first.bind_peer_thread(**guard(job), creation_key=mapping["creation_key"], thread_id="thread-1")
            job = first.start_intent(**guard(job), thread_id="thread-1")
    with TelegramInbox(path, clock=clock) as restored:
        restored.recover_on_startup()
        assert restored.require_open(event_id)
        actual = restored.read_job(job["job_id"])
        assert actual["state"] == ("needs_review" if started else "released")
        if started:
            assert restored.claim(instruction_root=ROOT, instruction_revision=REVISION) is None
            assert actual["thread_id"] == "thread-1"
        else:
            assert claim(restored)["event_ids"] == [event_id]


def test_known_running_turn_and_intake_survive_restart_without_replay(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "inbox" / "inbound.sqlite3"
    with TelegramInbox(path, clock=clock) as first:
        first.duty_set(True)
        first.record(1, 1)
        job = running(first)
        ack = receipt(first, job, "ack")
        first.record(2, 2)
    with TelegramInbox(path, clock=clock) as restored:
        restored.recover_on_startup()
        current = restored.read_job(job["job_id"])
        assert current["state"] == "needs_review"
        assert current["turn_id"] == "turn-1"
        assert current["effects"][0]["receipt_key"] == ack
        assert restored.record(3, 3)
        assert restored.status()["open_events"] == 3
        assert restored.claim(instruction_root=ROOT, instruction_revision=REVISION) is None


def test_acknowledgement_does_not_close_request_or_allow_final_completion(inbox: TelegramInbox) -> None:
    event = inbox.record(1, 1)
    job = running(inbox)
    key = receipt(inbox, job, "ack")
    assert inbox.event_state(event) == "open"
    with pytest.raises(InboxError, match="inbox_result_receipt_required"):
        finish(inbox, job, key)
    assert inbox.read_job(job["job_id"])["state"] == "running"


@pytest.mark.parametrize(
    "outcome,kind", [("answered", "result"), ("awaiting_client", "clarification"), ("handoff", "handoff")]
)
def test_terminal_outcome_requires_matching_verified_receipt(inbox: TelegramInbox, outcome: str, kind: str) -> None:
    inbox.record(1, 1)
    inbox.record(1, 2)
    job = running(inbox)
    key = receipt(inbox, job, kind)
    done = finish(inbox, job, key, outcome)
    assert done["state"] == "completed"
    assert done["outcome"] == outcome
    assert all(inbox.event_state(event_id) == "reply_verified" for event_id in job["event_ids"])
    assert inbox.claim(instruction_root=ROOT, instruction_revision=REVISION) is None


def test_unverified_receipt_and_unknown_effect_hold_completion(inbox: TelegramInbox) -> None:
    inbox.record(1, 1)
    job = running(inbox)
    good = receipt(inbox, job)
    event = inbox.resolve(job["event_ids"][0])
    mutation = inbox.reserve_effect(
        **guard(job), kind="mutation", text_sha256=None, peer_id=1, reply_to_message_id=event.message_id
    )
    inbox.mark_effect_intent(mutation)
    with pytest.raises(InboxError, match="inbox_effect_outcome_unknown"):
        finish(inbox, job, good)
    with pytest.raises(InboxError, match="inbox_receipt_unverified"):
        inbox.verify_effect(effect_key=mutation, receipt_key=mutation, message_id=9, verified=False)
    inbox.verify_effect(effect_key=mutation, receipt_key=mutation, message_id=9)
    assert finish(inbox, job, good)["state"] == "completed"


def test_completion_rejects_wrong_coverage_and_foreign_receipts(inbox: TelegramInbox) -> None:
    inbox.record(1, 1)
    inbox.record(1, 2)
    job = running(inbox)
    good = receipt(inbox, job)
    with pytest.raises(InboxError, match="inbox_coverage_invalid"):
        inbox.result(**guard(job), outcome="answered", event_ids=job["event_ids"][:1], receipt_keys=[good])
    with pytest.raises(InboxError, match="inbox_receipt_unverified"):
        inbox.result(**guard(job), outcome="answered", event_ids=job["event_ids"], receipt_keys=["foreign-receipt"])


@pytest.mark.parametrize("reason", ["duplicate", "spam", "reaction_only", "not_actionable"])
def test_no_reply_requires_fixed_reason_not_free_text(inbox: TelegramInbox, reason: str) -> None:
    event = inbox.record(1, 1)
    job = running(inbox)
    with pytest.raises(InboxError, match="inbox_no_reply_reason_invalid"):
        inbox.result(
            **guard(job), outcome="no_reply_needed", event_ids=[event], receipt_keys=[], reason_code="client said hello"
        )
    done = inbox.result(**guard(job), outcome="no_reply_needed", event_ids=[event], receipt_keys=[], reason_code=reason)
    assert done["outcome"] == "no_reply_needed"
    assert inbox.event_state(event) == "no_reply_needed"


def test_effect_identity_stays_stable_and_body_change_conflicts(inbox: TelegramInbox) -> None:
    inbox.record(1, 1)
    job = running(inbox)
    key = receipt(inbox, job)
    assert (
        inbox.reserve_effect(**guard(job), kind="result", text_sha256="b" * 64, peer_id=1, reply_to_message_id=1) == key
    )
    with pytest.raises(InboxError, match="inbox_effect_conflict"):
        inbox.reserve_effect(**guard(job), kind="result", text_sha256="c" * 64, peer_id=1, reply_to_message_id=1)
    with pytest.raises(InboxError, match="inbox_receipt_conflict"):
        inbox.verify_effect(effect_key=key, receipt_key="another-receipt", message_id=101)


def test_new_client_clarification_invalidates_prepared_result(inbox: TelegramInbox) -> None:
    inbox.record(1, 1)
    job = running(inbox)
    prepared = inbox.reserve_effect(**guard(job), kind="result", text_sha256="a" * 64, peer_id=1, reply_to_message_id=1)
    assert not inbox.read_effect(prepared)["attempted"]
    second = inbox.record(1, 2)
    with pytest.raises(InboxError, match="inbox_context_stale"):
        receipt(inbox, job)
    job = inbox.refresh_job(**guard(job), event_ids=[*job["event_ids"], second])
    assert job["context_revision"] == 2
    with pytest.raises(InboxError, match="inbox_effect_unavailable"):
        inbox.read_effect(prepared)
    key = receipt(inbox, job)
    assert finish(inbox, job, key)["event_ids"] == job["event_ids"]


def test_new_message_after_send_prevents_claiming_that_stale_answer_is_final(inbox: TelegramInbox) -> None:
    inbox.record(1, 1)
    job = running(inbox)
    key = receipt(inbox, job)
    inbox.record(1, 2)
    with pytest.raises(InboxError, match="inbox_context_stale"):
        finish(inbox, job, key)
    assert inbox.event_state(job["event_ids"][0]) == "open"


def test_multiple_verified_reply_slots_are_distinct_and_stable(inbox: TelegramInbox) -> None:
    inbox.record(1, 1)
    job = running(inbox)
    first = receipt(inbox, job)
    second = inbox.reserve_effect(
        **guard(job),
        kind="result",
        text_sha256="c" * 64,
        peer_id=1,
        reply_to_message_id=1,
        effect_slot=1,
    )
    assert second != first
    assert (
        inbox.reserve_effect(
            **guard(job),
            kind="result",
            text_sha256="c" * 64,
            peer_id=1,
            reply_to_message_id=1,
            effect_slot=1,
        )
        == second
    )
    inbox.mark_effect_intent(second)
    inbox.verify_effect(effect_key=second, receipt_key=second, message_id=102)
    done = inbox.result(**guard(job), outcome="answered", event_ids=job["event_ids"], receipt_keys=[first, second])
    assert len(done["effects"]) == 2


def test_unknown_effect_cannot_be_bypassed_by_slot_or_new_context(inbox: TelegramInbox) -> None:
    inbox.record(1, 1)
    job = running(inbox)
    first = inbox.reserve_effect(
        **guard(job),
        kind="result",
        text_sha256="b" * 64,
        peer_id=1,
        reply_to_message_id=1,
    )
    inbox.mark_effect_intent(first)
    with pytest.raises(InboxError, match="inbox_effect_outcome_unknown"):
        inbox.reserve_effect(
            **guard(job),
            kind="result",
            text_sha256="b" * 64,
            peer_id=1,
            reply_to_message_id=1,
            effect_slot=1,
        )
    second_event = inbox.record(1, 2)
    job = inbox.refresh_job(**guard(job), event_ids=[*job["event_ids"], second_event])
    with pytest.raises(InboxError, match="inbox_effect_outcome_unknown"):
        inbox.reserve_effect(
            **guard(job),
            kind="result",
            text_sha256="b" * 64,
            peer_id=1,
            reply_to_message_id=1,
        )
    assert len(inbox.read_job(job["job_id"])["effects"]) == 1


def test_two_prepared_slots_cannot_bypass_unknown_effect_at_reservation_or_attempt(inbox: TelegramInbox) -> None:
    inbox.record(1, 1)
    job = running(inbox)
    first = inbox.reserve_effect(**guard(job), kind="result", text_sha256="a" * 64, peer_id=1, reply_to_message_id=1)
    second = inbox.reserve_effect(
        **guard(job), kind="result", text_sha256="b" * 64, peer_id=1, reply_to_message_id=1, effect_slot=1
    )
    inbox.mark_effect_intent(first)
    generation = inbox.generation
    # Reconcile the original key; neither its identity nor its attempt changes.
    assert (
        inbox.reserve_effect(**guard(job), kind="result", text_sha256="a" * 64, peer_id=1, reply_to_message_id=1)
        == first
    )
    inbox.mark_effect_intent(first)
    with pytest.raises(InboxError, match="inbox_effect_outcome_unknown"):
        inbox.reserve_effect(
            **guard(job), kind="result", text_sha256="b" * 64, peer_id=1, reply_to_message_id=1, effect_slot=1
        )
    with pytest.raises(InboxError, match="inbox_effect_outcome_unknown"):
        inbox.mark_effect_intent(second)
    assert inbox.generation == generation
    assert inbox.read_effect(second)["attempted"] == 0
    inbox.verify_effect(effect_key=first, receipt_key=first, message_id=101)
    assert (
        inbox.reserve_effect(
            **guard(job), kind="result", text_sha256="b" * 64, peer_id=1, reply_to_message_id=1, effect_slot=1
        )
        == second
    )
    assert inbox.mark_effect_intent(second)["attempted"] == 1


@pytest.mark.parametrize(
    "outcome,kind",
    [("answered", "result"), ("awaiting_client", "clarification"), ("handoff", "handoff"), ("no_reply_needed", None)],
)
@pytest.mark.parametrize(
    "reason",
    [
        "synthetic customer body +12345",
        "syntheticcustomerpayload",
        "a" * 17,
        "business_result_verified",
        "arbitrary\nbody",
        "x" * 97,
        {},
        42,
        True,
    ],
)
def test_terminal_result_rejects_non_code_reasons_without_persisting(
    inbox: TelegramInbox, outcome: str, kind: str | None, reason: Any
) -> None:
    event = inbox.record(1, 1)
    job = running(inbox)
    keys = [] if kind is None else [receipt(inbox, job, kind)]
    generation = inbox.generation
    error = "inbox_no_reply_reason_invalid" if outcome == "no_reply_needed" else "inbox_reason_invalid"
    with pytest.raises(InboxError, match=error):
        inbox.result(**guard(job), outcome=outcome, event_ids=[event], receipt_keys=keys, reason_code=reason)
    actual = inbox.read_job(job["job_id"])
    assert actual["state"] == "running" and actual["reason_code"] is None
    assert inbox.event_state(event) == "open"
    assert inbox.generation == generation


@pytest.mark.parametrize(
    "outcome,kind", [("answered", "result"), ("awaiting_client", "clarification"), ("handoff", "handoff")]
)
def test_terminal_result_accepts_only_no_reason(inbox: TelegramInbox, outcome: str, kind: str) -> None:
    event = inbox.record(1, 1)
    job = running(inbox)
    key = receipt(inbox, job, kind)
    done = inbox.result(**guard(job), outcome=outcome, event_ids=[event], receipt_keys=[key], reason_code=None)
    assert done["state"] == "completed" and done["reason_code"] is None


def test_context_always_contains_anchor_after_many_recent_predecessors(inbox: TelegramInbox, clock: Clock) -> None:
    for message in range(1, 9):
        inbox.record(1, message)
        clock.advance(1)
    anchor = inbox.record(1, 9)
    clock.advance(1)
    next_message = inbox.record(1, 10)
    context = inbox.context(anchor, limit=5)
    assert len(context) == 5
    assert anchor in {item["event_id"] for item in context}
    assert next_message in {item["event_id"] for item in context}


def test_event_pages_cover_queue_without_oversized_single_response(inbox: TelegramInbox) -> None:
    expected = [inbox.record(1, message) for message in range(1, 304)]
    first = inbox.events()
    second = inbox.events(after_event_id=first[-1]["event_id"])
    third = inbox.events(after_event_id=second[-1]["event_id"], limit=256)
    assert [item["event_id"] for item in first + second + third] == expected
    assert inbox.events(after_event_id=third[-1]["event_id"]) == []


def test_capacity_retains_all_accepted_references_and_persists_gap(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "inbox" / "inbound.sqlite3"
    with TelegramInbox(path, max_open_events=2, clock=clock) as queue:
        queue.duty_set(True)
        accepted = [queue.record(1, number) for number in (1, 2)]
        with pytest.raises(InboxError, match="inbound_monitor_full"):
            queue.record(1, 3)
        with pytest.raises(InboxError, match="inbound_monitor_full"):
            queue.record(1, 4)
        assert queue.gaps()[0]["message_id"] == 3
        assert queue.status()["dropped_open_events"] == 0
        assert queue.status()["rejected_events"] == 2
    with TelegramInbox(path, max_open_events=2, clock=clock) as restored:
        assert [restored.resolve(event).message_id for event in accepted] == [1, 2]
        job = running(restored)
        finish(restored, job, receipt(restored, job))
        assert restored.record(1, 3)
        restored.clear_gap(peer_id=1, through_message_id=2)
        assert restored.gaps()
        restored.clear_gap(peer_id=1, through_message_id=4)
        assert restored.gaps() == []


def test_pruning_removes_only_completed_after_retention(inbox: TelegramInbox, clock: Clock) -> None:
    old = inbox.record(1, 1)
    job = running(inbox)
    finish(inbox, job, receipt(inbox, job))
    inbox.advance_checkpoint(1, 1)
    unresolved = inbox.record(2, 1)
    clock.advance(7 * 24 * 3600 - 1)
    assert inbox.prune() == 0
    clock.advance(2)
    assert inbox.prune() == 1
    with pytest.raises(InboxError, match="inbound_event_unavailable"):
        inbox.resolve(old)
    assert inbox.require_open(unresolved)
    assert inbox.status()["open_events"] == 1
    assert inbox.status()["completed_jobs"] == 0


@pytest.mark.parametrize("duty_enabled", [False, True])
def test_retention_preserves_dedup_until_history_covers_gaps(
    inbox: TelegramInbox, clock: Clock, duty_enabled: bool
) -> None:
    old = inbox.record(1, 10)
    job = running(inbox)
    finish(inbox, job, receipt(inbox, job))
    inbox.advance_checkpoint(1, 4)
    inbox.mark_gap(peer_id=1, message_id=5, received_at=clock.value.isoformat(), reason_code="capacity")
    if not duty_enabled:
        inbox.duty_set(False)
    gap = inbox.gaps()
    clock.advance(7 * 24 * 3600 + 1)

    assert inbox.prune() == 0
    assert inbox.resolve(old).message_id == 10
    assert inbox.event_state(old) == "reply_verified"
    assert inbox.checkpoint(1) == 4 and inbox.gaps() == gap

    # Only history recovery may cover the missing prefix; pruning never does.
    inbox.advance_checkpoint(1, 10)
    inbox.clear_gap(peer_id=1, through_message_id=10)
    inbox.mark_gap(peer_id=1, message_id=11, received_at=clock.value.isoformat(), reason_code="capacity")
    later_gap = inbox.gaps()
    assert inbox.prune() == 1
    assert inbox.checkpoint(1) == 10 and inbox.gaps() == later_gap
    with pytest.raises(InboxError, match="inbound_event_unavailable"):
        inbox.resolve(old)


def test_checkpoint_never_moves_backwards(inbox: TelegramInbox) -> None:
    assert inbox.checkpoint(1) == 0
    inbox.advance_checkpoint(1, 20)
    inbox.advance_checkpoint(1, 10)
    assert inbox.checkpoint(1) == 20


def test_pruned_old_message_below_durable_checkpoint_is_not_replayed(inbox: TelegramInbox, clock: Clock) -> None:
    inbox.record(1, 1)
    job = running(inbox)
    finish(inbox, job, receipt(inbox, job))
    inbox.advance_checkpoint(1, 1)
    clock.advance(7 * 24 * 3600 + 1)
    inbox.prune()
    assert inbox.record(1, 1) is None
    assert inbox.record(1, 2)


def test_failed_database_write_rolls_back_reference_and_generation(inbox: TelegramInbox) -> None:
    before = inbox.generation
    inbox._db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
    inbox._db.commit()
    with pytest.raises(InboxError, match="inbox_storage_failed"):
        inbox.record(1, 1)
    assert inbox.generation == before
    assert inbox.status()["open_events"] == 0
    assert inbox._db.execute("SELECT COUNT(*) FROM peers").fetchone()[0] == 0


def test_import_preserves_opaque_id_and_holds_legacy_ack_closures(inbox: TelegramInbox, clock: Clock) -> None:
    references = [
        {
            "event_id": "inbound-100",
            "peer_id": 1,
            "message_id": 3,
            "received_at": clock.value.isoformat(),
            "state": "reply_verified",
        }
    ]
    result = inbox.import_references(references, instruction_root=ROOT, instruction_revision=REVISION)
    assert result["imported"] == 1
    assert inbox.resolve("inbound-100").message_id == 3
    assert inbox.require_open("inbound-100")
    assert inbox.active_jobs()[0]["reason_code"] == "legacy_import_review_required"
    assert inbox.claim(instruction_root=ROOT, instruction_revision=REVISION) is None
    assert inbox.import_references(references, instruction_root=ROOT, instruction_revision=REVISION)["imported"] == 0


def test_import_conflict_rolls_back_entire_batch(inbox: TelegramInbox, clock: Clock) -> None:
    inbox.import_references(
        [
            {
                "event_id": "inbound-100",
                "peer_id": 1,
                "message_id": 1,
                "received_at": clock.value.isoformat(),
                "state": "open",
            }
        ],
        instruction_root=ROOT,
        instruction_revision=REVISION,
    )
    generation = inbox.generation
    with pytest.raises(InboxError, match="inbound_import_conflict"):
        inbox.import_references(
            [
                {
                    "event_id": "inbound-200",
                    "peer_id": 2,
                    "message_id": 2,
                    "received_at": clock.value.isoformat(),
                    "state": "open",
                },
                {
                    "event_id": "inbound-100",
                    "peer_id": 3,
                    "message_id": 1,
                    "received_at": clock.value.isoformat(),
                    "state": "open",
                },
            ],
            instruction_root=ROOT,
            instruction_revision=REVISION,
        )
    assert inbox.generation == generation
    with pytest.raises(InboxError, match="inbound_event_unavailable"):
        inbox.resolve("inbound-200")
    assert inbox.resolve("inbound-100").peer_id == 1


def test_import_rejects_content_fields(inbox: TelegramInbox, clock: Clock) -> None:
    with pytest.raises(InboxError, match="inbox_import_invalid"):
        inbox.import_references(
            [
                {
                    "event_id": "inbound-100",
                    "peer_id": 1,
                    "message_id": 1,
                    "received_at": clock.value.isoformat(),
                    "state": "open",
                    "text": "synthetic body",
                }
            ],
            instruction_root=ROOT,
            instruction_revision=REVISION,
        )
    assert inbox.status()["open_events"] == 0


def test_manual_recovery_does_not_retry_unknown_actions(inbox: TelegramInbox) -> None:
    inbox.record(1, 1)
    job = running(inbox)
    receipt(inbox, job, "ack")
    job = inbox.needs_review(**guard(job), reason_code="codex_turn_failed")
    job = review_stopped(inbox, job)
    with pytest.raises(InboxError, match="inbox_resolution_review_required"):
        inbox.resolve_job(**guard(job), decision="retry_no_effects")
    with pytest.raises(InboxError, match="inbox_retry_effects_present"):
        inbox.resolve_job(**guard(job), decision="retry_no_effects", effects_reviewed=True)
    resumed = inbox.resolve_job(**guard(job), decision="continue_bound_turn", effects_reviewed=True)
    assert resumed["state"] == "running"
    assert resumed["turn_id"] == "turn-1"


def test_explicitly_reviewed_retry_releases_only_harmless_preparations(inbox: TelegramInbox) -> None:
    event = inbox.record(1, 1)
    job = running(inbox)
    key = inbox.reserve_effect(**guard(job), kind="result", text_sha256="b" * 64, peer_id=1, reply_to_message_id=1)
    job = inbox.needs_review(**guard(job), reason_code="codex_turn_failed")
    job = review_stopped(inbox, job)
    with pytest.raises(InboxError, match="inbox_resolution_review_required"):
        inbox.resolve_job(**guard(job), decision="retry_no_effects")
    assert inbox.read_effect(key)["attempted"] == 0
    released = inbox.resolve_job(**guard(job), decision="retry_no_effects", effects_reviewed=True)
    assert released["state"] == "released" and released["effects"] == []
    assert inbox.require_open(event)
    with pytest.raises(InboxError, match="inbox_effect_unavailable"):
        inbox.read_effect(key)
    inbox.set_execution_paused(paused=False, expected_revision=inbox.status()["execution_revision"])
    retried = claim(inbox)
    assert retried["event_ids"] == [event] and retried["job_id"] != job["job_id"]


@pytest.mark.parametrize("verified", [False, True])
def test_reviewed_retry_retains_unknown_or_verified_external_effects(inbox: TelegramInbox, verified: bool) -> None:
    inbox.record(1, 1)
    job = running(inbox)
    key = inbox.reserve_effect(**guard(job), kind="result", text_sha256="b" * 64, peer_id=1, reply_to_message_id=1)
    inbox.mark_effect_intent(key)
    if verified:
        inbox.verify_effect(effect_key=key, receipt_key=key, message_id=101)
    job = inbox.needs_review(**guard(job), reason_code="codex_turn_failed")
    job = review_stopped(inbox, job)
    generation = inbox.generation
    error = "inbox_retry_effects_present" if verified else "inbox_effect_outcome_unknown"
    with pytest.raises(InboxError, match=error):
        inbox.resolve_job(**guard(job), decision="retry_no_effects", effects_reviewed=True)
    assert inbox.read_job(job["job_id"])["state"] == "needs_review"
    assert inbox.read_effect(key)["attempted"] == 1
    assert inbox.generation == generation


def test_active_job_pages_cover_2000_holds_without_full_payloads(tmp_path: Path, clock: Clock) -> None:
    with TelegramInbox(tmp_path / "inbox" / "inbound.sqlite3", clock=clock) as queue:
        # A long valid physical path must never multiply the IPC page size.
        root = "/" + "r" * 4095
        queue.configure_instructions(root, REVISION)
        queue.duty_set(True)
        references = [
            {
                "event_id": f"inbound-{number}",
                "peer_id": number,
                "message_id": number,
                "received_at": clock.value.isoformat(),
                "state": "open",
            }
            for number in range(1, 2001)
        ]
        imported = queue.import_references(references, instruction_root=root, instruction_revision=REVISION)
        expected = imported["review_job_ids"]
        actual: list[str] = []
        cursor = None
        pages = 0
        while True:
            page = queue.active_job_page(limit=100, after_job_id=cursor)
            assert 0 < len(page["jobs"]) <= 100
            assert len(json.dumps({"ok": True, **page}).encode()) < 128 * 1024
            for summary in page["jobs"]:
                assert "effects" not in summary and "instruction_root" not in summary
                assert summary["state"] == "needs_review"
                assert summary["event_ids"] and summary["lease"] and summary["revision"] == 1
                actual.append(summary["job_id"])
            pages += 1
            cursor = page["next_job_id"]
            if cursor is None:
                break
            assert cursor == page["jobs"][-1]["job_id"]
        assert pages == 20 and actual == expected and len(set(actual)) == 2000
        # Internal recovery helpers retain their full details and old semantics.
        assert queue.active_jobs()[0]["instruction_root"] == root


def test_active_job_summary_excludes_effect_history(inbox: TelegramInbox) -> None:
    inbox.record(1, 1)
    job = running(inbox)
    for slot in range(32):
        key = inbox.reserve_effect(
            **guard(job), kind="ack", text_sha256="b" * 64, peer_id=1, reply_to_message_id=1, effect_slot=slot
        )
        inbox.verify_effect(effect_key=key, receipt_key=key, message_id=101 + slot)
    full = inbox.read_job(job["job_id"])
    summary = inbox.active_job_page()["jobs"][0]
    assert len(full["effects"]) == 32 and "effects" not in summary
    assert len(json.dumps(summary)) < len(json.dumps(full)) / 10
    for field in ("job_id", "lease", "revision", "event_ids", "state", "thread_id", "turn_id", "reason_code"):
        assert summary[field] == full[field]


def test_active_job_cursor_survives_previous_job_leaving_active_state(inbox: TelegramInbox, clock: Clock) -> None:
    inbox.configure_instructions(ROOT, REVISION)
    clock.value += timedelta(microseconds=500000)
    inbox.duty_set(False)
    stamp = clock.value.replace(microsecond=0).isoformat()
    for peer in range(1, 4):
        inbox.record(peer, 1, stamp, timestamp_precision="second")
    first = inbox.active_job_page(limit=1)
    cursor = first["next_job_id"]
    assert cursor == first["jobs"][0]["job_id"]
    job = inbox.read_job(cursor)
    inbox.set_execution_paused(paused=True, expected_revision=inbox.status()["execution_revision"])
    inbox.resolve_job(**guard(job), decision="retry_no_effects", effects_reviewed=True)
    remaining = inbox.active_job_page(limit=100, after_job_id=cursor)
    assert len(remaining["jobs"]) == 2 and remaining["next_job_id"] is None
    assert cursor not in {item["job_id"] for item in remaining["jobs"]}


@pytest.mark.parametrize("limit", [True, 0, 101, "50", None])
def test_active_job_page_rejects_invalid_limits(inbox: TelegramInbox, limit: Any) -> None:
    with pytest.raises(InboxError, match="inbox_page_limit_invalid"):
        inbox.active_job_page(limit=limit)


def test_active_job_page_validates_cursor_and_handles_empty_tail(inbox: TelegramInbox) -> None:
    assert inbox.active_job_page() == {"jobs": [], "next_job_id": None}
    with pytest.raises(InboxError, match="inbox_job_cursor_invalid"):
        inbox.active_job_page(after_job_id="arbitrary body")
    with pytest.raises(InboxError, match="inbox_job_cursor_unavailable"):
        inbox.active_job_page(after_job_id="job-" + "f" * 32)
    inbox.record(1, 1)
    job = claim(inbox)
    assert inbox.active_job_page(after_job_id=job["job_id"]) == {"jobs": [], "next_job_id": None}


def _health_snapshot(path: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(path)) as db:
        return {
            "health": db.execute("SELECT * FROM health ORDER BY component").fetchall(),
            "incidents": db.execute("SELECT * FROM incidents ORDER BY opened_at,rowid").fetchall(),
            "generation": int(db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()[0]),
        }


@pytest.mark.parametrize("old_code", [None, "transport_lost"], ids=["first_incident", "changed_cause"])
def test_failed_health_write_rolls_back_all_incident_changes(
    tmp_path: Path, clock: Clock, old_code: str | None
) -> None:
    path = tmp_path / "inbox" / "inbound.sqlite3"
    old_state = "ready" if old_code is None else "offline"
    with TelegramInbox(path, clock=clock) as inbox:
        inbox.set_health("codex", old_state, old_code)
        if old_code is not None:
            error = inbox.pending_notifications()[0]
            inbox.notification_verified(error["notification_id"], "receipt-old-error")
        before = _health_snapshot(path)
        clock.advance(1)
        inbox._db.execute(
            "CREATE TRIGGER fail_health_write BEFORE INSERT ON health BEGIN SELECT RAISE(ABORT, 'synthetic'); END"
        )
        inbox._db.commit()
        with pytest.raises(InboxError, match="inbox_storage_failed"):
            inbox.set_health("codex", "held", "rpc_error")
        assert _health_snapshot(path) == before
        inbox._db.execute("DROP TRIGGER fail_health_write")
        inbox._db.commit()
        inbox.set_health("codex", old_state, old_code)
        assert _health_snapshot(path) == before and inbox.pending_notifications() == []

        inbox.set_health("codex", "held", "rpc_error")
        pending = inbox.pending_notifications()
        expected_kinds = ["error"] if old_code is None else ["recovery", "error"]
        assert [item["kind"] for item in pending] == expected_kinds
        assert pending[-1]["code"] == "codex_rpc_error" and pending[-1]["recovered_at"] is None
        if old_code is not None:
            assert pending[0]["incident_id"] == error["incident_id"]
            assert pending[0]["opened_receipt"] == "receipt-old-error"
            assert pending[0]["recovered_at"] is not None
        after = _health_snapshot(path)
        assert after["generation"] == before["generation"] + 1
    with TelegramInbox(path, clock=clock) as restored:
        assert restored.health()["codex"] == {"state": "held", "error_code": "rpc_error"}
        assert restored.pending_notifications() == pending
    assert _health_snapshot(path) == after


def test_incident_and_recovery_notifications_survive_restart_once(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "inbox" / "inbound.sqlite3"
    with TelegramInbox(path, clock=clock) as first:
        first.set_health("codex", "offline", "transport_lost")
        first.set_health("codex", "offline", "transport_lost")
        pending = first.pending_notifications()
        assert len(pending) == 1
        notification = pending[0]["notification_id"]
        first.set_health("codex", "ready")
        assert first.pending_notifications()[0]["kind"] == "error"
    with TelegramInbox(path, clock=clock) as restored:
        restored.notification_verified(notification, "receipt-error")
        pending = restored.pending_notifications()
        assert len(pending) == 1 and pending[0]["kind"] == "recovery"
        restored.notification_verified(pending[0]["notification_id"], "receipt-recovery")
        assert restored.pending_notifications() == []
        restored.set_health("codex", "ready")
        assert restored.pending_notifications() == []


@pytest.mark.parametrize("component", ["codex", "telegram", "intake"])
@pytest.mark.parametrize("states", [("offline", "offline"), ("offline", "held"), ("held", "offline")])
@pytest.mark.parametrize("prefixed_first", [False, True])
def test_health_error_alias_preserves_incident_and_raw_api(
    inbox: TelegramInbox, clock: Clock, component: str, states: tuple[str, str], prefixed_first: bool
) -> None:
    codes = ("turn_failed", component + "_turn_failed")
    if prefixed_first:
        codes = codes[::-1]
    inbox.set_health(component, states[0], codes[0])
    notification = inbox.pending_notifications()[0]
    assert notification["code"] == component + "_turn_failed"
    inbox.notification_verified(notification["notification_id"], "receipt-error")
    clock.advance(1)
    health = inbox.set_health(component, states[1], codes[1])
    assert health[component] == {"state": states[1], "error_code": codes[1]}
    current = inbox.read_incident(notification["incident_id"])
    assert current["opened_at"] == notification["opened_at"]
    assert current["opened_receipt"] == "receipt-error"
    assert current["recovered_at"] is None
    assert inbox.pending_notifications() == []
    assert (
        inbox._db.execute("SELECT incident_id FROM health WHERE component=?", (component,)).fetchone()[0]
        == current["incident_id"]
    )
    assert inbox._db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1


def test_health_alias_keeps_legacy_incident_and_receipt_after_restart(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "inbox" / "inbound.sqlite3"
    with TelegramInbox(path, clock=clock) as first:
        first.set_health("codex", "offline", "codex_turn_failed")
        notification = first.pending_notifications()[0]
        first.notification_verified(notification["notification_id"], "receipt-error")
        # Simulate the already installed prefixing behavior without changing
        # any runtime database or rewriting historical production receipts.
        with first._write():
            first._db.execute(
                "UPDATE incidents SET reason_code=? WHERE incident_id=?",
                ("codex_codex_turn_failed", notification["incident_id"]),
            )
    with TelegramInbox(path, clock=clock) as restored:
        assert restored.set_health("codex", "held", "turn_failed")["codex"] == {
            "state": "held",
            "error_code": "turn_failed",
        }
        incident = restored.read_incident(notification["incident_id"])
        assert incident["reason_code"] == "codex_codex_turn_failed"
        assert incident["recovered_at"] is None and incident["opened_receipt"] == "receipt-error"
        assert restored.pending_notifications() == []
        assert restored._db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1
        restored.set_health("codex", "ready")
        pending = restored.pending_notifications()
        assert len(pending) == 1 and pending[0]["kind"] == "recovery"
        assert pending[0]["incident_id"] == notification["incident_id"]


@pytest.mark.parametrize("next_state", ["offline", "held"])
def test_health_distinct_cause_still_recovers_previous_and_opens_new(
    inbox: TelegramInbox, clock: Clock, next_state: str
) -> None:
    inbox.set_health("codex", "offline", "turn_failed")
    first = inbox.pending_notifications()[0]
    inbox.notification_verified(first["notification_id"], "receipt-error")
    clock.advance(1)
    health = inbox.set_health("codex", next_state, "codex_rpc_error")
    assert health["codex"] == {"state": next_state, "error_code": "codex_rpc_error"}
    pending = inbox.pending_notifications()
    assert [item["kind"] for item in pending] == ["recovery", "error"]
    assert pending[0]["incident_id"] == first["incident_id"]
    assert pending[1]["incident_id"] != first["incident_id"]
    assert pending[1]["code"] == "codex_rpc_error" and pending[1]["recovered_at"] is None


def test_default_health_error_uses_same_incident_as_unprefixed_alias(inbox: TelegramInbox) -> None:
    assert inbox.set_health("codex", "offline")["codex"]["error_code"] == "codex_offline"
    first = inbox.pending_notifications()[0]
    assert first["code"] == "codex_offline"
    inbox.set_health("codex", "offline", "offline")
    pending = inbox.pending_notifications()
    assert len(pending) == 1 and pending[0]["incident_id"] == first["incident_id"]
    assert pending[0]["recovered_at"] is None


def test_component_incidents_are_independent(inbox: TelegramInbox) -> None:
    inbox.set_health("codex", "offline", "transport_lost")
    inbox.set_health("telegram", "offline", "transport_lost")
    assert len(inbox.pending_notifications()) == 2
    inbox.set_health("codex", "ready")
    assert inbox.health()["telegram"]["state"] == "offline"
    assert sum(item["recovered_at"] is None for item in inbox.pending_notifications()) == 1


@pytest.mark.parametrize("code", ["turn_failed", "codex_turn_failed"])
def test_incident_code_has_one_component_prefix(inbox: TelegramInbox, code: str) -> None:
    inbox.set_health("codex", "held", code)
    assert [item["code"] for item in inbox.pending_notifications()] == ["codex_turn_failed"]


def test_normal_busy_or_paused_worker_does_not_create_incident(inbox: TelegramInbox) -> None:
    for state in ("ready", "running", "waiting_thread", "paused", "ready"):
        inbox.set_health("codex", state)
    assert inbox.pending_notifications() == []


def test_dialogue_traversal_cursor_survives_restart_without_access_hash(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "inbox" / "inbound.sqlite3"
    with TelegramInbox(path, clock=clock) as first:
        assert first.reconciliation_cursor() is None
        first.set_reconciliation_cursor(1, 25, clock.value.isoformat())
        generation = first.generation
        first.set_reconciliation_cursor(1, 25, clock.value.isoformat())
        assert first.generation == generation
    with TelegramInbox(path, clock=clock) as restored:
        cursor = restored.reconciliation_cursor()
        assert cursor == {"peer_id": 1, "message_id": 25, "received_at": clock.value.isoformat(timespec="microseconds")}
        restored.clear_reconciliation_cursor()
        assert restored.reconciliation_cursor() is None


@pytest.mark.parametrize("value", [True, 0, -1, "1", 1.5, None])
def test_rejects_non_locator_values_without_persisting(inbox: TelegramInbox, value: Any) -> None:
    with pytest.raises(InboxError):
        inbox.record(value, 1)
    assert inbox.status()["retained_events"] == 0


def test_malformed_rpc_values_raise_fixed_errors_not_type_errors(inbox: TelegramInbox) -> None:
    inbox.record(1, 1)
    job = running(inbox)
    with pytest.raises(InboxError, match="inbound_event_invalid"):
        inbox.result(**guard(job), outcome="answered", event_ids=[{}], receipt_keys=[])
    with pytest.raises(InboxError, match="inbox_health_state_invalid"):
        inbox.set_health("codex", [])
    with pytest.raises(InboxError, match="inbox_reason_invalid"):
        inbox.needs_review(**guard(job), reason_code="an arbitrary message body")


def test_rejects_symlinked_database_and_shared_root_directory(tmp_path: Path) -> None:
    directory = tmp_path / "inbox"
    directory.mkdir()
    target = directory / "target"
    target.touch()
    (directory / "inbound.sqlite3").symlink_to(target)
    with pytest.raises(InboxError, match="inbox_directory_invalid"):
        TelegramInbox(directory / "inbound.sqlite3")
    with pytest.raises(InboxError, match="inbox_directory_invalid"):
        TelegramInbox(Path("/tmp/inbound.sqlite3"))
    assert os.stat(target).st_size == 0


def test_database_does_not_have_message_or_media_columns(inbox: TelegramInbox) -> None:
    inbox.record(1, 1)
    with sqlite3.connect(inbox.path) as read_only:
        columns = {
            row[1]
            for table in ("events", "jobs", "effects", "incidents")
            for row in read_only.execute(f"PRAGMA table_info({table})")
        }
    assert columns.isdisjoint({"text", "body", "media", "transcript", "name", "vin", "phone"})
