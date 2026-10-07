# manager.resolve_vin_oem_parts — Составной ограниченный поиск каталоговых кандидатов

Составной ограниченный поиск каталоговых кандидатов

Основной модуль: [E4](../modules/E4.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: partsapi_ru; первичная база: declared_by_provider; VIN/TecDoc/maintenance/AUTONORMS namespaces. Исполнение: composed.

Вызов: native Manager MCP `resolve_vin_oem_parts`.

Входы: `identifier`, `requested_part`, `make`, `model`, `model_year`, `engine`, `transmission`, `market`, `drivetrain`, `axle`, `side`, `position`, `inner_outer`, `live_vpic`, `live_partsapi_identity`, `live_partsapi_oem`, `max_live_calls`, `max_candidates`, `timeout`, `max_attempts`, `partsapi_category_index`, `tecdoc_tree_node_id`, `vehicle_type`, `dry_run`, `crm_context`, `vehicle_identity`, `identifier_type`.
Defaults: `{"axle":null,"crm_context":null,"drivetrain":null,"dry_run":false,"engine":null,"identifier_type":"auto","inner_outer":null,"live_partsapi_identity":false,"live_partsapi_oem":false,"live_vpic":true,"make":null,"market":null,"max_attempts":1,"max_candidates":3,"max_live_calls":3,"model":null,"model_year":null,"partsapi_category_index":null,"position":null,"side":null,"tecdoc_tree_node_id":null,"timeout":20.0,"transmission":null,"vehicle_identity":null,"vehicle_type":null}`.
Обязательные facade поля: `identifier`, `requested_part`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"dry_run":true,"identifier":"WVWZZZ1KZAW000001","live_vpic":false,"requested_part":"передние колодки 1 комплект"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"dry_run":true,"identifier":null,"requested_part":"передние колодки 1 комплект"}
```

Выход: Legacy flat schema=VinOemResolution: ok/mode/status/identifier/identity/part_intent.; tecdoc_vehicle/tecdoc_vehicle_fallback/category_resolution/readiness/article_candidates/article_candidate_count; oem_candidates/candidate_count; tecdoc_lookup_outcome/oem_lookup_outcome/enrichment.; calls/source_failures/call_count/live_call_count/max_live_calls/blockers/manual_actions/crm_writeback_gate/privacy..
Ошибки и неполнота: Invalid input or ready identity binding stops before providers; catalogue configuration/auth/quota/provider failures remain in calls/source_failures/blockers and structured status..

- Read-only catalogue research; no CRM write and no automatic order.
- Article candidates and TecDoc modification agreement do not establish the original part for this VIN; current PartsAPI methods do not return authoritative VIN-specific OEM numbers.
- dry_run=true makes configured requests unverified and skips live calls; live_vpic defaults true outside dry run.

Подробный контракт: [справочник](../references/partsapi.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
