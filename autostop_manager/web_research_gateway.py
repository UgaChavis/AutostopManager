"""Read-only Manager adapter for the shared E8 Web Research Gateway contract.

A composed, authenticated E8 client can be installed without changing the
public Manager MCP schema.  Until then the explicit local DuckDuckGo adapter
is a bounded fallback.  Both routes normalize to one compact V1 result and
remove VIN-like values before any provider invocation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import math
import re
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlparse

from .work_pricing_research import PUBLIC_RESEARCH_TIMEOUT_SECONDS, _ddg_search


WEB_RESEARCH_GATEWAY_SCHEMA = "WebResearchGatewayV1"
SEARCH_WEB_MULTI_CAPABILITY = "search_web_multi"
RESEARCH_PART_PUBLIC_EVIDENCE_CAPABILITY = "research_part_public_evidence"
_LOCAL_DDG_PROVIDER = "duckduckgo"
_DEFAULT_LIMIT = 5
_MAX_LIMIT = 10
_MAX_PART_EVIDENCE_LIMIT = 5
_MAX_PART_EVIDENCE_PAGES = 2
_MAX_TIMEOUT_SECONDS = 60
_VIN_LIKE_TOKEN = re.compile(r"(?<![A-HJ-NPR-Z0-9])(?:[A-HJ-NPR-Z0-9][ ._/\\-]?){17}(?![A-HJ-NPR-Z0-9])", re.I)
_SOURCE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,79}")
_SOURCE_TYPES = frozenset({"oem_catalog", "price_catalog"})


class WebResearchGateway(Protocol):
    """Minimal, read-only E8 capability surface used by Manager E7."""

    def search_web_multi(
        self,
        *,
        query: str,
        limit: int = _DEFAULT_LIMIT,
        allowed_domains: Sequence[str] | None = None,
        providers: Sequence[str] | None = None,
        timeout_seconds: float = PUBLIC_RESEARCH_TIMEOUT_SECONDS,
    ) -> dict[str, Any]: ...

    def research_part_public_evidence(
        self,
        *,
        query: str,
        limit: int = _DEFAULT_LIMIT,
        allowed_domains: Sequence[str] | None = None,
        providers: Sequence[str] | None = None,
        max_pages: int = 1,
        timeout_seconds: float = PUBLIC_RESEARCH_TIMEOUT_SECONDS,
    ) -> dict[str, Any]: ...


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(numeric) or not numeric.is_integer():
        return default
    return max(minimum, min(int(numeric), maximum))


def _bounded_timeout(value: Any) -> int:
    return _bounded_int(
        value,
        default=PUBLIC_RESEARCH_TIMEOUT_SECONDS,
        minimum=1,
        maximum=_MAX_TIMEOUT_SECONDS,
    )


def _compact(value: Any, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def redact_vin_like_text(value: Any, *, limit: int = 1000) -> tuple[str, int]:
    """Remove every full VIN-like token before it leaves Manager."""

    raw = _compact(unquote(str(value or "")), limit=1000)
    sanitized, replacements = _VIN_LIKE_TOKEN.subn(" ", raw)
    return _compact(sanitized, limit=limit), replacements


def _sanitize_query(value: Any) -> tuple[str, int]:
    return redact_vin_like_text(value)


def _normalize_domain(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw if "://" in raw else f"//{raw}")
    except ValueError:
        return ""
    return str(parsed.hostname or "").casefold().rstrip(".")


def _normalize_domains(values: Sequence[str] | None) -> list[str]:
    raw_values: Sequence[str] = [values] if isinstance(values, str) else (values or [])
    return sorted({domain for value in raw_values if (domain := _normalize_domain(value))})


def _normalize_providers(values: Sequence[str] | None) -> list[str]:
    raw_values: Sequence[str] = [values] if isinstance(values, str) else (values or [])
    return list(
        dict.fromkeys(_compact(value, limit=40).casefold() for value in raw_values if _compact(value, limit=40))
    )


def _result_hostname(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        if parsed.hostname and parsed.hostname.casefold() in {"duckduckgo.com", "www.duckduckgo.com"}:
            wrapped = parse_qs(parsed.query).get("uddg", [])
            if parsed.path.startswith("/l/") and len(wrapped) == 1:
                parsed = urlparse(unquote(wrapped[0]))
    except ValueError:
        return ""
    return str(parsed.hostname or "").casefold().rstrip(".")


def _domain_allowed(url: str, allowed_domains: Sequence[str]) -> bool:
    if not allowed_domains:
        return True
    host = _result_hostname(url)
    return bool(host) and any(host == domain or host.endswith("." + domain) for domain in allowed_domains)


def _compact_provider_attempts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    attempts: list[dict[str, Any]] = []
    for row in value[:_MAX_LIMIT]:
        if not isinstance(row, Mapping):
            continue
        provider = _compact(row.get("provider"), limit=40)
        status = _compact(row.get("status"), limit=40)
        if not provider and not status:
            continue
        attempt: dict[str, Any] = {"provider": provider or "unknown", "status": status or "unknown"}
        for field in ("result_count", "added_count"):
            if isinstance(row.get(field), int) and not isinstance(row.get(field), bool):
                attempt[field] = max(0, min(int(row[field]), _MAX_LIMIT))
        reason = _compact(row.get("reason"), limit=80)
        if reason:
            attempt["reason"] = reason
        attempts.append(attempt)
    return attempts


def _compact_results(raw_results: Any, *, allowed_domains: Sequence[str], limit: int) -> list[dict[str, Any]]:
    if not isinstance(raw_results, list):
        return []
    results: list[dict[str, Any]] = []
    for row in raw_results:
        if not isinstance(row, Mapping):
            continue
        url = _compact(row.get("url"), limit=2048)
        if not url or not _domain_allowed(url, allowed_domains):
            continue
        result: dict[str, Any] = {
            "title": _compact(row.get("title"), limit=140),
            "url": url,
            "snippet": _compact(row.get("snippet"), limit=240),
            "provider": _compact(row.get("provider"), limit=40) or _LOCAL_DDG_PROVIDER,
        }
        source = _compact(row.get("source") or row.get("domain") or _result_hostname(url), limit=100)
        if source:
            result["source"] = source
        domain = _compact(row.get("domain"), limit=253)
        if domain:
            result["domain"] = domain
        source_id = _compact(row.get("source_id"), limit=80)
        source_type = _compact(row.get("source_type"), limit=40).casefold()
        if row.get("source_authorized") is True and _SOURCE_ID.fullmatch(source_id) and source_type in _SOURCE_TYPES:
            result.update(
                {
                    "source_id": source_id,
                    "source_type": source_type,
                    "source_authorized": True,
                }
            )
        results.append(result)
        if len(results) >= limit:
            break
    return results


def _gateway_response(
    *,
    ok: bool,
    query: str,
    results: list[dict[str, Any]],
    allowed_domains: list[str],
    provider_order: list[str],
    providers: list[dict[str, Any]],
    fallback_used: bool,
    adapter: str,
    capability: str,
    vin_redacted: bool,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": ok,
        "schema": WEB_RESEARCH_GATEWAY_SCHEMA,
        "capability": capability,
        "adapter": adapter,
        "query": query,
        "results": results,
        "allowed_domains": allowed_domains,
        "provider_order": provider_order,
        "providers": providers,
        "fallback_used": fallback_used,
        "read_only": True,
        "vin_redacted": vin_redacted,
    }
    if error is not None:
        result["error"] = error
    return result


def _invalid_query_response(
    *,
    allowed_domains: list[str],
    provider_order: list[str],
    adapter: str,
    capability: str,
    vin_redacted: bool,
    fallback_used: bool,
) -> dict[str, Any]:
    return _gateway_response(
        ok=False,
        query="",
        results=[],
        allowed_domains=allowed_domains,
        provider_order=provider_order,
        providers=[],
        fallback_used=fallback_used,
        adapter=adapter,
        capability=capability,
        vin_redacted=vin_redacted,
        error={
            "code": "vin_like_query_rejected" if vin_redacted else "web_research_query_required",
            "retryable": False,
        },
    )


def normalize_web_research_response(
    payload: Any,
    *,
    query: str,
    limit: int,
    allowed_domains: Sequence[str] | None = None,
    adapter: str = "capability_adapter",
    capability: str = SEARCH_WEB_MULTI_CAPABILITY,
    vin_redacted: bool = False,
) -> dict[str, Any]:
    """Normalize plain E8 data or an ``invoke_web_research`` envelope to V1."""

    safe_query, removed = _sanitize_query(query)
    normalized_domains = _normalize_domains(allowed_domains)
    normalized_limit = _bounded_int(limit, default=_DEFAULT_LIMIT, minimum=1, maximum=_MAX_LIMIT)
    redacted = vin_redacted or bool(removed)
    envelope = payload if isinstance(payload, Mapping) else {}
    if envelope.get("ok") is False:
        raw_error = envelope.get("error")
        error = dict(raw_error) if isinstance(raw_error, Mapping) else {}
        return _gateway_response(
            ok=False,
            query=safe_query,
            results=[],
            allowed_domains=normalized_domains,
            provider_order=[],
            providers=[],
            fallback_used=bool(envelope.get("fallback_used")),
            adapter=adapter,
            capability=capability,
            vin_redacted=redacted,
            error={
                "code": _compact(error.get("code"), limit=80) or "web_research_gateway_failed",
                "retryable": bool(error.get("retryable", True)),
            },
        )
    data = envelope.get("data") if isinstance(envelope.get("data"), Mapping) else envelope
    if not isinstance(data, Mapping):
        return _gateway_response(
            ok=False,
            query=safe_query,
            results=[],
            allowed_domains=normalized_domains,
            provider_order=[],
            providers=[],
            fallback_used=False,
            adapter=adapter,
            capability=capability,
            vin_redacted=redacted,
            error={"code": "web_research_gateway_invalid_response", "retryable": False},
        )
    return _gateway_response(
        ok=True,
        query=safe_query,
        results=_compact_results(data.get("results"), allowed_domains=normalized_domains, limit=normalized_limit),
        allowed_domains=normalized_domains,
        provider_order=_normalize_providers(data.get("provider_order")),
        providers=_compact_provider_attempts(data.get("providers")),
        fallback_used=bool(data.get("fallback_used")),
        adapter=adapter,
        capability=capability,
        vin_redacted=redacted or bool(data.get("vin_redacted")),
    )


class CapabilityWebResearchGatewayAdapter:
    """Injectable client for E8 capabilities; it does not create a transport."""

    def __init__(self, invoke: Callable[[str, dict[str, Any]], Any]) -> None:
        self._invoke = invoke

    def _call(
        self,
        *,
        capability: str,
        query: str,
        limit: int,
        allowed_domains: Sequence[str] | None,
        providers: Sequence[str] | None,
        timeout_seconds: float,
        max_pages: int | None = None,
    ) -> dict[str, Any]:
        safe_query, removed = _sanitize_query(query)
        normalized_domains = _normalize_domains(allowed_domains)
        normalized_providers = _normalize_providers(providers)
        if not safe_query:
            return _invalid_query_response(
                allowed_domains=normalized_domains,
                provider_order=normalized_providers,
                adapter="capability_adapter",
                capability=capability,
                vin_redacted=bool(removed),
                fallback_used=False,
            )
        arguments: dict[str, Any] = {"query": safe_query, "limit": limit}
        if normalized_domains:
            arguments["allowed_domains"] = normalized_domains
        if normalized_providers:
            arguments["providers"] = normalized_providers
        if max_pages is not None:
            arguments["max_pages"] = max_pages
        _ = _bounded_timeout(timeout_seconds)  # Reserved for an authenticated transport implementation.
        try:
            payload = self._invoke(capability, arguments)
        except (OSError, TimeoutError, ValueError, RuntimeError):
            return _gateway_response(
                ok=False,
                query=safe_query,
                results=[],
                allowed_domains=normalized_domains,
                provider_order=normalized_providers,
                providers=[],
                fallback_used=False,
                adapter="capability_adapter",
                capability=capability,
                vin_redacted=bool(removed),
                error={"code": "web_research_transport_failed", "retryable": True},
            )
        return normalize_web_research_response(
            payload,
            query=safe_query,
            limit=limit,
            allowed_domains=normalized_domains,
            adapter="capability_adapter",
            capability=capability,
            vin_redacted=bool(removed),
        )

    def search_web_multi(
        self,
        *,
        query: str,
        limit: int = _DEFAULT_LIMIT,
        allowed_domains: Sequence[str] | None = None,
        providers: Sequence[str] | None = None,
        timeout_seconds: float = PUBLIC_RESEARCH_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        return self._call(
            capability=SEARCH_WEB_MULTI_CAPABILITY,
            query=query,
            limit=_bounded_int(limit, default=_DEFAULT_LIMIT, minimum=1, maximum=_MAX_LIMIT),
            allowed_domains=allowed_domains,
            providers=providers,
            timeout_seconds=timeout_seconds,
        )

    def research_part_public_evidence(
        self,
        *,
        query: str,
        limit: int = _DEFAULT_LIMIT,
        allowed_domains: Sequence[str] | None = None,
        providers: Sequence[str] | None = None,
        max_pages: int = 1,
        timeout_seconds: float = PUBLIC_RESEARCH_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        return self._call(
            capability=RESEARCH_PART_PUBLIC_EVIDENCE_CAPABILITY,
            query=query,
            limit=_bounded_int(limit, default=_DEFAULT_LIMIT, minimum=1, maximum=_MAX_PART_EVIDENCE_LIMIT),
            allowed_domains=allowed_domains,
            providers=providers,
            timeout_seconds=timeout_seconds,
            max_pages=_bounded_int(max_pages, default=1, minimum=0, maximum=_MAX_PART_EVIDENCE_PAGES),
        )


class DuckDuckGoWebResearchGateway:
    """Explicit local fallback while no authenticated E8 client is installed."""

    def __init__(self, search: Callable[..., dict[str, Any]] = _ddg_search) -> None:
        self._search = search

    def _call(
        self,
        *,
        capability: str,
        query: str,
        limit: int,
        allowed_domains: Sequence[str] | None,
        providers: Sequence[str] | None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        safe_query, removed = _sanitize_query(query)
        normalized_domains = _normalize_domains(allowed_domains)
        if not safe_query:
            return _invalid_query_response(
                allowed_domains=normalized_domains,
                provider_order=[_LOCAL_DDG_PROVIDER],
                adapter="local_duckduckgo_fallback",
                capability=capability,
                vin_redacted=bool(removed),
                fallback_used=True,
            )
        _ = _normalize_providers(providers)
        try:
            payload = self._search(safe_query, timeout_seconds=_bounded_timeout(timeout_seconds))
        except (OSError, TimeoutError, ValueError):
            return _gateway_response(
                ok=False,
                query=safe_query,
                results=[],
                allowed_domains=normalized_domains,
                provider_order=[_LOCAL_DDG_PROVIDER],
                providers=[{"provider": _LOCAL_DDG_PROVIDER, "status": "failed"}],
                fallback_used=True,
                adapter="local_duckduckgo_fallback",
                capability=capability,
                vin_redacted=bool(removed),
                error={"code": "web_search_provider_failed", "retryable": True},
            )
        result = normalize_web_research_response(
            payload,
            query=safe_query,
            limit=limit,
            allowed_domains=normalized_domains,
            adapter="local_duckduckgo_fallback",
            capability=capability,
            vin_redacted=bool(removed),
        )
        result["provider_order"] = [_LOCAL_DDG_PROVIDER]
        result["providers"] = [
            {
                "provider": _LOCAL_DDG_PROVIDER,
                "status": "success" if result["ok"] else "failed",
                "result_count": len(result["results"]),
            }
        ]
        result["fallback_used"] = True
        return result

    def search_web_multi(
        self,
        *,
        query: str,
        limit: int = _DEFAULT_LIMIT,
        allowed_domains: Sequence[str] | None = None,
        providers: Sequence[str] | None = None,
        timeout_seconds: float = PUBLIC_RESEARCH_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        return self._call(
            capability=SEARCH_WEB_MULTI_CAPABILITY,
            query=query,
            limit=_bounded_int(limit, default=_DEFAULT_LIMIT, minimum=1, maximum=_MAX_LIMIT),
            allowed_domains=allowed_domains,
            providers=providers,
            timeout_seconds=timeout_seconds,
        )

    def research_part_public_evidence(
        self,
        *,
        query: str,
        limit: int = _DEFAULT_LIMIT,
        allowed_domains: Sequence[str] | None = None,
        providers: Sequence[str] | None = None,
        max_pages: int = 1,
        timeout_seconds: float = PUBLIC_RESEARCH_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        _ = _bounded_int(max_pages, default=1, minimum=0, maximum=_MAX_PART_EVIDENCE_PAGES)
        return self._call(
            capability=RESEARCH_PART_PUBLIC_EVIDENCE_CAPABILITY,
            query=query,
            limit=_bounded_int(limit, default=_DEFAULT_LIMIT, minimum=1, maximum=_MAX_PART_EVIDENCE_LIMIT),
            allowed_domains=allowed_domains,
            providers=providers,
            timeout_seconds=timeout_seconds,
        )


_DEFAULT_GATEWAY: WebResearchGateway = DuckDuckGoWebResearchGateway()


def install_web_research_gateway(gateway: WebResearchGateway | None) -> None:
    """Install an authenticated E8 client during process composition only."""

    global _DEFAULT_GATEWAY
    _DEFAULT_GATEWAY = gateway if gateway is not None else DuckDuckGoWebResearchGateway()


def _safe_boundary_result(
    result: Any,
    *,
    query: str,
    vin_redacted: bool,
    capability: str,
    allowed_domains: list[str],
    provider_order: list[str],
) -> dict[str, Any]:
    if not isinstance(result, dict):
        return _gateway_response(
            ok=False,
            query=query,
            results=[],
            allowed_domains=allowed_domains,
            provider_order=provider_order,
            providers=[],
            fallback_used=False,
            adapter="gateway_boundary",
            capability=capability,
            vin_redacted=vin_redacted,
            error={"code": "web_research_gateway_invalid_response", "retryable": False},
        )
    result["query"] = query
    result["vin_redacted"] = bool(result.get("vin_redacted")) or vin_redacted
    result["capability"] = capability
    return result


def _gateway_failure_response(
    *,
    query: str,
    vin_redacted: bool,
    capability: str,
    allowed_domains: list[str],
    provider_order: list[str],
) -> dict[str, Any]:
    return _gateway_response(
        ok=False,
        query=query,
        results=[],
        allowed_domains=allowed_domains,
        provider_order=provider_order,
        providers=[],
        fallback_used=False,
        adapter="gateway_boundary",
        capability=capability,
        vin_redacted=vin_redacted,
        error={"code": "web_research_gateway_failed", "retryable": True},
    )


def search_web_multi(
    *,
    query: str,
    limit: int = _DEFAULT_LIMIT,
    allowed_domains: Sequence[str] | None = None,
    providers: Sequence[str] | None = None,
    timeout_seconds: float = PUBLIC_RESEARCH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Call generic E8 search through the installed read-only adapter."""

    safe_query, removed = _sanitize_query(query)
    domains = _normalize_domains(allowed_domains)
    provider_order = _normalize_providers(providers)
    if not safe_query:
        return _invalid_query_response(
            allowed_domains=domains,
            provider_order=provider_order,
            adapter="gateway_boundary",
            capability=SEARCH_WEB_MULTI_CAPABILITY,
            vin_redacted=bool(removed),
            fallback_used=False,
        )
    try:
        result = _DEFAULT_GATEWAY.search_web_multi(
            query=safe_query,
            limit=limit,
            allowed_domains=domains,
            providers=provider_order,
            timeout_seconds=timeout_seconds,
        )
        return _safe_boundary_result(
            result,
            query=safe_query,
            vin_redacted=bool(removed),
            capability=SEARCH_WEB_MULTI_CAPABILITY,
            allowed_domains=domains,
            provider_order=provider_order,
        )
    except Exception:  # noqa: BLE001 - an injected gateway must not expose provider errors.
        return _gateway_failure_response(
            query=safe_query,
            vin_redacted=bool(removed),
            capability=SEARCH_WEB_MULTI_CAPABILITY,
            allowed_domains=domains,
            provider_order=provider_order,
        )


