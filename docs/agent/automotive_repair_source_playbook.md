# Automotive Repair Source Playbook

Purpose: make AutostopManager stricter and more useful for repair diagnostics,
technical recommendations, recalls, TSBs, OEM service information, and
source-backed parts decisions.

## Source Catalog

Use the local knowledge package in `docs/agent/automotive_sources/`:

- `docs/agent/automotive_sources/automotive_repair_sources_catalog.json` is the
  main source catalog.
- Brand and data-type routes are derived from each catalog record's `brands`
  and `data_types`; do not maintain duplicate projection files.
- `docs/agent/automotive_sources/open_dataset_endpoints.json` lists legally
  open datasets and endpoints.

Do not update `last_verified` fields in source catalogs during documentation
hygiene unless the external source was actually checked in that pass.
Treat an old `last_verified` value as a prompt to recheck the source before
operational use, not as proof that the route is still current.

## Intelligent Source Selection

Start with the technical question and choose evidence adaptively rather than
following a fixed sequence:

- Use live CRM only when an identified card or its vehicle/repair-order context
  answers part of the question. It is not a substitute for service literature.
- Use AutoStop App only for internal catalog, stock, price, supplier, or quote
  facts. It does not prove vehicle applicability or a repair procedure.
- Use VIN/frame and OEM/EPC routes when identity, configuration, part
  applicability, production split, engine, or transmission must be proven.
- Use `lookup_public_automotive_evidence` for compact official public recall
  signals and manufacturer-communication/TSB metadata. NHTSA results are
  U.S. model-level evidence, not confirmation of an open campaign for a VIN.
- Use public web search and profile forums to discover symptom patterns,
  terminology, publications, and competing hypotheses. Keep the evidence
  boundary below: a forum is not final authority for procedures or safety data.
- Use OEM or legally licensed service information for final torque, timing,
  repair steps, wiring, fluid approval/capacity, programming, ADAS, SRS, HV,
  and exact-fitment conclusions.

For timing/ГРМ questions, establish the exact engine and timing drive first,
then seek the applicable procedure, timing marks/phases, torque-plus-angle
values, special tools, and any crank/cam locking requirements. Never transfer
values between engine variants or infer a procedure from a visual forum post.

## Web Research Tools

For public internet research, use the lightest route that works:

1. source catalog / local knowledge route;
2. resolve `search_web_multi` and page excerpt through
   `discover_raw_capabilities` -> `get_raw_capability_schema` ->
   `call_raw_capability`;
3. resolve `fetch_page_browser` through the same route only for public
   JS-heavy pages, forums, and marketplace pages that do not render useful text
   through HTTP.

`search_web_multi` tries configured providers in order:
Brave Search API -> Tavily -> Google Custom Search JSON API -> DuckDuckGo HTML.
Configure only secret env vars in runtime, never in docs or Git:
`BRAVE_SEARCH_API_KEY`, `TAVILY_API_KEY`,
`GOOGLE_CUSTOM_SEARCH_API_KEY`, `GOOGLE_CUSTOM_SEARCH_CX`.

Browser output is evidence collection, not authority. Do not bypass CAPTCHA,
login walls, paywalls, IP blocks, robots restrictions, or private cabinets. If
the browser result reports `captcha_required`, `login_required`, `ip_blocked`,
`access_denied`, or similar flags, stop and report that manual or approved
account access is required.

## Drive2 Practical Repair Cases

When the owner asks for real repair histories, recurring symptoms, or the
practical sequence that led to a confirmed result, prefer the hidden read-only
`research_drive2_cases` capability. Resolve it through
`discover_raw_capabilities` -> `get_raw_capability_schema` ->
`call_raw_capability`; never invoke it directly.

Pass the actual complaint plus the smallest useful vehicle context: model,
engine code, transmission, and DTCs when known. The route issues at most three
public `site:drive2.ru/l/` searches, reads at most five candidate journals
sequentially, deduplicates URLs, joins only compact case evidence, and keeps a
bounded 15-minute in-process cache. It never uses an account, does not bypass
login/CAPTCHA/IP restrictions, and does not persist raw journal pages.

Each returned case is a hypothesis card: title/URL, vehicle and date hints,
short evidence excerpts, relevance score, and separate article/comment access
status. `comments_limited=true` does not invalidate a public article; require a
human only when the article itself cannot be read or a real access block is
reported. Do not turn a Drive2 narrative into a confirmed diagnosis without
matching vehicle/aggregate context and independent evidence.

## Required Vehicle Context

Before a technical recommendation, extract from CRM or ask for:

- VIN or chassis/frame number
- year, make, model, trim
- engine code, displacement, fuel type
- transmission type and code
- drivetrain
- market or region
- mileage
- customer complaint
- scan results and DTCs, if available

If VIN or chassis number is missing, lower confidence and say what data is
missing.

## Source Priority

Use sources in this order:

1. OEM service information for the exact VIN, market, engine, and procedure.
2. Official recalls, campaigns, and government regulator datasets.
3. Licensed professional databases such as Bosch ESI[tronic], Autodata,
   ALLDATA, Mitchell ProDemand, MOTOR, Identifix, TecAlliance.
4. SAE and ISO standards for terminology and architecture.
5. Bosch/Springer books and training materials for theory.
6. Internal confirmed service cases and CRM experience.

Do not treat forums, pirated manuals, random PDFs, or unclear aggregators as
technical sources.

Forums can be useful for symptom patterns and next-check ideas only. They do
not confirm torque, wiring, coding, SRS/ADAS/HV, immobilizer, programming,
fluid capacity, OEM part applicability, labor time, or safety procedure facts.

## Transmission-Specific Route

For gearbox, clutch, or transmission-fluid questions, start with
`docs/agent/transmission_playbook.md`.

Required first-pass routing:

1. exact VIN or chassis number
2. market / region
3. exact transmission code or family
4. exact symptom and operating condition
5. Gateway v2 raw discovery, schema lookup, then
   `call_raw_capability` for `recommend_automotive_sources` with
   `data_type="transmission"` (or the equivalent local CLI)

Prefer the exact OEM service document, then transmission-manufacturer
documentation, then component-supplier documentation. For transmission work,
generic AT / CVT / DCT / MT labels are not enough to confirm applicability.

## Do Not Invent

Never invent:

- torque specs
- fluid capacities
- oil approvals
- connector pinouts
- wire colors
- ADAS calibration procedures
- SRS procedures
- HV procedures
- adaptation or programming codes
- labor times
- original part prices

When source-backed data is unavailable, use this phrase:

`Требуется проверка по OEM-сервисной информации для конкретного VIN.`

## Safety-Critical Areas

For brakes, steering, suspension, SRS, ADAS, HV, fuel systems, immobilizer,
keys, ECU programming, and security gateway work, only use OEM or licensed
professional sources.

Do not provide bypass instructions for immobilizers, security systems,
odometer changes, emissions deletes, or removal of safety limits.

## Citation Rule

Every technical fact from a database or source should carry:

- `source_id`
- `document_id` or `source_url`
- `document_type`
- publication or update date, if known
- `license_status`

If only a source route is known, say that the fact still needs document-level
verification.

## Confidence Rule

- High: VIN matches and OEM/licensed source confirms the fact.
- Medium: year/make/model/engine match and the source is official, but VIN is
  not confirmed.
- Low: only generic data is available, or market, engine, or transmission is
  unknown.

## Response Shape

For manager-facing recommendations, include:

- vehicle
- symptoms
- likely causes
- first checks
- missing data
- safety risk
- source route or citations
- confidence

Keep the answer operational. Use the source catalog to decide where to check
next before giving technical details.
