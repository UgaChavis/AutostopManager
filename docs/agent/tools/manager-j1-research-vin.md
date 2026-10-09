# manager.j1_research_vin — Самостоятельное VIN-исследование J1

Исследование одного VIN без предварительного декодера

Основной модуль: [E15](../modules/E15.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: public_web; первичная база: primary URL lineage must be preserved. Исполнение: job_write.

Вызов: native Manager MCP `j1_research_vin`.

Входы: `vin`, `idempotency_key`.
Defaults: `{}`.
Обязательные facade поля: `vin`, `idempotency_key`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"idempotency_key":"START_DEMO_0001","vin":"1HGCM82673A000000"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"idempotency_key":"START_DEMO_0001","vin":null}
```

Выход: schema=autostop.j1.vin_research.v1: job_id, collection_status, analysis_status, revision, expiry and cumulative budget..
Ошибки и неполнота: ok=false/error.code: vin_research_disabled, invalid VIN/key, scope/runtime/budget/job errors; no network on rejected input..

- Always registered but disabled by default; enabling requires an explicitly authorized runtime change.
- One exact VIN/job/recipient/TTL scope. No E2/E4 prerequisite, automatic decoder fallback, decoder API, CRM/Store or external LLM.
- Collection is not analysis; the current agent reads documents and records cited claims before finalize.
- 12 queries/60 documents/6 de-identified browser pages/12 OCR pages/300 active worker seconds/24h TTL; private state only in checked tmpfs.
- VIN PDF limit: 24 MiB only for explicit server-registry A/B sources; otherwise 8 млн байт. Extracted text is limited to 2 MiB and 1000 pages.

Подробный контракт: [справочник](../references/j1-vin-research.md).
