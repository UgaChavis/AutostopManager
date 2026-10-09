# manager.j1_research_document — Документ собственного J1 job

Основной модуль: [E15](../modules/E15.md); другие модули: нет.
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

Выход: Legacy flat schema=autostop.j1.research.v1: ok/job_id/document_id/url/canonical_url/title/kind/source/source_class/source_basis/source_tier.; search_snippet/search_rank/search_engines/language/extraction_method/duplicate_of/duplicate/discovered_at/retrieved_at/text/offset/total_chars/next_offset.; VIN jobs expose versioned document/page text, server-derived match_evidence_id and bounded OCR queue state..
Ошибки и неполнота: ok=false/error.code: identifier_invalid/pagination_invalid/document_not_found/j1_store_unavailable.; VIN page/OCR: unsupported for general jobs; scope, expiry, page and cached-document failures remain explicit..

- job_id/document_id refer to one returned J1 job; synthetic examples demonstrate shape only.
- General J1 remains de-identified; the separate VIN job permits only its original scoped VIN. Source URL lineage stays attached.
- Default document reads saved text; page/ocr options are VIN-only. OCR queues a deduplicated selected cached PDF page without network and changes document/analysis revision.
- Whole-tool annotations: readOnlyHint=false, destructiveHint=false, idempotentHint=true, openWorldHint=false. No factory truth follows from a literal VIN match.
- VIN PDFs: explicit registry A/B sources up to 24 MiB, others up to 8 млн байт. Returned VIN-bearing source URLs are masked; raw originals stay only in private tmpfs for provenance.

Подробный контракт: [справочник](../references/web-research.md).
