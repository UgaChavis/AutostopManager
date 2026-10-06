# manager.assess_part_market — Оценка переданной рыночной выборки

Оценка переданной рыночной выборки

Основной модуль: E11; ссылки: нет. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: pure.

Вызов: native Manager MCP `assess_part_market`.

Входы: `article`, `observations`, `brand`, `target_region`.
Defaults: `{"brand":null,"target_region":"Красноярск"}`.
Обязательные facade поля: `article`, `observations`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"article":"DEMO","observations":[]}
```
Отрицательный вход:
```json
{"article":null,"observations":[]}
```

Выход: Legacy flat schema=PartMarketAssessmentV1: ok/read_only/target/status/accepted_offer_count/rejected_observation_count.; segments/rejected_observations/warnings; each accepted segment preserves source observations and market class..
Ошибки и неполнота: ok=false/error_code for invalid inputs; rejected observations are retained separately from accepted offers; empty/insufficient status is not a positive market conclusion..

- Pure assessment of supplied observations: no network, Store, CRM or experience reads.
- Public retail, procurement, used/contract and offer classes stay distinct; never infer fitment or buy a part from a median.

Подробный контракт: [справочник](../references/market-listings.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
