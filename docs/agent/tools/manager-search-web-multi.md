# manager.search_web_multi — Публичный поиск

Публичный поиск

Основной модуль: [E15](../modules/E15.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: public_web; первичная база: primary URL lineage must be preserved. Исполнение: network_read.

Вызов: native Manager MCP `search_web_multi`.

Входы: `query`, `limit`, `allowed_domains`, `providers`.
Defaults: `{"limit":5,"allowed_domains":null,"providers":null}`.
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

Выход: Legacy flat web gateway: ok/schema/capability/adapter/query/results/allowed_domains/provider_order/providers/fallback_used/read_only/vin_redacted.; results preserve URL/domain/title/snippet and provider metadata; optional error={code,retryable}.; Provider diagnostics distinguish empty results, unrecognized markup, access challenge and transport errors without exposing private exceptions..
Ошибки и неполнота: error.code distinguishes unsafe/empty query, unavailable gateway/provider and invalid provider response; retryable is explicit..

- Public search only: VINs are redacted and private contacts refused; preserve primary URL lineage across search providers.
- A hit/snippet is a lead; verify its primary document before exact technical or fitment conclusions.
- For a short part crosscheck begin with two queries and two target pages; expand only for a concrete missing fact.

Подробный контракт: [справочник](../references/web-research.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
