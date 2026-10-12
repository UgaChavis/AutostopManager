from __future__ import annotations

import os
import sqlite3
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
    job = inbox.start_intent(**guard(job), thread_id="thread-1")
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
    return inbox.result(**guard(job), outcome=outcome, event_ids=job["event_ids"], receipt_keys=[key])


def test_sqlite_storage_is_private_durable_and_one_writer(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "inbox" / "inbound.sqlite3"
    first = TelegramInbox(path, clock=clock)
    first.duty_set(True)
    event_id = first.record(1, 11)
    assert event_id
    generation = first.generation
    assert first._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert first._db.execute("PRAGMA synchronous").fetchone()[0] == 2
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
        restored.resolve_job(**guard(held), decision="retry_no_effects", effects_reviewed=True)
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


def test_configured_owner_is_unbatched(inbox: TelegramInbox) -> None:
    first = inbox.record(7, 1)
    inbox.record(7, 2)
    job = claim(inbox, unbatched_peer_id=7)
    assert job["event_ids"] == [first]


def test_claim_lease_and_revision_prevent_stale_or_unauthorized_transition(inbox: TelegramInbox) -> None:
    inbox.record(1, 1)
    job = claim(inbox)
    with pytest.raises(InboxError, match="inbox_lease_mismatch"):
        inbox.start_intent(job_id=job["job_id"], lease="wrong", expected_revision=job["revision"], thread_id="thread-1")
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


def test_no_reply_requires_fixed_reason_not_free_text(inbox: TelegramInbox) -> None:
    event = inbox.record(1, 1)
    job = running(inbox)
    with pytest.raises(InboxError, match="inbox_no_reply_reason_invalid"):
        inbox.result(
            **guard(job), outcome="no_reply_needed", event_ids=[event], receipt_keys=[], reason_code="client said hello"
        )
    done = inbox.result(
        **guard(job), outcome="no_reply_needed", event_ids=[event], receipt_keys=[], reason_code="reaction_only"
    )
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
    with pytest.raises(InboxError, match="inbox_resolution_review_required"):
        inbox.resolve_job(**guard(job), decision="retry_no_effects")
    with pytest.raises(InboxError, match="inbox_retry_effects_present"):
        inbox.resolve_job(**guard(job), decision="retry_no_effects", effects_reviewed=True)
    resumed = inbox.resolve_job(**guard(job), decision="continue_bound_turn", effects_reviewed=True)
    assert resumed["state"] == "running"
    assert resumed["turn_id"] == "turn-1"


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


def test_component_incidents_are_independent(inbox: TelegramInbox) -> None:
    inbox.set_health("codex", "offline", "transport_lost")
    inbox.set_health("telegram", "offline", "transport_lost")
    assert len(inbox.pending_notifications()) == 2
    inbox.set_health("codex", "ready")
    assert inbox.health()["telegram"]["state"] == "offline"
    assert sum(item["recovered_at"] is None for item in inbox.pending_notifications()) == 1


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
