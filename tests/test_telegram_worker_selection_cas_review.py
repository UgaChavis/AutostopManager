"""Synthetic worker-selection CAS regressions using private files and processes.

Four distinct scenarios, four pytest runs, no interface repetitions. The public
select_worker path keeps its real config/bundle validators, SHA256, staging,
fsync and replace. Only App Server, bridge/status boundaries and account lookup
are synthetic; every file is under tmp_path and no socket is contacted.

The race observer pauses A before its real exclusive staging open. On the old
implementation B completes before releasing A. With a stable flock around the
final check/publication, B's real nonblocking contention releases A, followed
by the ordinary blocking acquisition. No lock success or SQL/RPC result is
fabricated by this observer. Wait limits detect a broken harness or deadlock;
they never decide which configuration is accepted. This covers concurrent
separate-worker selection, not same-worker instruction refresh or live jobs.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import multiprocessing
import os
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_wake as wake

OLD = "00000000-0000-4000-8000-000000000001"
WORKER_A = "00000000-0000-4000-8000-000000000002"
WORKER_B = "00000000-0000-4000-8000-000000000003"
REVISION = "a" * 40
WAIT = 20


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _configured_bytes(worker, bundle):
    return (
        json.dumps(
            {
                "thread_id": worker,
                "project_dir": wake.PROJECT_DIR,
                "app_socket": wake.APP_SOCKET,
                "instruction_root": str(bundle),
                "instruction_revision": REVISION,
            }
        )
        + "\n"
    ).encode()


@pytest.fixture
def layout(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("real production config and bundle validators require root ownership")
    releases = tmp_path / "releases"
    bundle = releases / "snapshot"
    for relative in (
        "AGENTS.md",
        "docs/agent/modules/B2.md",
        "docs/agent/modules/B4.md",
        ".agents/skills/manage-owner-telegram/SKILL.md",
        ".agents/skills/manage-autostop-client/SKILL.md",
    ):
        target = bundle / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("synthetic instructions\n")
        target.chmod(0o644)
    bundle.chmod(0o755)
    (bundle / "REVISION").write_text(REVISION + "\n")
    (bundle / "REVISION").chmod(0o644)
    config = tmp_path / "wake.json"
    config.write_bytes(_configured_bytes(OLD, bundle))
    config.chmod(0o600)
    return config, bundle


def _attempt(bundle, expected_thread, expected_hash, selected):
    try:
        result = asyncio.run(
            wake.select_worker(
                expected_thread_id=expected_thread,
                expected_config_sha256=expected_hash,
                instruction_root=str(bundle),
                instruction_revision=REVISION,
                worker_thread_id=selected,
            )
        )
    except (wake.WakeError, OSError) as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
    return {"ok": True, "result": result}


async def _paused_status(calls, arguments):
    calls.append(("status", arguments))
    assert arguments == {"operation": "status"}
    return {"ok": True, "enabled": False, "active": False, "outcome_unknown": False}


def _child(connection, config_text, bundle_text, expected_thread, expected_hash, selected, mode, ready, release):
    """One disposable process, with no real service boundary available."""
    config = Path(config_text)
    bundle = Path(bundle_text)
    staging = config.with_name(config.name + ".worker-selection")
    calls = []
    original_load = wake.WakeConfig.load
    real_open = os.open
    real_replace = os.replace
    real_flock = fcntl.flock
    replace_fault_used = False
    lock_contention_observed = False

    class FakeApp:
        def __init__(self, current):
            self.config = current
            self.resumed_model = "synthetic-model"

        async def connect(self):
            calls.append(("connect", self.config.thread_id))

        async def resume(self, *, allow_active=False):
            calls.append(("resume", self.config.thread_id, allow_active))
            assert allow_active is (self.config.thread_id == expected_thread)
            return "idle"

        async def request(self, method, params):
            calls.append((method, params))
            assert method == "thread/name/set"
            assert params == {"threadId": selected, "name": wake.WORKER_TASK_NAME}
            return {}

        async def close(self):
            calls.append(("close", self.config.thread_id))

    class EmptyJobs:
        def __init__(self, uid):
            assert uid == 123

        async def request(self, operation, **arguments):
            calls.append(("bridge", operation, arguments))
            assert operation == "job_active"
            assert arguments == {"limit": 1, "after_job_id": None}
            return {"ok": True, "jobs": [], "next_job_id": None}

    def observed_open(path, flags, *args, **kwargs):
        if mode == "race_a" and Path(path) == staging and flags & os.O_EXCL:
            ready.set()
            assert release.wait(WAIT), "A never received release from B"
        return real_open(path, flags, *args, **kwargs)

    def observed_flock(fd, operation):
        nonlocal lock_contention_observed
        if mode == "race_b" and operation & fcntl.LOCK_EX:
            try:
                result = real_flock(fd, operation | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_contention_observed = True
                release.set()
                if operation & fcntl.LOCK_NB:
                    raise
                return real_flock(fd, operation)
            return result
        return real_flock(fd, operation)

    def fail_first_replace(source, destination, *args, **kwargs):
        nonlocal replace_fault_used
        if Path(source) == staging and Path(destination) == config and not replace_fault_used:
            replace_fault_used = True
            raise OSError("synthetic prepublication replace failure")
        return real_replace(source, destination, *args, **kwargs)

    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(wake, "CONFIG_PATH", config)
            patch.setattr(wake, "INSTRUCTION_RELEASE_ROOTS", (bundle.parent,))
            # The production default argument was bound at definition time.
            # Route it explicitly while retaining every real load guard.
            patch.setattr(wake.WakeConfig, "load", classmethod(lambda cls, path=config: original_load(path)))
            patch.setattr(wake, "AppServer", FakeApp)
            patch.setattr(wake, "BridgeJobs", EmptyJobs)
            patch.setattr(wake, "local_request", partial(_paused_status, calls))
            patch.setattr(wake.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=123))
            patch.setattr(os, "open", observed_open)
            patch.setattr(fcntl, "flock", observed_flock)
            if mode == "fail_then_retry":
                patch.setattr(os, "replace", fail_first_replace)
            first = _attempt(bundle, expected_thread, expected_hash, selected)
            reply = {"attempts": [first]}
            if mode == "fail_then_retry":
                reply["after_error_bytes"] = config.read_bytes()
                reply["after_error_staging_exists"] = staging.exists()
                # Same process: a leaked open flock is not hidden by process exit.
                reply["attempts"].append(_attempt(bundle, expected_thread, expected_hash, selected))
            reply.update(calls=calls, lock_contention_observed=lock_contention_observed)
    # Serialize child harness failures to the parent instead of reporting silent EOF.
    except Exception as exc:  # noqa: BLE001
        reply = {"harness_error": f"{type(exc).__name__}: {exc}"}
    finally:
        if mode == "race_b":
            # Baseline B has fully completed select_worker before A continues.
            release.set()
    try:
        connection.send(reply)
    finally:
        connection.close()


def _start(context, layout, expected_thread, expected_hash, selected, *, mode="normal", ready=None, release=None):
    config, bundle = layout
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_child,
        args=(sender, str(config), str(bundle), expected_thread, expected_hash, selected, mode, ready, release),
    )
    process.start()
    sender.close()
    return process, receiver


def _receive(handle):
    process, receiver = handle
    assert receiver.poll(WAIT), "isolated selector did not finish (possible lock leak/deadlock)"
    result = receiver.recv()
    process.join(WAIT)
    assert not process.is_alive(), "isolated selector failed to exit"
    assert process.exitcode == 0
    assert "harness_error" not in result, result
    return result


def _dispose(handles):
    for process, receiver in handles:
        if process.is_alive():
            process.terminate()
        process.join(WAIT)
        if process.is_alive():
            process.kill()
            process.join(WAIT)
        receiver.close()
        process.close()


def _single(layout, expected_thread, expected_hash, selected, *, mode="normal"):
    context = multiprocessing.get_context("fork")
    handle = _start(context, layout, expected_thread, expected_hash, selected, mode=mode)
    try:
        return _receive(handle)
    finally:
        _dispose([handle])


def _success(attempt):
    assert attempt == {
        "ok": True,
        "result": {"ok": True, "selected": True, "separate_worker": True, "instruction_revision": REVISION},
    }


def _stale(attempt):
    assert attempt == {"ok": False, "error_type": "WakeError", "error": "wake_config_changed"}


def _published(layout, worker):
    config, bundle = layout
    assert config.read_bytes() == _configured_bytes(worker, bundle)
    assert config.stat().st_mode & 0o777 == 0o600
    assert not config.with_name(config.name + ".worker-selection").exists()


def test_concurrent_selectors_with_same_expected_config_cannot_both_publish(layout):
    config, _bundle = layout
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    digest = _digest(config)
    handles = []
    try:
        handles.append(_start(context, layout, OLD, digest, WORKER_A, mode="race_a", ready=ready, release=release))
        assert ready.wait(WAIT), "A never reached the real staging boundary"
        handles.append(_start(context, layout, OLD, digest, WORKER_B, mode="race_b", ready=ready, release=release))
        second = _receive(handles[1])
        first = _receive(handles[0])
        attempts = [first["attempts"][0], second["attempts"][0]]
        assert sum(attempt["ok"] for attempt in attempts) == 1, attempts
        winner = attempts.index(next(attempt for attempt in attempts if attempt["ok"]))
        _success(attempts[winner])
        _stale(attempts[1 - winner])
        _published(layout, (WORKER_A, WORKER_B)[winner])
    finally:
        release.set()
        _dispose(handles)


def test_sequential_selection_accepts_a_fresh_thread_and_digest(layout):
    config, _bundle = layout
    first = _single(layout, OLD, _digest(config), WORKER_A)
    _success(first["attempts"][0])
    _published(layout, WORKER_A)
    second = _single(layout, WORKER_A, _digest(config), WORKER_B)
    _success(second["attempts"][0])
    _published(layout, WORKER_B)


def test_stale_selection_rejects_before_service_calls_and_preserves_winner(layout):
    config, _bundle = layout
    old_digest = _digest(config)
    _success(_single(layout, OLD, old_digest, WORKER_A)["attempts"][0])
    result = _single(layout, OLD, old_digest, WORKER_B)
    _stale(result["attempts"][0])
    assert result["calls"] == []
    _published(layout, WORKER_A)


def test_prepublication_error_cleans_staging_and_releases_lock_for_same_process_retry(layout):
    config, _bundle = layout
    original = config.read_bytes()
    result = _single(layout, OLD, _digest(config), WORKER_A, mode="fail_then_retry")
    assert result["attempts"][0] == {
        "ok": False,
        "error_type": "OSError",
        "error": "synthetic prepublication replace failure",
    }
    assert result["after_error_bytes"] == original
    assert result["after_error_staging_exists"] is False
    _success(result["attempts"][1])
    assert sum(call[0] == "close" for call in result["calls"]) == 2
    _published(layout, WORKER_A)
