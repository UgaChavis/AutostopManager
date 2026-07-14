# CRM VIN OEM Parts Lookup Playbook

Purpose: run the AutoStop CRM workflow where a card contains a vehicle and a
VIN/frame/body number, the owner asks for a concrete part, and the manager must
find the OEM catalog number, check replacements/crosses, quote procurement and
Russian market prices, then write a structured result back to CRM.

This is the orchestration layer. It does not replace:

- `docs/agent/vehicle_identity_playbook.md` for identifier classification;
- `docs/agent/vin_oem_lookup_playbook.md` for source-aware OEM routing;
- `docs/agent/procurement_pricing_playbook.md` for закупка, retail, and CRM
  material-price discipline;
- live AutoStop CRM MCP tools for cards, repair orders, clients, and files.

## Trigger

Use this playbook when the task combines CRM, vehicle identity, part identity,
OEM lookup, analogs/crosses, supplier prices, and writeback, for example:

- `В карточке VIN, найди оригинальный номер свечей и аналоги`;
- `по номеру кузова подбери колодки, закупка и рынок РФ, запиши в CRM`;
- `найди OEM фильтра по VIN, проверь кроссы и добавь результат в карточку`;
- `в карточке есть Korean VIN, нужна цена запчасти и выбранный аналог`.

If the owner only asks to decode a VIN, use `vehicle_identity_and_oem`. If the
owner already gives an OEM/article and only asks for price or availability, use
`parts_sourcing`.

## Non-Negotiable Rules

- Do not invent OEM numbers, supersessions, crosses, applicability, prices, or
  stock.
- Do not present one weak marketplace or seller source as exact OEM
  confirmation.
- Keep these identities separate: `OEM reference`, `selected part`,
  `cross/analog`, `procurement price`, `public retail/market price`, and
  `client sale price`.
- High confidence requires VIN/frame-specific confirmation plus at least one
  independent check: OEM/EPC second source, supplier fitment confirmation,
  TecDoc/CROSSBASE-style applicability, or seller confirmation with article and
  vehicle data visible.
- If the source is weak, write `confidence: medium` or `confidence: low` and
  name what is missing.
- Do not store raw customer VIN/frame, phone, client identity, CRM snapshots,
  supplier secrets, raw quotes, or full card text in durable memory or Git.

## CRM Intake

1. Start with live CRM, not memory:
   - `agent_bootstrap` if connector state is uncertain;
   - `agent_search` and `agent_entity_context` for the exact `card_id`;
   - `agent_entity_context(entity="repair_order")` only when materials will be
     updated.
2. Extract only the fields needed for the quote:
   - `card_id`, title, vehicle fields, description snippets that name the part;
   - VIN, Japanese frame/chassis number, body number, license only if needed to
     find the card;
   - make, model, market, model year or build window;
   - engine, transmission, drivetrain, body, trim/grade/options if present;
   - requested detail, side, axle, position, quantity, condition, urgency;
   - existing OEM, article, old-part label, photos/files, repair-order rows.
3. Preserve manual CRM fields. If fields conflict, do not overwrite them; add
   a short uncertainty note.

## Identifier Classification

Classify before decoding.

### 17-Character VIN

- Normalize to uppercase and remove separators.
- Validate 17 characters and no `I`, `O`, `Q`.
- Use vPIC or a market-appropriate VIN decoder for base identity.
- For OEM part lookup, vPIC alone is not enough: hand off to a VIN-capable EPC,
  Parts-Catalogs, PartsAPI VINdecodeOE/getPartsbyVIN, 17VIN, dealer catalog,
  or brand catalog.

### Japanese Frame / Body Number

- Treat the frame/chassis/body number as the primary key for JDM vehicles.
- Keep the hyphenated form when the source expects it, e.g. `GXE10-0088644`.
- Do not invent a global 17-character VIN.
- Use manufacturer/Japan recall routes, Parts-Catalogs VIN/FRAME, epc-data,
  PartSouq/Amayama-style catalog, or brand EPC to identify the exact catalog
  vehicle.
