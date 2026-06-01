# VIN/OEM Lookup Playbook

Purpose: make OEM and original catalog number lookup from VIN or chassis/frame
number deterministic, source-aware, and auditable.

For the full CRM card workflow that starts with a live card and ends with
crosses/analogs, закупка/RF market price, and CRM writeback, use
`docs/agent/crm_vin_oem_parts_lookup_playbook.md` as the orchestration layer.
For a 10-card or batch quality report, use `benchmark_vin_parts_lookup` /
`vin-parts-benchmark` after identity decode. Treat it as readiness evidence, not
as OEM fitment proof when live EPC/catalog/provider credentials are missing.
Use `build_vin_parts_work_order` / `vin-parts-work-order` after benchmark to
turn readiness into the exact per-card lookup sequence: OEM/EPC route,
prepared API check, cross/applicability verification, supplier search order,
and CRM writeback gate.
When catalog routing ids are already known, use `lookup_oem_catalog_candidates`
/ `oem-catalog-lookup` to execute or dry-run the three-provider OEM candidate
lookup through Parts-Catalogs, PartsAPI, and 17VIN.

## Core Rule

Never force every identifier through one VIN decoder path.

Never invent an OEM number. If a source does not return a VIN/frame-specific
catalog result, mark the candidate as unconfirmed instead of filling the gap
from a weak listing or model guess.

Classify the identifier first, then choose the market-appropriate source:

- ISO VIN -> decode through a VIN decoder first
- Japan-market chassis or frame number -> use the manufacturer or EPC path
  that accepts frame number input
- market-specific code -> resolve the market and model family first, then
  choose the source

## Source Priority

1. Official decode source for the identifier type.
2. Manufacturer catalog or recall portal that accepts the same identifier.
3. Public EPC mirror or catalog that exposes the relevant original part data.
4. Marketplace only after the OEM or replacement number is known.

## Minimal Inputs To Collect

Before lookup, capture:

- raw identifier
- identifier type if known
- make / model if known
- market if known
- model year or build window if known
- engine and transmission if the source needs them

## Routing Rules

### ISO VIN

1. Normalize the VIN.
2. Run `decode_vehicle_identity` first. It checks vPIC, WMI/platform hints,
   model-year/check-digit diagnostics, CRM context, conflicts, and adapter
   readiness.
3. Use the decoded make, model, and year to select the catalog route.
4. If the VIN decode is incomplete, keep the uncertainty explicit and require
   PartsAPI, Parts-Catalogs, 17VIN, partslink24, AUTOPOISK, or brand EPC before
   high-confidence parts lookup.
5. For 17VIN, use `vin17-decode` first to obtain the EPC/vehicle profile and
   then `vin17-search-part` only when an exact OE/part number search is needed;
   both routes require `VIN17_ACCOUNT` and `VIN17_SECRET` for live calls.
6. For PartsAPI, use `partsapi-lookup --operation vin_decode_oe` for VIN/frame
   OEM-catalog identity, `parts_by_vin` for a requested group, and
   `oe_applicability` / `crosses_with_brand` after an OEM or selected article is
   known. Live calls require `PARTSAPI_KEY` and `PARTSAPI_BASE_URL`.
7. For a combined read-only request after the vehicle/group route is known, use
   `oem-catalog-lookup <identifier> --part <name> --catalog-id <catalog>
   --car-id <car> --group-id <group> --epc <epc>`.

### Japan-Market Chassis / Frame Number

1. Keep the frame number exactly as shown on the plate or inspection document.
2. Run `decode_vehicle_identity`; if it suggests a hyphenated query form, try
   both raw and hyphenated forms in the catalog.
3. Use the manufacturer or EPC route that accepts frame number input.
4. Do not invent a full 17-character VIN-style decode when the market does not
   provide one.
5. Treat the catalog output as authoritative only if the source explicitly
   shows the original part number or supersession chain.

### Market-Specific Codes

1. Identify the market first.
2. Map the code to the catalog family or model code.
3. Use the most direct official catalog or EPC route.
4. If the code is ambiguous, return the smallest safe set of candidate routes.

## Output Shape

Return a compact lookup card:

- identifier type
- normalized identifier
- market
- decoded make / model / generation
- catalog route used
- OEM candidate numbers
- supersession or replacement notes
- confidence and uncertainty

## Memory Rule

Store only durable conclusions:

- which identifier class worked
- which source route was authoritative
- which OEM or replacement number was chosen
- which compatibility caveat should be reused later

Do not store full catalog dumps or temporary search results in manager memory.
