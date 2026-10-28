"""Queue telemetry stays indexed as completed client history grows."""

from datetime import UTC, datetime

import pytest

from autostop_manager.telegram_inbox import TelegramInbox

NOW = datetime(2026, 10, 24, 9, tzinfo=UTC)


def seed_history(inbox, retained=1000):
    jobs = [
        (2, "completed", "peer_v1"),
        (3, "running", "peer_v1"),
        (4, "needs_review", "peer_v1"),
        (5, "needs_review", "peer_v1"),
        (5, "needs_review", "peer_v1"),
        (6, "needs_review", "legacy"),
        (10, "needs_review", "peer_v1"),
        (10, "running", "legacy"),
        (11, "completed", "legacy"),
        (12, "running", "legacy"),
    ]
    jobs.extend((peer, "completed", "peer_v1") for peer in range(10000, 10000 + retained))
    peers = set(range(1, 13)) | {peer for peer, _state, _mode in jobs}
    with inbox._db:
        inbox._db.executemany("INSERT INTO peers VALUES (?,1)", ((peer,) for peer in sorted(peers)))
        inbox._db.executemany(
            "INSERT INTO jobs(job_id,peer_id,state,revision,lease,context_revision,instruction_root,"
            "instruction_revision,created_at,updated_at,context_mode) VALUES (?,?,?,1,?,1,?,?,?,?,?)",
            (
                (
                    f"job-{number:032x}",
                    peer,
                    state,
                    "synthetic-lease",
                    "/opt/synthetic-instructions",
                    "a" * 40,
                    NOW.isoformat(),
                    NOW.isoformat(),
                    mode,
                )
                for number, (peer, state, mode) in enumerate(jobs, start=1)
            ),
        )
        inbox._db.executemany(
            "INSERT INTO peer_threads VALUES (?,?,?,?,1)",
            (
                (peer, f"thread-{peer}" if state == "ready" else None, f"creation-{peer}", state)
                for peer, state in (
                    (4, "ready"),
                    (7, "ready"),
                    (8, "creating"),
                    (9, "creation_unknown"),
                    (10, "creation_unknown"),
                )
            ),
        )


def held_peer_plan(inbox):
    statements = []
    inbox._db.set_trace_callback(statements.append)
    try:
        inbox.status()
    finally:
        inbox._db.set_trace_callback(None)
    query = next(
        statement for statement in statements if statement.startswith("SELECT COUNT(*) FROM peers WHERE EXISTS")
    )
    return [row["detail"].upper() for row in inbox._db.execute("EXPLAIN QUERY PLAN " + query)]


def test_status_searches_jobs_by_peer_and_state_in_completed_history(tmp_path):
    with TelegramInbox(tmp_path / "inbox.sqlite3", clock=lambda: NOW) as inbox:
        seed_history(inbox)
        plan = held_peer_plan(inbox)
        assert any("SEARCH JOBS USING COVERING INDEX JOBS_PEER_STATE" in step for step in plan), plan
        assert not any(step.startswith("SCAN JOBS") for step in plan), plan


def test_index_preserves_held_peer_count_and_other_status_fields(tmp_path):
    with TelegramInbox(tmp_path / "inbox.sqlite3", clock=lambda: NOW) as inbox:
        seed_history(inbox)
        indexed = inbox.status()
        # Review jobs, creating/unknown threads, overlaps and repeated review
        # jobs hold six distinct peers. Running/complete/ready peers do not.
        assert indexed["held_peers"] == 6
        assert indexed["compatibility_hold"] is True
        with inbox._db:
            inbox._db.execute("DROP INDEX jobs_peer_state")
        assert inbox.status() == indexed


@pytest.mark.parametrize("version", [1, 2, 3])
def test_reopen_adds_missing_index_without_changing_registry_or_version_contract(tmp_path, version):
    path = tmp_path / "inbox.sqlite3"
    with TelegramInbox(path, clock=lambda: NOW) as inbox:
        inbox.duty_set(True)
        event_id = inbox.record(1, 1)
        job = inbox.claim(instruction_root="/opt/synthetic-instructions", instruction_revision="a" * 40)
        job = inbox.needs_review(
            job_id=job["job_id"],
            lease=job["lease"],
            expected_revision=job["revision"],
            reason_code="codex_execution_stopped",
        )
        inbox.set_execution_paused(paused=True, expected_revision=1)
        before = inbox.status()
        generation = inbox.generation
        with inbox._db:
            inbox._db.execute("DROP INDEX IF EXISTS jobs_peer_state")
            inbox._db.execute("UPDATE metadata SET value=? WHERE key='schema_version'", (str(version),))
    # Opening an existing schema 3 also installs the index; repeated startup
    # must leave queue identities, controls, jobs and generation intact.
    for _ in range(2):
        with TelegramInbox(path, clock=lambda: NOW) as restored:
            assert restored._meta("schema_version") == "5"
            assert restored.generation == generation
            assert restored.resolve(event_id).message_id == 1
            assert restored.read_job(job["job_id"]) == job
            assert restored.status() == before
            assert [row["name"] for row in restored._db.execute("PRAGMA index_info(jobs_peer_state)")] == [
                "peer_id",
                "state",
            ]
            assert sum(row["name"] == "jobs_peer_state" for row in restored._db.execute("PRAGMA index_list(jobs)")) == 1
