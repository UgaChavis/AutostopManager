"""Client conversation rotation over isolated Codex sockets and durable inboxes."""

import asyncio
import json
import os

import pytest
from instruction_bundle_fakes import create_instruction_bundle
from telegram_wake_fakes import connected_app, until
from test_telegram_bridge_durable import _message
from test_telegram_point_fixes import HistoryClient
from test_telegram_wake_durable import REVISION, FakeJobs
from test_telegram_wake_peer_lifecycle import inherited_server, peer_case

from autostop_manager import instruction_bundle, telegram_bridge as bridge, telegram_wake as wake
from autostop_manager.telegram_inbox import TelegramInbox


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """Use a user-owned synthetic bundle so CI need not run tests as root."""
    releases = tmp_path / "releases"
    root = create_instruction_bundle(releases / "snapshot", REVISION)
    monkeypatch.setattr(wake, "INSTRUCTION_RELEASE_ROOTS", (releases,))
    monkeypatch.setattr(instruction_bundle, "_OWNER_UID", os.geteuid())
    return root


def next_job(inbox, bundle):
    event = inbox.record(123, 2)
    job = inbox.claim(instruction_root=str(bundle), instruction_revision=REVISION)
    assert job is not None and job["event_ids"] == [event]
    return job


@pytest.mark.parametrize("mode", ["active", "archived", "deleted"])
@pytest.mark.parametrize("warm", [False, True])
def test_next_message_reuses_active_or_rotates_archived_and_deleted(tmp_path, bundle, mode, warm):
    async def scenario():
        server = inherited_server()
        async with peer_case(tmp_path, bundle, server) as (_bootstrap, dispatcher, inbox, first):
            await dispatcher._execute(first)
            old = inbox.read_peer_thread(first["job_id"])["thread_id"]
            assert inbox.read_job(first["job_id"])["state"] == "completed"
            assert server.counter == 1 and server.created_threads == [old]
            prior_calls = len(server.calls)
            if mode == "archived":
                server.archived_threads.add(old)
                if warm:
                    await server.sockets[-1].send(
                        json.dumps({"method": "thread/archived", "params": {"threadId": old}})
                    )
                    await until(lambda: dispatcher.app.worker_archived)
            elif mode == "deleted":
                server.deleted_threads.add(old)
            if not warm:
                await dispatcher._retire_peer(asyncio.get_running_loop().time() + 2)
            job = next_job(inbox, bundle)
            await dispatcher._execute(job)
            current = inbox.read_peer_thread(job["job_id"])["thread_id"]
            assert inbox.read_job(job["job_id"])["state"] == "completed"
            assert server.counter == 2
            assert [call["params"]["threadId"] for call in server.calls if call["method"] == "turn/start"] == [
                old,
                old if mode == "active" else current,
            ]
            if mode == "active":
                assert current == old and server.created_threads == [old]
            else:
                assert current != old and server.created_threads == [old, current]
                rotations = [call for call, _ in dispatcher.jobs.calls if call == "job_peer_thread_rotate"]
                assert len(rotations) == 1
                if mode == "archived":
                    assert old in server.archived_threads
                    assert not any(
                        call["method"] == "thread/unarchive" and call["params"]["threadId"] == old
                        for call in server.calls[prior_calls:]
                    )

    asyncio.run(scenario())


def test_archived_peer_rotates_after_wake_and_inbox_restart(tmp_path, bundle):
    async def scenario():
        server = inherited_server()
        path = tmp_path / "durable.sqlite3"
        first_socket = tmp_path / "before-restart"
        second_socket = tmp_path / "after-restart"
        first_socket.mkdir()
        second_socket.mkdir()
        with TelegramInbox(path) as inbox:
            inbox.duty_set(True)
            inbox.record(123, 1)
            job = inbox.claim(instruction_root=str(bundle), instruction_revision=REVISION)
            async with connected_app(first_socket, server) as app:
                await app.resume()
                dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), FakeJobs(inbox, finish=True))
                try:
                    await dispatcher._execute(job)
                    old = inbox.read_peer_thread(job["job_id"])["thread_id"]
                finally:
                    await dispatcher.close()
        server.archived_threads.add(old)
        with TelegramInbox(path) as inbox:
            second = next_job(inbox, bundle)
            async with connected_app(second_socket, server) as app:
                await app.resume()
                dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), FakeJobs(inbox, finish=True))
                try:
                    await dispatcher._execute(second)
                    current = inbox.read_peer_thread(second["job_id"])["thread_id"]
                    assert current != old and old in server.archived_threads
                    assert inbox.read_job(second["job_id"])["state"] == "completed"
                    assert server.created_threads == [old, current] and server.counter == 2
                finally:
                    await dispatcher.close()

    asyncio.run(scenario())


