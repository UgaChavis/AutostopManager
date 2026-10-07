# manager.store_search — Поиск сущностей Store

Поиск сущностей Store

Основной модуль: [E9](../modules/E9.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: store; первичная база: AutoStop Store authoritative offer context. Исполнение: network_read.

Вызов: native Manager MCP `store_search`.

Входы: `entity`, `query`, `filters`, `cursor`, `limit`.
Defaults: `{"cursor":null,"filters":null,"limit":25,"query":""}`.
Обязательные facade поля: `entity`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"entity":"store_part","limit":5,"query":"DEMO деталь"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"entity":null}
```

Выход: Store envelope format=store_agent_v1: ok/status/summary/items/changes/page/warnings/meta/generated_at.; page gives has_more/next_cursor/replay_cursor/limit; meta preserves source snapshot/principal context. Errors use summary.error_code/message/details..
Ошибки и неполнота: Unsupported entity or malformed query/filter/cursor/limit returns Store blocked/error envelope; transport/auth and source errors retain summary.error_code and sanitized meta.http_status..

- Private Store read; do not send returned business data to public web or persist it in the general source registry.
- entity is one of store_part/store_order/store_quote_request/store_batch/store_warehouse_operation/store_marketplace_listing/store_state/store_sourcing_offer.
- Use the live Store response for offer/stock/order claims; this operation makes no reservation, order or payment.

Подробный контракт: [справочник](../references/store-api.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
