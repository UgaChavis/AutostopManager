# partsapi.getCrossesWithBrand — getCrossesWithBrand

Вызвать getCrossesWithBrand; Cross/replacement lookup by part number and brand.

Основной модуль: [E5](../modules/E5.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: partsapi_ru; первичная база: declared_by_provider; VIN/TecDoc/maintenance/AUTONORMS namespaces. Исполнение: network_read.

Вызов: native Manager MCP `partsapi_catalog_lookup`.
operation: `crosses_with_brand`.
api_method: `getCrossesWithBrand`.

Входы: `part_number`, `brand`.
Defaults: `{}`.
Обязательные facade поля: `operation`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"brand":"DEMO","dry_run":true,"operation":"crosses_with_brand","part_number":"DEMO-ARTICLE-001"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"operation":[]}
```

Выход: source/parser/outcome и raw payload; специализированные profiles/tree/article/labor данные только для распознанных методов; generic payload сохраняется без выдуманных нормализованных полей.
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- Cross/analog/oe_reference/supersession различны; замена требует направления и первичного свидетельства.
- API method отличается от facade operation; key назначает сервер, secrets не принимаются.
- TecDoc/maintenance/AUTONORMS ID namespace не взаимозаменяемы; указанные IDs в примере синтетические.

Подробный контракт: [справочник](../references/partsapi.md).
