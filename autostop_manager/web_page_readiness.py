from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .storage import _now
from .web_search_readiness import (
    DEFAULT_ENV_PATHS,
    _first_env,
    _load_env_layers,
    _read_json_response,
    _safe_error_message,
)


READINESS_URL = "https://example.com/"


@dataclass(frozen=True)
class WebPageExtractor:
    provider: str
    label: str
    role: str
    env_groups: tuple[tuple[str, ...], ...]
    access_mode: str
    docs_url: str
    capabilities: tuple[str, ...]


WEB_PAGE_EXTRACTORS: tuple[WebPageExtractor, ...] = (
    WebPageExtractor(
        provider="crawl4ai",
        label="Crawl4AI local markdown extractor",
        role="primary_page_extractor",
        env_groups=(
            ("AUTOSTOP_CRAWL4AI_BASE_URL", "CRAWL4AI_BASE_URL"),
            ("AUTOSTOP_CRAWL4AI_API_TOKEN", "CRAWL4AI_API_TOKEN"),
        ),
        access_mode="local_http_bearer_token",
        docs_url="https://docs.crawl4ai.com/core/self-hosting/",
        capabilities=("self_hosted_browser_crawler", "markdown_extraction", "public_pages_only"),
    ),
)


def build_web_page_readiness(
    *,
    provider: str = "all",
    mode: str = "dry-run",
    env_paths: tuple[Path | str, ...] = DEFAULT_ENV_PATHS,
    url: str = READINESS_URL,
) -> dict[str, Any]:
    if mode not in {"dry-run", "live-readonly"}:
        return {"ok": False, "error": "mode must be dry-run or live-readonly", "mode": mode}

    env = _load_env_layers(env_paths)
    extractors = list(WEB_PAGE_EXTRACTORS)
    if provider != "all":
        extractors = [item for item in extractors if item.provider == provider]
        if not extractors:
            return {"ok": False, "error": "unknown provider", "provider": provider}

    results = [_extractor_result(item, env=env, mode=mode, url=url) for item in extractors]
    configured = [item for item in results if item["configured"]]
    live_ready = [item for item in results if item["live_readonly_status"] == "ready"]

    return {
        "ok": True,
        "schema": "WebPageReadinessV1",
        "generated_at": _now(),
        "provider": provider,
        "mode": mode,
        "url": url,
        "results": results,
        "summary": {
            "extractor_count": len(results),
            "configured_count": len(configured),
            "live_readonly_ready_count": len(live_ready),
            "primary_configured": any(item["provider"] == "crawl4ai" and item["configured"] for item in results),
            "http_fallback_available": True,
            "browser_fallback_available": True,
            "secrets_redacted": True,
        },
        "safety": {
            "read_only": True,
            "public_urls_only": True,
            "crm_writes_blocked": True,
            "secrets_redacted": True,
            "captcha_login_paywall_bypass": False,
        },
        "env_sources": [{"path": str(path), "present": Path(path).exists()} for path in env_paths],
    }


def _extractor_result(
    extractor: WebPageExtractor,
    *,
    env: dict[str, str],
    mode: str,
    url: str,
) -> dict[str, Any]:
    missing_groups = _missing_env_groups(extractor, env)
    configured = not missing_groups
    live_status = "not_requested"
    latency_ms: int | None = None
    warnings: list[str] = []
    error = ""

    if mode == "live-readonly":
        if not configured:
            live_status = "missing_env"
        else:
            live_status, latency_ms, error = _live_readonly_check(
                extractor,
                env=env,
                url=url,
            )
            if error:
                warnings.append("live_readonly_probe_failed")

    return {
        "provider": extractor.provider,
        "label": extractor.label,
        "role": extractor.role,
        "mode": mode,
        "configured": configured,
        "missing_env_groups": missing_groups,
        "missing_env_names": sorted({name for group in missing_groups for name in group}),
        "dry_run_payload_valid": bool(extractor.provider and extractor.label),
        "live_readonly_status": live_status,
        "latency_ms": latency_ms,
        "redaction_check": True,
        "access_mode": extractor.access_mode,
        "docs_url": extractor.docs_url,
        "capabilities": list(extractor.capabilities),
        "warnings": warnings,
        "error": error,
    }


def _missing_env_groups(
    extractor: WebPageExtractor,
    env: dict[str, str],
) -> list[list[str]]:
    missing: list[list[str]] = []
    for group in extractor.env_groups:
        if not any(str(env.get(name) or "").strip() for name in group):
            missing.append(list(group))
    return missing


def _live_readonly_check(
    extractor: WebPageExtractor,
    *,
    env: dict[str, str],
    url: str,
) -> tuple[str, int | None, str]:
    import time

    started = time.monotonic()
    try:
        if extractor.provider == "crawl4ai":
            _probe_crawl4ai(env, url=url)
        else:
            return "unknown_provider", None, "unknown provider"
    except (
        OSError,
        urllib.error.URLError,
        urllib.error.HTTPError,
        ValueError,
        TimeoutError,
    ) as exc:
        return "error", None, _safe_error_message(exc)
    latency_ms = int((time.monotonic() - started) * 1000)
    return "ready", latency_ms, ""


def _probe_crawl4ai(env: dict[str, str], *, url: str) -> None:
    normalized_url = _validated_public_http_url(url)
    base_url = _first_env(env, "AUTOSTOP_CRAWL4AI_BASE_URL", "CRAWL4AI_BASE_URL").rstrip("/")
    token = _first_env(env, "AUTOSTOP_CRAWL4AI_API_TOKEN", "CRAWL4AI_API_TOKEN")
    endpoint = base_url if base_url.endswith("/md") else f"{base_url}/md"
    body = json.dumps({"url": normalized_url, "f": "fit", "cache": "0"}).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 AutoStopManager/1.0",
    }
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    payload = _read_json_response(request)
    if payload.get("success") is False:
        raise ValueError("Crawl4AI reported unsuccessful extraction")
    markdown = payload.get("markdown")
    if not str(markdown or "").strip():
        raise ValueError("Crawl4AI response did not include markdown")


def _validated_public_http_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(str(url or "").strip())
    except ValueError as exc:
        raise ValueError("Only public HTTP(S) URLs are supported") from exc
    scheme = str(parsed.scheme or "").casefold()
    host = str(parsed.hostname or "").strip().casefold().rstrip(".")
    if scheme not in {"http", "https"} or not parsed.netloc or not host or parsed.username or parsed.password:
        raise ValueError("Only public HTTP(S) URLs are supported")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (
            host == "localhost"
            or host.endswith((".local", ".localhost", ".internal", ".lan", ".home", ".test", ".invalid"))
            or "." not in host
        ):
            raise ValueError("Local or private URLs are not supported")
        addresses = _resolve_public_addresses(host, parsed.port)
    else:
        addresses = [address]
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Local or private URLs are not supported")
    return parsed.geturl()


def _resolve_public_addresses(host: str, port: int | None) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve every current A/AAAA target and fail closed on mixed/private DNS."""

    try:
        answers = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise ValueError("Public URL hostname could not be resolved safely") from exc
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for answer in answers[:16]:
        try:
            address = ipaddress.ip_address(str(answer[4][0]).split("%", 1)[0])
        except (IndexError, TypeError, ValueError) as exc:
            raise ValueError("Public URL hostname returned an invalid address") from exc
        if address not in addresses:
            addresses.append(address)
    if len(answers) > 16:
        raise ValueError("Public URL hostname returned too many addresses")
    return addresses
