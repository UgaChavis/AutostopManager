# manager.estimate_repair_work_cost — Составная оценка стоимости работ

Составная оценка стоимости работ

Основной модуль: [E13](../modules/E13.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: composed.

Вызов: native Manager MCP `estimate_repair_work_cost`.

Входы: `vehicle`, `vin`, `chassis`, `make`, `model`, `year`, `engine`, `transmission`, `work_items`, `complaint`, `city`, `quotes_json`, `auto_research`, `labor_time_policy`, `use_internal_experience`, `price_evidence`, `internal_experience_json`.
Defaults: `{"auto_research":true,"chassis":null,"city":"Красноярск","complaint":null,"engine":null,"internal_experience_json":null,"labor_time_policy":"public_only","make":null,"model":null,"price_evidence":null,"quotes_json":null,"transmission":null,"use_internal_experience":true,"vehicle":null,"vin":null,"work_items":null,"year":null}`.
Обязательные facade поля: нет; ограничения конкретной операции всё равно применяются.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"auto_research":false,"make":"DEMO","use_internal_experience":false,"work_items":["замена передних тормозных колодок"]}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"vehicle":[]}
```

Выход: Legacy flat: ok/mode/read_only/crm_write_allowed/vehicle_context/normalized_operations/operation_estimates.; labor_time_sample/labor_time_analysis/labor_time_range_hours/labor_time_average_hours/labor_time_confidence/labor_time_cross_check/overlap_adjustments.; sources_checked/pricing_basis/market_sample/market_average_rub/russia_average_rub/autostop_price_rub/total_works_rub/recommended_total_works_rub/confidence/decision_confidence.; missing_context/next_actions/manager_summary/formula/warnings/privacy/research/playbook/source_catalog..
Ошибки и неполнота: ok=false/error/errors/limits for bounded-input failure; failed/conflicted or incompatible ready price_evidence is rejected before hidden research..

- Composed legacy estimator: auto_research=true and use_internal_experience=true are real defaults and can perform public research or private local experience reads.
- Bound ready price_evidence reuses supplied observations/labor and avoids hidden collection; auto_research=false plus use_internal_experience=false disables both acquisition branches.
- No CRM write. Public work prices, labor-time evidence, overlap adjustments and final pricing policy are separate claims; missing evidence lowers confidence.

Подробный контракт: [справочник](../references/work-pricing.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
