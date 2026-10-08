# manager.assess_part_market — Оценка переданной рыночной выборки

Основной модуль: [E11](../modules/E11.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: pure.

Вызов: native Manager MCP `assess_part_market`.

Входы: `article`, `observations`, `brand`, `target_region`.
Defaults: `{"brand":null,"target_region":"Красноярск"}`.
Обязательные facade поля: `article`, `observations`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"article":"DEMO","observations":[]}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"article":null,"observations":[]}
```

Выход: Legacy flat schema=PartMarketAssessmentV1: ok/read_only/target/status/accepted_offer_count/rejected_observation_count.; segments/rejected_observations/warnings; each accepted segment preserves source observations and market class..
Ошибки и неполнота: ok=false/error_code for invalid inputs; rejected observations are retained separately from accepted offers; empty/insufficient status is not a positive market conclusion..

- Pure assessment of supplied observations: no network, Store, CRM or experience reads.
- Public retail, procurement, used/contract and offer classes stay distinct; never infer fitment or buy a part from a median.

Подробный контракт: [справочник](../references/part-market.md).
