# aftermarket.denso — DENSO каталог

Известный номер → изделие и OE references

Основной модуль: [E4](../modules/E4.md); другие модули: [E5](../modules/E5.md), [E6](../modules/E6.md).
Классификация: active. Состояние реализации: implemented.
Источник: denso; первичная база: DENSO public catalog. Исполнение: network_read.

Вызов: native Manager MCP `public_aftermarket_catalog_lookup`.
provider: `denso`.

Входы: `provider`, `part_number`, `page_size`, `country`, `include_detail`, `dry_run`.
Defaults: `{"country":"europe","dry_run":false,"include_detail":true,"page_size":5}`.
Обязательные facade поля: `provider`, `part_number`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"dry_run":true,"part_number":"DEMO-ARTICLE-001","provider":"denso"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"part_number":"DEMO-ARTICLE-001","provider":null}
```

Выход: Legacy flat: ok/provider/operation/docs_url/role/request_plan/privacy/items/total_count.; DENSO adds offset/details/partial/requires_fallback/payload_status; optional errors retain source failures..
Ошибки и неполнота: ok=false/error for invalid request, unavailable configuration/transport or malformed response; empty/partial responses remain distinct..

- Selected public aftermarket catalogue read; dry_run=true only produces a request plan.
- Product vehicle lists, OE references and cross references are candidates and do not confirm a VIN-specific original part or stock.

Подробный контракт: [справочник](../references/public-aftermarket-catalogs.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
