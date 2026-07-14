# PartsAPI Method Contracts

Canonical adapter: `partsapi_catalog_lookup`. Live calls require
`PARTSAPI_BASE_URL=https://api.partsapi.ru` plus either global `PARTSAPI_KEY` or
a method-specific key. Never persist real keys, raw VIN/frame values, or raw
provider payloads in docs/tests/CRM. For free cabinet testing, method-specific
keys are supported:
`PARTSAPI_VINDECODE_KEY`, `PARTSAPI_VINDECODE_OE_KEY`,
`PARTSAPI_PARTS_BY_VIN_KEY`, `PARTSAPI_OE_APPLICABILITY_KEY`,
`PARTSAPI_CROSSES_KEY`, `PARTSAPI_CROSSES_WITH_BRAND_KEY`,
`PARTSAPI_CROSSES_TITLE_KEY`, `PARTSAPI_ARTICLE_CROSSES_KEY`,
`PARTSAPI_SEARCH_ARTICLES_KEY`, `PARTSAPI_GET_ENGINE_KEY`,
`PARTSAPI_SEARCH_TREE_KEY`, `PARTSAPI_ARTICLES_KEY`, `PARTSAPI_ARTICLE_KEY`,
and `PARTSAPI_ARTICLE_CRITERIA_KEY`.

| operation | PartsAPI method | required input | normalized output | notes |
| --- | --- | --- | --- | --- |
| `vin_decode` | `VINdecode` | `identifier`, `lang` default `ru` | `vehicle_profiles` | TecDoc/TecRMI identity, `carId`, make/model/engine/year routing evidence. |
| `vin_decode_oe` | `VINdecodeOE` | `identifier` | `vehicle_profiles` | OE-catalog vehicle identity for VIN or frame/chassis numbers. |
| `parts_by_vin` | `getPartsbyVIN` | `identifier`, `category`; `part_type` defaults to `oem` | `oem_candidates` | Live lookup requires numeric `cat` id. Use `part_type=omit`/`non-oem` to skip the `type` query parameter for non-original parts. Text candidates from `parts_intent` are routing hints and produce `category_unresolved`. Use bounded `timeout`/`max_attempts` for slow live reads. |
| `oe_applicability` | `getOEApplicability` | `part_number` as `query` | provider result / `empty_payload` | Extra validation only. Empty `null`, `[]`, or `{}` is no applicability evidence, not fitment proof. |
| `crosses` | `getCrosses` | `part_number` as `number` | `cross_candidates` | Brandless replacement/cross lookup. Never promote to OEM proof. |
| `crosses_with_brand` | `getCrossesWithBrand` | `part_number` as `number`, `brand` | `cross_candidates` | Branded replacement/cross lookup. Never promote to OEM proof. |
| `crosses_title` | `getCrossesTitle` | `part_number` as `number`, `lang` default `ru` | `cross_candidates` | Replacement/cross lookup with localized `partname`. Never promote to OEM proof. |
| `article_crosses` | `getArticleCrosses` | `article_id` as `ART_ID`, `LANG` default `16` | `article_candidates` | TecDoc cross articles by article ID. Never promote to OEM proof. |
| `search_articles` | `searchArticles` | `part_number` as `SEARCH_NUMBER`, `LANG` default `16` | `article_candidates` | TecDoc article metadata and search expansion. Not VIN-specific fitment proof. |
| `engine_info` | `getEngine` | `vehicle_type` as `TYPE` default `PC`, `type_id` as `TYPE_ID`, `LANG` default `16` | `vehicle_profiles` | TecDoc engine details by vehicle modification ID. Useful after `VINdecode`/TecDoc car routing returns a modification ID. |
| `search_tree` | `getSearchTree` | `vehicle_type` as `TYPE`, `type_id` as `TYPE_ID`, `LANG` default `16` | provider result | Product group tree for a resolved vehicle modification; use to build/validate the local numeric category index. |
| `articles` | `getArticles` | `vehicle_type` as `TYPE`, `type_id` as `TYPE_ID`, `category` as `STR_ID`, `LANG` default `16` | `article_candidates` | Articles linked to one product tree group; enrichment only unless VIN-specific OEM evidence exists. |
| `article` | `getArticle` | `article_id` as `ART_ID`, `LANG` default `16` | `article_candidates` | Full article details by TecDoc article id; enrichment only. |
| `article_criteria` | `getArticleCriteria` | `article_id` as `ART_ID`, `LANG` default `16` | `article_candidates` | Article characteristics/criteria by TecDoc article id; enrichment only. |

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
- `getPartsbyVIN` can be slow or time out; retry metadata must stay redacted
  and callers should cap live request count.
- `getOEApplicability` is advisory: empty payloads and provider-side failures
  are not fitment evidence.
- Empty cross lists are valid provider results, not adapter failures or
  evidence that no cross exists elsewhere.
- `searchArticles` can return broad result sets and should be capped in reports.
- `getSearchTree` is the preferred source for refreshing the local
  `partsapi_category_index`; batch live refreshes must be explicit and capped.
