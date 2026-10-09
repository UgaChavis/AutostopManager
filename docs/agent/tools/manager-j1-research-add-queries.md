# manager.j1_research_add_queries — Добавить запросы в текущий бюджет J1

Основной модуль: [E15](../modules/E15.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: public_web; первичная база: primary URL lineage must be preserved. Исполнение: job_write.

Вызов: native Manager MCP `j1_research_add_queries`.

Входы: `job_id`, `queries`.
Defaults: `{}`.
Обязательные facade поля: `job_id`, `queries`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"job_id":"00000000000000000000000000000000","queries":["manufacturer brake pad service bulletin"]}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"job_id":null,"queries":[]}
```

Выход: Legacy flat schema=autostop.j1.research.v1: ok/job_id/added/queries_total.; VIN jobs: schema=autostop.j1.vin_research.v1; added/query total/revision and shared remaining budget..
Ошибки и неполнота: ok=false/error.code: job_id_invalid/queries_invalid_or_sensitive/job_not_found/job_not_extendable/query_limit_reached/j1_busy/j1_store_unavailable.; VIN jobs additionally return explicit error.code for scope, expiry, private runtime and lost ephemeral state; rejected input starts no network..

- job_id/document_id are 32 lowercase hexadecimal characters returned by J1; synthetic example IDs demonstrate shape and do not assert existing records.
- General J1 remains de-identified and rejects full VIN; separate VIN jobs permit only their original exact scoped VIN before expiry. Contacts, secrets and foreign VIN remain rejected; source URL lineage stays attached.
- Writes additional de-identified queries to the local job; the worker may then acquire new public pages.
- VIN refinements may use only the original VIN, must not reset the 12-query/300-active-second budget and invalidate finalized analysis. No automatic decoder API or alternate VIN.

Подробный контракт: [справочник](../references/web-research.md).
