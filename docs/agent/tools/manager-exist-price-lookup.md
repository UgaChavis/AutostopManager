# manager.exist_price_lookup — Публичная цена Exist

Основной модуль: [E9](../modules/E9.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: exist; первичная база: Exist public retail. Исполнение: network_read.

Вызов: native Manager MCP `exist_price_lookup`.

Входы: `part_number`, `brand`, `pid`, `office_id`, `max_candidates`, `max_offers`, `include_more_offers`, `dry_run`.
Defaults: `{"brand":null,"dry_run":false,"include_more_offers":false,"max_candidates":5,"max_offers":10,"office_id":905,"pid":null}`.
Обязательные facade поля: `part_number`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"dry_run":true,"part_number":"DEMO-ARTICLE-001"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"dry_run":true,"part_number":null}
```

Выход: Legacy flat: ok/provider/operation/docs_url/role/office/benchmark_kind/requires_confirmation/request_plan/privacy.; search_suggestions/candidates/needs_disambiguation/selected_item/items/total_offers; optional error..
Ошибки и неполнота: ok=false/error for invalid request, unavailable provider/auth/transport or malformed payload; selected-item disambiguation is separate from price availability..

- Exist office 905 is a public retail benchmark, not a procurement offer, stock reservation or confirmed delivery.
- dry_run=true is a request plan only; live response does not establish vehicle fitment.

Подробный контракт: [справочник](../references/store-api.md).
