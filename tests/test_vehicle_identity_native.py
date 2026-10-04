from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import socket

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest
import uvicorn

from autostop_manager import config
from autostop_manager import mcp_tools
from autostop_manager.mcp_server import build_server
from autostop_manager.vehicle_identity_transport import IdentityBudget

SYNTHETIC_VIN = "WBA00000000000000"


@pytest.fixture(autouse=True)
def isolated_native_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", str(tmp_path / "empty.env"))
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "manager.sqlite3"))
    monkeypatch.setattr(config, "_ENV_LOADED", False)


@asynccontextmanager
async def native_server():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(build_server().streamable_http_app(), log_level="warning", access_log=False))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("native_e4_server_did_not_start")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        listener.close()


def test_native_year_validation_and_batch_row_isolation():
    async def run():
        async with native_server() as url:
            async with streamable_http_client(url) as (reader, writer, _):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    for field in ("model_year", "production_year", "transmission_speeds", "source_confidence"):
                        result = await session.call_tool(
                            "decode_vehicle_identity",
                            {"identifier": SYNTHETIC_VIN, field: True, "live_vpic": False, "live_wmi": False},
                        )
                        assert result.isError is True, field
                    result = await session.call_tool(
                        "decode_vehicle_identities",
                        {
                            "items": [
                                {"identifier": SYNTHETIC_VIN, "crm_context": 7},
                                {
                                    "identifier": SYNTHETIC_VIN,
                                    "production_year": "2004",
                                    "make": "BMW",
                                    "model": "TEST",
                                },
                                {"identifier": SYNTHETIC_VIN, "model_year": True},
                            ],
                            "live_vpic": False,
                        },
                    )
                    assert result.isError is False
                    payload = result.structuredContent or json.loads(result.content[0].text)
                    assert payload["count"] == 3
                    assert [row["item_index"] for row in payload["results"]] == [0, 1, 2]
                    for index in (0, 2):
                        row = payload["results"][index]
                        assert row["ok"] is False
                        assert row["vehicle_profile"] == {}
                        assert row["errors"]
                        assert row["parts_lookup_readiness"]["ready_for_family_lookup"] is False
                    healthy = payload["results"][1]
                    assert healthy["ok"] is True
                    assert all(row.keys() == healthy.keys() for row in payload["results"])
                    assert healthy["vehicle_profile"]["production_year"] == 2004
                    assert healthy["normalization_notes"]
                    assert healthy["parts_lookup_readiness"]["ready_for_crm_writeback"] is False
                    assert (await session.list_tools()).tools
                    await session.send_ping()

    asyncio.run(run())


def test_native_batch_preserves_context_and_equivalent_aliases_around_malformed_metadata():
    async def run():
        identifier = "WDD" + "212034" + "A" + "1" * 7
        context = {
            "make": "Mercedes-Benz",
            "model": "E200",
            "model_year": 2010,
            "engine": "M274.920",
        }
        items = [
            {"identifier": identifier, "crm_context": context, "model": None, "engine": None},
            {
                "identifier": identifier,
                "crm_context": {
                    **context,
                    "input_alias_conflicts": [
                        {"field": [], "canonical_value": "E200", "alias_value": "E200", "source": "model_display"}
                    ],
                },
            },
            {"identifier": identifier, "crm_context": {**context, "model_display": "Е200"}},
            {"identifier": identifier, "crm_context": context},
        ]
        async with native_server() as url:
            async with streamable_http_client(url) as (reader, writer, _):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    response = await asyncio.wait_for(
                        session.call_tool("decode_vehicle_identities", {"items": items, "live_vpic": False}), 5
                    )
                    assert response.isError is False
                    payload = response.structuredContent or json.loads(response.content[0].text)
                    assert payload["count"] == 4
                    rows = payload["results"]
                    assert [row["item_index"] for row in rows] == [0, 1, 2, 3]
                    assert all(row.keys() == rows[0].keys() for row in rows)
                    assert rows[1]["ok"] is False
                    assert rows[1]["vehicle_profile"] == {}
                    assert rows[1]["errors"]
                    for index in (0, 2, 3):
                        assert rows[index]["ok"] is True
                        assert rows[index]["vehicle_profile"]["model"] == "E200"
                        assert rows[index]["vehicle_profile"]["engine"] == "M274.920"
                        assert rows[index]["parts_lookup_readiness"]["ready_for_vehicle_lookup"] is True
                        assert rows[index]["parts_lookup_readiness"]["ready_for_crm_writeback"] is False
                    assert not rows[2]["conflicts"]
                    await session.send_ping()

    asyncio.run(run())


def test_native_ping_remains_responsive_during_provider_wait(monkeypatch):
    original = mcp_tools.decode_vehicle_identity_async

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def provider(request):
            calls.append(request.url.path)
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"Results": [{"Make": "BMW", "Model": "TEST", "VIN": SYNTHETIC_VIN}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:

            async def decode(identifier, **kwargs):
                return await original(
                    identifier, **kwargs, client=client, budget=IdentityBudget(deadline_seconds=2, max_attempts=2)
                )

            monkeypatch.setattr(mcp_tools, "decode_vehicle_identity_async", decode)
            async with native_server() as url:
                async with streamable_http_client(url) as (reader, writer, _):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        decoding = asyncio.create_task(
                            session.call_tool(
                                "decode_vehicle_identity", {"identifier": SYNTHETIC_VIN, "live_wmi": False}
                            )
                        )
                        try:
                            await asyncio.wait_for(entered.wait(), 1)
                            await asyncio.wait_for(session.send_ping(), 0.5)
                            assert not decoding.done()
                            release.set()
                            result = await asyncio.wait_for(decoding, 2)
                            assert result.isError is False
                            payload = result.structuredContent
                            assert payload["vehicle_profile"]["make"] == "BMW"
                            assert payload["processing"]["http_attempts"] == 1
                            assert len(calls) == 1
                        finally:
                            release.set()
                            await asyncio.gather(decoding, return_exceptions=True)

    asyncio.run(run())


def test_native_http_disconnect_stops_provider_work(monkeypatch):
    original = mcp_tools.decode_vehicle_identity_async

    async def run():
        entered = asyncio.Event()
        closed = asyncio.Event()
        calls = []

        async def provider(request):
            calls.append(request.url.path)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as providers:

            async def decode(identifier, **kwargs):
                return await original(
                    identifier, **kwargs, client=providers, budget=IdentityBudget(deadline_seconds=2, max_attempts=2)
                )

            monkeypatch.setattr(mcp_tools, "decode_vehicle_identity_async", decode)
            async with native_server() as url:
                async with httpx.AsyncClient() as caller:
                    calling = asyncio.create_task(
                        caller.post(
                            url,
                            headers={"Accept": "application/json, text/event-stream"},
                            json={
                                "jsonrpc": "2.0",
                                "id": 41,
                                "method": "tools/call",
                                "params": {
                                    "name": "decode_vehicle_identity",
                                    "arguments": {"identifier": SYNTHETIC_VIN, "live_wmi": False},
                                },
                            },
                        )
                    )
                    try:
                        await asyncio.wait_for(entered.wait(), 1)
                        calling.cancel()
                        await asyncio.gather(calling, return_exceptions=True)
                        await asyncio.wait_for(closed.wait(), 1)
                        await asyncio.sleep(0.06)
                        assert len(calls) == 1
                    finally:
                        calling.cancel()
                        await asyncio.gather(calling, return_exceptions=True)

    asyncio.run(run())
