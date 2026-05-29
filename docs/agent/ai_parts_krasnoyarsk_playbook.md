# AI Parts Krasnoyarsk Playbook

Purpose: route parts-search, local availability, supplier/API, used/contract parts, steering-rack, and offer-scoring questions into the active compact AutoStop parts-sourcing docs.

## Source Pack

- Path: `docs/agent/automotive_sources/source_cache/ai_parts_krasnoyarsk_project_pack/`
- Intake source: owner-provided archive; original user download path is
  historical and not part of active routing.
- Created in pack README: `2026-05-03`
- Classification: compact owner-provided project knowledge pack retained as a
  cold reference. Long workflow chapters were migrated into this playbook and
  procurement/parts catalogs; implementation drafts, prompts, schemas, configs,
  code skeleton, OpenAPI, samples, generated data, and duplicate Markdown
  chapters were removed during documentation reduction.
- Secret boundary: never store real supplier/API keys in Git, docs, CRM cards, or memory.

## Use This First When

- the user asks to find, price, compare, or source spare parts in Красноярск, Krasnoyarsk region, Siberia, or Russia-wide
- the part is expensive, VIN-dependent, used/contract, body, aggregate, electronic, or high return-risk
- the user mentions steering rack / рулевая рейка / рейка, контрактная деталь, разборка, Avito, Drom, FarPost, ZZap, Rossko, Forum-Auto, Autopiter, Emex, Armtek, Laximo, TecDoc, PartsAPI, 2GIS, Yandex Maps, delivery, seller call, offer scoring, или подтверждение наличия
- the task is to apply a repeatable sourcing, scoring, local vendor discovery, or reporting workflow

## Source Order

1. Exact request intake: VIN/frame, year/make/model, engine, gearbox, drivetrain, part name, side, OEM number, brand/article if known, photo/marking, required condition, urgency, budget, and repair-order context.
2. Vehicle identity and OEM route: `vehicle_identity_playbook.md`, `vin_oem_lookup_playbook.md`, and catalog/VIN sources.
3. Procurement pricing route: `procurement_pricing_playbook.md` and `procurement_price_sources.json`.
4. This playbook for workflow, scoring, local vendor discovery, compliance
   limits, pricing logic, and reporting. The local AI parts pack is now a
   compact cold reference for README/MANIFEST provenance only.
5. Supplier/API/cabinet/price-list sources allowed by contract; public marketplaces only as manual/official channels and sanity checks.
6. Phone/message confirmation for urgent or expensive parts before recommending purchase.

## Pack Navigation

Retained files:

- `README.md` - provenance, minimum sourcing chain, and safety boundary.
- `MANIFEST.md` - retained-file list and deletion record.

Active operational rules now live in this playbook,
`procurement_pricing_playbook.md`, `procurement_price_sources.json`,
`parts_search_playbook.md`, and `zzap_search_playbook.md`.

## Steering Rack Rule

Treat a steering rack request as high-risk and VIN-dependent:

- identify exact vehicle, steering type, side/drive market, VIN/frame, OEM number, old-part label, electrical connector, active steering/Servotronic/EPS options, and condition required;
- separate new, remanufactured, used, contract, repair/exchange, and unknown-condition offers;
- require photo/marking and seller warranty/return terms for used or contract racks;
- do not recommend purchase from title match alone;
- for urgent Красноярск jobs, compare local pickup against Russia-order cost plus bay idle/delay cost.

## Operating Rules

- Do not treat website or marketplace listing text as confirmed stock. Confirm who verified stock, when, address/warehouse, reserve possibility, and final price.
- Do not scrape closed cabinets, bypass CAPTCHA, reuse stolen cookies/tokens, or mass-dump marketplace listings.
- For every candidate offer, keep `fitment_basis`, selected brand/article, OEM reference, source, city, condition, stock status, lead time, price basis, return terms, confidence, and confirmation status.
- Recommended option is not the cheapest option; it is the lowest-risk option with acceptable fitment, availability, delivery, warranty, price, and downtime cost.
- Use public retail and marketplace data as sanity bounds unless закупка is supplier-confirmed.
- Before writing repair-order materials, follow `procurement_pricing_playbook.md`: selected part line only, clear price basis, no mixed OEM/analog priced line, and no payable invoice without confirmed selected part/price.

## Search Examples

```powershell
python -m autostop_manager.cli knowledge-probe "найти рулевую рейку в Красноярске цена наличие контрактная"
python -m autostop_manager.cli knowledge-search "рейка Красноярск vendor discovery offer scoring call confirmation" --domain parts_sourcing
```
