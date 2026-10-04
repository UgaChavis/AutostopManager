from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from autostop_manager.vehicle_identity_request import run_identity_request


def test_identity_request_without_http_context_preserves_result():
    async def operation():
        return {"ok": True}

    assert asyncio.run(run_identity_request(operation())) == {"ok": True}
    context = SimpleNamespace(request_context=SimpleNamespace(request=None))
    assert asyncio.run(run_identity_request(operation(), context)) == {"ok": True}


def test_identity_request_without_active_mcp_context_preserves_result():
    class NoContext:
        @property
        def request_context(self):
            raise ValueError("no_request")

    async def operation():
        return 3

    assert asyncio.run(run_identity_request(operation(), NoContext())) == 3


def test_disconnect_cancels_owned_work_before_another_attempt():
    async def run():
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        disconnected = asyncio.Event()
        attempts = []

        class Request:
            async def is_disconnected(self):
                return disconnected.is_set()

        async def operation():
            attempts.append(1)
            entered.set()
            try:
                await asyncio.Event().wait()
                attempts.append(2)
            finally:
                cancelled.set()

        ctx = SimpleNamespace(request_context=SimpleNamespace(request=Request()))
        task = asyncio.create_task(run_identity_request(operation(), ctx))
        await entered.wait()
        disconnected.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert cancelled.is_set()
        assert attempts == [1]

    asyncio.run(run())


@pytest.mark.parametrize("fail", [False, True])
def test_request_completion_and_failure_close_disconnect_watcher(fail):
    async def run():
        watcher_stopped = asyncio.Event()
        started = asyncio.Event()

        class Request:
            async def is_disconnected(self):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    watcher_stopped.set()

        async def operation():
            await started.wait()
            if fail:
                raise RuntimeError("synthetic_failure")
            return 4

        ctx = SimpleNamespace(request_context=SimpleNamespace(request=Request()))
        if fail:
            with pytest.raises(RuntimeError, match="synthetic_failure"):
                await run_identity_request(operation(), ctx)
        else:
            assert await run_identity_request(operation(), ctx) == 4
        assert watcher_stopped.is_set()

    asyncio.run(run())


def test_external_cancellation_closes_provider_work_and_watcher():
    async def run():
        entered = asyncio.Event()
        closed = asyncio.Event()

        class Request:
            async def is_disconnected(self):
                return False

        async def operation():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

        ctx = SimpleNamespace(request_context=SimpleNamespace(request=Request()))
        task = asyncio.create_task(run_identity_request(operation(), ctx))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()

    asyncio.run(run())
