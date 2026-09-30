# PartsAPI: контракт магазина

Сценарий: [E2](../modules/E2.md). Текущий поддерживаемый контракт основан на owner export `partsapi.ru/account/shop` от 2026-09-25: 43 метода. Private export, keys, sample URLs и customer responses не помещаются в Git/docs. Код: [partsapi_methods.py](../../../autostop_manager/partsapi_methods.py), [catalog_clients.py](../../../autostop_manager/catalog_clients.py).

## Доступ и параметры

GET `https://api.partsapi.ru`: `method`, `key` и поля точного метода. Для каждого метода используется свой `PARTSAPI_*_KEY`; точное сопоставление имён — `PARTSAPI_METHOD_KEY_ENV_NAMES` в catalog_clients.py. Общий исторический `PARTSAPI_KEY` доступа не даёт. Новый `getProductGroupsByBrandNumber` использует `PARTSAPI_GET_PRODUCT_GROUPS_BY_BRAND_NUMBER_KEY`. Keys opaque: экспорт пишет String(30), примеры имеют 32 символа; не обрезай значения. Test quota экспорта — 50 вызовов/сутки/device на key; не делай bulk calls и автоматический retry после quota rejection.

Configured/status/dry_run подтверждают только конфигурацию, не live authorization. Экспорт содержит таблицы полей, не гарантированный полный response envelope. Сохраняй регистр параметров: VINdecode.lang — двухбуквенный код (ru), TecDoc lang/LANG — числовой (16 русский, 4 английский), где это определено. Generic methods принимают только документированные scalar parameters; friendly operations могут заполнять документированные defaults.

## Все 43 метода

Каждый требует key; ниже указаны дополнительные обязательные поля. «Список пуст» в export — placeholder, не input.

| Группа | Методы и поля |
| --- | --- |
| TecDoc | `getMakes(carType)`; `getModels(carType, makeId, lang)`; `getCars(makeId, carType, modelId)`; `getSearchTree(lang, carId, carType)`; `getArticles(lang, strId, carId, carType)` |
| TecDoc | `getArticle(LANG, ART_NUM, SUP_ID)`; `getArticleMedia(ART_ID, LANG)`; `getArticleCrosses(ART_ID, LANG)`; `getArticleCriteria(ART_ID, LANG)`; `getApplicability(sku, brand)`; `getApplicability2(sku)` |
| TecDoc | `getFullInfoCV(LANG, COUNTRY, CV_ID)`; `searchArticles(SEARCH_NUMBER, LANG)`; `tecdocCrosses(number)`; `getEngine(TYPE, TYPE_ID, LANG)`; `getPassengerCarInfo(carId, lang)`; `getProductGroupsByBrandNumber(brand, sku, lang)` |
| VIN | `VINdecode(vin, lang)`; `decodeVINus(vin)`; `gosnomer2vin(gosnomer)` |
| Autonorms | `GetNormsMakes()`; `GetNormsModels(makeNameSEO)`; `GetNormsMotors(modelId)`; `GetNormsTimes(motorId, TopCatId, SubCatId)`; `GetFillVolumes(carId)` |
| Maintenance | `toMakes()`; `toModels(brandId)`; `toTypes(modelId)`; `toParts(typeId)`; `toDopusk(typeId)`; `toOils(typeId)` |
| Reference | `FindEAN13(brand, number)`; `PartSuggest(oem)`; `getCrosses(number)`; `getCrossesTitle(lang, number)`; `getPartnameByBrandNumber(brand, number, lang)`; `DecodeEAN13(code)` |
| Reference | `getCrossesWithBrand(number, brand)`; `getCarsByOilSpecification(query)`; `getPartWeight(brand, number)`; `carImpoundment(number, type)`; `rusNameSuggest(firstName, midName, surName)`; `partNameSuggest(query)` |

`getProductGroupsByBrandNumber` принимает TecDoc brand, sku с его пробелами/дефисами и numeric lang. Response fields: `PT_ID`, `PT_DES`, `PT_ASM_GROUP_DES`, `PT_NORM_DES`, `SKU_TYPE` (OE/AM). Группировка не устанавливает vehicle fitment.

## VIN → кандидат

VINdecode → сверка возвращённого VIN/вариантов → подтверждённый carId и vehicle_type → getSearchTree → strId именно этого дерева → getArticles. CarType не гарантирован VINdecode: нужен `PC`, `CV` или `Motorcycle`; неизвестный type останавливает resolver до tree lookup. Перед подтверждением сверяй двигатель, выпуск, рынок, options, side/axle/position, OEM и replacement chain. TecDoc article — кандидат.

`VINdecodeOE`, `getPartsbyVIN`, `getOEApplicability` в export отсутствуют и не входят в поддерживаемый surface. Старые credentials/public pages не подтверждают доступ. [partsapi_category_index.json](../partsapi_category_index.json) хранит непроверенные числовые cat hints старого getPartsbyVIN только как legacy fixture, не активный query path.

GetNormsModels.makeNameSEO берётся из GetNormsMakes, не TecDoc make ID; последующие model/motor IDs — из того же Autonorms catalog. getArticle требует ART_NUM/SUP_ID, media/crosses/criteria — ART_ID. Для getEngine, getPassengerCarInfo и части maintenance методов response tables неполны: parsing provisional до разрешённого live ответа. Generic payload сам не доказывает OEM/fitment.

Различай transport, auth, HTTP 5xx, malformed payload и genuine empty. Ошибка не доказывает отсутствия автомобиля/детали. Provider quota и доступ проверяются bounded вызовами по поручению.
