# manager.j1_research_record_facts — Основания VIN-исследования J1

Запись цитируемых утверждений и атомарная финализация анализа

Основной модуль: [E15](../modules/E15.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: public_web; первичная база: primary URL lineage must be preserved. Исполнение: job_write.

Вызов: native Manager MCP `j1_research_record_facts`.

Входы: `job_id`, `expected_revision`, `facts`, `idempotency_key`, `finalize`.
Defaults: `{"finalize":false}`.
Обязательные facade поля: `job_id`, `expected_revision`, `facts`, `idempotency_key`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"expected_revision":0,"facts":[],"finalize":true,"idempotency_key":"FACTS_DEMO_0001","job_id":"00000000000000000000000000000000"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"expected_revision":-1,"facts":[],"idempotency_key":"FACTS_DEMO_0001","job_id":"00000000000000000000000000000000"}
```

Выход: schema=autostop.j1.vin_research.v1: accepted claims, common revision and analysis_status; conflicts/unknowns retained.; VIN-bearing source_url is masked in returned claims; the raw original stays in private tmpfs for worker provenance..
Ошибки и неполнота: ok=false/error.code: vin_research_disabled, evidence/quote/revision/basis/match validation, collection not stopped, idempotency/scope/expiry failures..

- Always registered; AUTOSTOP_J1_VIN_RESEARCH_ENABLED defaults to 0 and rejects this write with vin_research_disabled before recording facts or finalizing analysis. Independent expiry cleanup still applies.
- Effectful temporary-state write, idempotent, no network and no business writes.
- Each claim cites a same-job document revision and exact quote; inference requires basis_claim_ids.
- vin_specific requires server-derived match_evidence_id before redaction. VIN binding is not semantic or factory truth.
- finalize=true requires the collection to have actually stopped and seals the common evidence revision. New documents, claims or OCR invalidate finality.
- support=corroborated requires current basis_claim_ids with matching field/value/unit/relationship and at least two distinct content clusters across the new claim and its matching bases; duplicate_of copies count once. Otherwise fact_corroboration_basis_required; this check does not certify semantic truth or independent data origin.

Подробный контракт: [справочник](../references/j1-vin-research.md).
