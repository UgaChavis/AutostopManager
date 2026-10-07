# manager.drom_start_parts_search — Начать bounded vendor task Drom

Начать bounded vendor task Drom

Основной модуль: [E10](../modules/E10.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: webbee; первичная база: Drom listings via Webbee. Исполнение: job_write.

Вызов: native Manager MCP `drom_start_parts_search`.

Входы: `query`, `region`, `limit`, `page_limit`, `dry_run`.
Defaults: `{"dry_run":false,"limit":50,"page_limit":3,"region":"krasnoyarsk"}`.
Обязательные facade поля: `query`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"dry_run":true,"query":"DEMO деталь"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"dry_run":true,"query":null}
```

Выход: Legacy flat: ok/source/status/search_url/limit/page_limit/task_id/uid/outcome_uncertain; optional dry_run/idempotent/progress..
Ошибки и неполнота: Legacy ok=false/source=drom/status/error/task_id/uid/outcome_uncertain; disabled/unconfigured/invalid request and uncertain provider submission are separate..

- Live start creates an external Webbee search job; queued/running status is not observed offers.
- dry_run=true creates no job; retain task_id/uid and reconcile outcome_uncertain before a new submission.

Подробный контракт: [справочник](../references/market-listings.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
