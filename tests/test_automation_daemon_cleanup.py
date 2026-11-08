from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from autostop_manager import automation_daemon as module


class Store:
    def __init__(self, harness):
        self.harness = harness

    def initialize(self):
        self.harness.events.append("initialize")

    def seed_defaults(self):
        self.harness.events.append("seed")

    def controller_heartbeat(self, **values):
        self.harness.events.append("started" if values.get("started") else values["state"])


class Timers:
    def __init__(self, harness):
        self.harness = harness

    def adopt_all_current(self):
        self.harness.events.append("adopt")
        if self.harness.phase == "adopt":
            raise self.harness.error


class Server:
    def __init__(self, harness):
        self.harness = harness

    async def start(self):
        self.harness.events.append("start")
        if self.harness.phase == "start":
            raise self.harness.error
        self.harness.active = True

    async def close(self):
        harness = self.harness
        harness.close_calls += 1
        harness.events.append("close_begin")
        harness.close_started.set()
        try:
            if harness.blocking_close:
                await harness.release_close.wait()
        except asyncio.CancelledError:
            harness.close_cancelled = True
            raise
        if harness.phase in {"close", "close_cancel_race"}:
            if harness.phase == "close_cancel_race":
                harness.real_get_loop().call_soon(harness.parent.cancel, "close race")
            raise harness.error
        harness.active = False
        harness.events.append("close_done")


async def inline_thread(function, *args):
    return function(*args)


class Harness:
    def __init__(self, monkeypatch, phase=None, *, blocking_child=False, blocking_close=False):
        self.phase = phase
        self.blocking_child = blocking_child
        self.blocking_close = blocking_close
        self.error = (
            OSError("synthetic wait failure") if phase == "wait" else RuntimeError("synthetic lifecycle failure")
        )
        self.events = []
        self.child_started = asyncio.Event()
        self.child_done = asyncio.Event()
        self.release_child = asyncio.Event()
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.stopping_notified = asyncio.Event()
        self.child_task = None
        self.parent = None
        self.child_cancelled = False
        self.close_cancelled = False
        self.active = False
        self.close_calls = 0
        self.signal_calls = 0
        self.real_get_loop = asyncio.get_running_loop
        self.daemon = module.AutomationDaemon(
            store=Store(self), timer_controller=Timers(self), control_server=Server(self)
        )
        monkeypatch.setattr(self.daemon, "scheduler_loop", self.scheduler)
        monkeypatch.setattr(module, "reject_obsolete_runtime_config", lambda: None)
        monkeypatch.setattr(module, "notify_systemd", self.notify)
        monkeypatch.setattr(module.asyncio, "get_running_loop", self.get_loop)
        monkeypatch.setattr(module.asyncio, "to_thread", inline_thread)
        if phase == "wait":
            monkeypatch.setattr(self.daemon.stopping, "wait", self.bad_wait)

    async def scheduler(self):
        self.child_task = asyncio.current_task()
        self.events.append("child_begin")
        self.child_started.set()
        try:
            if self.blocking_child or (self.phase == "ready" and not self.daemon.stopping.is_set()):
                await self.release_child.wait()
            self.daemon.stopping.set()
            if self.phase == "child":
                raise self.error
            if self.phase == "child_cancel":
                raise asyncio.CancelledError("synthetic child cancellation")
        except asyncio.CancelledError:
            self.child_cancelled = True
            raise
        finally:
            self.events.append("child_done")
            self.child_done.set()

    def notify(self, message):
        self.events.append(message)
        if message == "STOPPING=1":
            self.stopping_notified.set()
            if self.phase == "stop_notify":
                raise self.error
        if message == "READY=1" and self.phase == "ready":
            raise self.error

    def add_signal_handler(self, *_args):
        self.signal_calls += 1
        if self.phase == f"signal{self.signal_calls}":
            raise self.error
        if self.phase == "unsupported_signals":
            raise NotImplementedError

    def get_loop(self):
        return SimpleNamespace(add_signal_handler=self.add_signal_handler, create_task=self.real_get_loop().create_task)

    async def bad_wait(self):
        raise self.error

    async def settle(self, running=None):
        self.daemon.stopping.set()
        self.release_child.set()
        self.release_close.set()
        await asyncio.sleep(0)
        if running is not None:
            await asyncio.gather(running, return_exceptions=True)
        if self.child_task is not None:
            await asyncio.gather(self.child_task, return_exceptions=True)


def assert_drained_and_closed(harness):
    assert harness.close_calls == 1
    assert harness.active is False
    assert harness.child_cancelled is False
    assert harness.close_cancelled is False
    if harness.child_task is not None:
        assert harness.child_task.done()
        assert harness.events.index("child_done") < harness.events.index("close_begin")
    if "stopped" in harness.events and harness.child_task is not None:
        assert harness.events.index("child_done") < harness.events.index("stopped")


