# manager.store_owner_capabilities — store_owner_capabilities

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: outside. Состояние реализации: implemented.
Источник: store; первичная база: AutoStop Store owner-scoped OpenAPI operations and input contracts (with in-memory cache). Исполнение: network_read.

Вызов: native Manager MCP `store_owner_capabilities`.

Входы: `query`, `limit`, `operation_id`.
Defaults: `{"limit":200,"operation_id":"","query":""}`.
Обязательные facade поля: нет; ограничения конкретной операции всё равно применяются.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"query":null}
```

Выход: Owner-scoped operations или точный input_contract/schema_hash; data_included=false, без бизнес-данных..
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- При холодном/просроченном кеше читает Store OpenAPI по HTTP с owner credential; тёплый in-memory cache может вернуть результат без сети.
- Список операций или input contract не выдаёт полномочия на бизнес-действие; полномочия и prerequisites проверяет конкретный исполнитель.
- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Наличие в общем native inventory не разрешает business action или чтение клиента.

Подробный контракт: [справочник](../references/store-api.md).
