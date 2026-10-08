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

Выход: Legacy flat schema=autostop.j1.report.v1: ok/job_id/status/profile/automotive_context/report.; report contains evidence_confidence/frequency/confirmed/hypotheses/alternative_causes/sources/duplicates/unavailable/limitations/read_only/fitment_confirmed/crm_written..
Ошибки и неполнота: ok=false/error.code: job_id_invalid/job_not_found/j1_store_unavailable, using report schema; unavailable/duplicate documents remain report limitations..

- job_id/document_id are 32 lowercase hexadecimal characters returned by J1; synthetic example IDs demonstrate shape and do not assert existing records.
- Public, de-identified corpus only; no private VIN/contact/CRM/Telegram/secret query. Source URL lineage stays attached to documents.
- Read-only report from saved local corpus; hypotheses and source counts are not a fabricated failure frequency or confirmed repair.

Подробный контракт: [справочник](../references/web-research.md).
