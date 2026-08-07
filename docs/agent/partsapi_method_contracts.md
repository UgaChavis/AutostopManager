# PartsAPI Method Contracts

Canonical adapter: `partsapi_catalog_lookup`. Live calls require
`PARTSAPI_BASE_URL=https://api.partsapi.ru` plus either global `PARTSAPI_KEY` or
a method-specific key. Never persist real keys, raw VIN/frame values, or raw
provider payloads in docs/tests/CRM. For free cabinet testing, method-specific
keys are supported:
`PARTSAPI_VINDECODE_KEY`, `PARTSAPI_VINDECODE_OE_KEY`,
`PARTSAPI_GOSNOMER2VIN_KEY`,
`PARTSAPI_PARTS_BY_VIN_KEY`, `PARTSAPI_OE_APPLICABILITY_KEY`,
`PARTSAPI_CROSSES_KEY`, `PARTSAPI_CROSSES_WITH_BRAND_KEY`,
`PARTSAPI_CROSSES_TITLE_KEY`, `PARTSAPI_ARTICLE_CROSSES_KEY`,
`PARTSAPI_PARTNAME_BY_BRAND_NUMBER_KEY`,
`PARTSAPI_SEARCH_ARTICLES_KEY`, `PARTSAPI_GET_ENGINE_KEY`,
`PARTSAPI_SEARCH_TREE_KEY`, `PARTSAPI_ARTICLES_KEY`, `PARTSAPI_ARTICLE_KEY`,
`PARTSAPI_ARTICLE_CRITERIA_KEY`, `PARTSAPI_GET_NORMS_MAKES_KEY`,
`PARTSAPI_GET_NORMS_MODELS_KEY`, `PARTSAPI_GET_NORMS_MOTORS_KEY`, and
`PARTSAPI_GET_NORMS_TIMES_KEY`, and `PARTSAPI_GET_FILL_VOLUMES_KEY`.

The key is selected by the exact method: for example, `VINdecode` uses
`PARTSAPI_VINDECODE_KEY`. A missing global key alone is not a configuration
failure when that method key is present. Provider/auth/config blocks and empty
candidate sets are `inconclusive`, not evidence that a part does not exist or
fits the vehicle.

| operation | PartsAPI method | required input | normalized output | notes |
| --- | --- | --- | --- | --- |
| `vin_decode` | `VINdecode` | `identifier`, `lang` default `ru` | `vehicle_profiles` | TecDoc/TecRMI identity, `carId`, make/model/engine/year routing evidence. |
| `vin_decode_oe` | `VINdecodeOE` | `identifier` | `vehicle_profiles` | OE-catalog vehicle identity for VIN or frame/chassis numbers. |
| `plate_to_vin` | `gosnomer2vin` | `registration_number` as `gosnomer` | provider result / `vehicle_profiles` | Russian registration-number lookup. Treat returned VIN as an identity lead: verify it against the vehicle/CRM before any write. |
| `parts_by_vin` | `getPartsbyVIN` | `identifier`, `category`; `part_type` defaults to `oem` | `oem_candidates` | Live lookup requires numeric `cat` id. Use `part_type=omit`/`non-oem` to skip the `type` query parameter for non-original parts. Text candidates from `parts_intent` are routing hints and produce `category_unresolved`. Use bounded `timeout`/`max_attempts` for slow live reads. |
| `oe_applicability` | `getOEApplicability` | `part_number` as `query` | provider result / `empty_payload` | Extra validation only. Empty `null`, `[]`, or `{}` is no applicability evidence, not fitment proof. |
| `crosses` | `getCrosses` | `part_number` as `number` | `cross_candidates` | Brandless replacement/cross lookup. Never promote to OEM proof. |
| `crosses_with_brand` | `getCrossesWithBrand` | `part_number` as `number`, `brand` | `cross_candidates` | Branded replacement/cross lookup. Never promote to OEM proof. |
| `crosses_title` | `getCrossesTitle` | `part_number` as `number`, `lang` default `ru` | `cross_candidates` | Replacement/cross lookup with localized `partname`. Never promote to OEM proof. |
| `part_name_by_brand_number` | `getPartnameByBrandNumber` | `brand`, `part_number` as `number`, `lang` default `ru` | `article_candidates` | Brand/article name enrichment. It identifies an article; it does not prove vehicle applicability. |
| `article_crosses` | `getArticleCrosses` | `article_id` as `ART_ID`, `LANG` default `16` | `article_candidates` | TecDoc cross articles by article ID. Never promote to OEM proof. |
| `search_articles` | `searchArticles` | `part_number` as `SEARCH_NUMBER`, `LANG` default `16` | `article_candidates` | TecDoc article metadata and search expansion. Not VIN-specific fitment proof. |
| `engine_info` | `getEngine` | `vehicle_type` as `TYPE` default `PC`, `type_id` as `TYPE_ID`, `LANG` default `16` | `vehicle_profiles` | TecDoc engine details by vehicle modification ID. Useful after `VINdecode`/TecDoc car routing returns a modification ID. |
| `search_tree` | `getSearchTree` | `vehicle_type` as `TYPE`, `type_id` as `TYPE_ID`, `LANG` default `16` | provider result | Product group tree for a resolved vehicle modification; use to build/validate the local numeric category index. |
| `articles` | `getArticles` | `vehicle_type` as `TYPE`, `type_id` as `TYPE_ID`, `category` as `STR_ID`, `LANG` default `16` | `article_candidates` | Articles linked to one product tree group; enrichment only unless VIN-specific OEM evidence exists. |
| `article` | `getArticle` | `article_id` as `ART_ID`, `LANG` default `16` | `article_candidates` | Full article details by TecDoc article id; enrichment only. |
| `article_criteria` | `getArticleCriteria` | `article_id` as `ART_ID`, `LANG` default `16` | `article_candidates` | Article characteristics/criteria by TecDoc article id; enrichment only. |
| `norms_makes` | `GetNormsMakes` | none | provider result | AUTONORMS makes and `makeNameSEO` identifiers. |
| `norms_models` | `GetNormsModels` | `make_name_seo` as `makeNameSEO` | provider result | AUTONORMS models and `modelId` identifiers for one make. |
| `norms_motors` | `GetNormsMotors` | `model_id` as `modelId` | provider result | AUTONORMS engine modifications and `motorId` identifiers for one model. |
| `norms_times` | `GetNormsTimes` | `motor_id`, `top_category_id`, `sub_category_id` | provider result | AUTONORMS work rows and `workTime`; norm-hours are an evidence layer, not a final service price. |
| `fill_volumes` | `GetFillVolumes` | `car_id` as `carId` | `fill_volumes` | AUTONORMS fluid volume/type/unit rows for a vehicle modification. Verify fluid approval separately before selecting oil or coolant. |

