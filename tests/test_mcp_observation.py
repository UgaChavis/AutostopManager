"""Read-only MCP observation never claims a complete service drain."""

from __future__ import annotations

import asyncio
import sys
import threading

import httpx
import pytest
from starlette.responses import JSONResponse

from autostop_manager import listing_executor, mcp_store_lifecycle
from autostop_manager.listing_executor import BoundedListingExecutor, ListingExecutionError
from autostop_manager.mcp_observation import MCPObservation, MCPObservationMiddleware, worker_observations
from autostop_manager.mcp_store_executor import StoreWorkers
from autostop_manager.mcp_store_lifecycle import StoreFastMCP


def test_scope_count_is_thread_safe_and_cannot_be_released_twice():
    observation = MCPObservation()
    first = observation.acquire()
    barrier = threading.Barrier(21)
    tokens = []
    tokens_lock = threading.Lock()

    def contender():
        barrier.wait()
        token = observation.acquire()
        with tokens_lock:
            tokens.append(token)

    contenders = [threading.Thread(target=contender) for _ in range(20)]
    for thread in contenders:
        thread.start()
    barrier.wait()
    for thread in contenders:
        thread.join(2)
        assert not thread.is_alive()
    assert observation.status()["http_scopes_active"] == len(tokens) + 1
    observation.release(first)
    for token in tokens:
        observation.release(token)
    with pytest.raises(KeyError):
        observation.release(first)
    status = observation.status()
    assert status["http_scopes_active"] == 0
    assert status["admission_control"] == "unavailable"
    assert status["service_drained"] is None
    assert status["safe_to_stop"] is False
    assert status["untracked_work"] == "unknown"


def test_middleware_does_not_block_and_releases_scope_on_cancellation():
    async def run():
        observation = MCPObservation()
        entered = asyncio.Event()
        release = asyncio.Event()
        dispatches = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            pass

        async def application(_scope, _receive, _send):
            dispatches.append("entered")
            entered.set()
            await release.wait()

        app = MCPObservationMiddleware(application, observation)
        scope = {"type": "http", "path": "/mcp", "method": "POST", "headers": []}
        first = asyncio.create_task(app(scope, receive, send))
        await entered.wait()
        second = asyncio.create_task(app(scope, receive, send))
        await asyncio.sleep(0)
        assert dispatches == ["entered", "entered"]
        assert observation.status()["http_scopes_active"] == 2
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert observation.status()["http_scopes_active"] == 1
        release.set()
        await second
        assert observation.status()["http_scopes_active"] == 0
        assert observation.status()["service_drained"] is None

    asyncio.run(run())


def test_worker_observation_outlives_caller_timeout(monkeypatch):
    executor = BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=0.1)
    store = StoreWorkers()
    monkeypatch.setattr(listing_executor, "_EXECUTOR", executor)
    started = threading.Event()
    release = threading.Event()

    def operation():
        started.set()
        assert release.wait(5)
        return {"ok": True}

    async def run():
        try:
            with pytest.raises(ListingExecutionError) as error:
                await executor.run(operation)
            assert error.value.adapter_may_continue and started.is_set()
            observations = worker_observations(store)
            assert observations["shared_process_pending"]["avito"] == 1
            assert observations["complete"] is False and observations["atomic"] is False
        finally:
            release.set()
            await asyncio.to_thread(executor.close)
            store.close()
        assert worker_observations(store)["shared_process_pending"]["avito"] == 0
        assert MCPObservation().status()["safe_to_stop"] is False

    asyncio.run(run())


@pytest.mark.parametrize("value", [True, -1, None, "0", RuntimeError("counter failure")])
def test_failed_or_invalid_counter_is_unknown(monkeypatch, value):
    class Unavailable:
        @property
        def pending_count(self):
            if isinstance(value, Exception):
                raise value
            return value

    store = StoreWorkers()
    monkeypatch.setattr(listing_executor, "_EXECUTOR", Unavailable())
    try:
        observation = worker_observations(store)
        assert observation["shared_process_pending"]["avito"] is None
        assert observation["complete"] is False
    finally:
        store.close()


