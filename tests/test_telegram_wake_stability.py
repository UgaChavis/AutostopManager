"""Executor lifecycle guards use only temporary paths and synthetic tasks."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from autostop_manager import telegram_wake as wake
from autostop_manager.telegram_inbox import TelegramInbox
from test_telegram_wake_durable import FakeJobs, bind_test_peer
from telegram_wake_fakes import THREAD


def test_wake_socket_owner_rejects_duplicate_and_preserves_replacement(tmp_path):
    path = tmp_path / "wake.sock"
    first = wake._WakeSocketOwner(path)
    first.acquire()
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(path))
        first.bound()
        original = path.lstat()
        second = wake._WakeSocketOwner(path)
        with pytest.raises(wake.WakeError, match=r"^wake_daemon_active$"):
            second.acquire()
        assert path.lstat().st_ino == original.st_ino
        second.close()
        assert path.exists()
        path.rename(tmp_path / "previous.sock")
        path.write_text("replacement")
        first.close()
        assert path.read_text() == "replacement"
    assert path.with_suffix(".lock").exists()


def test_bounded_cancel_retains_task_until_cleanup_finishes():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def pending_cleanup():
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                await release.wait()

        task = asyncio.create_task(pending_cleanup())
        await entered.wait()
        retained = set()
        started = time.monotonic()
        try:
            with pytest.raises(TimeoutError):
                await wake._cancel_task_until(task, asyncio.get_running_loop().time() + 0.02, retained)
            assert time.monotonic() - started < 0.5
            assert task in retained and not task.done()
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert not retained

    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["lifecycle_monitor", "worker_monitor"])
def test_pause_bounds_monitor_cleanup_and_blocks_resume(tmp_path, monkeypatch, field):
    async def scenario():
        release = asyncio.Event()
        entered = asyncio.Event()

        async def cleanup():
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                await release.wait()

        with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
            app = wake.AppServer(wake.WakeConfig(THREAD))
            dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), FakeJobs(inbox))
            task = asyncio.create_task(cleanup())
            setattr(dispatcher, field, task)
            await entered.wait()
            started = time.monotonic()
            try:
                with pytest.raises(wake.WakeError, match="wake_pause_outcome_unknown"):
                    await dispatcher.pause(persist=False)
                assert time.monotonic() - started < 0.5
                assert not dispatcher.enabled and dispatcher.terminal_stop_pending
                assert dispatcher.status()["cleanup_pending"]
                with pytest.raises(wake.WakeError, match="wake_cleanup_pending"):
                    await dispatcher.resume_execution()
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.sleep(0)

    monkeypatch.setattr(wake, "FAILED_RECOVERY_TIMEOUT", 0.02)
    asyncio.run(scenario())


def test_pause_deadline_includes_control_lock(tmp_path, monkeypatch):
    async def scenario():
        with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
            app = wake.AppServer(wake.WakeConfig(THREAD))
            jobs = FakeJobs(inbox)
            dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), jobs)
            await dispatcher.control_lock.acquire()
            try:
                started = time.monotonic()
                with pytest.raises(wake.WakeError, match="wake_pause_outcome_unknown"):
                    await dispatcher.pause(persist=False)
                assert time.monotonic() - started < 0.5
                assert not dispatcher.enabled and dispatcher.pause_requested
                assert jobs.calls == []
            finally:
                dispatcher.control_lock.release()
                await asyncio.sleep(0)

    monkeypatch.setattr(wake, "FAILED_RECOVERY_TIMEOUT", 0.02)
    asyncio.run(scenario())


def test_peer_thread_start_preserves_model_settings_without_history():
    app = wake.AppServer(wake.WakeConfig(THREAD))
    app.resumed_settings = {
        "model": "unchanged-model",
        "modelProvider": "unchanged-provider",
        "reasoningEffort": "high",
        "serviceTier": "priority",
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "sandbox": {"type": "dangerFullAccess"},
        "activePermissionProfile": None,
        "runtimeWorkspaceRoots": [wake.PROJECT_DIR],
    }
    params = app.peer_start_params()
    assert params == {
        "model": "unchanged-model",
        "modelProvider": "unchanged-provider",
        "serviceTier": "priority",
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "cwd": wake.PROJECT_DIR,
        "ephemeral": False,
        "config": {"model_reasoning_effort": "high"},
        "sandbox": "danger-full-access",
        "runtimeWorkspaceRoots": [wake.PROJECT_DIR],
    }
    app.resumed_settings["sandbox"] = {"type": "externalSandbox", "networkAccess": "enabled"}
    with pytest.raises(wake.WakeError, match="wake_worker_settings_unrepresentable"):
        app.peer_start_params()


def test_busy_legacy_bootstrap_cannot_authorize_first_peer_claim(tmp_path):
    async def scenario():
        with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
            app = wake.AppServer(wake.WakeConfig(THREAD))
            app.connected = True
            app.resume = AsyncMock(return_value="active")
            jobs = FakeJobs(inbox)
            dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), jobs)
            with pytest.raises(wake.WakeError, match="wake_legacy_execution_requires_review"):
                await dispatcher._check_bootstrap_idle()
            assert not dispatcher.bootstrap_checked
            assert jobs.calls == []

    asyncio.run(scenario())


@pytest.mark.skipif(os.geteuid() != 0, reason="bridge IPC requires root")
def test_bridge_heartbeat_refreshes_liveness_without_reclaiming(tmp_path, monkeypatch):
    async def scenario():
        calls = []
        release = asyncio.Event()

        async def handle(reader, writer):
            request = json.loads(await reader.readline())
            calls.append(request["operation"])
            try:
                if request["operation"] == "job_subscribe":
                    assert request["heartbeat"] is True
                    writer.write(b'{"ok":true,"generation":1,"heartbeat":false}\n')
                    await writer.drain()
                    await release.wait()
                    writer.write(b'{"ok":true,"generation":1,"heartbeat":true}\n')
                    await writer.drain()
                    await reader.read()
                else:
                    assert request["operation"] == "job_status"
                    writer.write(
                        b'{"ok":true,"status":{"pending_events":0,"execution_paused":false,"worker_protocol":2,"context_mode":"serial_peer_v1"}}\n'
                    )
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        monkeypatch.setattr(wake, "BRIDGE_STALE_SECONDS", 0.08)
        async with await asyncio.start_unix_server(handle, path=str(tmp_path / "bridge.sock")):
            jobs = wake.BridgeJobs(os.geteuid(), tmp_path / "bridge.sock")
            jobs.start()
            try:
                await asyncio.wait_for(jobs.changed.wait(), 0.5)
                jobs.changed.clear()
                release.set()
                for _ in range(20):
                    if calls.count("job_status") >= 2:
                        break
                    await asyncio.sleep(0.005)
                assert jobs.status_fresh
                assert not jobs.changed.is_set()
                assert "job_claim" not in calls
                await asyncio.sleep(0.1)
                assert not jobs.status_fresh
            finally:
                await jobs.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure", ["bind_reply", "start_reply", "shared_rejection", "peer_protocol", "bootstrap_protocol"]
)
def test_peer_creation_unknowns_use_exact_readback_and_never_recreate(tmp_path, failure):
    async def scenario():
        with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
            inbox.duty_set(True)
            inbox.record(123, 1)
            inbox.record(124, 2)
            job = inbox.claim(instruction_root=str(tmp_path), instruction_revision="a" * 40)

            class Jobs(FakeJobs):
                async def request(self, operation, **arguments):
                    result = await super().request(operation, **arguments)
                    if operation == "job_peer_thread_bind" and failure == "bind_reply":
                        raise wake.TransportLost("wake_bridge_disconnected")
                    return result

            app = wake.AppServer(wake.WakeConfig(THREAD))
            app.ensure_ready = AsyncMock()
            app.resumed_settings = {
                "model": "same",
                "modelProvider": "same",
                "serviceTier": None,
                "approvalPolicy": "never",
                "approvalsReviewer": "user",
                "sandbox": {"type": "dangerFullAccess"},
            }
            ident = "00000000-0000-4000-8000-000000000002"
            app.request = AsyncMock(
                return_value={
                    "thread": {
                        "id": ident,
                        "cwd": wake.PROJECT_DIR,
                        "ephemeral": False,
                        "turns": [],
                        "status": {"type": "idle"},
                    }
                }
            )
            if failure == "start_reply":
                app.request.side_effect = wake.TransportLost("codex_disconnected")
            elif failure == "shared_rejection":
                app.request.side_effect = wake.RPCRejected("codex_rpc_rejected")
            elif failure == "bootstrap_protocol":
                app.ensure_ready.side_effect = wake.WakeError("codex_rpc_frame_invalid")
            jobs = Jobs(inbox)
            dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), jobs)
            dispatcher._use_thread = AsyncMock()
            if failure == "peer_protocol":
                dispatcher._use_thread.side_effect = wake.WakeError("codex_rpc_frame_invalid")
            if failure == "bind_reply":
                await dispatcher._select_peer(job)
                dispatcher._use_thread.assert_awaited_once_with(ident)
                assert inbox.read_peer_thread(job["job_id"])["thread_id"] == ident
                assert [name for name, _args in jobs.calls].count("job_peer_thread_read") == 1
            elif failure == "start_reply":
                with pytest.raises(wake.WakeError, match="wake_peer_thread_unknown"):
                    await dispatcher._select_peer(job)
                assert inbox.read_peer_thread(job["job_id"])["state"] == "creation_unknown"
                dispatcher._use_thread.assert_not_awaited()
            else:
                await dispatcher._execute(job)
                assert dispatcher.execution_error == (
                    "codex_rpc_frame_invalid"
                    if failure in {"peer_protocol", "bootstrap_protocol"}
                    else "wake_shared_backend_rejected"
                )
                assert inbox.status()["execution_paused"] is True
                assert inbox.execution_slot() is None
                assert inbox.read_job(job["job_id"])["execution_proof"] == "not_started"
                assert inbox.claim(instruction_root=str(tmp_path), instruction_revision="a" * 40) is None
                if failure == "peer_protocol":
                    dispatcher._use_thread.assert_awaited_once_with(ident)
                else:
                    dispatcher._use_thread.assert_not_awaited()
            if failure == "bootstrap_protocol":
                app.request.assert_not_awaited()
            else:
                expected = ["thread/start"]
                if failure in {"bind_reply", "peer_protocol"}:
                    expected.extend(["thread/archive", "thread/unarchive"])
                assert [call.args[0] for call in app.request.await_args_list] == expected

    asyncio.run(scenario())


def test_persisted_twenty_minute_budget_includes_preparation(tmp_path, monkeypatch):
    async def scenario():
        # The request was claimed 1199.96 seconds ago, before this executor
        # attached. Only the original deadline's remaining time is available.
        past = datetime.now(UTC)
        with TelegramInbox(tmp_path / "inbox.sqlite3", clock=lambda: past) as inbox:
            inbox.duty_set(True)
            inbox.record(123, 1)
            job = inbox.claim(instruction_root=str(tmp_path), instruction_revision="a" * 40)
            deadline = job["deadline_at"]

            class Clock:
                fromisoformat = staticmethod(datetime.fromisoformat)

                @staticmethod
                def now(_tz):
                    return past + timedelta(seconds=wake.REQUEST_TIMEOUT_SECONDS - 0.04)

            monkeypatch.setattr(wake, "datetime", Clock)
            entered = asyncio.Event()

            async def prepare(_job):
                entered.set()
                await asyncio.Event().wait()

            app = wake.AppServer(wake.WakeConfig(THREAD))
            app.request = AsyncMock(side_effect=AssertionError("preparation must not start a model turn"))
            dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), FakeJobs(inbox))
            dispatcher._select_peer = prepare
            await asyncio.wait_for(dispatcher._execute(job), 0.5)
            assert entered.is_set() and dispatcher.deadline_exceeded
            stored = inbox.read_job(job["job_id"])
            assert stored["deadline_at"] == deadline
            assert stored["state"] == "needs_review"
            assert stored["reason_code"] == "wake_request_deadline_exceeded"
            assert stored["execution_proof"] == "not_started"
            assert inbox.execution_slot() is None
            app.request.assert_not_awaited()

    asyncio.run(scenario())


def test_restart_reuses_original_deadline_instead_of_resetting_budget(tmp_path, monkeypatch):
    async def scenario():
        start = datetime.now(UTC)
        with TelegramInbox(tmp_path / "inbox.sqlite3", clock=lambda: start) as inbox:
            inbox.duty_set(True)
            inbox.record(123, 1)
            job = inbox.claim(instruction_root=str(tmp_path), instruction_revision="a" * 40)

            class Clock:
                current = start + timedelta(seconds=1190)
                fromisoformat = staticmethod(datetime.fromisoformat)

                @classmethod
                def now(cls, _tz):
                    return cls.current

            monkeypatch.setattr(wake, "datetime", Clock)
            first = wake.DurableWakeDispatcher(wake.AppServer(wake.WakeConfig(THREAD)), os.geteuid(), FakeJobs(inbox))
            first.request_deadline = first._deadline(job)
            assert 9.9 < first.request_deadline - asyncio.get_running_loop().time() <= 10
            original = first.request_deadline
            Clock.current += timedelta(seconds=5)
            # A reconnect cannot overwrite the running coordinator's deadline.
            assert first.request_deadline == original
            second = wake.DurableWakeDispatcher(wake.AppServer(wake.WakeConfig(THREAD)), os.geteuid(), FakeJobs(inbox))
            remaining = second._deadline(inbox.read_job(job["job_id"])) - asyncio.get_running_loop().time()
            assert 4.9 < remaining <= 5

    asyncio.run(scenario())


def test_completed_result_and_simultaneous_pauses_keep_slot_until_local_cleanup(tmp_path):
    async def scenario():
        with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
            inbox.duty_set(True)
            inbox.record(123, 1)
            inbox.record(124, 2)
            job = inbox.claim(instruction_root=str(tmp_path), instruction_revision="a" * 40)
            bind_test_peer(inbox, job)
            job = inbox.start_intent(
                job_id=job["job_id"], lease=job["lease"], expected_revision=job["revision"], thread_id=THREAD
            )
            job = inbox.bind_turn(
                job_id=job["job_id"], lease=job["lease"], expected_revision=job["revision"], turn_id="owned"
            )
            job = inbox.result(
                job_id=job["job_id"],
                lease=job["lease"],
                expected_revision=job["revision"],
                outcome="no_reply_needed",
                event_ids=job["event_ids"],
                receipt_keys=[],
                reason_code="not_actionable",
            )
            app = wake.AppServer(wake.WakeConfig(THREAD))
            app.connected = True
            app.ready_event.set()
            app.active_turn = "owned"
            app.outcome_unknown = True
            app._turn_metadata = AsyncMock(return_value="completed")
            app.interrupt = AsyncMock(side_effect=AssertionError("terminal turn must not be interrupted"))
            dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), FakeJobs(inbox))
            dispatcher.current_job = job
            dispatcher.request_deadline = dispatcher._deadline(job)
            cleanup_entered = asyncio.Event()
            release = asyncio.Event()

            async def local_execution():
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup_entered.set()
                    await release.wait()

            task = asyncio.create_task(local_execution())
            dispatcher.execution_task = task
            await asyncio.sleep(0)
            first = asyncio.create_task(dispatcher.pause(persist=False))
            await asyncio.wait_for(cleanup_entered.wait(), 0.5)
            second = asyncio.create_task(dispatcher.pause(persist=False))
            await asyncio.sleep(0)
            try:
                assert not first.done() and not second.done()
                assert inbox.execution_slot() is not None
                assert inbox.claim(instruction_root=str(tmp_path), instruction_revision="a" * 40) is None
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(first, second), 0.5)
                await asyncio.gather(task, return_exceptions=True)
            assert inbox.execution_slot() is None
            assert inbox.read_job(job["job_id"])["execution_proof"] == "completed"
            assert dispatcher.current_job is None and dispatcher.request_deadline is None
            assert dispatcher.stop_proof_verified
            app.interrupt.assert_not_awaited()

    asyncio.run(scenario())
