# partsapi.searchArticles — searchArticles

Вызвать searchArticles; TecDoc article search by any part-number form.

Основной модуль: [E4](../modules/E4.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: partsapi_ru; первичная база: declared_by_provider; VIN/TecDoc/maintenance/AUTONORMS namespaces. Исполнение: network_read.

Вызов: native Manager MCP `partsapi_catalog_lookup`.
operation: `search_articles`.
api_method: `searchArticles`.

Входы: `part_number`.
Defaults: `{"lang_id":16}`.
Обязательные facade поля: `operation`.
Fingerprint проверяет [manifest](../manager_mcp_catalog.json); полная inputSchema берётся из регистрации и `tools/list`, в CRM — native_schemas того же bundle.

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"dry_run":true,"operation":"search_articles","part_number":"DEMO-ARTICLE-001"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"operation":[]}
```

Выход: article_candidates с номером, брендом и article ID; готовность getArticle и поддерживаемые article-ID операции указаны отдельно; в summary сохраняются counts и обрезка, без raw payload.
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- Article/cross/OE-reference не подтверждают оригинальный номер конкретного VIN.
- API method отличается от facade operation; key назначает сервер, secrets не принимаются.
- TecDoc/maintenance/AUTONORMS ID namespace не взаимозаменяемы; указанные IDs в примере синтетические.
- searchArticles не возвращает SUP_ID по своему контракту. Для полного getArticle нужен supplier ID из связанного getArticles либо подтверждённого входа; из названия бренда его не выводи. При ART_ID доступны article_criteria, getArticleMedia и article_crosses.

Подробный контракт: [справочник](../references/partsapi.md). Карточка и inputSchema согласованы versioned export; ручной цвет не является результатом проверки.
