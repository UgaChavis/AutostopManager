from __future__ import annotations

import asyncio
import json
import os
import socket

import httpx
import pytest
import uvicorn

from autostop_manager import config
import autostop_manager.web_research_gateway as web_gateway
from autostop_manager.mcp_probe import (
    SYNTHETIC_IDENTIFIER,
    async_probe_manager_mcp,
    classify_transport_exception,
)
from autostop_manager.mcp_server import build_server


def test_transport_classifier_keeps_route_auth_and_registration_failures_distinct():
    assert classify_transport_exception(httpx.ConnectError("connection refused")) == "transport_route_unavailable"
    assert classify_transport_exception(ExceptionGroup("transport", [httpx.ConnectError("connection refused")])) == (
        "transport_route_unavailable"
    )
    assert classify_transport_exception(RuntimeError("HTTP 403")) == "transport_auth_failure"
    assert classify_transport_exception(RuntimeError("Unknown tool requested")) == "tool_not_registered"
    assert classify_transport_exception(RuntimeError("Tool not found: retired_command")) == "tool_not_registered"
    assert classify_transport_exception(RuntimeError("unclassified transport error")) == "transport_failure"


@pytest.mark.parametrize(
    ("store_check", "store_ready", "browser_check"),
    [
        (False, False, False),
        (True, False, False),
        (True, True, False),
        (False, False, True),
    ],
)
def test_native_manager_mcp_transport_probe_uses_only_synthetic_redacted_data(
    tmp_path, monkeypatch, caplog, store_check, store_ready, browser_check
):
    sentinel_secret = "probe-secret-must-not-appear"
    previous_env_loaded = config._ENV_LOADED
    for name in tuple(os.environ):
        if name.startswith("PARTSAPI_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AUTOSTOP_MANAGER_DB", str(tmp_path / "manager.sqlite3"))
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", str(tmp_path / "does-not-exist.env"))
    monkeypatch.setenv("PARTSAPI_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("PARTSAPI_VINDECODE_KEY", sentinel_secret)
    config._ENV_LOADED = False
    monkeypatch.setattr(
        "autostop_manager.store_api.StoreApiClient.runtime_status",
        lambda *a, **kw: {"ok": store_ready, "private_payload": sentinel_secret},
    )
    monkeypatch.setattr(
        "autostop_manager.store_owner_api.StoreOwnerApiClient.list_capabilities",
        lambda *a, **kw: {"ok": store_ready, "private_payload": sentinel_secret},
    )
    monkeypatch.setattr(
        web_gateway,
        "fetch_j1_browser_page",
        lambda url, *, max_chars: {
            "ok": True,
            "url": url,
            "title": "Example Domain",
            "text": "Example Domain synthetic browser smoke"[:max_chars],
        },
    )

    async def run_probe() -> dict[str, object]:
        manager_server = build_server()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        uvicorn_server = uvicorn.Server(
            uvicorn.Config(manager_server.streamable_http_app(), log_level="warning", access_log=False)
        )
        task = asyncio.create_task(uvicorn_server.serve(sockets=[listener]))
        try:
            for _ in range(200):
                if uvicorn_server.started:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("native_manager_mcp_test_server_did_not_start")
            return await async_probe_manager_mcp(
                f"http://127.0.0.1:{port}/mcp",
                timeout=5,
                provider_failure_check=True,
                store_check=store_check,
                browser_check=browser_check,
            )
        finally:
            uvicorn_server.should_exit = True
            await asyncio.wait_for(task, timeout=5)
            listener.close()

    try:
        report = asyncio.run(run_probe())
    finally:
        config._ENV_LOADED = previous_env_loaded

    rendered = json.dumps(report, ensure_ascii=False)
    assert report["ok"] is (not store_check or store_ready)
    assert report["diagnostic"] == ("ok" if not store_check or store_ready else "store_connection_unavailable")
    if store_check:
        assert report["checks"]["store_runtime_status"] == {"ok": store_ready}
        assert report["checks"]["store_owner_capabilities"] == {"ok": store_ready}
    if browser_check:
        assert report["checks"]["fetch_page_browser"] == {
            "ok": True,
            "mode": "browser",
            "domain": "example.com",
            "has_excerpt": True,
        }
    assert report["checks"]["native_ping"]["ok"] is True
    assert report["checks"]["tools_list"]["ok"] is True
    assert report["checks"]["tools_list"]["tool_count"] == 42
    assert report["checks"]["catalog_provider_status"]["ok"] is True
    assert report["checks"]["catalog_provider_status"]["stage"] == "catalog_cross"
    assert report["checks"]["synthetic_resolver"]["ok"] is True
    assert report["checks"]["synthetic_resolver"]["diagnostic"] == "vin_decode_dry_run"
    assert report["checks"]["synthetic_resolver"]["live_call_count"] == 0
    assert report["checks"]["synthetic_resolver"]["oem_candidate_count"] == 0
    assert report["checks"]["provider_failure"]["ok"] is True
    assert report["checks"]["provider_failure"]["diagnostic"] == "provider_failure"
    assert report["checks"]["provider_failure"]["failure_class"] in {
        "network_error",
        "timeout",
        "adapter_malformed_payload",
    }
    assert report["privacy"]["raw_identifier_returned"] is False
    assert report["privacy"]["secret_exposed"] is False
    assert SYNTHETIC_IDENTIFIER not in rendered
    assert sentinel_secret not in rendered
    assert SYNTHETIC_IDENTIFIER not in caplog.text
    assert sentinel_secret not in caplog.text