- Require model code, production date/month, engine, transmission, drive, and
  grade when the catalog splits parts by production range or option.

### Korean / KDM VIN

- Most Korean/KDM vehicles use a normal 17-character VIN, but the useful result
  is often a market-specific profile.
- Confirm Hyundai/Kia make, model family, market, production window, engine,
  transmission, and plant with a VIN/EPC source or supplier catalog.
- Do not trust generic trim names for fitment unless the source supports the
  exact KDM/Russian-market vehicle.

## OEM Lookup Flow

1. Resolve the vehicle profile from VIN/frame/body:
   `make, model, market, year/build month, engine, transmission, drivetrain,
   body/chassis, grade/options`.
2. Normalize the requested part:
   common name, catalog group, side/axis/position, quantity/unit, repair
   operation, old part markings, photo evidence.
   Use the local part-intent profile from `crm-vin-parts-plan` /
   `oem-parts-provider-plan` to expand phrases such as `передние колодки` into
   catalog search terms, critical fitment fields, quantity basis, and caveats.
3. Search OEM in source order:
   - official dealer/OEM EPC if available;
   - for MAN trucks, buses, vans, and industrial engines: MAN Service
     Portal/webMANTIS or MAN partslink24 with authorized access; do not treat
     third-party MANTIS downloads as official evidence;
   - Parts-Catalogs API/widget for VIN/FRAME and catalog groups;
   - `lookup_oem_catalog_candidates` / `oem-catalog-lookup` when
     Parts-Catalogs `catalog_id/car_id/group_id` and 17VIN `epc` are known,
     to collect OEM candidates from Parts-Catalogs, PartsAPI, and 17VIN in one
     read-only result;
   - PartsAPI `partsapi_catalog_lookup` for the normalized method set in
     `partsapi_method_contracts.md` (`PARTSAPI_KEY` + `PARTSAPI_BASE_URL`, or
     dry-run adapter check first);
   - 17VIN VIN-based part category/list/search endpoints
     (`VIN17_ACCOUNT` + `VIN17_SECRET`, or dry-run adapter check first);
   - AUTOPOISK EPC/Cross tab under subscription;
   - PartSouq/epc-data as manual fallback and visual verification;
   - supplier manager/dealer quote when online evidence is insufficient.
4. Verify applicability:
   - VIN/frame-specific vehicle is selected;
   - part group is correct;
   - side/axis/position is correct;
   - production date range includes the car;
   - engine/transmission/drivetrain/body/grade/options match;
   - quantity and kit composition are clear.
5. Check replacements/supersession:
   - old OEM -> current OEM;
   - current OEM -> allowed replacements;
   - blocked/obsolete/discontinued notes;
   - whether the replacement changes kit contents or side/axis coverage.
6. Check cross/analog:
   - TecDoc/PartsAPI article crosses/applicability;
   - CROSSBASE-style cross methods;
   - supplier catalog replacements;
   - ZZap replacements and seller notes only as market evidence until fitment
     is independently confirmed.

### MAN And Common Wear Parts

- If the card is a MAN vehicle and the request is for ТО filters, belts,
  brake wear parts, sensors, or engine service kits, first obtain the MAN
  vehicle identity and genuine reference through MAN webMANTIS/partslink24,
  Parts-Catalogs, PartsAPI, 17VIN, or a dealer/supplier quote.
- After the MAN/OEM reference is stable, use official manufacturer aftermarket
  catalogs for analog selection: MANN-FILTER, MAHLE/Knecht, Hengst, Bosch,
  Donaldson, Fleetguard, NGK/NTK, ZF Aftermarket, or TecDoc-backed catalogs.
- Verify analogs against category, engine, production range, dimensions,
  side/axis, kit content, and replacement notes. A filter cross-reference is
  not enough by itself when the service kit differs by engine, market, cab,
  wheelbase, emissions package, or build date.
