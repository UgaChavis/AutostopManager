# manager.catalog_provider_status — Необязательная справка о конфигурации

Необязательная справка о конфигурации

Основной модуль: [E1](../modules/E1.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: local_read.

Вызов: native Manager MCP `catalog_provider_status`.

Входы: `stage`.
Defaults: `{"stage":null}`.
Обязательные facade поля: нет; ограничения конкретной операции всё равно применяются.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"stage":[]}
```

Выход: Legacy flat JSON payload: ok/stage/providers/stage_matrix/configured_count/live_callable_count/missing_provider_ids/disabled_provider_ids.; Native MCP publishes this JSON as structuredContent and text; providers contain configuration/capability metadata without secrets..
Ошибки и неполнота: Missing or disabled provider configuration is represented in providers and missing_provider_ids/disabled_provider_ids; status does not probe provider authentication..

- Local configuration/source registry read, no HTTP.
- Configured PartsAPI keys and live_callable capability remain configured_unverified until a selected live response; no secrets are returned.

Подробный контракт: [справочник](../references/manager-runtime.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
