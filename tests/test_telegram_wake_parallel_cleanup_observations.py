"""Parallel wake observations and retirement ownership over private fake IPC."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import os
from pathlib import Path
import socket
import subprocess

import pytest
from telegram_wake_fakes import THREAD, MockServer, until
from test_telegram_wake_durable import REVISION, FakeJobs
from test_telegram_wake_durable import bundle as bundle
from websockets.asyncio.server import unix_serve

from autostop_manager import telegram_wake as wake
from autostop_manager.telegram_inbox import TelegramInbox


@pytest.fixture(autouse=True)
def isolated_boundaries(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "unused-manager.sqlite3"))
    original_connect = wake.AppServer.connect
    socket_connect = socket.socket.connect

    async def connect(app):
        assert Path(app.config.app_socket).parent.resolve() == tmp_path.resolve()
        await original_connect(app)

    def connect_socket(sock, address):
        assert sock.family == socket.AF_UNIX, "only private fake Unix IPC is allowed"
        return socket_connect(sock, address)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no DNS, process, model or external transport")

    monkeypatch.setattr(wake.AppServer, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect", connect_socket)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.mark.parametrize("one_failed_peer", [False, True])
def test_root_timings_observe_real_parallel_children_and_preserve_peer_isolation(
    tmp_path, bundle, monkeypatch, one_failed_peer
):
    select_peer = wake.DurableWakeDispatcher._select_peer

    async def select(self, job):
        if one_failed_peer and job["peer_id"] == 101:
            raise wake.WakeError("codex_thread_busy")
        await select_peer(self, job)

    monkeypatch.setattr(wake.DurableWakeDispatcher, "_select_peer", select)

    async def scenario():
        server = MockServer(peer_threads=True, strict_peer_lifecycle=True, complete=True)
        path = str(tmp_path / "app.sock")
        with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
            inbox.duty_set(True)
            for peer in (101, 102, 103):
                inbox.record(peer, 1)
            jobs = FakeJobs(inbox, finish=True)
            async with unix_serve(server.handle, path, compression=None):
                app = wake.AppServer(
                    wake.WakeConfig(
                        THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION
                    )
                )
                dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), jobs)
                children = []
                launch = dispatcher._launch

                def observe(job, *, recovering=False):
                    launch(job, recovering=recovering)
                    children.append(dispatcher.executions[job["job_id"]][0])

                monkeypatch.setattr(dispatcher, "_launch", observe)
                dispatcher.start()
                try:
                    expected_completed = 2 if one_failed_peer else 3
                    await until(lambda: len(children) == 3 and not dispatcher.executions)
                    assert dispatcher.completed == expected_completed
                    assert server.counter == expected_completed
                    assert not inbox.execution_slots() and not dispatcher.unsettled
                    # Positive control: the production child path actually measured work.
                    assert all(
                        child._stage_timings.snapshot()["new"]["peer_prepare"]["count"] > 0 for child in children
                    )
                    timings = dispatcher.status()["stage_timings"]
                    assert timings["new"]["peer_prepare"]["count"] == 3
                    assert timings["new"]["turn_lifecycle"]["count"] == expected_completed
                    assert timings["new"]["finish_request"]["count"] == 3
                    assert timings["new"]["peer_prepare"]["raised"] == int(one_failed_peer)
                    assert all(stage["in_flight"] == 0 for stage in timings["new"].values())
                    assert timings["recovery"]["turn_lifecycle"]["count"] == 0
                finally:
                    await dispatcher.close()
                assert not any(task is not asyncio.current_task() and not task.done() for task in asyncio.all_tasks())

    asyncio.run(scenario())


def _assert_expired_wait(inbox, second_event, dispatcher, jobs):
    expired = next(job for job in inbox.active_jobs() if second_event in job["event_ids"])
    assert expired["state"] == "needs_review"
    assert expired["reason_code"] == "wake_request_deadline_exceeded"
    assert expired["execution_proof"] == "not_started"
    assert expired["turn_id"] is None and not inbox.execution_slots()
    assert not any(
        operation == "job_progress" and arguments["job_id"] == expired["job_id"] for operation, arguments in jobs.calls
    )


async def _controlled_close(app, original_close, release, closing, held_apps, delayed_tasks, *, delay, fail):
    if delay and app.client_peer and (not held_apps or app is held_apps[0]):
        if not held_apps:
            held_apps.append(app)
        delayed_tasks.append(asyncio.current_task())
        closing.set()
        if fail and not release.is_set():
            raise OSError("synthetic peer close failure")
        while not release.is_set():
            with suppress(asyncio.CancelledError):
                await release.wait()
    await original_close(app)


@pytest.mark.parametrize("mode", ["ordinary", "delay", "late_cancel", "deadline", "early_cancel", "close_error"])
def test_parent_retains_cleanup_until_post_proof_peer_retirement_finishes(tmp_path, bundle, monkeypatch, mode):
    original_close = wake.AppServer.close
    original_retire = wake.DurableWakeDispatcher._retire_peer
    delayed_close = mode != "ordinary"
    shutdown_pending = mode in {"late_cancel", "early_cancel"}
    expire_wait = mode == "deadline"
    shutdown_early = mode == "early_cancel"
    close_error = mode == "close_error"

    async def scenario():
        release, closing = asyncio.Event(), asyncio.Event()
        delayed_tasks = []
        retired = []
        held_apps = []

        async def close(app):
            await _controlled_close(
                app, original_close, release, closing, held_apps, delayed_tasks, delay=delayed_close, fail=close_error
            )

        async def retire(child, deadline):
            if child.parent is not None and child.stop_proof_verified:
                if not shutdown_early:
                    deadline = min(deadline, asyncio.get_running_loop().time() + 0.02)
                retired.append(child)
            await original_retire(child, deadline)

        monkeypatch.setattr(wake.AppServer, "close", close)
        monkeypatch.setattr(wake.DurableWakeDispatcher, "_retire_peer", retire)
        server = MockServer(peer_threads=True, strict_peer_lifecycle=True, complete=True)
        path = str(tmp_path / "app.sock")
        with TelegramInbox(tmp_path / "inbox.sqlite3") as inbox:
            inbox.duty_set(True)
            inbox.record(101, 1)
            jobs = FakeJobs(inbox, finish=True)
            async with unix_serve(server.handle, path, compression=None):
                app = wake.AppServer(
                    wake.WakeConfig(
                        THREAD, app_socket=path, instruction_root=str(bundle), instruction_revision=REVISION
                    )
                )
                dispatcher = wake.DurableWakeDispatcher(app, os.geteuid(), jobs)
                dispatcher.start()
                try:
                    await until(lambda: dispatcher.completed == 1 and retired)
                    assert server.counter == 1 and not inbox.execution_slots()
                    assert retired and retired[0].stop_proof_verified
                    if delayed_close:
                        await asyncio.wait_for(closing.wait(), timeout=1)
                        if not shutdown_early:
                            await until(lambda: bool(dispatcher.unsettled))
                        assert closing.is_set()
                        if not close_error:
                            assert any(not task.done() for task in delayed_tasks)
                        if not shutdown_early and not close_error:
                            assert any(not task.done() for task in retired[0].cleanup_tasks)
                        # A successful terminal proof is not completion of later transport cleanup.
                        if not shutdown_early:
                            assert retired[0] in dispatcher.unsettled.values()
                        if not close_error:
                            assert any(owner is retired[0] for owner, _task in dispatcher.executions.values())
                        if shutdown_pending:
                            await asyncio.wait_for(dispatcher.close(), timeout=7)
                            assert dispatcher.cleanup_pending
                            assert dispatcher.terminal_stop_pending
                            assert retired[0] in dispatcher.unsettled.values()
                            assert any(not task.done() for task in retired[0].cleanup_tasks)
                            assert server.counter == 1 and not inbox.execution_slots()
                        else:
                            first_thread = retired[0].current_job["thread_id"]
                            if expire_wait:
                                original_deadline = wake.DurableWakeDispatcher._deadline

                                def deadline(child, job):
                                    if job["peer_id"] == 101:
                                        return asyncio.get_running_loop().time() - 1
                                    return original_deadline(child, job)

                                monkeypatch.setattr(wake.DurableWakeDispatcher, "_deadline", deadline)
                            second_event = inbox.record(101, 2)
                            inbox.record(102, 1)
                            jobs.changed.set()
                            await until(lambda: server.counter == 2 and dispatcher.completed == 2)
                            assert list(server.turn_threads.values()).count(first_thread) == 1
                            if close_error:
                                waiting = next(job for job in inbox.active_jobs() if second_event in job["event_ids"])
                                await until(lambda: waiting["job_id"] in dispatcher.unsettled)
                                assert dispatcher.unsettled[waiting["job_id"]].terminal_stop_pending
                                assert dispatcher.unsettled[waiting["job_id"]].execution_task is None
                                assert inbox.execution_slot(job_id=waiting["job_id"])["slot_phase"] == "claimed"
                                assert not retired[0].cleanup_tasks
                            elif expire_wait:
                                await until(lambda: dispatcher.failed == 1)
                                _assert_expired_wait(inbox, second_event, dispatcher, jobs)
                            else:
                                assert inbox.execution_slots()[0]["peer_id"] == 101
                                assert inbox.execution_slots()[0]["slot_phase"] == "claimed"
                            release.set()
                            if close_error:
                                await until(lambda: not dispatcher.executions)
                                assert server.counter == 2 and dispatcher.unsettled
                            else:
                                expected = 2 if expire_wait else 3
                                await until(lambda: server.counter == expected and not dispatcher.executions)
                                assert list(server.turn_threads.values()).count(first_thread) == (
                                    1 if expire_wait else 2
                                )
                                assert dispatcher.completed == expected and not dispatcher.unsettled
                    else:
                        await until(lambda: not dispatcher.executions)
                        assert not dispatcher.cleanup_pending
                finally:
                    release.set()
                    await asyncio.gather(*delayed_tasks, return_exceptions=True)
                    await dispatcher.close()
                assert not any(task is not asyncio.current_task() and not task.done() for task in asyncio.all_tasks())

    asyncio.run(scenario())
