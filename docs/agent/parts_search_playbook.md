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
Resolve `search_web_multi`, page excerpt, and `fetch_page_browser` through the
Gateway v2 raw-capability route. Search first, then excerpt; use the browser
only when a public marketplace page is JS-heavy or the excerpt is empty. Do not
bypass CAPTCHA, login, paywall, IP block, or private cabinet pages; report that
manual/approved access is needed.
If the input is a VIN, Japanese chassis number, or other market-specific
vehicle code, decode it first with `docs/agent/vehicle_identity_playbook.md`
before starting marketplace search. If the owner wants original catalog
numbers rather than marketplace candidates, route through
`docs/agent/vin_oem_lookup_playbook.md` first.

Do not treat marketplace text as the source for an original catalog number.
The OEM-number workflow is: VIN decode -> catalog vehicle selection -> part
group lookup -> OEM candidate validation -> only then market price search.
For BMW/VAG, prefer legal paid/official EPC routes such as partslink24,
BMW AOS/AIR/ETK, or VAG ETKA; public EPC mirrors are fallback evidence only.
For MAN, prefer MAN Service Portal/webMANTIS or MAN partslink24 for
VIN-specific OEM references. MAHLE, Bosch, MANN-FILTER, Hengst, Donaldson,
Fleetguard, NGK/NTK, and TecDoc-backed catalogs are official aftermarket/cross
layers: useful for selecting расходники and analogs after the genuine reference
or exact vehicle profile is known, but not enough on their own to claim an OEM
number by VIN.
When `data/offline_parts_catalogs/catalog_index.json` exists, search the local
offline catalog text before marketplace browsing for filter/plug/commercial
service-part crosses:

```bash
rg -n "<OEM-or-article-or-engine-code>" data/offline_parts_catalogs/text
```

Treat local PDF/XLSX hits as source-backed fitment/cross evidence, then move to
supplier/marketplace price checks only after the OEM reference or selected
replacement remains stable.

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
3. Search EuroAuto third for used, contract, and new-part alternatives.
4. Search Avito fourth as a manual fallback.
5. If exact part hits are sparse, widen the query with the vehicle model,
   platform, and description.
6. If the exact OEM still does not surface, search by fitment clues from the
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

Use Avito as the fourth marketplace and manual fallback after Drom, ZZap, and EuroAuto.

Recommended query pattern:

- exact OEM number
- OEM number + part description
- model + part description
- city + part name

External search snippets can omit or stale Avito listing details. Treat Avito
as a direct public-site search when accessible and verify the live listing.
For AutoStop's own Avito business account, a separate official Business API
integration can later read and manage only that account's listings, chats,
orders, and account analytics after owner-approved OAuth/application access.
It does not provide a legitimate bulk catalogue of other sellers' listings;
keep competitor and marketplace sourcing on public search and never scrape,
bypass protection, or automate an unauthorised account.

What to check in each listing:

- exact OEM or visible photo match
- city and delivery options
- seller activity and response quality
- compatibility notes
- whether the listing is a single part or a bundle

## EuroAuto Workflow

Use EuroAuto as a public, read-only catalog after Drom and ZZap when a used,
contract, or new-part alternative could be useful. Search the public catalog
by exact OEM/article first; use the VIN search only to narrow the catalog, not
as proof of an OEM number. It is a market source, not a закупочная API and not
an authorization to place an order.

Check the live listing for:

- exact OEM/article and the donor vehicle or fitment notes
- condition, completeness, and photos
- delivery to Красноярск, warranty, return terms, and current availability

The site may protect automated requests. Do not bypass that protection or use
private/mobile endpoints; record the result as requiring live confirmation when
the public page cannot be read.

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

For steering racks and other high-return-risk aggregates, also require the old
part label/connector, steering or driveline options, condition, photo proof,
warranty, and return terms. Separate new, remanufactured, used, contract, and
exchange offers; a title/model match alone is not fitment proof.

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
option. If the result is written to a public CRM description, omit confidence,
risk, source, and missing-check text and follow
`docs/agent/crm_card_description_standard.md`.

## Memory Boundary

Do not store the chosen part, seller, current offer, price, listing dump, or
customer vehicle identity in manager memory. Store only a reusable search or
compatibility lesson that will improve future work.
