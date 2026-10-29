"""Fault injection over isolated sockets and real durable ledgers; no live APIs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from instruction_bundle_fakes import create_instruction_bundle
from telegram_wake_fakes import THREAD, MockServer, until
from telegram_wake_fakes import quick_recovery as quick_recovery
from websockets.asyncio.server import unix_serve

from autostop_manager import telegram_wake as wake
from autostop_manager.telegram_inbox import InboxError, TelegramInbox

REVISION = "a" * 40


def test_authoritative_empty_queue_clears_stale_resolved_hold(tmp_path, bundle):
    async def scenario():
        inbox = TelegramInbox(tmp_path / "db" / "inbound.sqlite3")
        inbox.duty_set(True)
        jobs = FakeJobs(inbox)
        app = wake.AppServer(wake.WakeConfig(THREAD, instruction_root=str(bundle), instruction_revision=REVISION))
        app.connected = True
        app.ready_event.set()
        app.resume = AsyncMock(return_value="idle")
        dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
        dispatcher.execution_state = "held"
        dispatcher.last_error = "codex_job_requires_review"
        jobs.changed.set()
        dispatcher.worker = asyncio.create_task(dispatcher.work())
        try:
            await until(lambda: dispatcher.status()["ready"])
            assert dispatcher.last_error is None
            assert jobs.health[-1] == {"component": "codex", "state": "ready", "error_code": None}
        finally:
            await stop_dispatcher(dispatcher)
            inbox.close()

    asyncio.run(scenario())


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    if os.geteuid() != 0:
        pytest.skip("production instruction bundles are root-owned")
    releases = tmp_path / "releases"
    root = create_instruction_bundle(releases / "snapshot", REVISION)
    monkeypatch.setattr(wake, "INSTRUCTION_RELEASE_ROOTS", (releases,))
    return root


class FakeJobs:
    """Exercise the same inbox APIs behind IPC without touching a real bridge."""

    def __init__(self, inbox, *, finish=False):
        self.inbox = inbox
        self.finish = finish
        self.connected = True
        self.changed = asyncio.Event()
        self.last_error = None
        self.listener = None
        self.calls = []
        self.health = []
        self.cleanup_tasks = set()

    @property
    def status_fresh(self):
        return self.connected

    @property
    def status_age(self):
        return 0 if self.connected else None

    @property
    def snapshot(self):
        return self.inbox.status()

    def start(self):
        self.changed.set()

    async def close(self):
        self.connected = False

    async def request(self, operation, **arguments):
        self.calls.append((operation, dict(arguments)))
        if operation == "job_status":
            return {"ok": True, "status": self.inbox.status()}
        if operation == "job_active":
            return {"ok": True, **self.inbox.active_job_page(**arguments)}
        if operation == "job_execution_slot":
            return {"ok": True, "job": self.inbox.execution_slot()}
        if operation == "job_read":
            return {"ok": True, "job": self.inbox.read_job(arguments["job_id"])}
        if operation == "job_health":
            self.health.append(arguments)
            return {"ok": True}
        if operation == "job_execution_set":
            return {"ok": True, "status": self.inbox.set_execution_paused(**arguments)}
        if operation == "job_claim":
            assert arguments.pop("worker_protocol") == wake.WORKER_PROTOCOL
        peer_functions = {
            "job_peer_thread_prepare": self.inbox.prepare_peer_thread,
            "job_peer_thread_read": self.inbox.read_peer_thread,
            "job_peer_thread_bind": self.inbox.bind_peer_thread,
            "job_peer_thread_unknown": self.inbox.mark_peer_thread_unknown,
        }
        if operation in peer_functions:
            try:
                return {"ok": True, "peer_thread": peer_functions[operation](**arguments)}
            except InboxError as exc:
                raise wake.WakeError(str(exc)) from exc
        functions = {
            "job_claim": self.inbox.claim,
            "job_start_intent": self.inbox.start_intent,
            "job_turn_bound": self.inbox.bind_turn,
            "job_needs_review": self.inbox.needs_review,
            "job_release": self.inbox.release_job,
            "job_defer_failed": self.inbox.defer_failed_request,
            "job_execution_proof": self.inbox.finish_execution,
        }
        try:
            job = functions[operation](**arguments)
            if operation == "job_turn_bound" and self.finish:
                job = self.inbox.result(
                    job_id=job["job_id"],
                    lease=job["lease"],
                    expected_revision=job["revision"],
                    outcome="no_reply_needed",
                    event_ids=job["event_ids"],
                    receipt_keys=[],
                    reason_code="not_actionable",
                )
        except InboxError as exc:
            raise wake.WakeError(str(exc)) from exc
        if operation != "job_claim" or job is not None:
            self.changed.set()
        return {"ok": True, "job": job}


def new_inbox(tmp_path):
    inbox = TelegramInbox(tmp_path / "db" / "inbound.sqlite3")
    inbox.duty_set(True)
    event_id = inbox.record(123, 1)
    assert event_id is not None
    return inbox, event_id


def bind_test_peer(inbox, job, thread_id=THREAD):
    """Explicitly provision a synthetic peer context for manually staged runs."""
    mapping = inbox.prepare_peer_thread(job_id=job["job_id"], lease=job["lease"], expected_revision=job["revision"])
    inbox.bind_peer_thread(
        job_id=job["job_id"],
        lease=job["lease"],
        expected_revision=job["revision"],
        creation_key=mapping["creation_key"],
        thread_id=thread_id,
    )


async def stop_dispatcher(dispatcher):
    await dispatcher.close()
    await dispatcher.app.close()


@pytest.mark.parametrize("status", [[], {}, None, 2, True])
def test_turn_metadata_bad_status_is_fixed_error(status):
    async def scenario():
        app = wake.AppServer(wake.WakeConfig(THREAD))
        app.request = AsyncMock(return_value={"data": [{"id": "1", "status": status}]})
        with pytest.raises(wake.WakeError, match=r"^codex_turn_metadata_invalid$"):
            await app._turn_metadata("1")
        with pytest.raises(wake.WakeError, match=r"^codex_turn_metadata_invalid$"):
            app._complete_turn(status, recovered=True)

    asyncio.run(scenario())


def test_busy_selected_worker_waits_manual_completion_without_poll_or_overlap(tmp_path, bundle):
    async def scenario():
        server = MockServer(peer_threads=True)
        server.peer_thread_status = "active"
        path = str(tmp_path / "app.sock")
        inbox, _ = new_inbox(tmp_path)
        jobs = FakeJobs(inbox, finish=True)
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(
                wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
            )
            dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
            dispatcher.start()
            try:
                await until(lambda: dispatcher.execution_state == "waiting_thread" and server.resume_calls >= 4)
                calls = len(server.calls)
                await asyncio.sleep(0.02)
                assert len(server.calls) == calls
                assert server.counter == 0 and dispatcher.enabled
                server.peer_thread_status = "idle"
                await server.sockets[-1].send(
                    json.dumps(
                        {
                            "method": "turn/completed",
                            "params": {
                                "threadId": dispatcher.app.config.thread_id,
                                "turn": {"id": "manual", "status": "completed"},
                            },
                        }
                    )
                )
                await until(lambda: dispatcher.completed == 1)
                assert server.counter == 1 and not app.blocked
                assert inbox.status()["open_events"] == 0
                methods = [name for name, _args in jobs.calls]
                assert methods.index("job_start_intent") < methods.index("job_turn_bound")
                assert "job_owner_control" not in methods
                assert not hasattr(dispatcher, "queue")
                prompt = next(
                    call["params"]["input"][0]["text"] for call in server.calls if call.get("method") == "turn/start"
                )
                assert str(bundle) + "/AGENTS.md" in prompt
                assert "job-result" in prompt and "kind (ack/result/clarification)." in prompt
                assert "job-handoff" in prompt
                assert "role=client" in prompt
            finally:
                await stop_dispatcher(dispatcher)
                inbox.close()

    asyncio.run(scenario())


def test_model_completed_without_result_holds_durable_event(tmp_path, bundle):
    async def scenario():
        inbox, event_id = new_inbox(tmp_path)
        server = MockServer(peer_threads=True)
        jobs = FakeJobs(inbox)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(
                wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
            )
            dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
            dispatcher.start()
            try:
                await until(
                    lambda: (
                        inbox.status()["needs_review_jobs"] == 1
                        and inbox.execution_slot() is None
                        and dispatcher.status()["ready"]
                    )
                )
                assert dispatcher.completed == 0 and server.counter == 1
                assert inbox.resolve(event_id) is not None
                assert inbox.active_jobs()[0]["state"] == "needs_review"
                assert dispatcher.status()["needs_review"] == 1
                assert dispatcher.status()["intake_enabled"]
                assert inbox.record(124, 2) is not None
                jobs.changed.set()
                await until(lambda: server.counter == 2 and inbox.status()["needs_review_jobs"] == 2)
                assert inbox.execution_slot() is None
            finally:
                await stop_dispatcher(dispatcher)
                inbox.close()

    asyncio.run(scenario())


def test_explicit_start_rejection_does_not_lock_next_real_client_request(tmp_path, bundle):
    async def scenario():
        inbox, event_id = new_inbox(tmp_path)
        server = MockServer(peer_threads=True)
        server.reject_method = "turn/start"
        jobs = FakeJobs(inbox, finish=True)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(
                wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
            )
            dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
            dispatcher.start()
            try:
                await until(
                    lambda: any(name == "job_defer_failed" for name, _ in jobs.calls) and dispatcher.status()["ready"]
                )
                failed_id = next(args["job_id"] for name, args in jobs.calls if name == "job_defer_failed")
                assert inbox.read_job(failed_id)["state"] == "failed_waiting_input"
                assert inbox.read_job(failed_id)["execution_proof"] == "not_started"
                assert inbox.execution_slot() is None and not inbox.active_jobs()
                starts = sum(call.get("method") == "turn/start" for call in server.calls)
                assert inbox.record(123, 1) == event_id
                jobs.changed.set()
                await asyncio.sleep(0)
                assert sum(call.get("method") == "turn/start" for call in server.calls) == starts
                server.reject_method = None
                assert inbox.record(123, 2) is not None
                jobs.changed.set()
                await until(lambda: dispatcher.completed == 1)
                assert inbox.status()["open_events"] == 0
                assert not dispatcher.app.blocked
            finally:
                await stop_dispatcher(dispatcher)
                inbox.close()

    asyncio.run(scenario())


def test_lost_start_response_survives_restart_without_replay(tmp_path, bundle):
    async def scenario():
        inbox, _ = new_inbox(tmp_path)
        server = MockServer(peer_threads=True, disconnect=True)
        jobs = FakeJobs(inbox)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(
                wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
            )
            dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
            dispatcher.start()
            try:
                await until(lambda: bool(inbox.active_jobs()) and inbox.active_jobs()[0]["state"] == "needs_review")
                assert inbox.active_jobs()[0]["turn_id"] is None
                assert server.counter == 1
            finally:
                await stop_dispatcher(dispatcher)
            inbox.close()
            reopened = TelegramInbox(tmp_path / "db" / "inbound.sqlite3")
            later = wake.AppServer(app.config)
            later_jobs = FakeJobs(reopened)
            second = wake.DurableWakeDispatcher(later, 123, later_jobs)
            second.start()
            try:
                await until(lambda: second.execution_state == "held")
                assert server.counter == 1
                assert reopened.status()["needs_review_jobs"] == 1
                with pytest.raises(wake.WakeError, match="wake_pause_outcome_unknown"):
                    await second.pause()
                with pytest.raises(wake.WakeError, match="wake_pause_outcome_unknown"):
                    await second.resume_execution()
                await asyncio.sleep(0.02)
                assert server.counter == 1 and reopened.status()["needs_review_jobs"] == 1
            finally:
                await stop_dispatcher(second)
                reopened.close()

    asyncio.run(scenario())


def test_pause_returns_unstarted_claim_and_preserves_events(tmp_path, bundle):
    async def scenario():
        inbox, event_id = new_inbox(tmp_path)
        server = MockServer(peer_threads=True)
        server.peer_thread_status = "active"
        jobs = FakeJobs(inbox)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(
                wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
            )
            dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
            dispatcher.start()
            try:
                await until(lambda: dispatcher.execution_state == "waiting_thread")
                await dispatcher.pause()
                assert not dispatcher.enabled and not dispatcher.active
                assert inbox.resolve(event_id) is not None
                assert inbox.active_jobs()[0]["state"] == "needs_review"
                assert inbox.active_jobs()[0]["execution_proof"] == "not_started"
                assert inbox.execution_slot() is None
                assert server.counter == 0
                assert not any(call.get("method") == "turn/interrupt" for call in server.calls)
            finally:
                await stop_dispatcher(dispatcher)
                inbox.close()

    asyncio.run(scenario())


def test_initial_subscription_snapshot_recovers_lost_notification(tmp_path, bundle):
    async def scenario():
        inbox, _ = new_inbox(tmp_path)
        jobs = FakeJobs(inbox, finish=True)
        # No event handoff was sent. The initial subscription snapshot suffices.
        server = MockServer(peer_threads=True)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(
                wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
            )
            dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
            dispatcher.start()
            try:
                await until(lambda: dispatcher.completed == 1)
                assert server.counter == 1
            finally:
                await stop_dispatcher(dispatcher)
                inbox.close()

    asyncio.run(scenario())


def test_immutable_bundle_symlink_or_revision_mismatch_fails(bundle):
    wake.validate_instruction_bundle(str(bundle), REVISION)
    with pytest.raises(wake.WakeError, match="wake_instruction_revision_mismatch"):
        wake.validate_instruction_bundle(str(bundle), "b" * 40)
    alias = bundle.parent / "current"
    alias.symlink_to(bundle)
    with pytest.raises(wake.WakeError, match="wake_instruction_bundle_invalid"):
        wake.validate_instruction_bundle(str(alias), REVISION)


def test_instruction_bundle_without_full_client_skill_is_rejected(bundle):
    (bundle / ".agents/skills/manage-autostop-client/SKILL.md").unlink()
    with pytest.raises(wake.WakeError, match=r"^wake_instruction_inventory_invalid$"):
        wake.validate_instruction_bundle(str(bundle), REVISION)


def test_instruction_bundle_rejects_directory_in_place_of_client_skill(bundle):
    target = bundle / ".agents/skills/manage-autostop-client/SKILL.md"
    target.unlink()
    target.mkdir()
    with pytest.raises(wake.WakeError, match=r"^wake_instruction_inventory_invalid$"):
        wake.validate_instruction_bundle(str(bundle), REVISION)


@pytest.mark.skipif(os.geteuid() != 0, reason="root-only control socket")
def test_control_socket_closes_after_schema_error(tmp_path):
    async def scenario():
        app = wake.AppServer(wake.WakeConfig(THREAD))
        dispatcher = wake.WakeDispatcher(app, os.geteuid())
        dispatcher.failed_context = lambda: (_ for _ in ()).throw(TypeError("private payload"))
        path = tmp_path / "control.sock"
        async with await asyncio.start_unix_server(dispatcher.serve, path=str(path)):
            reader, writer = await asyncio.open_unix_connection(str(path))
            writer.write(b'{"operation":"failed_context"}\n')
            await writer.drain()
            reply = json.loads(await asyncio.wait_for(reader.readline(), 1))
            assert reply == {"ok": False, "error": "wake_request_failed"}
            assert await asyncio.wait_for(reader.read(), 1) == b""
            writer.close()
            await writer.wait_closed()

    asyncio.run(scenario())


def test_durable_control_denies_non_root_even_bridge_uid(monkeypatch):
    class FakeSocket:
        def getsockopt(self, *_args):
            return struct.pack("3i", 1, 123, 123)

    class Writer:
        def __init__(self):
            self.data = b""
            self.closed = False

        def get_extra_info(self, _name):
            return FakeSocket()

        def write(self, data):
            self.data += data

        async def drain(self):
            pass

        def close(self):
            self.closed = True

        async def wait_closed(self):
            pass

    async def scenario():
        app = wake.AppServer(wake.WakeConfig(THREAD))
        dispatcher = wake.DurableWakeDispatcher(app, 123)
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"operation":"pause"}\n')
        writer = Writer()
        await dispatcher.serve(reader, writer)
        assert json.loads(writer.data) == {"ok": False, "error": "wake_sender_denied"}
        assert writer.closed and dispatcher.enabled

    asyncio.run(scenario())


def test_worker_selection_requires_root_and_exact_configuration(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        with pytest.raises(wake.WakeError, match="wake_setup_denied"):
            await wake.select_worker(
                expected_thread_id=THREAD,
                expected_config_sha256="0" * 64,
                instruction_root="/none",
                instruction_revision=REVISION,
            )

    asyncio.run(scenario())


def test_verified_ack_is_not_business_completion(tmp_path, bundle):
    async def scenario():
        inbox, event_id = new_inbox(tmp_path)
        jobs = FakeJobs(inbox)
        server = MockServer(peer_threads=True, complete=False)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(
                wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
            )
            dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
            dispatcher.start()
            try:
                await until(lambda: bool(inbox.active_jobs()) and inbox.active_jobs()[0]["state"] == "running")
                job = inbox.active_jobs()[0]
                key = inbox.reserve_effect(
                    job_id=job["job_id"],
                    lease=job["lease"],
                    expected_revision=job["revision"],
                    kind="ack",
                    text_sha256="0" * 64,
                    peer_id=123,
                    reply_to_message_id=1,
                )
                inbox.verify_effect(effect_key=key, message_id=2, receipt_key="ack-2")
                assert inbox.resolve(event_id) is not None
                await server.sockets[-1].send(
                    json.dumps(
                        {
                            "method": "turn/completed",
                            "params": {"threadId": job["thread_id"], "turn": {"id": "1", "status": "completed"}},
                        }
                    )
                )
                server.turn_statuses["1"] = "completed"
                await until(
                    lambda: (
                        inbox.status()["needs_review_jobs"] == 1
                        and inbox.execution_slot() is None
                        and dispatcher.status()["ready"]
                    )
                )
                assert dispatcher.completed == 0
                assert inbox.status()["open_events"] == 1
                assert inbox.active_jobs()[0]["effects"][0]["verified"]
            finally:
                await stop_dispatcher(dispatcher)
                inbox.close()

    asyncio.run(scenario())


def test_binding_response_loss_reads_exact_committed_turn_and_never_replays(tmp_path, bundle):
    class LoseBindingResponse(FakeJobs):
        async def request(self, operation, **arguments):
            response = await super().request(operation, **arguments)
            if operation == "job_turn_bound":
                raise OSError("lost binding reply")
            return response

    async def scenario():
        inbox, _ = new_inbox(tmp_path)
        jobs = LoseBindingResponse(inbox)
        server = MockServer(peer_threads=True, complete=False)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(
                wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
            )
            dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
            dispatcher.start()
            try:
                await until(
                    lambda: (
                        bool(inbox.active_jobs())
                        and inbox.active_jobs()[0]["state"] == "running"
                        and any(name == "job_read" for name, _args in jobs.calls)
                    )
                )
                job = inbox.active_jobs()[0]
                assert job["turn_id"] == "1" and job["thread_id"] == dispatcher.app.config.thread_id
                assert job["thread_id"] != THREAD
                assert server.counter == 1
                current = inbox.read_job(job["job_id"])
                inbox.result(
                    job_id=current["job_id"],
                    lease=current["lease"],
                    expected_revision=current["revision"],
                    outcome="no_reply_needed",
                    event_ids=current["event_ids"],
                    receipt_keys=[],
                    reason_code="not_actionable",
                )
                jobs.changed.set()
                server.turn_statuses["1"] = "completed"
                await server.sockets[-1].send(
                    json.dumps(
                        {
                            "method": "turn/completed",
                            "params": {"threadId": job["thread_id"], "turn": {"id": "1", "status": "completed"}},
                        }
                    )
                )
                await until(lambda: dispatcher.completed == 1)
                assert server.counter == 1 and inbox.active_jobs() == []
            finally:
                await stop_dispatcher(dispatcher)
                inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("old_thread_status", ["idle", "active"])
def test_selected_worker_config_is_atomic_pinned_and_keeps_owner_thread(
    tmp_path, bundle, monkeypatch, old_thread_status
):
    selected = "00000000-0000-4000-8000-000000000002"
    config_path = tmp_path / "wake.json"
    config_path.write_text(
        json.dumps({"thread_id": THREAD, "project_dir": wake.PROJECT_DIR, "app_socket": wake.APP_SOCKET})
    )
    config_path.chmod(0o600)
    monkeypatch.setattr(wake, "CONFIG_PATH", config_path)
    # WakeConfig.load's default path is bound at definition; keep its actual
    # permission/target validator while routing only to this test's file.
    original_load = wake.WakeConfig.load
    monkeypatch.setattr(wake.WakeConfig, "load", classmethod(lambda cls, path=config_path: original_load(path)))
    calls = []

    class FakeApp:
        def __init__(self, config):
            self.config = config
            self.resumed_model = "existing-model"

        async def connect(self):
            pass

        async def resume(self, *, allow_active=False):
            calls.append(("resume", self.config.thread_id, allow_active))
            if self.config.thread_id == THREAD:
                assert allow_active
                return old_thread_status
            assert not allow_active
            return "idle"

        async def request(self, method, params):
            calls.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": selected}}
            return {}

        async def worker_instruction_config(self):
            return {"project_doc_max_bytes": 0, "skills.include_instructions": False}

        async def close(self):
            pass

    class EmptyJobs:
        def __init__(self, _uid):
            pass

        async def request(self, operation, **arguments):
            assert operation == "job_active"
            assert arguments == {"limit": 1, "after_job_id": None}
            return {"ok": True, "jobs": [], "next_job_id": None}

    monkeypatch.setattr(wake, "AppServer", FakeApp)
    monkeypatch.setattr(wake, "BridgeJobs", EmptyJobs)
    monkeypatch.setattr(wake.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=123))
    monkeypatch.setattr(
        wake,
        "local_request",
        AsyncMock(return_value={"ok": True, "enabled": False, "active": False, "outcome_unknown": False}),
    )
    expected_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()

    async def scenario():
        with pytest.raises(wake.WakeError, match="wake_config_changed"):
            await wake.select_worker(
                expected_thread_id=THREAD,
                expected_config_sha256="0" * 64,
                instruction_root=str(bundle),
                instruction_revision=REVISION,
            )
        assert calls == []
        result = await wake.select_worker(
            expected_thread_id=THREAD,
            expected_config_sha256=expected_hash,
            instruction_root=str(bundle),
            instruction_revision=REVISION,
        )
        assert result["separate_worker"]
        configured = json.loads(config_path.read_text())
        assert configured["thread_id"] == selected
        assert configured["instruction_root"] == str(bundle)
        assert configured["instruction_revision"] == REVISION
        assert config_path.stat().st_mode & 0o777 == 0o600
        assert not config_path.with_name("wake.json.worker-selection").exists()
        assert ("resume", THREAD, True) in calls
        assert ("resume", selected, False) in calls
        assert (
            "thread/start",
            {
                "cwd": wake.PROJECT_DIR,
                "ephemeral": False,
                "model": "existing-model",
                "config": {"project_doc_max_bytes": 0, "skills.include_instructions": False},
            },
        ) in calls
        assert not any(call[0] in {"thread/archive", "turn/interrupt", "turn/start"} for call in calls)

    asyncio.run(scenario())


def test_empty_queue_block_publishes_one_incident_without_an_incoming_event(tmp_path, bundle):
    async def scenario():
        inbox = TelegramInbox(tmp_path / "db" / "inbound.sqlite3")
        inbox.duty_set(True)
        jobs = FakeJobs(inbox)
        server = MockServer(peer_threads=True)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(
                wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
            )
            dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
            dispatcher.start()
            try:
                await until(lambda: any(item["state"] == "ready" for item in jobs.health))
                assert inbox.events() == [] and server.counter == 0
                app.block("codex_rpc_frame_invalid")
                app.block("codex_rpc_frame_invalid")
                await until(lambda: any(item["state"] == "held" for item in jobs.health))
                incidents = [item for item in jobs.health if item["state"] == "held"]
                assert incidents == [{"component": "codex", "state": "held", "error_code": "codex_rpc_frame_invalid"}]
                assert dispatcher.execution_state == "held" and not dispatcher.status()["ready"]
                await dispatcher.pause()
                assert dispatcher.lifecycle_monitor.done()
                published = len(jobs.health)
                app.block("codex_recovery_failed")
                await asyncio.sleep(0.01)
                assert len(jobs.health) == published
            finally:
                await stop_dispatcher(dispatcher)
                inbox.close()

    asyncio.run(scenario())


def test_empty_queue_disconnect_and_recovery_publish_once_each(tmp_path, bundle, quick_recovery):
    async def scenario():
        inbox = TelegramInbox(tmp_path / "db" / "inbound.sqlite3")
        inbox.duty_set(True)
        jobs = FakeJobs(inbox)
        server = MockServer(peer_threads=True)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(
                wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
            )
            dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
            dispatcher.start()
            try:
                await until(lambda: dispatcher.status()["ready"] and dispatcher.bootstrap_checked and bool(jobs.health))
                await server.sockets[-1].close()
                await until(lambda: len(server.sockets) == 2 and dispatcher.status()["ready"] and len(jobs.health) >= 3)
                assert [item["state"] for item in jobs.health] == ["ready", "offline", "ready"]
                assert jobs.health[1]["error_code"] == "codex_transport_failed_or_unknown"
                assert jobs.health[-1]["error_code"] is None
                assert dispatcher.execution_state == "ready"
                assert inbox.events() == [] and server.counter == 0
                count = len(server.calls)
                await asyncio.sleep(0.02)
                assert len(server.calls) == count
            finally:
                await stop_dispatcher(dispatcher)
                inbox.close()

    asyncio.run(scenario())


def test_disconnect_during_manual_thread_wait_preserves_wait_and_reports_health(tmp_path, bundle, quick_recovery):
    async def scenario():
        inbox, _ = new_inbox(tmp_path)
        jobs = FakeJobs(inbox, finish=True)
        server = MockServer(peer_threads=True)
        server.peer_thread_status = "active"
        server.disconnect_resume_on_call = 4
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            app = wake.AppServer(
                wake.WakeConfig(THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION)
            )
            dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
            dispatcher.start()
            try:
                await until(
                    lambda: (
                        len(server.sockets) == 3 and dispatcher.app.ready_event.is_set() and server.resume_calls >= 6
                    )
                )
                assert dispatcher.execution_state == "waiting_thread"
                assert inbox.active_jobs()[0]["state"] == "claimed"
                assert server.counter == 0
                incidents = [item for item in jobs.health if item["state"] == "offline"]
                assert incidents == [
                    {"component": "codex", "state": "offline", "error_code": "codex_transport_failed_or_unknown"}
                ]
                assert any(item["state"] == "waiting_thread" and item["error_code"] is None for item in jobs.health)
                server.peer_thread_status = "idle"
                await server.sockets[-1].send(
                    json.dumps(
                        {
                            "method": "turn/completed",
                            "params": {
                                "threadId": dispatcher.app.config.thread_id,
                                "turn": {"id": "manual", "status": "completed"},
                            },
                        }
                    )
                )
                await until(lambda: dispatcher.completed == 1)
                assert server.counter == 1
            finally:
                await stop_dispatcher(dispatcher)
                inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status",
    [
        {"enabled": True, "active": False, "outcome_unknown": False},
        {"enabled": False, "active": True, "outcome_unknown": False},
        {"enabled": False, "active": False, "outcome_unknown": True},
        {"enabled": False},
    ],
)
def test_worker_migration_denies_enabled_owned_active_or_unknown_wake(tmp_path, bundle, monkeypatch, status):
    config_path = tmp_path / "wake.json"
    original = json.dumps(
        {"thread_id": THREAD, "project_dir": wake.PROJECT_DIR, "app_socket": wake.APP_SOCKET}
    ).encode()
    config_path.write_bytes(original)
    config_path.chmod(0o600)
    original_load = wake.WakeConfig.load
    monkeypatch.setattr(wake, "CONFIG_PATH", config_path)
    monkeypatch.setattr(wake.WakeConfig, "load", classmethod(lambda cls, path=config_path: original_load(path)))
    monkeypatch.setattr(wake, "local_request", AsyncMock(return_value={"ok": True, **status}))
    monkeypatch.setattr(
        wake, "AppServer", lambda *_args: pytest.fail("guard must reject before App Server interaction")
    )
    monkeypatch.setattr(wake, "BridgeJobs", lambda *_args: pytest.fail("guard must reject before bridge interaction"))

    async def scenario():
        with pytest.raises(wake.WakeError, match="wake_worker_selection_not_paused"):
            await wake.select_worker(
                expected_thread_id=THREAD,
                expected_config_sha256=hashlib.sha256(original).hexdigest(),
                instruction_root=str(bundle),
                instruction_revision=REVISION,
            )
        assert config_path.read_bytes() == original

    asyncio.run(scenario())
