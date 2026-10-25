# manager.j1_research_status — Статус собственного J1 job

Основной модуль: [E15](../modules/E15.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: public_web; первичная база: primary URL lineage must be preserved. Исполнение: composed.

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
- Opening local state may initialize/migrate SQLite. VIN paths may delete expired private jobs, mark lost-state queue pointers failed and reconcile stopped-worker queries/documents/OCR before domain checks, including when VIN writes are disabled. This housekeeping performs no network or business writes.
- Whole-tool annotations: readOnlyHint=false, destructiveHint=true, idempotentHint=false, openWorldHint=false. Scoped domain deduplication/CAS/receipts do not make lifecycle housekeeping idempotent.
- Native status dispatch uses a separate bounded pool with lazily started workers: one worker, one waiting call, fixed 30-second caller wait. It shares no automation, provider-read or web-worker capacity. This serializes only status workers with each other, not all J1 operations; routing, SQLite init and VIN lifecycle housekeeping remain unchanged.
- Executor refusal/failure returns the generic J1 transport envelope `schema=autostop.j1.research.v1`, `ok=false`, `error={code:"j1_status_executor_unavailable",reason:...}`. The finite reasons are `busy`, `wait_timeout`, `execution_failed`, `executor_unavailable`, `executor_draining`; unknown executor reasons map to `executor_unavailable`. No job ID/input is echoed and no routing DB is read to classify an unstarted call. Normal general/VIN responses and domain errors retain their original schemas.
- `stage`, `adapter_started` and `adapter_may_continue` describe dispatch; `outcome_uncertain=true` for any started failed/timed-out operation, including a completed worker exception that may already have changed housekeeping state. It is false only before dispatch. Caller timeout/cancellation does not stop a worker or roll back its effects; the real admission slot remains occupied until completion, while an abandoned queued call does not start. `retryable=false` and `retry_attempted=false`: no automatic retry after an unknown outcome.

Подробный контракт: [справочник](../references/web-research.md).
