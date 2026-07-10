# Vehicle Identity Playbook

Purpose: help the AutoStop manager identify vehicles correctly across
different markets before any parts search, recall check, or compatibility
decision.

## Core Question

Before decoding, ask:

- What kind of identifier is this?
- Which market does it belong to?
- Do I need a vehicle profile or a parts-compatibility profile?

Do not force every identifier into the same 17-character VIN path.
Use the local `decode_vehicle_identity` tool / `python -m autostop_manager.cli
decode-vehicle` before OEM or parts lookup when the input comes from CRM,
looks ROW/JDM, or has weak/partial vPIC output.
Use `catalog_provider_status` to verify live catalog/API readiness before
claiming that PartsAPI, Parts-Catalogs, 17VIN, AUTOPOISK, partslink24, or
supplier APIs are available.
For board-wide checks, use the batch decoder path (`decode_vehicle_identities`
or `decode-vehicles`); it calls public vPIC batch for ISO VINs, then applies
local WMI/platform rules and CRM context.
For parts-search quality checks across a 10-card batch, use
`benchmark_vin_parts_lookup` / `vin-parts-benchmark` after the batch decoder.
It reports identity coverage, requested-part recognition, safe public query
coverage, PartsAPI/17VIN dry-run readiness, and missing live catalog/supplier
credentials without echoing raw customer identifiers.
When the benchmark shows identity and part intent are ready, use
`build_vin_parts_work_order` / `vin-parts-work-order` to produce the exact
per-card EPC/API/supplier work order and CRM writeback gates.
When the input comes from AutoStop CRM, pass the compact vehicle profile
(`make_display`, `model_display`, `production_year`, engine/transmission,
drivetrain, and source confidence) into the decoder; the local tool normalizes
common CRM typos such as `Volkskwagen` before routing.

## Identifier Types

### ISO VIN

Use the standard VIN route when the input is a 17-character VIN.

Typical checks:

- 17 characters
- no `I`, `O`, or `Q`
- check digit validation when the market uses it
- WMI, VDS, VIS split

Important limitation: NHTSA vPIC is a useful official baseline, but it is not
an EPC. It may decode North-American VINs cleanly while returning partial or
conflicting data for Europe/ROW/Russia/CIS/Japan/China identifiers. Treat vPIC
as one evidence source, not as final parts applicability.

### Japan-Market Chassis / Frame Number

Treat Japanese chassis numbers as primary identifiers for Japan-market cars.
They may appear as:

- `frame number`
- `chassis number`
- `車台番号`
- model/frame number variants used by the manufacturer

Typical traits:

- may be shorter than a global VIN
- may require hyphen removal or split-field entry
- recall and service portals often ask for the chassis number from the
  inspection certificate
- model code, engine code, and market code are often needed to finish the
  decode

The number may be entered without a hyphen in CRM, for example `MR41S123456`.
Normalize this into both raw and catalog-query forms (`MR41S123456` and
`MR41S-123456`) and use Suzuki/Honda/Toyota/Nissan/Mitsubishi-compatible EPC
routes. Do not invent a 17-character VIN.

### Korea-Market VIN

Most Korea-market cars still use standard VIN decoding, but the useful output
is often a market-specific vehicle profile rather than a full trim dump.

Typical checks:

- validate the VIN format
- confirm market and model family
- cross-check trim, engine, transmission, and plant against the manufacturer
  or EPC source

### Other Market-Specific Codes

Some vehicles expose extra internal identifiers:

- body number
- model code
- engine code
- transmission code
- trim code
- production or plant code

Treat these as supplements, not as replacements for the main identifier.

## Routing Rules

### Europe and Russia

1. Validate the VIN.
2. Decode the base structure.
3. Confirm vehicle family, engine, transmission, and market.
4. If parts are needed, hand off to the parts-search playbook.

