# Parts Search Playbook

Purpose: help the AutoStop manager source spare parts for the Красноярск
workshop quickly and consistently.

## Scope

Use this playbook when the owner asks to:

- find a spare part
- compare offers
- choose a seller
- estimate delivery
- verify whether a part fits a specific car

The manager should treat the CRM card or repair order as the source for the
vehicle context, then use public marketplaces for the market scan.
If the input is a VIN, Japanese chassis number, or other market-specific
vehicle code, decode it first with `docs/agent/vehicle_identity_playbook.md`
before starting marketplace search. If the owner wants original catalog
numbers rather than marketplace candidates, route through
`docs/agent/vin_oem_lookup_playbook.md` first.

If the owner asks for закупочная цена, local availability, repair-order
materials pricing, or cost correction, load
`docs/agent/procurement_pricing_playbook.md` before writing prices.

## Information To Collect First

Before searching, extract:

- make, model, year
- VIN or chassis number if available
- OEM number or article number
- left / right / front / rear
- new / used / contract / analog
- city or region
- target price or budget ceiling
- urgency and delivery method

If the only input is a free-text card, normalize the part name first. Example:

- `датчик кислорода` -> oxygen sensor / lambda probe
- `патрубок` -> hose / pipe / tube
- `локер` -> wheel-arch liner / fender trim
- `эмблема` -> emblem / logo badge

## Search Order

1. Search Drom first.
2. Search ZZap second for price comparison, seller coverage, and replacements.
3. Search Avito third as a manual fallback.
4. If exact part hits are sparse, widen the query with the vehicle model,
   platform, and description.
5. If the exact OEM still does not surface, search by fitment clues from the
   card and compare cross references.

## Drom Workflow

Use Drom as the primary marketplace because it is already present in CRM work
and usually gives the most useful OEM-based results for Красноярск.

Recommended query pattern:

- exact OEM number
- exact OEM number + make/model
- OEM number + `Красноярск`
- OEM number + part description

Search both the raw format and the compressed format of the number:

- `86310-1G100`
- `863101G100`
- `G052182A2`
- `02E305051C`

What to check in each listing:

- exact OEM match or known cross
- seller city
- delivery to transport company
- photo quality
- condition
- warranty / return policy
- fitment notes

Prefer listings where the title or description explicitly names the OEM
number. If Drom has no exact hit, try a broader model or platform search on the
same site.

## Avito Workflow

Use Avito as the second marketplace and manual fallback.

Recommended query pattern:

- exact OEM number
- OEM number + part description
- model + part description
- city + part name

Important note from testing: exact OEM searches for Avito are less reliable in
external web search than Drom. Treat Avito as a direct-site search task, not as
something that must be solved through search-engine snippets.

What to check in each listing:

- exact OEM or visible photo match
- city and delivery options
- seller activity and response quality
- compatibility notes
- whether the listing is a single part or a bundle

## ZZap Workflow

Use ZZap as the price and replacement layer between Drom and Avito.

Recommended query pattern:

- exact OEM number
- exact OEM number + brand family
- exact OEM number + Красноярск
- exact OEM number + part description

Search the same part in all three ZZap modes:

- all offers
- new only
- used / discounted / description-based

What to check in each listing:

- exact OEM or replacement number
- seller city and region
- delivery time
- secure deal or special conditions
- seller rating
- whether the listing is an analog or an exact part

For replacement checks, open `все замены` and compare the analog against the
vehicle generation and fitment clues from the CRM card.

## Ranking Rules

Rank candidates in this order:

1. exact OEM match
2. known cross-reference with the same platform or generation
3. visually confirmed match from photo and description
4. generic or universal part

When prices are close, prefer:

- Krasnoyarsk seller or fast local pickup
- clear photos
- explicit warranty or return terms
- a seller who names the exact part number

## Output Format

When reporting back to the owner, keep it short:

- part requested
- where you searched
- top 3 options
- price
- city
- seller
- delivery
- confidence / risk note

If the owner wants a purchase decision, give a recommendation and a backup
option.

## What To Remember

Store only the durable conclusion in manager memory:

- which part was chosen
- which seller was preferred
- what search pattern worked
- any compatibility warning that should be reused later

Do not store full listing dumps in memory.

## Test Observations

The following patterns worked in live checks:

- `863101G100` on Drom produced usable Красноярск offers and clear OEM-based
  matches.
- `G052182A2` and `02E305051C` are better searched together with the gearbox
  context, not as a raw part number only.
- Rare or awkward OEM numbers may not produce good exact hits on the first
  search, so the fallback should move to model, platform, and part description.
- Avito direct results were not reliable through external search snippets in
  the test pass, so the playbook treats Avito as a direct manual search step.
- ZZap gives the best value when used as a structured price-comparison layer
  with region and filter control, not as a raw free-text search box.
