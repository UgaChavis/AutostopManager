from __future__ import annotations

import os
import ipaddress
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "autostop_manager.sqlite3"
_ENV_LOADED = False


def _strip_env_value(value: str) -> str:
    clean = value.strip()
    if len(clean) >= 2 and clean[0] == clean[-1] and clean[0] in {"'", '"'}:
        return clean[1:-1]
    return clean


def _iter_runtime_env_files() -> list[Path]:
    configured = os.environ.get("AUTOSTOP_MANAGER_ENV_FILE")
    if configured:
        return [Path(item).expanduser() for item in configured.split(os.pathsep) if item.strip()]
    return [PROJECT_ROOT / ".env", PROJECT_ROOT / ".env.local"]


def load_runtime_env(*, force: bool = False) -> list[Path]:
    """Load local manager .env files without overriding real environment vars."""

    global _ENV_LOADED
    if _ENV_LOADED and not force:
        return []

    loaded: list[Path] = []
    for path in _iter_runtime_env_files():
        if not path.exists() or not path.is_file():
            continue
        loaded.append(path)
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
                continue
            os.environ.setdefault(key, _strip_env_value(value))

    _ENV_LOADED = True
    return loaded


def get_db_path() -> Path:
    configured = os.environ.get("AUTOSTOP_MANAGER_DB")
    if configured:
        return Path(configured).expanduser().resolve()
    return DEFAULT_DB_PATH


def get_mcp_host() -> str:
    host = os.environ.get("AUTOSTOP_MANAGER_MCP_HOST", "127.0.0.1").strip()
    if host.casefold() == "localhost":
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("AUTOSTOP_MANAGER_MCP_HOST must be a loopback IP address or localhost") from exc
    if not address.is_loopback:
        raise ValueError(
            "AutoStop Manager MCP has no built-in authentication and must bind to loopback; "
            "publish it only through an authenticated reverse proxy"
        )
    return host


def get_mcp_port() -> int:
    return int(os.environ.get("AUTOSTOP_MANAGER_MCP_PORT", "41931"))


def get_mcp_path() -> str:
    path = os.environ.get("AUTOSTOP_MANAGER_MCP_PATH", "/mcp")
    return path if path.startswith("/") else f"/{path}"
