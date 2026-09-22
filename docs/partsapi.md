# PartsAPI operations

`partsapi_catalog_lookup` supports the 43 shop methods reviewed on 2026-09-16,
plus the existing `parts_by_vin` and `oe_applicability` routes (45 total).
`catalog_provider_status` lists operation names, required parameters, API parameter
names (`provider_params`), defaults and per-method configuration presence. A configured
key remains `authorization_status=unverified` with
`readiness_basis=configuration_only`; status and dry-run output do not claim live
availability or successful authorization.

Existing friendly operation names remain supported. Additional methods use their
API name, for example `getMakes` with `provider_parameters={"carType": "PC"}`.
The optional `provider_parameters` object accepts only documented scalar API
parameters; credentials, method selection and URLs cannot be overridden there.
Use `dry_run=true` to validate without spending quota. Readiness is configuration,
not a successful live response. New generic methods return provider payloads and
do not infer OEM candidates or confirmed fitment from arbitrary rows.

`search_tree` and `articles` now send `carType`, `carId`, `lang` and (for articles)
`strId`. Existing `type_id`, `vehicle_type`, `category`, `lang_id` arguments remain.
`article` requires `part_number` and `supplier_id`; `article_id` alone no longer
satisfies this method. Article media/criteria/crosses still accept article IDs.

Method keys live only in the private runtime environment, never this document or
fixtures. Preserve working keys when adding missing test credentials. Test limits
are provider controlled; avoid bulk example calls or retries on quota rejection.
The shop export also describes non-catalog methods; configure them without making
unrequested personal-data lookups. Do not commit the export or customer responses.

VINdecodeOE normalization retains shared build date, model code, production
period, paint/trim and explicitly labelled engine/transmission/market options.
Direct shared `engine`, `engine_info`, `market` and `prodrange` attributes are
also retained. The ambiguous provider `manufactured` year is exposed separately
as `catalog_year`; it must not replace the explicit `production_date` or be
assumed to be a model year without other evidence.
It never selects a modification from an ambiguous modification list. Non-clean
vPIC results remain partial evidence; their variant/engine/transmission fields
are not promoted into the normalized vehicle profile.

`engine_info` accepts the current top-level `getEngine` list as well as legacy
envelopes, retaining engine code, capacity, power, cylinders, valves and torque.
HTTP 401/403 is `provider_auth_error`, never retried: verify
`PARTSAPI_GET_ENGINE_KEY` against the current shop export and method access.
The error suggests `vin_decode_oe` when a VIN/frame is available; it does not
silently substitute another vehicle or spend quota on an automatic VIN lookup.

For `getPartsbyVIN`, the provider documents `cat` as `String(25)`, not a
numeric-only field. E1 therefore prefers a numeric category from its local
index, but can send a short, curated text category from one recognised,
single-part intent. Arbitrary text, an unknown part and a multi-part request
remain blocked. `category_queryable` means that the input is safe to try; a
successful response still requires position and applicability checks. The
existing `type=oem` adapter default remains a local convention rather than a
provider-documented enum. An auth/5xx failure is reported as a provider failure
and falls back to manual EPC instead of being presented as no matching part.

For `norms_models`, obtain `makeNameSEO` from `norms_makes`, not a TecDoc make ID.
These codes are uppercase; surrounding whitespace and letter case are normalized
for both friendly inputs and `provider_parameters`. Lowercase codes can trigger
an upstream HTTP 5xx despite valid credentials. Model/motor IDs for further
AUTONORMS calls must come from that catalog, not TecDoc.

Retries remain opt-in (`max_attempts=2`): AUTONORMS allows at most one retry,
with a short delay. The default makes one request to conserve test quota.
Exhausted transient errors return an explicit temporary-unavailability message,
`requires_fallback=true`, and `empty_payload=false`; they do not mean that a
vehicle, service operation or part is absent.
