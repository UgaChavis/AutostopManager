# partsapi.getCars — getCars

Вызвать getCars; PartsAPI getCars; use provider_parameters with the documented API parameter names.

Основной модуль: [E2](../modules/E2.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: partsapi_ru; первичная база: declared_by_provider; VIN/TecDoc/maintenance/AUTONORMS namespaces. Исполнение: network_read.

Вызов: native Manager MCP `partsapi_catalog_lookup`.
operation: `getCars`.
api_method: `getCars`.

Входы: `provider_parameters.makeId`, `provider_parameters.carType`, `provider_parameters.modelId`.
Defaults: `{}`.
Обязательные facade поля: `operation`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"operation":"getCars","dry_run":true,"provider_parameters":{"makeId":42,"carType":"PC","modelId":42}}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"operation":[]}
```

Выход: vehicle_profiles with catalog IDs, modification, engine, displacement_cc/litres, power and production bounds; uppercase and legacy aliases are checked together; multiple modifications remain candidates.
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- Модельный год отдельно от даты производства; WMI не определяет двигатель.
- API method отличается от facade operation; key назначает сервер, secrets не принимаются.
- TecDoc/maintenance/AUTONORMS ID namespace не взаимозаменяемы; указанные IDs в примере синтетические.
- CAPACITY units and YEAR_START/YEAR_END are retained; conflicting aliases or incomplete profiles do not establish an exact vehicle.

Подробный контракт: [справочник](../references/partsapi.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
