from __future__ import annotations

from autostop_manager import mcp_server
from autostop_manager import config


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
    monkeypatch.setattr(mcp_server, "register_manager_tools", registered.append)
    monkeypatch.setattr(mcp_server, "assert_manager_mcp_surface", lambda server: {"ok": True})

    server = mcp_server.build_server()

    assert registered == [server]
    assert server.kwargs["name"] == "AutostopManager"
    assert server.kwargs["host"] == "127.0.0.9"
    assert server.kwargs["port"] == 41931
    assert server.kwargs["streamable_http_path"] == "/manager-mcp"
    assert server.kwargs["json_response"] is True
    assert server.kwargs["stateless_http"] is True
    assert "agent_brief" not in server.kwargs["instructions"]
    assert "customer cases" in server.kwargs["instructions"]


def test_main_runs_streamable_http_server(monkeypatch):
    server = _FakeFastMCP()
    monkeypatch.setattr(mcp_server, "build_server", lambda: server)

    mcp_server.main()

    assert server.run_calls == [{"transport": "streamable-http"}]


def test_environment_file_is_loaded_before_transport_and_store_registration(monkeypatch, tmp_path):
    # The loader writes directly to environ; discard all of its synthetic values
    # at teardown, including keys that were absent before monkeypatch.delenv.
    monkeypatch.setattr(config.os, "environ", dict(config.os.environ))
    env_file = tmp_path / "manager.env"
    env_file.write_text(
        "AUTOSTOP_MANAGER_MCP_PORT=41939\n"
        "AUTOSTOP_STORE_API_URL=http://127.0.0.1:18010\n"
        "AUTOSTOP_STORE_READ_TOKEN=synthetic-read-token\n"
    )
    for name in ("AUTOSTOP_MANAGER_MCP_PORT", "AUTOSTOP_STORE_API_URL", "AUTOSTOP_STORE_READ_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", str(env_file))
    monkeypatch.setattr(config, "_ENV_LOADED", False)
    monkeypatch.setattr(mcp_server, "FastMCP", _FakeFastMCP)
    monkeypatch.setattr(mcp_server, "assert_manager_mcp_surface", lambda server: None)
    observed = []
    monkeypatch.setattr(
        mcp_server,
        "register_manager_tools",
        lambda server: observed.append((config.get_store_api_url(), config.get_store_read_token())),
    )
    server = mcp_server.build_server()
    assert observed == [("http://127.0.0.1:18010/internal/agent/v1", "synthetic-read-token")]
    assert server.kwargs["port"] == 41939