- Official PDFs from MAHLE/Bosch/MANN/Donaldson-style catalogs can support a
  manual check, but keep the CRM note compact: source title, publisher, date or
  version, and checked number. Do not paste catalog tables or store raw catalog
  dumps in the card, memory, or Git.
- If `data/offline_parts_catalogs/catalog_index.json` exists, check the local
  offline cache before broad web searching:
  `rg -n "<OEM-or-article-or-engine-code>" data/offline_parts_catalogs/text`.
  Use the matching `catalog_id` and source record from
  `docs/agent/automotive_sources/source_cache/offline_parts_catalogs_knowledge_pack/`.
  This is a cross/applicability layer only; MAN OEM references still require
  MAN webMANTIS/PartsBase/partslink24, a dealer, supplier, or configured EPC/API
  evidence.

If supplier/catalog APIs are not configured, use the generated
`manual_public_search_queries` only as search starting points. They intentionally
exclude raw VIN/frame values and must not be treated as OEM or закупка proof.
For public catalog or marketplace discovery, use CRM agent `search_web_multi`
first, then ordinary excerpt. For JS-heavy public pages, the CRM agent may use
`fetch_page_browser` after that. Browser text can support a
manual evidence trail, but CAPTCHA, login, paywall, IP block, or private
cabinet pages require manual/approved account access and must not be bypassed.
For 10-card or broad CRM quality checks, run `benchmark_vin_parts_lookup` /
`vin-parts-benchmark` before claiming coverage. It reports identity confidence,
part-intent recognition, safe public-query coverage, PartsAPI/17VIN dry-run
readiness, and the exact missing live catalog/supplier credentials while
redacting raw identifiers from output. Pass `requested_part` per item when each
card asks for a different part; use the global part only as a fallback.
After a benchmark, run `build_vin_parts_work_order` / `vin-parts-work-order`
when the next step is actual lookup execution. It turns each card into a
search work order: OEM/EPC routes, prepared PartsAPI/17VIN checks,
cross/applicability steps, supplier sequence, CRM writeback gates, and
acceptance checklist.

## Price Flow

Use `docs/agent/procurement_pricing_playbook.md` before writing prices.

1. Quote only after OEM/reference identity is stable enough.
2. For every candidate, capture:
   - role: `OEM reference`, `selected part`, `cross/analog`, `rejected`,
     `pending`;
   - selected brand and article;
   - OEM reference used for fitment;
   - source, city/warehouse, stock, lead time, return terms;
   - закупка, public retail/market price, client sale price if requested;
   - package/kit/quantity basis;
   - confidence and confirmation status.
3. Procurement source order:
   - ROSSKO, AutoEuro, Armtek, Autopiter, Emex, Exist, Autodoc, local
     Krasnoyarsk suppliers when account/cabinet/API/export is available;
   - Drom/Avito/FarPost for used/contract/local urgent parts;
   - ZZap for market benchmark, replacement visibility, and average/stat price
     checks;
   - Moscow/Russia-wide suppliers when local Красноярск stock is weak.
4. Market price:
   - use 3-5 comparable current RF offers where possible;
   - exclude out-of-stock, zero, placeholder, old, damaged, unclear-kit, and
     foreign-only offers from the main average;
   - record whether the number is закупка, retail upper bound, or client sale.

## CRM Public Description

Every nontrivial CRM writeback must follow
`docs/agent/crm_card_description_standard.md`. The public card description gets
only the selected working facts, not the lookup dossier.

```markdown
🚘 **Авто:** <make model, year/build only if useful>.

**Задача:** **<requested part/work>**.

**Каталожный номер:** **++<OEM/catalog number>++**.

**Выбор:** **++<selected brand/article>++**, <quantity/price if known>.
```

Do not write source/provenance, lookup method, confidence, missing checks,
supplier-check reminders, or `Нужна проверка` blocks into the public
description. Keep source evidence and confidence in the internal owner report,
manager run, or structured lookup result when needed.

Do not put phone numbers, full client names, raw VIN dumps, or long private
source excerpts into `board_summary`.

## CRM Material Lines

Write repair-order material rows only for the selected part that has a price
basis.

