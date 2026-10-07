# PartsAPI: контракт магазина

Карта задач: [E1](../modules/E1.md); 43 операции распределены по профильным подмодулям; exact invocation указан в едином registry. Текущий поддерживаемый контракт основан на owner export `partsapi.ru/account/shop` от 2026-09-25: 43 метода. Private export, keys, sample URLs и customer responses не помещаются в Git/docs. Код: [partsapi_methods.py](../../../autostop_manager/partsapi_methods.py), [catalog_clients.py](../../../autostop_manager/catalog_clients.py).

## Доступ и параметры

GET `https://api.partsapi.ru`: `method`, `key` и поля точного метода. Для каждого метода используется свой `PARTSAPI_*_KEY`; точное сопоставление имён — `PARTSAPI_METHOD_KEY_ENV_NAMES` в catalog_clients.py. Общий исторический `PARTSAPI_KEY` доступа не даёт. Новый `getProductGroupsByBrandNumber` использует `PARTSAPI_GET_PRODUCT_GROUPS_BY_BRAND_NUMBER_KEY`. Keys opaque: экспорт пишет String(30), примеры имеют 32 символа; не обрезай значения. Test quota экспорта — 50 вызовов/сутки/device на key; не делай bulk calls и автоматический retry после quota rejection.

Configured/status/dry_run подтверждают только конфигурацию, не live authorization. Экспорт содержит таблицы полей, не гарантированный полный response envelope. Значение возвращаемых VINdecode `vin`/`vin8` и влияние тестового режима на точность официально не подтверждены. Сохраняй регистр параметров: VINdecode.lang — двухбуквенный код (ru), TecDoc lang/LANG — числовой (16 русский, 4 английский), где это определено. Generic methods принимают только документированные scalar parameters; friendly operations могут заполнять документированные defaults.

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

VINdecode → проверка привязки ответа; запасной маршрут — getMakes → getModels → getCars, при необходимости getEngine. Для выбора модификации нужны независимые сведения клиента, фотографий или CRM о двигателе, мощности, выпуске, КПП и приводе. Данные VINdecode/getCars/getEngine одного каталога не становятся независимыми свидетельствами друг для друга. Несколько подходящих вариантов требуют конкретного параметра для уточнения, а не выбора первого. Однозначный carId и vehicle_type → getSearchTree → strId именно этого дерева → getArticles. Нужен `PC`, `CV` или `Motorcycle`; неизвестный type закрывает tree lookup. Перед подтверждением сверяй рынок, options, side/axle/position, OEM и replacement chain. TecDoc article — кандидат, не подтверждённая применимость.

`resolve_vin_oem_parts.crm_context` хранит независимые факты, включая production_year/date, modification, displacement_cc, power_kw/hp и engine_type. `tecdoc_vehicle_fallback` возвращает status, selected_profile, vehicle_profiles, missing_fields и conflicting_fields. getCars вызывается как `operation="getCars"` с точными `provider_parameters`. `max_live_calls` общий для всех методов; полный маршрут с VINdecode и деталями требует минимум 6 вызовов, с getEngine — 7, вместо прежнего default 3.

`identifier` и `provider_parameters.vin` нормализуются одинаково по регистру и пробелам. Разные значения отклоняются как `invalid_input` до HTTP, включая dry-run; при передаче только `provider_parameters.vin` он становится исходным VIN. Исходный VIN сохраняется отдельно от возвращаемого каталогом; каталожный VIN не заменяет его и не устанавливает год или комплектацию машины. Совпадение первых восьми символов не подтверждает точную идентификацию.

| Результат `vin_decode` | `ok` | `identifier_matches_request` | Действие |
| --- | --- | --- | --- |
| `success` | true | true | Продолжить проверки подбора |
| `identifier_unverified` | true | null | Закрыть точную расшифровку этим ответом; возможен независимый запасной маршрут |
| `identifier_mismatch` | false | false | Отвергнуть ответ; `failure_class=provider_identifier_mismatch`, без повтора VINdecode; возможен независимый запасной маршрут |

VIN каждой карточки проверяется вместе с VIN её родительских оболочек; все варианты регистра поля учитываются. Эхо запроса и соседние ветви не подтверждают карточку. Чужой полный VIN закрывает весь ответ для точной расшифровки, но сам по себе не доказывает конфликт автомобиля. Пустой, частичный или маскированный VIN также не подтверждает совпадение; характеристики отвергнутого ответа не переносятся в согласованный профиль.

Характеристики автомобиля из родительской оболочки сохраняются при сверке вложенной, даже если оболочка содержит только одно поле. Противоречие или другой carId нельзя скрыть выбором дочерней карточки. Поддерживаемые альтернативные имена полей берутся из той же схемы, что и нормализованный профиль; metadata и эхо запроса, включая requestParams/requestParameters, не являются карточками автомобиля.

Совпадающий VIN только во вложенном фрагменте не подтверждает родительскую карточку; чужой VIN в таком фрагменте закрывает точное использование ответа. Противоречивые carId/typeNumber/TYPE_ID, carType/CAR_TYPE или kp/kpp и превышение глубины разбора отклоняют весь ответ как `unparsed_response`, без публикации частичного успеха. Согласованные обозначения КПП сохраняют число передач.

