from __future__ import annotations

from autostop_manager import mcp_server


class _FakeFastMCP:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.run_calls = []

    def run(self, **kwargs):
        self.run_calls.append(kwargs)


def test_build_server_uses_runtime_transport_config_and_registers_tools(monkeypatch):
    registered = []
    monkeypatch.setattr(mcp_server, "FastMCP", _FakeFastMCP)
    monkeypatch.setattr(mcp_server, "get_mcp_host", lambda: "127.0.0.9")
    monkeypatch.setattr(mcp_server, "get_mcp_port", lambda: 41931)
    monkeypatch.setattr(mcp_server, "get_mcp_path", lambda: "/manager-mcp")
    monkeypatch.setattr(mcp_server, "register_manager_memory_tools", registered.append)

    server = mcp_server.build_server()

    assert registered == [server]
    assert server.kwargs["name"] == "AutostopManager"
    assert server.kwargs["host"] == "127.0.0.9"
    assert server.kwargs["port"] == 41931
    assert server.kwargs["streamable_http_path"] == "/manager-mcp"
    assert server.kwargs["json_response"] is True
    assert server.kwargs["stateless_http"] is True
    assert "agent_bootstrap" in server.kwargs["instructions"]


def test_main_runs_streamable_http_server(monkeypatch):
    server = _FakeFastMCP()
    monkeypatch.setattr(mcp_server, "build_server", lambda: server)

    mcp_server.main()

    assert server.run_calls == [{"transport": "streamable-http"}]
