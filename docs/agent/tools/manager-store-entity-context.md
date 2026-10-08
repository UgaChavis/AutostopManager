# manager.store_entity_context — Точный контекст сущности Store

Основной модуль: [E9](../modules/E9.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: store; первичная база: AutoStop Store authoritative offer context. Исполнение: network_read.

Вызов: native Manager MCP `store_entity_context`.

Входы: `entity`, `entity_id`, `detail`.
Defaults: `{"detail":"summary"}`.
Обязательные facade поля: `entity`, `entity_id`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"detail":"summary","entity":"store_part","entity_id":"DEMO"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"entity":null,"entity_id":"DEMO"}
```

Выход: Store envelope format=store_agent_v1: ok/status/summary/items/changes/page/warnings/meta/generated_at.; page gives has_more/next_cursor/replay_cursor/limit; meta preserves source snapshot/principal context. Errors use summary.error_code/message/details..
Ошибки и неполнота: Unsupported entity or malformed query/filter/cursor/limit returns Store blocked/error envelope; transport/auth and source errors retain summary.error_code and sanitized meta.http_status..

- Private Store read; do not send returned business data to public web or persist it in the general source registry.
- entity is one of store_part/store_order/store_quote_request/store_batch/store_warehouse_operation/store_marketplace_listing/store_state/store_sourcing_offer.
- Use the live Store response for offer/stock/order claims; this operation makes no reservation, order or payment.
- Replace the synthetic entity_id with an ID returned by Store; the example does not assert an existing record.

Подробный контракт: [справочник](../references/store-api.md).
