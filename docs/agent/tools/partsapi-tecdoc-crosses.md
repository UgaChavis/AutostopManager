# partsapi.tecdocCrosses — tecdocCrosses

Вызвать tecdocCrosses; PartsAPI tecdocCrosses; use provider_parameters with the documented API parameter names.

Основной модуль: E5; ссылки: нет. Состояние реализации: implemented.
Источник: partsapi_ru; первичная база: declared_by_provider; VIN/TecDoc/maintenance/AUTONORMS namespaces. Исполнение: network_read.

Вызов: native Manager MCP `partsapi_catalog_lookup`.
operation: `tecdocCrosses`.
api_method: `tecdocCrosses`.

Входы: `provider_parameters.number`.
Defaults: `{}`.
Обязательные facade поля: `operation`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"dry_run":true,"operation":"tecdocCrosses","provider_parameters":{"number":"DEMO"}}
```
Отрицательный вход:
```json
{"operation":"getArticle"}
```

Выход: source/parser/outcome и raw payload; специализированные profiles/tree/article/labor данные только для распознанных методов; generic payload сохраняется без выдуманных нормализованных полей.
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- Cross/analog/oe_reference/supersession различны; замена требует направления и первичного свидетельства.
- API method отличается от facade operation; key назначает сервер, secrets не принимаются.
- TecDoc/maintenance/AUTONORMS ID namespace не взаимозаменяемы; указанные IDs в примере синтетические.

Подробный контракт: [справочник](../references/partsapi.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
