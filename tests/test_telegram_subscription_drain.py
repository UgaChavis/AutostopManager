"""Exercise subscription output deadlines on actual private Unix transports."""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge


@pytest.fixture(autouse=True)
def local_only(monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")

    def forbidden(*args, **kwargs):
        pytest.fail("external Telegram/config access is forbidden")

    monkeypatch.setattr(bridge, "_load_telethon", forbidden)
    monkeypatch.setattr(bridge, "_config_for_account", forbidden)
    monkeypatch.setattr(bridge, "_require_root_client", lambda writer: None)


def synthetic_monitor():
    monitor = object.__new__(bridge.DurableInboundMonitor)
    monitor.subscribers = set()
    monitor.generation = 17
    return monitor


@asynccontextmanager
async def backpressured_subscription(monitor):
    connected = asyncio.get_running_loop().create_future()
    blocked = asyncio.Event()
    with TemporaryDirectory(prefix="tg-sub-drain-", dir="/tmp") as directory:
        path = Path(directory) / "rpc.sock"

        async def handler(reader, writer):
            writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            writer.transport.set_write_buffer_limits(high=1024, low=512)
            real_drain = writer.drain

            async def drain():
                if writer.transport.get_write_buffer_size() > 1024:
                    blocked.set()
                await real_drain()

            writer.drain = drain
            connected.set_result(SimpleNamespace(task=asyncio.current_task(), reader=reader, writer=writer))
            await bridge._serve_client(object(), object(), reader, writer, inbound_monitor=monitor)

        server = await asyncio.start_unix_server(handler, path=str(path))
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        peer.setblocking(False)
        connection = None
        try:
            loop = asyncio.get_running_loop()
            await loop.sock_connect(peer, str(path))
            await loop.sock_sendall(peer, b'{"operation":"job_subscribe"}\n')
            connection = await asyncio.wait_for(connected, 1)
            for _ in range(10000):
                if blocked.is_set():
                    break
                monitor.generation += 1
                for subscriber in tuple(monitor.subscribers):
                    subscriber.set()
                await asyncio.sleep(0)
            assert blocked.is_set(), "native Unix transport did not reach backpressure"
            assert connection.writer.transport.get_write_buffer_size() > 1024
            yield SimpleNamespace(peer=peer, **vars(connection))
        finally:
            peer.close()
            server.close()
            await server.wait_closed()
            if connection is not None:
                if not connection.task.done():
                    connection.task.cancel()
                await asyncio.gather(connection.task, return_exceptions=True)


@pytest.mark.parametrize("half_close", [False, True])
def test_nonreading_subscription_releases_handler_while_peer_remains_open(monkeypatch, half_close):
    async def scenario():
        monkeypatch.setattr(bridge, "LOCAL_RPC_DRAIN_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(bridge, "LOCAL_RPC_CLOSE_TIMEOUT_SECONDS", 0.02)
        monitor = synthetic_monitor()
        before = asyncio.all_tasks()
        async with backpressured_subscription(monitor) as connection:
            if half_close:
                connection.peer.shutdown(socket.SHUT_WR)
                async with asyncio.timeout(1):
                    while not connection.reader.at_eof():
                        await asyncio.sleep(0)
            done, _pending = await asyncio.wait({connection.task}, timeout=2)
            assert done, "subscription output must expire without a full peer close"
            await connection.task
            assert connection.peer.fileno() >= 0
            assert not monitor.subscribers
            assert connection.writer.transport.is_closing()
            assert connection.writer.transport.get_write_buffer_size() == 0
        assert asyncio.all_tasks() == before

    asyncio.run(scenario())


def test_backpressured_subscription_cancellation_releases_owned_tasks(monkeypatch):
    async def scenario():
        monkeypatch.setattr(bridge, "LOCAL_RPC_DRAIN_TIMEOUT_SECONDS", 1.0)
        monkeypatch.setattr(bridge, "LOCAL_RPC_CLOSE_TIMEOUT_SECONDS", 0.02)
        monitor = synthetic_monitor()
        before = asyncio.all_tasks()
        async with backpressured_subscription(monitor) as connection:
            assert monitor.subscribers and connection.reader._waiter is not None
            connection.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(connection.task, 1)
            assert connection.peer.fileno() >= 0
            assert not monitor.subscribers and connection.reader._waiter is None
            assert connection.writer.transport.is_closing()
            assert connection.writer.transport.get_write_buffer_size() == 0
            assert asyncio.all_tasks() == before
        assert asyncio.all_tasks() == before

    asyncio.run(scenario())


def test_normal_subscription_frames_and_heartbeats_outlive_frame_budget(monkeypatch):
    async def scenario():
        monkeypatch.setattr(bridge, "LOCAL_RPC_DRAIN_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(bridge, "JOB_SUBSCRIPTION_HEARTBEAT_SECONDS", 0.04)
        monitor = synthetic_monitor()
        connected = asyncio.get_running_loop().create_future()
        before = asyncio.all_tasks()
        with TemporaryDirectory(prefix="tg-sub-frame-", dir="/tmp") as directory:
            path = Path(directory) / "rpc.sock"

            async def handler(reader, writer):
                connected.set_result(asyncio.current_task())
                await bridge._serve_client(object(), object(), reader, writer, inbound_monitor=monitor)

            server = await asyncio.start_unix_server(handler, path=str(path))
            writer = task = None
            try:
                reader, writer = await asyncio.open_unix_connection(str(path))
                writer.write(b'{"operation":"job_subscribe","heartbeat":true}\n')
                await writer.drain()
                task = await asyncio.wait_for(connected, 1)
                assert json.loads(await asyncio.wait_for(reader.readline(), 1)) == {
                    "ok": True,
                    "generation": 17,
                    "heartbeat": False,
                }
                monitor.generation = 18
                for subscriber in tuple(monitor.subscribers):
                    subscriber.set()
                assert json.loads(await asyncio.wait_for(reader.readline(), 1)) == {
                    "ok": True,
                    "generation": 18,
                    "heartbeat": False,
                }
                assert json.loads(await asyncio.wait_for(reader.readline(), 1)) == {
                    "ok": True,
                    "generation": 18,
                    "heartbeat": True,
                }
                assert monitor.generation == 18 and monitor.subscribers and not task.done()
                writer.close()
                await writer.wait_closed()
                await asyncio.wait_for(task, 1)
                assert not monitor.subscribers
            finally:
                if writer is not None:
                    writer.close()
                    with suppress(OSError):
                        await writer.wait_closed()
                server.close()
                await server.wait_closed()
                if task is not None:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        assert asyncio.all_tasks() == before

    asyncio.run(scenario())
