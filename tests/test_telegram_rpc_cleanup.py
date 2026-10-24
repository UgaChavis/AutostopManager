"""Bound server RPC cleanup without cancelling issued receipt work."""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import suppress
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_effects import EffectExecutor


class MemoryWriter:
    def __init__(self, *, blocked=False, error=None):
        self.output = bytearray()
        self.closed = False
        self.close_calls = 0
        self.wait_calls = 0
        self.aborts = 0
        self.blocked = blocked
        self.error = error
        self.waiting = asyncio.Event()
        self.close_waiter = asyncio.get_running_loop().create_future()
        self.transport = SimpleNamespace(abort=self.abort)

    def write(self, value):
        self.output.extend(value)

    async def drain(self):
        pass

    def close(self):
        self.closed = True
        self.close_calls += 1

    async def wait_closed(self):
        self.wait_calls += 1
        self.waiting.set()
        if self.error is not None:
            raise self.error
        if self.blocked:
            await self.close_waiter

    def abort(self):
        self.aborts += 1


def request_reader(payload):
    reader = asyncio.StreamReader()
    reader.feed_data(json.dumps(payload).encode() + b"\n")
    return reader


@pytest.fixture(autouse=True)
def local_only(monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")

    def forbidden(*args, **kwargs):
        pytest.fail("external Telegram/config access is forbidden")

    monkeypatch.setattr(bridge, "_load_telethon", forbidden)
    monkeypatch.setattr(bridge, "_config_for_account", forbidden)
    monkeypatch.setattr(bridge, "_require_root_client", lambda writer: None)


@pytest.mark.parametrize("failure", ["timeout", "io_error"])
def test_close_is_bounded_idempotent_and_has_no_detached_waiter(monkeypatch, failure):
    async def scenario():
        monkeypatch.setattr(bridge, "LOCAL_RPC_CLOSE_TIMEOUT_SECONDS", 0.01)
        writer = MemoryWriter(
            blocked=failure == "timeout", error=OSError("synthetic close failure") if failure == "io_error" else None
        )
        before = asyncio.all_tasks()
        await asyncio.wait_for(bridge._close_rpc_writer(writer), 1)
        await bridge._close_rpc_writer(writer)
        assert writer.closed and writer.aborts == 1
        assert writer.close_calls == writer.wait_calls == 1
        assert asyncio.all_tasks() == before

    asyncio.run(scenario())


def test_normal_read_response_closes_once_without_abort(monkeypatch):
    async def scenario():
        writer = MemoryWriter()
        expected = {"ok": True, "messages": [{"text": "synthetic response"}]}

        async def operation(*args, **kwargs):
            return expected

        monkeypatch.setattr(bridge, "_handle_operation", operation)
        await bridge._serve_client(object(), object(), request_reader({"operation": "read"}), writer)
        assert json.loads(writer.output) == expected
        assert writer.closed and writer.close_calls == writer.wait_calls == 1
        assert writer.aborts == 0

    asyncio.run(scenario())


def test_subscription_return_still_uses_outer_rpc_cleanup(monkeypatch):
    async def scenario():
        writer = MemoryWriter()
        monitor = object.__new__(bridge.DurableInboundMonitor)
        calls = []

        async def subscription(reader, actual_writer, actual_monitor, *, heartbeat=False):
            assert actual_writer is writer and actual_monitor is monitor and heartbeat
            calls.append("subscription")

        monkeypatch.setattr(bridge, "_serve_job_subscription", subscription)
        await bridge._serve_client(
            object(),
            object(),
            request_reader({"operation": "job_subscribe", "heartbeat": True}),
            writer,
            inbound_monitor=monitor,
        )
        assert calls == ["subscription"] and not writer.output
        assert writer.closed and writer.close_calls == writer.wait_calls == 1 and writer.aborts == 0

    asyncio.run(scenario())


def test_real_subscription_eof_releases_waiters_before_bounded_close(monkeypatch):
    async def scenario():
        monkeypatch.setattr(bridge, "LOCAL_RPC_CLOSE_TIMEOUT_SECONDS", 0.01)
        writer = MemoryWriter(blocked=True)
        monitor = object.__new__(bridge.DurableInboundMonitor)
        monitor.subscribers = set()
        monitor.generation = 17
        reader = request_reader({"operation": "job_subscribe", "heartbeat": True})
        reader.feed_eof()
        before = asyncio.all_tasks()
        await asyncio.wait_for(bridge._serve_client(object(), object(), reader, writer, inbound_monitor=monitor), 1)
        assert json.loads(writer.output) == {"ok": True, "generation": 17, "heartbeat": False}
        assert not monitor.subscribers
        assert writer.closed and writer.aborts == 1
        assert writer.close_calls == writer.wait_calls == 1
        assert asyncio.all_tasks() == before

    asyncio.run(scenario())


@pytest.mark.parametrize("read_cleanup_error", [False, True])
def test_real_subscription_cancellation_releases_owned_waiters_and_propagates(monkeypatch, read_cleanup_error):
    async def scenario():
        class Reader(asyncio.StreamReader):
            async def read(self, n=-1):
                try:
                    return await super().read(n)
                except asyncio.CancelledError:
                    if read_cleanup_error:
                        raise ConnectionResetError("synthetic peer reset during cleanup") from None
                    raise

        monkeypatch.setattr(bridge, "LOCAL_RPC_CLOSE_TIMEOUT_SECONDS", 0.01)
        writer = MemoryWriter(blocked=True)
        frame_sent = asyncio.Event()
        original_drain = writer.drain

        async def drain():
            await original_drain()
            frame_sent.set()

        writer.drain = drain
        monitor = object.__new__(bridge.DurableInboundMonitor)
        monitor.subscribers = set()
        monitor.generation = 17
        reader = Reader()
        reader.feed_data(b'{"operation":"job_subscribe","heartbeat":true}\n')
        before = asyncio.all_tasks()
        task = asyncio.create_task(bridge._serve_client(object(), object(), reader, writer, inbound_monitor=monitor))
        try:
            await asyncio.wait_for(frame_sent.wait(), 1)
            assert monitor.subscribers and reader._waiter is not None
            assert not writer.waiting.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert not monitor.subscribers and reader._waiter is None
            assert writer.closed and writer.aborts == 1
            assert writer.close_calls == writer.wait_calls == 1
            assert asyncio.all_tasks() == before
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["request", "cleanup"])
def test_external_cancellation_closes_writer_and_propagates(monkeypatch, phase):
    async def scenario():
        writer = MemoryWriter(blocked=phase == "cleanup")
        reader = asyncio.StreamReader() if phase == "request" else request_reader({"operation": "status"})

        async def operation(*args, **kwargs):
            return {"ok": True}

        monkeypatch.setattr(bridge, "_handle_operation", operation)
        task = asyncio.create_task(bridge._serve_client(object(), object(), reader, writer))
        try:
            if phase == "cleanup":
                await asyncio.wait_for(writer.waiting.wait(), 1)
            else:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert writer.closed and writer.close_calls == writer.wait_calls == 1
            assert writer.aborts == (1 if phase == "cleanup" else 0)
            assert not any(other is not asyncio.current_task() and not other.done() for other in asyncio.all_tasks())
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    asyncio.run(scenario())


