from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .storage import _now


CRM_ROOT = Path("/opt/autostopcrm")
DEFAULT_ENV_PATHS = (
    PROJECT_ROOT / ".env",
    CRM_ROOT / ".env",
)
READINESS_QUERY = "AutoStop web search readiness"
TIMEOUT_SECONDS = 8.0
MAX_QUERY_CHARS = 240
MAX_RESPONSE_BYTES = 500_000
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 50_000


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, code, _msg, headers, _newurl):  # noqa: ANN001
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


@dataclass(frozen=True)
class WebSearchProvider:
    provider: str
    label: str
    role: str
    env_groups: tuple[tuple[str, ...], ...]
    access_mode: str
    docs_url: str
    capabilities: tuple[str, ...]


WEB_SEARCH_PROVIDERS: tuple[WebSearchProvider, ...] = (
    WebSearchProvider(
        provider="searxng",
        label="SearXNG local metasearch",
        role="self_hosted_search_api",
        env_groups=(("AUTOSTOP_SEARXNG_BASE_URL", "SEARXNG_BASE_URL"),),
        access_mode="local_http_json",
        docs_url="https://docs.searxng.org/dev/search_api.html",
        capabilities=("self_hosted_metasearch", "json_search_results", "no_api_key_required"),
    ),
    WebSearchProvider(
        provider="marginalia",
        label="Marginalia Search API",
        role="public_free_search_api",
        env_groups=(("AUTOSTOP_MARGINALIA_ENABLED", "AUTOSTOP_MARGINALIA_API_KEY", "MARGINALIA_API_KEY"),),
        access_mode="public_api_key_optional",
        docs_url="https://about.marginalia-search.com/article/api/",
        capabilities=("independent_niche_index", "json_search_results", "public_key_available"),
    ),
    WebSearchProvider(
        provider="brave",
        label="Brave Search API",
        role="primary_search_api",
        env_groups=(("BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY"),),
        access_mode="api_key",
        docs_url="https://brave.com/search/api/",
        capabilities=("independent_web_index", "snippets", "fresh_web_results"),
    ),
    WebSearchProvider(
        provider="tavily",
        label="Tavily Search API",
        role="agent_search_api",
        env_groups=(("TAVILY_API_KEY",),),
        access_mode="api_key",
        docs_url="https://docs.tavily.com/",
        capabilities=("agent_search", "rag_search", "content_snippets"),
    ),
    WebSearchProvider(
        provider="google_cse",
        label="Google Custom Search JSON API",
        role="fallback_search_api",
        env_groups=(
            ("GOOGLE_CUSTOM_SEARCH_API_KEY", "GOOGLE_CSE_API_KEY"),
            ("GOOGLE_CUSTOM_SEARCH_CX", "GOOGLE_CSE_CX", "GOOGLE_CSE_ID"),
        ),
        access_mode="api_key_and_cx",
        docs_url="https://developers.google.com/custom-search/v1/overview",
        capabilities=("programmable_search", "json_search_results"),
    ),
    WebSearchProvider(
        provider="duckduckgo",
        label="DuckDuckGo HTML",
        role="last_resort_fallback",
        env_groups=(),
        access_mode="public_html",
        docs_url="https://html.duckduckgo.com/html/",
        capabilities=("public_html_fallback", "no_api_key_required"),
    ),
)


