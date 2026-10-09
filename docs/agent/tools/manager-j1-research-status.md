# manager.j1_research_status — Статус собственного J1 job

Основной модуль: [E15](../modules/E15.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: public_web; первичная база: primary URL lineage must be preserved. Исполнение: job_read.

Вызов: native Manager MCP `j1_research_status`.

Входы: `job_id`.
Defaults: `{}`.
Обязательные facade поля: `job_id`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"job_id":"00000000000000000000000000000000"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"job_id":null}
```

Выход: Legacy flat schema=autostop.j1.research.v1: ok/job_id/status/objective/created_at/updated_at/max_pages/profile/automotive_context/query_plan.; pages_total/pages_fetched/pages_failed/pages_unavailable/pages_duplicates/browser_pages_attempted/browser_pages_remaining/queries_total/queries_done/queries_failed/page_failures/search_failures/coverage/query_suggestions/error.; VIN jobs: schema=autostop.j1.vin_research.v1; collection_status/analysis_status/revision/expiry, budget and counts. Collection completion is separate from finalized analysis..
Ошибки и неполнота: ok=false/error.code: job_id_invalid/job_not_found/j1_store_unavailable; worker errors remain in job status/error.; VIN jobs additionally return explicit error.code for scope, expiry, private runtime and lost ephemeral state; rejected input starts no network..

- job_id/document_id are 32 lowercase hexadecimal characters returned by J1; synthetic example IDs demonstrate shape and do not assert existing records.
- General J1 remains de-identified and rejects full VIN; separate VIN jobs permit only their original exact scoped VIN before expiry. Contacts, secrets and foreign VIN remain rejected; source URL lineage stays attached.
- Reads local job state; this operation itself does not search or fetch pages.
- VIN state exists only in private checked tmpfs; missing state after host reboot is vin_ephemeral_state_lost, never reconstructed from the general stub.

Подробный контракт: [справочник](../references/web-research.md).
