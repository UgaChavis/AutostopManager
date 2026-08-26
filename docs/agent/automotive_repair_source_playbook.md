# Automotive Repair Source Playbook

Use this route for diagnostics, repair procedures, recalls/TSBs, technical
recommendations, and source-backed part applicability.

## Canonical Sources

- `docs/agent/automotive_sources/automotive_repair_sources_catalog.json` is the
  source catalog; derive brand and data-type views from it instead of creating
  projection files.
- `docs/agent/automotive_sources/open_dataset_endpoints.json` owns public
  dataset routes.
- Never refresh `last_verified` during documentation cleanup. An old value
  means recheck before use, not that the source remains current.

## Vehicle And Question

Before a final recommendation, establish the smallest exact context needed:

- VIN or chassis/frame, market, year, make, model, and trim;
- engine and transmission codes, drivetrain, and relevant options;
- mileage, complaint, operating condition, DTCs, scan data, and history.

If identity or configuration is incomplete, lower confidence and name the
missing facts.

## Evidence Order And Boundaries

Use evidence in this order:

1. Exact-VIN/market OEM service information and EPC.
2. Official campaigns, recalls, and regulator data.
3. Licensed professional databases.
4. Standards and manufacturer/component-supplier literature.
5. Confirmed internal service experience.

CRM proves only its recorded vehicle and service context. AutoStop App proves
only internal catalog, stock, quote, and supplier facts. Neither proves a
procedure or fitment.

`lookup_public_automotive_evidence` provides official public signals and TSB
metadata. NHTSA results are U.S. model-level evidence, not proof that a VIN has
an open campaign.

Forums, marketplace pages, copied PDFs, and related-model material can suggest
terms or hypotheses but cannot confirm torque, wiring, coding, safety steps,
fluid data, labor time, or OEM fitment.

For timing/GRM work, identify the exact engine and drive first. Confirm the
applicable procedure, timing marks, torque-plus-angle values, special tools,
and crank/cam locking; never transfer them between variants.

For gearbox, clutch, adaptation, or transmission-fluid work, use
`docs/agent/transmission_playbook.md` and the exact transmission family/code.

## Public Research

Use the local catalog first, then resolve `search_web_multi` through the normal
raw-discovery contract. Use `fetch_page_browser` only when a public JS-heavy
page does not yield useful text through HTTP. Browser output collects evidence;
it does not make a source authoritative. Stop at CAPTCHA, login, paywall, IP,
robots, or private-cabinet restrictions.

For practical public repair histories, resolve the read-only
`research_drive2_cases` capability through the same contract. Pass the complaint
and only useful vehicle context. Keep raw pages transient, use no account or
access bypass, and treat every result as a hypothesis requiring matching
vehicle context and independent evidence.

## Safety And Non-Invention

For brakes, steering, suspension, SRS, ADAS, high voltage, fuel systems,
immobilizers, keys, ECU programming, and security gateways, use only OEM or
licensed professional instructions.

Never invent torque, capacity, approval, pinout, wire color, calibration,
programming/adaptation code, labor time, or original-part price. Do not provide
immobilizer/security bypass, odometer change, emissions-delete, or safety-limit
removal instructions.

When document-level proof is unavailable, say:

`Требуется проверка по OEM-сервисной информации для конкретного VIN.`

## Evidence And Response

Attach `source_id`, `document_id` or `source_url`, `document_type`, known
publication/update date, and `license_status` to technical facts. If only a
route is known, state that document-level verification remains open.

Grade confidence:

- High: exact VIN plus OEM/licensed confirmation.
- Medium: official evidence matches year/model/engine, but VIN is unconfirmed.
- Low: evidence is generic or market/engine/transmission is unknown.

Return a compact operational answer: vehicle, symptom, likely causes, first
checks, missing data, safety risk, evidence route/citations, and confidence.
