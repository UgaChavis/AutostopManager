# manager.search_offline_parts_catalogs — Поиск подготовленных локальных каталогов

Поиск подготовленных локальных каталогов

Основной модуль: [E4](../modules/E4.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: offline_catalog; первичная база: locally prepared licensed document. Исполнение: local_read.

Вызов: native Manager MCP `search_offline_parts_catalogs`.

Входы: `query`, `catalog_id`, `brand`, `limit`.
Defaults: `{"brand":null,"catalog_id":null,"limit":8}`.
Обязательные facade поля: `query`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"query":"DEMO деталь"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"query":null}
```

Выход: Legacy flat schema=offline_parts_catalog_search_v1: ok/candidate_only/fitment_rule/results[].; selected_catalogs/scanned_catalogs/scanned_bytes/result_limit_reached/search_incomplete/skipped_catalog_ids/skipped_catalog_count/partially_extracted_catalog_ids/warnings..
Ошибки и неполнота: ok=false with error for invalid/sensitive query, limit/catalog selection, unsafe or missing catalog root; empty results are separate from an incomplete scan..

- Reads selected local catalogue files; no network request.
- A file hit is a candidate only; preserve catalogue/locator and verify market, modification, dimensions and OE applicability independently.

Подробный контракт: [справочник](../references/offline-catalogs.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
