# PartsAPI Method Contracts

Canonical adapter: `partsapi_catalog_lookup`. Live calls require
`PARTSAPI_BASE_URL=https://api.partsapi.ru` plus either global `PARTSAPI_KEY` or
a method-specific key. Never persist real keys, raw VIN/frame values, or raw
provider payloads in docs/tests/CRM. For free cabinet testing, method-specific
keys are supported:
`PARTSAPI_VINDECODE_KEY`, `PARTSAPI_VINDECODE_OE_KEY`,
`PARTSAPI_PARTS_BY_VIN_KEY`, `PARTSAPI_OE_APPLICABILITY_KEY`,
`PARTSAPI_CROSSES_KEY`, `PARTSAPI_CROSSES_WITH_BRAND_KEY`, and
`PARTSAPI_SEARCH_ARTICLES_KEY`.

| operation | PartsAPI method | required input | normalized output | notes |
| --- | --- | --- | --- | --- |
| `vin_decode` | `VINdecode` | `identifier`, `lang` default `ru` | `vehicle_profiles` | TecDoc/TecRMI identity, `carId`, make/model/engine/year routing evidence. |
| `vin_decode_oe` | `VINdecodeOE` | `identifier` | `vehicle_profiles` | OE-catalog vehicle identity for VIN or frame/chassis numbers. |
| `parts_by_vin` | `getPartsbyVIN` | `identifier`, `category`; `part_type` defaults to `oem` | `oem_candidates` | Live lookup requires numeric `cat` id. Text candidates from `parts_intent` are routing hints and produce `category_unresolved`. |
| `oe_applicability` | `getOEApplicability` | `part_number` as `query` | provider result / `empty_payload` | Extra validation only. Empty `null`, `[]`, or `{}` is no applicability evidence, not fitment proof. |
| `crosses` | `getCrosses` | `part_number` as `number` | `cross_candidates` | Brandless replacement/cross lookup. Never promote to OEM proof. |
| `crosses_with_brand` | `getCrossesWithBrand` | `part_number` as `number`, `brand` | `cross_candidates` | Branded replacement/cross lookup. Never promote to OEM proof. |
| `search_articles` | `searchArticles` | `part_number` as `SEARCH_NUMBER`, `LANG` default `16` | `article_candidates` | TecDoc article metadata and search expansion. Not VIN-specific fitment proof. |

## Normalized Buckets

- `vehicle_profiles`: safe vehicle summaries; include redacted identifier only.
- `oem_candidates`: VIN/frame-specific original candidates. Current source:
  `getPartsbyVIN` with `type=oem`.
- `cross_candidates`: analog/replacement rows with `fitment_confirmed=false`.
- `article_candidates`: TecDoc article metadata with `FOUND_VIA`/`found_via`.
- `empty_payload`: true when the provider response is empty; callers must not
  interpret this as positive fitment or cross evidence.

## Smoke Notes

- `getPartsbyVIN` has proven useful when a numeric group id is known.
- `getOEApplicability` can return empty payloads or provider-side failures for
  otherwise valid-looking numbers; it is advisory.
- `getCrosses` and `getCrossesWithBrand` can return empty lists for cabinet
  examples; an empty list is not an adapter failure.
- `searchArticles` can return broad result sets and should be capped in reports.
