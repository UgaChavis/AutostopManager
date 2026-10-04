"""Asynchronous E4 entry points; synchronous consumers keep the same pure builder."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .vehicle_identity import decode_vehicle_identity, invalid_identity_result, summarize_identity_batch
from .vehicle_identity_inputs import MAX_IDENTITY_ITEMS, validate_identity_input, validate_identity_item
from .vehicle_identity_transport import (
    BATCH_HTTP_ATTEMPT_CAP,
    SINGLE_HTTP_ATTEMPT_CAP,
    IdentityBudget,
    collect_identity_provider_results_async,
)


def _build_row(
    item: dict[str, Any],
    vpic_result: dict[str, Any] | None,
    wmi_result: dict[str, Any] | None,
    *,
    item_index: int | None = None,
) -> dict[str, Any]:
    if not item["ok"]:
        return invalid_identity_result(item["errors"], item_index=item_index)
    try:
        result = decode_vehicle_identity(
            item["identifier"],
            crm_context=item["context"],
            identifier_type=item["identifier_type"],
            live_vpic=False,
            live_wmi=False,
            vpic_result=vpic_result,
            wmi_result=wmi_result,
        )
    except (TypeError, ValueError, AttributeError, OverflowError):
        # A recoverable internal row failure must not discard other input positions.
        result = invalid_identity_result(
            [{"code": "identity_processing_error", "field": "item", "stage": "identity_merge"}],
            item_index=item_index,
        )
    if item_index is not None:
        result["item_index"] = item_index
    notes = item.get("normalization_notes") or []
    if notes:
        result["normalization_notes"] = notes
    return result


async def decode_vehicle_identity_async(
    identifier: str,
    *,
    crm_context: dict[str, Any] | None = None,
    model_year: int | None = None,
    make_hint: str | None = None,
    live_vpic: bool = True,
    live_wmi: bool = True,
    identifier_type: str = "auto",
    vpic_result: dict[str, Any] | None = None,
    wmi_result: dict[str, Any] | None = None,
    budget: IdentityBudget | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    request_budget = budget or IdentityBudget(max_attempts=SINGLE_HTTP_ATTEMPT_CAP)
    item = validate_identity_input(
        identifier, crm_context, model_year=model_year, make_hint=make_hint, identifier_type=identifier_type
    )
    if not item["ok"]:
        return invalid_identity_result(item["errors"])
    collected = await collect_identity_provider_results_async(
        [item],
        live_vpic=live_vpic and vpic_result is None,
        live_wmi=live_wmi and wmi_result is None,
        use_vpic_batch=False,
        budget=request_budget,
        client=client,
    )
    result = _build_row(
        item,
        vpic_result if vpic_result is not None else collected["vpic_results"][0],
        wmi_result if wmi_result is not None else collected["wmi_results"][0],
    )
    result["processing"] = collected["processing"]
    return result


async def decode_vehicle_identities_async(
    items: list[dict[str, Any]],
    *,
    live_vpic: bool = True,
    use_vpic_batch: bool = True,
    budget: IdentityBudget | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    request_budget = budget or IdentityBudget(max_attempts=BATCH_HTTP_ATTEMPT_CAP)
    if not isinstance(items, list) or len(items) > MAX_IDENTITY_ITEMS:
        return {
            "ok": False,
            "status": "invalid_input",
            "count": 0,
            "results": [],
            "errors": [
                {
                    "code": "identity_batch_too_large" if isinstance(items, list) else "expected_list",
                    "field": "items",
                    "stage": "input_validation",
                }
            ],
        }
    prepared = [validate_identity_item(item) for item in items]
    collected = await collect_identity_provider_results_async(
        prepared,
        live_vpic=live_vpic,
        live_wmi=live_vpic,
        use_vpic_batch=use_vpic_batch,
        budget=request_budget,
        client=client,
    )
    results = []
    for index, item in enumerate(prepared):
        # Let unrelated MCP requests and cancellation run between small groups of pure merges.
        if index % 10 == 0:
            await asyncio.sleep(0)
        results.append(
            _build_row(item, collected["vpic_results"][index], collected["wmi_results"][index], item_index=index)
        )
    return summarize_identity_batch(results, vpic_batch=collected["vpic_batch"], processing=collected["processing"])
