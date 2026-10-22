"""Exact worker archive recovery over isolated RPC sockets; no live service state."""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.server import unix_serve

from autostop_manager import telegram_wake as wake
from test_telegram_wake_durable import REVISION, FakeJobs, bind_test_peer, new_inbox
from test_telegram_wake_durable import bundle as bundle
from telegram_wake_fakes import THREAD, until
from telegram_wake_fakes import quick_recovery as quick_recovery


def archive_message(thread_id=THREAD):
    return f"session {thread_id} is archived. Run `codex unarchive {thread_id}` to unarchive it first."


class ArchiveServer:
    def __init__(self, *, archived=True):
        self.archived = archived
        self.error = {"code": -32600, "message": archive_message()}
        self.calls = []
        self.sockets = []
        self.thread = {"id": THREAD, "cwd": wake.PROJECT_DIR, "ephemeral": False, "status": {"type": "idle"}}
        self.restored_thread = None
        self.unarchive_error = None

    async def handle(self, socket):
        self.sockets.append(socket)
        async for raw in socket:
            request = json.loads(raw)
            self.calls.append(request)
            if "id" not in request:
                continue
            method = request["method"]
            response = {"id": request["id"], "result": {}}
            if method == "thread/resume" and self.archived:
                response = {"id": request["id"], "error": self.error}
            elif method in {"thread/read", "thread/resume", "thread/unarchive"}:
                if method == "thread/unarchive":
                    if self.unarchive_error is not None:
                        response = {"id": request["id"], "error": self.unarchive_error}
                    else:
                        self.archived = False
                if "error" not in response:
                    thread = self.restored_thread if method == "thread/unarchive" else None
                    response["result"] = {
                        "thread": thread or self.thread,
                        "model": "gpt-6.1-sol",
                        "modelProvider": "test-provider",
                        "approvalPolicy": "never",
                        "approvalsReviewer": "user",
                        "sandbox": {"type": "dangerFullAccess"},
                    }
            await socket.send(json.dumps(response))

    def methods(self):
        return [call.get("method") for call in self.calls]


def config(path, bundle):
    return wake.WakeConfig(THREAD, app_socket=str(path), instruction_root=str(bundle), instruction_revision=REVISION)


@pytest.mark.parametrize("status", ["idle", "notLoaded"])
def test_cold_archived_pinned_worker_recovers_exact_id_without_model_turn(tmp_path, bundle, status):
    async def scenario():
        server = ArchiveServer()
        server.thread["status"] = {"type": status}
        path = tmp_path / "app.sock"
        async with unix_serve(server.handle, str(path), compression=None):
            app = wake.AppServer(config(path, bundle))
            app.managed = True
            try:
                await app.connect()
                assert await app.resume(allow_active=status == "notLoaded") == status
                assert server.methods() == [
                    "initialize",
                    "initialized",
                    "thread/resume",
                    "thread/read",
                    "thread/unarchive",
                    "thread/resume",
                ]
                assert all(
                    call["params"]["threadId"] == THREAD
                    for call in server.calls
                    if call.get("method", "").startswith("thread/")
                )
                assert app.resumed_model == "gpt-6.1-sol"
                assert app.config == config(path, bundle)
                assert not app.outcome_unknown and app.active_turn is None
            finally:
                await app.close()

    asyncio.run(scenario())


def test_unarchive_rejection_stops_after_one_attempt_with_fixed_error(tmp_path, bundle, caplog):
    async def scenario():
        server = ArchiveServer()
        server.unarchive_error = {"code": -32603, "message": "private unarchive failure"}
        path = tmp_path / "app.sock"
        async with unix_serve(server.handle, str(path), compression=None):
            app = wake.AppServer(config(path, bundle))
            app.managed = True
            try:
                await app.connect()
                with pytest.raises(wake.WakeError, match=r"^codex_thread_unarchive_rejected$"):
                    await app.resume()
                assert server.methods().count("thread/unarchive") == 1
                assert server.methods().count("thread/resume") == 1
                assert "turn/start" not in server.methods()
            finally:
                await app.close()

    asyncio.run(scenario())
    assert "private" not in caplog.text


