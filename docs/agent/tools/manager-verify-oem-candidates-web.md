# manager.verify_oem_candidates_web — Оценка веб-свидетельств номеров

Оценка веб-свидетельств номеров

Основной модуль: E6; ссылки: нет. Состояние реализации: implemented.
Источник: public_web; первичная база: primary URL lineage must be preserved. Исполнение: composed.

Вызов: native Manager MCP `verify_oem_candidates_web`.

Входы: `candidates`, `requested_part`, `make`, `model`, `model_year`, `engine`, `axle`, `side`, `position`, `live_search`, `max_candidates`, `max_results_per_candidate`, `timeout`.
Defaults: `{"axle":null,"candidates":null,"engine":null,"live_search":false,"make":null,"max_candidates":3,"max_results_per_candidate":3,"model":null,"model_year":null,"position":null,"requested_part":null,"side":null,"timeout":4}`.
Обязательные facade поля: нет; ограничения конкретной операции всё равно применяются.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{}
```
Отрицательный вход:
```json
{"candidates":null}
```

Выход: Legacy flat: ok/schema/mode/status/input_context/source_routes/candidate_evidence/conflicts/manual_actions/warnings/rules/privacy.; candidate_evidence includes oem_number/part_number/brand/query/search_url/evidence_status/evidence/sources/applicability_conditions/contradictions/fitment_confirmed..
Ошибки и неполнота: Malformed candidates/context are refused; search failures and missing evidence remain per-candidate evidence_status, warnings and manual actions..

- live_search=false plans source routes; live_search=true performs bounded public searches.
- Public web corroboration is not VIN-specific OEM confirmation; no CRM write and no private identifier/contact query.

Подробный контракт: [справочник](../references/partsapi.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
