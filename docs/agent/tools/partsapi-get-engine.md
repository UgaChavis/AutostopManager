# partsapi.getEngine — getEngine

Вызвать getEngine; TecDoc engine details and characteristics by vehicle type and modification ID.

Основной модуль: [E2](../modules/E2.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: partsapi_ru; первичная база: declared_by_provider; VIN/TecDoc/maintenance/AUTONORMS namespaces. Исполнение: network_read.

Вызов: native Manager MCP `partsapi_catalog_lookup`.
operation: `engine_info`.
api_method: `getEngine`.

Входы: `vehicle_type`, `type_id`, `lang_id`.
Defaults: `{"lang_id":16,"vehicle_type":"PC"}`.
Обязательные facade поля: `operation`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"dry_run":true,"operation":"engine_info","type_id":"42"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"operation":[]}
```

Выход: source/parser/outcome и характеристики двигателя: числовые поля — числа, диапазоны сохранены; `COOLING_TYPE` и `CYLINDER_CONSTRUCTION` доступны как `cooling_type` и `cylinder_construction`.
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- Модельный год отдельно от даты производства; WMI не определяет двигатель.
- Масштаб bore/stroke/compression сохраняется без пересчёта и догадок об единицах; характеристики каталога не подтверждают VIN/OEM fitment.
- API method отличается от facade operation; key назначает сервер, secrets не принимаются.
- TecDoc/maintenance/AUTONORMS ID namespace не взаимозаменяемы; указанные IDs в примере синтетические.

Подробный контракт: [справочник](../references/partsapi.md).
