"""Real WebSocket regression for a known turn's delayed recovery consumer."""

import asyncio
import json
from contextlib import suppress

from test_telegram_wake import THREAD
from websockets.asyncio.server import unix_serve

from autostop_manager import telegram_wake as wake


def test_known_turn_waiter_does_not_query_real_socket_during_initialize(tmp_path):
    async def scenario():
        initializing = asyncio.Event()
        release_initialize = asyncio.Event()
        premature_metadata = asyncio.Event()
        response_tasks = []
        calls = []

        async def handle(socket):
            initialized = False

            async def finish_initialize(ident):
                await release_initialize.wait()
                await socket.send(json.dumps({"id": ident, "result": {}}))

            async for raw in socket:
                message = json.loads(raw)
                method = message.get("method")
                calls.append(method)
                if method == "initialize":
                    initializing.set()
                    response_tasks.append(asyncio.create_task(finish_initialize(message["id"])))
                    continue
                if method == "initialized":
                    initialized = True
                    continue
                if not initialized:
                    premature_metadata.set()
                    await socket.send(
                        json.dumps({"id": message["id"], "error": {"code": -32600, "message": "Not initialized"}})
                    )
                    continue
                if method == "thread/resume":
                    result = {"thread": {"id": THREAD, "cwd": wake.PROJECT_DIR, "status": {"type": "active"}}}
                elif method == "thread/turns/list":
                    result = {"data": [{"id": "known-turn", "status": "completed"}], "nextCursor": None}
                elif method == "turn/interrupt":
                    result = {}
                else:
                    raise AssertionError(f"unexpected synthetic request: {method}")
                await socket.send(json.dumps({"id": message["id"], "result": result}))

        path = str(tmp_path / "review-app.sock")
        async with unix_serve(handle, path, compression=None):
            app = wake.AppServer(wake.WakeConfig(THREAD, app_socket=path))
            app.managed = True
            app.active_turn = "known-turn"
            app.outcome_unknown = True
            old_generation = app.generation
            supervisor = asyncio.create_task(app.supervise())
            waiter = None
            try:
                await asyncio.wait_for(initializing.wait(), 2)
                assert app.connected and not app.ready_event.is_set()
                # Resume a delayed consumer of the known turn while the new
                # transport exists but has not completed the protocol handshake.
                waiter = asyncio.create_task(app._wait_turn(old_generation))
                await asyncio.sleep(0.02)
                assert not premature_metadata.is_set(), "metadata was sent before initialized/thread resume"
                assert calls == ["initialize"]
                assert not waiter.done()
                release_initialize.set()
                await asyncio.wait_for(waiter, 2)
                assert calls == ["initialize", "initialized", "thread/resume", "thread/turns/list"]
                assert app.active_turn is None and not app.outcome_unknown
            finally:
                app.pausing = True
                if waiter is not None:
                    waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
                supervisor.cancel()
                await asyncio.gather(supervisor, return_exceptions=True)
                release_initialize.set()
                await app.close()
                for task in response_tasks:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    asyncio.run(scenario())
