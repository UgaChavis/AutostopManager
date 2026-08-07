# Parts Sourcing And Pricing Playbook

Canonical route for finding workshop parts, comparing suppliers and public
offers, and keeping procurement, retail and client prices distinct.

## Intake And Identity

Collect vehicle/VIN or frame, market, year, engine, transmission, part name,
side/axis/position, quantity, condition, city, urgency and any old article or
label photo. Decode market-specific identifiers through
`vehicle_identity_playbook.md`. For CRM VIN-to-OEM writeback use
`crm_vin_oem_parts_lookup_playbook.md`.

Keep these identities separate:

- `OEM reference`: fitment reference from VIN/frame catalog evidence;
- `selected part`: exact brand/article/package being quoted;
- `price basis`: confirmed procurement, public retail reference, client sale,
  or unconfirmed estimate.

Marketplace text is not OEM proof. Stabilize the OEM/reference or selected
replacement before comparing prices.

## Source Order

1. Live internal catalog/stock and contracted supplier offers when authorized.
2. Krasnoyarsk supplier cabinet/API/export or direct confirmation.
3. Drom, ZZap, EuroAuto and Avito public listings.
4. Nearby Siberia, then Russia-wide suppliers.
5. Foreign prices only as a sanity check when import is actually intended.

Supplier capabilities and secret names live only in
`procurement_price_sources.json`. Do not scrape private cabinets, bypass
CAPTCHA/login/paywalls, or infer availability from an old listing. Use
`search_web_multi` first and `fetch_page_browser` only for a public JS-heavy
page that ordinary excerpts cannot read.

For used/contract parts require exact article or physical-label match, donor
context where relevant, condition, completeness, photos, warranty, return
terms, city and delivery. Expensive or high-return-risk aggregates also need
connector/options and seller confirmation.

## Offer Comparison

For each credible offer capture only:

- selected brand/article and OEM reference used for fitment;
- source, city/warehouse, current stock, lead time and return terms;
- package size, unit/kit quantity and total required quantity;
- procurement, public retail and client sale price as separate values;
- confirmation time and confidence.

Reject zero, placeholder, stale, out-of-stock, unclear-package and mismatched
article offers. Prefer the lowest credible offer that can actually be ordered,
not the cheapest unsupported snippet. Use three to five comparable current
offers for a market range when possible.

## Quantities And CRM Materials

Never confuse one piece, liter, canister, kit or complete service quantity. If
CRM cannot total a textual quantity, put the service quantity in the material
name and use `quantity=1` with the total package cost.

A repair-order material row contains the selected priced part only, for example
`Brembo 09.C410.13 диски передние, 2 шт`. Keep an OEM reference elsewhere
unless the selected priced item is the genuine OEM part. Do not mix OEM and an
analog in one priced line.

Before any repair-order write, require the owner's exact instruction and reread
the order. After writing, verify quantities and works/materials/cashless totals.
Supplier purchase is a separate exact financial/procurement command; sourcing
never authorizes an order.

## Output And Memory

Report the requested part, selected options, price basis, city, delivery,
availability and the weak items needing confirmation. Public CRM descriptions
follow `crm_card_description_standard.md` and contain only confirmed compact
working facts; source lists and confidence stay in the owner report.

Do not store current offers, seller contacts, customer vehicle identifiers,
supplier secrets or listing dumps in Manager memory. Keep only reusable search
or compatibility lessons.