def test_archive_notification_racing_idle_response_cannot_authorize_start(tmp_path, bundle):
    async def scenario():
        server = ArchiveServer(archived=False)
        path = tmp_path / "app.sock"
        async with unix_serve(server.handle, str(path), compression=None):
            app = wake.AppServer(config(path, bundle))
            app.managed = True
            try:
                await app.connect()
                original = app.request

                async def request(method, params):
                    result = await original(method, params)
                    if method == "thread/resume":
                        app._notice_thread_archived({"threadId": THREAD})
                    return result

                app.request = request
                with pytest.raises(wake.TransportLost, match=r"^codex_thread_archive_changed$"):
                    await app.resume()
                assert not app.ready_event.is_set()
                assert "turn/start" not in server.methods()
            finally:
                await app.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cause", ["archive", "readiness"])
def test_archive_or_lost_readiness_during_committed_intent_never_starts_model(tmp_path, bundle, cause):
    async def scenario():
        server = ArchiveServer(archived=False)
        path = tmp_path / "app.sock"
        inbox, event = new_inbox(tmp_path)
        job = inbox.claim(instruction_root=str(bundle), instruction_revision=REVISION)
        bind_test_peer(inbox, job)
        app = wake.AppServer(config(path, bundle))

        class IntentInterruptionJobs(FakeJobs):
            async def request(self, operation, **arguments):
                result = await super().request(operation, **arguments)
                if operation == "job_start_intent":
                    target = dispatcher.app
                    if cause == "archive":
                        sequence = target.thread_archive_sequence
                        server.archived = True
                        await server.sockets[-1].send(
                            json.dumps({"method": "thread/archived", "params": {"threadId": THREAD}})
                        )
                        await until(lambda: target.thread_archive_sequence > sequence)
                    else:
                        target.ready_event.clear()
                return result

        jobs = IntentInterruptionJobs(inbox)
        dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
        app.managed = True
        async with unix_serve(server.handle, str(path), compression=None):
            try:
                await app.connect()
                await app.resume()
                app.ready_event.set()
                generation = app.generation
                await dispatcher._execute(job)
                held = inbox.read_job(job["job_id"])
                assert held["state"] == "needs_review"
                assert held["thread_id"] == THREAD and held["turn_id"] is None
                assert held["effects"] == [] and inbox.event_state(event) == "open"
                assert app.generation == generation and app.connected
                assert dispatcher.app.outcome_unknown and dispatcher.app.active_turn is None
                assert server.methods().count("turn/start") == 0
                assert "thread/unarchive" not in server.methods()
                assert held["reason_code"] == (
                    "codex_thread_archived" if cause == "archive" else "codex_start_connection_changed"
                )
                assert dispatcher.failed == 1 and dispatcher.completed == 0
            finally:
                await dispatcher.close()
                await app.close()
                inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error",
    [
        {"code": -32600, "message": "private arbitrary resume error"},
        {"code": -32600, "message": archive_message("foreign")},
        {"code": -32600, "message": "private details: " + archive_message()},
        {"code": -32603, "message": archive_message()},
        {"code": -32600, "message": None},
    ],
)
def test_arbitrary_or_foreign_rpc_rejection_never_unarchives(tmp_path, bundle, error, caplog):
    async def scenario():
        server = ArchiveServer()
        server.error = error
        path = tmp_path / "app.sock"
        async with unix_serve(server.handle, str(path), compression=None):
            app = wake.AppServer(config(path, bundle))
            app.managed = True
            try:
                await app.connect()
                with pytest.raises(wake.WakeError, match=r"^codex_thread_resume_rejected$"):
                    await app.resume()
                assert "thread/unarchive" not in server.methods()
                assert "thread/read" not in server.methods()
            finally:
                await app.close()

    asyncio.run(scenario())
    assert "private" not in caplog.text and THREAD not in caplog.text