def research_part_public_evidence(
    *,
    query: str,
    limit: int = _DEFAULT_LIMIT,
    allowed_domains: Sequence[str] | None = None,
    providers: Sequence[str] | None = None,
    max_pages: int = 1,
    timeout_seconds: float = PUBLIC_RESEARCH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Prefer E8 part evidence; use generic search only for a legacy adapter."""

    safe_query, removed = _sanitize_query(query)
    domains = _normalize_domains(allowed_domains)
    provider_order = _normalize_providers(providers)
    if not safe_query:
        return _invalid_query_response(
            allowed_domains=domains,
            provider_order=provider_order,
            adapter="gateway_boundary",
            capability=RESEARCH_PART_PUBLIC_EVIDENCE_CAPABILITY,
            vin_redacted=bool(removed),
            fallback_used=False,
        )
    try:
        method = getattr(_DEFAULT_GATEWAY, "research_part_public_evidence", None)
        if callable(method):
            result = method(
                query=safe_query,
                limit=limit,
                allowed_domains=domains,
                providers=provider_order,
                max_pages=max_pages,
                timeout_seconds=timeout_seconds,
            )
        else:
            result = _DEFAULT_GATEWAY.search_web_multi(
                query=safe_query,
                limit=limit,
                allowed_domains=domains,
                providers=provider_order,
                timeout_seconds=timeout_seconds,
            )
            if isinstance(result, dict):
                result["capability_fallback_used"] = True
        return _safe_boundary_result(
            result,
            query=safe_query,
            vin_redacted=bool(removed),
            capability=RESEARCH_PART_PUBLIC_EVIDENCE_CAPABILITY,
            allowed_domains=domains,
            provider_order=provider_order,
        )
    except Exception:  # noqa: BLE001 - an injected gateway must not expose provider errors.
        return _gateway_failure_response(
            query=safe_query,
            vin_redacted=bool(removed),
            capability=RESEARCH_PART_PUBLIC_EVIDENCE_CAPABILITY,
            allowed_domains=domains,
            provider_order=provider_order,
        )


__all__ = [
    "RESEARCH_PART_PUBLIC_EVIDENCE_CAPABILITY",
    "SEARCH_WEB_MULTI_CAPABILITY",
    "WEB_RESEARCH_GATEWAY_SCHEMA",
    "CapabilityWebResearchGatewayAdapter",
    "DuckDuckGoWebResearchGateway",
    "WebResearchGateway",
    "install_web_research_gateway",
    "normalize_web_research_response",
    "research_part_public_evidence",
    "search_web_multi",
]
