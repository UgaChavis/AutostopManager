# manager.j1_research_cancel — Отменить собственный J1 job

Основной модуль: [E15](../modules/E15.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: public_web; первичная база: primary URL lineage must be preserved. Исполнение: job_write.

Вызов: native Manager MCP `j1_research_cancel`.

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

Выход: Legacy flat schema=autostop.j1.research.v1: ok/job_id/status..
Ошибки и неполнота: ok=false/error.code: job_id_invalid/job_not_found/j1_store_unavailable..

- job_id/document_id are 32 lowercase hexadecimal characters returned by J1; synthetic example IDs demonstrate shape and do not assert existing records.
- Public, de-identified corpus only; no private VIN/contact/CRM/Telegram/secret query. Source URL lineage stays attached to documents.
- Writes cancellation state for this exact local job; retained corpus is separate from job cancellation.

Подробный контракт: [справочник](../references/web-research.md).
