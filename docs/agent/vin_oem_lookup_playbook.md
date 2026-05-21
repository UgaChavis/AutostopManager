# VIN/OEM Lookup Playbook

Purpose: make OEM and original catalog number lookup from VIN or chassis/frame
number deterministic, source-aware, and auditable. The first strengthened
route is BMW/VAG with paid/official EPC sources as the preferred authority and
public mirrors only as fallback.

## Core Rule

Never force every identifier through one VIN decoder path.

Classify the identifier first, then choose the market-appropriate source:

- ISO VIN -> decode through a VIN decoder first
- Japan-market chassis or frame number -> use the manufacturer or EPC path
  that accepts frame number input
- market-specific code -> resolve the market and model family first, then
  choose the source

## Source Priority

1. Official decode source for the identifier type.
2. Paid/official EPC or genuine-parts portal that accepts the same identifier.
3. Manufacturer catalog, AIR/ETK/ETKA, or recall/service portal that helps
   confirm market, options, PR/SA codes, or campaigns.
4. Public EPC mirror only as fallback.
5. Marketplace only after the OEM or replacement number is known.

## Minimal Inputs To Collect

Before lookup, capture:

- raw identifier
- identifier type if known
- make / model if known
- market if known
- model year or build window if known
- engine and transmission if the source needs them
- part name or catalog group
- side, axle, front/rear, left/right, or position when relevant
- old part number or label photo for high-variant parts

## Workflow Phases

1. `VIN decode`: classify the identifier and decode ISO VINs through NHTSA
   vPIC or the market-appropriate frame/chassis route. vPIC is vehicle identity
   only, not an OEM-parts catalog.
2. `Catalog vehicle selection`: choose the brand/market catalog route from
   `vin_oem_sources.json`; for BMW/VAG prefer paid/official partslink24,
   BMW AOS/AIR/ETK, or VAG ETKA routes when legal access exists.
3. `Part group lookup`: search the catalog by part name, group, diagram, or
   old part number. Do not use Drom/ZZap/Avito to decide the OEM number.
4. `OEM candidate validation`: record only numbers backed by a VIN-specific
   EPC screen/export, including supersession chain and quantity where visible.
5. `Market price search`: begin pricing or availability search only after the
   OEM reference is stable.

## Routing Rules

### ISO VIN

1. Normalize the VIN.
2. Decode it with NHTSA vPIC first.
3. Use the decoded make, model, and year to select the catalog route.
4. If the VIN decode is incomplete, keep the uncertainty explicit.

### Japan-Market Chassis / Frame Number

1. Keep the frame number exactly as shown on the plate or inspection document.
2. Use the manufacturer or EPC route that accepts frame number input.
3. Do not invent a full 17-character VIN-style decode when the market does not
   provide one.
4. Treat the catalog output as authoritative only if the source explicitly
   shows the original part number or supersession chain.

### Market-Specific Codes

1. Identify the market first.
2. Map the code to the catalog family or model code.
3. Use the most direct official catalog or EPC route.
4. If the code is ambiguous, return the smallest safe set of candidate routes.

### BMW / MINI

Preferred route:

1. partslink24 when an authorized account is available.
2. BMW AOS -> AIR/ETK when subscribed.
3. BMW technical/service sources only for vehicle context; confirm the actual
   part number in ETK/AIR/partslink24.

Capture exact OEM number, supersession, quantity, SA/options, and any VIN
variant note. If ETK/AIR shows multiple variants, request old part number,
label photo, or option data instead of guessing.

### VAG / Audi / Volkswagen / Skoda / Seat / Cupra

Preferred route:

1. partslink24 when an authorized account is available.
2. ETKA or an authorized VAG parts catalog.
3. erWin only for service/repair context; confirm part numbers in ETKA or
   partslink24.

For DSG/mechatronic, control units, steering racks, body electronics, and
option-dependent parts, capture gearbox code, PR/options, old label number, and
hardware/software number when the catalog asks for them.

## Output Shape

Return an OEM lookup dossier:

- identifier type
- normalized identifier
- market
- decoded make / model / generation
- `catalog_routes` / backward-compatible `steps`
- `provider_adapters`: `route_only`, `manual_capture`, future `connected`
- `oem_candidates`
- `supersessions`
- `fitment_confidence`
- `missing_context`
- `next_actions`

Confidence model:

- `high`: VIN-specific EPC accepted the vehicle and produced one OEM number or
  a clear supersession chain.
- `medium`: source-backed OEM exists, but options or variant notes still need
  review.
- `low`: public mirror or indirect catalog result only.
- `blocked`: missing part name, EPC capture, old part number, PR/SA option, or
  other data needed to avoid a wrong purchase.

## Memory Rule

Store only durable conclusions:

- which identifier class worked
- which source route was authoritative
- which OEM or replacement number was chosen
- which compatibility caveat should be reused later

Do not store full catalog dumps or temporary search results in manager memory.
