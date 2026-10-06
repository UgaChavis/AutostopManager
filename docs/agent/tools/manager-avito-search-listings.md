# manager.avito_search_listings — Поиск объявлений Avito

Поиск объявлений Avito

Основной модуль: E10; ссылки: нет. Состояние реализации: implemented.
Источник: reefapi; первичная база: Avito listings via ReefAPI. Исполнение: network_read.

Вызов: native Manager MCP `avito_search_listings`.

Входы: `query`, `location`, `category`, `page`, `limit`, `price_min`, `price_max`, `delivery_only`, `dry_run`.
Defaults: `{"category":"zapchasti_i_aksessuary","delivery_only":false,"dry_run":false,"limit":50,"location":"krasnoyarsk","page":1,"price_max":null,"price_min":null}`.
Обязательные facade поля: `query`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"dry_run":true,"query":"DEMO деталь"}
```
Отрицательный вход:
```json
{"dry_run":true,"query":null}
```

Выход: Legacy flat: ok/source/verification/count/listings[]; provider_count/scanned_count/rejected_count/duplicate_count/unscanned_count.; Dry run adds dry_run/request and returns no observed listings..
Ошибки и неполнота: Legacy ok=false/source=avito/error string; optional provider_code/http_status/retryable. Input validation, api_key_missing/auth/quota/rate_limit/timeout/source-blocked and malformed_response remain distinct..

- dry_run=true gives verification=configuration_only and sends no request; live responses use provider_response_received.
- Price, condition, delivery and completeness are listing observations; seller confirmation and vehicle fitment remain separate.
- One bounded page, up to 50 normalized listings; rejected/deduplicated rows remain counted.

Подробный контракт: [справочник](../references/market-listings.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
