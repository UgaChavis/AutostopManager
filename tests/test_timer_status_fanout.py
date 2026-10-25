"""Default timer status reads overlap without changing rows or repeating work."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor as RealExecutor
import socket
import sqlite3
import subprocess
import threading

import pytest

from autostop_manager import automation_timers as timers


@pytest.fixture(autouse=True)
def no_external_operations(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("timer status test attempted external activity")

    for owner, name in (
        (subprocess, "Popen"),
        (socket.socket, "connect"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (sqlite3, "connect"),
    ):
        monkeypatch.setattr(owner, name, denied)


def controller(tmp_path, count=5, *, runner=None):
    policies = {
        f"demo{index}": timers.SystemTimerPolicy(f"demo{index}", f"demo-{index}.timer", f"DEMO {index}")
        for index in reversed(range(count))
    }
    options = {"policies": policies, "store": object(), "dropin_root": tmp_path / "owned-dropins"}
    if runner is not None:
        options["runner"] = runner
    return timers.SystemTimerController(**options)


def completed(command):
    assert command[:2] == ["systemctl", "show"]
    return subprocess.CompletedProcess(
        command,
        0,
        stdout=(
            "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\nResult=success\n"
            "NextElapseUSecRealtime=Sun 2026-10-25 12:00:00 UTC\n"
            "TimersMonotonic={ OnUnitActiveUSec=15min ; next_elapse=15min }\n"
        ),
        stderr="",
    )


def capture_pool(monkeypatch, executor_class=RealExecutor):
    created = []

    def factory(*args, **kwargs):
        pool = executor_class(*args, **kwargs)
        created.append(pool)
        return pool

    # The old implementation has no pool; this control stays valid on it.
    monkeypatch.setattr(timers, "ThreadPoolExecutor", factory, raising=False)
    return created


def assert_joined(pools):
    assert all(not thread.is_alive() for pool in pools for thread in pool._threads)


def run_until_released(controller, entered, release, *, excess=None):
    result = {}

    def call():
        try:
            result["rows"] = controller.list_status()
        except BaseException as error:  # noqa: BLE001 - return the owned thread's failure to the test caller
            result["error"] = error

    caller = threading.Thread(target=call, name="synthetic-status-caller")
    caller.start()
    try:
        all_entered = entered.wait(1)
        too_many = excess.wait(0.1) if all_entered and excess is not None else False
    finally:
        release.set()
        caller.join(3)
    assert not caller.is_alive(), "owned status caller did not settle"
    if "error" in result:
        raise result["error"]
    return result["rows"], all_entered, too_many


@pytest.mark.parametrize("count", [5, 9])
def test_default_timer_reads_overlap_in_a_bounded_wave_and_keep_sorted_rows(tmp_path, monkeypatch, count):
    lock, entered, release, excess = threading.Lock(), threading.Event(), threading.Event(), threading.Event()
    calls = Counter()
    active = maximum = 0

    def slow_timeout(command, **kwargs):
        nonlocal active, maximum
        assert kwargs["timeout"] == 5
        with lock:
            calls[command[2]] += 1
            active += 1
            maximum = max(maximum, active)
            if active == 5:
                entered.set()
            if active > 5:
                excess.set()
        try:
            assert release.wait(3)
            raise subprocess.TimeoutExpired(command, timeout=kwargs["timeout"])
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(subprocess, "run", slow_timeout)
    pools = capture_pool(monkeypatch)
    rows, overlapped, exceeded = run_until_released(controller(tmp_path, count), entered, release, excess=excess)
    assert overlapped, "five independent default inspections waited serially"
    assert not exceeded and maximum == 5
    assert [row["timer_id"] for row in rows] == sorted(f"demo{i}" for i in range(count))
    assert calls == Counter({f"demo-{i}.timer": 1 for i in range(count)})
    assert all(not row["inspection_ok"] and row["state"]["actual_state"] == "unknown" for row in rows)
    assert active == 0
    assert_joined(pools)


def test_default_fanout_keeps_healthy_and_failed_timer_evidence_separate(tmp_path, monkeypatch):
    def mixed(command, **_kwargs):
        if command[2] == "demo-1.timer":
            raise subprocess.TimeoutExpired(command, timeout=5)
        if command[2] == "demo-3.timer":
            raise OSError("synthetic systemd unavailable")
        return completed(command)

    monkeypatch.setattr(subprocess, "run", mixed)
    pools = capture_pool(monkeypatch)
    rows = controller(tmp_path).list_status()
    assert [row["timer_id"] for row in rows] == [f"demo{i}" for i in range(5)]
    assert [row["inspection_ok"] for row in rows] == [True, False, True, False, True]
    assert [row["state"]["period_minutes"] for row in rows] == [15, None, 15, None, 15]
    assert_joined(pools)


def test_custom_runner_keeps_order_and_calling_thread(tmp_path):
    calls, caller = [], threading.get_ident()

    def stateful_runner(command):
        calls.append((command[2], threading.get_ident()))
        return completed(command)

    rows = controller(tmp_path, runner=stateful_runner).list_status()
    assert [row["timer_id"] for row in rows] == [f"demo{i}" for i in range(5)]
    assert calls == [(f"demo-{i}.timer", caller) for i in range(5)]


@pytest.mark.parametrize("count", [0, 1])
def test_empty_or_single_default_inspection_keeps_direct_behavior(tmp_path, monkeypatch, count):
    calls, caller = [], threading.get_ident()

    def direct(command, **_kwargs):
        calls.append(threading.get_ident())
        return completed(command)

    monkeypatch.setattr(subprocess, "run", direct)
    rows = controller(tmp_path, count).list_status()
    assert len(rows) == count
    assert calls == [caller] * count


@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
def test_pool_creation_failure_preserves_complete_serial_status(tmp_path, monkeypatch, error_type):
    calls = Counter()

    def unavailable(*_args, **_kwargs):
        raise error_type("synthetic pool unavailable")

    def backend(command, **_kwargs):
        calls[command[2]] += 1
        return completed(command)

    monkeypatch.setattr(timers, "ThreadPoolExecutor", unavailable, raising=False)
    monkeypatch.setattr(subprocess, "run", backend)
    rows = controller(tmp_path).list_status()
    assert all(row["inspection_ok"] for row in rows)
    assert calls == Counter({f"demo-{i}.timer": 1 for i in range(5)})


def test_submit_failure_after_queueing_before_worker_start_falls_back_once(tmp_path, monkeypatch):
    class CannotStart(RealExecutor):
        def _adjust_thread_count(self):
            raise RuntimeError("synthetic thread capacity unavailable")

    calls = Counter()

    def backend(command, **_kwargs):
        calls[command[2]] += 1
        return completed(command)

    pools = capture_pool(monkeypatch, CannotStart)
    monkeypatch.setattr(subprocess, "run", backend)
    rows = controller(tmp_path).list_status()
    assert len(rows) == 5 and all(row["inspection_ok"] for row in rows)
    assert calls == Counter({f"demo-{i}.timer": 1 for i in range(5)})
    assert_joined(pools)


def test_lost_future_after_started_submit_does_not_repeat_known_inspection(tmp_path, monkeypatch):
    class LostFuture(RealExecutor):
        def submit(self, *args, **kwargs):
            super().submit(*args, **kwargs)
            raise RuntimeError("synthetic post-queue submission failure")

    calls, lock = Counter(), threading.Lock()

    def backend(command, **_kwargs):
        with lock:
            calls[command[2]] += 1
        return completed(command)

    pools = capture_pool(monkeypatch, LostFuture)
    monkeypatch.setattr(subprocess, "run", backend)
    rows = controller(tmp_path).list_status()
    assert len(rows) == 5 and all(row["inspection_ok"] for row in rows)
    assert calls == Counter({f"demo-{i}.timer": 1 for i in range(5)})
    assert_joined(pools)


@pytest.mark.parametrize("error_type", [ValueError, SystemExit])
def test_unexpected_inspection_error_is_observed_after_workers_settle(tmp_path, monkeypatch, error_type):
    def backend(command, **_kwargs):
        if command[2] == "demo-1.timer":
            raise error_type("synthetic unexpected inspection")
        return completed(command)

    pools = capture_pool(monkeypatch)
    monkeypatch.setattr(subprocess, "run", backend)
    with pytest.raises(error_type, match="synthetic unexpected inspection"):
        controller(tmp_path).list_status()
    assert_joined(pools)


def test_unexpected_submit_error_propagates_after_owned_workers_settle(tmp_path, monkeypatch):
    class UnexpectedSubmit(RealExecutor):
        def submit(self, *args, **kwargs):
            super().submit(*args, **kwargs)
            raise ValueError("synthetic unexpected submit")

    pools = capture_pool(monkeypatch, UnexpectedSubmit)
    monkeypatch.setattr(subprocess, "run", lambda command, **_kwargs: completed(command))
    with pytest.raises(ValueError, match="synthetic unexpected submit"):
        controller(tmp_path).list_status()
    assert_joined(pools)