def build_web_search_readiness(
    *,
    provider: str = "all",
    mode: str = "dry-run",
    env_paths: tuple[Path | str, ...] = DEFAULT_ENV_PATHS,
    query: str = READINESS_QUERY,
) -> dict[str, Any]:
    if mode not in {"dry-run", "live-readonly"}:
        return {"ok": False, "error": "mode must be dry-run or live-readonly", "mode": mode}
    if not str(query or "").strip() or len(str(query)) > MAX_QUERY_CHARS:
        return {
            "ok": False,
            "error": f"query must contain 1..{MAX_QUERY_CHARS} characters",
            "mode": mode,
        }

    env = _load_env_layers(env_paths)
    disabled = _disabled_search_providers(env)
    providers = list(WEB_SEARCH_PROVIDERS)
    if provider != "all":
        providers = [item for item in providers if item.provider == provider]
        if not providers:
            return {"ok": False, "error": "unknown provider", "provider": provider}

    results = [_provider_result(item, env=env, mode=mode, query=query, disabled=disabled) for item in providers]
    quality_results = [item for item in results if item["role"] != "last_resort_fallback" and not item.get("disabled")]
    configured_quality = [item for item in quality_results if item["configured"]]
    live_ready_quality = [item for item in quality_results if item["live_readonly_status"] == "ready"]

    return {
        "ok": True,
        "schema": "WebSearchReadinessV1",
        "generated_at": _now(),
        "provider": provider,
        "mode": mode,
        "results": results,
        "summary": {
            "provider_count": len(results),
            "quality_provider_count": len(quality_results),
            "quality_configured_count": len(configured_quality),
            "quality_live_readonly_ready_count": len(live_ready_quality),
            "fallback_available": any(item["provider"] == "duckduckgo" and item["configured"] for item in results),
            "fallback_only": not configured_quality,
            "missing_quality_provider_count": len(quality_results) - len(configured_quality),
            "disabled_provider_count": sum(1 for item in results if item.get("disabled")),
            "secrets_redacted": True,
        },
        "safety": {
            "read_only": True,
            "orders_blocked": True,
            "crm_writes_blocked": True,
            "secrets_redacted": True,
            "captcha_login_paywall_bypass": False,
        },
        "env_sources": [{"path": str(path), "present": Path(path).exists()} for path in env_paths],
    }


def _provider_result(
    provider: WebSearchProvider,
    *,
    env: dict[str, str],
    mode: str,
    query: str,
    disabled: set[str],
) -> dict[str, Any]:
    is_disabled = provider.provider in disabled
    missing_groups = _missing_env_groups(provider, env)
    configured = not is_disabled and not missing_groups
    live_status = "not_requested"
    latency_ms: int | None = None
    warnings: list[str] = []
    error = ""

    if is_disabled:
        live_status = "disabled"
        warnings.append("provider_disabled_by_env")
    elif mode == "live-readonly":
        if not configured:
            live_status = "missing_env"
        else:
            live_status, latency_ms, error = _live_readonly_check(provider, env=env, query=query)
            if error:
                warnings.append("live_readonly_probe_failed")

    return {
        "provider": provider.provider,
        "label": provider.label,
        "role": provider.role,
        "mode": mode,
        "configured": configured,
        "disabled": is_disabled,
        "missing_env_groups": missing_groups,
        "missing_env_names": sorted({name for group in missing_groups for name in group}),
        "dry_run_payload_valid": bool(provider.provider and provider.label),
        "live_readonly_status": live_status,
        "latency_ms": latency_ms,
        "redaction_check": True,
        "access_mode": provider.access_mode,
        "docs_url": provider.docs_url,
        "capabilities": list(provider.capabilities),
        "warnings": warnings,
        "error": error,
    }


def _missing_env_groups(
    provider: WebSearchProvider,
    env: dict[str, str],
) -> list[list[str]]:
    missing: list[list[str]] = []
    for group in provider.env_groups:
        if not any(str(env.get(name) or "").strip() for name in group):
            missing.append(list(group))
    return missing


def _live_readonly_check(
    provider: WebSearchProvider,
    *,
    env: dict[str, str],
    query: str,
) -> tuple[str, int | None, str]:
    import time

    started = time.monotonic()
    try:
        if provider.provider == "searxng":
            _probe_searxng(env, query=query)
        elif provider.provider == "marginalia":
            _probe_marginalia(env, query=query)
        elif provider.provider == "brave":
            _probe_brave(env, query=query)
        elif provider.provider == "tavily":
            _probe_tavily(env, query=query)
        elif provider.provider == "google_cse":
            _probe_google_cse(env, query=query)
        elif provider.provider == "duckduckgo":
            _probe_duckduckgo(query=query)
        else:
            return "unknown_provider", None, "unknown provider"
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as exc:
        return "error", None, _safe_error_message(exc)
    latency_ms = int((time.monotonic() - started) * 1000)
    return "ready", latency_ms, ""


def _probe_searxng(env: dict[str, str], *, query: str) -> None:
    base_url = _first_env(env, "AUTOSTOP_SEARXNG_BASE_URL", "SEARXNG_BASE_URL").rstrip("/")
    endpoint = base_url if base_url.endswith("/search") else f"{base_url}/search"
    params = urllib.parse.urlencode({"q": query, "format": "json", "categories": "general", "safesearch": "0"})
    request = urllib.request.Request(
        f"{endpoint}?{params}",
        headers={"User-Agent": "Mozilla/5.0 AutoStopManager/1.0"},
    )
    payload = _read_json_response(request)
    if not isinstance(payload.get("results"), list):
        raise ValueError("response did not include JSON results")