@pytest.mark.parametrize("phase", ["signal1", "signal2", "ready", "child", "stop_notify", "wait"])
def test_owned_server_closes_once_on_setup_or_shutdown_failure(monkeypatch, phase):
    async def scenario():
        harness = Harness(monkeypatch, phase)
        try:
            with pytest.raises(type(harness.error)) as caught:
                await harness.daemon.run()
            assert caught.value is harness.error
            assert_drained_and_closed(harness)
            if phase == "ready":
                assert harness.child_done.is_set()
            if phase == "child":
                assert "stopped" not in harness.events
        finally:
            await harness.settle()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", [None, "unsupported_signals"])
def test_normal_lifecycle_and_unsupported_signals_keep_order(monkeypatch, phase):
    async def scenario():
        harness = Harness(monkeypatch, phase)
        await harness.daemon.run()
        assert harness.events[:5] == ["initialize", "seed", "adopt", "started", "start"]
        assert harness.events.index("READY=1") < harness.events.index("STOPPING=1")
        assert harness.events.index("stopped") < harness.events.index("close_begin")
        assert_drained_and_closed(harness)

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["start", "adopt"])
def test_failed_acquisition_never_closes_an_unowned_server(monkeypatch, phase):
    async def scenario():
        harness = Harness(monkeypatch, phase)
        with pytest.raises(RuntimeError) as caught:
            await harness.daemon.run()
        assert caught.value is harness.error
        assert harness.close_calls == 0
        assert harness.child_task is None

    asyncio.run(scenario())


def test_close_failure_keeps_original_exception_and_is_not_retried(monkeypatch):
    async def scenario():
        harness = Harness(monkeypatch, "close")
        with pytest.raises(RuntimeError) as caught:
            await harness.daemon.run()
        assert caught.value is harness.error
        assert harness.close_calls == 1
        assert harness.child_done.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize("repeat_during_drain", [False, True])
def test_parent_cancellation_waits_for_owned_child_and_cleanup(monkeypatch, repeat_during_drain):
    async def scenario():
        harness = Harness(monkeypatch, blocking_child=True)
        running = asyncio.create_task(harness.daemon.run())
        try:
            await asyncio.wait_for(harness.child_started.wait(), 2)
            running.cancel("first")
            await asyncio.wait_for(harness.stopping_notified.wait(), 2)
            if repeat_during_drain:
                running.cancel("again")
            for _ in range(3):
                await asyncio.sleep(0)
            assert not running.done()
            assert harness.close_calls == 0
            assert not harness.child_cancelled
            harness.release_child.set()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert_drained_and_closed(harness)
        finally:
            await harness.settle(running)

    asyncio.run(scenario())


def test_parent_cancellation_during_async_close_does_not_abandon_close(monkeypatch):
    async def scenario():
        harness = Harness(monkeypatch, blocking_close=True)
        running = asyncio.create_task(harness.daemon.run())
        try:
            await asyncio.wait_for(harness.close_started.wait(), 2)
            running.cancel("during close")
            for _ in range(3):
                await asyncio.sleep(0)
            assert not running.done()
            assert not harness.close_cancelled
            harness.release_close.set()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert_drained_and_closed(harness)
        finally:
            await harness.settle(running)

    asyncio.run(scenario())


def test_stopping_notification_failure_still_drains_an_active_child(monkeypatch):
    async def scenario():
        harness = Harness(monkeypatch, "stop_notify", blocking_child=True)
        running = asyncio.create_task(harness.daemon.run())
        try:
            await asyncio.wait_for(harness.child_started.wait(), 2)
            harness.daemon.stopping.set()
            await asyncio.wait_for(harness.stopping_notified.wait(), 2)
            for _ in range(3):
                await asyncio.sleep(0)
            assert not running.done()
            assert harness.close_calls == 0
            harness.release_child.set()
            with pytest.raises(RuntimeError) as caught:
                await running
            assert caught.value is harness.error
            assert_drained_and_closed(harness)
        finally:
            await harness.settle(running)

    asyncio.run(scenario())


def test_settled_cancelled_child_closes_without_reporting_success_or_looping(monkeypatch):
    async def scenario():
        harness = Harness(monkeypatch, "child_cancel")
        with pytest.raises(asyncio.CancelledError):
            await harness.daemon.run()
        assert harness.child_task.done() and harness.child_task.cancelled()
        assert harness.close_calls == 1
        assert harness.active is False
        assert "stopped" not in harness.events

    asyncio.run(scenario())


def test_cleanup_failure_is_observed_when_parent_cancel_races_completion(monkeypatch):
    async def scenario():
        harness = Harness(monkeypatch, "close_cancel_race")
        running = asyncio.create_task(harness.daemon.run())
        harness.parent = running
        try:
            with pytest.raises(RuntimeError) as caught:
                await running
            assert caught.value is harness.error
            assert harness.close_calls == 1
            assert harness.child_done.is_set()
        finally:
            await harness.settle(running)

    asyncio.run(scenario())
