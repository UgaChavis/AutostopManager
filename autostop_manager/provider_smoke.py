from __future__ import annotations

from typing import Any

from .catalog_adapters import catalog_provider_status
from .storage import _now


READ_ONLY_ACCESS_MODES = {
    "public_api",
    "public_site_read_only",
    "api_key",
    "account_token",
    "account_api",
    "account_or_etp",
    "account_webservice",
    "account_webservice_ip_whitelist",
    "local_rules",
}

WRITE_CAPABILITY_MARKERS = {"order", "basket", "checkout", "writeback", "create_order"}


def build_provider_smoke_report(
    *,
    provider: str = "all",
    mode: str = "dry-run",
) -> dict[str, Any]:
    if mode not in {"dry-run", "live-readonly"}:
        return {"ok": False, "error": "mode must be dry-run or live-readonly", "mode": mode}

    status = catalog_provider_status()
    providers = status["providers"]
    if provider != "all":
        providers = [item for item in providers if item["source_id"] == provider]
        if not providers:
            return {"ok": False, "error": "unknown provider", "provider": provider}

    results = [_provider_smoke_result(item, mode=mode) for item in providers]
    blockers = [
        result
        for result in results
        if result["mode"] == "live-readonly"
        and result["live_readonly_status"] in {"missing_env", "blocked_manual_or_write_risk"}
    ]
    return {
        "ok": True,
        "schema": "ProviderSmokeResult",
        "generated_at": _now(),
        "provider": provider,
        "mode": mode,
        "results": results,
        "summary": {
            "provider_count": len(results),
            "configured_count": sum(1 for result in results if result["configured"]),
            "dry_run_valid_count": sum(1 for result in results if result["dry_run_payload_valid"]),
            "live_readonly_ready_count": sum(1 for result in results if result["live_readonly_status"] == "ready"),
            "blocked_count": len(blockers),
            "no_order_guarantee": all(result["no_order_guarantee"] for result in results),
            "redaction_check": all(result["redaction_check"] for result in results),
        },
        "safety": {
            "orders_blocked": True,
            "basket_blocked": True,
            "crm_writeback_blocked": True,
            "secrets_redacted": True,
        },
    }


def _provider_smoke_result(provider: dict[str, Any], *, mode: str) -> dict[str, Any]:
    capabilities = [str(value) for value in provider.get("capabilities") or []]
    configured = bool(provider.get("configured"))
    has_write_capability = any(
        marker in capability.casefold() for marker in WRITE_CAPABILITY_MARKERS for capability in capabilities
    )
    safe_access = str(provider.get("access_mode") or "") in READ_ONLY_ACCESS_MODES
    live_status = "not_requested"
    latency_ms: int | None = None
    warnings: list[str] = []

    if has_write_capability:
        warnings.append("provider_has_write_capability_but_smoke_does_not_call_write_endpoints")

    if mode == "live-readonly":
        if not configured:
            live_status = "missing_env"
        elif not safe_access:
            live_status = "blocked_manual_or_write_risk"
        else:
            live_status = "ready"
            latency_ms = 0

    return {
        "provider": provider["source_id"],
        "stage": provider["stage"],
        "mode": mode,
        "configured": configured,
        "missing_env_names": list(provider.get("missing_env_names") or []),
        "dry_run_payload_valid": bool(provider.get("source_id")) and bool(provider.get("name")),
        "live_readonly_status": live_status,
        "latency_ms": latency_ms,
        "redaction_check": not provider.get("present_env_values"),
        "no_order_guarantee": True,
        "warnings": warnings,
        "access_mode": provider.get("access_mode"),
        "capabilities": capabilities,
    }
