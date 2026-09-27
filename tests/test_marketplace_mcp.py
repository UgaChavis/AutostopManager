from __future__ import annotations

import asyncio
import socket

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import uvicorn

from autostop_manager import config, mcp_tools
from autostop_manager.catalog_adapters import catalog_provider_status
from autostop_manager.mcp_probe import _payload_from_tool_result
from autostop_manager.mcp_server import build_server
from autostop_manager.storage import StoreState


class _ToolServer:
    def __init__(self) -> None:
        self.tools = {}
        self.options = {}

    def tool(self, *, name, description="", **kwargs):
        def register(function):
            self.tools[name] = function
            self.options[name] = kwargs
            return function

        return register


def test_marketplace_status_requires_both_webbee_settings_and_never_exposes_keys(monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setattr(config, "_ENV_LOADED", False)
    for name in ("REEFAPI_API_KEY", "WEBBEE_API_TOKEN", "WEBBEE_DROM_ROBOT_ALIAS"):
        monkeypatch.delenv(name, raising=False)

    missing = catalog_provider_status(stage="market_listing")
    assert {row["source_id"] for row in missing["providers"]} == {"avito_reefapi", "drom_webbee"}
    assert all(row["configured"] is False for row in missing["providers"])
    assert all(row["live_callable_now"] is False for row in missing["providers"])
    assert all(row["authorization_status"] == "not_configured" for row in missing["providers"])

    monkeypatch.setenv("REEFAPI_API_KEY", "reef-private-fixture")
    monkeypatch.setenv("WEBBEE_API_TOKEN", "webbee-private-fixture")
    partial = catalog_provider_status(stage="market_listing")
    assert next(row for row in partial["providers"] if row["source_id"] == "avito_reefapi")["configured"] is True
    assert next(row for row in partial["providers"] if row["source_id"] == "drom_webbee")["configured"] is False
    assert "reef-private-fixture" not in repr(partial)
    assert "webbee-private-fixture" not in repr(partial)

    monkeypatch.setenv("WEBBEE_DROM_ROBOT_ALIAS", "test-robot")
    complete = catalog_provider_status(stage="market_listing")
    assert all(row["configured"] is True for row in complete["providers"])
    assert all(row["live_callable_now"] is True for row in complete["providers"])
    assert all(row["authorization_status"] == "unverified" for row in complete["providers"])


def test_marketplace_mcp_tools_are_independent_and_forward_arguments(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setattr(config, "_ENV_LOADED", False)
    calls = []

    def forwarded(name):
        def call(**kwargs):
            calls.append((name, kwargs))
            return {"ok": True, "name": name}

        return call

    for name in (
        "avito_search_listings",
        "avito_read_listing",
        "drom_start_parts_search",
        "drom_get_parts_search",
    ):
        monkeypatch.setattr(mcp_tools, name, forwarded(name))
    server = _ToolServer()
    mcp_tools.register_manager_tools(server, store=StoreState(tmp_path / "memory.sqlite3"))

    assert server.tools["avito_search_listings"]("filter", dry_run=True)["name"] == "avito_search_listings"
    assert server.tools["avito_read_listing"]("123", dry_run=True)["name"] == "avito_read_listing"
    assert server.tools["drom_start_parts_search"]("filter", dry_run=True)["name"] == "drom_start_parts_search"
    assert server.tools["drom_get_parts_search"](12, "uid")["name"] == "drom_get_parts_search"
    assert [name for name, _ in calls] == [
        "avito_search_listings",
        "avito_read_listing",
        "drom_start_parts_search",
        "drom_get_parts_search",
    ]
    assert calls[2][1]["page_limit"] == 3
    assert server.options["drom_start_parts_search"]["annotations"].readOnlyHint is False


def test_marketplace_tools_are_callable_through_local_manager_mcp(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "manager.sqlite3"))
    monkeypatch.setattr(config, "_ENV_LOADED", False)
    for name in ("REEFAPI_API_KEY", "WEBBEE_API_TOKEN", "WEBBEE_DROM_ROBOT_ALIAS"):
        monkeypatch.delenv(name, raising=False)

    async def call_tools():
        server = build_server()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        uvicorn_server = uvicorn.Server(
            uvicorn.Config(server.streamable_http_app(), log_level="warning", access_log=False)
        )
        task = asyncio.create_task(uvicorn_server.serve(sockets=[listener]))
        try:
            for _ in range(200):
                if uvicorn_server.started:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("manager_mcp_test_server_did_not_start")
            async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = {}
                    for name, arguments in (
                        ("avito_search_listings", {"query": "тормозной диск", "dry_run": True}),
                        ("avito_read_listing", {"ad_id": "8386499447", "dry_run": True}),
                        ("drom_start_parts_search", {"query": "тормозной диск", "dry_run": True}),
                        ("drom_get_parts_search", {"task_id": 1, "uid": "test-run"}),
                    ):
                        response = await session.call_tool(name, arguments)
                        assert response.isError is False
                        result[name] = _payload_from_tool_result(response)
                    return result
        finally:
            uvicorn_server.should_exit = True
            await asyncio.wait_for(task, timeout=5)
            listener.close()

    responses = asyncio.run(call_tools())
    for name in ("avito_search_listings", "avito_read_listing", "drom_start_parts_search"):
        assert responses[name]["ok"] is True
    assert responses["drom_start_parts_search"]["status"] == "dry_run"
    assert responses["drom_get_parts_search"]["error"] == "webbee_not_configured"
