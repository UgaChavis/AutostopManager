# manager.j1_research_results — Результаты собственного J1 job

Основной модуль: [J1](../modules/J1.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: public_web; первичная база: primary URL lineage must be preserved. Исполнение: composed.

Вызов: native Manager MCP `j1_research_results`.

Входы: `job_id`, `query`, `cursor`, `limit`, `query_mode`.
Defaults: `{"cursor":0,"limit":20,"query":"","query_mode":"lexical"}`.
Обязательные facade поля: `job_id`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"job_id":"00000000000000000000000000000000","limit":10}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"job_id":null}
```

Выход: Legacy flat schema=autostop.j1.research.v1: ok/job_id/query/total/cursor/next_cursor/results[]. Результаты по фрагментам содержат page/fragment_kind/section/source_url.; Пустой `query` перечисляет документы. При непустом запросе `total` считает совпавшие строки, один `document_id` может встречаться у нескольких фрагментов. Совпадение только в названии возвращает заголовок и URL целого документа; VIN jobs: schema=autostop.j1.vin_research.v1; bounded local corpus search results and cursor; quotes/URLs are redacted while document lineage and revision remain available.; Точный режим возвращает query_mode и match_kind=body или title.
Ошибки и неполнота: ok=false/error.code: job_id_invalid/pagination_invalid/query_invalid_or_sensitive/job_not_found/j1_store_unavailable.; VIN jobs additionally return explicit error.code for scope, expiry, private runtime and lost ephemeral state; rejected input starts no network.; query_mode_public_only при точном режиме для задания VIN.

- job_id/document_id are 32 lowercase hexadecimal characters returned by J1; synthetic example IDs demonstrate shape and do not assert existing records.
- General J1 remains de-identified and rejects full VIN; separate VIN jobs permit only their original exact scoped VIN before expiry. Contacts, secrets and foreign VIN remain rejected; source URL lineage stays attached.
- Reads/searches the saved local job corpus; cursor pagination does not acquire new public pages.
- Local search does not authorize another network query; use add_queries on the same VIN job and its unchanged budget.
- Opening local state may initialize/migrate SQLite. VIN paths may delete expired private jobs, mark lost-state queue pointers failed and reconcile stopped-worker queries/documents/OCR before domain checks, including when VIN writes are disabled. This housekeeping performs no network or business writes.
- Whole-tool annotations: readOnlyHint=false, destructiveHint=true, idempotentHint=false, openWorldHint=false. Scoped domain deduplication/CAS/receipts do not make lifecycle housekeeping idempotent.
- Публичный query_mode=dtc или part_number проверяет целый идентификатор. У DTC сохраняются суффиксы. В артикулах нормализуются дефисы, пробелы и регистр. lexical сохраняет прежний полнотекстовый поиск.

Подробный контракт: [справочник](../references/web-research.md).
