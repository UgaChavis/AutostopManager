# PartsAPI shop contract

Source: owner-provided private export from `https://partsapi.ru/account/shop`,
captured on 2026-09-25. Its 43 distinct methods are the current PartsAPI
configuration reference. Never commit the export, test keys, sample URLs, or
customer responses.

## Access

Use GET `https://api.partsapi.ru` with query fields `method`, `key`, and
the fields required by the exact method. The export gives a separate test
key for every method and states a limit of 50 uses per day per device for
each test key. Keep keys only in private runtime configuration. The new
`getProductGroupsByBrandNumber` key maps to
`PARTSAPI_GET_PRODUCT_GROUPS_BY_BRAND_NUMBER_KEY`. Each current method
requires its own `PARTSAPI_*_KEY`; the historical shared `PARTSAPI_KEY`
does not grant access under this contract. A configured key,
`catalog_provider_status`, or `dry_run=true` shows only configuration;
it does not prove live authorization or a successful provider response.

The input tables call `key` `String(30)`, while all 43 example test keys
are 32 characters. Treat keys as opaque and never truncate them to the
table length. The export contains field tables, not complete sample JSON
responses or guaranteed top-level envelopes.

## Current 43 methods

These are the exact provider method names from the 2026-09-25 export.
Every method requires `key`; method-specific required fields follow.
`Список пуст` in two input tables is a placeholder, not a parameter.

| Group | Methods and required fields |
| --- | --- |
| TecDoc | `getMakes(carType)`; `getModels(carType, makeId, lang)`; `getCars(makeId, carType, modelId)`; `getSearchTree(lang, carId, carType)`; `getArticles(lang, strId, carId, carType)` |
| TecDoc | `getArticle(LANG, ART_NUM, SUP_ID)`; `getArticleMedia(ART_ID, LANG)`; `getArticleCrosses(ART_ID, LANG)`; `getArticleCriteria(ART_ID, LANG)`; `getApplicability(sku, brand)`; `getApplicability2(sku)` |
| TecDoc | `getFullInfoCV(LANG, COUNTRY, CV_ID)`; `searchArticles(SEARCH_NUMBER, LANG)`; `tecdocCrosses(number)`; `getEngine(TYPE, TYPE_ID, LANG)`; `getPassengerCarInfo(carId, lang)`; `getProductGroupsByBrandNumber(brand, sku, lang)` |
| VIN | `VINdecode(vin, lang)`; `decodeVINus(vin)`; `gosnomer2vin(gosnomer)` |
| Autonorms | `GetNormsMakes()`; `GetNormsModels(makeNameSEO)`; `GetNormsMotors(modelId)`; `GetNormsTimes(motorId, TopCatId, SubCatId)`; `GetFillVolumes(carId)` |
| Maintenance | `toMakes()`; `toModels(brandId)`; `toTypes(modelId)`; `toParts(typeId)`; `toDopusk(typeId)`; `toOils(typeId)` |
| Reference | `FindEAN13(brand, number)`; `PartSuggest(oem)`; `getCrosses(number)`; `getCrossesTitle(lang, number)`; `getPartnameByBrandNumber(brand, number, lang)`; `DecodeEAN13(code)` |
| Reference | `getCrossesWithBrand(number, brand)`; `getCarsByOilSpecification(query)`; `getPartWeight(brand, number)`; `carImpoundment(number, type)`; `rusNameSuggest(firstName, midName, surName)`; `partNameSuggest(query)` |

`getProductGroupsByBrandNumber` is new in this export (shop ID 99).
It takes a TecDoc brand, article `sku` including its spaces and hyphens,
and numeric TecDoc `lang` (4 English, 16 Russian). The response field
table lists `PT_ID`, `PT_DES`, `PT_ASM_GROUP_DES`, `PT_NORM_DES`,
and `SKU_TYPE` (`OE` or `AM`). Product grouping does not establish
fitment for a vehicle.

Language schemes differ by method: `VINdecode.lang` is a two-letter
code such as `ru`; TecDoc `lang` or `LANG` uses numeric codes such as
16 where specified. Preserve case in all parameter names. The adapter's
friendly operations may fill documented required fields with local defaults.
Generic methods accept only their documented scalar provider parameters.

## VIN to parts candidate route

Use `VINdecode(vin, lang)` to obtain a TecDoc `carId`. Check any returned
VIN against the requested one and resolve ambiguous variants. The export
does not guarantee `carType` in the VINdecode response: supply a confirmed
`vehicle_type` (`PC`, `CV`, or `Motorcycle`) if it is absent. The resolver
stops before the tree lookup when this type is unknown. Then call
`getSearchTree(lang, carId, carType)`, select the part's `strId` from
that vehicle's tree, and call
`getArticles(lang, strId, carId, carType)`. TecDoc articles from this
route are candidates. Verify engine, production date, market, options,
side/axle/position, OEM number, and replacement chain before confirming
fitment or writing it to a customer case.

The export does **not** include `VINdecodeOE`, `getPartsbyVIN`, or
`getOEApplicability`. Previous credentials and public method pages do
not confirm current shop access to these three routes. Exclude them from
the current supported PartsAPI surface. The local
`docs/agent/partsapi_category_index.json` holds unverified numeric
`cat` hints for the old `getPartsbyVIN` route; it is a legacy fixture,
not an active query path.

## Response and failure handling

The provider tables for `getEngine`, `getPassengerCarInfo`, and several
maintenance methods lack useful response fields. Keep their parsing
provisional until an authorized live response is inspected. Generic
methods should return the provider payload without inferring OEM parts
or confirmed fitment from arbitrary rows.

`GetNormsModels.makeNameSEO` comes from `GetNormsMakes`, not a TecDoc
make ID; model and motor IDs for subsequent Autonorms calls must come
from the same catalog. `getArticle` needs `ART_NUM` and `SUP_ID`,
while media, crosses, and criteria calls need `ART_ID`.

Keep transport failure, authorization failure, HTTP 5xx, malformed
payload, and genuine empty result distinct. None of the failure classes
proves absence of a vehicle or part. Conserve test quota: no bulk
example calls or automatic retries on quota rejection.
