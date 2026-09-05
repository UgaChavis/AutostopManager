from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .config import get_mcp_host, get_mcp_path, get_mcp_port
from .mcp_tools import register_manager_memory_tools


def build_server() -> FastMCP:
    server = FastMCP(
        name="AutostopManager",
        instructions=(
            "Tools for an AutoStop director. Treat agent_brief as guidance, combine useful capabilities, and drive "
            "the task to an outcome. CRM, Store, Gmail and Telegram own their current data; Manager coordinates them."
        ),
        host=get_mcp_host(),
        port=get_mcp_port(),
        streamable_http_path=get_mcp_path(),
        json_response=True,
        stateless_http=True,
        log_level="WARNING",
    )
    register_manager_memory_tools(server)
    return server


def main() -> None:
    build_server().run(transport="streamable-http")


if __name__ == "__main__":
    main()
