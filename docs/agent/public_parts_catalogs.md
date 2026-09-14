# Public parts catalogs: local capability contract

These are optional manual web routes, not new HTTP/browser adapters. Use the
existing browser or web search available to the executing agent. No catalog
download, account login, cart, order or CRM/Store write is implemented here.

## Available through existing Manager tools

- `catalog_provider_status(stage="oem_catalog")`: PartSouq/Amayama use
  `access_mode="public_site_manual"`, `manual_allowed=true`, and
  `live_callable_now=false`. `configured=true` means no credential setup is
  required for the manual route, not that a live page has responded.
- `plan_oem_parts_providers(identifier=..., requested_part=...)`: returns public
  entry links and evidence fields to capture. An empty identifier is supported
  when `vehicle_identity.vehicle_profile` supplies vehicle parameters.
- `lookup_original_parts(...)`: Japanese VIN/Frame routes include both catalogs.
  A manually captured OEM number remains preliminary; these routes do not
  authorize a fitment-confirmed offer or writeback.
- Existing `public_aftermarket_catalog_lookup(provider="fapi", brand=...,
  part_number=...)` can enrich a discovered number. Demo access is explicit;
  cross candidates are not automatic fitment confirmation.

Capture the diagram/part-page URL, original number and any supersession,
catalog group, quantity, and visible model/build-period/engine/transmission/
market/position conditions. Use the original identifier only in the intended
interactive catalog; generated public search links do not contain it.

The main behavioral rules live in
[manage-autostop-store](../../.agents/skills/manage-autostop-store/SKILL.md).
The service-case skill links there; no mandatory source sequence is introduced.

## Failure and verification boundaries

These manual routes make no network calls and add no retry loops or timeout
settings. Browser timeout, access challenge, unavailable or empty page is not
evidence that the part does not exist. Do not bypass access controls; choose
another useful source and state the remaining uncertainty.

Local tests cover route discovery, missing registry, identifier redaction,
preliminary confidence, no-network planning, existing decoder timeout fallback,
MCP read-only annotations/schema parity, and loading the current skill text
into a disposable knowledge index. They are not live catalog/fitment tests.

The standalone MCP input-schema manifest remains unchanged. Production CRM
loads a separate subset: local registration/tests do not prove current gateway
availability. Activation/release requires its own authorization and live readback.

## Emex: no automated adapter

Emex is not registered as an automated adapter in the current Manager package.
That does not prove the supplier is unavailable and does not establish that its
browser route works. If the owner explicitly authorizes manual browser use for
the current task, and an already accessible permitted browser session can access
it, treat it as a temporary research source rather than an integration. This
document neither requests, records, recovers nor supplies credentials or
sessions, and does not advertise manual access as a working automation. Cart,
order and CRM/Store writes remain outside this document.
