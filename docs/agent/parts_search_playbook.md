# Parts Sourcing Playbook

Canonical route for spare-part search, local availability, supplier/API checks,
used or contract parts, marketplace comparison, offer scoring, and Красноярск
procurement.

## Apply when

- a CRM card or owner request names a part, OEM/article, replacement, used or
  contract unit, local stock, delivery, supplier, or price comparison;
- the part is VIN-dependent, expensive, electronic, body/aggregate, a steering
  rack, or otherwise high return-risk;
- Drom, ZZap, Avito, supplier APIs/cabinets, 2GIS/Yandex Maps, or a seller call
  is needed to establish an offer.

If the task starts with VIN/frame/body number, open
`vehicle_identity_playbook.md` and `vin_oem_lookup_playbook.md` first. A
marketplace title is never proof of an OEM number or fitment. If закупочная
price or repair-order materials are involved, also open
`procurement_pricing_playbook.md`; this playbook never authorizes a CRM write or
supplier order.

## Required input

Collect only what the task needs:

- vehicle make/model/year, market, engine/gearbox, VIN/frame when available;
- exact part/function, side/axle/position, old marking and OEM/article;
- new/remanufactured/used/contract condition, quantity/package basis;
- city, urgency, budget, delivery and return requirements.

Normalize the name before search (`датчик кислорода` -> lambda/oxygen sensor,
`локер` -> wheel-arch liner). Decode the vehicle before widening a VIN-critical
query.

## Evidence order

1. Confirm vehicle identity and source-backed OEM/supersession in an official,
   licensed, or reviewed catalog route.
2. Check approved supplier/API or current price-list sources for stock,
   procurement price, lead time, package basis, and return terms.
3. Search Drom first for OEM-based local/used offers.
4. Search ZZap second for structured price spread, sellers, replacements, and
   regional filters.
5. Search Avito third as a direct-site/manual fallback.
6. Widen by platform, model, engine, part description, photo, or marking only
   when exact-number coverage is sparse.
7. For an urgent, expensive, or high-risk choice, confirm by seller call or
   message before recommendation.

Use CRM agent `search_web_multi`, then a bounded page excerpt. Use browser
rendering only for a public JS-heavy page. Never bypass CAPTCHA, login, paywall,
IP blocks, closed cabinets, or access controls; do not reuse cookies/tokens or
mass-dump listings.

## Number and catalog checks

Search separated and compressed article forms, with brand/vehicle context when
ambiguous. Official aftermarket/TecDoc-backed catalogs can support a cross or
service-part selection after the genuine reference/vehicle profile is stable;
they are not VIN-specific OEM proof.

When the ignored local cache exists, search it before marketplace browsing:

```bash
rg -n "<OEM-or-article-or-engine-code>" data/offline_parts_catalogs/text
```

Keep the local catalog as fitment/cross evidence, not as live stock or price.

## Marketplace checks

For every candidate capture compactly:

- selected brand/article and relation to the OEM reference;
- fitment basis, condition, photos/marking, seller and city;
- stock confirmation status/time, lead time, price basis and package quantity;
- warranty/return terms, confidence, and who confirmed it.

ZZap sequence: exact article -> alternate number form -> region -> all/new/used
mode -> replacements -> seller/rating/delivery filters. Treat replacement data
as informational until vehicle applicability and seller stock are confirmed.

Listing text is not confirmed stock. Distinguish `listed`, `supplier-confirmed`,
and `seller-confirmed`; record the confirmation time. Public retail and
marketplace prices are sanity bounds unless a procurement source confirms the
actual selected offer.

## High-risk steering rack rule

Require steering type, drive-side market, VIN/frame, OEM/old label, connectors,
EPS/Servotronic/active-steering options, condition, photos, and return/warranty
terms. Separate new, remanufactured, used, contract, repair/exchange, and
unknown-condition offers. Never recommend from a title match alone. Compare
local pickup against Russia delivery plus workshop downtime.

## Ranking

Rank by fitment and operational risk, not lowest price:

1. exact OEM/verified supersession and confirmed fitment;
2. source-backed cross with vehicle-generation applicability;
3. photo/marking-supported candidate requiring final confirmation;
4. generic/title-only listing (do not recommend).

Then weigh confirmed stock, local pickup/lead time, seller reliability,
return/warranty, package basis, total delivered cost, and bay downtime.

## Result and write boundary

Return the requested part, identifiers searched, sources checked, top three
offers, price/city/delivery/condition, fitment basis, confirmation status, and a
recommended plus backup option. State blockers precisely.

Do not write full listing dumps to Manager memory. Keep only a durable compact
lesson such as the selected article, successful search pattern, or reusable
fitment warning. A repair-order material write requires a separate exact owner
command, selected part/quantity/price confirmation, dry-run, pre-state capture,
and post-write reread.

The retained source pack under
`docs/agent/automotive_sources/source_cache/ai_parts_krasnoyarsk_project_pack/`
is provenance/manifest only; this file is the active workflow.
