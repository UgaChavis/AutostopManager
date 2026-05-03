# Procurement Pricing Playbook

Purpose: estimate realistic закупочная цена for AutoStop repair orders in
Красноярск first, then Russia-wide suppliers, without confusing unit price,
package price, sell price, or repair-order line totals.

## Trigger

Use this playbook when the owner asks for:

- закупочная цена
- цена запчастей or материалов
- наличие in Красноярск
- estimate parts for a repair order
- compare supplier offers
- prepare a purchase-oriented ЗН

Also use it before filling CRM repair-order materials when prices are not
already confirmed by an internal supplier invoice or chat.

## Core Rule

Never write a material price into a repair order until you know what the price
means:

- one piece
- one liter
- one canister
- one kit
- full required service quantity
- retail price
- wholesale/procurement price
- client sell price

If the quantity is not numeric in CRM, put the service quantity in the name and
set `quantity=1`, because CRM totals can skip text quantities such as `5 л`.

## Selected Part Rule

Before filling repair-order materials, separate these identities:

- **OEM reference**: original catalog number used to verify fitment.
- **Selected part**: the exact brand/article/package that will be bought or
  quoted to the client.
- **Price basis**: confirmed закупка, public retail upper bound, estimated
  закупка, or needs confirmation.

The material line in CRM must describe the **selected part only**. The price in
that line must belong to that selected part.

Do not write a combined line such as `BMW 34116794429 / Brembo 09.C410.13` when
the price is for Brembo. Write `Brembo 09.C410.13 диски передние, 2 шт` in the
material line, and keep `OEM reference: BMW 34116794429` in the card
description or quote matrix.

If the selected part is genuine BMW, the material line may contain the BMW
number because that is the priced selected part.

If no selected part and no confirmed price exist yet, do not present the repair
order as a payable invoice. Mark it as `предварительная смета` and list
`needs supplier confirmation`.

## Search Order

1. Identify the exact vehicle and part/fluid route:
   VIN/chassis, market, year, engine, gearbox, drivetrain, OEM number, fluid
   approval, and required service quantity.
2. Search local Krasnoyarsk availability:
   ROSSKO/Armtek/Autopiter/Emex/Exist/Autodoc/fifth-gear/local suppliers if
   accessible, Drom Красноярск, Avito Красноярск, ZZap with region, and direct
   supplier phone confirmation when web data is weak.
3. Search nearby Siberia if Krasnoyarsk is weak:
   Novosibirsk, Kemerovo, Tomsk, Irkutsk, Barnaul.
4. Search Moscow/Russia order:
   Autopiter, AutoEuro, Emex, Exist, Autodoc, Mikado/other contracted
   suppliers, AutoOpt/АвтоАльянс, MotorOil24, Toyota-specialized sellers, oil
   shops, Drom/ZZap Russia-wide, and brand-specific stores.
5. Keep international prices only as sanity checks, not as a primary Russian
   procurement price.

## Source Catalog

Open `docs/agent/procurement_price_sources.json` before choosing where to
check a price. It lists:

- supplier priority for Krasnoyarsk and Russia-wide закупка
- aliases such as `Роска`, `Роско`, `Росско`, and `ROSSKO`
- access mode: public site, supplier cabinet, price-list export, or API
- API status and the evidence URL to verify before integration
- normalized fields every quote should store

Do not rely on memory for supplier availability. The source catalog is the
first local source of truth for where to search and whether API access is
realistic.

## API and Export Integration Rules

Prefer official or contracted data paths in this order:

1. Supplier account API or supplier-approved price-list export.
2. Supplier cabinet/manual lookup with current city and price type visible.
3. Public marketplace benchmark.
4. Phone or messenger confirmation from supplier manager.

High-priority API candidates:

- ROSSKO: account keys are exposed in supplier profile according to third-party
  integration docs; confirm current official terms with the ROSSKO manager.
- AutoEuro: public API v2 documentation covers search, price/stock
  confirmation, order creation, and order status after API activation.
- Autopiter: supplier web-service route is available after account/application;
  use as Russia-wide order and wholesale benchmark candidate.
- Armtek: treat as B2B/ETP account route until API access is confirmed for
  AutoStop.
- ZZap: use public search for market comparison; partner API requires request
  and should be treated as benchmark until contract is confirmed.
