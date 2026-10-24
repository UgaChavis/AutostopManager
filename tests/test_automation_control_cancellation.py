from __future__ import annotations

import asyncio
import json
import os
import struct
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from autostop_manager.automation_control import AUTOMATION_CONTROL_PROTOCOL, AutomationControlServer


class ConnectionWriter:
    def __init__(self):
        self.output = b""
        self.close_calls = 0
        self.wait_closed_calls = 0

    def get_extra_info(self, name):
        assert name == "socket"
        return SimpleNamespace(getsockopt=lambda *_args: struct.pack("3i", 123, os.getuid(), os.getgid()))

    def write(self, data):
        self.output += data

    async def drain(self):
        pass

    def close(self):
        self.close_calls += 1

    async def wait_closed(self):
        self.wait_closed_calls += 1


def request_frame():
    return (
        json.dumps(
            {
                "protocol": AUTOMATION_CONTROL_PROTOCOL,
                "request_id": str(uuid4()),
                "operation": "status",
                "actor": {"kind": "codex", "id": "synthetic-control-cleanup", "is_admin": True},
                "payload": {},
            }
        ).encode()
        + b"\n"
    )


@pytest.mark.parametrize("phase", ["read", "dispatch", "drain"])
def test_cancelled_connection_closes_stream_and_preserves_worker_completion(tmp_path, phase):
    async def scenario():
        entered = asyncio.Event()
        read_settled = asyncio.Event()
        release = threading.Event()
        worker_finished = threading.Event()
        calls = []
        loop = asyncio.get_running_loop()

        class Reader:
            async def readline(self):
                if phase == "read":
                    entered.set()
                    try:
                        await asyncio.Future()
                    finally:
                        read_settled.set()
                return request_frame()

        class Service:
            def handle(self, request):
                calls.append(request["request_id"])
                try:
                    if phase == "dispatch":
                        loop.call_soon_threadsafe(entered.set)
                        assert release.wait(5)
                    return {"ok": True}
                finally:
                    worker_finished.set()

        class Writer(ConnectionWriter):
            async def drain(self):
                if phase == "drain":
                    entered.set()
                    await asyncio.Future()

        writer = Writer()
        server = AutomationControlServer(
            service=Service(),
            socket_path=tmp_path / "unused.sock",
            allowed_uids=frozenset({os.getuid()}),
            socket_gid=os.getgid(),
        )
        task = asyncio.create_task(server.serve_connection(Reader(), writer))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert writer.close_calls == 1
            assert writer.wait_closed_calls == 1
            if phase == "read":
                assert read_settled.is_set()
                assert calls == []
            else:
                assert len(calls) == 1
            if phase == "dispatch":
                # Cancelling to_thread only abandons the await, not the real operation.
                assert not worker_finished.is_set()
                release.set()
                assert await asyncio.to_thread(worker_finished.wait, 5)
                assert len(calls) == 1
            assert bool(writer.output) is (phase == "drain")
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_response_encoding_failure_still_closes_connection(tmp_path):
    class Reader:
        async def readline(self):
            return request_frame()

    service = SimpleNamespace(handle=lambda _request: {"unserializable": object()})
    server = AutomationControlServer(
        service=service,
        socket_path=tmp_path / "unused.sock",
        allowed_uids=frozenset({os.getuid()}),
        socket_gid=os.getgid(),
    )
    writer = ConnectionWriter()

    with pytest.raises(TypeError):
        asyncio.run(server.serve_connection(Reader(), writer))

    assert writer.close_calls == 1
    assert writer.wait_closed_calls == 1
    assert writer.output == b""