def test_cancelled_active_rpc_keeps_receipt_work_in_effect_lane(monkeypatch):
    async def scenario():
        executor = EffectExecutor()
        writer = MemoryWriter()
        entered, release, receipt_saved = asyncio.Event(), asyncio.Event(), asyncio.Event()
        monkeypatch.setattr(bridge, "_effect_executor", lambda: executor)

        async def operation(*args, **kwargs):
            entered.set()
            await release.wait()
            receipt_saved.set()
            return {"ok": True, "receipt_key": "synthetic-receipt"}

        monkeypatch.setattr(bridge, "_handle_operation", operation)
        task = asyncio.create_task(
            bridge._serve_client(
                object(),
                object(),
                request_reader({"operation": "send", "mode": "apply", "job_id": "synthetic-job"}),
                writer,
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert writer.closed and executor.has_job_work("synthetic-job") and not receipt_saved.is_set()
            progress = executor.progress_event
            release.set()
            await asyncio.wait_for(receipt_saved.wait(), 1)
            await asyncio.wait_for(progress.wait(), 1)
            assert not executor.has_job_work("synthetic-job") and not executor.failed.is_set()
        finally:
            release.set()
            await executor.close()
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    asyncio.run(scenario())


def test_backpressured_unix_response_releases_handler_without_peer_close(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(bridge, "LOCAL_RPC_CLOSE_TIMEOUT_SECONDS", 0.02)
        real_timeout = asyncio.timeout
        monkeypatch.setattr(
            bridge.asyncio, "timeout", lambda seconds: real_timeout(0.02 if seconds == 5.0 else seconds)
        )
        connected = asyncio.get_running_loop().create_future()
        path = tmp_path / "rpc.sock"

        async def operation(*args, **kwargs):
            return {"ok": True, "messages": [{"text": "x" * (2 * 1024 * 1024)}]}

        monkeypatch.setattr(bridge, "_handle_operation", operation)

        async def handler(reader, writer):
            writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            original_write = writer.write
            observed = {}

            def write(value):
                original_write(value)
                observed["backpressure_bytes"] = writer.transport.get_write_buffer_size()

            writer.write = write
            connected.set_result((asyncio.current_task(), writer, observed))
            await bridge._serve_client(object(), object(), reader, writer)

        server = await asyncio.start_unix_server(handler, path=str(path))
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        peer.setblocking(False)
        try:
            await asyncio.get_running_loop().sock_connect(peer, str(path))
            await asyncio.get_running_loop().sock_sendall(peer, b'{"operation":"read"}\n')
            task, writer, observed = await asyncio.wait_for(connected, 1)
            await asyncio.wait_for(task, 2)
            assert peer.fileno() >= 0 and observed["backpressure_bytes"] > 0
            assert writer.transport.is_closing() and writer.transport.get_write_buffer_size() == 0
        finally:
            peer.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())
