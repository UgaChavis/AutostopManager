from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "autostop_manager.sqlite3"
STORE_AGENT_API_PREFIX = "/internal/agent/v1"
_ENV_LOADED = False


def _strip_env_value(value: str) -> str:
    clean = value.strip()
    if len(clean) >= 2 and clean[0] == clean[-1] and clean[0] in {"'", '"'}:
        return clean[1:-1]
    return clean


def _iter_runtime_env_files() -> list[Path]:
    configured = os.environ.get("AUTOSTOP_MANAGER_ENV_FILE")
    if configured:
        return [Path(item.strip()).expanduser() for item in configured.split(os.pathsep) if item.strip()]
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
    return os.environ.get("AUTOSTOP_MANAGER_MCP_HOST", "127.0.0.1")


def get_mcp_port() -> int:
    return int(os.environ.get("AUTOSTOP_MANAGER_MCP_PORT", "41931"))


def get_mcp_path() -> str:
    path = os.environ.get("AUTOSTOP_MANAGER_MCP_PATH", "/mcp")
    return path if path.startswith("/") else f"/{path}"


def get_store_api_url() -> str:
    """Return the configured internal store API root without credentials."""

    configured = os.environ.get("AUTOSTOP_STORE_API_URL", "").strip().rstrip("/")
    if not configured:
        return ""
    candidate = configured if configured.endswith(STORE_AGENT_API_PREFIX) else f"{configured}{STORE_AGENT_API_PREFIX}"
    try:
        parsed = urlsplit(candidate)
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return ""
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return ""
    if parsed.path.rstrip("/") != STORE_AGENT_API_PREFIX:
        return ""
    if hostname == "autostop24.shop":
        return candidate if parsed.scheme == "https" and port in {None, 443} else ""
    if hostname in {"127.0.0.1", "localhost", "::1"} and parsed.scheme in {"http", "https"}:
        return candidate
    return ""


def get_store_read_token() -> str:
    """Return the runtime-only store aggregate-read credential."""

    return os.environ.get("AUTOSTOP_STORE_READ_TOKEN", "").strip()
