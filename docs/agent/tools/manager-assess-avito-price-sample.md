# manager.assess_avito_price_sample — Оценка переданной выборки Avito

Оценка переданной выборки Avito

Основной модуль: [E11](../modules/E11.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: pure.

Вызов: native Manager MCP `assess_avito_price_sample`.

Входы: `part_number`, `listings`, `condition`, `city`.
Defaults: `{"city":null,"condition":null}`.
Обязательные facade поля: `part_number`, `listings`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"listings":[],"part_number":"DEMO-ARTICLE-001"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"listings":[],"part_number":null}
```

Выход: Legacy flat schema=AvitoPriceSampleV1: ok/read_only/verification/status/target/input_count/accepted_count/excluded_count.; accepted_listings/excluded_listings/segments/independent_source_count/independent_source_market_median_price_rub/fitment_confirmed/availability_confirmed/warnings..
Ошибки и неполнота: ok=false/error_code for invalid input; excluded listing reasons and insufficient status do not count as successful market evidence..

- Pure supplied-listing assessment; zero network and no listing acquisition.
- Duplicate listings, condition and market segments are evaluated separately; Avito remains one source and independent_source_market_median_price_rub is null. Fitment and availability remain unconfirmed.

Подробный контракт: [справочник](../references/market-listings.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
