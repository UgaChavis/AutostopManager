"""Canonical policy delivery over synthetic sockets and persistent inboxes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
import json
import os
import threading
from unittest.mock import AsyncMock

import pytest
from instruction_bundle_fakes import create_instruction_bundle
from telegram_wake_fakes import THREAD, MockServer, until
from telegram_wake_fakes import quick_recovery as quick_recovery
from test_telegram_wake_durable import FakeJobs, active_child, new_inbox, stop_dispatcher
from websockets.asyncio.server import unix_serve

from autostop_manager import instruction_bundle as bundles
from autostop_manager import telegram_wake as wake
from autostop_manager.telegram_inbox import TelegramInbox

REVISION = "a" * 40


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    monkeypatch.setattr(bundles, "_OWNER_UID", os.getuid())
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex-home"))
    old = tmp_path / "old-checkout"
    old.mkdir()
    (old / "AGENTS.md").write_text("STALE_POLICY_FROM_CWD: publish procurement costs.\n")
    skill = old / ".agents/skills/manage-autostop-client/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("STALE_CLIENT_SKILL_FROM_CWD\n")
    monkeypatch.setattr(wake, "PROJECT_DIR", str(old))
    releases = tmp_path / "releases"
    monkeypatch.setattr(wake, "INSTRUCTION_RELEASE_ROOTS", (releases,))
    return create_instruction_bundle(releases / "current-bundle", REVISION), str(old)


def config_for(pinned, socket):
    root, cwd = pinned
    return wake.WakeConfig(
        THREAD, project_dir=cwd, app_socket=socket, instruction_root=str(root), instruction_revision=REVISION
    )


@asynccontextmanager
async def running(tmp_path, pinned, server, *, finish=True):
    inbox, _event_id = new_inbox(tmp_path)
    jobs = FakeJobs(inbox, finish=finish)
    path = str(tmp_path / "app.sock")
    async with unix_serve(server.handle, path, compression=None):
        dispatcher = wake.DurableWakeDispatcher(wake.AppServer(config_for(pinned, path)), 123, jobs)
        dispatcher.start()
        try:
            yield inbox, jobs, dispatcher
        finally:
            await stop_dispatcher(dispatcher)
            inbox.close()


def restrictions():
    return [{"path": f"/synthetic-disabled/{number}", "enabled": False} for number in range(27)]


def assert_worker_config(config, pinned):
    assert config["project_doc_max_bytes"] == 0
    assert config["skills.include_instructions"] is False
    assert "skills" not in config and "skills.config" not in config


def hold_verification(monkeypatch):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    load = wake._load_instruction_bundle
    calls = []

    def delayed(root, revision):
        calls.append((root, revision))
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3), "verification hold was not released"
        return load(root, revision)

    monkeypatch.setattr(wake, "_load_instruction_bundle", delayed)
    return entered, release, calls


@pytest.mark.skipif(os.geteuid() != 0, reason="Wake status IPC is root-only")
def test_worker_answers_unix_status_during_pinned_verification(tmp_path, pinned, monkeypatch):
    async def scenario():
        entered, release, calls = hold_verification(monkeypatch)
        server = MockServer(peer_threads=True)
        path = tmp_path / "status.sock"
        async with running(tmp_path, pinned, server) as (_inbox, jobs, dispatcher):
            try:
                await asyncio.wait_for(entered.wait(), 1)
                async with await asyncio.start_unix_server(dispatcher.serve, path=path):
                    status = await asyncio.wait_for(wake.local_request({"operation": "status"}, path), 1)
                assert status["ok"] and calls == [(str(pinned[0]), REVISION)]
                assert not any(operation == "job_claim" for operation, _arguments in jobs.calls)
            finally:
                release.set()
            await until(lambda: dispatcher.completed == 1)

    asyncio.run(scenario())


def test_cancelled_verification_uses_its_slot_until_the_read_finishes(tmp_path, pinned, monkeypatch):
    async def scenario():
        entered, release, calls = hold_verification(monkeypatch)
        verifier = wake.AppServer(config_for(pinned, str(tmp_path / "app.sock"))).instruction_verifier
        key = str(pinned[0]), REVISION
        first = asyncio.create_task(verifier.load(*key))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            entered.clear()
            second = asyncio.create_task(verifier.load(*key))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(entered.wait(), 0.05)
            assert calls == [key]
        finally:
            release.set()
        assert (await asyncio.wait_for(second, 1)).revision == REVISION
        assert (await verifier.load(*key)).revision == REVISION
        assert calls == [key, key, key]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ("pause", "codex_paused"),
        ("archive", "codex_thread_archived"),
        ("generation", "codex_start_connection_changed"),
        ("thread", "codex_start_connection_changed"),
        ("foreign_turn", "codex_thread_context_changed"),
        ("pin", "wake_instruction_bundle_mismatch"),
        ("digest", "wake_instruction_bundle_mismatch"),
    ],
)
def test_changes_during_final_verification_prevent_intent_and_turn(tmp_path, pinned, monkeypatch, change, error):
    async def scenario():
        config = config_for(pinned, str(tmp_path / "app.sock"))
        app = wake.AppServer(config)
        bundle = wake._load_instruction_bundle(str(pinned[0]), REVISION)
        app.prepare_instructions(bundle, {})
        app.connected = True
        app.managed = change == "thread"
        app.ready_event.set()
        app.resume = AsyncMock(return_value="idle")
        app.request = AsyncMock(side_effect=AssertionError("stale verification cannot authorize RPC"))
        intent = AsyncMock()
        entered, release, _calls = hold_verification(monkeypatch)
        task = asyncio.create_task(app._start_turn("synthetic", before_start=intent))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if change == "pause":
                app.pausing = True
            elif change == "archive":
                app.thread_archive_sequence += 1
            elif change == "generation":
                app.generation += 1
            elif change == "thread":
                app.config = replace(app.config, thread_id="12345678-1234-1234-1234-123456789000")
            elif change == "foreign_turn":
                app.thread_notification_sequence += 1
            elif change == "pin":
                app.config = replace(app.config, instruction_revision="b" * 40)
            else:
                (pinned[0] / "AGENTS.md").write_text("modified after original verification\n")
        finally:
            release.set()
        with pytest.raises(wake.WakeError, match=error):
            await asyncio.wait_for(task, 1)
        intent.assert_not_called()
        app.request.assert_not_called()
        assert not app.turn_start_attempted and not app.outcome_unknown

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["pause", "archive", "generation"])
def test_changes_during_start_intent_keep_unknown_without_injecting(tmp_path, pinned, change):
    async def scenario():
        app = wake.AppServer(config_for(pinned, str(tmp_path / "app.sock")))
        app.prepare_instructions(wake._load_instruction_bundle(str(pinned[0]), REVISION), {})
        app.connected = True
        app.resume = AsyncMock(return_value="idle")
        app.request = AsyncMock(side_effect=AssertionError("stale intent cannot authorize RPC"))

        async def commit_intent():
            if change == "pause":
                app.pausing = True
            elif change == "archive":
                app.thread_archive_sequence += 1
            else:
                app.generation += 1

        intent = AsyncMock(side_effect=commit_intent)
        with pytest.raises(wake.WakeError):
            await app._start_turn("synthetic", before_start=intent)
        intent.assert_awaited_once()
        app.request.assert_not_called()
        assert not app.turn_start_attempted and app.outcome_unknown

    asyncio.run(scenario())


def test_canonical_policy_precedes_generation_and_preserves_execution_settings(tmp_path, pinned):
    async def scenario():
        server = MockServer(peer_threads=True, strict_peer_lifecycle=True)
        selectors = restrictions()
        server.effective_config = {
            "skills": {"config": selectors},
            "developer_instructions": "Preserve this individual configuration value.",
            "model": "unmodified-model-setting",
        }
        before = deepcopy(server.effective_config)
        server.thread_settings[THREAD] = {
            "reasoningEffort": "xhigh",
            "serviceTier": "priority",
            "activePermissionProfile": {"id": "existing-profile", "extends": None},
        }
        async with running(tmp_path, pinned, server) as (_inbox, _jobs, dispatcher):
            await until(lambda: dispatcher.completed == 1)
            methods = [call["method"] for call in server.calls]
            assert "config/read" not in methods
            assert methods.index("thread/inject_items") < methods.index("turn/start")
            injection = next(call["params"] for call in server.calls if call["method"] == "thread/inject_items")
            turn = next(call["params"] for call in server.calls if call["method"] == "turn/start")
            assert injection["threadId"] == turn["threadId"] != THREAD
            assert injection["items"][0]["role"] == "developer"
            policy = injection["items"][0]["content"][0]["text"]
            root, _cwd = pinned
            assert (root / "AGENTS.md").read_text() in policy
            assert (root / ".agents/skills/manage-autostop-client/SKILL.md").read_text() in policy
            assert "STALE_POLICY_FROM_CWD" not in policy and "STALE_CLIENT_SKILL_FROM_CWD" not in policy
            assert "$manage-autostop-client" not in turn["input"][0]["text"]
            assert turn["effort"] == "xhigh"
            for call in server.calls:
                if call["method"] in {"thread/start", "thread/resume", "turn/start"}:
                    assert "baseInstructions" not in call["params"]
                    assert "developerInstructions" not in call["params"]
            starts = [call["params"] for call in server.calls if call["method"] == "thread/start"]
            resumes = [
                call["params"]
                for call in server.calls
                if call["method"] == "thread/resume" and call["params"]["threadId"] == injection["threadId"]
            ]
            for params in starts + resumes:
                assert_worker_config(params["config"], pinned)
                assert params["config"]["model_reasoning_effort"] == "xhigh"
                assert params["permissions"] == "existing-profile"
                assert params["model"] == "test-model" and params["modelProvider"] == "test-provider"
                assert params["approvalPolicy"] == "never" and params["serviceTier"] == "priority"
            assert server.effective_config == before
            status = dispatcher.status()
            assert status["instruction_sha256"] == status["instruction_protocol_sha256"]
            assert status["instruction_root"] == str(root)
            assert status["instruction_model_context_verified"] is False

    asyncio.run(scenario())


def test_reconnect_keeps_config_and_does_not_inject_or_replay_until_new_job(tmp_path, pinned, quick_recovery):
    async def scenario():
        server = MockServer(peer_threads=True, strict_peer_lifecycle=True)
        server.effective_config = {"skills": {"config": restrictions()}}
        async with running(tmp_path, pinned, server) as (inbox, jobs, dispatcher):
            await until(lambda: dispatcher.completed == 1)
            peer_thread = server.created_threads[0]
            old_sockets = len(server.sockets)
            await dispatcher.bootstrap_app.websocket.close()
            await until(lambda: len(server.sockets) > old_sockets and dispatcher.bootstrap_app.ready_event.is_set())
            assert server.counter == 1
            assert len([call for call in server.calls if call["method"] == "thread/inject_items"]) == 1
            params = [
                call["params"]
                for call in server.calls
                if call["method"] == "thread/resume" and call["params"]["threadId"] == peer_thread
            ][-1]
            assert_worker_config(params["config"], pinned)
            assert inbox.record(123, 2) is not None
            jobs.changed.set()
            await until(lambda: dispatcher.completed == 2)
            assert server.counter == 2
            injections = [call["params"] for call in server.calls if call["method"] == "thread/inject_items"]
            assert len(injections) == 2 and injections[0] == injections[1]

    asyncio.run(scenario())


def test_busy_peer_does_not_receive_instruction_append_until_idle(tmp_path, pinned):
    async def scenario():
        server = MockServer(peer_threads=True)
        server.peer_thread_status = "active"
        async with running(tmp_path, pinned, server) as (_inbox, _jobs, dispatcher):
            await until(
                lambda: (
                    active_child(dispatcher) is not None
                    and active_child(dispatcher).execution_state == "waiting_thread"
                    and server.resume_calls >= 4
                )
            )
            assert not any(call["method"] in {"thread/inject_items", "turn/start"} for call in server.calls)
            server.peer_thread_status = "idle"
            await server.sockets[-1].send(
                json.dumps(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": active_child(dispatcher).app.config.thread_id,
                            "turn": {"id": "manual", "status": "completed"},
                        },
                    }
                )
            )
            await until(lambda: dispatcher.completed == 1)
            assert server.counter == 1

    asyncio.run(scenario())


def test_foreign_turn_after_idle_proof_prevents_append_and_generation(tmp_path, pinned):
    async def scenario():
        server = MockServer(peer_threads=True)
        inbox, _event_id = new_inbox(tmp_path)

        class ChangingJobs(FakeJobs):
            async def request(self, operation, **arguments):
                response = await super().request(operation, **arguments)
                if operation == "job_start_intent":
                    peer_app = active_child(dispatcher).app
                    sequence = peer_app.thread_notification_sequence
                    peer = peer_app.config.thread_id
                    server.thread_statuses[peer] = "active"
                    await server.sockets[-1].send(
                        json.dumps(
                            {
                                "method": "thread/status/changed",
                                "params": {"threadId": peer, "status": {"type": "active", "activeFlags": []}},
                            }
                        )
                    )
                    await until(lambda: peer_app.thread_notification_sequence > sequence)
                return response

        jobs = ChangingJobs(inbox)
        path = str(tmp_path / "app.sock")
        async with unix_serve(server.handle, path, compression=None):
            dispatcher = wake.DurableWakeDispatcher(wake.AppServer(config_for(pinned, path)), 123, jobs)
            dispatcher.start()
            try:
                await until(lambda: inbox.status()["needs_review_jobs"] == 1 and not dispatcher.active)
                job = inbox.active_jobs()[0]
                assert job["reason_code"] == "codex_thread_context_changed"
                assert job["execution_proof"] == "not_started" and job["turn_id"] is None
                assert server.counter == 0
                assert not any(
                    call["method"] in {"thread/inject_items", "turn/start", "turn/interrupt"} for call in server.calls
                )
                assert not any(operation == "job_defer_failed" for operation, _arguments in jobs.calls)
            finally:
                await stop_dispatcher(dispatcher)
                inbox.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("problem", ["rejected", "disconnected", "malformed"])
def test_delivery_failure_persists_review_and_never_starts_or_replays(tmp_path, pinned, quick_recovery, problem):
    async def scenario():
        server = MockServer(peer_threads=True)
        if problem == "rejected":
            server.reject_method = "thread/inject_items"
        elif problem == "disconnected":
            server.disconnect_injection = True
        else:
            server.injection_response = ["malformed-response"]
        inbox, event_id = new_inbox(tmp_path)
        path = str(tmp_path / "app.sock")
        config = config_for(pinned, path)
        async with unix_serve(server.handle, path, compression=None):
            dispatcher = wake.DurableWakeDispatcher(wake.AppServer(config), 123, FakeJobs(inbox))
            dispatcher.start()
            try:
                await until(lambda: inbox.status()["execution_paused"] and not dispatcher.active)
                job = inbox.active_jobs()[0]
                assert job["state"] == "needs_review" and job["execution_proof"] == "not_started"
                expected = "rejected" if problem == "rejected" else "unknown"
                assert job["reason_code"] == "wake_instruction_delivery_" + expected
                assert job["thread_id"] is not None and job["turn_id"] is None
                assert inbox.execution_slot() is None and inbox.event_state(event_id) == "open"
                assert server.counter == 0
                assert not any(call["method"] == "turn/start" for call in server.calls)
                assert len([call for call in server.calls if call["method"] == "thread/inject_items"]) == 1
            finally:
                await stop_dispatcher(dispatcher)
                inbox.close()
            # A successful reconnect/restart is not permission to replay this append or turn.
            server.reject_method = None
            server.disconnect_injection = False
            server.injection_response = {}
            reopened = TelegramInbox(tmp_path / "db" / "inbound.sqlite3")
            second = wake.DurableWakeDispatcher(wake.AppServer(config), 123, FakeJobs(reopened))
            second.start()
            try:
                await until(lambda: second.execution_state in {"paused", "held"})
                assert reopened.status()["needs_review_jobs"] == 1
                assert reopened.active_jobs()[0]["job_id"] == job["job_id"]
                assert server.counter == 0
                assert len([call for call in server.calls if call["method"] == "thread/inject_items"]) == 1
            finally:
                await stop_dispatcher(second)
                reopened.close()

    asyncio.run(scenario())


def test_existing_peer_keeps_thread_only_skill_restrictions_without_reading_file_defaults(tmp_path, pinned):
    async def scenario():
        server = MockServer(peer_threads=True, strict_peer_lifecycle=True)
        server.reject_method = "config/read"
        async with running(tmp_path, pinned, server) as (inbox, jobs, dispatcher):
            await until(lambda: dispatcher.completed == 1)
            peer = dispatcher.app.config.thread_id
            private_selectors = [{"path": "/thread-only/restricted-skill", "enabled": False}]
            server.thread_configs[peer] = {
                "skills": {"config": deepcopy(private_selectors), "max_context_tokens": 123},
                "unrelated": "preserved",
            }
            server.effective_config = {"skills": {"config": []}}
            assert inbox.record(123, 2) is not None
            jobs.changed.set()
            await until(lambda: dispatcher.completed == 2)
            assert server.thread_configs[peer]["skills"]["config"] == private_selectors
            assert server.thread_configs[peer]["skills"]["max_context_tokens"] == 123
            assert server.thread_configs[peer]["unrelated"] == "preserved"
            assert "config/read" not in [call["method"] for call in server.calls]
            for call in server.calls:
                if call["method"] in {"thread/start", "thread/resume"} and "config" in call["params"]:
                    assert_worker_config(call["params"]["config"], pinned)

    asyncio.run(scenario())