Подборщик и benchmark используют одну сверку характеристик. Реальный конфликт двигателя, мощности, КПП, привода, марки, модели, модификации или явно указанного рынка закрывает точный подбор. Значения клиента/CRM и привязанных независимых источников сверяются независимо от порядка; спорные характеристики нельзя восстановить данными того же каталога. WMI и локальные предположения не устанавливают точный двигатель и рынок. Диапазон выпуска сверяется с годом/датой производства; модельный год хранится отдельно и не подменяет выпуск.

getCars нормализует идентификаторы, названия модификаций, объём, мощность, тип двигателя и границы выпуска. Поддерживаются YYYY, YYYYMM, YYYY-MM, YYYY/MM и HTTP/RFC-даты вида `Tue, 01 May 2018 00:00:00 GMT`; пустая граница открыта, неразобранная не подтверждает совпадение или противоречие. Отсутствующий существенный параметр остаётся поводом для уточнения.

Фактическая uppercase-форма getCars (`CAR_ID`, `CAR_NAME`, `MAKE_NAME`, `MODEL_NAME`,
`ENGINE_TYPE`, `POWER_KW`, `POWER_PS`, `YEAR_START`, `YEAR_END`) нормализуется вместе со
старыми aliases. `CAPACITY` вида `1998/2.0 l` содержит отдельные значения см³ и литров;
противоречие со старыми полями не скрывается. Несколько модификаций остаются кандидатами.

getArticle выдаёт `article_candidates[].oe_references` только из `OEM_NUMBERS` и
`article_candidates[].criteria` из `ARTICLE_CRITERIA`. Бренд, номер, подпись и единицы
сохраняются; номер самой aftermarket-статьи не становится OEM. OE-reference и размеры
не подтверждают VIN-применимость, список номеров не задаёт направление замены.

decodeVINus отделяет transport success от полноты и привязки VIN. Распознанный ответ
NHTSA/vPIC сохраняет это происхождение; другая неизвестная форма остаётся unknown.
PartsAPI transport и vPIC с одной первичной базой не дают два независимых подтверждения.
Диагностика и отсутствующая модификация сохраняют partial; чужой VIN отклоняется.

Facade `type_id` передавай строкой, например `"42"`; числовые provider IDs в generic
`provider_parameters` следуют своей схеме. `catalog_provider_status.stage` допускает
только identity, oem_catalog, catalog_cross, aftermarket_catalog, procurement_price,
market_price, market_listing либо null. Значение `oem` недопустимо.

`detail="summary"` у PartsAPI и provider status сокращает показанные коллекции до25
элементов с общим количеством и признаком обрезки, после всех проверок полного ответа.
Сырой payload не дублируется; для generic методов остаётся ограниченный preview.
`detail="full"` — совместимый default. Новые показатели выполнения не подтверждают
авторизацию провайдера или точную применимость сами по себе.

Ошибка провайдера внутри известных оболочек data/result/array/items возвращает `provider_rejected` без автоматического повтора; ошибка в metadata не является результатом операции. Недоступность одного метода не доказывает отсутствие автомобиля или конфликт его характеристик; запасной маршрут требует доступности собственных методов.

Без подтверждённого VIN выставляется `requires_exact_identifier_confirmation=true`. Поиск кандидатов требует однозначной модификации, независимого контекста, отсутствия выявленных противоречий, положительного carId и согласованного carType. `vin_fitment_confirmed=false`, ручная проверка обязательна, автоматическая запись в CRM запрещена. Transport, auth, quota, empty и unparsed сохраняют отдельные исходы.

`VINdecodeOE`, `getPartsbyVIN`, `getOEApplicability` в export отсутствуют и не входят в поддерживаемый surface. Старые credentials/public pages не подтверждают доступ. `docs/agent/partsapi_category_index.json` хранит непроверенные числовые cat hints старого getPartsbyVIN только как legacy fixture, не активный query path.

GetNormsModels.makeNameSEO берётся из GetNormsMakes, не TecDoc make ID; последующие model/motor IDs — из того же Autonorms catalog. getArticle требует ART_NUM/SUP_ID, media/crosses/criteria — ART_ID. Для getEngine, getPassengerCarInfo и части maintenance методов response tables неполны: parsing provisional до разрешённого live ответа. Generic payload сам не доказывает OEM/fitment.

Различай transport, auth, HTTP 5xx, malformed payload и genuine empty. Ошибка не доказывает отсутствия автомобиля/детали. Provider quota и доступ проверяются bounded вызовами по поручению.

## Проверки исходников

CI и release gates требуют общее покрытие проекта не ниже 82% и отдельное покрытие ветвей E2 не ниже 82%. Для E2 суммируются покрытые и все ветви `catalog_clients.py`, `vin_oem_resolver.py`, `vin_parts_benchmark.py` из полного прогона тестов. Проверка — `scripts/check-e2-branch-coverage.py`; отсутствие данных по файлу или измерения ветвей останавливает gate.
