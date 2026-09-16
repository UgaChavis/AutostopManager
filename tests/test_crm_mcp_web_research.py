from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from autostop_manager import config, crm_mcp_web_research as crm_transport, mcp_server


def _connection() -> config.CrmMcpConnectionConfig:
    return config.CrmMcpConnectionConfig(
        configured=True,
        url="http://127.0.0.1:8001/mcp",
        bearer_token="synthetic-manager-only-bearer",
    )


def test_crm_mcp_config_uses_manager_environment_and_rejects_non_loopback(monkeypatch):
    monkeypatch.delenv(config.CRM_MCP_URL_ENV, raising=False)
    monkeypatch.delenv(config.CRM_MCP_BEARER_TOKEN_ENV, raising=False)
    assert config.get_crm_mcp_connection_config() == config.CrmMcpConnectionConfig(configured=False)
    assert crm_transport.build_crm_mcp_web_research_gateway(config.CrmMcpConnectionConfig(configured=False)) is None

    monkeypatch.setenv(config.CRM_MCP_URL_ENV, "http://127.0.0.1:8001/mcp")
    monkeypatch.setenv(config.CRM_MCP_BEARER_TOKEN_ENV, "synthetic-manager-only-bearer")
    connection = config.get_crm_mcp_connection_config()
    assert connection.configured is True
    assert connection.url == "http://127.0.0.1:8001/mcp"
    assert connection.bearer_token == "synthetic-manager-only-bearer"
    assert "synthetic-manager-only-bearer" not in repr(connection)

    for url in (
        "https://127.0.0.1:8001/mcp",
        "http://localhost:8001/mcp",
        "http://192.0.2.1:8001/mcp",
        "http://user:password@127.0.0.1:8001/mcp",
        "http://127.0.0.1:8001/mcp?token=private",
        "http://127.0.0.1:8001/another-route",
        "http://127.0.0.1:41831/mcp",
    ):
        monkeypatch.setenv(config.CRM_MCP_URL_ENV, url)
        assert config.get_crm_mcp_connection_config().error_code == "crm_mcp_configuration_invalid"


def test_transport_discovers_schema_then_calls_only_part_evidence_with_hash(monkeypatch):
    captured: dict[str, object] = {"calls": []}

    class FakeHttpClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    @asynccontextmanager
    async def fake_stream(url, *, http_client):
        captured["url"] = url
        captured["http_client"] = http_client
        yield object(), object(), lambda: None

    class FakeSession:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def initialize(self):
            captured["initialized"] = True

        async def call_tool(self, name, arguments, **kwargs):
            captured["calls"].append((name, arguments, kwargs))
            if name == "get_raw_capability_schema":
                return SimpleNamespace(
                    isError=False,
                    structuredContent={
                        "ok": True,
                        "summary": {
                            "name": "research_part_public_evidence",
                            "risk": "read",
                            "schema_hash": "a" * 64,
                        },
                        "data": {"input_schema": {"type": "object"}},
                    },
                )
            return SimpleNamespace(
                isError=False,
                structuredContent={
                    "ok": True,
                    "data": {
                        "ok": True,
                        "results": [{"url": "https://partsouq.com/catalog/1712024"}],
                    },
                },
            )

    monkeypatch.setattr(crm_transport.httpx, "AsyncClient", FakeHttpClient)
    monkeypatch.setattr(crm_transport, "streamable_http_client", fake_stream)
    monkeypatch.setattr(crm_transport, "ClientSession", FakeSession)

    result = crm_transport.LoopbackCrmMcpWebResearchTransport(_connection()).invoke(
        "research_part_public_evidence", {"query": "FORD 1712024", "limit": 3}
    )

    assert result == {
        "ok": True,
        "data": {"ok": True, "results": [{"url": "https://partsouq.com/catalog/1712024"}]},
    }
    assert captured["initialized"] is True
    assert captured["url"] == "http://127.0.0.1:8001/mcp"
    assert captured["client_kwargs"] == {
        "timeout": crm_transport.CRM_MCP_E8_TIMEOUT_SECONDS,
        "follow_redirects": False,
        "headers": {"Authorization": "Bearer synthetic-manager-only-bearer"},
        "trust_env": False,
    }
    assert captured["calls"] == [
        ("get_raw_capability_schema", {"name": "research_part_public_evidence"}, captured["calls"][0][2]),
        (
            "call_raw_capability",
            {
                "name": "research_part_public_evidence",
                "arguments": {"query": "FORD 1712024", "limit": 3},
                "schema_hash": "a" * 64,
                "allow_large_output": False,
            },
            captured["calls"][1][2],
        ),
    ]


def test_configured_transport_error_is_structured_and_never_uses_local_fallback(monkeypatch):
    @asynccontextmanager
    async def failed_stream(*_args, **_kwargs):
        raise RuntimeError("private bearer must not escape")
        yield  # pragma: no cover

    monkeypatch.setattr(crm_transport, "streamable_http_client", failed_stream)
    gateway = crm_transport.build_crm_mcp_web_research_gateway(_connection())
    assert gateway is not None

    result = gateway.research_part_public_evidence(query="FORD 1712024")

    assert result["ok"] is False
    assert result["fallback_used"] is False
    assert result["error"] == {"code": "crm_mcp_transport_failed", "retryable": True}
    assert "private bearer" not in json.dumps(result)


def test_mcp_server_installs_optional_crm_gateway(monkeypatch):
    installed = []

    class FakeServer:
        def __init__(self, **_kwargs):
            pass

    gateway = object()
    monkeypatch.setattr(mcp_server, "FastMCP", FakeServer)
    monkeypatch.setattr(mcp_server, "load_runtime_env", lambda: None)
    monkeypatch.setattr(mcp_server, "build_crm_mcp_web_research_gateway", lambda: gateway)
    monkeypatch.setattr(mcp_server, "install_web_research_gateway", installed.append)
    monkeypatch.setattr(mcp_server, "get_mcp_host", lambda: "127.0.0.1")
    monkeypatch.setattr(mcp_server, "get_mcp_port", lambda: 41931)
    monkeypatch.setattr(mcp_server, "get_mcp_path", lambda: "/mcp")
    monkeypatch.setattr(mcp_server, "register_manager_tools", lambda _server: None)
    monkeypatch.setattr(mcp_server, "assert_manager_mcp_surface", lambda _server: None)

    mcp_server.build_server()

    assert installed == [gateway]
