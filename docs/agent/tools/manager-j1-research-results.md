# manager.j1_research_results — Результаты собственного J1 job

Результаты собственного J1 job

Основной модуль: E15; ссылки: нет. Состояние реализации: implemented.
Источник: public_web; первичная база: primary URL lineage must be preserved. Исполнение: job_read.

Вызов: native Manager MCP `j1_research_results`.

Входы: `job_id`, `query`, `cursor`, `limit`.
Defaults: `{"cursor":0,"limit":20,"query":""}`.
Обязательные facade поля: `job_id`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"job_id":"00000000000000000000000000000000","limit":10}
```
Отрицательный вход:
```json
{"job_id":null}
```

Выход: Legacy flat schema=autostop.j1.research.v1: ok/job_id/query/total/cursor/next_cursor/results[]..
Ошибки и неполнота: ok=false/error.code: job_id_invalid/pagination_invalid/query_invalid_or_sensitive/job_not_found/j1_store_unavailable..

- job_id/document_id are 32 lowercase hexadecimal characters returned by J1; synthetic example IDs demonstrate shape and do not assert existing records.
- Public, de-identified corpus only; no private VIN/contact/CRM/Telegram/secret query. Source URL lineage stays attached to documents.
- Reads/searches the saved local job corpus; cursor pagination does not acquire new public pages.

Подробный контракт: [справочник](../references/web-research.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
