from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "autostop_manager.sqlite3"
STORE_AGENT_API_PREFIX = "/internal/agent/v1"
CRM_MCP_URL_ENV = "AUTOSTOP_CRM_MCP_URL"
CRM_MCP_BEARER_TOKEN_ENV = "AUTOSTOP_CRM_MCP_BEARER_TOKEN"
_ENV_LOADED = False


@dataclass(frozen=True)
class CrmMcpConnectionConfig:
    """One optional Manager-owned, loopback-only CRM MCP connection."""

    configured: bool
    url: str = ""
    bearer_token: str = field(default="", repr=False)
    error_code: str | None = None


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
    configured = os.environ.get("AUTOSTOP_MANAGER_MCP_HOST", "127.0.0.1").strip()
    try:
        address = ipaddress.ip_address(configured)
    except ValueError as exc:
        raise ValueError("mcp_host_invalid") from exc
    if not address.is_loopback:
        raise ValueError("mcp_host_not_loopback")
    return str(address)


def get_mcp_port() -> int:
    return int(os.environ.get("AUTOSTOP_MANAGER_MCP_PORT", "41931"))


def get_mcp_path() -> str:
    path = os.environ.get("AUTOSTOP_MANAGER_MCP_PATH", "/mcp")
    return path if path.startswith("/") else f"/{path}"


def normalize_crm_mcp_url(value: str) -> str:
    """Normalize a literal-loopback CRM MCP endpoint or fail closed.

    Manager must not resolve a hostname or follow a redirect while carrying the
    CRM bearer.  The source endpoint therefore has to be a plain HTTP URL with
    a literal loopback address and a deliberate MCP path.
    """

    configured = str(value or "").strip()
    if not configured:
        return ""
    try:
        parsed = urlsplit(configured)
        host = parsed.hostname or ""
        port = parsed.port
        address = ipaddress.ip_address(host)
    except ValueError:
        return ""
    if (
        parsed.scheme != "http"
        or not parsed.netloc
        or not address.is_loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"/mcp", "/mcp/"}
        or port != 8001
    ):
        return ""
    authority = f"[{address.compressed}]" if address.version == 6 else address.compressed
    if port is not None:
        authority = f"{authority}:{port}"
    return urlunsplit(("http", authority, "/mcp", "", ""))


def _normalize_crm_mcp_bearer(value: str) -> str:
    token = str(value or "").strip()
    if not token or len(token) > 512 or any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in token):
        return ""
    return token


def get_crm_mcp_connection_config() -> CrmMcpConnectionConfig:
    """Read the optional E8 route from Manager's runtime environment only."""

    raw_url = os.environ.get(CRM_MCP_URL_ENV, "")
    raw_bearer = os.environ.get(CRM_MCP_BEARER_TOKEN_ENV, "")
    configured = bool(str(raw_url or "").strip() or str(raw_bearer or "").strip())
    if not configured:
        return CrmMcpConnectionConfig(configured=False)
    url = normalize_crm_mcp_url(raw_url)
    bearer_token = _normalize_crm_mcp_bearer(raw_bearer)
    if not url or not bearer_token:
        return CrmMcpConnectionConfig(configured=True, error_code="crm_mcp_configuration_invalid")
    return CrmMcpConnectionConfig(configured=True, url=url, bearer_token=bearer_token)


def get_store_api_url() -> str:
    """Return only an allowlisted internal Store Agent root."""

    return normalize_store_api_url(os.environ.get("AUTOSTOP_STORE_API_URL", ""))


def normalize_store_api_url(value: str) -> str:
    """Normalize one internal/loopback Store Agent URL or fail closed."""

    configured = str(value or "").strip()
    if not configured:
        return ""
    try:
        parsed = urlsplit(configured)
        host = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme != "http"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/", STORE_AGENT_API_PREFIX, f"{STORE_AGENT_API_PREFIX}/"}
    ):
        return ""

    production_target = host == "autostop-app" and port == 8000
    loopback_target = port is not None and _is_loopback_store_host(host)
    if not production_target and not loopback_target:
        return ""
    return f"http://{parsed.netloc}{STORE_AGENT_API_PREFIX}"


def _is_loopback_store_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def get_store_read_token() -> str:
    """Return the runtime-only store read token."""

    return os.environ.get("AUTOSTOP_STORE_READ_TOKEN", "").strip()


def get_store_manage_token() -> str:
    """Return the runtime-only store management token."""

    return os.environ.get("AUTOSTOP_STORE_MANAGE_TOKEN", "").strip()


def get_store_owner_token() -> str:
    """Return the runtime-only owner service-principal token."""

    return os.environ.get("AUTOSTOP_STORE_OWNER_TOKEN", "").strip()


def get_store_quote_token() -> str:
    """Return the runtime-only exact-quote and sourcing token."""

    return os.environ.get("AUTOSTOP_STORE_QUOTE_TOKEN", "").strip()
