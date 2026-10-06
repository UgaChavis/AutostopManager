# manager.j1_research_start — Начать bounded исследование J1

Начать bounded исследование J1

Основной модуль: E15; ссылки: нет. Состояние реализации: implemented.
Источник: public_web; первичная база: primary URL lineage must be preserved. Исполнение: job_write.

Вызов: native Manager MCP `j1_research_start`.

Входы: `objective`, `queries`, `max_pages`, `automotive_context`, `profile`.
Defaults: `{"automotive_context":null,"max_pages":300,"profile":"general","queries":null}`.
Обязательные facade поля: `objective`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"max_pages":3,"objective":"Открытые источники по замене тормозных колодок","queries":["manufacturer brake pad replacement documentation"]}
```
Отрицательный вход:
```json
{"objective":null}
```

Выход: Legacy flat schema=autostop.j1.research.v1: ok/job_id/status=queued/profile/max_pages/query_count/automotive_context..
Ошибки и неполнота: ok=false/error.code: objective_invalid_or_sensitive/queries_or_objective_invalid/queries_or_automotive_context_invalid/max_pages_invalid/j1_busy/j1_store_unavailable..

- Writes a durable local public-research job; a background worker later searches/fetches public pages.
- queued is not completed evidence. Supply nonempty queries or a valid automotive profile/context; starting a second active job is refused.

Подробный контракт: [справочник](../references/web-research.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
