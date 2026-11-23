# manager.j1_research_document — Документ собственного J1 job

Основной модуль: [J1](../modules/J1.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: public_web; первичная база: primary URL lineage must be preserved. Исполнение: job_write.

Вызов: native Manager MCP `j1_research_document`.

Входы: `job_id`, `document_id`, `offset`, `max_chars`, `page`, `ocr`.
Defaults: `{"max_chars":8000,"ocr":false,"offset":0,"page":null}`.
Обязательные facade поля: `job_id`, `document_id`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"document_id":"11111111111111111111111111111111","job_id":"00000000000000000000000000000000","max_chars":1200}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"document_id":"DEMO","job_id":null}
```

Выход: Legacy flat schema=autostop.j1.research.v1: ok/job_id/document_id/url/canonical_url/title/kind/source/source_class/source_basis/source_tier.; search_snippet/search_rank/search_engines/language/extraction_method/duplicate_of/duplicate/discovered_at/retrieved_at/text/offset/total_chars/next_offset.; Публичный page возвращает один фрагмент с page/fragment_kind/section/source_url и previous_page/next_page. Для неполной страницы PDF возвращается fragment_truncated=true. VIN jobs expose versioned document/page text, server-derived match_evidence_id and bounded OCR queue state.; Смешанный публичный PDF сохраняет fragment_extraction_method для страницы и pdf_limitations при неполном извлечении.
Ошибки и неполнота: ok=false/error.code: identifier_invalid/pagination_invalid/document_not_found/j1_store_unavailable.; Публичный `document_page_unavailable` означает, что запрошенный фрагмент не сохранён. VIN-режим возвращает ошибки области, срока действия, страницы и OCR.

- job_id/document_id refer to one returned J1 job; synthetic examples demonstrate shape only.
- General J1 remains de-identified; the separate VIN job permits only its original scoped VIN. Source URL lineage stays attached.
- Без page читается сохранённый плоский текст. Публичный page выбирает страницу PDF или раздел HTML, offset относится к выбранному фрагменту. OCR ставит выбранную cached PDF-страницу в очередь только в VIN-режиме, без новой сети, с изменением revision.
- Whole-tool annotations: readOnlyHint=false, destructiveHint=true, idempotentHint=false, openWorldHint=false. Selected-page OCR is deduplicated, but lifecycle housekeeping can write again. No factory truth follows from a literal VIN match.
- VIN PDFs: explicit registry A/B sources up to 24 MiB, others up to 8 млн байт. Returned VIN-bearing source URLs are masked; raw originals stay only in private tmpfs for provenance.
- Opening local state may initialize/migrate SQLite. VIN paths may delete expired private jobs, mark lost-state queue pointers failed and reconcile stopped-worker queries/documents/OCR before domain checks, including when VIN writes are disabled. This housekeeping performs no network or business writes.

Подробный контракт: [справочник](../references/web-research.md).
