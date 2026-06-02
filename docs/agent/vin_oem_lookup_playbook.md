# VIN/OEM Lookup Playbook

Purpose: make VIN/frame -> original catalog part lookup deterministic,
source-aware, and auditable. This document is the canonical workflow. PartsAPI
method-level details live in `docs/agent/partsapi_method_contracts.md`.

For full CRM orchestration with crosses, procurement prices, and CRM writeback,
use `docs/agent/crm_vin_oem_parts_lookup_playbook.md`.

## Core Rules

- Never invent an OEM number. If a source does not return a VIN/frame-specific
  catalog result, mark the candidate as unconfirmed.
- Do not force every identifier through one VIN decoder path:
  - ISO VIN -> decode identity first.
  - Japan-market frame/chassis -> use a catalog route that accepts frame input.
  - Market code -> resolve market/model family before catalog lookup.
- Keep original/OEM candidates separate from crosses, articles, supplier prices,
  and marketplace matches.
- Do not store raw VIN/frame, client contacts, API keys, or full catalog dumps
  in docs, durable memory, tests, or CRM board summaries.

## Source Priority

1. Official decode source for the identifier type.
2. Paid/official EPC or genuine-parts portal that accepts the same identifier.
3. Manufacturer catalog or service portal that confirms market/options/campaigns.
4. Public EPC mirror only as fallback.
5. Marketplace only after the OEM or replacement number is known.

## Workflow

1. Classify and normalize the identifier with `decode_vehicle_identity`.
   vPIC is vehicle identity only; it is not an OEM-parts catalog.
2. Recognize the requested part with `normalize_part_intent`; capture axle,
   side, position, quantity basis, old number, and label photo when relevant.
3. Select a catalog route from `vin_oem_sources.json`.
4. Use PartsAPI as the current MVP route:
   - `vin_decode` / `VINdecode` -> TecDoc/TecRMI identity and `carId`.
   - `vin_decode_oe` / `VINdecodeOE` -> OE-catalog vehicle identity.
   - `parts_by_vin` / `getPartsbyVIN` -> VIN-specific OEM candidates; live
     calls require numeric `cat` id and default to `type=oem`.
   - `oe_applicability` -> extra applicability evidence only; empty output is
     not a negative fitment proof.
   - `crosses` / `crosses_with_brand` -> replacements in `cross_candidates`.
   - `search_articles` -> TecDoc metadata in `article_candidates`.
5. Record OEM candidates only from VIN/frame-specific catalog evidence. Keep
   cross and article metadata as enrichment until applicability is confirmed.
6. Start supplier/market price lookup only after the OEM reference or selected
   replacement is stable.

## PartsAPI Output Buckets

- `vehicle_profiles`: vehicle identity from `VINdecode` and `VINdecodeOE`.
- `oem_candidates`: original candidates only from VIN-specific `getPartsbyVIN`.
- `cross_candidates`: analogs/replacements from `getCrosses*`.
- `article_candidates`: TecDoc article metadata from `searchArticles`.
- `empty_payload`: provider returned an empty `null`, list, or object; this is
  not the same as a confirmed match.
- `partsapi_category_resolution`: distinguishes numeric `cat` ids from text
  part-intent hints and `category_unresolved` blockers.

## CRM Smoke Check

Use `partsapi-vin-smoke` for one read-only CRM-like item after adapter changes:

```bash
python -m autostop_manager.cli partsapi-vin-smoke \
  --item-json '<json object>' \
  --partsapi-category '<numeric cat id if known>'
```

The smoke report must redact the identifier, omit raw payloads, cap enrichment
to a small candidate set, and never write to CRM. If no numeric PartsAPI `cat`
id is known, report `category_unresolved` instead of spending live request quota
on an ambiguous `getPartsbyVIN` call.

## Confidence

- `high`: VIN/frame-specific catalog returned one OEM candidate or a clear
  supersession chain.
- `medium`: source-backed OEM exists, but option/side/production split still
  needs review.
- `low`: only public mirror, article metadata, cross, or model-level evidence.
- `blocked`: missing part group, numeric PartsAPI `cat`, EPC route, old number,
  option code, side/axis, or another field needed to avoid a wrong purchase.

## Memory Rule

Store only durable conclusions: identifier class, authoritative route, chosen
OEM/replacement number, and reusable compatibility caveat. Do not store raw VIN,
client data, secrets, full payloads, or temporary search lists.
