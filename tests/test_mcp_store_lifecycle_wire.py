"""Store shutdown through real stateless MCP and owned, synthetic HTTP servers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
from socketserver import TCPServer
import subprocess
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest
import uvicorn

from autostop_manager import mcp_tools
from autostop_manager.listing_executor import BoundedListingExecutor
from autostop_manager.mcp_store_executor import StoreWorkers
from autostop_manager.mcp_store_lifecycle import StoreFastMCP
from autostop_manager.storage import StoreState
from autostop_manager.store_api import StoreApiClient


@pytest.fixture(autouse=True)
def owned_network_only(monkeypatch, tmp_path):
    ports = set()
    original_bind = socket.socket.bind
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def loopback(host):
        return host in {"127.0.0.1", "::1", b"127.0.0.1", b"::1"}

    def bind(sock, address):
        assert isinstance(address, tuple) and loopback(address[0]) and address[1] == 0
        result = original_bind(sock, address)
        ports.add(sock.getsockname()[1])
        return result

    def check(address):
        assert isinstance(address, tuple) and loopback(address[0]) and address[1] in ports, (
            "Only this test's ephemeral loopback servers are allowed"
        )

    def connect(sock, address):
        check(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(address)
        return original_connect_ex(sock, address)

    def getaddrinfo(host, port, *args, **kwargs):
        check((host, int(port)))
        return original_getaddrinfo(host, port, *args, **kwargs)

    def denied(*_args, **_kwargs):
        raise AssertionError("External DNS and subprocesses are forbidden in lifecycle wire tests")

    # Windows asyncio's stdlib socketpair binds its own ephemeral loopback port.
    monkeypatch.setattr(socket.socket, "bind", bind)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", denied)
    monkeypatch.setattr(socket, "gethostbyaddr", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(os, "system", denied)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,::1")
    monkeypatch.setenv("no_proxy", "127.0.0.1,::1")
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", os.devnull)
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "manager.sqlite3"))
    monkeypatch.setenv("AUTOSTOP_AUTOMATION_DB", str(tmp_path / "automations.sqlite3"))
    monkeypatch.setenv("AUTOSTOP_J1_CACHE_DIR", str(tmp_path / "j1"))
    monkeypatch.setenv("AUTOSTOP_J1_VIN_CACHE_DIR", str(tmp_path / "vin"))
    for name in (
        "get_store_api_url",
        "get_store_read_token",
        "get_store_manage_token",
        "get_store_quote_token",
        "get_store_owner_token",
    ):
        monkeypatch.setattr(mcp_tools, name, lambda: "")


def envelope(item=None):
    return {
        "ok": True,
        "format": "store_agent_v1",
        "status": "completed",
        "summary": {},
        "items": [] if item is None else [item],
        "changes": [],
        "page": {},
        "warnings": [],
        "meta": {},
    }


@pytest.fixture
def fake_store():
    before = {"entity": "store_batch", "id": "batch-1", "storage_location": "A-1", "updated_at": "version-1"}
    after = {**before, "storage_location": "B-2", "updated_at": "version-2"}
    state = SimpleNamespace(
        requests=[],
        errors=[],
        threads=[],
        post_entered=threading.Event(),
        release_post=threading.Event(),
        readback_entered=threading.Event(),
        release_readback=threading.Event(),
    )

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def handle_request(self):
            state.threads.append(threading.current_thread())
            try:
                size = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(size) if size else None
                state.requests.append((self.command, self.path, self.headers.get("Authorization"), body))
                if self.command == "POST":
                    state.post_entered.set()
                    assert state.release_post.wait(12), "fixture POST was never released"
                    result = envelope()
                elif state.post_entered.is_set():
                    state.readback_entered.set()
                    assert state.release_readback.wait(12), "fixture readback was never released"
                    result = envelope(after)
                else:
                    result = envelope(before)
                payload = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()
            except OSError:
                pass  # A failed assertion can close the peer during fixture teardown.
            except Exception as error:  # noqa: BLE001 - forward fixture failures to the owning test.
                state.errors.append(error)
            finally:
                self.close_connection = True

        do_GET = handle_request
        do_POST = handle_request

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def server_bind(self):
            TCPServer.server_bind(self)
            # HTTPServer's default getfqdn is unnecessary for an owned numeric endpoint.
            self.server_name = "synthetic-store"
            self.server_port = self.server_address[1]

    server = Server(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    state.url = f"http://127.0.0.1:{server.server_port}"
    worker.start()
    try:
        yield state
    finally:
        state.release_post.set()
        state.release_readback.set()
        server.shutdown()
        server.server_close()
        worker.join(3)
        assert not worker.is_alive()
        for handler in state.threads:
            handler.join(3)
            assert not handler.is_alive()
        assert not state.errors


async def until(predicate, timeout=6):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@asynccontextmanager
async def serve(manager):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(
        uvicorn.Config(
            manager.streamable_http_app(),
            log_level="error",
            access_log=False,
            timeout_graceful_shutdown=5,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await until(lambda: server.started or task.done())
        assert server.started and not task.done()
        yield SimpleNamespace(url=f"http://127.0.0.1:{listener.getsockname()[1]}", server=server, task=task)
    finally:
        server.should_exit = True
        await asyncio.wait_for(asyncio.shield(task), 15)
        listener.close()


@asynccontextmanager
async def connected(url):
    async with streamable_http_client(url + "/mcp") as (read, write, session_id):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=8)) as session:
            await session.initialize()
            assert session_id() is None
            yield session


def payload(response):
    assert response.isError is False
    assert len(response.content) == 1
    result = json.loads(response.content[0].text)
    assert result == response.structuredContent
    return result


def registered(tmp_path, state, *, wait_timeout=2):
    workers = StoreWorkers(
        reads=BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=8),
        stateful=BoundedListingExecutor(max_workers=1, max_waiting_calls=0, timeout_seconds=wait_timeout),
    )
    manager = StoreFastMCP(
        "synthetic-store-lifecycle",
        store_workers=workers,
        json_response=True,
        stateless_http=True,
        log_level="ERROR",
    )
    client = StoreApiClient(
        api_url=state.url,
        read_token="synthetic-read",
        manage_token="synthetic-manage",
        timeout=8,
        max_read_attempts=1,
    )
    mcp_tools.register_manager_tools(
        manager,
        store=StoreState(tmp_path / "unused.sqlite3"),
        store_client=client,
        include_tools={"store_management_action", "store_runtime_status"},
    )
    return manager, workers


def write_arguments():
    return {
        "domain": "store_batch",
        "action": "set_batch_storage_location",
        "target_id": "batch-1",
        "planned_changes": {"storage_location": "B-2"},
        "owner_intent": "Synthetic exact target instruction",
        "expected_updated_at": "version-1",
        "idempotency_key": "lifecycle-once",
        "correlation_id": "contract:lifecycle-once",
        "mode": "apply",
    }


def assert_exact_write_and_readback(state, arguments):
    assert [request[0] for request in state.requests] == ["GET", "POST", "GET"]
    assert state.requests[0][1] == state.requests[2][1]
    assert urlsplit(state.requests[0][1]).path == "/entities/store_batch/batch-1"
    assert urlsplit(state.requests[1][1]).path == "/actions/set_batch_storage_location"
    assert [request[2] for request in state.requests] == [
        "Bearer synthetic-read",
        "Bearer synthetic-manage",
        "Bearer synthetic-read",
    ]
    posted = json.loads(state.requests[1][3])
    for field in (
        "target_id",
        "planned_changes",
        "owner_intent",
        "expected_updated_at",
        "idempotency_key",
        "correlation_id",
        "mode",
    ):
        assert posted[field] == arguments[field]


async def begin_wire_drain(url, workers):
    async with httpx.AsyncClient(timeout=3) as client:
        before = await client.get(url + "/internal/store-workers")
        assert before.status_code == 200
        assert before.json()["instance_id"] == workers.instance_id
        response = await client.post(
            url + "/internal/store-workers/drain",
            headers={"X-Autostop-Store-Instance": workers.instance_id},
        )
    assert response.status_code in {200, 202}
    result = response.json()
    assert result["format"] == "store_worker_lifecycle_v1"
    assert result["accepting"] is False
    assert result["drained"] is (response.status_code == 200)
    return response.status_code


async def wire_status(url, workers):
    async with httpx.AsyncClient(timeout=3) as client:
        response = await client.get(url + "/internal/store-workers")
    assert response.status_code == 200
    result = response.json()
    assert result["instance_id"] == workers.instance_id
    assert result["format"] == "store_worker_lifecycle_v1"
    return result


@pytest.mark.parametrize("abandonment", ["cancel", "timeout"])
def test_wire_drain_waits_for_abandoned_write_and_its_readback(tmp_path, fake_store, abandonment):
    manager, workers = registered(tmp_path, fake_store, wait_timeout=8 if abandonment == "cancel" else 2)
    arguments = write_arguments()
    original = deepcopy(arguments)

    async def run():
        async with serve(manager) as endpoint, connected(endpoint.url) as session:
            task = asyncio.create_task(session.call_tool("store_management_action", arguments))
            try:
                await until(fake_store.post_entered.is_set)
                async with asyncio.timeout(2):
                    await session.send_ping()
                    listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} == {"store_management_action", "store_runtime_status"}
                if abandonment == "cancel":
                    # ClientSession cancellation does not claim a delivered MCP CancelledNotification.
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    result = payload(await task)
                    assert result["summary"]["error_code"] == "store_executor_wait_timeout"
                    assert result["meta"]["outcome_uncertain"] is True
                    assert result["meta"]["readback_verified"] is False
                assert await begin_wire_drain(endpoint.url, workers) == 202
                draining = await wire_status(endpoint.url, workers)
                assert draining["status"] == "draining" and draining["drained"] is False
                async with asyncio.timeout(2):
                    await session.send_ping()
                    assert {tool.name for tool in (await session.list_tools()).tools} == {
                        "store_management_action",
                        "store_runtime_status",
                    }
                    read_refused = payload(await session.call_tool("store_runtime_status", {"live": False}))
                assert read_refused["summary"]["error_code"] == "store_executor_draining"
                assert read_refused["meta"]["request_dispatched"] is False
                refused = payload(await session.call_tool("store_management_action", arguments))
                assert refused["ok"] is False
                assert refused["summary"]["error_code"] == "store_executor_draining"
                assert refused["meta"]["request_dispatched"] is False
                assert refused["meta"]["outcome_uncertain"] is False
                assert [request[0] for request in fake_store.requests] == ["GET", "POST"]
                assert not (await workers.drain(timeout_seconds=0.05))["ok"]
                assert workers.status()["drained"] is False
                fake_store.release_post.set()
                await until(fake_store.readback_entered.is_set)
                waiting = await wire_status(endpoint.url, workers)
                assert waiting["pending"]["stateful"] == 1
                assert waiting["pending"]["transport"] == 1
                assert waiting["drained"] is False
                assert not (await workers.drain(timeout_seconds=0.05))["ok"]
                fake_store.release_readback.set()
                assert (await workers.drain(timeout_seconds=5))["ok"] is True
                completed = await wire_status(endpoint.url, workers)
                assert completed["pending"] == {"read": 0, "stateful": 0, "transport": 0}
                assert completed["drained"] is True and completed["status"] == "drained"
                assert_exact_write_and_readback(fake_store, original)
                assert arguments == original
            finally:
                fake_store.release_post.set()
                fake_store.release_readback.set()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert not (tmp_path / "unused.sqlite3").exists()


def test_asgi_shutdown_drains_timed_out_write_before_finishing(tmp_path, fake_store):
    manager, workers = registered(tmp_path, fake_store)
    arguments = write_arguments()

    async def run():
        async with serve(manager) as endpoint, connected(endpoint.url) as session:
            task = asyncio.create_task(session.call_tool("store_management_action", arguments))
            try:
                await until(fake_store.post_entered.is_set)
                result = payload(await task)
                assert result["summary"]["error_code"] == "store_executor_wait_timeout"
                endpoint.server.should_exit = True
                await until(lambda: not workers.status()["accepting"])
                assert not endpoint.task.done()
                assert workers.status()["drained"] is False
                fake_store.release_post.set()
                await until(fake_store.readback_entered.is_set)
                assert not endpoint.task.done()
                assert workers.status()["pending"]["stateful"] == 1
                assert workers.status()["drained"] is False
                fake_store.release_readback.set()
                await asyncio.wait_for(asyncio.shield(endpoint.task), 7)
                assert workers.status()["drained"] is True
                assert_exact_write_and_readback(fake_store, arguments)
            finally:
                fake_store.release_post.set()
                fake_store.release_readback.set()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_stateless_requests_and_other_server_shutdown_keep_own_admission_open(tmp_path, fake_store):
    first, first_workers = registered(tmp_path / "first", fake_store)
    second, second_workers = registered(tmp_path / "second", fake_store)
    assert first_workers.instance_id != second_workers.instance_id

    async def run():
        async with serve(first) as one, serve(second) as two:
            async with connected(one.url) as first_session, connected(two.url) as second_session:
                for session, workers in ((first_session, first_workers), (second_session, second_workers)):
                    for _ in range(2):
                        result = payload(await session.call_tool("store_runtime_status", {"live": False}))
                        assert result["ok"] is True
                        assert workers.status()["accepting"] is True
                assert await begin_wire_drain(one.url, first_workers) == 200
                one.server.should_exit = True
                await asyncio.wait_for(asyncio.shield(one.task), 7)
                assert first_workers.status()["drained"] is True
                assert second_workers.status()["accepting"] is True
                result = payload(await second_session.call_tool("store_runtime_status", {"live": False}))
                assert result["ok"] is True
                await second_session.send_ping()
                assert fake_store.requests == []

    asyncio.run(run())
