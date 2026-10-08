# manager.avito_read_listing — Чтение одного объявления Avito

Основной модуль: [E10](../modules/E10.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: reefapi; первичная база: Avito listings via ReefAPI. Исполнение: network_read.

Вызов: native Manager MCP `avito_read_listing`.

Входы: `ad_id`, `dry_run`.
Defaults: `{"dry_run":false}`.
Обязательные facade поля: `ad_id`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"ad_id":"123456789","dry_run":true}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"ad_id":null,"dry_run":true}
```

Выход: Legacy flat: ok/source/verification/listing; dry run instead has dry_run/request..
Ошибки и неполнота: Legacy ok=false/source=avito/error string; optional provider_code/http_status/retryable. Input validation, api_key_missing/auth/quota/rate_limit/timeout/source-blocked and malformed_response remain distinct..

- dry_run=true gives verification=configuration_only and sends no request; live responses use provider_response_received.
- Price, condition, delivery and completeness are listing observations; seller confirmation and vehicle fitment remain separate.
- ad_id accepts a numeric ID of 6–20 digits or supported public Avito listing URL; the synthetic ID example is only a dry-run request.

Подробный контракт: [справочник](../references/market-listings.md).
