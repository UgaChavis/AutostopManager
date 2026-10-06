# manager.benchmark_vin_parts_lookup — benchmark_vin_parts_lookup

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; ссылки: нет. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: local_read.

Вызов: native Manager MCP `benchmark_vin_parts_lookup`.

Входы: `items`, `requested_part`, `city`, `live_vpic`, `use_vpic_batch`, `include_partsapi_dry_run`, `live_partsapi_identity`, `live_partsapi_oem`, `resolve_oem`, `max_live_calls`, `max_candidates`, `partsapi_category_index`, `partsapi_timeout`.
Defaults: `{"city":"Красноярск","include_partsapi_dry_run":true,"live_partsapi_identity":false,"live_partsapi_oem":false,"live_vpic":true,"max_candidates":3,"max_live_calls":3,"partsapi_category_index":null,"partsapi_timeout":20.0,"resolve_oem":false,"use_vpic_batch":true}`.
Обязательные facade поля: `items`, `requested_part`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"items":[],"requested_part":"передние колодки 1 комплект"}
```
Отрицательный вход:
```json
{"items":null,"requested_part":"передние колодки 1 комплект"}
```

Выход: outcome/data/evidence/missing_fields/conflicts/warnings/execution (новые helpers); legacy поля сохраняются.
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Наличие в общем native inventory не разрешает business action или чтение клиента.

Подробный контракт: [справочник](../references/manager-runtime.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
