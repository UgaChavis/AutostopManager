"""Exercise local HTTP controls and real outer ASGI lifetime without network."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import socket
import threading

import anyio
import httpx
import pytest
from starlette.requests import Request

from autostop_manager import mcp_store_lifecycle
from autostop_manager.mcp_store_executor import offload_store
from autostop_manager.mcp_store_lifecycle import INSTANCE_HEADER, StoreFastMCP


BASE = "http://127.0.0.1:41931"
STATUS = "/internal/store-workers"
DRAIN = STATUS + "/drain"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("Store control tests must use only in-process ASGI")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture
def servers():
    created = []

    def create(**kwargs):
        server = StoreFastMCP(
            "synthetic-store-control",
            host="127.0.0.1",
            port=41931,
            stateless_http=True,
            json_response=True,
            **kwargs,
        )
        created.append(server)
        return server

    yield create
    for server in created:
        assert server.store_workers.status()["pending"] == {"read": 0, "stateful": 0, "transport": 0}
        server.store_workers.close()


async def call(server, method="GET", path=STATUS, *, headers=None, content=None, peer=("127.0.0.1", 32000)):
    transport = httpx.ASGITransport(app=server.streamable_http_app(), client=peer)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        return await client.request(method, path, headers=headers, content=content)


@pytest.mark.parametrize("method,path", [("GET", STATUS), ("POST", DRAIN)])
@pytest.mark.parametrize(
    "headers,peer",
    [
        ({"Origin": "http://localhost:41931"}, ("127.0.0.1", 32000)),
        ({"Origin": "null"}, ("127.0.0.1", 32000)),
        ({"Origin": ""}, ("127.0.0.1", 32000)),
        ({"Forwarded": "for=127.0.0.1"}, ("127.0.0.1", 32000)),
        ({"X-Forwarded-For": "127.0.0.1"}, ("127.0.0.1", 32000)),
        ({"X-Forwarded-Host": "127.0.0.1:41931"}, ("127.0.0.1", 32000)),
        ({"X-Forwarded-Proto": "http"}, ("127.0.0.1", 32000)),
        ({"X-Forwarded-Unknown": ""}, ("127.0.0.1", 32000)),
        ({}, ("192.0.2.20", 32000)),
        ({}, ("not-an-ip", 32000)),
        ({}, None),
    ],
)
def test_controls_reject_nonlocal_browser_and_proxy_requests(servers, method, path, headers, peer):
    server = servers()
    supplied = {INSTANCE_HEADER: server.store_workers.instance_id, **headers}
    response = asyncio.run(call(server, method, path, headers=supplied, peer=peer))
    assert response.status_code == 403
    assert response.json() == {"error_code": "store_control_local_request_required"}
    assert response.headers["cache-control"] == "no-store"
    assert server.store_workers.status()["accepting"] is True


@pytest.mark.parametrize(
    "hosts",
    [
        [],
        ["127.0.0.1:41931", "127.0.0.1:41931"],
        ["127.0.0.1:41931", "attacker.invalid:41931"],
        ["attacker.invalid:41931"],
        ["127.0.0.1:41932"],
        ["127.0.0.9:41931"],
        ["127.0.0.1"],
        ["127.0.0.1:65536"],
        ["127.0.0.1:bad"],
        ["[::1:41931"],
        ["user@127.0.0.1:41931"],
        ["@127.0.0.1:41931"],
        ["127.0.0.1:41931?"],
        ["127.0.0.1:41931#"],
        ["127.0.0.1:41931/"],
        ["\t127.0.0.1:41931"],
        ["127.0.0.1:\t41931"],
    ],
)
def test_status_rejects_missing_duplicate_and_malformed_host(servers, hosts):
    server = servers()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": STATUS,
            "headers": [(b"host", value.encode()) for value in hosts],
            "client": ("127.0.0.1", 32000),
            "server": ("127.0.0.1", 41931),
        }
    )
    response = asyncio.run(server._worker_status(request))
    assert response.status_code == 403
    assert server.store_workers.status()["accepting"] is True


@pytest.mark.parametrize(
    "host,peer,port",
    [
        ("127.0.0.1:41931", "127.0.0.1", 41931),
        ("127.0.0.9:41931", "127.0.0.9", 41931),
        ("localhost:41931", "127.0.0.1", 41931),
        ("[::1]:41931", "::1", 41931),
        ("127.0.0.1", "127.0.0.1", 80),
        ("localhost", "127.0.0.1", 80),
        ("[::1]", "::1", 80),
        ("127.0.0.1:80", "127.0.0.1", 80),
        ("[::1]:80", "::1", 80),
    ],
)
def test_status_accepts_canonical_loopback_hosts_and_actual_port(servers, host, peer, port):
    server = servers()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": STATUS,
            "headers": [(b"host", host.encode())],
            "client": (peer, 32000),
            "server": (peer, port),
        }
    )
    response = asyncio.run(server._worker_status(request))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert server.store_workers.status()["accepting"] is True


@pytest.mark.parametrize("instance_values", [[], ["stale"], ["current", "current"], ["current", "stale"]])
def test_drain_requires_exactly_one_current_instance_header(servers, instance_values):
    server = servers()
    headers = [
        (INSTANCE_HEADER, server.store_workers.instance_id if value == "current" else value)
        for value in instance_values
    ]
    response = asyncio.run(call(server, "POST", DRAIN, headers=headers))
    assert response.status_code == 409
    assert response.json()["error_code"] == "store_control_instance_mismatch"
    assert response.headers["cache-control"] == "no-store"
    assert server.store_workers.status()["accepting"] is True


@pytest.mark.parametrize(
    "extra_headers,content",
    [
        ([], b"x"),
        ([("Content-Length", "1")], b""),
        ([("Content-Length", "bad")], b""),
        ([("Content-Length", "0"), ("Content-Length", "0")], b""),
        ([("Transfer-Encoding", "chunked")], b""),
    ],
)
def test_drain_rejects_nonempty_or_ambiguous_body_framing(servers, extra_headers, content):
    server = servers()
    headers = [(INSTANCE_HEADER, server.store_workers.instance_id), *extra_headers]
    response = asyncio.run(call(server, "POST", DRAIN, headers=headers, content=content))
    assert response.status_code == 400
    assert response.json()["error_code"] == "store_control_empty_body_required"
    assert response.headers["cache-control"] == "no-store"
    assert server.store_workers.status()["accepting"] is True


def test_empty_drain_is_idempotent_keeps_polling_and_does_not_change_other_server(servers):
    first, second = servers(), servers()

    async def run():
        initial = await call(first)
        assert initial.headers["cache-control"] == "no-store"
        packet = initial.json()
        assert packet["accepting"] and not packet["drained"]
        assert packet["instance_id"] != second.store_workers.instance_id
        headers = {INSTANCE_HEADER: packet["instance_id"]}
        once = await call(first, "POST", DRAIN, headers=headers)
        twice = await call(first, "POST", DRAIN, headers=headers)
        after = await call(first)
        assert once.status_code == twice.status_code == after.status_code == 200
        assert once.json() == twice.json() == after.json()
        assert after.json()["drained"] and not after.json()["accepting"]
        assert after.json()["pending"] == {"read": 0, "stateful": 0, "transport": 0}
        assert second.store_workers.status()["accepting"] is True
        assert await first.list_tools() == await second.list_tools() == []

    asyncio.run(run())


def test_drain_returns_immediately_while_accepted_worker_and_status_remain_available(servers):
    server = servers()
    workers = server.store_workers
    release = threading.Event()

    async def run():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def operation():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3)
            return {"ok": True}

        task = asyncio.create_task(workers.stateful.run(operation))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            response = await asyncio.wait_for(
                call(server, "POST", DRAIN, headers={INSTANCE_HEADER: workers.instance_id}), 1
            )
            assert response.status_code == 202
            assert response.json()["status"] == "draining"
            assert response.json()["pending"]["stateful"] == 1
            polled = await asyncio.wait_for(call(server), 1)
            assert polled.status_code == 200
            assert polled.json() == response.json()
            assert not task.done()
        finally:
            release.set()
            assert await task == {"ok": True}
        final = await call(server)
        assert final.json()["status"] == "drained"
        assert final.json()["drained"] is True

    try:
        asyncio.run(run())
    finally:
        release.set()


@pytest.mark.parametrize("path", [STATUS, DRAIN])
def test_control_routes_do_not_offer_cors_preflight(servers, path):
    server = servers()
    response = asyncio.run(
        call(
            server,
            "OPTIONS",
            path,
            headers={"Origin": "https://attacker.invalid", "Access-Control-Request-Method": "POST"},
        )
    )
    assert response.status_code == 405
    assert not any(name.startswith("access-control-") for name in response.headers)
    assert server.store_workers.status()["accepting"] is True


def test_stateless_request_lifespan_does_not_close_application_workers(servers):
    events = []

    @asynccontextmanager
    async def request_lifespan(_server):
        events.append("request_start")
        try:
            yield {}
        finally:
            events.append("request_end")

    server = servers(lifespan=request_lifespan)
    app = server.streamable_http_app()

    async def run():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 32000))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
                for request_id in (1, 2):
                    response = await client.post(
                        "/mcp",
                        json={"jsonrpc": "2.0", "id": request_id, "method": "tools/list"},
                        headers={"Accept": "application/json, text/event-stream"},
                    )
                    assert response.status_code == 200
                    assert response.json()["result"]["tools"] == []
                    assert server.store_workers.status()["accepting"] is True
            assert events.count("request_start") == 2
        assert events.count("request_start") == events.count("request_end") == 2
        assert server.store_workers.status()["drained"] is True

    asyncio.run(run())


def test_shutdown_timeout_reports_unfinished_worker_and_keeps_admission_closed(servers, monkeypatch, caplog):
    monkeypatch.setattr(mcp_store_lifecycle, "SHUTDOWN_DRAIN_SECONDS", 0.02)
    caplog.set_level(logging.ERROR, logger=mcp_store_lifecycle.__name__)
    server = servers()
    workers = server.store_workers
    app = server.streamable_http_app()
    release = threading.Event()

    async def run():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def operation():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3)
            return {"ok": True}

        task = None
        try:
            async with app.router.lifespan_context(app):
                task = asyncio.create_task(workers.stateful.run(operation))
                await asyncio.wait_for(entered.wait(), 1)
            assert workers.status()["pending"]["stateful"] == 1
            assert workers.status()["accepting"] is False
            assert workers.status()["drained"] is False
            assert "store_drain_timeout" in caplog.text
            refused = await offload_store(
                lambda: pytest.fail("Drain admitted another write"), "store_owner_api", workers
            )()
            assert refused["error"]["code"] == "store_executor_draining"
            assert refused["meta"]["request_dispatched"] is False
        finally:
            release.set()
            if task is not None:
                assert await task == {"ok": True}
        assert (await workers.drain(1))["ok"] is True

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_outer_shutdown_shields_drain_from_anyio_cancellation(servers, monkeypatch):
    monkeypatch.setattr(mcp_store_lifecycle, "SHUTDOWN_DRAIN_SECONDS", 1)
    server = servers()
    workers = server.store_workers
    app = server.streamable_http_app()
    release = threading.Event()
    completed = threading.Event()

    async def run():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def operation():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3)
            completed.set()
            return {"ok": True}

        task = None
        try:
            with anyio.CancelScope() as cancellation:
                async with app.router.lifespan_context(app):
                    task = asyncio.create_task(workers.stateful.run(operation))
                    await asyncio.wait_for(entered.wait(), 1)
                    loop.call_later(0.02, release.set)
                    cancellation.cancel()
            assert completed.is_set()
            assert workers.status()["drained"] is True
        finally:
            release.set()
            if task is not None:
                assert await task == {"ok": True}

    try:
        asyncio.run(run())
    finally:
        release.set()
