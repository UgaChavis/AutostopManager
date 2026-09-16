# PartsAPI operations

`partsapi_catalog_lookup` supports the 43 shop methods reviewed on 2026-09-16,
plus the existing `parts_by_vin` and `oe_applicability` routes (45 total).
`catalog_provider_status` lists operation names, required parameters, API parameter
names (`provider_params`), defaults and per-method credential readiness.

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
It never selects a modification from an ambiguous modification list. Non-clean
vPIC results remain partial evidence; their variant/engine/transmission fields
are not promoted into the normalized vehicle profile.
