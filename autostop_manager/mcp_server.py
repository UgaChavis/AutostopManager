from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .config import get_mcp_host, get_mcp_path, get_mcp_port, load_runtime_env
from .crm_mcp_web_research import build_crm_mcp_web_research_gateway
from .mcp_contract import assert_manager_mcp_surface
from .mcp_tools import register_manager_tools
from .web_research_gateway import install_web_research_gateway


def build_server() -> FastMCP:
    load_runtime_env()
    # No CRM MCP configuration keeps the explicit bounded local fallback.  A
    # configured but broken route installs a structured-failure gateway instead.
    install_web_research_gateway(build_crm_mcp_web_research_gateway())
    server = FastMCP(
        name="AutostopManager",
        instructions=(
            "Tools for AutoStop customer cases, Store operations and parts research. Drive "
            "the task to an outcome. CRM, Store, Gmail and Telegram own their current data; Manager coordinates them. "
            "For E9 market research, use search_web_multi and fetch_page_excerpt or fetch_page_browser, "
            "then assess_part_market. Quick budget: 2 minutes, 6 searches, 12 pages; deep budget only "
            "when needed: 5 minutes, 20 searches, 30 pages. The result is a preliminary public market "
            "reference; do not write CRM or publish an F4 quote from E9 research."
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