def test_rotated_peer_can_read_staff_reply_and_older_history(tmp_path, bundle, monkeypatch):
    async def resolve(_client, peer):
        assert peer == "123"
        return 123, {"id": 123, "kind": "private"}

    monkeypatch.setattr(bridge, "_resolve_peer", resolve)

    async def scenario():
        server = inherited_server()
        async with peer_case(tmp_path, bundle, server) as (_bootstrap, dispatcher, inbox, first):
            await dispatcher._execute(first)
            old = inbox.read_peer_thread(first["job_id"])["thread_id"]
            server.archived_threads.add(old)
            event = inbox.record(123, 151)
            job = inbox.claim(instruction_root=str(bundle), instruction_revision=REVISION)
            assert job["event_ids"] == [event]
            await dispatcher._select_peer(job)
            assert dispatcher.app.config.thread_id != old

            rows = [_message(i, peer=123, text="earlier agreement" if i == 7 else "context") for i in range(1, 152)]
            rows[149] = _message(150, peer=123, out=True, text="staff reply")
            client = HistoryClient([*rows, _message(300, peer=456, text="different client")])
            config = bridge.TelegramConfig(
                123456,
                "a" * 32,
                state_dir=tmp_path,
                session_path=tmp_path / "account",
                socket_path=tmp_path / "bridge.sock",
            )
            monitor = bridge.DurableInboundMonitor(inbox, enabled=True)
            monitor.self_peer_id = 900
            page = await bridge._dispatch_operation(
                client, config, {"operation": "read", "peer": "123", "limit": 20}, inbound_monitor=monitor
            )
            staff = next(row for row in page["messages"] if row["id"] == 150)
            assert staff["out"] and staff["text"] == "staff reply"
            assert inbox.outgoing_context(123) == [{"message_id": 150, "origin": "human"}]
            assert page["has_more"] and all(row["id"] != 300 for row in page["messages"])
            while page["has_more"]:
                page = await bridge._dispatch_operation(
                    client,
                    config,
                    {
                        "operation": "read",
                        "peer": "123",
                        "limit": 100,
                        "before_message_id": page["next_before_message_id"],
                    },
                    inbound_monitor=monitor,
                )
            assert any(row["id"] == 7 and row["text"] == "earlier agreement" for row in page["messages"])
            refreshed = inbox.refresh_job(
                job_id=job["job_id"], lease=job["lease"], expected_revision=job["revision"], event_ids=[event]
            )
            assert refreshed["peer_id"] == 123 and refreshed["event_ids"] == [event]

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["missing_rollout_existing_read", "ambiguous_rejection"])
def test_uncertain_missing_or_rejected_resume_keeps_old_binding_and_does_not_start(tmp_path, bundle, failure):
    async def scenario():
        server = inherited_server()
        async with peer_case(tmp_path, bundle, server) as (_bootstrap, dispatcher, inbox, first):
            await dispatcher._execute(first)
            mapping = inbox.read_peer_thread(first["job_id"])
            old = mapping["thread_id"]
            if failure == "missing_rollout_existing_read":
                server.missing_rollout_threads.add(old)
            else:
                server.reject_method = "thread/resume"
            job = next_job(inbox, bundle)
            await dispatcher._execute(job)
            assert inbox.read_peer_thread(job["job_id"]) == mapping
            assert inbox.read_job(job["job_id"])["state"] == "needs_review"
            assert inbox.read_job(job["job_id"])["execution_proof"] == "not_started"
            assert len(server.created_threads) == 1 and server.counter == 1
            assert not any(call == "job_peer_thread_rotate" for call, _ in dispatcher.jobs.calls)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "history",
    [
        {"data": [{"id": "external-turn", "status": "inProgress"}], "nextCursor": None},
        {"data": [{"id": "external-turn", "status": "unknown"}], "nextCursor": None},
        {"data": [{"id": "external-turn"}], "nextCursor": None},
        {"data": "invalid", "nextCursor": None},
        {"data": [{"id": "", "status": "completed"}], "nextCursor": None},
        {"data": [], "nextCursor": "unexpected-page"},
    ],
)
def test_archived_peer_with_unfinished_or_invalid_history_is_not_rotated(tmp_path, bundle, history):
    async def scenario():
        server = inherited_server()
        async with peer_case(tmp_path, bundle, server) as (_bootstrap, dispatcher, inbox, first):
            await dispatcher._execute(first)
            mapping = inbox.read_peer_thread(first["job_id"])
            server.archived_threads.add(mapping["thread_id"])
            server.metadata_response = history
            job = next_job(inbox, bundle)
            await dispatcher._execute(job)
            assert inbox.read_peer_thread(job["job_id"]) == mapping
            assert inbox.read_job(job["job_id"])["state"] == "needs_review"
            assert inbox.read_job(job["job_id"])["execution_proof"] == "not_started"
            assert len(server.created_threads) == 1 and server.counter == 1
            assert not any(call == "job_peer_thread_rotate" for call, _ in dispatcher.jobs.calls)

    asyncio.run(scenario())
