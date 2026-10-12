"""Registration of independent automotive tools; schemas come from their functions."""

from __future__ import annotations

from typing import Any

from mcp.types import ToolAnnotations
from mcp.server.fastmcp import Context

from .automotive_identity import (
    compare_vehicle_modifications,
    decode_vehicle_batch_async,
    decode_vin_vpic,
    decode_wmi_vpic,
    inspect_vehicle_identifier,
    reconcile_vehicle_identity,
)
from .automotive_labor import calculate_work_price, collect_work_price_evidence, normalize_labor_time
from .automotive_parts import (
    assess_part_fitment,
    capture_oem_evidence,
    compare_part_relations,
    normalize_parts_request,
    resolve_catalog_group,
)


def register_automotive_tools(server: Any) -> None:
    from .elcats_catalog import elcats_catalog_query
    from .automotive_offline import (
        corgi_decode,
        decode_frame_local,
        decode_wmi_local,
        vin_brand_details,
        vininfo_decode,
    )

    tools = (
        (
            elcats_catalog_query,
            "Read one selected public catalog operation using a supplied vehicle profile and bound catalog references. Elcats, Japancats and Ssangyong remain distinct providers; default-disabled and robots/access restrictions are enforced. No VIN decode, hidden provider fallback or fitment assertion.",
            True,
        ),
        (inspect_vehicle_identifier, "Inspect identifier format without network or substitutions.", False),
        (decode_vin_vpic, "Call only the selected vPIC VIN endpoint once. No WMI or PartsAPI fallback.", True),
        (decode_wmi_vpic, "Call only vPIC WMI; manufacturer hints do not prove model or engine.", True),
        (decode_wmi_local, "Read the versioned local WMI registry; unsupported prefixes remain unknown.", False),
        (decode_frame_local, "Read supported local frame families without network; unsupported is explicit.", False),
        (
            vin_brand_details,
            "Read limited versioned brand rules and context; family hints do not prove build configuration.",
            False,
        ),
        (vininfo_decode, "Decode offline with pinned VINinfo; no online fallback or package download.", False),
        (
            corgi_decode,
            "Decode offline with prepared, attested Corgi and local vPIC database; never downloads while decoding.",
            False,
        ),
        (
            reconcile_vehicle_identity,
            "Pure reconciliation of bound ready results with lineage, variants and conflicts.",
            False,
        ),
        (
            compare_vehicle_modifications,
            "Pure comparison of known context and modification candidates; ambiguity survives.",
            False,
        ),
        (
            normalize_parts_request,
            "Pure multiposition request normalization preserving quantities, markings and unknown fields.",
            False,
        ),
        (
            resolve_catalog_group,
            "Pure selection from a supplied modification-bound tree; rejects mixed namespaces.",
            False,
        ),
        (
            capture_oem_evidence,
            "Capture explicit OEM evidence with source, scope and binding; article/cross is insufficient.",
            False,
        ),
        (
            compare_part_relations,
            "Pure cross/analog/OE-reference/supersession comparison; primary evidence required for replacements.",
            False,
        ),
        (
            assess_part_fitment,
            "Pure scoped vehicle/part assessment; unknown or mismatched conditions never become supported.",
            False,
        ),
        (
            normalize_labor_time,
            "Pure labor normalization retaining raw time, unit, source, included and overlapping operations.",
            False,
        ),
        (
            collect_work_price_evidence,
            "Collect selected public price evidence or explicit provided aggregate under one deadline.",
            True,
        ),
        (
            calculate_work_price,
            "Pure calculation using supplied labor, observations, rate and versioned policy; no hidden experience read.",
            False,
        ),
    )
    for function, description, network in tools:
        server.tool(
            name=function.__name__,
            description=description,
            annotations=ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=not network, openWorldHint=network
            ),
        )(function)

    @server.tool(
        name="decode_vehicle_batch",
        description="Bounded ordered batch using one selected decoder; request cancellation stops subsequent rows. An issued synchronous read retains its bounded timeout.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True),
    )
    async def decode_vehicle_batch_tool(
        items: list[dict[str, Any] | str], decoder: str, deadline_seconds: float = 30, ctx: Context | None = None
    ) -> dict[str, Any]:
        from .vehicle_identity_request import run_identity_request

        return await run_identity_request(decode_vehicle_batch_async(items, decoder, deadline_seconds), ctx)