- AutoSputnik/Mikado/APEC: evaluate as additional supplier API candidates, but
  confirm account, current endpoint, delivery route, returns, and commercial
  terms before treating results as закупка.
- PartsAPI/UMAPI/AUTOPOISK: evaluate for catalog, cross, VIN, fitment,
  price-list exchange, or supplier-market enrichment, but do not treat as
  local закупка unless connected supplier stock and price are available.

Never store supplier passwords or API keys in Git, docs, cards, or memory.
Expected secret names are listed in `procurement_price_sources.json`.

Do not scrape private supplier cabinets. If an API does not exist or terms are
unclear, mark the line `needs account/API confirmation` and use manual
checking.

## Query Pattern

Run several forms, not one:

- exact OEM: `08885-81060`
- compact OEM: `0888581060`
- OEM + city: `08885-81060 Красноярск`
- OEM + product: `08885-81060 Toyota LT 75W-85`
- product + approval + city: `Toyota LT 75W-85 GL-5 Красноярск`
- analog only after OEM/spec is stable: `75W-85 GL-5 JWS 2272 Красноярск`

For fluids, search both original and acceptable equivalents only after the OEM
specification is known.

## Price Extraction Rules

For each candidate, record:

- role: OEM reference, selected part, alternative, rejected, or pending
- selected brand and selected article
- OEM reference used for fitment
- source URL or supplier name
- city/warehouse
- availability: in stock, под заказ, no stock, unknown
- delivery date or lead time
- package size
- price per package
- calculated price for required service quantity
- price basis: confirmed procurement, public retail upper bound, estimate, or
  needs confirmation
- whether it is original, analog, used, old stock, damaged package, or unclear
- confidence: high / medium / low

Reject or mark low-confidence prices when:

- the listing is out of stock
- price is `0`, placeholder, old, or only visible after login
- article number conflicts with title
- package size is unclear
- source says price may change after manager confirmation
- the price looks like a unit but the line requires a kit or multiple liters

## Procurement Estimate Logic

Use three layers:

- **Local confirmed**: Krasnoyarsk in stock or supplier-confirmed pickup.
- **Russian order**: in stock or realistic order from Russia, with delivery.
- **Market sanity range**: 3-5 comparable offers used to avoid one bad price.

For a repair order, use the lowest credible закупка that can actually be
ordered, not the highest retail and not a random marketplace outlier.

If only retail public prices are visible, state: `закупка не подтверждена,
использована открытая розничная цена как верхняя граница`.

## Fluids and Packaging

Always calculate required service quantity separately from package count.

Example:

- need 4.3 L engine oil
- available 4 L + 1 L
- line name: `Масло ДВС Toyota 0W-20 SP 4+1л`
- quantity: `1`
- price: total закупка for both packages

Do not write `6 л x 12900` when `12900` is the total for 5 L + 1 L. If CRM
needs the line total, set quantity to `1`.

## CRM Output Rules

For materials in ЗН:

- keep line names short
- put the selected brand/article/package in the name if the catalog field does
  not persist
- keep OEM references and alternative cross numbers in the card description,
  not mixed into the priced material row
- put total procurement cost for a package set as `price` with `quantity=1`
- use numeric quantity only when price is truly per unit
- keep client sell price separate from закупка unless owner explicitly asks for
  selling estimate
- add a short quote matrix to the card description for nontrivial estimates:
  `operation | OEM reference | selected part | price basis | source/status`

Before finalizing, re-open the repair order and verify:

- material total equals manual sum
- every material line has one selected article/brand or is clearly marked as
  pending confirmation
- no line with text quantity was skipped
- works/materials/base/cashless totals are correct
- card description and board summary match the ЗН totals

## Mandatory Answer Shape

When reporting price work, include:

- what was priced
- whether prices are закупка, retail, or sell estimate
- local Krasnoyarsk result
- fallback Russia/Moscow result
- total materials
- weak lines requiring phone/login confirmation
- source links

## Anti-Patterns

Do not:

- mix procurement and client sale prices
- mix OEM reference and selected analog in one priced material line
- make the client guess whether the price is for original or replacement
- treat one canister as one liter
- multiply total package price by liters
- use out-of-stock discount as current закупка
- use foreign prices as Russian закупка unless importing is the actual plan
- fill a repair order without reopening it to verify calculated totals
