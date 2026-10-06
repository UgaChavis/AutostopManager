#!/usr/bin/env python3
"""Read-only registry/schema/invocation/link/coverage check using disposable state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    from mcp.server.fastmcp import FastMCP
    from autostop_manager.automotive_catalog import validate_registry
    from autostop_manager.catalog_clients import PARTSAPI_OPERATIONS
    from autostop_manager.mcp_contract import assert_manager_mcp_surface
    from autostop_manager.mcp_tools import register_manager_tools

    with tempfile.TemporaryDirectory(prefix="automotive-catalog-") as temp:
        os.environ["AUTOSTOP_MANAGER_ENV_FILE"] = os.devnull
        os.environ["AUTOSTOP_MANAGER_DB"] = str(Path(temp) / "schema.sqlite3")
        server = FastMCP("catalog-check")
        register_manager_tools(server)
        schemas = {name: tool.parameters for name, tool in server._tool_manager._tools.items()}
        registry = json.loads((ROOT / "docs/agent/automotive_tools.json").read_text())
        validation = validate_registry(ROOT, registry, schemas, PARTSAPI_OPERATIONS)
        validation["mcp"] = assert_manager_mcp_surface(server)
        print(json.dumps(validation, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
