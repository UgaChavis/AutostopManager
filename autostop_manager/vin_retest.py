"""Repeatable native VIN diagnostic run; persist technical metrics only."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, UTC
import json
import math
from pathlib import Path
import re
import time
from typing import Any, cast

OUTCOMES = frozenset(
    "success partial partial_result empty empty_result unsupported invalid_input invalid_operation "
    "group_match identifier_mismatch unparsed_response parse_error dependency_missing database_missing "
    "configuration_missing configuration_error provider_error provider_timeout provider_http_error "
    "provider_http_5xx provider_auth_error provider_ip_quota_exceeded auth_error quota_error client_error".split()
)
FIELD_STATUSES = frozenset({"candidate", "supported", "observed", "disputed", "missing"})
METRIC_FIELDS = (
    "call_id",
    "started_at",
    "ended_at",
    "wall_ms",
    "backend_elapsed_ms",
    "processing_elapsed_ms",
    "network_calls",
    "provider_network_attempt_count",
    "attempt_count",
)


def counter(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def checked_metrics(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    metrics = {}
    valid: bool | re.Match[str] | None
    for key in METRIC_FIELDS:
        item: Any = value.get(key)
        if key == "call_id":
            valid = isinstance(item, str) and re.fullmatch(
                r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", item
            )
        elif key in {"started_at", "ended_at"}:
            valid = isinstance(item, str) and len(item) <= 40
            if valid:
                try:
                    valid = datetime.fromisoformat(item.replace("Z", "+00:00")).tzinfo is not None
                except ValueError:
                    valid = False
        else:
            valid = type(item) in {int, float} and math.isfinite(item) and item >= 0
        if valid:
            metrics[key] = item
    return metrics


def response_body(response: Any) -> dict[str, Any]:
    if response.structuredContent is not None:
        body = response.structuredContent
    else:
        body = json.loads(next(item.text for item in response.content if item.type == "text"))
    if not isinstance(body, dict):
        raise ValueError("Tool response must be an object")
    return body


def technical_summary(payload: dict[str, Any]) -> dict[str, Any]:
    data = cast(dict[str, Any], payload.get("data")) if isinstance(payload.get("data"), dict) else payload
    outcome = payload.get("outcome", payload.get("status"))
    execution = cast(dict[str, Any], payload.get("execution")) if isinstance(payload.get("execution"), dict) else {}
    processing = cast(dict[str, Any], payload.get("processing")) if isinstance(payload.get("processing"), dict) else {}
    fields = cast(dict[str, Any], data.get("field_statuses")) if isinstance(data.get("field_statuses"), dict) else {}
    statuses = {}
    for field, value in fields.items():
        status = value.get("status") if isinstance(value, dict) else value
        if (
            isinstance(field, str)
            and isinstance(status, str)
            and status in FIELD_STATUSES
            and re.fullmatch(r"[a-z_]{1,40}", field)
        ):
            statuses[field] = status
    diagnostics = data.get("provider_diagnostics") or data.get("diagnostics") or {}
    codes = diagnostics.get("error_codes", []) if isinstance(diagnostics, dict) else []
    codes = codes[:64] if isinstance(codes, list) else []
    provider_errors = (
        cast(list[Any], data.get("provider_errors")) if isinstance(data.get("provider_errors"), list) else []
    )
    for error in provider_errors[:64]:
        if isinstance(error, dict):
            extra = error.get("error_codes")
            if isinstance(extra, list):
                codes.extend(extra[:64])
    checked_codes = list(
        dict.fromkeys(
            str(code)
            for code in codes
            if not isinstance(code, bool) and isinstance(code, (str, int)) and re.fullmatch(r"[0-9]{1,4}", str(code))
        )
    )
    network = counter(execution.get("network_calls"))
    if network is None:
        network = counter(processing.get("http_attempts"))
    retries = counter(processing.get("retry_count"))
    if retries is None:
        retries = counter(execution.get("retry_count"))
    attempts = execution.get("attempts")
    if retries is None and (payload.get("retry_attempted") is False or attempts == []):
        retries = 0
    return {
        "outcome": outcome if isinstance(outcome, str) and outcome in OUTCOMES else "unknown",
        "ok": payload.get("ok") is True,
        "response_json_bytes": len(json.dumps(payload, ensure_ascii=False).encode()),
        "network_calls": network,
        "automatic_retries": retries,
        "reused": execution.get("reused") if type(execution.get("reused")) is bool else None,
        "field_statuses": statuses,
        "diagnostic_codes": checked_codes,
        "missing_field_count": len(payload.get("missing_fields") or data.get("missing_fields") or []),
        "conflict_count": len(payload.get("conflicts") or data.get("conflicts") or []),
        "tool_execution": checked_metrics(payload.get("tool_execution")),
    }


async def run_retest(
    session: Any,
    identifier: str,
    schemas: dict[str, dict[str, Any]],
    *,
    timeout_seconds: float = 25,
    full_control: bool = False,
) -> dict[str, Any]:
    """Run once; no transport retry, raw result persistence or automatic full repeat."""
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", identifier):
        raise ValueError("A complete normalized VIN is required")
    if not 0 < timeout_seconds <= 60:
        raise ValueError("timeout_seconds must be in (0, 60]")
    results: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    start = time.monotonic()

    def detail(tool: str) -> dict[str, str]:
        properties = schemas.get(tool, {}).get("properties", {})
        return {"detail": "summary"} if "summary" in properties.get("detail", {}).get("enum", []) else {}

    async def call(key: str, tool: str, arguments: dict[str, Any]) -> None:
        tick = time.monotonic()
        row: dict[str, Any] = {"key": key, "tool": tool, "started_at": datetime.now(UTC).isoformat()}
        try:
            response = await session.call_tool(tool, arguments, read_timeout_seconds=timedelta(seconds=timeout_seconds))
            body = response_body(response)
            if response.isError:
                row.update(outcome="client_error", mcp_is_error=True)
            else:
                row.update(technical_summary(body))
                results[key] = body
        except Exception as exc:  # noqa: BLE001 - isolate SDK failures without persisting private exception text.
            row.update(outcome="client_error", client_error_class=type(exc).__name__)
        row.update(ended_at=datetime.now(UTC).isoformat(), client_wall_ms=round((time.monotonic() - tick) * 1000, 3))
        rows.append(row)

    parts = {"timeout": min(12, timeout_seconds), "max_attempts": 1, **detail("partsapi_catalog_lookup")}
    operations: list[tuple[str, str, dict[str, Any]]] = [
        ("inspect", "inspect_vehicle_identifier", {"identifier": identifier, "identifier_type": "vin"}),
        (
            "identity",
            "decode_vehicle_identity",
            {"identifier": identifier, "live_vpic": True, "live_wmi": True, **detail("decode_vehicle_identity")},
        ),
        (
            "vin_decode",
            "partsapi_catalog_lookup",
            {"operation": "vin_decode", "identifier": identifier, "lang": "ru", **parts},
        ),
        ("vininfo", "vininfo_decode", {"identifier": identifier}),
        ("corgi", "corgi_decode", {"identifier": identifier, "timeout_seconds": min(10, timeout_seconds)}),
        ("brand", "vin_brand_details", {"identifier": identifier}),
        (
            "vinus_summary",
            "partsapi_catalog_lookup",
            {"operation": "decodeVINus", "provider_parameters": {"vin": identifier}, **parts},
        ),
    ]
    await asyncio.gather(*(call(*operation) for operation in operations))
    followups = []
    if "identity" in results and "vin_decode" in results:
        followups.append(
            call(
                "reconcile",
                "reconcile_vehicle_identity",
                {
                    "identifier": identifier,
                    "results": [results["identity"], results["vin_decode"]],
                    **detail("reconcile_vehicle_identity"),
                },
            )
        )
    # A diagnostic comparison is opt-in and cannot retry an empty or failed source.
    if (
        full_control
        and results.get("vinus_summary", {}).get("ok") is True
        and results.get("vinus_summary", {}).get("outcome") in {"success", "partial_result"}
    ):
        followups.append(
            call(
                "vinus_full_control",
                "partsapi_catalog_lookup",
                {
                    "operation": "decodeVINus",
                    "provider_parameters": {"vin": identifier},
                    **parts,
                    "detail": "full",
                },
            )
        )
    await asyncio.gather(*followups)
    networks = [counter(row.get("network_calls")) for row in rows]
    retries = [counter(row.get("automatic_retries")) for row in rows]
    return {
        "schema_version": "autostop.vin-retest.v1",
        "calls": rows,
        "live_wall_ms": round((time.monotonic() - start) * 1000, 3),
        "manager_calls": len(rows),
        "network_calls": sum(networks) if all(value is not None for value in networks) else None,
        "known_network_calls": sum(value for value in networks if value is not None),
        "network_counts_complete": all(value is not None for value in networks),
        "automatic_retries": sum(retries) if all(value is not None for value in retries) else None,
        "known_automatic_retries": sum(value for value in retries if value is not None),
        "retry_counts_complete": all(value is not None for value in retries),
        "full_control_requested": full_control,
        "identifier_or_payload_persisted": False,
    }


def render_report(receipt: dict[str, Any]) -> str:
    lines = [
        "# VIN diagnostic run",
        "",
        f"Live block: {receipt['live_wall_ms'] / 1000:.3f} s; Manager calls: {receipt['manager_calls']}; HTTP attempts: {receipt['network_calls']}.",
        "This excludes agent preparation and final-answer time.",
        "",
        "| Tool | Outcome | Server ms | Bytes |",
        "| --- | --- | ---: | ---: |",
    ]
    for row in receipt["calls"]:
        lines.append(
            f"| {row['key']} | {row['outcome']} | {row.get('tool_execution', {}).get('wall_ms', 'unknown')} | {row.get('response_json_bytes', 'unknown')} |"
        )
    return "\n".join(lines) + "\n"


def save_receipt(directory: Path, receipt: dict[str, Any]) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / "live-trace.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    (directory / "REPORT.md").write_text(render_report(receipt))
