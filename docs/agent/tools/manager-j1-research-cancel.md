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

Выход: Legacy flat schema=autostop.j1.research.v1: ok/job_id/status.; VIN jobs: schema=autostop.j1.vin_research.v1; updated collection_status and cancellation state; private partial corpus remains until expiry..
Ошибки и неполнота: ok=false/error.code: job_id_invalid/job_not_found/j1_store_unavailable.; VIN jobs additionally return explicit error.code for scope, expiry, private runtime and lost ephemeral state; rejected input starts no network..

- job_id/document_id are 32 lowercase hexadecimal characters returned by J1; synthetic example IDs demonstrate shape and do not assert existing records.
- General J1 remains de-identified and rejects full VIN; separate VIN jobs permit only their original exact scoped VIN before expiry. Contacts, secrets and foreign VIN remain rejected; source URL lineage stays attached.
- Writes cancellation state for this exact local job; retained corpus is separate from job cancellation.
- Cancellation request is not proof that an inflight operation has ended; read collection_status/inflight before finalize. Expiry deletes private evidence and forbids further network.

Подробный контракт: [справочник](../references/web-research.md).
