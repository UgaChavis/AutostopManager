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

## Identifier Types

### ISO VIN

Use the standard VIN route when the input is a 17-character VIN.

Typical checks:

- 17 characters
- no `I`, `O`, or `Q`
- check digit validation when the market uses it
- WMI, VDS, VIS split

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
- `docs/agent/zzap_search_playbook.md`

Keep only durable conclusions in memory:

- which identifier type worked
- which market path worked
- which source was authoritative
- which compatibility caveat must be reused later

## Sources

- [NHTSA vPIC API](https://vpic.nhtsa.dot.gov/api/)
- [Toyota Japan recall search](https://www.toyota.co.jp/recall-search/dc/en/search)
- [Nissan recall search](https://www.nissan.co.jp/RECALL/search_en.html)
- [Mazda recall search](https://www2.mazda.co.jp/service/recall/)
- [Subaru recall search](https://recall.subaru.co.jp/lqsb/)
- [Honda recall page](https://www.honda.co.jp/recall/)
- [MLIT vehicle inspection certificate](https://www.jidoushatouroku-portal.mlit.go.jp/jidousha/kensatoroku/about/inspect/certificate/index.html)
- [Kia VIN overview](https://www.kia.com/nmc/en/discover-kia/ask/what-is-a-vin.html)
- [Hyundai Australia VIN FAQ](https://www.hyundai.com/au/en/owning/myhyundaicare/faq)