def test_unavailable_worker_snapshot_stays_unknown(monkeypatch):
    manager = StoreFastMCP("synthetic", json_response=True, stateless_http=True)

    def unavailable(_workers):
        raise RuntimeError("synthetic observation failure")

    monkeypatch.setattr(mcp_store_lifecycle, "worker_observations", unavailable)
    try:
        status = manager._mcp_snapshot()
        assert status["worker_observations"]["error_code"] == "worker_observation_unavailable"
        assert status["service_drained"] is None and status["safe_to_stop"] is False
    finally:
        manager.store_workers.close()


def test_observation_does_not_import_missing_executor_module(monkeypatch):
    module_name = "autostop_manager.mcp_drom_executor"
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    store = StoreWorkers()
    try:
        observations = worker_observations(store)
        assert observations["shared_process_pending"]["drom_start"] is None
        assert module_name not in sys.modules
    finally:
        store.close()


def test_observation_at_root_path_is_read_only_and_does_not_change_store():
    async def run():
        manager = StoreFastMCP("synthetic", json_response=True, stateless_http=True)
        app = manager.streamable_http_app()
        async with app.router.lifespan_context(app):
            before = manager.store_workers.status()
            transport = httpx.ASGITransport(app=app, root_path="/prefix")
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:41931") as client:
                response = await client.get("/prefix/internal/mcp-observation")
                assert response.status_code == 200
                assert response.headers["cache-control"] == "no-store"
                status = response.json()
                assert status["format"] == "manager_mcp_observation_v1"
                assert status["http_scopes_active"] == 1
                assert status["admission_control"] == "unavailable"
                assert status["service_drained"] is None and status["safe_to_stop"] is False
                assert status["worker_observations"]["complete"] is False
                assert (
                    await client.get("/prefix/internal/mcp-observation", headers={"Origin": "http://localhost"})
                ).status_code == 403
                assert (await client.post("/prefix/internal/mcp-observation")).status_code == 405
                assert (await client.post("/prefix/internal/mcp-admission/close")).status_code == 404
            assert manager.store_workers.status() == before

    asyncio.run(run())


@pytest.mark.parametrize("path", ["/internal/mcp-observation", "/internal/{operation}", "/internal/mcp-observation/"])
def test_existing_mcp_route_is_never_shadowed(path):
    async def run():
        manager = StoreFastMCP("synthetic", streamable_http_path=path, json_response=True, stateless_http=True)

        @manager.tool(name="synthetic")
        async def synthetic() -> str:
            return "ok"

        app = manager.streamable_http_app()
        assert not any(
            getattr(route, "endpoint", None) == manager._mcp_observation_status for route in app.router.routes
        )
        address = path.replace("{operation}", "mcp-observation")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:41931"
            ) as client:
                response = await client.post(
                    address,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "synthetic", "arguments": {}},
                    },
                    headers={"Accept": "application/json, text/event-stream"},
                )
                assert response.status_code == 200
                assert response.json()["result"]["content"][0]["text"] == "ok"

    asyncio.run(run())


def test_existing_custom_route_with_other_method_is_not_shadowed():
    async def run():
        manager = StoreFastMCP("synthetic", json_response=True, stateless_http=True)

        @manager.custom_route("/internal/mcp-observation", methods=["POST"])
        async def custom(_request):
            return JSONResponse({"original": True})

        app = manager.streamable_http_app()
        assert not any(
            getattr(route, "endpoint", None) == manager._mcp_observation_status for route in app.router.routes
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:41931"
            ) as client:
                assert (await client.get("/internal/mcp-observation")).status_code == 405
                assert (await client.post("/internal/mcp-observation")).json() == {"original": True}

    asyncio.run(run())
