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

## Official Catalog Boundaries

- MAN commercial vehicles: use MAN Service Portal/webMANTIS or MAN
  partslink24 first. webMANTIS is the spare-parts catalogue route for MAN
  Genuine Parts and accepts the MAN vehicle production number or a 17-character
  VIN through the Service Portal flow. partslink24 is an official ordering
  route for MAN Genuine Parts and identifies parts with the VIN, but it requires
  registration/customer data and may be trial or paid access.
- Do not use downloadable MANTIS/EPC installers from third-party shops,
  torrents, or forum mirrors as an official source. If such a page is the only
  hit, mark the route as `blocked: legal_epc_access_required` and ask for
  authorized MAN/webMANTIS, partslink24, dealer, or supplier confirmation.
- MAHLE, Bosch, MANN-FILTER, Hengst, Donaldson, Fleetguard, NGK/NTK, and
  similar manufacturer catalogs are aftermarket/OE-supplier catalogs. Use them
  to confirm supplier article numbers, filters, plugs, sensors, crosses,
  dimensions, replacements, and applicability. They can raise confidence for a
  selected analog, but they do not by themselves prove the vehicle-specific OEM
  number unless the catalog explicitly exposes an OEM reference and it is checked
  against the VIN/frame EPC result.
- TecDoc/TecAlliance data is a strong aftermarket identity layer for vehicle
  type, article, OE-reference, cross, supersession, and applicability checks.
  Treat it as cross/applicability evidence, not as the sole source of an OEM
  EPC decision when the part is configuration-sensitive.
- Freely downloadable official PDF catalogs are source records and manual
  reference material. Do not commit full catalog PDFs or database exports to
  Git unless the license, size, and owner intent are explicit. Prefer storing
  source URL, publisher, catalog title/version, product scope, and date checked;
  if a local copy is necessary, keep it in an untracked cache and cite the source
  record in the CRM note.

## Offline Catalog Cache

Use the local offline cache when it exists:

- source pack:
  `docs/agent/automotive_sources/source_cache/offline_parts_catalogs_knowledge_pack/`;
- runtime index: `data/offline_parts_catalogs/catalog_index.json`;
- extracted searchable text: `data/offline_parts_catalogs/text/`.

The cache is a supporting evidence layer for official downloadable
aftermarket/catalog PDFs and spreadsheets. It is useful for filters, plugs,
commercial-vehicle service parts, OE-reference rows, competitor crosses,
dimensions, product notes, and kit/application footnotes.

Recommended local check:

```bash
rg -n "<OEM-or-article-or-engine-code>" data/offline_parts_catalogs/text
```

When using a hit, name the `catalog_id`, publisher, title, source URL,
retrieved date, and checked number. Do not paste tables. Do not treat an
offline MAHLE/Bosch/MANN/Donaldson/NGK/ZF match as a VIN-specific OEM result
unless the OEM number was already confirmed through VIN/frame EPC or authorized
dealer/catalog evidence.

MAN boundary: no legal public offline MAN EPC is in the local cache. MAN OEM
numbers still require authorized MAN Service Portal/webMANTIS, MAN
PartsBase/partslink24, dealer, supplier, or configured catalog-provider
confirmation. If only third-party MANTIS installers/mirrors appear, mark the
route blocked until authorized evidence is available.

## Workflow

1. Classify and normalize the identifier with `decode_vehicle_identity`.
   vPIC is vehicle identity only; it is not an OEM-parts catalog.
2. Recognize the requested part with `normalize_part_intent`; capture axle,
   side, position, quantity basis, old number, and label photo when relevant.
3. Select a catalog route from `vin_oem_sources.json` and resolve numeric
   PartsAPI group ids through `partsapi_category_index`.
4. Use PartsAPI as the current MVP route:
   - `vin_decode` / `VINdecode` -> TecDoc/TecRMI identity and `carId`.
   - `vin_decode_oe` / `VINdecodeOE` -> OE-catalog vehicle identity.
   - `plate_to_vin` / `gosnomer2vin` -> read-only VIN lead from a Russian
     registration number. Verify against vehicle/CRM before treating it as the
     identity or writing anything.
   - `parts_by_vin` / `getPartsbyVIN` -> VIN-specific OEM candidates; live
     calls require numeric `cat` id and default to `type=oem`.
   - `oe_applicability` -> extra applicability evidence only; empty output is
     not a negative fitment proof.
   - `crosses` / `crosses_with_brand` / `crosses_title` -> replacements in
     `cross_candidates`; `crosses_title` also carries localized `partname`.
   - `part_name_by_brand_number` / `getPartnameByBrandNumber` -> article-name
     enrichment by brand/article; it does not establish applicability.
   - `search_articles` -> TecDoc metadata in `article_candidates`.
   - `article_crosses` / `getArticleCrosses` -> related TecDoc cross articles
     after `search_articles` has returned an `ART_ID`.
   - `search_tree` / `getSearchTree` -> refresh/validate numeric category
     routing for live `getPartsbyVIN`.
5. Record OEM candidates only from VIN/frame-specific catalog evidence. Keep
   cross and article metadata as enrichment until applicability is confirmed.
6. For filters, plugs, belts, wipers, sensors, and other common wear parts,
   use manufacturer aftermarket catalogs after the VIN/EPC step:
   - check the local offline cache first when
     `data/offline_parts_catalogs/catalog_index.json` exists;
   - search the confirmed OEM/reference and normalized vehicle profile in
     MAHLE/Bosch/MANN-FILTER/Hengst/NGK/Donaldson/Fleetguard as relevant;
   - compare product category, dimensions, notes, production range, side/axis,
     engine code, and kit contents;
   - keep the aftermarket article in `cross_candidates` or `selected_part`,
     not in `oem_candidates`, unless the source is the genuine OEM EPC.
7. Start supplier/market price lookup only after the OEM reference or selected
   replacement is stable.

For labor estimates, hand the confirmed vehicle profile to
`docs/agent/work_labor_pricing_playbook.md`. Its AUTONORMS chain supplies
operation-specific `workTime` evidence, while the labor price still comes from
the separate public-market estimate.

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

Store only reusable conclusions: identifier class, authoritative source route,
and a general compatibility caveat that will improve future work. Do not store
customer-specific chosen numbers, raw VIN, client data, secrets, full payloads,
or temporary search lists.
