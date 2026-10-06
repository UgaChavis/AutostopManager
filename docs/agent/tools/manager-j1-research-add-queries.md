# manager.j1_research_add_queries — Добавить запросы в текущий бюджет J1

Добавить запросы в текущий бюджет J1

Основной модуль: E15; ссылки: нет. Состояние реализации: implemented.
Источник: public_web; первичная база: primary URL lineage must be preserved. Исполнение: job_write.

Вызов: native Manager MCP `j1_research_add_queries`.

Входы: `job_id`, `queries`.
Defaults: `{}`.
Обязательные facade поля: `job_id`, `queries`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"job_id":"00000000000000000000000000000000","queries":["manufacturer brake pad service bulletin"]}
```
Отрицательный вход:
```json
{"job_id":null,"queries":[]}
```

Выход: Legacy flat schema=autostop.j1.research.v1: ok/job_id/added/queries_total..
Ошибки и неполнота: ok=false/error.code: job_id_invalid/queries_invalid_or_sensitive/job_not_found/job_not_extendable/query_limit_reached/j1_busy/j1_store_unavailable..

- job_id/document_id are 32 lowercase hexadecimal characters returned by J1; synthetic example IDs demonstrate shape and do not assert existing records.
- Public, de-identified corpus only; no private VIN/contact/CRM/Telegram/secret query. Source URL lineage stays attached to documents.
- Writes additional de-identified queries to the local job; the worker may then acquire new public pages.

Подробный контракт: [справочник](../references/web-research.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
