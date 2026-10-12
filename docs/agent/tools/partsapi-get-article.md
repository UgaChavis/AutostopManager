# partsapi.getArticle — getArticle

Вызвать getArticle; TecDoc article details by article number and supplier ID.

Основной модуль: [E4](../modules/E4.md); другие модули: нет.
Классификация: active. Состояние реализации: implemented.
Источник: partsapi_ru; первичная база: declared_by_provider; VIN/TecDoc/maintenance/AUTONORMS namespaces. Исполнение: network_read.

Вызов: native Manager MCP `partsapi_catalog_lookup`.
operation: `article`.
api_method: `getArticle`.

Входы: `part_number`, `supplier_id`.
Defaults: `{"lang_id":16}`.
Обязательные facade поля: `operation`.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{"dry_run":true,"operation":"article","part_number":"DEMO-ARTICLE-001","supplier_id":"42"}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"operation":[]}
```

Выход: article_candidates with article ID/part number/brand and typed oe_references from OEM_NUMBERS; criteria from ARTICLE_CRITERIA preserve labels, values and units; OEM candidates are not inferred by generic recursion.
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- Article/cross/OE-reference не подтверждают оригинальный номер конкретного VIN.
- API method отличается от facade operation; key назначает сервер, secrets не принимаются.
- TecDoc/maintenance/AUTONORMS ID namespace не взаимозаменяемы; указанные IDs в примере синтетические.
- OE references are not directed supersession and do not confirm VIN fitment; the aftermarket article number remains separate.
- SUP_ID берётся из связанного getArticles/подтверждённого входа; searchArticles его не обещает. Если ID сохранён из запроса, supplier_id_source явно отмечает происхождение; это не поле ответа провайдера и не VIN-применимость.
- completeness_scope=catalog_response описывает полноту каталожных полей, а не точность комплектации автомобиля.

Подробный контракт: [справочник](../references/partsapi.md).
