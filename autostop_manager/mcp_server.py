from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .config import get_mcp_host, get_mcp_path, get_mcp_port
from .mcp_contract import assert_manager_mcp_surface
from .mcp_tools import register_manager_tools


def build_server() -> FastMCP:
    server = FastMCP(
        name="AutostopManager",
        instructions=(
            "Tools for AutoStop customer cases, Store operations and parts research. Drive "
            "the task to an outcome. CRM, Store, Gmail and Telegram own their current data; Manager coordinates them."
        ),
        host=get_mcp_host(),
        port=get_mcp_port(),
        streamable_http_path=get_mcp_path(),
        json_response=True,
        stateless_http=True,
        log_level="WARNING",
    )
    register_manager_tools(server)
    # The native endpoint must never advertise a stale subset or a schema that
    # differs from the reviewed manifest.  Fail before opening the listener.
    assert_manager_mcp_surface(server)
    return server


def main() -> None:
    build_server().run(transport="streamable-http")


if __name__ == "__main__":
    main()
