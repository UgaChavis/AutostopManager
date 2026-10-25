"""Recent retained history must not add a full scan to every new locator."""

from datetime import UTC, datetime, timedelta

import pytest

from autostop_manager.telegram_inbox import TelegramInbox


NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)
RECENT = (NOW - timedelta(hours=1)).isoformat()


def intake_work(path, retained, *, reopen):
    inbox = TelegramInbox(path, clock=lambda: NOW)
    try:
        inbox.duty_set(True)
        with inbox._db:
            inbox._db.execute("INSERT INTO peers VALUES (1,?)", (retained,))
            inbox._db.executemany(
                "INSERT INTO jobs(job_id,peer_id,state,revision,lease,context_revision,instruction_root,"
                "instruction_revision,created_at,updated_at,context_mode) "
                "VALUES (?,1,'completed',1,'synthetic-lease',1,'/synthetic/instructions',?,?,?,'peer_v1')",
                ((f"job-{number:032x}", "a" * 40, RECENT, RECENT) for number in range(1, retained + 1)),
            )
            inbox._db.executemany(
                "INSERT INTO events(sequence,peer_id,message_id,received_at,state,context_revision,job_id,completed_at) "
                "VALUES (?,1,?,?,'no_reply_needed',1,?,?)",
                ((str(number), number, RECENT, f"job-{number:032x}", RECENT) for number in range(1, retained + 1)),
            )
            inbox._db.execute("INSERT INTO checkpoints VALUES (1,?,?)", (retained, RECENT))
        if reopen:
            # Existing queues acquire access paths at startup without rewriting
            # their locators, controls or schema-version contract.
            with inbox._db:
                names = [
                    row["name"]
                    for table in ("events", "jobs")
                    for row in inbox._db.execute(f"PRAGMA index_list({table})")
                    if not row["unique"]
                ]
                for name in names:
                    inbox._db.execute('DROP INDEX "' + name.replace('"', '""') + '"')
            inbox.close()
            inbox = TelegramInbox(path, clock=lambda: NOW)

        generation = inbox.generation
        work = 0

        def tick():
            nonlocal work
            work += 1
            return 0

        inbox._db.set_progress_handler(tick, 1)
        try:
            event_id = inbox.record(2, 1)
        finally:
            inbox._db.set_progress_handler(None, 0)
        assert inbox.require_open(event_id).message_id == 1
        assert inbox.generation == generation + 1
        assert inbox._meta("schema_version") == "3"
        assert inbox._db.execute("SELECT COUNT(*) FROM events WHERE peer_id=1").fetchone()[0] == retained
        assert inbox._db.execute("SELECT COUNT(*) FROM jobs WHERE state='completed'").fetchone()[0] == retained
        assert inbox.checkpoint(1) == retained
        assert inbox.execution_slot() is None
        assert not list(inbox._db.execute("PRAGMA foreign_key_check"))
        return work
    finally:
        inbox.close()


@pytest.mark.parametrize("reopen", [False, True], ids=["new-queue", "existing-queue"])
def test_new_intake_does_not_scan_recent_completed_history(tmp_path, reopen):
    small = intake_work(tmp_path / "small" / "inbox.sqlite3", 32, reopen=reopen)
    large = intake_work(tmp_path / "large" / "inbox.sqlite3", 4096, reopen=reopen)
    # VM work, not wall time: 128 times more recent history has no expired rows
    # to prune and should not require a comparable increase in intake work.
    assert large <= small * 3, (small, large)