def test_warm_archive_notification_removes_stale_ready_then_recovers_without_polling(tmp_path, bundle, quick_recovery):
    async def scenario():
        server = ArchiveServer(archived=False)
        path = tmp_path / "app.sock"
        async with unix_serve(server.handle, str(path), compression=None):
            app = wake.AppServer(config(path, bundle))
            supervisor = asyncio.create_task(app.supervise())
            try:
                await until(lambda: app.ready_event.is_set())
                await server.sockets[-1].send(
                    json.dumps({"method": "thread/archived", "params": {"threadId": "foreign"}})
                )
                await asyncio.sleep(0.01)
                assert app.ready_event.is_set() and not app.worker_archived
                server.archived = True
                await server.sockets[-1].send(json.dumps({"method": "thread/archived", "params": {"threadId": THREAD}}))
                await until(lambda: not app.ready_event.is_set())
                await until(lambda: app.ready_event.is_set() and not server.archived)
                assert not app.blocked and not app.outcome_unknown and app.active_turn is None
                assert server.methods().count("thread/unarchive") == 1
                assert "turn/start" not in server.methods()
                calls = len(server.calls)
                await asyncio.sleep(0.02)
                assert len(server.calls) == calls
            finally:
                await app.close()
                supervisor.cancel()
                await asyncio.gather(supervisor, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("guard", ["active", "unknown", "failed", "paused", "blocked", "unmanaged", "unpinned"])
def test_cold_archive_cannot_revive_active_unknown_or_unpinned_execution(tmp_path, bundle, guard):
    async def scenario():
        server = ArchiveServer()
        path = tmp_path / "app.sock"
        async with unix_serve(server.handle, str(path), compression=None):
            app = wake.AppServer(
                config(path, bundle) if guard != "unpinned" else wake.WakeConfig(THREAD, app_socket=str(path))
            )
            app.managed = guard != "unmanaged"
            if guard == "active":
                app.active_turn = "owned-turn"
            elif guard == "unknown":
                app.outcome_unknown = True
            elif guard == "failed":
                app.failed_turn = wake.FailedTurnContext("inbound-1", "owned-turn")
            elif guard == "paused":
                app.pausing = True
            elif guard == "blocked":
                app.blocked = True
            try:
                await app.connect()
                with pytest.raises(wake.WakeError, match=r"^codex_thread_archived$"):
                    await app.resume(allow_active=True)
                assert "thread/unarchive" not in server.methods() and "turn/start" not in server.methods()
            finally:
                await app.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("guard", ["active", "unknown", "failed"])
def test_warm_archive_during_owned_execution_holds_without_replay(tmp_path, bundle, guard):
    async def scenario():
        server = ArchiveServer(archived=False)
        path = tmp_path / "app.sock"
        async with unix_serve(server.handle, str(path), compression=None):
            app = wake.AppServer(config(path, bundle))
            app.managed = True
            try:
                await app.connect()
                await app.resume()
                app.ready_event.set()
                if guard == "active":
                    app.active_turn = "owned-turn"
                elif guard == "unknown":
                    app.outcome_unknown = True
                else:
                    app.failed_turn = wake.FailedTurnContext("inbound-1", "owned-turn")
                server.archived = True
                await server.sockets[-1].send(json.dumps({"method": "thread/archived", "params": {"threadId": THREAD}}))
                await until(lambda: app.blocked)
                assert app.recovery_error == "codex_thread_archived"
                assert "thread/unarchive" not in server.methods() and "turn/start" not in server.methods()
                assert app.active_turn == ("owned-turn" if guard == "active" else None)
                assert app.outcome_unknown is (guard == "unknown")
            finally:
                await app.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("target", ["active", "foreign", "cwd", "ephemeral"])
def test_archive_readback_requires_exact_idle_visible_worker(tmp_path, bundle, target):
    async def scenario():
        server = ArchiveServer()
        if target == "active":
            server.thread["status"] = {"type": "active", "activeFlags": []}
        elif target == "foreign":
            server.thread["id"] = "foreign"
        elif target == "cwd":
            server.thread["cwd"] = "/foreign"
        else:
            server.thread["ephemeral"] = True
        path = tmp_path / "app.sock"
        async with unix_serve(server.handle, str(path), compression=None):
            app = wake.AppServer(config(path, bundle))
            app.managed = True
            try:
                await app.connect()
                with pytest.raises(wake.WakeError, match=r"^codex_thread_archive_target_invalid$"):
                    await app.resume()
                assert "thread/unarchive" not in server.methods()
            finally:
                await app.close()

    asyncio.run(scenario())
