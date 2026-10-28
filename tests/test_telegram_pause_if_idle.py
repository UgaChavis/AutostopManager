from contextlib import contextmanager

import pytest

from autostop_manager.telegram_inbox import InboxError, TelegramInbox

ROOT = "/opt/test-runtime"
REVISION = "a" * 40


def test_busy_claim_and_stale_revision_do_not_change_control(tmp_path):
    with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
        inbox.duty_set(True)
        inbox.record(1, 11)
        job = inbox.claim(instruction_root=ROOT, instruction_revision=REVISION)
        before = inbox.status()
        with pytest.raises(InboxError, match="inbox_execution_slot_occupied"):
            inbox.pause_execution_if_idle(expected_revision=before["execution_revision"])
        assert inbox.status()["execution_paused"] is False
        assert inbox.read_job(job["job_id"])["state"] == "claimed"
        with pytest.raises(InboxError, match="inbox_execution_revision_conflict"):
            inbox.pause_execution_if_idle(expected_revision=before["execution_revision"] + 1)


def test_pause_wins_between_claim_precheck_and_transaction(tmp_path, monkeypatch):
    with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
        inbox.duty_set(True)
        inbox.record(1, 11)
        original = inbox._write
        fired = False

        @contextmanager
        def interleaved():
            nonlocal fired
            if not fired:
                fired = True
                inbox.pause_execution_if_idle(expected_revision=inbox.status()["execution_revision"])
            with original():
                yield

        monkeypatch.setattr(inbox, "_write", interleaved)
        assert inbox.claim(instruction_root=ROOT, instruction_revision=REVISION) is None
        assert inbox.status()["execution_paused"] is True
        assert inbox.status()["global_slot"] is None
        assert inbox.status()["open_events"] == 1


def test_pause_preserves_held_legacy_jobs(tmp_path):
    with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
        inbox.duty_set(True)
        revision = inbox.status()["execution_revision"]
        status = inbox.pause_execution_if_idle(expected_revision=revision)
        assert status["execution_paused"] is True
        assert status["execution_revision"] == revision + 1
        assert inbox.pause_execution_if_idle(expected_revision=revision + 1)["execution_revision"] == revision + 1


def test_legacy_without_stop_proof_blocks_pause_without_mutation(tmp_path):
    with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
        inbox.duty_set(True)
        event = inbox.record(1, 12)
        job = inbox.claim(instruction_root=ROOT, instruction_revision=REVISION)
        with inbox._write():
            inbox._db.execute("DELETE FROM global_slot WHERE job_id=?", (job["job_id"],))
            inbox._db.execute(
                "UPDATE jobs SET context_mode='legacy', execution_proof=NULL WHERE job_id=?", (job["job_id"],)
            )
        before = inbox.read_job(job["job_id"])
        revision = inbox.status()["execution_revision"]
        with pytest.raises(InboxError, match="inbox_legacy_execution_unproven"):
            inbox.pause_execution_if_idle(expected_revision=revision)
        assert inbox.read_job(job["job_id"]) == before
        assert inbox.resolve(event).message_id == 12
        assert inbox.status()["execution_paused"] is False
        with inbox._write():
            inbox._db.execute("UPDATE jobs SET execution_proof='not_started' WHERE job_id=?", (job["job_id"],))
        assert inbox.pause_execution_if_idle(expected_revision=revision)["execution_paused"] is True
        assert inbox.read_job(job["job_id"])["state"] == "claimed"


def test_storage_failure_rolls_back_pause(tmp_path):
    with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
        inbox.duty_set(True)
        event = inbox.record(1, 13)
        before = inbox.status()
        inbox._db.execute(
            "CREATE TRIGGER reject_pause BEFORE UPDATE ON metadata WHEN NEW.key='execution_revision' BEGIN SELECT RAISE(ABORT, 'synthetic'); END"
        )
        with pytest.raises(InboxError, match="inbox_storage_failed"):
            inbox.pause_execution_if_idle(expected_revision=before["execution_revision"])
        assert inbox.status() == before
        assert inbox.resolve(event).message_id == 13