For `WDD`, `XW8`, `WVW`, `WAU`, BMW, Mercedes, VAG, and Skoda VINs, expect
vPIC to be incomplete. A high-confidence decode usually needs brand EPC,
partslink24, erWin/ETKA/EPC, dealer catalog, or a commercial VIN/OE API.

### Japan

1. Classify the input as a chassis/frame number first.
2. Normalize the number exactly as shown on the inspection certificate or
   plate.
3. Use manufacturer recall or owner portals that accept chassis number input.
4. Pull model code, engine code, trim, and build clues from official sources
   or EPC data.
5. Do not invent a full VIN-style decode when the market does not provide one.

Useful official patterns:

- Toyota Japan recall search uses chassis number input.
- Nissan, Mazda, Subaru, and Honda recall portals also use chassis / vehicle
  number style searches.
- Japanese inspection documents commonly expose `車台番号` as the stable key.

### Korea

1. Validate the VIN.
2. Decode the core VIN structure.
3. Cross-check the result against the manufacturer, service portal, or EPC.
4. Mark option packages and trim details as confirmed only if the source
   explicitly supports them.

## Output Shape

Return a compact vehicle identity card:

- identifier type
- raw identifier
- market
- make / model / generation
- year or build window
- engine
- transmission
- drive / body / chassis family
- plant or origin if confirmed
- compatibility notes
- confidence and unknowns

Also include:

- check digit and model-year diagnostics
- field-level evidence and conflicts
- source limitations
- required next source for high confidence
- adapter status for PartsAPI, Parts-Catalogs, 17VIN, AUTOPOISK, and brand EPC
- missing live credentials such as `PARTSAPI_KEY`, `PARTSAPI_BASE_URL`,
  `PARTS_CATALOGS_API_KEY`, `PARTS_CATALOGS_BASE_URL`, `VIN17_ACCOUNT`, and
  `VIN17_SECRET`

## Error Handling

If the identifier is ambiguous:

- ask for a photo of the plate
- ask for the registration or inspection document
- ask for engine or transmission code if the market needs it
- do not guess unsupported trim details

If sources disagree:

- prefer the document or plate over a generic decoder
- prefer manufacturer or EPC data over marketplace guesses
- mark the conflict explicitly

## Handoff To Parts

Once the vehicle identity is stable, pass the result to:

- `docs/agent/vin_oem_lookup_playbook.md`
- `docs/agent/parts_search_playbook.md`

Before writing to CRM materials, identity should be at least `high` for the
vehicle and the selected part still needs VIN/frame-specific EPC or supplier
confirmation. If identity is only `medium`, write the uncertainty and required
confirmation into the description/quote matrix; do not write final parts as
confirmed.

Keep only durable conclusions in memory:

- which identifier type worked
- which market path worked
- which source was authoritative
- which compatibility caveat must be reused later

## Sources

- [NHTSA vPIC API](https://vpic.nhtsa.dot.gov/api/)
- [PartsAPI docs](https://partsapi.ru/docs)
- [17VIN API docs](https://en.17vin.com/doc.html)
- [Parts-Catalogs API docs](https://www.parts-catalogs.com/doc/us/introduction.htm)
- [AUTOPOISK](https://autopoisk.su/en)
- [partslink24](https://www.partslink24.com/en)
- [Toyota Japan recall search](https://www.toyota.co.jp/recall-search/dc/en/search)
- [Nissan recall search](https://www.nissan.co.jp/RECALL/search_en.html)
- [Mazda recall search](https://www2.mazda.co.jp/service/recall/)
- [Subaru recall search](https://recall.subaru.co.jp/lqsb/)
- [Honda recall page](https://www.honda.co.jp/recall/)
- [MLIT vehicle inspection certificate](https://www.jidoushatouroku-portal.mlit.go.jp/jidousha/kensatoroku/about/inspect/certificate/index.html)
- [Kia VIN overview](https://www.kia.com/nmc/en/discover-kia/ask/what-is-a-vin.html)
- [Hyundai Australia VIN FAQ](https://www.hyundai.com/au/en/owning/myhyundaicare/faq)
