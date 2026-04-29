from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "autostop_manager.sqlite3"


def get_db_path() -> Path:
    configured = os.environ.get("AUTOSTOP_MANAGER_DB")
    if configured:
        return Path(configured).expanduser().resolve()
    return DEFAULT_DB_PATH


def get_mcp_host() -> str:
    return os.environ.get("AUTOSTOP_MANAGER_MCP_HOST", "127.0.0.1")


def get_mcp_port() -> int:
    return int(os.environ.get("AUTOSTOP_MANAGER_MCP_PORT", "41931"))


def get_mcp_path() -> str:
    path = os.environ.get("AUTOSTOP_MANAGER_MCP_PATH", "/mcp")
    return path if path.startswith("/") else f"/{path}"