## Operational Routes

### Registration number to a verified vehicle identity

Call `plate_to_vin` with `registration_number` only for a read-only lookup.
The provider parameter is `gosnomer`; the response may contain a VIN. Treat an
empty payload as no provider result, not a negative identity assertion. Treat a
returned VIN as a lead: compare it against the physical vehicle and exact CRM
vehicle/card context before any CRM write. Keep plates and VINs out of docs,
durable memory, Git, and broad logs.

### Labor-time evidence for a price estimate

Use the four AUTONORMS operations as a constrained chain:

`norms_makes` -> `norms_models` -> `norms_motors` -> `norms_times`.

`GetNormsTimes` requires the `motorId` returned by the selected modification
and numeric `TopCatId`/`SubCatId`. Its category identifiers come from the
provider-linked AUTONORMS category source, not the PartsAPI product-group index
used by `getPartsbyVIN`. Match work rows to the requested scope and avoid
double-counting shared remove/install operations. `workTime` is evidence for
duration and effective hourly-rate plausibility; `workPrice` is not an AutoStop
selling price. See `work_labor_pricing_playbook.md` for the final market-price
calculation.

### Fluid volumes for maintenance estimates

Use `fill_volumes` only after the exact vehicle modification has yielded its
provider `carId`. Return `fillVolume`, `fillUnit`, `fillType`, `fillTitle`, and
`fillInfo` as a separate fluid-evidence block. It supports quantities in a ТО
estimate, but does not itself confirm an oil approval, product selection, or
final material price.

### Part-name and cross enrichment

Use `part_name_by_brand_number` for a brand/article label and `crosses_title`
for localized names of cross candidates. Both methods are aftermarket metadata:
they do not establish OEM status or VIN-specific fitment. Provider HTTP 5xx,
an empty response, or test-key quota exhaustion is `inconclusive`, not evidence
that the part or cross does not exist.

### Bounded VIN/OEM smoke

Use `partsapi-vin-smoke` for one read-only CRM-like item or an explicit
identifier/category check. Prefer `--dry-run` until the exact method key and a
numeric category are confirmed; its redacted result proves adapter behavior,
not final fitment and never authorizes CRM writeback.

## Normalized Buckets

- `vehicle_profiles`: safe vehicle summaries; include redacted identifier only.
- `oem_candidates`: VIN/frame-specific original candidates. Current source:
  `getPartsbyVIN` with `type=oem`; non-original lookups omit `type`.
- `cross_candidates`: analog/replacement rows with `fitment_confirmed=false`.
- `article_candidates`: TecDoc article metadata with `FOUND_VIA`/`found_via`,
  including `getArticleCrosses`, `getArticles`, `getArticle`, and
  `getArticleCriteria` article rows by `ART_ID`/tree group.
- `empty_payload`: true when the provider response is empty; callers must not
  interpret this as positive fitment or cross evidence.

## Durable Provider Constraints

- `getPartsbyVIN` requires a numeric group id; text categories must be resolved
  first.
- When `lookup_oem_catalog_candidates` has no candidate or no configured
  provider, it completes with `status=inconclusive` and explicit blockers; do
  not treat that as a write/transport failure or invent a numeric `cat` id.
- `getPartsbyVIN` can be slow or time out; retry metadata must stay redacted
  and callers should cap live request count.
- `getOEApplicability` is advisory: empty payloads and provider-side failures
  are not fitment evidence.
- Empty cross lists are valid provider results, not adapter failures or
  evidence that no cross exists elsewhere.
- `searchArticles` can return broad result sets and should be capped in reports.
- `getSearchTree` is the preferred source for refreshing the local
  `partsapi_category_index`; batch live refreshes must be explicit and capped.
- AUTONORMS requires the full make -> model -> motor -> category route. Keep
  `workTime` separate from the workshop hourly rate, materials, and final
  repair-order price.
