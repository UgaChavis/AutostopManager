# manager.search_partsapi_category_index — search_partsapi_category_index

Справочный/отдельный контур; не automotive workflow

Основной модуль: outside; другие модули: нет.
Классификация: historical. Состояние реализации: implemented.
Источник: manager; первичная база: AutoStop versioned rules or explicit supplied inputs. Исполнение: local_read.

Вызов: native Manager MCP `search_partsapi_category_index`.

Входы: `query`, `intent_id`, `path`, `limit`.
Defaults: `{"intent_id":null,"limit":8,"path":null,"query":null}`.
Обязательные facade поля: нет; ограничения конкретной операции всё равно применяются.
Текущая inputSchema и проверка версии — [D1](../modules/D1.md).

Синтетический вход (форма, не утверждение о реальном автомобиле/артикуле):
```json
{}
```
Вход, отклоняемый схемой до исполнения инструмента:
```json
{"query":[]}
```

Выход: outcome/data/evidence/missing_fields/conflicts/warnings/execution (новые helpers); legacy поля сохраняются.
Ошибки и неполнота: invalid_input сохраняет ошибочный вход как отказ; empty/partial/unsupported не success; configuration/auth/quota/provider/parse ошибки различаются для сетевого источника.

- Назначение и полномочия определяет профильный контур; automotive smoke не выполняет записи.
- Исторический numeric category fixture не современное дерево TecDoc.

Подробный контракт: [справочник](../references/manager-runtime.md).
