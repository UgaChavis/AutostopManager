"""Shared synthetic app-server and lifecycle helpers for wake tests."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from websockets.asyncio.server import unix_serve

from autostop_manager import telegram_wake as wake

THREAD = "00000000-0000-4000-8000-000000000001"


def ready_app():
    app = wake.AppServer(wake.WakeConfig(THREAD))
    app.connected = True
    app.recovery_state = "ready"
    return app


class MockServer:
    def __init__(self, *, complete=True, status="completed", disconnect=False):
        self.calls = []
        self.complete = complete
        self.status = status
        self.disconnect = disconnect
        self.started = asyncio.Event()
        self.release_start = asyncio.Event()
        self.release_start.set()
        self.counter = 0
        self.loaded = False
        self.unload_after_turn = False
        self.reject_method = None
        self.thread_status = "idle"
        self.sockets = []
        self.loaded_by_socket = {}
        self.turn_statuses = {}
        self.disconnect_after_start_response = False
        self.metadata_pages = None
        self.metadata_response = None
        self.metadata_calls = 0
        self.disconnect_metadata_once = False
        self.release_initialize = asyncio.Event()
        self.release_initialize.set()
        self.release_resume = asyncio.Event()
        self.release_resume.set()
        self.initializing = asyncio.Event()
        self.resuming = asyncio.Event()
        self.emit_started = True
        self.resume_calls = 0
        self.disconnect_resume_on_call = None
        self.metadata_requested = asyncio.Event()
        self.release_metadata = asyncio.Event()
        self.release_metadata.set()

    async def handle(self, socket):
        self.sockets.append(socket)
        self.loaded_by_socket[socket] = False
        async for raw in socket:
            msg = json.loads(raw)
            self.calls.append(msg)
            method = msg.get("method")
            if "id" not in msg:
                continue
            if method == self.reject_method or (method == "turn/start" and not self.loaded_by_socket[socket]):
                await socket.send(
                    json.dumps({"id": msg["id"], "error": {"code": -32600, "message": "private error details"}})
                )
                continue
            result = {}
            if method == "initialize":
                self.initializing.set()
                await self.release_initialize.wait()
            if method == "thread/resume":
                self.resume_calls += 1
                self.resuming.set()
                await self.release_resume.wait()
                if self.resume_calls == self.disconnect_resume_on_call:
                    await socket.close()
                    return
            if method in {"thread/start", "thread/resume"}:
                self.loaded = True
                self.loaded_by_socket[socket] = True
            if method in {"thread/start", "thread/resume", "thread/read"}:
                result = {
                    "thread": {
                        "id": THREAD,
                        "cwd": wake.PROJECT_DIR,
                        "ephemeral": False,
                        "status": {"type": self.thread_status},
                        "turns": [{"items": [{"type": "agentMessage", "text": "WAKE_PROBE_OK"}]}],
                    }
                }
            if method == "turn/start":
                self.counter += 1
                turn = {"id": str(self.counter), "status": "inProgress"}
                self.turn_statuses[str(self.counter)] = "inProgress"
                self.started.set()
                await self.release_start.wait()
                if self.emit_started:
                    await socket.send(
                        json.dumps({"method": "turn/started", "params": {"threadId": THREAD, "turn": turn}})
                    )
                result = {"turn": turn}
            if method == "reject":
                await socket.send(json.dumps({"id": msg["id"], "error": {"message": "private payload"}}))
                continue
            if method == "turn/start" and self.disconnect:
                await socket.close()
                return
            if method == "thread/turns/list":
                self.metadata_calls += 1
                self.metadata_requested.set()
                await self.release_metadata.wait()
                if self.disconnect_metadata_once:
                    self.disconnect_metadata_once = False
                    await socket.close()
                    return
                if self.metadata_response is not None:
                    result = self.metadata_response
                elif self.metadata_pages is not None:
                    page = int(msg["params"].get("cursor") or "0")
                    result = self.metadata_pages[page]
                else:
                    result = {
                        "data": [{"id": ident, "status": status} for ident, status in self.turn_statuses.items()],
                        "nextCursor": None,
                    }
            await socket.send(json.dumps({"id": msg["id"], "result": result}))
            if method == "turn/start" and self.disconnect_after_start_response:
                self.disconnect_after_start_response = False
                await socket.close()
                return
            if (method == "turn/start" and self.complete) or method == "turn/interrupt":
                status = "interrupted" if method == "turn/interrupt" else self.status
                params = {"threadId": THREAD, "turn": {"id": str(self.counter), "status": status}}
                if self.unload_after_turn:
                    self.loaded = False
                    self.loaded_by_socket[socket] = False
                self.turn_statuses[str(self.counter)] = status
                await socket.send(json.dumps({"method": "turn/completed", "params": params}))


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.fixture
def quick_recovery(monkeypatch):
    monkeypatch.setattr(wake, "RECONNECT_DELAYS", (0.01,))
    monkeypatch.setattr(wake, "RECOVERY_POLL_INTERVAL", 0.01)


def event(number):
    return {"operation": "event", "event_id": f"inbound-{number}"}


async def stop(dispatcher):
    try:
        await dispatcher.pause()
    except wake.WakeError:
        # Unknown side effects must remain visible; cleanup never retries them.
        pass
    finally:
        await dispatcher.app.close()


@asynccontextmanager
async def managed_wake(tmp_path, server):
    path = str(tmp_path / "app.sock")
    async with unix_serve(server.handle, path, compression=None):
        app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
        dispatcher = wake.WakeDispatcher(app, 123)
        dispatcher.start()
        try:
            await asyncio.wait_for(app.ready_event.wait(), 3)
            yield app, dispatcher
        finally:
            await stop(dispatcher)


@asynccontextmanager
async def connected_app(tmp_path, server):
    path = str(tmp_path / "app.sock")
    async with unix_serve(server.handle, path, compression=None):
        app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
        try:
            await app.connect()
            yield app
        finally:
            await app.close()
