# Automotive Repair Source Playbook

Purpose: make AutostopManager stricter and more useful for repair diagnostics,
technical recommendations, recalls, TSBs, OEM service information, and
source-backed parts decisions.

## Source Catalog

Use the local knowledge package in `docs/agent/automotive_sources/`:

- `automotive_repair_sources_catalog.json` is the main source catalog.
- `brand_source_map.json` maps vehicle brands to preferred sources.
- `data_type_source_map.json` maps work type to preferred sources.
- `open_dataset_endpoints.json` lists legally open datasets and endpoints.

Do not update `last_verified` fields in source catalogs during documentation
hygiene unless the external source was actually checked in that pass.

## Web Research Tools

For public internet research, use the lightest route that works:

1. source catalog / local knowledge route;
2. CRM agent `search_web_multi` results for discovery;
3. CRM agent `fetch_page_excerpt` for Crawl4AI Markdown extraction with HTTP fallback;
4. CRM agent `fetch_page_browser` for public JS-heavy pages, forums, and
   marketplace pages that do not render useful text through HTTP.

`search_web_multi` can run the free local route without owner account setup:
SearXNG local metasearch -> Marginalia public API -> DuckDuckGo HTML.
The legacy paid/account providers remain available when explicitly configured:
Brave Search API, Tavily, and Google Custom Search JSON API.

`fetch_page_excerpt` uses local self-hosted Crawl4AI first. It calls Crawl4AI's
authenticated `/md` endpoint for clean Markdown, then falls back to direct HTTP
HTML cleanup if Crawl4AI is unavailable. Use `fetch_page_browser` only after
that, when the page is public but requires browser rendering.

Runtime knobs:

```bash
AUTOSTOP_SEARXNG_BASE_URL=http://127.0.0.1:8890
AUTOSTOP_MARGINALIA_ENABLED=1
AUTOSTOP_SEARCH_PROVIDER_ORDER=searxng,marginalia,duckduckgo
AUTOSTOP_SEARCH_DISABLED_PROVIDERS=brave,tavily,google_cse
AUTOSTOP_CRAWL4AI_BASE_URL=http://127.0.0.1:11235
AUTOSTOP_CRAWL4AI_ENABLED=1
```

The local SearXNG runtime uses a narrow working engine set in
`/opt/autostopcrm/data/searxng/config/settings.yml`: `bing`, `qwant`,
`privacywall`, `gmx`, `dogpile`, and `vuhuv`. Keep `duckduckgo`, `brave`,
`startpage`, `google`, `yahoo`, `mojeek`, `presearch`, and `yep` disabled there
unless live-readonly checks prove they are no longer rate-limited or blocked
from this server.

Configure only secret env vars in runtime, never in docs or Git:
`BRAVE_SEARCH_API_KEY`, `TAVILY_API_KEY`,
`GOOGLE_CUSTOM_SEARCH_API_KEY`, `GOOGLE_CUSTOM_SEARCH_CX`, and optional
`MARGINALIA_API_KEY` if a dedicated Marginalia key is issued. Crawl4AI runtime
secrets are `AUTOSTOP_CRAWL4AI_API_TOKEN` and
`AUTOSTOP_CRAWL4AI_SECRET_KEY`.

Before relying on search and page extraction quality, check readiness:

```bash
python -m autostop_manager.cli web-search-readiness --mode dry-run
python -m autostop_manager.cli web-search-readiness --mode live-readonly
python -m autostop_manager.cli web-page-readiness --mode dry-run
python -m autostop_manager.cli web-page-readiness --mode live-readonly
```

`dry-run` must never print secret values. Treat `fallback_only=true` as an
external access backlog, not a server failure: DuckDuckGo HTML still works as a
last-resort discovery path. A healthy free layer is SearXNG live-readonly ready
with Marginalia or DuckDuckGo available behind it.

Crawl4AI must stay local, authenticated, and public-URL-only. Do not pass
browser hooks, local files, private network URLs, credentials, or account-only
targets into page extraction. If extraction reports a challenge, login, IP
block, or paywall, stop and report that manual or approved account access is
required.

Browser output is evidence collection, not authority. Do not bypass CAPTCHA,
login walls, paywalls, IP blocks, robots restrictions, or private cabinets. If
the browser result reports `captcha_required`, `login_required`, `ip_blocked`,
`access_denied`, or similar flags, stop and report that manual or approved
account access is required.

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
5. `recommend_automotive_sources(data_type="transmission")`

Prefer the exact OEM service document, then transmission-manufacturer
documentation, then component-supplier documentation. For transmission work,
generic AT / CVT / DCT / MT labels are not enough to confirm applicability.

## Model-Specific Overrides

Use model-specific playbooks before broad brand routing when one exists:

- Toyota GR Yaris / Yaris GR / GXPA16 / G16E-GTS:
  `docs/agent/toyota_gr_yaris_playbook.md`

For Toyota GR Yaris, official Toyota-Tech/TIS/Toyota Manuals AU/dealer EPC
routes take priority for repair procedures, TSBs, wiring, torque specs,
calibrations, and final OEM part fitment. Public Toyota owner manuals can be
used for owner-level maintenance data, but VIN/frame, market, production date,
transmission, LSD, and grade still control final applicability.

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
