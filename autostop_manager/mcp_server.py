from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .config import get_mcp_host, get_mcp_path, get_mcp_port
from .mcp_tools import register_manager_memory_tools


def build_server() -> FastMCP:
    server = FastMCP(
        name="AutostopManager",
        instructions=(
            "Headless manager memory for AutoStop CRM. "
            "Use these tools only for long-term manager memory. "
            "Use the existing AutoStop CRM MCP tools for cards, clients, vehicles, repair orders, cashboxes, and board state."
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
