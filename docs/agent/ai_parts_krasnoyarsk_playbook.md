# AI Parts Krasnoyarsk Playbook

Purpose: route parts-search, local availability, supplier/API, used/contract parts, steering-rack, and offer-scoring questions into the owner-provided AI Parts Search Krasnoyarsk project pack.

## Source Pack

- Path: `docs/agent/automotive_sources/source_cache/ai_parts_krasnoyarsk_project_pack/`
- Intake source: owner-provided archive; original user download path is
  historical and not part of active routing.
- Created in pack README: `2026-05-03`
- Classification: owner-provided project knowledge pack with Markdown docs, prompts, JSON schemas, YAML configs, CSV/JSONL data, Python code skeleton, and OpenAPI draft.
- Secret boundary: the pack includes `configs/api_credentials.example.env` with placeholder names and default non-secret values only; never store real supplier/API keys in Git, docs, CRM cards, or memory.

## Use This First When

- the user asks to find, price, compare, or source spare parts in Красноярск, Krasnoyarsk region, Siberia, or Russia-wide
- the part is expensive, VIN-dependent, used/contract, body, aggregate, electronic, or high return-risk
- the user mentions steering rack / рулевая рейка / рейка, контрактная деталь, разборка, Avito, Drom, FarPost, ZZap, Rossko, Forum-Auto, Autopiter, Emex, Armtek, Laximo, TecDoc, PartsAPI, 2GIS, Yandex Maps, delivery, seller call, offer scoring, или подтверждение наличия
- the task is to design or implement a parts search gateway, normalized quote schema, scoring model, local vendor registry, or reporting template

## Source Order

1. Exact request intake: VIN/frame, year/make/model, engine, gearbox, drivetrain, part name, side, OEM number, brand/article if known, photo/marking, required condition, urgency, budget, and repair-order context.
2. Vehicle identity and OEM route: `vehicle_identity_playbook.md`, `vin_oem_lookup_playbook.md`, and catalog/VIN sources.
3. Procurement pricing route: `procurement_pricing_playbook.md` and `procurement_price_sources.json`.
4. This playbook and the local AI parts pack for workflow, scoring, schemas, source registry, local vendor discovery, API connector planning, and reporting.
5. Supplier/API/cabinet/price-list sources allowed by contract; public marketplaces only as manual/official channels and sanity checks.
6. Phone/message confirmation for urgent or expensive parts before recommending purchase.

## Pack Navigation

Core docs:

- `docs/00_executive_summary_ru.md` - concept and minimum workflow.
- `docs/01_market_analysis_ru.md` - Красноярск/Russia market structure and local-vs-order logic.
- `docs/02_data_sources_ru.md` - source matrix and trust levels.
- `docs/03_part_identity_ru.md` - VIN/OEM/brand/article/fitment normalization.
- `docs/04_search_workflows_ru.md` - local, supplier, marketplace, no-result, and escalation workflows.
- `docs/05_api_integration_plan_ru.md` - API and price-list integration plan.
- `docs/06_scoring_and_decision_model_ru.md` - offer scoring model.
- `docs/07_reporting_templates_ru.md` - manager-facing report templates.
- `docs/08_krasnoyarsk_vendor_discovery_ru.md` - local seller discovery and vendor registry workflow.
- `docs/09_compliance_limits_ru.md` - legal/terms boundaries.
- `docs/10_implementation_roadmap_ru.md` - implementation phases.
- `docs/11_data_quality_and_feedback_ru.md` - feedback loop and stale data rules.
- `docs/12_source_references_ru.md` - source references.
- `docs/13_api_connector_specifications_ru.md` - connector shape and source-specific notes.
- `docs/14_pricing_and_averaging_model_ru.md` - median/outlier/local-premium and downtime cost model.

Structured files:

- `schemas/*.schema.json` - part request, offer, seller, search report, and feedback contracts.
- `configs/*.yaml` - source registry, scoring weights, query templates, categories, and brand aliases.
- `data/*.csv` and `data/*.jsonl` - source catalog, local vendor seed, query templates, sample requests/offers, synonyms, category rules, call checklist, scoring test cases, and risk map.
- `prompts/*.md` and `PROMPT_TO_PASTE_IN_CODEX.txt` - role and task prompts for a parts sourcing agent.
- `openapi/parts_search_gateway.openapi.yaml` - internal search gateway API draft.
- `code_skeleton/` - Python skeleton for future implementation planning; not production code by itself.

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
python -m autostop_manager.cli knowledge-search "source registry scoring weights local vendor seed" --domain parts_sourcing
python -m autostop_manager.cli knowledge-search "parts search gateway OpenAPI offer schema" --domain parts_sourcing
```