Good material line:

```text
NGK 91568 свечи зажигания, комплект 4 шт
quantity=1
price=<total закупка for selected set>
```

Bad material line:

```text
Toyota 90919-01275 / NGK 91568
```

Keep OEM references, alternatives, rejected crosses, and source notes out of
the priced material row. Put only the compact selected facts allowed by
`docs/agent/crm_card_description_standard.md` into the public description. If
the selected part is genuine OEM, the row may use the OEM brand/number because
that is the priced selected part.

## Writeback Pipeline

1. Start a manager run for auditability when the job is multi-step.
2. Read the target card and repair order.
3. Build the vehicle identity and OEM lookup plan.
4. Find OEM/replacements/crosses with source evidence.
5. Quote procurement and market price.
   Use Exist only as `public_retail_reference`: source `Exist`, office `905`,
   price/lead-time/analog summary, confidence, and `requires_confirmation`.
   Do not write basket links, add-to-cart URLs, private-cabinet data, or raw
   HTML into the CRM card.
6. Build an internal quote matrix / lookup dossier.
7. Write the public card description through
   `agent_board_workflow(operation="cleanup_card")` using
   `docs/agent/crm_card_description_standard.md`, preserving old useful facts.
8. Update repair-order materials through `agent_finance_workflow` only for
   selected priced parts, not OEM references.
9. Update `board_summary` with a short plain result without VIN/client private
   data, source lists, or confidence/provenance text:
   `OEM найден, выбран NGK 91568`.
10. Re-open the card and repair order with `agent_entity_context`.
11. Verify description, board summary, material totals, quantity basis, and the
    internal confidence/evidence record.
12. Finish the manager run with verification evidence.

## Confidence

Use `high` only when:

- VIN/frame-specific catalog or supplier output confirms the OEM/reference;
- side/axis/position/date range/options are checked;
- replacement/cross selected part has independent applicability confirmation;
- price comes from supplier/account/API/export or current seller confirmation.

Use `medium` when OEM is likely but one independent check or supplier
confirmation is missing.

Use `low` when the source is generic, marketplace-only, title-match-only, or
missing VIN/frame applicability.

## Integration Backlog

Keep implementation candidates in:

- `docs/agent/vin_oem_sources.json` for VIN/OEM/catalog/cross/applicability;
- `docs/agent/procurement_price_sources.json` for закупка, stock, and RF market
  price sources.

MVP tool chain:

1. `read CRM card vehicle data`: AutoStop CRM `agent_entity_context` for card,
   repair-order, and file metadata reads.
2. `identify vehicle by VIN/frame`: `decode_vehicle_identity` first, then
   `lookup_original_parts` and Parts-Catalogs/PartsAPI/17VIN/AUTOPOISK or
   brand EPC adapters when confidence is not high.
3. `plan provider readiness`: `catalog_provider_status` and
   `plan_oem_parts_providers`; if live OEM/supplier APIs are missing, record the
   exact missing adapter/env requirement instead of pretending the lookup is
   complete.
4. `benchmark batch readiness`: `benchmark_vin_parts_lookup` when working with
   10-card or board-wide VIN/frame sets, especially before reporting that
   decoding or parts search quality improved.
5. `build per-card work orders`: `build_vin_parts_work_order` to choose exact
   OEM/EPC routes, prepared API checks, supplier sequence, writeback gates, and
   acceptance checklist per card.
6. `find OEM for requested part`: catalog group search and VIN/frame part
   lookup.
7. `find replacements/crosses`: supersession, TecDoc/CROSSBASE-style crosses,
   ZZap replacements, supplier substitutions.
8. `quote procurement and market retail prices`: normalized supplier quote
   adapters with stale-price checks.
9. `build quote matrix`: internal lookup structure for owner report and write
   decisions.
10. `write structured result to CRM card`: description, selected material rows,
    short board summary.
11. `reopen/verify CRM write`: card/reorder reread and totals check.

No adapter may place supplier orders or change financial CRM records without a
separate explicit owner command.
