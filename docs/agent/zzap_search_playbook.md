# ZZap Search Playbook

Purpose: use ZZap as a price-monitoring and supplier-comparison layer for
spare parts in Красноярск and other covered regions.

## When To Use

Use ZZap when the owner wants to:

- compare prices across sellers
- check whether a part has replacements or analogs
- find sellers by city, region, rating, or delivery terms
- verify whether a part is in stock now or only under order

If the result will be written into a repair order as закупочная цена, also use
`docs/agent/procurement_pricing_playbook.md` so unit/package totals are
calculated correctly.

## Core Rule

ZZap is an OEM-first search system. Treat the exact part number as the best
starting point. If the exact number is unclear, normalize the part name first
and search the cleanest OEM variant.

## Search Sequence

1. Search by exact OEM or article number.
2. Re-run with the compressed and separated number forms.
3. Narrow by region, especially Красноярск.
4. Compare `all`, `new`, and `used/discounted` search modes.
5. Open `все замены` when the exact number needs analogs.
6. Use filters for seller city, rating, delivery, sale conditions, and minimum
   order.

## Number Normalization

Search the part in all common representations:

- `86310-1G100`
- `863101G100`
- `G052182A2`
- `02E305051C`
- `7P6122101H`
- `5NN854819A9B9`

When the result is ambiguous, search the number together with the brand or
market family:

- `VAG`
- `KIA HYUNDAI`
- `VOLKSWAGEN`

## Search Modes

ZZap exposes three useful modes:

- `Любые по номеру` for the widest price comparison
- `Новые по номеру` for new-only search
- `Поиск по б/у и уценке` for used, recovered, discounted, or description-based
  search

Use the narrowest mode that still returns enough candidates.

## Filters That Matter

The most useful filters for manager work are:

- region or city
- seller proximity
- seller rating
- safety / secure deal status
- delivery to the region or to a transport company
- price range
- minimum order

If the same search is reused later, preserve the filter settings and adjust only
the number or region.

## What To Check In Results

For each candidate, capture:

- seller name
- city
- exact OEM or replacement number
- price
- delivery time
- stock status
- payment terms
- whether the listing is a replacement, analog, or exact original

## Replacement Handling

When the exact OEM is missing, use `все замены` to inspect analogs.
Cross-check the replacement against:

- vehicle generation
- engine family
- gearbox family
- left/right or front/rear fitment

Treat replacement data as informational until the seller confirms fitment.

## Reporting Format

When reporting ZZap results to the owner, keep it short:

- part requested
- exact number searched
- 3 best offers
- price spread
- seller city
- delivery estimate
- whether the offer is exact or an analog

If the owner only wants market monitoring, report the lowest price, the
fastest delivery, and the most trustworthy seller separately.

## Test Observations

Live checks showed:

- `G052182A2` returns a dense result set with multiple sellers and clear price
  spread.
- `450000115` returns clean Hyundai/Kia pricing and seller data.
- Some less common numbers may not surface well without the correct brand or
  family.
- The official help pages confirm that region, filters, and replacement views
  are the right way to narrow the search.
