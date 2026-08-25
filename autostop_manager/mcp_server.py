from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .config import get_mcp_host, get_mcp_path, get_mcp_port
from .mcp_tools import register_manager_memory_tools


def build_server() -> FastMCP:
    server = FastMCP(
        name="AutostopManager",
        instructions=(
            "AutoStop routing, safe memory and workflow ledger. Start non-trivial work with agent_brief; "
            "follow the canonical Manager rules and named Gateway workflows. CRM, Store, Gmail and Telegram "
            "remain sources of truth for their own data; Manager keeps only safe rules, refs and compact lessons."
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
