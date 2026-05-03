# VIN/OEM Lookup Playbook

Purpose: make OEM and original catalog number lookup from VIN or chassis/frame
number deterministic, source-aware, and auditable.

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
