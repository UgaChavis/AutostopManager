# CRM VIN OEM Parts Lookup Playbook

Purpose: run the AutoStop CRM workflow where a card contains a vehicle and a
VIN/frame/body number, the owner asks for a concrete part, and the manager must
find the OEM catalog number, check replacements/crosses, quote procurement and
Russian market prices, then write a structured result back to CRM.

This is the orchestration layer. Use it with:

- `docs/agent/vehicle_identity_playbook.md` for identifier classification;
- `docs/agent/parts_search_playbook.md` for sourcing, retail, and CRM
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
- If the source is weak, record `confidence: medium` or `confidence: low` and
  what is missing in the internal result, never in the public card text.
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
3. Preserve manual CRM fields. If fields conflict, do not overwrite them; keep
   the uncertainty in the internal lookup result or owner report.

## Identifier Classification

Classify before decoding.

### 17-Character VIN

- Normalize to uppercase and remove separators.
- Validate 17 characters and no `I`, `O`, `Q`.
- Use vPIC or a market-appropriate VIN decoder for base identity.
- For OEM part lookup, vPIC alone is not enough: hand off to a VIN-capable EPC,
  PartsAPI VINdecodeOE/getPartsbyVIN, 17VIN, dealer catalog, or brand catalog.

### Japanese Frame / Body Number

- Treat the frame/chassis/body number as the primary key for JDM vehicles.
- Keep the hyphenated form when the source expects it, e.g. `GXE10-0088644`.
- Do not invent a global 17-character VIN.
- Use manufacturer/Japan recall routes, epc-data, PartSouq/Amayama-style
  catalog, or brand EPC to identify the exact catalog vehicle.
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
   Use `resolve_vin_oem_parts` to expand phrases such as `передние колодки` into
   catalog search terms, critical fitment fields, quantity basis, and caveats.
3. Search OEM in source order:
   - official dealer/OEM EPC if available;
   - for MAN trucks, buses, vans, and industrial engines: MAN Service
     Portal/webMANTIS or MAN partslink24 with authorized access; do not treat
     third-party MANTIS downloads as official evidence;
   - `resolve_vin_oem_parts` for the canonical read-only identity, part-intent,
     PartsAPI candidate, enrichment, readiness, and manual-action result;
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
  PartsAPI, 17VIN, or a dealer/supplier quote.
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
  Use the matching `catalog_id` and source record from that local index.
  This is a cross/applicability layer only; MAN OEM references still require
  MAN webMANTIS/PartsBase/partslink24, a dealer, supplier, or configured EPC/API
  evidence.

If supplier/catalog APIs are not configured, use the generated
`manual_public_search_queries` only as search starting points. They intentionally
exclude raw VIN/frame values and must not be treated as OEM or закупка proof.
For public catalog or marketplace discovery, resolve `search_web_multi`, page
excerpt, and `fetch_page_browser` through `discover_raw_capabilities`, inspect
the selected schema with `get_raw_capability_schema`, and invoke it with
`call_raw_capability`. Search first, then excerpt; use the browser only for
public JS-heavy pages. Browser text can support a manual evidence trail, but
CAPTCHA, login, paywall, IP block, or private cabinet pages require
manual/approved account access and must not be bypassed.
For 10-card or broad CRM quality checks, run `benchmark_vin_parts_lookup`
before claiming coverage. It reports identity confidence,
part-intent recognition, safe public-query coverage, PartsAPI/17VIN dry-run
readiness, and the exact missing live catalog/supplier credentials while
redacting raw identifiers from output. Pass `requested_part` per item when each
card asks for a different part; use the global part only as a fallback.

## Price Flow

Use `docs/agent/parts_search_playbook.md` before writing prices.

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
   - ROSSKO, AutoEuro, Armtek, Autopiter, Exist, Autodoc, local
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
`docs/agent/crm_card_description_standard.md`. It is the only owner of public
text composition, editing, formatting and `board_summary` rules. The lookup
dossier, source evidence, confidence and price matrix remain internal.

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
the priced material row. Put only the confirmed useful selected facts allowed by
`docs/agent/crm_card_description_standard.md` into the public description. If
the selected part is genuine OEM, the row may use the OEM brand/number because
that is the priced selected part.

## Writeback Pipeline

1. Start or resume a Gateway v2 workflow when the job is multi-step.
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
8. If the owner explicitly requested material changes, resolve
   `replace_repair_order_materials` through `discover_raw_capabilities`, inspect
   its schema, and call it through `call_raw_capability` only for selected
   priced parts, not OEM references. This is not an `agent_finance_workflow`
   operation.
9. If the current state changed, update `description` and `board_summary`
   together under the canonical text standard.
10. Re-open the card and repair order with `agent_entity_context`.
11. Verify description, board summary, material totals, quantity basis, and the
    internal confidence/evidence record.
12. Complete the Gateway v2 workflow only with positive verification evidence.

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

## Provider Registry

Provider readiness and not-yet-implemented adapters live only in:

- `docs/agent/vin_oem_sources.json` for VIN/OEM/catalog/cross/applicability;
- `docs/agent/procurement_price_sources.json` for procurement, stock, and RF
  market price sources.

Use `catalog_provider_status` and `plan_oem_parts_providers` through the Gateway
v2 raw-capability route when a named workflow does not cover the task. Use
`resolve_vin_oem_parts` for exact read-only lookup and
`benchmark_vin_parts_lookup` for broad quality checks. Missing adapter or
environment readiness is a blocker, not evidence that a lookup succeeded.

No adapter may place supplier orders or change financial CRM records without a
separate explicit owner command.
