# manager.benchmark_vin_parts_lookup — benchmark_vin_parts_lookup

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: diagnostic. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop identity/part-intent rules and supplied context; NHTSA vPIC by default; PartsAPI only with explicit live flags. Исполнение: composed.

Вызов: native Manager MCP `benchmark_vin_parts_lookup`.

Входы: `items`, `requested_part`, `city`, `live_vpic`, `use_vpic_batch`, `include_partsapi_dry_run`, `live_partsapi_identity`, `live_partsapi_oem`, `resolve_oem`, `max_live_calls`, `max_candidates`, `partsapi_category_index`, `partsapi_timeout`.
Defaults: `{"city":"Красноярск","include_partsapi_dry_run":true,"live_partsapi_identity":false,"live_partsapi_oem":false,"live_vpic":true,"max_candidates":3,"max_live_calls":3,"partsapi_category_index":null,"partsapi_timeout":20.0,"resolve_oem":false,"use_vpic_batch":true}`.
Обязательные facade поля: `items`, `requested_part`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"items":[],"requested_part":"передние колодки 1 комплект"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"items":null,"requested_part":"передние колодки 1 комплект"}
```

Выход: Обезличенный контрольный отчёт: mode, summary, items; identity/part-intent, prepared/live calls, warnings и readiness отдельно..
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- По умолчанию live_vpic=true: подходящий VIN/WMI может вызвать публичный HTTP vPIC; live_vpic=false отключает этот acquisition.
- PartsAPI live identity/OEM требуют явных live_partsapi_identity/live_partsapi_oem; include_partsapi_dry_run готовит план, не подтверждая доступ или применимость.
- Составляет локальные правила, переданный контекст и разрешённые acquisition stages; не пишет CRM/Store, отчёт скрывает исходные идентификаторы.
- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Наличие в общем native inventory не разрешает business action или чтение клиента.

Подробный контракт: [справочник](../references/manager-runtime.md).
