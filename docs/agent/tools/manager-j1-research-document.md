# manager.j1_research_document — Документ собственного J1 job

Документ собственного J1 job

Основной модуль: E15; ссылки: нет. Состояние реализации: implemented.
Источник: public_web; первичная база: primary URL lineage must be preserved. Исполнение: job_read.

Вызов: native Manager MCP `j1_research_document`.

Входы: `job_id`, `document_id`, `offset`, `max_chars`.
Defaults: `{"max_chars":8000,"offset":0}`.
Обязательные facade поля: `job_id`, `document_id`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"document_id":"11111111111111111111111111111111","job_id":"00000000000000000000000000000000","max_chars":1200}
```
Отрицательный вход:
```json
{"document_id":"DEMO","job_id":null}
```

Выход: Legacy flat schema=autostop.j1.research.v1: ok/job_id/document_id/url/canonical_url/title/kind/source/source_class/source_basis/source_tier.; search_snippet/search_rank/search_engines/language/extraction_method/duplicate_of/duplicate/discovered_at/retrieved_at/text/offset/total_chars/next_offset..
Ошибки и неполнота: ok=false/error.code: identifier_invalid/pagination_invalid/document_not_found/j1_store_unavailable..

- job_id/document_id are 32 lowercase hexadecimal characters returned by J1; synthetic example IDs demonstrate shape and do not assert existing records.
- Public, de-identified corpus only; no private VIN/contact/CRM/Telegram/secret query. Source URL lineage stays attached to documents.
- Reads a bounded saved document excerpt with primary URL, extraction method and retrieval time; no live page fetch.

Подробный контракт: [справочник](../references/web-research.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
