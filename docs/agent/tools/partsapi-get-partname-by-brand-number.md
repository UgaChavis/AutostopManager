# partsapi.getPartnameByBrandNumber — getPartnameByBrandNumber

Вызвать getPartnameByBrandNumber; Part-name lookup by aftermarket brand and article number; article enrichment only.

Основной модуль: [E3](../modules/E3.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: partsapi_ru; первичная база: declared_by_provider; VIN/TecDoc/maintenance/AUTONORMS namespaces. Исполнение: network_read.

Вызов: native Manager MCP `partsapi_catalog_lookup`.
operation: `part_name_by_brand_number`.
api_method: `getPartnameByBrandNumber`.

Входы: `brand`, `part_number`, `lang`.
Defaults: `{"lang":"ru"}`.
Обязательные facade поля: `operation`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"brand":"DEMO","dry_run":true,"operation":"part_name_by_brand_number","part_number":"DEMO-ARTICLE-001"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"operation":[]}
```

Выход: source/parser/outcome и raw payload; специализированные profiles/tree/article/labor данные только для распознанных методов; generic payload сохраняется без выдуманных нормализованных полей.
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- Маркировка/EAN/наименование сами не становятся OEM.
- API method отличается от facade operation; key назначает сервер, secrets не принимаются.
- TecDoc/maintenance/AUTONORMS ID namespace не взаимозаменяемы; указанные IDs в примере синтетические.

Подробный контракт: [справочник](../references/partsapi.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
