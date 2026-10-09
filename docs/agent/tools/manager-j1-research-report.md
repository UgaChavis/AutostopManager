# manager.j1_research_report — Evidence ledger собственного J1 job

Основной модуль: [E15](../modules/E15.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: public_web; первичная база: primary URL lineage must be preserved. Исполнение: job_read.

Вызов: native Manager MCP `j1_research_report`.

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

Выход: Legacy flat schema=autostop.j1.report.v1: ok/job_id/status/profile/automotive_context/report.; report contains evidence_confidence/frequency/confirmed/hypotheses/alternative_causes/sources/duplicates/unavailable/limitations/read_only/fitment_confirmed/crm_written.; VIN jobs: schema=autostop.j1.vin_report.v1; collection_status/analysis_status/revision/finalized_revision/claims/conflicts/unknown_fields/sources/budget/stop_reason..
Ошибки и неполнота: ok=false/error.code: job_id_invalid/job_not_found/j1_store_unavailable, using report schema; unavailable/duplicate documents remain report limitations.; VIN jobs additionally return explicit error.code for scope, expiry, private runtime and lost ephemeral state; rejected input starts no network..

- job_id/document_id are 32 lowercase hexadecimal characters returned by J1; synthetic example IDs demonstrate shape and do not assert existing records.
- General J1 remains de-identified and rejects full VIN; separate VIN jobs permit only their original exact scoped VIN before expiry. Contacts, secrets and foreign VIN remain rejected; source URL lineage stays attached.
- Read-only report from saved local corpus; hypotheses and source counts are not a fabricated failure frequency or confirmed repair.
- VIN collection completion is not ready analysis; record_facts(finalize=true) seals a quiescent common revision. Later queries, evidence or OCR invalidate finality. Literal VIN match and source counts do not certify factory configuration.

Подробный контракт: [справочник](../references/web-research.md).