def _probe_marginalia(env: dict[str, str], *, query: str) -> None:
    api_key = _first_env(env, "AUTOSTOP_MARGINALIA_API_KEY", "MARGINALIA_API_KEY") or "public"
    params = urllib.parse.urlencode({"query": query, "count": "1", "nsfw": "1"})
    request = urllib.request.Request(
        f"https://api2.marginalia-search.com/search?{params}",
        headers={"API-Key": api_key, "User-Agent": "Mozilla/5.0 AutoStopManager/1.0"},
    )
    payload = _read_json_response(request)
    if not isinstance(payload.get("results"), list):
        raise ValueError("response did not include JSON results")


def _probe_brave(env: dict[str, str], *, query: str) -> None:
    api_key = _first_env(env, "BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY")
    params = urllib.parse.urlencode({"q": query, "count": "1", "result_filter": "web"})
    request = urllib.request.Request(
        f"https://api.search.brave.com/res/v1/web/search?{params}",
        headers={"Accept": "application/json", "X-Subscription-Token": api_key},
    )
    _read_json_response(request)


def _probe_tavily(env: dict[str, str], *, query: str) -> None:
    api_key = _first_env(env, "TAVILY_API_KEY")
    body = json.dumps(
        {
            "query": query,
            "search_depth": "basic",
            "max_results": 1,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://api.tavily.com/search",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    _read_json_response(request)


def _probe_google_cse(env: dict[str, str], *, query: str) -> None:
    api_key = _first_env(env, "GOOGLE_CUSTOM_SEARCH_API_KEY", "GOOGLE_CSE_API_KEY")
    cx = _first_env(env, "GOOGLE_CUSTOM_SEARCH_CX", "GOOGLE_CSE_CX", "GOOGLE_CSE_ID")
    params = urllib.parse.urlencode({"key": api_key, "cx": cx, "q": query, "num": "1", "safe": "active"})
    request = urllib.request.Request(f"https://www.googleapis.com/customsearch/v1?{params}")
    _read_json_response(request)


def _probe_duckduckgo(*, query: str) -> None:
    params = urllib.parse.urlencode({"q": query})
    request = urllib.request.Request(
        f"https://html.duckduckgo.com/html/?{params}",
        headers={"User-Agent": "Mozilla/5.0 AutoStopManager/1.0"},
    )
    _read_text_response(request)


def _read_json_response(request: urllib.request.Request) -> dict[str, Any]:
    text = _read_text_response(request)
    payload = json.loads(text, parse_constant=_reject_json_constant)
    if not isinstance(payload, dict):
        raise ValueError("response was not a JSON object")
    _validate_json_shape(payload)
    return payload


def _read_text_response(request: urllib.request.Request) -> str:
    with _NO_REDIRECT_OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("response exceeded maximum size")
    return raw.decode("utf-8", errors="replace")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")


def _validate_json_shape(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > MAX_JSON_DEPTH:
            raise ValueError("response JSON is too deeply nested")
        if nodes > MAX_JSON_NODES:
            raise ValueError("response JSON contains too many values")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)


def _load_env_layers(paths: tuple[Path | str, ...]) -> dict[str, str]:
    result = dict(os.environ)
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            continue
        result.update(_parse_env_file(path))
    return result


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = raw_value.strip().strip('"').strip("'")
    return values


def _first_env(env: dict[str, str], *names: str) -> str:
    for name in names:
        value = str(env.get(name) or "").strip()
        if value:
            return value
    return ""


def _disabled_search_providers(env: dict[str, str]) -> set[str]:
    aliases = {
        "brave_search": "brave",
        "brave": "brave",
        "tavily": "tavily",
        "google": "google_cse",
        "google_cse": "google_cse",
        "google_custom_search": "google_cse",
        "searx": "searxng",
        "searxng": "searxng",
        "searx_ng": "searxng",
        "marginalia": "marginalia",
        "marginalia_search": "marginalia",
        "duckduckgo": "duckduckgo",
        "ddg": "duckduckgo",
    }
    raw = _first_env(env, "AUTOSTOP_SEARCH_DISABLED_PROVIDERS")
    disabled: set[str] = set()
    for item in raw.replace(",", " ").split():
        provider = aliases.get(item.strip().casefold(), "")
        if provider:
            disabled.add(provider)
    return disabled


def _safe_error_message(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return exc.__class__.__name__
    return " ".join(str(exc or "").split())[:160]
