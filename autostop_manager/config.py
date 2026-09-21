from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "autostop_manager.sqlite3"
DEFAULT_AUTOMATION_DB_PATH = Path("/var/lib/autostop-manager-scheduler/registry.sqlite3")
STORE_AGENT_API_PREFIX = "/internal/agent/v1"
CRM_MCP_URL_ENV = "AUTOSTOP_CRM_MCP_URL"
CRM_MCP_BEARER_TOKEN_ENV = "AUTOSTOP_CRM_MCP_BEARER_TOKEN"
AUTOMATION_CONTROL_SOCKET_ENV = "AUTOSTOP_AUTOMATION_CONTROL_SOCKET"
DEFAULT_AUTOMATION_CONTROL_SOCKET = Path("/run/autostop-manager-automation/control.sock")
_ENV_LOADED = False
_TECHNICAL_VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,79}\Z")


@dataclass(frozen=True)
class CrmMcpConnectionConfig:
    """One optional Manager-owned, loopback-only CRM MCP connection."""

    configured: bool
    url: str = ""
    bearer_token: str = field(default="", repr=False)
    error_code: str | None = None


@dataclass(frozen=True)
class AutomationCrmConnectionConfig:
    """One loopback CRM HTTP route for the durable change feed."""

    configured: bool
    api_url: str = ""
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


def get_automation_db_path() -> Path:
    configured = os.environ.get("AUTOSTOP_AUTOMATION_DB")
    path = Path(configured).expanduser() if configured else DEFAULT_AUTOMATION_DB_PATH
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("automation_db_path_invalid")
    return path.resolve()


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


def get_automation_control_socket_path() -> Path:
    configured = os.environ.get(AUTOMATION_CONTROL_SOCKET_ENV, str(DEFAULT_AUTOMATION_CONTROL_SOCKET)).strip()
    path = Path(configured)
    if not path.is_absolute() or ".." in path.parts or len(str(path)) > 100:
        raise ValueError("automation_control_socket_invalid")
    return path


def get_automation_control_allowed_uids() -> frozenset[int]:
    configured = os.environ.get("AUTOSTOP_AUTOMATION_CONTROL_ALLOWED_UIDS", "0")
    try:
        values = frozenset(int(item.strip()) for item in configured.split(",") if item.strip())
    except ValueError as exc:
        raise ValueError("automation_control_allowed_uids_invalid") from exc
    if not values or any(value < 0 for value in values):
        raise ValueError("automation_control_allowed_uids_invalid")
    return values


def get_automation_control_peer_roles() -> dict[int, frozenset[str]]:
    configured = os.environ.get("AUTOSTOP_AUTOMATION_CONTROL_PEERS", "0:codex|system")
    allowed_roles = {"codex", "system", "crm_operator", "telegram_owner"}
    result: dict[int, frozenset[str]] = {}
    try:
        for raw_entry in configured.split(","):
            entry = raw_entry.strip()
            if not entry:
                continue
            raw_uid, separator, raw_roles = entry.partition(":")
            if not separator:
                raise ValueError
            uid = int(raw_uid)
            roles = frozenset(role.strip() for role in raw_roles.split("|") if role.strip())
            if uid < 0 or not roles or not roles.issubset(allowed_roles) or uid in result:
                raise ValueError
            result[uid] = roles
    except ValueError as exc:
        raise ValueError("automation_control_peers_invalid") from exc
    if not result:
        raise ValueError("automation_control_peers_invalid")
    return result


def get_automation_control_gid() -> int | None:
    configured = os.environ.get("AUTOSTOP_AUTOMATION_CONTROL_GID", "").strip()
    if not configured:
        return None
    try:
        gid = int(configured)
    except ValueError as exc:
        raise ValueError("automation_control_gid_invalid") from exc
    if gid < 0:
        raise ValueError("automation_control_gid_invalid")
    return gid


def get_automation_runtime_identity() -> dict[str, str]:
    """Return bounded, non-secret build identities for readiness packets."""

    result: dict[str, str] = {}
    for output_key, environment_key in (
        ("manager_revision", "AUTOSTOP_MANAGER_REVISION"),
        ("crm_version", "AUTOSTOP_AUTOMATION_CRM_VERSION"),
        ("crm_revision", "AUTOSTOP_AUTOMATION_CRM_REVISION"),
    ):
        value = os.environ.get(environment_key, "").strip()
        result[output_key] = value if _TECHNICAL_VERSION_PATTERN.fullmatch(value) else "unknown"
    return result


def normalize_automation_crm_api_url(value: str) -> str:
    configured = str(value or "").strip()
    if not configured:
        return ""
    try:
        parsed = urlsplit(configured)
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme != "http"
        or not address.is_loopback
        or parsed.username is not None
        or parsed.password is not None
        or port != 8000
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return ""
    authority = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return urlunsplit(("http", f"{authority}:8000", "", "", ""))


def get_automation_crm_connection_config() -> AutomationCrmConnectionConfig:
    raw_url = os.environ.get("AUTOSTOP_AUTOMATION_CRM_API_URL", "").strip()
    if not raw_url:
        raw_mcp_url = os.environ.get(CRM_MCP_URL_ENV, "").strip()
        normalized_mcp = normalize_crm_mcp_url(raw_mcp_url)
        if normalized_mcp:
            parsed = urlsplit(normalized_mcp)
            host = parsed.hostname or ""
            authority = f"[{host}]" if ":" in host else host
            raw_url = f"http://{authority}:8000"
    token = _normalize_crm_mcp_bearer(os.environ.get(CRM_MCP_BEARER_TOKEN_ENV, ""))
    api_url = normalize_automation_crm_api_url(raw_url)
    configured = bool(raw_url or token)
    if not configured:
        return AutomationCrmConnectionConfig(configured=False)
    if not api_url or not token:
        return AutomationCrmConnectionConfig(configured=True, error_code="automation_crm_configuration_invalid")
    return AutomationCrmConnectionConfig(configured=True, api_url=api_url, bearer_token=token)


def get_work_telegram_socket_path() -> Path:
    configured = os.environ.get(
        "AUTOSTOP_AUTOMATION_TELEGRAM_SOCKET", "/run/autostop-work-telegram/bridge.sock"
    ).strip()
    path = Path(configured)
    if not path.is_absolute() or ".." in path.parts or len(str(path)) > 100:
        raise ValueError("automation_telegram_socket_invalid")
    return path


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
    """Read the optional loopback CRM Gateway route from Manager's runtime environment."""

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
